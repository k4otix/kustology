# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier-2 coverage for ``top-nested``."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import Assignment, ColumnRef, TopNestedOp, find_all

# ``with others=`` is written before ``by``, not after: every spelling that
# puts it after the aggregate fails to parse.
FULL = (
    "T | top-nested 3 of State with others='OTHER' by Tot=sum(x) desc, "
    "top-nested of bin(t, 1h) by count(), "
    "top-nested 2 of a by max(b) asc"
)


def _top_nested(query: str) -> TopNestedOp:
    return parse(query).to_ir().main_pipeline.operators[0]


def test_each_level_records_its_count_key_aggregate_direction_and_others():
    # The clean parse is the claim here: ``with others=`` is written before
    # ``by``, and every spelling that puts it after fails with "Expected: ;".
    ir = parse(FULL).to_ir()
    assert not [d for d in ir.diagnostics if d.severity == "Error"], ir.diagnostics

    op = ir.main_pipeline.operators[0]
    assert len(op.levels) == 3

    first, second, third = op.levels
    assert first.count == 3
    assert isinstance(first.of, ColumnRef) and first.of.name == "State"
    assert first.by.name == "Tot"
    assert first.direction == "desc"
    assert first.others.value == "OTHER"

    # ``top-nested of c by ...`` writes no count, and a bare ``by count()``
    # is a plain function call rather than an OrderedExpression, so nothing
    # reads a direction off it.
    assert second.count is None
    assert isinstance(second.of, Assignment) and second.of.name == "t"
    assert second.by.name == "count_"
    assert second.direction is None
    assert second.others is None

    assert third.count == 2
    assert third.direction == "asc"


def test_the_nested_keys_reach_find_all():
    """A key named only inside a ``top-nested`` level is a column the query
    reads. Keeping the operator as text hid every one of them.
    """
    names = {c.name for c in find_all(_top_nested(FULL), ColumnRef)}
    assert {"State", "x", "t", "a", "b"} <= names
