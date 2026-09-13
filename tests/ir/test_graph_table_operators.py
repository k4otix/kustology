# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier-2 coverage for graph-mark-components, graph-to-table, macro-expand."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import (
    FuncCall,
    GraphMarkComponentsOp,
    GraphToTableOp,
    MacroExpandOp,
    PathExpr,
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
