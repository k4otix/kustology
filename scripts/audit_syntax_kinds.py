#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Audit the IR builder's SyntaxKind coverage against Kusto.Language.dll.

Compares the live ``Kusto.Language.Syntax.SyntaxKind`` enum (read via
pythonnet reflection) against :attr:`IRBuilder.HANDLED_OPERATOR_KINDS`,
:attr:`IRBuilder.HANDLED_EXPR_KINDS` and
:attr:`IRBuilder.HANDLED_STATEMENT_KINDS`. Writes (or compares to) a JSON
baseline at ``tests/fixtures/syntax_kinds_baseline.json``.

Usage
-----
    python scripts/audit_syntax_kinds.py                     # human-readable summary
    python scripts/audit_syntax_kinds.py --check             # exit 1 if new gaps appeared
    python scripts/audit_syntax_kinds.py --update-baseline   # regenerate baseline

The baseline carries:

* ``all_syntax_kinds`` — full enum from the loaded DLL.
* ``handled_expr_kinds`` / ``handled_operator_kinds`` /
  ``handled_statement_kinds`` — the IR builder's static dispatch contract
  (Python class names, since the builder dispatches on
  ``type(node).__name__``). Keyword and token *kinds* are therefore never
  members: they have no node class behind them.
* ``dispatched_via_class`` — empirical map from Python class name to the
  set of SyntaxKind enum values that resolve to it, discovered by
  parsing the corpus under ``tests/fixtures/complex_queries/`` and
  walking the AST. ``"BinaryExpression"`` collapses ~20 kinds
  (``AddExpression``, ``EqualExpression``, …); without this section the
  audit's "unhandled" count is misleadingly inflated by every such
  subkind. Re-discovered on every ``--update-baseline``.
* ``deliberately_skipped`` — kinds the IR has no intent to model
  (tokens, trivia, structural list helpers, etc.). Allow-list for the
  diff.
* ``unhandled`` — everything in ``all`` that's neither handled (directly
  or via class-name collapse) nor skipped. ``--check`` fails when this
  set grows beyond what the baseline records.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "tests" / "fixtures" / "syntax_kinds_baseline.json"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "complex_queries"

# Default allowlist of kinds the IR has no intent to model. Anything matching
# these prefixes/suffixes is structural noise (lexer tokens, trivia, list
# delimiters) rather than a coverage gap.
_DEFAULT_SKIP_PREFIXES = ("Token",)
_DEFAULT_SKIP_SUFFIXES = (
    "Token", "Trivia", "List", "SyntaxList", "SeparatedElement",
)
_DEFAULT_SKIP_EXACT = frozenset({
    "None",
    "Unknown",
    "Custom",
    "Other",
    "Bad",
})


def _is_default_skipped(kind: str) -> bool:
    if kind in _DEFAULT_SKIP_EXACT:
        return True
    if any(kind.startswith(p) for p in _DEFAULT_SKIP_PREFIXES):
        return True
    return any(kind.endswith(s) for s in _DEFAULT_SKIP_SUFFIXES)


