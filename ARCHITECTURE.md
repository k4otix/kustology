# Architecture

A 400-foot view of the codebase, oriented for new contributors.

## Layout

```
src/kustology/
  bridge.py          # .NET CLR init, loads Kusto.Language.dll via pythonnet
  core.py            # KustoQuery wrapper (parse / format / validate seam)
  services.py        # Public entry points: parse(), format_query(), validate()
  reflection.py      # Runtime introspection of Kusto.Language for func classification
  cli.py             # Command-line interface — kustology parse/format/validate/version
  ir/                # Tier-2: pydantic IR (opt-in via [ir] extras)
    builder.py       # Walks .NET syntax tree → QueryIR; dispatch tables for operators/expressions
    query.py         # Operator and pipeline node models
    expr.py          # Expression node models
    binder.py        # SchemaAttacher: schema attachment + type enrichment
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
    analysis.py      # AST-level analysis: table refs, operator stats, time ranges
    walker.py        # KustoWalker + iter_elements: primitive AST traversal
    schema_state.py  # build_global_state: Python schema dict → .NET GlobalState
  bin/               # Bundled Kusto.Language.dll + VERSION.txt (SHA-256 pinned)

scripts/             # Tooling: audit_syntax_kinds.py, verify_corpus.py,
                     # sample_sentinel_corpus.py, extract_sentinel_schemas.py,
                     # verify_dll.py, refresh_dll.py

tests/               # pytest suite
  ir/                # IR-specific tests
  fixtures/          # complex_queries/, sentinel_sample/ (gitignored),
                     # syntax_kinds_baseline.json
```

## Tiers

**Tier 1** — `kustology` top-level surface (`bridge`, `services`, `core`, `utils`,
`reflection`, `cli`). SemVer-stable. Public API for callers and the CLI.

**Tier 2** — `kustology.ir.*`. Pydantic IR with semantic enrichment. On a pre-1.0
track until the IR survives one Kusto.Language.dll upgrade cycle without breaking. Minor
breaking changes are possible at minor versions; each is called out in CHANGELOG.md.

See README.md "Versioning and stability" for what counts as breaking.

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

4. Decide how the operator affects **column scope**, in
   `src/kustology/ir/binder.py`. If it reshapes its output schema, add a
   scope rule in `SchemaAttacher._walk_operator()`; if it passes its input
   schema through, the generic fallback already handles it. Either way,
   update the coverage list in `SchemaAttacher`'s class docstring, which
   enumerates which operators reshape scope, which pass it through, and
   which leave downstream scope knowingly stale. Skipping this step is how
   `SchemaAttacher` silently ended up covering 17 of 53 operators.
5. Add a minimal `.kql` fixture under `tests/fixtures/complex_queries/`. The
   parametrized harness in `tests/ir/test_complex_harness.py` picks it up
   automatically.
6. Regenerate the baseline:
   `python scripts/audit_syntax_kinds.py --update-baseline`.

**A new IR expression** (e.g. a new literal kind, a new operator shape):

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
   fallthrough to a bare `"?"`, so a missing branch degrades quietly rather
   than raising: 11 `Expr` types had fallen through it, rendering `-X > 1`,
   `D.a == 1` and `toscalar(...) > 1` all as the same string `"? > 1"`.
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
- `CONTRIBUTING.md` — workflow, coding conventions, pre-commit setup.
- `CHANGELOG.md` — every minor version's breaking changes.
