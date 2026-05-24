# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tests for the generic IR walker.

Covers root-first yield, list and dict descent, scalar skipping, optional
None handling, type filtering, and nested descent through sub-pipelines.
"""

from typing import Optional

import pytest
from pydantic import BaseModel

from kustology.ir import (
    ColumnRef,
    FilterOp,
    IRBuilder,
    JoinOp,
    SortOp,
    TableRef,
    find_all,
    walk,
)


@pytest.fixture
def simple_ir():
    return IRBuilder().build("DeviceProcessEvents | where FileName == 'a.exe'")


@pytest.fixture
def joined_ir():
    return IRBuilder().build(
        "DeviceProcessEvents "
        "| where FileName == 'a.exe' "
        "| join (DeviceNetworkEvents | where RemoteIP != '127.0.0.1') on DeviceId"
    )


def test_walk_yields_root_first(simple_ir):
    first = next(iter(walk(simple_ir)))
    assert first is simple_ir


def test_walk_descends_into_list_fields(simple_ir):
    # Pipeline.operators is a list — every FilterOp inside must surface.
    op_types = {type(n).__name__ for n in walk(simple_ir)}
    assert "FilterOp" in op_types


def test_walk_descends_into_dict_values():
    # Synthetic model with a dict[str, BaseModel] field to exercise dict
    # descent (the IR doesn't currently use this shape but the walker
    # must handle it).
    class Leaf(BaseModel):
        name: str

    class Holder(BaseModel):
        items: dict[str, Leaf]

    holder = Holder(items={"a": Leaf(name="x"), "b": Leaf(name="y")})
    leaves = [n for n in walk(holder) if isinstance(n, Leaf)]
    assert {leaf.name for leaf in leaves} == {"x", "y"}


def test_walk_skips_scalars(simple_ir):
    for n in walk(simple_ir):
        assert isinstance(n, BaseModel), (
            f"walk yielded a non-BaseModel: {type(n).__name__} = {n!r}"
        )


def test_walk_handles_optional_none():
    # A model whose Optional field is None must not raise.
    class WithOptional(BaseModel):
        child: Optional[ColumnRef] = None

    node = WithOptional()
    yielded = list(walk(node))
    assert yielded == [node]


def test_find_all_filters_by_type(simple_ir):
    filters = list(find_all(simple_ir, FilterOp))
    assert len(filters) == 1
    assert isinstance(filters[0], FilterOp)


def test_find_all_finds_nested(joined_ir):
    # DeviceNetworkEvents lives inside the JoinOp's right pipeline.
    table_names = {n.name for n in find_all(joined_ir, TableRef)}
    assert table_names == {"DeviceProcessEvents", "DeviceNetworkEvents"}

    # The inner where on RemoteIP must also surface.
    column_names = {n.name for n in find_all(joined_ir, ColumnRef)}
    assert {"FileName", "RemoteIP", "DeviceId"} <= column_names


def test_find_all_returns_empty_for_absent_type(simple_ir):
    # The query has no sort — find_all should yield nothing.
    assert list(find_all(simple_ir, SortOp)) == []
    # And no JoinOp either.
    assert list(find_all(simple_ir, JoinOp)) == []
