## Summary

What does this change and why?

## Checklist

- [ ] Tests added or updated for the behavior change, asserting a
      **non-default** value on a real parse.
- [ ] `pytest`, `ruff check src tests scripts examples`, and `mypy src` pass
      locally.
- [ ] `CHANGELOG.md` updated if user-visible, under `## [Unreleased]`.
      `IR_SCHEMA_VERSION` / `SEMANTIC_HASH_SCHEME` left alone — they move at
      release.
- [ ] Public API changes documented in the README and docstrings.
- [ ] If `IRBuilder.HANDLED_OPERATOR_KINDS` / `HANDLED_EXPR_KINDS` changed:
      `python scripts/audit_syntax_kinds.py --update-baseline` run and
      `tests/fixtures/syntax_kinds_baseline.json` committed.
- [ ] If the IR changed: `python scripts/mine_corpus.py` is clean.
- [ ] If the bundled DLL was refreshed: `python scripts/verify_dll.py` passes.
