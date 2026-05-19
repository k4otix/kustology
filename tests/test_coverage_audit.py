# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Coverage audit: every parser SyntaxKind is either handled by the IR
builder or explicitly skipped.

The baseline lives in ``tests/fixtures/syntax_kinds_baseline.json`` and is
regenerated with ``python scripts/audit_syntax_kinds.py --update-baseline``.
When a DLL upgrade introduces a new SyntaxKind, this test fails until the
contributor either:

* adds handling in ``ir/builder.py`` and re-runs the script, or
* adds the kind to ``deliberately_skipped`` (for tokens / trivia / variants
  the IR has no use for) and re-runs the script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kustology.ir import IRBuilder  # noqa: E402
from kustology.reflection import syntax_kinds  # noqa: E402

BASELINE = Path(__file__).resolve().parent / "fixtures" / "syntax_kinds_baseline.json"


def _current_unhandled(skipped: set[str]) -> set[str]:
    all_kinds = set(syntax_kinds())
    handled = IRBuilder._HANDLED_EXPR_KINDS | IRBuilder._HANDLED_OPERATOR_KINDS
    return all_kinds - handled - skipped


@pytest.mark.skipif(
    not BASELINE.exists(),
    reason="baseline not generated yet — run scripts/audit_syntax_kinds.py --update-baseline",
)
def test_no_new_unhandled_syntax_kinds():
    baseline = json.loads(BASELINE.read_text())
    skipped = set(baseline.get("deliberately_skipped", []))
    baseline_unhandled = set(baseline.get("unhandled", []))

    current_unhandled = _current_unhandled(skipped)

    new_unhandled = current_unhandled - baseline_unhandled
    assert not new_unhandled, (
        "New SyntaxKinds appeared that the IR builder doesn't handle:\n  "
        + "\n  ".join(sorted(new_unhandled))
        + "\n\nEither add a case in src/kustology/ir/builder.py and update "
        "_HANDLED_*_KINDS, or add to `deliberately_skipped` in the baseline. "
        "Then regenerate with `python scripts/audit_syntax_kinds.py --update-baseline`."
    )
