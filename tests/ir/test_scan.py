# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier-2 coverage for ``scan`` and for qualified column references."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import ColumnRef, LetValueRef, PathExpr, ScanOp, find_all

# Every field of ScanOp and ScanStep written at once. The grammar's clause
# order is ``order by`` then ``partition by`` then ``declare``; any other
# order parses with syntax diagnostics and drops a clause silently.
FULL = (
    "T | scan with_match_id=mid with_step_name=sname "
    "order by a asc partition by b declare(p:string='', n:long=0) "
    "with (step s1 output=last: a > 1 => p = s1.p, n = n + 1; "
    "step s2 optional output=none: b < 2 => n = 0;)"
)


def _scan(query: str) -> ScanOp:
    return parse(query).to_ir().main_pipeline.operators[0]


def test_scan_records_every_clause_it_was_given():
    ir = parse(FULL).to_ir()
    # The clause order above is the one spelling the grammar accepts, and a
    # rejected clause is dropped rather than reported as missing, so the
    # field assertions below only mean something on a clean parse.
    assert not [d for d in ir.diagnostics if d.severity == "Error"], ir.diagnostics
    op = ir.main_pipeline.operators[0]

    assert op.with_match_id == "mid"
    assert op.with_step_name == "sname"
    assert [d.decl.name for d in op.declarations] == ["p", "n"]
    assert [d.decl.declared_type for d in op.declarations] == ["string", "long"]
    assert [d.default.value for d in op.declarations] == ["", 0]
    assert [(k.expression.name, k.direction) for k in op.order_by] == [("a", "asc")]
    assert [e.name for e in op.partition_by] == ["b"]
    assert [s.name for s in op.steps] == ["s1", "s2"]
    assert [s.is_optional for s in op.steps] == [False, True]
    assert [s.output for s in op.steps] == ["last", "none"]
    assert [a.name for a in op.steps[0].assignments] == ["p", "n"]


def test_a_step_qualified_reference_is_a_qualified_column_not_a_step_name():
    """``s1.p`` names the column ``p`` as step ``s1`` saw it. Lowering the
    left-hand name to a ``ColumnRef`` would make ``find_all(ir, ColumnRef)``
    report a column called ``s1``, which no table has.
    """
    op = _scan(FULL)
    names = {c.name for c in find_all(op, ColumnRef)}

    assert "s1" not in names
    qualified = [c for c in find_all(op, ColumnRef) if c.qualifier is not None]
    assert [(c.qualifier, c.name) for c in qualified] == [("s1", "p")]


def test_an_unqualified_reference_outside_a_scan_keeps_qualifier_unset():
    """Negative control for the qualifier scope: the step-name set is
    restored after the operator, so ``s`` elsewhere reads as it always has.
    """
    ir = parse("T | scan with (step s: a > 1 => ;) | where s > 1").to_ir()
    outer = ir.main_pipeline.operators[1].predicate.left
    assert isinstance(outer, ColumnRef)
    assert outer.qualifier is None


def test_a_dynamic_property_access_is_still_a_path_expression():
    """The qualifier arm must fire only on a name the enclosing operator
    declared. ``d.field`` inside a step reads a dynamic bag.
    """
    op = _scan("T | scan with (step s: d.field == 1 => ;)")
    assert isinstance(op.steps[0].condition.left, PathExpr)


def test_a_step_with_no_computation_clause_records_no_assignments():
    op = _scan("T | scan with (step s: a > 1;)")
    assert op.steps[0].assignments == []
    assert op.steps[0].output is None


def test_an_empty_arrow_records_no_assignments():
    """``=> ;`` yields one ``ScanAssignment`` whose name is a zero-width
    missing node. Recording it would put an empty column name in the digest.
    """
    op = _scan("T | scan with (step s: a > 1 => ;)")
    assert op.steps[0].assignments == []


def test_a_let_bound_name_inside_a_step_stays_a_let_value_ref():
    """Step names and ``let`` names share one namespace check. A ``let``
    still wins for a name no step declared.
    """
    ir = parse("let n = 5; T | scan with (step s: a > n => ;)").to_ir()
    op = ir.main_pipeline.operators[0]
    assert isinstance(op.steps[0].condition.right, LetValueRef)
