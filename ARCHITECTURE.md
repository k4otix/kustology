# Architecture

A 400-foot view of the codebase, oriented for new contributors.

## Layout

```
src/kustology/
  bridge.py          # .NET CLR init, loads Kusto.Language.dll via pythonnet
  core.py            # KustoQuery wrapper; to_ir() is the tier-1 → tier-2 seam
  services.py        # Public entry points: parse(), format_query(), validate()
  reflection.py      # Runtime introspection of Kusto.Language for func classification
  cli.py             # Command-line interface — kustology parse/format/validate/version
  ir/                # Tier-2: pydantic IR (opt-in via [ir] extras)
    builder.py       # Walks .NET syntax tree → QueryIR; dispatch tables for operators/expressions
    query.py         # Operator and pipeline node models
    expr.py          # Expression node models
    binder.py        # SchemaAttacher: provenance (ColumnRef.table, origins)
    walk.py          # Generic IR traversal: walk() and find_all()
    transforms.py    # semantic_hash, canonicalization, SEMANTIC_HASH_SCHEME
    llm_view.py      # to_llm_dict — compact JSON-safe rendering for LLMs
    analyzers.py     # The Finding vocabulary for IR-driven static analysis
    types.py         # Kusto type enum
    spans.py         # Source location tracking
    _normalize.py    # canonical() — backs Expr.canonical_form
    _builder_helpers.py  # Stateless .NET-node helpers used by IRBuilder
    _guard.py        # Optional-dependency guard: clear error without [ir] extras
  utils/             # Tier-1 helpers, re-exported from kustology.utils
    analysis.py      # AST-level analysis: table refs, operator stats,
                     # find_time_expressions, replace_table
    walker.py        # KustoWalker + iter_elements: primitive AST traversal
    schema_state.py  # build_global_state: Python schema dict → .NET GlobalState
  bin/               # Bundled Kusto.Language.dll + VERSION.txt (SHA-256 pinned)

scripts/             # Tooling
  audit_syntax_kinds.py     # Coverage gate: HANDLED_*_KINDS vs the DLL's kinds
  mine_corpus.py            # Unknown-node census over the corpus (CI gate)
  verify_corpus.py          # Full build+enrich pass over a private corpus
  extract_complex_corpus.py # Rewrites only the fixtures listed in
                            # RELATIVE_PATHS, in tests/fixtures/complex_queries/,
                            # from published Azure-Sentinel analytic rules. It
                            # deletes nothing: the rest are hand-written
                            # synthetics and the script does not know they exist
  sample_sentinel_corpus.py, extract_sentinel_schemas.py
  verify_dll.py, refresh_dll.py   # DLL provenance and refresh

tests/               # pytest suite
  ir/                # IR-specific tests
    test_binder_oracle.py   # Our result_schema vs Microsoft's ResultType
    test_complex_harness.py # Parametrized over tests/fixtures/complex_queries
  fixtures/          # complex_queries/ (extracted + hand-written, see
                     # extract_complex_corpus.py above), sentinel_sample/
                     # (gitignored), syntax_kinds_baseline.json
```

## Tiers

**Tier 1** — `kustology` top-level surface (`bridge`, `services`, `core`, `utils`,
`reflection`, `cli`). Public API for callers and the CLI, and on a stabilization
track — but this is a `0.y` line, so pre-1.0 Tier 1 may still break at a minor.
0.2.0 did: `get_operator_chain()` stopped returning the source table as element
0, so a consumer indexing from 1 to skip it now skips a real operator. In the
same release `get_time_range()` became `find_time_expressions()`, which is the
softer kind of break — the old name is kept as a deprecated alias.

**Tier 2** — `kustology.ir.*`. Pydantic IR with semantic enrichment. On a pre-1.0
track until the IR survives one Kusto.Language.dll upgrade cycle without breaking. Minor
breaking changes are possible at minor versions; each is called out in CHANGELOG.md.

See README.md "Versioning and stability" for what counts as breaking, and for the
three independent version tags (`__version__`, `IR_SCHEMA_VERSION`,
`SEMANTIC_HASH_SCHEME`).

## Where to add things

**A new tabular operator** (e.g. `mv-apply`, `partition`):

1. Add an IR node class in `src/kustology/ir/query.py`.
2. Add its `SyntaxKind` string to `IRBuilder.HANDLED_OPERATOR_KINDS` in
   `src/kustology/ir/builder.py`.
