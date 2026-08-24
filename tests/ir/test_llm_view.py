# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tests for ``ir.llm_view.to_llm_dict``.

The LLM view is a lossy projection of the IR — discriminator-prefixed,
default-stripped, span-free. Tests pin the shape contract: every node
carries ``kind``, the root additionally carries ``ir_schema_version``, and
every default value is omitted. The three operators whose KQL ``kind``
clause would collide with that discriminator (``render``, ``join``,
``lookup``) are pinned too — the model fields are named ``render_kind`` /
``join_kind`` / ``lookup_kind``, so the view renames nothing and both keys
survive side by side.
"""

from __future__ import annotations

import pytest

from kustology.ir import (
    IRBuilder,
    QueryIR,
    to_llm_dict,
)
from kustology.ir.binder import SchemaAttacher
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
    """Default values (``result_type_inner=None``) carry no signal for an
    LLM and are stripped. Non-default values survive."""
    out = to_llm_dict(storm_ir)
    column = out["main_pipeline"]["operators"][1]["columns"][0]
    # Defaults that the binder leaves untouched: dropped.
    assert "result_type_inner" not in column   # default None
    # Non-defaults that the binder populates: kept.
    assert column["name"] == "StartTime"
    assert column["result_type"] == "datetime"
    assert column["table"] == "StormEvents"    # binder resolved it


def test_empty_diagnostics_array_is_dropped(storm_ir):
    out = to_llm_dict(storm_ir)
    assert "diagnostics" not in out
    assert "let_bindings" not in out


def test_schema_attached_flag_is_dropped(storm_ir):
    """``schema_attached: true`` carries no signal an LLM can't get from the
    presence of ``result_schema`` itself."""
    out = to_llm_dict(storm_ir)
    assert "schema_attached" not in out


def test_operator_result_schema_is_dropped_but_the_pipeline_keeps_its_own(storm_ir):
    """``Operator.result_schema`` restates the pipeline's column list once per
    operator, which is what the view exists to avoid.

    ``Pipeline.result_schema`` answers "what does this query return"; the
    per-operator copy answers the same question once per step, and on a
    bound parse most steps give the same answer. Measured over the 49-query
    fixture corpus against a schema covering every referenced column, the
    per-operator copies were 35% of the whole bound LLM view — 295,156 of
    851,224 bytes.

    ``model_dump_json`` keeps every one of them — this is a view decision,
    the same call ``_cap_datatable_rows`` makes about ``rows``.
    """
    out = to_llm_dict(storm_ir)
    pipeline = out["main_pipeline"]

    # Both operators really do carry one on the model, so the assertion below
    # is about the view and not about an unpopulated field.
    assert [op.result_schema.columns for op in storm_ir.main_pipeline.operators] == [
        {"StartTime": "datetime", "State": "string",
         "EventType": "string", "DeathsDirect": "int"},
    ] * 2

    assert [op["kind"] for op in pipeline["operators"]] == ["filter", "project"]
    for op in pipeline["operators"]:
        assert "result_schema" not in op

    # The pipeline's own survives, populated.
    assert pipeline["result_schema"]["columns"]["DeathsDirect"] == "int"

    # And the lossless dump is untouched.
    assert '"result_schema"' in storm_ir.model_dump_json()
    assert storm_ir.model_dump_json().count('"DeathsDirect":"int"') == 3


def test_redundant_canonical_form_dropped_on_leaves(storm_ir):
    """``canonical_form`` is dropped on ColumnRef when it restates ``name``
    (bare or ``table.name`` for bound nodes), and on LiteralExpr when it's
    the canonical restatement of ``value``. It survives on subtree
    expressions where it summarizes the tree."""
    out = to_llm_dict(storm_ir)
    bin_op = out["main_pipeline"]["operators"][0]["predicate"]["operands"][0]

    # Leaf ColumnRef: canonical_form == "StormEvents.State" matches
    # table + name under the bound-canonical rule → dropped.
    assert "canonical_form" not in bin_op["left"]
    assert bin_op["left"]["name"] == "State"
    assert bin_op["left"]["table"] == "StormEvents"

    # Leaf LiteralExpr: canonical_form == '"TEXAS"' matches value="TEXAS"
    # under the string-quoting rule → dropped.
    assert "canonical_form" not in bin_op["right"]
    assert bin_op["right"]["value"] == "TEXAS"

    # Subtree BinOp: canonical_form summarizes the comparison and now
    # carries the bound table-qualified column → kept.
    assert bin_op["canonical_form"] == 'StormEvents.State == "TEXAS"'


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


def test_setmembership_shows_its_real_operator():
    """``SetMembership.op`` is carried on the model, so the view surfaces it
    rather than synthesizing one from polarity.

    Synthesizing labelled ``has_any`` and ``has_all`` as ``in``, and since
    ``case_sensitive`` defaults to False the default-stripping pass removed
    that too -- a model was shown ``has_all`` as a case-sensitive ``in``.
    """
    for op in ("in", "!in", "in~", "!in~", "has_any", "has_all"):
        ir = IRBuilder().build(f'T | where x {op} ("a", "b")')
        pred = to_llm_dict(ir)["main_pipeline"]["operators"][0]["predicate"]
        assert pred["op"] == op, op
        assert "polarity" not in pred


def test_between_synthesizes_between_op():
    ir_pos = IRBuilder().build("T | where x between (1 .. 10)")
    op_pos = to_llm_dict(ir_pos)["main_pipeline"]["operators"][0]["predicate"]
    assert op_pos["op"] == "between"
    assert "polarity" not in op_pos

    ir_neg = IRBuilder().build("T | where x !between (1 .. 10)")
    op_neg = to_llm_dict(ir_neg)["main_pipeline"]["operators"][0]["predicate"]
    assert op_neg["op"] == "!between"
    assert "polarity" not in op_neg


# KQL ``kind`` clauses that would collide with the discriminator ---------

def test_join_kind_field_is_renamed():
    """``join kind=inner`` and the ``join`` discriminator both reach the dump.

    The model field is called ``join_kind``, so the view renames nothing --
    the collision was designed out at the model rather than patched in the
    projection. What is pinned here is that both keys survive, which is what
    a rename in either direction would break.
    """
    ir = IRBuilder().build("T | join kind=inner (U) on x")
    out = to_llm_dict(ir)
    join = out["main_pipeline"]["operators"][0]
    assert join["kind"] == "join"          # the discriminator
    assert join["join_kind"] == "inner"    # the KQL clause


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


def test_llm_view_kind_comes_from_the_model_field():
    """KIND ClassVars are gone; the view reads the pydantic discriminator
    default, so the two can never disagree (the drift IR-5 warned about)."""
    from kustology.ir import FilterOp

    assert not hasattr(FilterOp, "KIND")
    assert FilterOp.model_fields["kind"].default == "filter"


def test_every_ir_model_class_has_kind_field():
    """Every BaseModel subclass exported from ``kustology.ir`` must
    declare a ``kind`` field with a ``Literal`` default. Catches drift
    when a new operator is added without updating the LLM discriminator
    vocabulary.

    This test used to check for a ``KIND`` ClassVar. Task 4 converted the
    four big operator/source/tables unions to ``Field(discriminator="kind")``,
    which now enforces (at model-build time) that every member of those
    unions carries a working ``kind`` discriminator. But ``Expr`` subclasses
    are gathered into the smart-mode ``AnyExpr`` union, which is a plain
    ``Union`` rather than a discriminated one, so nothing else guarantees
    every ``Expr`` class still carries a ``kind`` field. This test is what
    is left to catch that after the ``KIND`` ClassVar's removal.
    """
    from pydantic import BaseModel

    import kustology.ir as ir_pkg

    # ``Span`` is stripped from LLM output entirely, so it needs no ``kind``.
    # ``Finding`` is an analyzer-output shape, not a parsed-query node,
    # and never reaches ``to_llm_dict``.
    EXEMPT = {"Span", "Finding"}

    missing: list[str] = []
    for name in ir_pkg.__all__:
        if name in EXEMPT:
            continue
        obj = getattr(ir_pkg, name, None)
        if not isinstance(obj, type) or not issubclass(obj, BaseModel):
            continue
        default = obj.model_fields["kind"].default if "kind" in obj.model_fields else None
        if not isinstance(default, str):
            missing.append(name)
    assert not missing, f"classes without a kind field default: {missing}"


def test_kind_values_are_unique_per_class():
    """Two different IR classes must not share a ``kind`` default.

    Discriminated unions (Task 4) already reject a duplicate discriminator
    value for the classes they cover, but ``Expr`` subclasses sit outside
    any discriminated union (see ``test_every_ir_model_class_has_kind_field``),
    so this test is the only thing still checking uniqueness across the
    full exported vocabulary, ``Expr`` included.
    """
    from pydantic import BaseModel

    import kustology.ir as ir_pkg

    seen: dict[str, str] = {}
    for name in ir_pkg.__all__:
        obj = getattr(ir_pkg, name, None)
        if not isinstance(obj, type) or not issubclass(obj, BaseModel):
            continue
        if "kind" not in obj.model_fields:
            continue
        kind = obj.model_fields["kind"].default
        if not isinstance(kind, str):
            continue
        if kind in seen and seen[kind] != name:
            pytest.fail(f"kind collision: {seen[kind]} and {name} both = {kind!r}")
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


def test_dispatch_survives_a_class_rename():
    """The view's rules dispatch on class identity, not on class *name*.

    They used to compare ``cls.__name__`` against string literals, so
    renaming a class would silently stop every rule that mentioned it --
    no error, just a quietly worse LLM view. A subclass is the cheapest
    way to prove identity dispatch: it has a different ``__name__`` and
    must still get the rules.
    """
    from kustology.ir.expr import ColumnRef, SetMembership
    from kustology.ir.llm_view import (
        _collapse_polarity_into_op,
        _drop_redundant_canonical_form,
    )

    class RenamedColumnRef(ColumnRef):
        pass

    class RenamedSetMembership(SetMembership):
        pass

    out = {"canonical_form": "Account", "name": "Account", "table": None}
    _drop_redundant_canonical_form(out, RenamedColumnRef)
    assert "canonical_form" not in out

    # SetMembership carries its own op, so the rule only drops the
    # now-redundant polarity -- but it still has to *fire*, which is what
    # identity dispatch buys.
    out2 = {"polarity": "exclusion", "op": "!in~"}
    _collapse_polarity_into_op(out2, RenamedSetMembership)
    assert out2 == {"op": "!in~"}


def test_body_span_is_omitted_from_a_let_function():
    """``_OMIT_FIELDS`` matched the name ``span`` exactly, so ``LetFunction``
    -- the one model whose span field is called ``body_span`` -- shipped a
    raw character offset into the LLM view that every other node was spared.
    """
    ir = IRBuilder().build("let f = (x:int){x+1}; T | extend y = f(a)")
    fn = to_llm_dict(ir)["let_bindings"][0]["rhs_function"]

    # The node is really there, so the missing key below is a strip and not
    # an absent function.
    assert fn["kind"] == "let_function"
    assert fn["parameters"] == ["x"]

    assert "body_span" not in fn


def test_the_llm_view_is_tagged_with_the_ir_schema_version():
    """A dump handed to a model, cached, or written to disk had nothing on it
    saying which IR shape produced it.

    ``model_dump_json`` round-trips through pydantic, which validates the
    shape and fails loudly on drift; the LLM view is a lossy projection with
    no validator behind it, so a consumer holding one from an earlier release
    had no way to tell -- the fields it expected were simply absent, which
    reads identically to a query that did not use them. The tag is the same
    ``IR_SCHEMA_VERSION`` the CLI's JSON envelope already publishes, so the
    two agree by construction.
    """
    from kustology.ir import IR_SCHEMA_VERSION

    dumped = to_llm_dict(IRBuilder().build("T | count"))
    assert dumped["ir_schema_version"] == IR_SCHEMA_VERSION
    assert dumped["ir_schema_version"] == "0.2"


def test_only_the_root_carries_the_schema_version():
    """It tags the document, not every node. A per-node copy would be noise
    in the context window the view exists to conserve."""
    dumped = to_llm_dict(IRBuilder().build("T | where a == 1 | count"))
    assert "ir_schema_version" not in dumped["main_pipeline"]
    assert "ir_schema_version" not in dumped["main_pipeline"]["operators"][0]

    # And a sub-tree dumped on its own is not a document, so it is untagged.
    sub = to_llm_dict(IRBuilder().build("T | count").main_pipeline)
    assert "ir_schema_version" not in sub


def test_the_null_flag_strip_is_scoped_to_binop():
    """``polarity``/``case_sensitive`` are stripped when ``None`` *on BinOp*,
    where ``None`` means "the operator is arithmetic, so neither question
    applies". The strip used to be typeless -- it took only the output dict
    -- so it reached into every node in the IR, and any future model with a
    legitimately-optional ``case_sensitive`` would have had it silently
    removed from the view with no way to tell an absent field from a null
    one.

    Exercised through a plain ``BaseModel`` rather than an ``Expr``
    subclass: defining one of those inside a test registers it on
    ``Expr.__subclasses__()`` for the rest of the session, which
    ``test_canonical_coverage`` walks.
    """
    from pydantic import BaseModel

    from kustology.ir.llm_view import _drop_inapplicable_operator_flags

    class NotABinOp(BaseModel):
        # Required, so the default-stripping pass cannot drop it either.
        polarity: str | None
        case_sensitive: bool | None

    out = to_llm_dict(NotABinOp(polarity=None, case_sensitive=None))
    assert out["polarity"] is None
    assert out["case_sensitive"] is None

    # And the helper itself declines the class, rather than the result above
    # depending on some other pass having run first.
    direct = {"polarity": None, "case_sensitive": None}
    _drop_inapplicable_operator_flags(direct, NotABinOp)
    assert direct == {"polarity": None, "case_sensitive": None}


def test_the_null_flag_strip_still_fires_on_an_arithmetic_binop():
    """The other side of the scoping: it must still do its job."""
    from kustology.ir import BinOp, find_all

    dumped = to_llm_dict(next(iter(find_all(IRBuilder().build("T | extend y = a + 2"), BinOp))))
    assert dumped["op"] == "+"
    assert "polarity" not in dumped
    assert "case_sensitive" not in dumped
