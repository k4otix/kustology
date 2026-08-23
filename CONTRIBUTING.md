# Contributing

Thanks for your interest in Kustology. This project is small; the
contribution loop is intentionally short.

## Setup

```bash
git clone https://github.com/k4otix/kustology.git
cd kustology
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires .NET 8.0+. On Homebrew macOS the bridge auto-detects
`/opt/homebrew/opt/dotnet/libexec`; elsewhere set `DOTNET_ROOT` if your
runtime is not on a standard path.

## Workflow

1. Open an issue first for non-trivial changes so the design can be discussed.
2. Branch from `main`.
3. Run the full check locally:
   ```bash
   pytest
   ruff check src tests scripts examples
   mypy src
   ```
4. Add or update tests for any behavior change. Tests should pin a specific
   diagnostic code, AST shape, or output — avoid asserting on free-form English
   text from the upstream Microsoft library. **A test that asserts a default
   proves nothing**: `assert expr.result_type_inner is None` on a hand-built
   node passes identically whether the populating code works or has never run
   once. Assert a non-default value on a real parse.
5. Update `CHANGELOG.md` under an `## [Unreleased]` heading.
6. Changing an IR field shape, or anything that feeds `semantic_hash`? Record
   it in the CHANGELOG, but do **not** bump `IR_SCHEMA_VERSION`
   (`src/kustology/ir/__init__.py`) or `SEMANTIC_HASH_SCHEME`
   (`src/kustology/ir/transforms.py`) yourself. They move once, at release, so
   they mark what a consumer can observe rather than the project's internal
   history — several branches can land between releases and still share one
   increment.
7. Open a PR. CI runs the same checks on Linux across Python 3.10–3.13,
   with macOS and Windows sanity cells on 3.12, plus five jobs the local
   loop above does not cover: the suite again under `de-DE` and `fr-FR`
   (the culture pin), `python scripts/audit_syntax_kinds.py --check`,
   `python scripts/mine_corpus.py`, DLL-provenance verification, and an
   SBOM build. Run the two script gates locally if you touched the IR
   builder — they are fast and they are the two most likely to surprise
   you.

## The oracle harness

`tests/ir/test_binder_oracle.py` is the reason most Tier 2 schema work does
not need a new rule. Microsoft's binder already computes, exactly, the
columns a query returns, and the oracle asserts that
`ir.main_pipeline.result_schema.columns` equals `code.ResultType.Columns`
**as an ordered list** — column order is part of a KQL result, so a dict
comparison would let a reordering through. It runs a 74-shape operator
matrix plus all 49 fixtures in `tests/fixtures/complex_queries/`.

It has two legs, and the second is the one that gates hand-written rules.
The **bound** leg compares Microsoft's answer with itself wherever the
symbol is closed, so it can only fail where Microsoft left the symbol open;
four cases are xfailed there. The **unbound** leg reaches the IR with the
schema going only through `SchemaAttacher`, so every case exercises the
hand-rolled scope walk; eleven are xfailed.

Both xfail lists are `strict=True`. A case you fix therefore turns the test
red until you delete its entry — that is deliberate, and it is how a fix
stays recorded rather than sitting as a silent xpass. Add an entry only with
a reason, and prefer fixing the rule.

## Refreshing the bundled DLL

```bash
python scripts/refresh_dll.py --version X.Y.Z --pin
python scripts/verify_dll.py
pytest
python scripts/audit_syntax_kinds.py --check
```

`--pin` updates `pyproject.toml` and `bin/VERSION.txt` together. Always run
the test suite after a refresh; upstream parser changes can shift diagnostic
codes or AST kinds. The coverage audit is the one that catches a *new*
`SyntaxKind` the builder has never seen — regenerate its baseline with
`--update-baseline` once you have decided whether to model the new kind or
let it fall through to `UnknownOp` / `UnknownExpr`, and commit
`tests/fixtures/syntax_kinds_baseline.json` with the change.

## Coding conventions

- Modern typing (`X | None`, not `Optional[X]`).
- No comments unless they encode a non-obvious *why*.
- Public API changes require a CHANGELOG entry.
- Examples must use the documented public API; do not reach into `_code`
  or other private attributes. A new example needs a zero-arg `main()` and
  an SPDX/copyright header, and one that imports `kustology.ir` must be
  added to `IR_EXAMPLES` in `tests/test_examples.py` so it skips cleanly on
  a base install. `tests/test_examples.py` runs every one of them.
- Do not declare a model field you cannot populate in the same change.
  A declared-but-unfilled field reads as implemented and is invisible to
  tests; `docs/superpowers/reports/2026-08-20-stub-sweep.md` is the record
  of what that cost.

## Further reading

- `ARCHITECTURE.md` — layout, where to add a new operator/expression, and
  which file enforces which invariant.
- `AGENTS.md` — non-obvious interop and AST traversal notes useful when
  modifying CLR bridge code, the IR builder, or the bundled DLL. Aimed
  at AI assistants and new contributors alike. Every entry is a defect
  that shipped once.
- `examples/` — runnable and CI-tested; the fastest read of what each tier
  can do.
- `docs/superpowers/reports/` — the audits behind 0.2.0.
