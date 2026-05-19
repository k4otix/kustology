# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Reflection over Kusto.Language.Functions / Aggregates / Syntax."""

from __future__ import annotations

from kustology.reflection import (
    aggregate_functions,
    all_function_names,
    string_functions,
    syntax_kinds,
    time_functions,
)


def test_time_functions_contains_canonical_names():
    funcs = time_functions()
    # Anchor on the canonical handful — the set is a superset (possibly larger
    # after future Kusto.Language upgrades), so test for membership not equality.
    for canonical in ("ago", "now", "datetime", "bin", "startofday"):
        assert canonical in funcs, f"expected {canonical!r} in time_functions(); got {sorted(funcs)[:20]}"


def test_aggregate_functions_contains_canonical_names():
    funcs = aggregate_functions()
    for canonical in ("count", "dcount", "sum", "avg", "min", "max"):
        assert canonical in funcs, f"expected {canonical!r} in aggregate_functions(); got {sorted(funcs)[:20]}"


def test_string_functions_contains_canonical_names():
    funcs = string_functions()
    for canonical in ("strcat", "substring", "tolower", "toupper"):
        assert canonical in funcs, f"expected {canonical!r} in string_functions(); got {sorted(funcs)[:20]}"


def test_all_function_names_supersets_categories():
    everything = all_function_names()
    assert time_functions().issubset(everything)
    assert aggregate_functions().issubset(everything)
    assert string_functions().issubset(everything)


def test_syntax_kinds_has_expected_breadth():
    """SyntaxKind reflection: every enum member as a string. Sanity-check size
    + a few canonical members. The actual coverage audit lives elsewhere
    (scripts/audit_syntax_kinds.py)."""
    kinds = syntax_kinds()
    # The Kusto syntax grammar is broad — sanity-check that reflection
    # returned a real result, not an empty fallback. 100 is well below the
    # real number (~600).
    assert len(kinds) > 100, f"expected >100 SyntaxKinds via reflection, got {len(kinds)}"
    # SyntaxKind names are granular (AddExpression / EqualExpression / etc.)
    # rather than the Python class names ("BinaryExpression") the IR builder
    # dispatches on. Pick canonical members the enum is guaranteed to expose.
    for canonical in ("FilterOperator", "JoinOperator", "AddExpression", "AndExpression"):
        assert canonical in kinds, f"expected {canonical!r} in syntax_kinds()"
