# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier-2 coverage for graph-mark-components, graph-to-table, macro-expand."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import (
    ColumnRef,
    FuncCall,
    GraphMarkComponentsOp,
    GraphToTableOp,
    LetValueRef,
    LiteralExpr,
    MacroExpandOp,
    PathExpr,
    TableRef,
    find_all,
)

GRAPH = "T | make-graph a --> b | "


def _last_op(query: str):
    ir = parse(query).to_ir()
    return ir.main_pipeline.operators[-1]


def test_graph_mark_components_records_its_kind_and_id_column():
    op = _last_op(GRAPH + "graph-mark-components kind=weak with_component_id=cid")
    assert isinstance(op, GraphMarkComponentsOp)
    assert op.component_kind == "weak"
    assert op.with_component_id == "cid"


def test_graph_mark_components_rejects_a_kind_the_engine_rejects():
    """``kind=bogus`` draws Microsoft's own "Expected one of: weak, strong".
    The Literal must not turn that into a ValidationError out of ``to_ir()``.
    """
    op = _last_op(GRAPH + "graph-mark-components kind=bogus")
    assert op.component_kind is None


def test_graph_to_table_records_each_output_with_its_alias_and_id_columns():
    op = _last_op(
        GRAPH + "graph-to-table nodes as N with_node_id=nid, "
        "edges as E with_source_id=sid with_target_id=tid"
    )
    assert isinstance(op, GraphToTableOp)
    assert [o.entity for o in op.outputs] == ["nodes", "edges"]
    assert [o.alias for o in op.outputs] == ["N", "E"]
    assert op.outputs[0].node_id == "nid"
    assert op.outputs[1].source_id == "sid"
    assert op.outputs[1].target_id == "tid"


def test_graph_to_table_skips_a_malformed_output_clause():
    """A clause whose ``EntityKeyword`` is missing carries no ``Parameters``
    either, so the builder skips it rather than raising on the Literal.
    """
    op = _last_op(GRAPH + "graph-to-table nodes, bogus")
    assert [o.entity for o in op.outputs] == ["nodes"]


def test_macro_expand_over_a_named_entity_group():
    ir = parse("macro-expand EG as X (X.T | count)").to_ir()
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, MacroExpandOp)
    assert op.entity_group_name == "EG"
    assert op.entities == []
    assert op.alias == "X"
    assert op.pipeline is not None
    assert [o.kind for o in op.pipeline.operators] == ["count"]


def test_macro_expand_over_an_inline_entity_group_types_each_entity():
    """The entities are expressions, not text. Recording them as node text
    would carry whitespace sensitivity that no other modelled field has.
    """
    ir = parse(
        "macro-expand entity_group "
        "[cluster('c1').database('d1'), cluster('c2').database('d2')] "
        "as X (X.T | count)"
    ).to_ir()
    op = ir.main_pipeline.operators[0]
    assert op.entity_group_name is None
    assert len(op.entities) == 2
    assert all(isinstance(e, PathExpr) for e in op.entities)
    assert {c.name for c in find_all(op.entities[0], FuncCall)} == {"cluster", "database"}
    assert op.alias == "X"


def test_macro_expand_body_pipeline_skips_a_leading_let_statement():
    """A ``LetStatement`` in the body exposes ``.Expression`` too (its
    right-hand-side value), so the statement that fills ``pipeline`` must be
    picked by class, not by duck-typing on that attribute alone.
    """
    ir = parse("macro-expand EG as X (let y = 1; X.T | where a > y)").to_ir()
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, MacroExpandOp)
    assert isinstance(op.pipeline.source, TableRef)
    assert op.pipeline.source.name == "T"
    assert [o.kind for o in op.pipeline.operators] == ["filter"]
    assert ir.additional_pipelines == []


def test_macro_expand_body_let_binding_reaches_body_lets_not_the_top_level():
    """The body's own ``let`` is excluded from the top-level ``let`` and
    statement sweeps the same way its ``ExpressionStatement`` sibling is, so
    it does not surface in ``let_bindings``, ``additional_pipelines``, or
    ``statements`` — it lands on ``MacroExpandOp.body_lets`` instead.
    """
    ir = parse("macro-expand EG as X (let y = 1; X.T | where a > y)").to_ir()
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, MacroExpandOp)
    assert [lb.name for lb in op.body_lets] == ["y"]
    assert isinstance(op.body_lets[0].rhs_expr, LiteralExpr)
    assert op.body_lets[0].rhs_expr.value == 1
    assert ir.let_bindings == []
    assert ir.additional_pipelines == []
    assert ir.statements == []


def test_macro_expand_body_let_is_a_let_value_ref_inside_the_body():
    """A name a body ``let`` binds reads as a value reference inside the
    body's own pipeline, the same reach a ``let``-function's body ``let``
    gets — not a plain column, which would make ``find_all(ir, ColumnRef)``
    report a column that does not exist.
    """
    ir = parse("macro-expand EG as X (let y = 1; X.T | where a > y)").to_ir()
    op = ir.main_pipeline.operators[0]
    predicate = op.pipeline.operators[0].predicate
    assert isinstance(predicate.right, LetValueRef)
    assert predicate.right.name == "y"


def test_macro_expand_body_let_does_not_leak_past_the_operator():
    """``_let_names`` is restored once the body closes, so a same-named
    column read after the operator is an ordinary column, not a value
    reference into a binding the outer query never wrote.
    """
    ir = parse(
        "macro-expand EG as X (let y = 1; X.T | where a > y) | where y == 2"
    ).to_ir()
    outer_filter = ir.main_pipeline.operators[1]
    assert isinstance(outer_filter.predicate.left, ColumnRef)
    assert outer_filter.predicate.left.name == "y"
    assert list(find_all(outer_filter, LetValueRef)) == []


def test_macro_expand_bodys_statements_are_scoped_to_the_operator_in_source_order():
    """``.StatementList`` admits every statement kind the top-level query's
    own statement list does, beside ``let`` and the tabular tail, so each
    lands on ``body_statements`` in source order rather than being dropped
    the way the top-level sweep drops it (``_is_a_top_level_statement``
    excludes a ``MacroExpandOperator`` ancestor).
    """
    ir = parse(
        "macro-expand EG as X ("
        "set querytrace; "
        'declare pattern P = (a:string) { ("x") = { T | take 1 }; }; '
        "alias database d = cluster('c').database('d'); "
        "restrict access to (V); "
        "declare query_parameters(p:long = 1); "
        "X.T | count)"
    ).to_ir()
    op = ir.main_pipeline.operators[0]
    assert [type(s).__name__ for s in op.body_statements] == [
        "SetOptionStmt", "PatternStmt", "AliasStmt", "RestrictStmt",
        "QueryParametersStmt",
    ]
    assert op.body_statements[0].name == "querytrace"
    assert op.body_statements[1].name == "P"
    assert op.body_statements[2].name == "d"
    assert isinstance(op.body_statements[3].expressions[0], ColumnRef)
    assert [p.decl.name for p in op.body_statements[4].parameters] == ["p"]
    assert ir.statements == []
    assert ir.let_bindings == []