3. Add a dispatch branch in `IRBuilder._visit_operator()` that reads the .NET
   node's attributes and constructs your IR node. Probe attribute names with:

   ```python
   from kustology.bridge import KustoCode
   from Kusto.Language import GlobalState
   code = KustoCode.ParseAndAnalyze("T | <your-operator>", GlobalState.Default)
   # inspect type(node).__name__ and dir(node)
   ```

4. Your operator gets its **output schema** from Microsoft with no rule to
   write. `_visit_operator()` in `src/kustology/ir/builder.py` wraps every
   operator's dispatch and stamps `<node>.ResultType` onto `result_schema`
   in one place, so a new operator gets the binder's answer for free
   instead of a hand-written per-operator rule — every `Operator` also
   inherits `hints` (the `hint.*` named parameters) from the same wrapper.
   Both fields are volatile: they are in `transforms._VOLATILE_FIELDS` and
   so never reach `semantic_hash`.

   What you owe, in `src/kustology/ir/binder.py`, is **provenance**: does
   your operator reshape *which table* a column comes from? `join` /
   `lookup`, `union` and `search` do — they bring new sources into scope —
   and each needs a structural branch in
   `SchemaAttacher._walk_operator_provenance()`.
   If your operator just passes its input scope through unreshaped, the
   generic fallback already fills its expressions and walks its
   sub-pipelines, and there is nothing to add. Either way, add a row to
   `MATRIX` in `tests/ir/test_binder_oracle.py` naming the construct your
   operator exercises — the dict-path leg runs the full `MATRIX`
   automatically, checking your operator's `result_schema` against
   Microsoft's `ResultType` column-by-column and in order. Add the id to
   `BOUND_LEG_IDS` too if you want the bound leg's sampled run to cover it
   as well.
5. Add a minimal `.kql` fixture under `tests/fixtures/complex_queries/`. The
   parametrized harness in `tests/ir/test_complex_harness.py` picks it up
   automatically, and so does the oracle's corpus run in
   `tests/ir/test_binder_oracle.py`, on both legs. There is no xfail list to
   file a wrong shape under: Microsoft's `ResultType.IsOpen` tells the
   oracle when to expect `result_schema=None` instead of an exact match,
   and that is the only leniency either leg grants.
6. Regenerate the baseline:
   `python scripts/audit_syntax_kinds.py --update-baseline`.

If the operator's inner structure is genuinely not worth modelling yet, the
honest fallback is a single `raw_text` field plus a class docstring saying
what is inside the string and what that costs — see `ScanOp` and the seven
operators it names. Do **not** declare typed fields you cannot populate: a
declared-but-unfilled field reads as implemented, and
`docs/superpowers/reports/2026-08-20-stub-sweep.md` is the record of what
that cost last time.

**A new IR expression** (e.g. a new literal kind, a new operator shape):

0. If what you are adding is a **name**, check the three that already
   exist before adding a fourth. A `let`-bound scalar in expression
   position is a `LetValueRef` (`let n = 5; T | where a > n`), a
   `let`-bound table in source position is a `LetRef`, and a row-scope
   name is a `ColumnRef`. `LetValueRef` is the pattern to copy for any
   name that is neither a table nor a column: it is deliberately *not* a
   `ColumnRef` subclass, because the binder places columns by
   `isinstance` and a subclass would inherit the resolution the node
   exists to prevent. Which of the three a name becomes is decided in
   `IRBuilder` from the `let` statements alone, without the binder, so
   the classification — and therefore `semantic_hash` — does not depend
   on whether a schema was passed.
