# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tests for the generic IR walker.

Covers root-first yield, list and dict descent, scalar skipping, optional
None handling, type filtering, nested descent through sub-pipelines, and
pruning descent at a predicate-matched node.
"""


import pytest
from pydantic import BaseModel

from kustology import parse
from kustology.ir import (
    ColumnRef,
    FilterOp,
    FuncCall,
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
    # Pipeline.operators is a list, so every FilterOp inside must surface.
    op_types = {type(n).__name__ for n in walk(simple_ir)}
    assert "FilterOp" in op_types


def test_walk_descends_into_dict_values():
    # The IR carries no dict[str, BaseModel] field, and the walker must still
    # descend one.
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
        child: ColumnRef | None = None

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
    # The query has no sort, so find_all yields nothing.
    assert list(find_all(simple_ir, SortOp)) == []
    # And no JoinOp either.
    assert list(find_all(simple_ir, JoinOp)) == []


def test_walk_with_predicate_filters_yielded_nodes():
    """``walk(node, predicate=...)`` should yield only nodes the predicate
    accepts, but still descend through skipped parents."""
    from kustology.ir import BinOp
    ir = IRBuilder().build(
        "DeviceProcessEvents | where FileName == 'cmd.exe' or FileName =~ 'pwsh.exe'"
    )
    case_insensitive = list(walk(ir, lambda n: isinstance(n, BinOp) and not n.case_sensitive))
    assert len(case_insensitive) == 1
    assert case_insensitive[0].op == "=~"

    # Predicate yields nothing → empty iterator, no crash.
    assert list(walk(ir, lambda n: False)) == []


def test_walk_descends_tuple_valued_fields():
    """``CaseExpr.branches`` is ``list[tuple[Expr, Expr]]``.

    A walker that descends list- and dict-valued fields but not tuples reaches
    only the ``default`` arm, leaving every expression inside a ``case(...)``
    arm invisible to ``walk``/``find_all``, whole sub-pipelines included.
    """
    from kustology.ir import CaseExpr

    ir = IRBuilder().build(
        "DeviceProcessEvents "
        "| extend Risk = case(FileName == 'a.exe', AccountName, "
        "FileName == 'b.exe', DeviceId, ProcessId)"
    )
    branches = list(find_all(ir, CaseExpr))
    assert len(branches) == 1

    names = [n.name for n in find_all(ir, ColumnRef)]
    # Two predicate refs + two value refs + the default, plus nothing lost.
    assert names.count("FileName") == 2
    assert "AccountName" in names
    assert "DeviceId" in names
    assert "ProcessId" in names


def test_walk_descends_tuples_of_non_models_without_error():
    """``ExternalDataExpr.columns`` is ``list[tuple[str, str]]``, so tuple
    descent must skip plain values the way list descent does."""
    ir = IRBuilder().build(
        'DeviceProcessEvents | where FileName in '
        '((externaldata(n:string, v:string) [@"https://example/x.csv"]))'
    )
    # Traversal completes and the root is still yielded exactly once.
    nodes = list(walk(ir))
    assert nodes[0] is ir
    assert all(isinstance(n, BaseModel) for n in nodes)


def test_walk_yields_a_shared_node_once():
    """A node reachable by two paths is yielded once.

    ``LetBinding.inner_time_exprs`` indexes the same ``FuncCall`` objects that
    sit inside ``rhs_pipeline``. Without the visited set, ``find_all(ir,
    FuncCall)`` returns ``ago`` and ``now`` twice each and every caller
    counting occurrences double-counts them.
    """
    from kustology.ir import FuncCall

    ir = IRBuilder().build(
        "let A = T | where d > ago(1h) | where d < now(); A | take 1"
    )
    assert [f.name for f in find_all(ir, FuncCall)] == ["ago", "now"]

    # The general invariant, beyond this one query: no object twice.
    ids = [id(n) for n in walk(ir)]
    assert len(ids) == len(set(ids))


JOIN_QUERY = "T | where TimeGenerated > ago(5m) | join (S | where TimeGenerated > ago(99d)) on X"


def _ago_args(nodes, query):
    return [n.args[0].span.text(query) for n in nodes if isinstance(n, FuncCall) and n.name == "ago"]


def test_prune_stops_descent_at_join():
    ir = parse(JOIN_QUERY).to_ir(semantic_hash=False)
    seen = list(walk(ir.main_pipeline, prune=lambda n: isinstance(n, JoinOp)))
    assert _ago_args(seen, JOIN_QUERY) == ["5m"]


def test_predicate_alone_still_descends():
    ir = parse(JOIN_QUERY).to_ir(semantic_hash=False)
    seen = list(walk(ir.main_pipeline, lambda n: not isinstance(n, JoinOp)))
    assert _ago_args(seen, JOIN_QUERY) == ["5m", "99d"]


def test_pruned_node_is_yielded_but_not_entered():
    ir = parse(JOIN_QUERY).to_ir(semantic_hash=False)
    seen = list(walk(ir.main_pipeline, prune=lambda n: isinstance(n, JoinOp)))
    assert sum(isinstance(n, JoinOp) for n in seen) == 1


def test_find_all_accepts_prune():
    ir = parse(JOIN_QUERY).to_ir(semantic_hash=False)
    outer = list(find_all(ir.main_pipeline, FuncCall, prune=lambda n: isinstance(n, JoinOp)))
    assert [f.name for f in outer] == ["ago"]
