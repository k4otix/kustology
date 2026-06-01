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
    binder.py        # Schema attachment + type enrichment
    walk.py          # Generic IR traversal: walk() and find_all()
    types.py         # Kusto type enum
    spans.py         # Source location tracking
  utils/             # Helpers for tree walking and schema binding
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

See README.md "Stability policy" for what counts as breaking.

## Where to add things

**A new tabular operator** (e.g. `mv-apply`, `partition`):

1. Add an IR node class in `src/kustology/ir/query.py`.
2. Add its `SyntaxKind` string to `IRBuilder._HANDLED_OPERATOR_KINDS` in
   `src/kustology/ir/builder.py`.
3. Add a dispatch branch in `IRBuilder._visit_operator()` that reads the .NET
   node's attributes and constructs your IR node. Probe attribute names with:

   ```python
   from kustology.bridge import KustoCode
   from Kusto.Language import GlobalState
   code = KustoCode.ParseAndAnalyze("T | <your-operator>", GlobalState.Default)
   # inspect type(node).__name__ and dir(node)
   ```

4. Add a minimal `.kql` fixture under `tests/fixtures/complex_queries/`. The
   parametrized harness in `tests/ir/test_complex_harness.py` picks it up
   automatically.
5. Regenerate the baseline: `python scripts/audit_syntax_kinds.py --update`.

**A new IR expression** (e.g. a new literal kind, a new operator shape):

1. Add the model in `src/kustology/ir/expr.py` (or reuse `LiteralExpr` if
   it's just a new `literal_kind`).
2. Add its kind to `IRBuilder._HANDLED_EXPR_KINDS`.
3. Add a dispatch branch in `IRBuilder._visit_expr()`.
4. Regenerate the baseline.

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

- `README.md` — quickstart, install, examples, stability policy.
- `CONTRIBUTING.md` — workflow, coding conventions, pre-commit setup.
- `CHANGELOG.md` — every minor version's breaking changes.
