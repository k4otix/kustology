#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Audit the IR builder's SyntaxKind coverage against Kusto.Language.dll.

Compares the live ``Kusto.Language.Syntax.SyntaxKind`` enum (read via
pythonnet reflection) against :attr:`IRBuilder._HANDLED_OPERATOR_KINDS` and
:attr:`IRBuilder._HANDLED_EXPR_KINDS`. Writes (or compares to) a JSON baseline
at ``tests/fixtures/syntax_kinds_baseline.json``.

Usage
-----
    python scripts/audit_syntax_kinds.py                     # human-readable summary
    python scripts/audit_syntax_kinds.py --check             # exit 1 if new gaps appeared
    python scripts/audit_syntax_kinds.py --update-baseline   # regenerate baseline

The baseline carries:

* ``all_syntax_kinds`` — full enum from the loaded DLL.
* ``handled_expr_kinds`` / ``handled_operator_kinds`` — the IR builder's
  static dispatch contract.
* ``deliberately_skipped`` — kinds the IR has no intent to model (tokens,
  trivia, structural list helpers, etc.). Allow-list for the diff.
* ``unhandled`` — everything in ``all`` that's neither handled nor skipped.
  ``--check`` fails when this set grows beyond what the baseline records.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "tests" / "fixtures" / "syntax_kinds_baseline.json"

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
    if any(kind.endswith(s) for s in _DEFAULT_SKIP_SUFFIXES):
        return True
    return False


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


def _compute_state(extra_skipped: set[str]) -> dict:
    from kustology.ir import IRBuilder
    from kustology.reflection import syntax_kinds

    all_kinds = set(syntax_kinds())
    handled_expr = set(IRBuilder._HANDLED_EXPR_KINDS)
    handled_op = set(IRBuilder._HANDLED_OPERATOR_KINDS)

    default_skip = {k for k in all_kinds if _is_default_skipped(k)}
    deliberately_skipped = default_skip | extra_skipped

    unhandled = all_kinds - handled_expr - handled_op - deliberately_skipped

    return {
        "kusto_language_version": _read_pin(),
        "all_syntax_kinds": sorted(all_kinds),
        "handled_expr_kinds": sorted(handled_expr),
        "handled_operator_kinds": sorted(handled_op),
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
        print(f"  handled (expr+op): {len(state['handled_expr_kinds']) + len(state['handled_operator_kinds'])}")
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
                "and update _HANDLED_*_KINDS, or (b) add to `deliberately_skipped` "
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
    print(f"Deliberately skipped: {len(state['deliberately_skipped'])}")
    print(f"Unhandled:            {len(state['unhandled'])}")
    if state["unhandled"]:
        print("\nUnhandled kinds:")
        for k in state["unhandled"]:
            print(f"  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
