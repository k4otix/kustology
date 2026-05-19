# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tests for ``ir.llm_view.to_llm_dict``.

The LLM view is a lossy projection of the IR — discriminator-prefixed,
default-stripped, span-free. Tests pin the shape contract: every node
carries ``kind``, every default value is omitted, and the three operators
with a colliding ``kind`` field (``render``, ``join``, ``lookup``) get
their field renamed in output.
"""

from __future__ import annotations

import pytest

from kustology.ir import (
    IRBuilder, QueryIR, SchemaAttacher, to_llm_dict,
)
from kustology.utils.analysis import build_global_state


STORM_EVENTS_SCHEMA = {
    "StormEvents": {
        "StartTime": "datetime",
        "State": "string",
        "EventType": "string",
        "DeathsDirect": "int",
    }
}


@pytest.fixture(scope="module")
def storm_ir() -> QueryIR:
    gs = build_global_state(STORM_EVENTS_SCHEMA)
    builder = IRBuilder(global_state=gs)
    ir = builder.build(
        'StormEvents '
        '| where State == "TEXAS" and EventType == "Tornado" '
        '| project StartTime, State, EventType, DeathsDirect'
    )
    SchemaAttacher(STORM_EVENTS_SCHEMA).enrich(ir)
    return ir


# Shape and discriminator -------------------------------------------------

def test_top_level_has_query_kind(storm_ir):
    out = to_llm_dict(storm_ir)
    assert out["kind"] == "query"


def test_every_structural_node_has_kind(storm_ir):
    """Spot-check ``kind`` at every model level — top, source, operator,
    nested expression. Universal walking can't distinguish model dicts
    (which carry ``kind``) from domain dicts (``TabularSchema.columns``,
    which don't), so we pin known sites explicitly."""
    out = to_llm_dict(storm_ir)
    assert out["kind"] == "query"
    assert out["main_pipeline"]["kind"] == "pipeline"
    assert out["main_pipeline"]["source"]["kind"] == "table_ref"
    assert out["main_pipeline"]["operators"][0]["kind"] == "filter"
    pred = out["main_pipeline"]["operators"][0]["predicate"]
    assert pred["kind"] == "and"
    assert pred["operands"][0]["kind"] == "bin_op"
    assert pred["operands"][0]["left"]["kind"] == "column_ref"
    assert pred["operands"][0]["right"]["kind"] == "literal"
    assert out["main_pipeline"]["operators"][1]["columns"][0]["kind"] == "column_ref"


def test_storm_pipeline_shape(storm_ir):
    out = to_llm_dict(storm_ir)
    pipeline = out["main_pipeline"]
    assert pipeline["kind"] == "pipeline"
    assert pipeline["source"] == {"kind": "table_ref", "name": "StormEvents"}

    ops = pipeline["operators"]
    assert [op["kind"] for op in ops] == ["filter", "project"]

    filter_op = ops[0]
    assert filter_op["predicate"]["kind"] == "and"
    assert len(filter_op["predicate"]["operands"]) == 2

    project_op = ops[1]
    assert [c["name"] for c in project_op["columns"]] == [
        "StartTime", "State", "EventType", "DeathsDirect",
    ]


# Field stripping ---------------------------------------------------------

def test_spans_are_omitted_everywhere(storm_ir):
    out = to_llm_dict(storm_ir)

    def assert_no_span(node):
        if isinstance(node, dict):
            assert "span" not in node, f"span leaked into {node!r}"
            for v in node.values():
                assert_no_span(v)
        elif isinstance(node, list):
            for v in node:
                assert_no_span(v)

    assert_no_span(out)


def test_default_fields_are_dropped(storm_ir):
    """Default values (``nullable=True``, ``result_type_inner=None``) carry
    no signal for an LLM and are stripped. Non-default values survive."""
    out = to_llm_dict(storm_ir)
    column = out["main_pipeline"]["operators"][1]["columns"][0]
    # Defaults that the binder leaves untouched: dropped.
    assert "nullable" not in column            # default True
    assert "result_type_inner" not in column   # default None
    # Non-defaults that the binder populates: kept.
    assert column["name"] == "StartTime"
    assert column["result_type"] == "datetime"
    assert column["table"] == "StormEvents"    # binder resolved it


def test_empty_diagnostics_array_is_dropped(storm_ir):
    out = to_llm_dict(storm_ir)
    assert "diagnostics" not in out
    assert "parse_warnings" not in out
    assert "let_bindings" not in out


def test_schema_attached_flag_is_dropped(storm_ir):
    """``schema_attached: true`` carries no signal an LLM can't get from the
    presence of ``result_schema`` itself."""
    out = to_llm_dict(storm_ir)
    assert "schema_attached" not in out


def test_redundant_canonical_form_dropped_on_leaves(storm_ir):
    """``canonical_form`` is dropped on ColumnRef when it equals ``name``,
    and on LiteralExpr when it's the canonical restatement of ``value``.
    It survives on subtree expressions where it summarizes the tree."""
    out = to_llm_dict(storm_ir)
    bin_op = out["main_pipeline"]["operators"][0]["predicate"]["operands"][0]

    # Leaf ColumnRef: canonical_form == name → dropped.
    assert "canonical_form" not in bin_op["left"]
    assert bin_op["left"]["name"] == "State"

    # Leaf LiteralExpr: canonical_form == '"TEXAS"' matches value="TEXAS"
    # under the string-quoting rule → dropped.
    assert "canonical_form" not in bin_op["right"]
    assert bin_op["right"]["value"] == "TEXAS"

    # Subtree BinOp: canonical_form summarizes the comparison → kept.
    assert bin_op["canonical_form"] == 'State == "TEXAS"'


def test_enum_values_unwrap_to_strings(storm_ir):
    out = to_llm_dict(storm_ir)
    column = out["main_pipeline"]["operators"][1]["columns"][0]
    assert isinstance(column["result_type"], str)
    assert column["result_type"] == "datetime"


# Polarity collapse ------------------------------------------------------

def test_binop_inclusion_drops_polarity(storm_ir):
    """Positive ``BinOp`` keeps ``op`` as-is and drops the noise ``polarity``
    field. Storm's ``State == "TEXAS"`` is the canonical case."""
    out = to_llm_dict(storm_ir)
    bin_op = out["main_pipeline"]["operators"][0]["predicate"]["operands"][0]
    assert bin_op["op"] == "=="
    assert "polarity" not in bin_op


def test_binop_exclusion_collapses_to_negative_op():
    """``!=`` materializes as ``op: "!="`` (special-cased), polarity gone."""
    ir = IRBuilder().build('T | where x != "y"')
    out = to_llm_dict(ir)
    bin_op = out["main_pipeline"]["operators"][0]["predicate"]
    assert bin_op["op"] == "!="
    assert "polarity" not in bin_op


def test_binop_contains_exclusion_becomes_not_contains():
    """``!contains`` uses the regular ``!``-prefix rule, not a special case."""
    ir = IRBuilder().build('T | where x !contains "y"')
    out = to_llm_dict(ir)
    bin_op = out["main_pipeline"]["operators"][0]["predicate"]
    assert bin_op["op"] == "!contains"
    assert "polarity" not in bin_op


def test_setmembership_synthesizes_in_op():
    """``SetMembership`` has no ``op`` field on the model; the LLM view
    synthesizes one from polarity (``in`` / ``!in``)."""
    ir_pos = IRBuilder().build("T | where x in (1, 2, 3)")
    op_pos = to_llm_dict(ir_pos)["main_pipeline"]["operators"][0]["predicate"]
    assert op_pos["op"] == "in"
    assert "polarity" not in op_pos

    ir_neg = IRBuilder().build("T | where x !in (1, 2, 3)")
    op_neg = to_llm_dict(ir_neg)["main_pipeline"]["operators"][0]["predicate"]
    assert op_neg["op"] == "!in"
    assert "polarity" not in op_neg


def test_between_synthesizes_between_op():
    ir_pos = IRBuilder().build("T | where x between (1 .. 10)")
    op_pos = to_llm_dict(ir_pos)["main_pipeline"]["operators"][0]["predicate"]
    assert op_pos["op"] == "between"
    assert "polarity" not in op_pos

    ir_neg = IRBuilder().build("T | where x !between (1 .. 10)")
    op_neg = to_llm_dict(ir_neg)["main_pipeline"]["operators"][0]["predicate"]
    assert op_neg["op"] == "!between"
    assert "polarity" not in op_neg


# Collision-renamed fields -----------------------------------------------

def test_join_kind_field_is_renamed():
    """``JoinOp.kind`` becomes ``join_kind`` in LLM output."""
    ir = IRBuilder().build("T | join kind=inner (U) on x")
    out = to_llm_dict(ir)
    join = out["main_pipeline"]["operators"][0]
    assert join["kind"] == "join"          # the discriminator
    assert join["join_kind"] == "inner"    # renamed from .kind


def test_render_kind_field_is_renamed():
    ir = IRBuilder().build("T | summarize count() by x | render barchart")
    out = to_llm_dict(ir)
    render = out["main_pipeline"]["operators"][-1]
    assert render["kind"] == "render"
    assert render["render_kind"] == "barchart"


def test_lookup_kind_field_is_renamed():
    ir = IRBuilder().build("T | lookup kind=leftouter (U) on x")
    out = to_llm_dict(ir)
    lookup = out["main_pipeline"]["operators"][0]
    assert lookup["kind"] == "lookup"
    assert lookup["lookup_kind"] == "leftouter"


# KIND coverage ----------------------------------------------------------

def test_every_ir_model_class_has_kind_constant():
    """Every BaseModel subclass exported from ``kustology.ir`` must
    declare a ``KIND`` class constant. Catches drift when a new operator
    is added without updating the LLM discriminator vocabulary."""
    from pydantic import BaseModel
    import kustology.ir as ir_pkg

    # ``Span`` is stripped from LLM output entirely, so it needs no KIND.
    EXEMPT = {"Span"}

    missing: list[str] = []
    for name in ir_pkg.__all__:
        if name in EXEMPT:
            continue
        obj = getattr(ir_pkg, name, None)
        if not isinstance(obj, type) or not issubclass(obj, BaseModel):
            continue
        if not hasattr(obj, "KIND") or not isinstance(obj.KIND, str):
            missing.append(name)
    assert not missing, f"classes without KIND: {missing}"


def test_kind_values_are_unique_per_class():
    """Two different IR classes must not share a KIND string."""
    from pydantic import BaseModel
    import kustology.ir as ir_pkg

    seen: dict[str, str] = {}
    for name in ir_pkg.__all__:
        obj = getattr(ir_pkg, name, None)
        if not isinstance(obj, type) or not issubclass(obj, BaseModel):
            continue
        kind = getattr(obj, "KIND", None)
        if not isinstance(kind, str):
            continue
        if kind in seen and seen[kind] != name:
            pytest.fail(f"KIND collision: {seen[kind]} and {name} both = {kind!r}")
        seen[kind] = name


# Convenience method on QueryIR ------------------------------------------

def test_query_ir_has_to_llm_dict_method(storm_ir):
    """``QueryIR.to_llm_dict()`` is a thin delegator that returns the
    same result as the module-level function."""
    assert storm_ir.to_llm_dict() == to_llm_dict(storm_ir)


# Round-trip safety ------------------------------------------------------

def test_canonical_serialization_still_round_trips(storm_ir):
    """Adding ``ClassVar[KIND]`` must not affect ``model_dump_json``."""
    dumped = storm_ir.model_dump_json()
    reloaded = QueryIR.model_validate_json(dumped)
    assert storm_ir.model_dump() == reloaded.model_dump()
