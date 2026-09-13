# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier-2 coverage for graph-match and graph-shortest-paths patterns."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import (
    Assignment,
    ColumnRef,
    FuncCall,
    GraphElementRef,
    GraphMatchOp,
    GraphShortestPathsOp,
    LiteralExpr,
    find_all,
)

GRAPH = "T | make-graph a --> b | "


def _last_op(query: str):
    return parse(query).to_ir().main_pipeline.operators[-1]


def test_a_pattern_records_every_element_with_its_direction_and_hop_range():
    op = _last_op(
        GRAPH + "graph-match cycles=none (x)-[e1*1..3]->(y)<-[e2]-(z)-[e3]-(w) "
        "where x.p == 1 project n1=x.p, y",
    )
    assert isinstance(op, GraphMatchOp)
    assert op.cycles == "none"
    assert len(op.patterns) == 1

    elements = op.patterns[0].elements
    assert [e.name for e in elements] == ["x", "e1", "y", "e2", "z", "e3", "w"]
    assert [e.direction for e in elements if e.kind == "graph_pattern_edge"] == [
        "forward", "backward", "any",
    ]
    e1 = elements[1]
    assert e1.variable_length is True
    assert (e1.min_hops, e1.max_hops) == (1, 3)
    assert elements[3].variable_length is False
    assert (elements[3].min_hops, elements[3].max_hops) == (None, None)


def test_an_unwritten_hop_bound_stays_none():
    """A written bound is a literal; an unwritten one is a zero-width
    ``NameReference`` the parser marks missing. Reading it as a literal would
    record a hop count the query never wrote.
    """
    op = _last_op(GRAPH + "graph-match (x)-[e*1..]->(y) project x")
    edge = op.patterns[0].elements[1]
    assert edge.variable_length is True
    assert (edge.min_hops, edge.max_hops) == (1, None)


@pytest.mark.parametrize(
    "arrow,direction",
    [("-->", "forward"), ("<--", "backward"), ("--", "any")],
    ids=["forward", "backward", "any"],
)
def test_a_bracket_free_arrow_carries_the_direction_it_is_written_with(arrow, direction):
    """Each bracket-free arrow is one token, so the edge writes no name and no
    hop range. The clean-parse assertion is the claim that all three spellings
    are accepted.
    """
    ir = parse(f"{GRAPH}graph-match (x){arrow}(y) project x").to_ir()
    assert not [d for d in ir.diagnostics if d.severity == "Error"], ir.diagnostics
    edge = ir.main_pipeline.operators[-1].patterns[0].elements[1]
    assert edge.kind == "graph_pattern_edge"
    assert edge.direction == direction
    assert edge.name is None
    assert edge.variable_length is False


def test_a_computed_hop_bound_is_the_expression_the_query_wrote():
    """A hop bound is an expression position. Narrowing the field to ``int``
    would hash ``*1..toint(3)`` as an unbounded pattern.
    """
    ir = parse(f"{GRAPH}graph-match (x)-[e*1..toint(3)]->(y) project x").to_ir()
    assert not [d for d in ir.diagnostics if d.severity == "Error"], ir.diagnostics
    edge = ir.main_pipeline.operators[-1].patterns[0].elements[1]
    assert edge.min_hops == 1
    assert isinstance(edge.max_hops, FuncCall)
    assert edge.max_hops.name == "toint"


def test_a_boolean_hop_bound_keeps_its_literal_kind():
    """``*1..true`` parses without a diagnostic and its ``LiteralValue`` is a
    Python ``bool``, which ``int()`` reads as 1. The bound stays the literal
    the query wrote, so it does not collide with ``*1..1``.
    """
    op = _last_op(f"{GRAPH}graph-match (x)-[e*1..true]->(y) project x")
    bound = op.patterns[0].elements[1].max_hops
    assert isinstance(bound, LiteralExpr)
    assert bound.value is True


def test_an_anonymous_element_keeps_its_name_unset():
    op = _last_op(GRAPH + "graph-match ()-[]->(m) project m")
    assert [e.name for e in op.patterns[0].elements] == [None, None, "m"]


def test_a_qualified_property_is_a_column_and_a_bare_element_is_not():
    """``x.p`` reads a property of the element bound to ``x``, so it is the
    column ``p``. Bare ``y`` is the element itself, which is neither a table
    nor a column, so it gets its own node the way ``LetValueRef`` does.
    """
    op = _last_op(
        GRAPH + "graph-match (x)-[e]->(y) where x.p == 1 project n1=x.p, y",
    )
    columns = list(find_all(op, ColumnRef))
    assert {c.name for c in columns} == {"p"}
    assert {c.qualifier for c in columns} == {"x"}
    assert "x" not in {c.name for c in columns}

    refs = find_all(op, GraphElementRef)
    assert [r.name for r in refs] == ["y"]


def test_a_named_projection_is_an_assignment():
    op = _last_op(GRAPH + "graph-match (x)-[e]->(y) project n1=x.p")
    assert isinstance(op.project[0], Assignment)
    assert op.project[0].name == "n1"


def test_two_comma_separated_patterns_are_two_patterns_in_source_order():
    query = GRAPH + "graph-match (x)-[e]->(y), (z)-[f]->(w) project x"
    op = _last_op(query)
    assert [[el.name for el in p.elements] for p in op.patterns] == [
        ["x", "e", "y"], ["z", "f", "w"],
    ]
    spans = [p.span for p in op.patterns]
    assert [query[s.text_start:s.text_start + s.width] for s in spans] == [
        "(x)-[e]->(y)", "(z)-[f]->(w)",
    ]


def test_graph_shortest_paths_records_output_and_cycles():
    op = _last_op(
        GRAPH + "graph-shortest-paths output=any cycles=none (x)-[e*1..5]->(y) "
        "where y.q == 2 project p=x.p",
    )
    assert isinstance(op, GraphShortestPathsOp)
    assert op.output == "any"
    assert op.cycles == "none"
    assert op.where is not None
    assert op.patterns[0].elements[1].max_hops == 5


def test_an_out_of_vocabulary_parameter_value_stays_none():
    """The parser accepts ``cycles=bogus`` without a diagnostic. The Literal
    must not turn a value Microsoft tolerates into a ValidationError out of
    ``to_ir()``.
    """
    op = _last_op(GRAPH + "graph-match cycles=bogus (x)-[e]->(y) project x")
    assert op.cycles is None


def test_the_element_scope_does_not_leak_past_the_operator():
    """Negative control on the qualifier scope: a bare ``x`` after the graph
    operator is an ordinary column reference again.
    """
    ir = parse(GRAPH + "graph-match (x)-[e]->(y) project x | where x > 1").to_ir()
    outer = ir.main_pipeline.operators[-1].predicate.left
    assert isinstance(outer, ColumnRef)
    assert outer.qualifier is None
