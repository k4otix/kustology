# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier-2 coverage for ``make-graph``."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import ColumnRef, MakeGraphOp, TableRef, find_all

SCHEMA = {
    "Edges": {"src": "string", "dst": "string", "dstKey": "string"},
    "Nodes": {"n": "string", "label": "string"},
}


def test_make_graph_records_its_edge_columns_direction_and_node_table():
    ir = parse("Edges | make-graph src --> dst with Nodes on n").to_ir()
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, MakeGraphOp)
    assert op.source.name == "src"
    assert op.target.name == "dst"
    assert op.direction == "-->"
    assert [n.node_table.name for n in op.nodes] == ["Nodes"]
    assert [n.key.name for n in op.nodes] == ["n"]


def test_the_node_table_reaches_find_all_on_an_unbound_parse():
    """Tier 1's ``get_referenced_tables`` resolves ``Nodes`` only on a bound
    parse, because it reads the resolved symbol. The IR reads the source
    position, so both bind states agree.
    """
    unbound = parse("Edges | make-graph src --> dst with Nodes on n").to_ir()
    bound = parse("Edges | make-graph src --> dst with Nodes on n", schema=SCHEMA).to_ir()

    assert {t.name for t in find_all(unbound, TableRef)} == {"Edges", "Nodes"}
    assert {t.name for t in find_all(bound, TableRef)} == {"Edges", "Nodes"}


def test_an_implicit_node_id_fills_its_own_field():
    ir = parse("Edges | make-graph src -- dst with_node_id=NodeId").to_ir()
    op = ir.main_pipeline.operators[0]
    assert op.direction == "--"
    assert op.node_id == "NodeId"
    assert op.nodes == []


def test_a_partitioned_by_clause_carries_its_key_and_its_sub_pipeline():
    ir = parse(
        "Edges | make-graph src --> dst partitioned-by dstKey "
        "(graph-match (a)-[e]->(b) project x=1)"
    ).to_ir()
    op = ir.main_pipeline.operators[0]
    assert op.partition_by == "dstKey"
    assert op.partition_pipeline is not None
    assert len(op.partition_pipeline.operators) == 1


def test_a_let_aliased_node_table_is_a_let_ref():
    """The node table goes through ``_visit_table_ref``, so a ``let`` alias
    reads as one rather than as a table nobody declared.
    """
    ir = parse("let N = Nodes; Edges | make-graph src --> dst with N on n").to_ir()
    op = ir.main_pipeline.operators[0]
    assert op.nodes[0].node_table.kind == "let_ref"


def test_the_edge_columns_reach_find_all():
    ir = parse("Edges | make-graph src --> dst with Nodes on n").to_ir()
    names = {c.name for c in find_all(ir.main_pipeline.operators[0], ColumnRef)}
    assert {"src", "dst", "n"} <= names


# -- malformed input degrades, it does not raise --------------------------

MALFORMED_DIRECTION = [
    ("no-columns", "Edges | make-graph", None),
    ("source-only", "Edges | make-graph a", "a"),
    ("unsupported-arrow", "Edges | make-graph a <-- b", "a"),
]


@pytest.mark.parametrize(
    "case_id, query, source", MALFORMED_DIRECTION,
    ids=[c[0] for c in MALFORMED_DIRECTION],
)
def test_a_make_graph_without_a_written_arrow_degrades_instead_of_raising(
    case_id, query, source,
):
    """``to_ir()`` must not be the thing that fails on bad KQL.

    An unwritten arrow leaves a ``DirectionToken`` that exists, holding a
    missing token whose ``Text`` is ``""``. A presence check alone lets that
    empty string reach ``Literal["-->", "--"]`` and turns a half-typed
    operator into a ``ValidationError`` out of ``to_ir()``, where
    ``T | take``, ``T | where`` and ``T | sort by`` all build a degraded
    operator and leave the complaint to the diagnostics. ``<--`` is the same
    shape by another route: Microsoft rejects it and writes no token.
    """
    parsed = parse(query)
    assert parsed.diagnostics, f"{case_id}: expected the parser to complain about {query!r}"
    ir = parsed.to_ir()                          # must not raise
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, MakeGraphOp)
    assert op.direction is None, "an unwritten arrow is not invented"
    assert (op.source.name or None) == source, "the written columns survive"
