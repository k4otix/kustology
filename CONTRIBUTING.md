# Contributing

Thanks for your interest in Kustology. This project is small; the
contribution loop is short.

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
   it in the CHANGELOG, but do **not** bump `IR_SCHEMA_VERSION` or
   `SEMANTIC_HASH_SCHEME` (both in `src/kustology/_ir_tags.py`) yourself. They
   move once, at release, so they mark what a consumer can observe rather than
   the project's internal history — several branches can land between releases
   and still share one increment.
7. Open a PR. CI runs the same checks on Linux across Python 3.10–3.14,
   with macOS and Windows sanity cells on 3.14. `test`, `test-ir`, and `lint`
   in `.github/workflows/test.yml` are the loop above; every other job in
   that workflow has no local counterpart and is listed below:

   | Job | What it adds |
   | --- | --- |
   | `dependency-review` | flags vulnerable dependencies on the PR itself |
   | `test-locale` | the whole suite again in three cells — `de-DE` and `fr-FR` for the culture pin, and `en_US.UTF-8` with `TZ=Asia/Tokyo` for the timezone bug below |
   | `coverage-audit` | `python scripts/audit_syntax_kinds.py --check` |
   | `corpus-regression` | `python scripts/mine_corpus.py` |
   | `verify-dll` | the bundled DLL's SHA-256, offline and against NuGet |
   | `sbom` | CycloneDX SBOM build |

   Run the two script gates locally if you touched the IR builder — they are
   fast and they are the two most likely to surprise you.

   **The `Asia/Tokyo` cell is the one worth knowing about.** A UTC runner
   cannot tell "converted to UTC" from "not converted", so a
   timezone-dependent defect is invisible in every other cell — and the
   library has exactly one such surface, the `DateTimeKind` branch in
   `ir/_builder_helpers.py:613-616` behind `LiteralExpr.value` / `.ticks`
   (it is the only `ToUniversalTime` / `DateTimeKind` / `TimeZone` read in
   `src/`). If you touch datetime literals, read
   *"Datetime literals are UTC-normalized at build"* in `AGENTS.md` first;
   that cell is what will catch you, and only on a PR.

## The oracle harness

`tests/ir/test_binder_oracle.py` is the reason most Tier 2 schema work does
not need a new rule. Microsoft's binder already computes, exactly, the
columns a query returns, and the oracle asserts that
`ir.main_pipeline.result_schema.columns` equals `code.ResultType.Columns`
**as an ordered list** — column order is part of a KQL result, so a dict
comparison would let a reordering through. It runs an operator-shape matrix
plus every fixture in `tests/fixtures/complex_queries/`.

It has two legs, and both compare Microsoft's own capture against Microsoft's
own direct answer — the dict-schema path re-binds through Microsoft's binder
rather than decorating the IR by hand, so neither leg compares a hand-rolled
guess against Microsoft's. The **bound** leg parses with a
schema up front (`parse(q, schema=...).to_ir()`); most of the matrix then
compares Microsoft's answer with itself and can only fail where a symbol is
open, so its operator-shape run samples one representative id per construct
family rather than the whole matrix. The **dict** leg —
`parse(q).to_ir(attach_schema=schema)` — is the public `attach_schema=dict`
entry point itself, re-bound through the same seam and therefore
byte-identical to the bound leg's IR shape wherever the schema is non-empty;
it is that public path being proven end-to-end, so it keeps the full matrix.

The two legs part ways on an **empty** schema, though: `parse(q, schema={})`
still binds — a real, if empty, database — while `to_ir(attach_schema={})`
is documented to treat `{}` as a no-op, the same as `attach_schema=False`, so
it never re-binds at all. The dict leg's corpus test skips a fixture whose
derived schema comes out empty rather than comparing against a re-bind that
never happened, so the two legs' corpus coverage isn't quite identical.
There are no xfail lists. Where Microsoft's `ResultType.IsOpen` is
true — it named the columns it could work out and declined to say the list is
complete — `microsoft_columns` returns the `OPEN` sentinel and the assertion
becomes `ours is None`. That is the same requirement as an exact match,
stated for the case where there is no exact answer to match: we decline where
the binder declined. An xfail list would only ever hold an open symbol whose
hand-rolled guess happened to line up with the partial list, or happened not
to.

So a divergence here is a real defect in the plumbing — the per-operator
capture, the column ordering, or the `Analyze` seam — and not a rule to be
tuned. Fix it rather than annotating it; if you genuinely need to park one,
say why in the marker and expect the next reader to delete it.

## Refreshing the bundled DLL

```bash
python scripts/refresh_dll.py --version X.Y.Z --pin
python scripts/verify_dll.py
pytest
python scripts/audit_syntax_kinds.py --check
```

`bin/VERSION.txt` is rewritten on **every** run — including a bare
`refresh_dll.py`, which re-resolves the already-pinned version and stamps a
fresh `refreshed=` timestamp. `--pin` adds the second write, to
`pyproject.toml`'s `[tool.kustology]`. So an unpinned run can still leave the
two files disagreeing about the version; pass `--pin` whenever `--version`
changes it. Always run the test suite after a refresh; upstream parser
changes can shift diagnostic codes or AST kinds. The coverage audit is the
one that catches a *new*
`SyntaxKind` the builder has never seen — regenerate its baseline with
`--update-baseline` once you have decided whether to model the new kind or
let it fall through to `UnknownOp` / `UnknownExpr` / `UnknownStmt`, and commit
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
- Examples print through `examples/_display.py`, which uses `rich` when the
  `[examples]` extra is installed and plain text when it is not. Call
  `banner`, `section`, `note`, `kql`, `data`, `table`, and `takeaway`
  rather than formatting output by hand, and never import `rich` in an
  example. The smoke test runs each example both ways.
- Do not declare a model field you cannot populate in the same change.
  A declared-but-unfilled field reads as implemented and is invisible to
  tests, so a downstream consumer can design against it before discovering
  it never fills.

## Further reading

- `ARCHITECTURE.md` — layout, where to add a new operator/expression, and
  which file enforces which invariant.
- `AGENTS.md` — non-obvious interop and AST traversal notes useful when
  modifying CLR bridge code, the IR builder, or the bundled DLL. Aimed
  at AI assistants and new contributors alike. Every entry is a defect
  that shipped once.
- `examples/` — runnable and CI-tested; the fastest read of what each tier
  can do.
