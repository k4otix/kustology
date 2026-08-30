# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every parser SyntaxKind is handled by the IR builder or explicitly skipped.

The baseline lives in ``tests/fixtures/syntax_kinds_baseline.json`` and is
regenerated with ``python scripts/audit_syntax_kinds.py --update-baseline``.
A DLL upgrade that introduces a new SyntaxKind fails this test until the
contributor either:

* adds handling in ``ir/builder.py`` and re-runs the script, or
* adds the kind to ``deliberately_skipped`` (tokens, trivia, or variants the
  IR has no use for) and re-runs the script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kustology.ir import IRBuilder
from kustology.reflection import syntax_kinds

BASELINE = Path(__file__).resolve().parent / "fixtures" / "syntax_kinds_baseline.json"


def _handled_classes() -> set[str]:
    """Return every Python class name the builder claims to dispatch on.

    All three sets belong in the union: a kind missing from it reads as
    unhandled however completely the builder models it.
    """
    return set(
        IRBuilder.HANDLED_EXPR_KINDS
        | IRBuilder.HANDLED_OPERATOR_KINDS
        | IRBuilder.HANDLED_STATEMENT_KINDS
    )


def _current_unhandled(skipped: set[str], collapsed: set[str]) -> set[str]:
    """Subtract directly-handled kinds, class-collapsed kinds, and the skip list.

    ``collapsed`` holds the SyntaxKinds whose Python class name appears in
    ``IRBuilder.HANDLED_*_KINDS``, discovered by the audit script and recorded
    in the baseline's ``dispatched_via_class`` section. Without it the audit
    flags every subkind of ``BinaryExpression`` and its peers.
    """
    return set(syntax_kinds()) - _handled_classes() - collapsed - skipped


@pytest.mark.skipif(
    not BASELINE.exists(),
    reason="baseline not generated yet — run scripts/audit_syntax_kinds.py --update-baseline",
)
def test_no_new_unhandled_syntax_kinds():
    baseline = json.loads(BASELINE.read_text())
    skipped = set(baseline.get("deliberately_skipped", []))
    baseline_unhandled = set(baseline.get("unhandled", []))

    # Recover the collapsed kinds the way the audit script does: union the
    # SyntaxKinds dispatched via a handled Python class.
    handled_classes = _handled_classes()
    dispatched_via_class: dict[str, list[str]] = baseline.get(
        "dispatched_via_class", {},
    )
    collapsed: set[str] = set()
    for cls_name, kinds in dispatched_via_class.items():
        if cls_name in handled_classes:
            collapsed.update(kinds)

    current_unhandled = _current_unhandled(skipped, collapsed)

    new_unhandled = current_unhandled - baseline_unhandled
    assert not new_unhandled, (
        "New SyntaxKinds appeared that the IR builder doesn't handle:\n  "
        + "\n  ".join(sorted(new_unhandled))
        + "\n\nEither add a case in src/kustology/ir/builder.py and update "
        "HANDLED_*_KINDS, or add to `deliberately_skipped` in the baseline. "
        "Then regenerate with `python scripts/audit_syntax_kinds.py --update-baseline`."
    )