def _read_pin() -> str:
    """Read the kusto_language_version pin from pyproject.toml."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "unknown"
    for line in pyproject.read_text().splitlines():
        line = line.strip()
        if line.startswith("kusto_language_version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "unknown"


def _discover_dispatched_via_class() -> dict[str, list[str]]:
    """Parse the complex-queries corpus and group SyntaxKinds by Python class.

    The builder dispatches on ``type(node).__name__``. Many Python classes
    collapse multiple SyntaxKind enum values — ``BinaryExpression`` covers
    ``AddExpression`` / ``EqualExpression`` / ``BangTildeExpression`` /
    etc. This empirical map makes that collapse visible to the audit so
    the "unhandled" set isn't padded with subkinds already covered by a
    class-level dispatch.

    Returns ``{python_class_name: sorted_unique_syntax_kinds}``.
    """
    from kustology import parse  # local import: defers CLR init until the audit runs

    by_class: dict[str, set[str]] = {}
    if not CORPUS_DIR.exists():
        return {}

    def walk(node, sink: dict[str, set[str]]):
        if node is None:
            return
        cls_name = type(node).__name__
        try:
            kind_str = str(node.Kind)
        except AttributeError:
            kind_str = cls_name
        sink.setdefault(cls_name, set()).add(kind_str)
        try:
            cc = node.ChildCount
        except AttributeError:
            return
        for i in range(cc):
            child = node.GetChild(i)
            if child is not None:
                walk(child, sink)

    for kql_path in sorted(CORPUS_DIR.glob("*.kql")):
        try:
            text = kql_path.read_text(encoding="utf-8")
            result = parse(text)
            walk(result.syntax, by_class)
        except Exception:  # pragma: no cover — corpus probe is best-effort
            continue

    return {cls: sorted(kinds) for cls, kinds in sorted(by_class.items())}


def _compute_state(extra_skipped: set[str]) -> dict:
    from kustology.ir import IRBuilder
    from kustology.reflection import syntax_kinds

    all_kinds = set(syntax_kinds())
    handled_expr = set(IRBuilder.HANDLED_EXPR_KINDS)
    handled_op = set(IRBuilder.HANDLED_OPERATOR_KINDS)
    handled_stmt = set(IRBuilder.HANDLED_STATEMENT_KINDS)
    handled_class_names = handled_expr | handled_op | handled_stmt

    dispatched_via_class = _discover_dispatched_via_class()

    # Kinds covered via class-name collapse: union of SyntaxKinds whose
    # Python class is in the handled set.
    collapsed_kinds: set[str] = set()
    for cls_name, kinds in dispatched_via_class.items():
        if cls_name in handled_class_names:
            collapsed_kinds.update(kinds)

    default_skip = {k for k in all_kinds if _is_default_skipped(k)}
    deliberately_skipped = default_skip | extra_skipped

    unhandled = (
        all_kinds
        - handled_expr - handled_op - handled_stmt
        - collapsed_kinds - deliberately_skipped
    )

    return {
        "kusto_language_version": _read_pin(),
        "all_syntax_kinds": sorted(all_kinds),
        "handled_expr_kinds": sorted(handled_expr),
        "handled_operator_kinds": sorted(handled_op),
        "handled_statement_kinds": sorted(handled_stmt),
        "dispatched_via_class": dispatched_via_class,
        "deliberately_skipped": sorted(deliberately_skipped),
        "unhandled": sorted(unhandled),
    }


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text())


def _write_baseline(data: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Compare against baseline; exit 1 on new unhandled kinds.",
    )
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Regenerate tests/fixtures/syntax_kinds_baseline.json.",
    )
    args = parser.parse_args()

    baseline = _load_baseline()
    extra_skipped = set(baseline.get("deliberately_skipped", []))

    state = _compute_state(extra_skipped)

    if args.update_baseline:
        _write_baseline(state)
        print(f"Wrote {BASELINE_PATH.relative_to(REPO_ROOT)}")
        print(f"  total kinds: {len(state['all_syntax_kinds'])}")
        print(
            "  handled (expr+op+stmt): "
            f"{len(state['handled_expr_kinds']) + len(state['handled_operator_kinds']) + len(state['handled_statement_kinds'])}"
        )
        print(f"  deliberately skipped: {len(state['deliberately_skipped'])}")
        print(f"  unhandled: {len(state['unhandled'])}")
        return 0

    if args.check:
        if not baseline:
            print(
                f"error: no baseline at {BASELINE_PATH.relative_to(REPO_ROOT)}; "
                "run with --update-baseline first.",
                file=sys.stderr,
            )
            return 2
        baseline_unhandled = set(baseline.get("unhandled", []))
        current_unhandled = set(state["unhandled"])
        new = current_unhandled - baseline_unhandled
        if new:
            print("New unhandled SyntaxKinds since baseline:", file=sys.stderr)
            for k in sorted(new):
                print(f"  {k}", file=sys.stderr)
            print(
                "\nEither: (a) add handling in src/kustology/ir/builder.py "
                "and update HANDLED_*_KINDS, or (b) add to `deliberately_skipped` "
                "in the baseline, then regenerate with --update-baseline.",
                file=sys.stderr,
            )
            return 1
        print(f"Coverage OK — {len(current_unhandled)} unhandled kinds, matches baseline.")
        return 0

    # Default: print a human-readable summary.
    print(f"Kusto.Language version: {state['kusto_language_version']}")
    print(f"Total SyntaxKinds:    {len(state['all_syntax_kinds'])}")
    print(f"Handled (expr):       {len(state['handled_expr_kinds'])}")
    print(f"Handled (operator):   {len(state['handled_operator_kinds'])}")
    print(f"Handled (statement):  {len(state['handled_statement_kinds'])}")
    print(f"Deliberately skipped: {len(state['deliberately_skipped'])}")
    print(f"Unhandled:            {len(state['unhandled'])}")
    if state["unhandled"]:
        print("\nUnhandled kinds:")
        for k in state["unhandled"]:
            print(f"  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
