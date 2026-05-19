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
   ruff check src tests scripts
   mypy src
   ```
4. Add or update tests for any behavior change. Tests should pin a specific
   diagnostic code, AST shape, or output — avoid asserting on free-form English
   text from the upstream Microsoft library.
5. Update `CHANGELOG.md` under an `## [Unreleased]` heading.
6. Open a PR. CI runs the same checks across macOS, Linux, and Windows on
   Python 3.10–3.12, plus a DLL-provenance verification.

## Refreshing the bundled DLL

```bash
python scripts/refresh_dll.py --version X.Y.Z --pin
python scripts/verify_dll.py
pytest
```

`--pin` updates `pyproject.toml` and `bin/VERSION.txt` together. Always run
the test suite after a refresh; upstream parser changes can shift diagnostic
codes or AST kinds.

## Coding conventions

- Modern typing (`X | None`, not `Optional[X]`).
- No comments unless they encode a non-obvious *why*.
- Public API changes require a CHANGELOG entry.
- Examples must use the documented public API; do not reach into `_code`
  or other private attributes.