1. Add the model in `src/kustology/ir/expr.py` (or reuse `LiteralExpr` if
   it's just a new `literal_kind`).
2. Add the class to the `AnyExpr` union in `expr.py` **and** to `__all__` in
   `src/kustology/ir/__init__.py`. Omitting either half produces surface
   that looks implemented and is not — both directions shipped in v0.1.0
   (see `docs/superpowers/reports/2026-08-20-stub-sweep.md`).
3. Add its kind to `IRBuilder.HANDLED_EXPR_KINDS`.
4. Add a dispatch branch in `IRBuilder._visit_expr()`.
5. Add a render branch to `canonical()` in `src/kustology/ir/_normalize.py`,
   which backs `Expr.canonical_form`. That function ends in a silent
   fallthrough — `raw_text` if the node has one, otherwise a bare `"?"` —
   so a missing branch degrades quietly rather than raising: 11 `Expr`
   types had fallen through it, rendering `-X > 1`, `D.a == 1` and
   `toscalar(...) > 1` all as the same string `"? > 1"`.

   Steps 2 and 5 are the three hand-maintained class lists in the IR, and
   `tests/ir/test_canonical_coverage.py` rebuilds each one by
   introspection and diffs it against what is written, so forgetting any
   of them fails CI rather than shipping.
6. Apply the **lossy-lowering check**. If the node is reachable from more
   than one KQL construct, it needs a field naming which one — this is a
   `semantic_hash` correctness requirement, not style, and it is why
   `SetMembership.op` and `Exists.op` exist. See "Lossy lowering: a
   populated node can still lose information" in `AGENTS.md`.
7. Regenerate the baseline.

**A new CLI subcommand**:

1. Add the subparser + handler in `src/kustology/cli.py`.
2. Add subprocess-based tests in `tests/test_cli.py` covering happy path,
   error path, and `--json` output shape if applicable.
3. Document the subcommand in the README's CLI section.

## The invariants, and what enforces each one

Most of this codebase's rules are enforced by a specific file rather than by
review. When a change of yours goes red, this table says what it was
protecting.

| Invariant | Enforced by |
| --- | --- |
| Every `.NET` member name `src/` reads exists in the assembly | `tests/test_reflection_audit.py` |
| Every parser `SyntaxKind` is handled or explicitly skipped | `tests/test_coverage_audit.py` + `scripts/audit_syntax_kinds.py --check` |
| Every kind in `HANDLED_OPERATOR_KINDS` actually builds from real KQL | `tests/ir/test_handled_kinds_smoke.py` |
| `canonical()`, `AnyExpr` and `ir.__all__` list every `Expr` subclass | `tests/ir/test_canonical_coverage.py` |
| Our `result_schema` equals Microsoft's `ResultType`, in order | `tests/ir/test_binder_oracle.py` (bound leg and dict-path leg) |
| `semantic_hash` splits queries that differ and merges those that don't | `tests/ir/test_hash_battery.py` |
| `semantic_hash` does not move when a schema is supplied | `tests/ir/test_semantic_hash_bind_invariance.py` |
| No IR model holds a live `System.Object` | `tests/ir/test_ast_isolation.py` |
| `IR_SCHEMA_VERSION` / `SEMANTIC_HASH_SCHEME` never move silently | `tests/ir/test_schema_tags.py` |
| The corpus produces no `Unknown*` nodes | `scripts/mine_corpus.py` (the `corpus-regression` CI job) |
| Every example still runs | `tests/test_examples.py` |
| Fractional literals survive a comma-decimal locale | `tests/test_culture.py`, plus the `de-DE` / `fr-FR` CI matrix |
| The bundled DLL is the pinned one | `scripts/verify_dll.py` |

## Bridge / .NET interop

`bridge.py` does runtime CLR initialization. It auto-detects `DOTNET_ROOT` from
Homebrew, apt, the Microsoft installer, and `~/.dotnet`. If everything fails, it
raises `RuntimeError` with the paths it tried.

`Kusto.Language.dll` is bundled at `src/kustology/bin/Kusto.Language.dll`,
pinned by SHA-256 in `src/kustology/bin/VERSION.txt`, and refreshed via
`scripts/refresh_dll.py` (which runs `dotnet publish` against a known nuget
version). CI verifies the hash on every push via `scripts/verify_dll.py`.

## See also

- `README.md` — quickstart, install, examples, versioning and stability.
- `CONTRIBUTING.md` — workflow, coding conventions, the local check loop.
- `AGENTS.md` — the non-obvious interop and traversal traps. Read it before
  touching `bridge.py`, `IRBuilder`, or anything that walks the IR; every
  entry in it is a defect that shipped.
- `CHANGELOG.md` — every minor version's breaking changes.
- `examples/` — runnable, and smoke-tested by `tests/test_examples.py`.
  `walk_tree.py` and `walk_ir.py` are the same query through each tier.
- `docs/superpowers/reports/` — the audits behind 0.2.0, including the
  stub sweep and the `MaterializeExpr` reachability proof.
