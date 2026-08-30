# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Test the provenance and honesty contracts of ``SchemaAttacher``.

Provenance is this walk's own: every ``ColumnRef`` gets the table it came
from, across ``project``, ``summarize``, joins, unions, ``search``, and
``let`` threading. Nothing else in the library supplies it, and most of this
file covers it.

Honesty is the other contract: ``Pipeline.result_schema`` is Microsoft's
``ResultType`` or ``None``, with no hand-rolled per-operator schema rules.
``tests/ir/test_binder_oracle.py`` pins the schemas themselves against the
binder; this file pins where an answer appears, where ``None`` appears, and
that ``None`` and an empty ``TabularSchema`` stay distinguishable.
"""

import pytest

from kustology import parse
from kustology.ir import ColumnRef, IRBuilder, Pipeline, find_all
from kustology.ir.binder import SchemaAttacher


@pytest.fixture(scope="module")
def schema():
    return {
        "DeviceProcessEvents": {
            "FileName": "string",
            "AccountName": "string",
            "DeviceName": "string",
            "TimeGenerated": "datetime",
            "ProcessId": "long",
        },
        "DeviceFileEvents": {
            "DeviceId": "string",
            "FileName": "string",
            "TimeGenerated": "datetime",
        },
    }


@pytest.fixture
def builder():
    return IRBuilder()


@pytest.fixture
def attacher(schema):
    return SchemaAttacher(schema)


# Honesty: where Microsoft declines, so do we ----------------------------


def test_an_open_symbol_gets_no_invented_schema():
    """Partial schemas are the norm. Where the table is not in the dict,
    Microsoft leaves the symbol open and ``result_schema`` is ``None``."""
    ir = parse("Unknown | project a, b").to_ir(attach_schema={"T": {"a": "long"}})
    (op,) = ir.main_pipeline.operators
    assert op.result_schema is None
    assert ir.main_pipeline.result_schema is None


def test_provenance_still_fills_under_an_open_symbol():
    """Honesty must not cost provenance: a column read from a table the dict
    describes keeps its table even when a later operator is open."""
    q = "T | where a > 1 | lookup Unknown on a | project a"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import FilterOp

    (where_op,) = [
        op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)
    ]
    assert {c.table for c in find_all(where_op, ColumnRef)} == {"T"}


def test_a_datatable_root_closes_with_no_schema_dict_at_all():
    """A ``datatable`` declares its own schema, so ``to_ir()`` with no
    ``attach_schema`` still lands a real one: the symbol is never open."""
    ir = parse("datatable(a:long)[1] | project a").to_ir()
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


def test_a_symbol_can_close_mid_pipeline_over_an_undescribed_table():
    """``IsOpen`` is per node, not per query.

    ``T | count`` returns ``Count:long`` whatever ``T`` is, so the binder
    closes the symbol there with no schema dict at all. ``getschema``
    answers the same way: its four columns describe the input's shape, so
    they can be named without knowing it.
    """
    assert parse("T | count").to_ir().main_pipeline.result_schema.columns == {
        "Count": "long",
    }
    assert parse("T | getschema").to_ir().main_pipeline.result_schema.columns == {
        "ColumnName": "string",
        "ColumnOrdinal": "long",
        "DataType": "string",
        "ColumnType": "string",
    }


def test_really_emitting_nothing_and_not_knowing_are_different_answers():
    """``columns={}`` claims "this emits no columns"; ``None`` claims nothing.

    ``project-away *`` produces a genuinely empty schema: the bound symbol
    closes empty and the stamp carries it. Without a schema the same query
    is open, and stamping ``{}`` there would make a query over an
    undescribed table indistinguishable from one that returns nothing.
    """
    closed = _dict_path("T | project-away *")
    assert closed.main_pipeline.result_schema is not None
    assert closed.main_pipeline.result_schema.columns == {}
    assert parse("T | project-away *").to_ir().main_pipeline.result_schema is None


def test_pipeline_result_schema_populated_after_enrich(schema):
    """The ``TabularSchema`` plumbing itself: Microsoft's answer arrives."""
    from kustology.ir import TabularSchema

    ir = parse("DeviceProcessEvents | project FileName, AccountName").to_ir(
        attach_schema=schema,
    )
    result = ir.main_pipeline.result_schema
    assert isinstance(result, TabularSchema)
    assert set(result.columns.keys()) == {"FileName", "AccountName"}


# The three schema value shapes the public dict path has to accept ----------


@pytest.mark.parametrize("value,expect_type", [
    ({"a": "long"}, "long"),          # typed columns
    ("(a:long, b:string)", "long"),   # a Kusto schema string
    (["a"], "string"),                # untyped columns, treated as string
])
def test_every_documented_schema_value_shape_reaches_the_walk(value, expect_type):
    """``to_ir(attach_schema=…)`` takes all three value shapes that
    ``parse(schema=…)`` and ``build_global_state`` document: it is the same
    schema argument on a different entry point.

    ``SchemaAttacher`` reads ``schemas[table][column]`` and takes only the
    first shape, so the other two are normalized before they reach it. Both
    halves are asserted: Microsoft's schema and the walk's provenance.
    """
    ir = parse("T | project a | where a > 1").to_ir(attach_schema={"T": value})
    assert ir.main_pipeline.result_schema.columns == {"a": expect_type}
    assert {c.table for c in find_all(ir, ColumnRef)} == {"T"}


def test_a_schema_string_does_not_crash_the_walks_type_fallback():
    """A string-valued schema entry rides the public path unharmed.

    ``core.to_ir`` normalizes every schema shape through
    ``build_global_state`` before ``SchemaAttacher`` runs, so a raw string
    never reaches ``_fill``'s type fallback, whose
    ``schemas[table].get(name)`` read raises ``AttributeError`` against a
    string value.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ir = parse("T | project n").to_ir(attach_schema={"T": "(n:bogus)"})
    assert ir.main_pipeline.result_schema.columns == {"n": "unknown"}
    assert {c.table for c in find_all(ir, ColumnRef)} == {"T"}


def test_a_schema_string_does_not_crash_the_search_seeding():
    """The second crash site for a string-valued schema entry.

    ``search`` seeds ``ScopeEntry(columns=dict(...))`` from the schema
    value, and ``dict("(a:long)")`` raises ``ValueError``. Normalization in
    ``core.to_ir`` keeps the raw string away from this site too.
    """
    ir = parse("search in (T) 'x'").to_ir(attach_schema={"T": "(a:long)"})
    assert ir.main_pipeline.result_schema.columns == {
        "$table": "string", "a": "long",
    }


# Make-series range fields ----------------------------------------------

def test_make_series_range_fields_populated(builder, attacher):
    """``range_from``, ``range_to``, ``step`` are populated from RangeClause."""
    ir = builder.build(
        "DeviceProcessEvents | make-series count() default=0 on TimeGenerated "
        "from datetime(2024-01-01) to datetime(2024-01-08) step 1h by DeviceName"
    )
    op = ir.main_pipeline.operators[0]
    assert op.range_from is not None
    assert op.range_to is not None
    assert op.step is not None


def test_make_series_step_only_populates_step(builder, attacher):
    """When only ``step`` is specified, from/to remain None but step is populated."""
    ir = builder.build(
        "DeviceProcessEvents | make-series count() default=0 on TimeGenerated step 1h by DeviceName"
    )
    op = ir.main_pipeline.operators[0]
    assert op.range_from is None
    assert op.range_to is None
    assert op.step is not None


# --- traversal completeness: every Expr child, every nested Pipeline --------
#
# The walker is derived from model_fields, so no subtree is skipped. A
# ColumnRef the walk misses keeps table=None, and the same column then
# resolves two ways in one query.


def _refs(ir):
    """{name: {table, ...}} for every ColumnRef in the IR."""
    from kustology.ir import ColumnRef, find_all

    out: dict[str, set] = {}
    for c in find_all(ir, ColumnRef):
        out.setdefault(c.name, set()).add(c.table)
    return out


def test_columns_resolve_inside_toscalar(schema):
    """A pipeline nested in an expression resolves against its own source.

    ``_fill`` recurses through every model field, so ToScalarExpr,
    MaterializeExpr, and SubqueryExpr subtrees are entered.
    """
    ir = parse(
        "DeviceProcessEvents "
        "| where ProcessId > toscalar(DeviceProcessEvents | summarize max(ProcessId)) "
        "| project AccountName"
    ).to_ir(attach_schema=schema)
    # The same column inside and outside the toscalar must agree.
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_columns_resolve_inside_case_arms(schema):
    """``CaseExpr.branches`` is tuple-nested and ``default`` is its own field."""
    ir = parse(
        "DeviceProcessEvents "
        "| extend Risk = iif(ProcessId > 100, AccountName, DeviceName)"
    ).to_ir(attach_schema=schema)
    refs = _refs(ir)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}
    assert refs["DeviceName"] == {"DeviceProcessEvents"}


def test_columns_resolve_under_operators_without_a_scope_rule(schema):
    """``sort`` and ``top`` carry expressions but reshape nothing.

    ``_walk_operator_provenance`` fills every operator's expressions whatever
    its type; only the four source-bringing families get a scope branch of
    their own.
    """
    ir = parse(
        "DeviceProcessEvents "
        "| sort by ProcessId desc "
        "| top 5 by TimeGenerated "
        "| project AccountName"
    ).to_ir(attach_schema=schema)
    refs = _refs(ir)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["TimeGenerated"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}


def test_columns_resolve_through_a_nested_pipeline_source(schema):
    """``Pipeline.source`` may itself be a ``Pipeline``.

    ``let M = materialize(T | where X)`` is the shape that produces one;
    Microsoft's parser does not accept ``materialize(...)`` at the head of a
    bare statement. ``_source_entry`` walks the inner pipeline so the outer
    one starts from its scope.
    """
    from kustology.ir.query import Pipeline

    ir = parse(
        "let M = materialize(DeviceProcessEvents | where ProcessId > 1); "
        "M | count"
    ).to_ir(attach_schema=schema)
    inner = ir.let_bindings[0].rhs_pipeline
    assert isinstance(inner.source, Pipeline), "expected a nested pipeline source"

    # The nested source's table reached the outer pipeline's scope, and the
    # ColumnRef inside it was visited.
    assert set(inner.result_schema.columns) >= {"ProcessId", "AccountName"}
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


# --- let threading ---------------------------------------------------------


def test_let_pipeline_is_enriched(schema):
    """``enrich`` walks every tabular binding, so a binding's
    ``result_schema`` and the ColumnRefs inside it are both filled."""
    ir = parse(
        "let Base = DeviceProcessEvents | where ProcessId > 1; Base | count"
    ).to_ir(attach_schema=schema)
    binding = ir.let_bindings[0]
    assert binding.rhs_pipeline.result_schema is not None
    assert "AccountName" in binding.rhs_pipeline.result_schema.columns
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_main_pipeline_resolves_columns_through_a_let_name(schema):
    """The whole point: `Base | project AccountName` knows what Base holds."""
    ir = parse(
        "let Base = DeviceProcessEvents | where ProcessId > 1; "
        "Base | project AccountName"
    ).to_ir(attach_schema=schema)
    refs = _refs(ir)
    assert refs["AccountName"] == {"Base"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_let_threading_follows_a_chain(schema):
    ir = parse(
        "let A = DeviceProcessEvents | project AccountName, ProcessId; "
        "let B = A | where ProcessId > 2; "
        "B | project AccountName"
    ).to_ir(attach_schema=schema)
    a, b = ir.let_bindings
    assert set(a.rhs_pipeline.result_schema.columns) == {"AccountName", "ProcessId"}
    assert set(b.rhs_pipeline.result_schema.columns) == {"AccountName", "ProcessId"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_let_threading_is_gated_on_microsoft_closing_the_binding():
    """A binding threads only once something says what it emits.

    ``_let_schemas`` is filled from each binding pipeline's own
    ``result_schema``, which is Microsoft's answer or nothing. On the dict
    path the binding closes and the alias carries its columns. On a raw
    unbound IR the binding is open, the alias registers nothing, and a
    column read through it stays unresolved.
    """
    schemas = {"T": {"a": "long", "s": "string"}}
    query = "let Base = T | where a > 1; Base | project s"

    threaded = parse(query).to_ir(attach_schema=schemas)
    assert threaded.let_bindings[0].rhs_pipeline.result_schema.columns == schemas["T"]
    assert _refs(threaded)["s"] == {"Base"}

    ungated = IRBuilder().build(query)
    SchemaAttacher(schemas).enrich(ungated)
    assert ungated.let_bindings[0].rhs_pipeline.result_schema is None
    assert _refs(ungated)["s"] == {None}
    # The binding *body* still reads a real table, so its own columns place.
    assert _refs(ungated)["a"] == {"T"}


def test_let_threading_does_not_resolve_a_forward_reference(schema):
    """A binding naming one declared later is not a ``LetRef``, so it stays
    an opaque table with nothing to thread.

    Its ``result_schema`` is ``None``: nothing determined what it emits. An
    empty ``TabularSchema`` would claim it emits no columns, which is false.
    """
    ir = parse(
        "let Early = Later | take 1; "
        "let Later = DeviceProcessEvents | take 1; "
        "Early | project AccountName"
    ).to_ir(attach_schema=schema)
    assert ir.let_bindings[0].rhs_pipeline.result_schema is None


def test_scalar_binding_is_untouched_by_threading(builder, attacher):
    ir = builder.build("let lookback = 1h; DeviceProcessEvents | count")
    attacher.enrich(ir)
    assert ir.let_bindings[0].rhs_pipeline is None
    assert ir.let_bindings[0].rhs_expr is not None


def test_let_names_do_not_leak_between_enrich_calls(schema):
    """The let scope is per-call state: a reused attacher must not carry one
    query's binding names into the next.

    The assertion reads ``_let_schemas`` directly, because a downstream
    column cannot see the leak. A second query's ``Base`` is a plain
    ``TableRef``, and ``_source_entry`` consults ``_let_schemas`` only for a
    ``LetRef``, which the builder emits only for a name that query's own
    ``let`` bound. Reading the registry makes the test falsifiable: drop the
    reset at the top of ``enrich`` and the second call's registry holds both
    names.

    Binding and enriching by hand is the only way to reuse one attacher
    across two queries, and a binding registers a name only once Microsoft
    closes it, so an unbound pair would leave the registry empty.
    """
    attacher = SchemaAttacher(schema)

    first = parse(
        "let Base = DeviceProcessEvents | project AccountName; Base | count",
        schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(first)
    assert set(attacher._let_schemas) == {"Base"}, "premise: the name registered"

    second = parse(
        "let Other = DeviceProcessEvents | project DeviceName; "
        "Other | project DeviceName",
        schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(second)
    assert set(attacher._let_schemas) == {"Other"}

    # And a query with no bindings at all leaves nothing behind either.
    third = parse(
        "DeviceProcessEvents | project AccountName", schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(third)
    assert attacher._let_schemas == {}


# --- function and pattern bodies --------------------------------------------


def test_builder_schemas_snapshot_reaches_body_pipelines(schema):
    """Precondition, not behavior: ``find_all(ir, Pipeline)`` is a generic
    walk, so the ``_builder_schemas`` snapshot ``enrich`` takes at entry
    already covers a let-function's ``body_pipeline``. Pinning it directly
    makes a regression here read as this assertion failing instead of as a
    resolution gap two layers away."""
    ir = parse(
        "let f = (n:long) { DeviceProcessEvents | where ProcessId > n }; "
        "DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=False)
    body_pipeline = ir.let_bindings[0].rhs_function.body_pipeline
    snapshot = {id(p): p.result_schema for p in find_all(ir, Pipeline)}
    assert id(body_pipeline) in snapshot


def test_a_function_bodys_columns_acquire_table_from_a_real_table_it_reads(schema):
    """The body is walked as its own scope, so a column it reads off a real
    table gets that table's provenance. The scalar parameter ``n`` names no
    table's column and stays unresolved."""
    ir = parse(
        "let f = (n:long) { "
        "DeviceProcessEvents | where ProcessId > n | project AccountName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    refs = _refs(fn)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}
    assert refs["n"] == {None}


def test_a_tabular_parameters_columns_answer_no_table_rather_than_the_callers(schema):
    """A tabular parameter's ``TableRef`` masks to an empty scope. Nothing
    says what columns it carries, so a column read off it stays
    ``table=None`` instead of resolving through the parameter's bare name.

    This test's resolution path is an ordinary bare ``ColumnRef``, filled by
    ``_resolve_column_table`` and ``_column_origins``. Those answer only
    from a scope entry's known columns, so an unknown column of a masked
    entry resolves to ``None`` the same way an unknown column of any other
    schema-less table does. The other candidate answer, ``table="X"`` (the
    parameter's own name), never arises here. It can arise on a second path:
    ``_resolve_side``'s single-entry fallback, reached only from inside a
    join's ``on`` clause, reads ``ScopeEntry.table`` directly and needs its
    own guard, ``_entry_table``, covered by
    ``test_a_masked_tabular_parameters_own_name_does_not_surface_inside_a_joins_on_clause``.

    The parameter is named after a real schema table so the mask is
    observable. Without it, ``_table_schema`` would answer that table's
    columns for this name and the reference would resolve to
    ``DeviceProcessEvents``, leaking the caller's table into a value the
    body never reads.
    """
    ir = parse(
        "let f = (DeviceProcessEvents:(*)) { "
        "DeviceProcessEvents | where AccountName == 'x' | project AccountName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    assert _refs(fn)["AccountName"] == {None}


def test_a_masked_tabular_parameters_own_name_does_not_surface_inside_a_joins_on_clause(
    schema,
):
    """Guards a second resolution path to the same masked name.

    ``$left.AccountName`` has no known column to resolve by name (the left
    side's one entry is masked to ``columns={}``), so it falls through to
    ``_resolve_side``'s single-entry fallback, which is built for an unknown
    table whose one entry still names the side. That fallback reads
    ``entries[0].table``, and ``_entry_table`` guards it: a masked name
    never becomes a ``ScopeEntry.table`` label, so the fallback answers
    ``None``.

    The right-hand table is a plain, unmasked reference and is unaffected.
    """
    ir = parse(
        "let f = (DeviceProcessEvents:(*)) { "
        "DeviceProcessEvents | join (DeviceFileEvents) "
        "on $left.AccountName == $right.FileName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    refs = {c.name: c for c in find_all(fn, ColumnRef)}
    assert refs["AccountName"].join_side == "left"
    assert refs["AccountName"].table is None
    assert refs["FileName"].join_side == "right"
    assert refs["FileName"].table == "DeviceFileEvents"


def test_body_lets_chain_inside_a_function_body(schema):
    """A tabular body ``let`` threads into the tail exactly as a top-level
    one threads into the main pipeline."""
    ir = parse(
        "let f = (n:long) { "
        "let Filtered = DeviceProcessEvents | where ProcessId > n; "
        "Filtered | project AccountName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    (body_let,) = fn.body_lets
    assert body_let.rhs_pipeline.result_schema is not None
    assert "AccountName" in body_let.rhs_pipeline.result_schema.columns
    assert _refs(fn)["AccountName"] == {"Filtered"}


def test_a_scalar_parameter_colliding_with_a_schema_table_does_not_leak_it(schema):
    """A scalar parameter named after a real schema table masks that table
    for the length of the body, so a reference to the parameter does not
    resolve against the table it shares a name with. The mask lifts when the
    body ends: the main pipeline's reference to the real table resolves
    normally."""
    ir = parse(
        "let f = (DeviceProcessEvents:long) { "
        "DeviceFileEvents | where TimeGenerated > DeviceProcessEvents "
        "}; DeviceProcessEvents | project AccountName",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    body_refs = _refs(fn)
    # The real table the body actually reads resolves normally.
    assert body_refs["TimeGenerated"] == {"DeviceFileEvents"}
    # The parameter reference does not borrow the colliding table's identity.
    assert body_refs["DeviceProcessEvents"] == {None}
    # Restored: the main pipeline's own real-table reference is unaffected.
    assert _refs(ir.main_pipeline)["AccountName"] == {"DeviceProcessEvents"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_a_nested_function_bodys_own_let_function_is_recursed_into(schema):
    """A ``let`` written inside a function body can itself be a
    ``FunctionDeclaration``. ``_walk_function_body``'s ``body_lets`` loop
    gets the same three-way dispatch ``enrich``'s top-level loop does, so a
    nested function's body is walked and masked too.

    Both maskings apply independently during the nested walk. ``inner``'s
    tabular parameter (``DeviceFileEvents``) is masked while ``inner``'s
    body runs, and ``outer``'s (``DeviceProcessEvents``) stays masked
    throughout, unioned in on the way into ``inner``. Both names collide
    with real schema tables. On the way back out, ``outer``'s tail still
    cannot see through its own mask, and the main pipeline's real-table
    reference resolves normally.
    """
    walked_ids: set[int] = set()
    attacher = SchemaAttacher(schema)
    original_walk_pipeline = attacher._walk_pipeline

    def recording_walk_pipeline(pipeline, inherited=None):
        walked_ids.add(id(pipeline))
        return original_walk_pipeline(pipeline, inherited)

    attacher._walk_pipeline = recording_walk_pipeline

    ir = parse(
        "let outer = (DeviceProcessEvents:(*)) { "
        "let inner = (DeviceFileEvents:(*)) { "
        "DeviceFileEvents | where FileName == 'x' | project FileName "
        "}; "
        "DeviceProcessEvents | where AccountName == 'x' | project AccountName "
        "}; "
        "DeviceProcessEvents | project AccountName",
        schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(ir)

    outer = ir.let_bindings[0].rhs_function
    (inner_binding,) = outer.body_lets
    inner = inner_binding.rhs_function

    # The nested body really was walked, not silently skipped.
    assert id(inner.body_pipeline) in walked_ids

    # Inner's own masked parameter does not leak the real DeviceFileEvents.
    assert _refs(inner)["FileName"] == {None}
    # Outer's masked parameter still doesn't leak, for outer's own tail,
    # even after the nested call unwound.
    assert _refs(outer)["AccountName"] == {None}
    # And the real thing, outside both scopes, resolves normally.
    assert _refs(ir.main_pipeline)["AccountName"] == {"DeviceProcessEvents"}


def test_masking_is_restored_even_if_the_body_walk_raises(schema):
    """``_walk_function_body`` restores ``_masked_tables`` and
    ``_let_schemas`` in ``finally``, so an exception partway through one
    body cannot leave the attacher masking a real table, or holding a stale
    let, for every query it enriches after."""
    attacher = SchemaAttacher(schema)
    ir = parse(
        "let f = (DeviceProcessEvents:long) { "
        "DeviceFileEvents | where TimeGenerated > DeviceProcessEvents "
        "}; DeviceProcessEvents | project AccountName",
        schema=schema,
    ).to_ir(attach_schema=False)
    fn = ir.let_bindings[0].rhs_function

    original_walk_pipeline = attacher._walk_pipeline

    def exploding_walk_pipeline(pipeline, inherited=None):
        if pipeline is fn.body_pipeline:
            raise RuntimeError("boom")
        return original_walk_pipeline(pipeline, inherited)

    attacher._walk_pipeline = exploding_walk_pipeline
    with pytest.raises(RuntimeError, match="boom"):
        attacher.enrich(ir)

    assert attacher._masked_tables == set()
    assert "DeviceProcessEvents" not in attacher._let_schemas


def test_a_masked_search_tables_own_name_does_not_surface_through_a_later_join(
    schema,
):
    """``_entry_table`` also guards ``search``/``find``'s table seeding
    (``_walk_operator_provenance``'s ``SearchOp``/``FindOp`` branch), which
    builds its ``ScopeEntry`` the same way ``_source_entry`` does.

    A ``join`` right after a masked ``search`` does not reach the gap:
    Microsoft always closes ``SearchOp.result_schema`` with at least a
    ``$table`` marker column, even over an open ``(*)`` parameter, so
    ``_overlay_result_schema`` adds a second anonymous entry beside the
    masked one and defeats ``_resolve_side``'s single-entry fallback.

    The masked ``search`` on the right side of a join does reach it.
    ``_flatten_side`` collapses the whole right-hand scope into one entry
    and reads every contributing entry's ``.table`` to label it, keeping a
    table when all of them named the same one. Without the guard, a masked
    search-seeded entry hands its colliding name to the merged right side,
    and ``_resolve_side``'s single-entry fallback (always reached for a
    join's right side, one entry by construction) reads it back for an
    otherwise unresolvable ``$right`` column.
    """
    ir = parse(
        "let f = (DeviceProcessEvents:(*)) { "
        "DeviceFileEvents | join (search in (DeviceProcessEvents) 'x') "
        "on $left.FileName == $right.Unknown "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    refs = {c.name: c for c in find_all(fn, ColumnRef)}
    assert refs["FileName"].join_side == "left"
    assert refs["FileName"].table == "DeviceFileEvents"
    assert refs["Unknown"].join_side == "right"
    assert refs["Unknown"].table is None


def test_a_search_over_a_non_tabular_let_alias_does_not_surface_through_a_later_join(
    schema,
):
    """The sibling of the masking test above, for the other half of the same
    ``ScopeEntry(table=..., columns={})`` fallback. ``search``/``find``'s
    ``LetRef`` seeding answers ``table=None`` when ``alias`` never reached
    ``_let_schemas`` (a scalar ``let``, a function binding, or a tabular
    ``let`` the binder could not close), matching ``_source_entry``'s
    ``LetRef`` branch for a pipeline's own source position.

    The columns path cannot show it, since an empty-columns entry
    contributes nothing to ``_column_origins``. ``_flatten_side`` reads
    every contributing entry's ``.table`` whatever its columns, so the same
    right-side-of-a-join shape surfaces it: with ``A`` a scalar, never
    registered in ``_let_schemas``, a naive seeding would label
    ``$right.Unknown`` with ``"A"``, a table the query never read.
    """
    ir = parse(
        "let A = 5; DeviceFileEvents | join (search in (A) 'x') "
        "on $left.FileName == $right.Unknown",
        schema=schema,
    ).to_ir(attach_schema=schema)
    refs = {c.name: c for c in find_all(ir, ColumnRef)}
    assert refs["FileName"].join_side == "left"
    assert refs["FileName"].table == "DeviceFileEvents"
    assert refs["Unknown"].join_side == "right"
    assert refs["Unknown"].table is None


def test_a_pattern_arms_body_is_walked_through_the_same_helper(schema):
    """``declare pattern`` reuses ``_walk_function_body`` with no parameters
    to mask, so the arm's own columns still get their table."""
    ir = parse(
        'declare pattern P = (a:string) { '
        '("x") = { DeviceProcessEvents | where ProcessId > 1 '
        '| project AccountName }; '
        '}; DeviceProcessEvents | count',
        schema=schema,
    ).to_ir(attach_schema=schema)
    (stmt,) = ir.statements
    (match,) = stmt.matches
    refs = _refs(match)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}


# Microsoft's per-operator schema (K-ARCH-1, Task 5.2) --------------------

def test_provenance_survives_an_authoritative_result_schema():
    """Taking names and types from the binder must not cost ``ColumnRef.table``.

    Microsoft's ``ResultType`` says which columns exist and how they are
    typed, not which table each came from; only this walk can say that.
    Replacing the scope with one anonymous entry carrying Microsoft's
    columns would drop provenance for every operator the binder can type.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, ProjectOp, SortOp, find_all

    schemas = {"DeviceProcessEvents": {"FileName": "string", "ProcessId": "long"}}
    ir = parse(
        "DeviceProcessEvents "
        "| where ProcessId > 4 "
        "| project FileName, ProcessId "
        "| sort by ProcessId desc",
        schema=schemas,
    ).to_ir()

    # Premise: the binder typed every operator, so the overlay ran.
    assert all(op.result_schema is not None for op in ir.main_pipeline.operators)

    project = next(op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp))
    sort = next(op for op in ir.main_pipeline.operators if isinstance(op, SortOp))
    assert {c.table for c in find_all(project, ColumnRef)} == {"DeviceProcessEvents"}
    assert {c.table for c in find_all(sort, ColumnRef)} == {"DeviceProcessEvents"}


def test_join_provenance_survives_an_authoritative_result_schema():
    """A join builds its per-side scope entries even where Microsoft answered.

    ``$left`` and ``$right`` resolve against a scope holding both sides, and
    a right-hand column has a table only because the join walk appended an
    entry for it. The overlay cannot reconstruct either, so ``join``,
    ``lookup``, ``union``, and ``search`` keep a structural branch that
    brings the sources into scope and derives no columns of its own.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, ProjectOp, find_all

    schemas = {
        "DeviceProcessEvents": {"DeviceId": "string", "FileName": "string"},
        "DeviceFileEvents": {"DeviceId": "string", "TimeGenerated": "datetime"},
    }
    ir = parse(
        "DeviceProcessEvents "
        "| join DeviceFileEvents on DeviceId "
        "| project FileName, TimeGenerated",
        schema=schemas,
    ).to_ir()

    assert all(op.result_schema is not None for op in ir.main_pipeline.operators)
    project = next(op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp))
    tables = {c.name: c.table for c in find_all(project, ColumnRef)}
    assert tables == {
        "FileName": "DeviceProcessEvents",
        "TimeGenerated": "DeviceFileEvents",
    }


def test_microsoft_declining_is_not_an_invitation_to_answer(builder, attacher):
    """``IRBuilder().build`` binds against globals with no tables at all.

    Every symbol it produces is open, so the pipeline says ``None`` and
    nothing fills in for it. The caller's dict still buys the other
    contract: every column reference is placed, from a dict the binder
    never saw.
    """
    ir = builder.build(
        "DeviceProcessEvents | project FileName, AccountName "
        "| where FileName == 'cmd.exe'"
    )
    assert all(op.result_schema is None for op in ir.main_pipeline.operators)
    attacher.enrich(ir)
    assert ir.main_pipeline.result_schema is None
    assert {c.table for c in find_all(ir, ColumnRef)} == {"DeviceProcessEvents"}


def test_count_closes_a_symbol_even_from_an_unknown_table(builder):
    """``IsOpen`` is per node, not per query, and ``count`` is the proof.

    ``T | count`` returns exactly ``Count:long`` whatever ``T`` is, so
    Microsoft closes the symbol there even though the source is unknown.
    The IR therefore reads the answer off each operator.
    """
    ir = builder.build("Whatever | where x > 1 | count")
    where_op, count_op = ir.main_pipeline.operators
    assert where_op.result_schema is None
    assert count_op.result_schema.columns == {"Count": "long"}


def test_microsoft_decides_the_column_order_not_the_scope_grouping():
    """The engine orders a join's output columns.

    ``ScopeEntry`` groups columns by originating table so provenance
    survives, which orders a merged scope left-side-first. Microsoft emits
    ``DeviceId, FileName, DeviceId1, TimeGenerated``, and the pipeline
    schema has to be that list in that order.
    """
    from kustology import parse

    schemas = {
        "DeviceProcessEvents": {"DeviceId": "string", "FileName": "string"},
        "DeviceFileEvents": {"DeviceId": "string", "TimeGenerated": "datetime"},
    }
    ir = parse(
        "DeviceProcessEvents | join DeviceFileEvents on DeviceId", schema=schemas,
    ).to_ir()
    assert list(ir.main_pipeline.result_schema.columns.items()) == [
        ("DeviceId", "string"),
        ("FileName", "string"),
        ("DeviceId1", "string"),
        ("TimeGenerated", "datetime"),
    ]


# --- the dict entry point -------------------------------------------------
#
# ``parse(q).to_ir(attach_schema=dict)`` is the public path for a caller who
# has a schema and no cluster to bind against. It re-binds through
# ``build_global_state`` + ``Analyze``, so Microsoft answers for every symbol
# it can close and this walk supplies provenance over the top.

DICT_SCHEMA = {
    "L": {"k": "string", "a": "long", "shared": "string"},
    "R": {"k": "string", "b": "real", "shared": "string"},
    "T": {
        "k": "string", "a": "long", "t": "datetime",
        "d": "dynamic", "s": "string", "g": "guid",
    },
    "U": {"k": "string", "a": "string", "z": "long"},
}


def _dict_path(query: str, schemas: dict | None = None):
    """``parse(query).to_ir(attach_schema=…)`` with the collision-heavy dict.

    ``L`` and ``R`` share ``k`` and ``shared``, which makes join collisions
    observable, and ``U`` types ``a`` differently from ``T`` so a union
    conflict has to split.
    """
    return parse(query).to_ir(
        attach_schema=DICT_SCHEMA if schemas is None else schemas,
    )


def _tables(ir) -> dict[str, set]:
    """``{column name: {table, ...}}`` over every ``ColumnRef`` in the IR."""
    from kustology.ir import ColumnRef, find_all

    out: dict[str, set] = {}
    for c in find_all(ir, ColumnRef):
        out.setdefault(c.name, set()).add(c.table)
    return out


# K28 (provenance): ScopeEntry.origins ---------------------------------------


def test_project_carries_provenance_into_a_later_operator():
    """``project`` keeps the source table in scope, so a column reference
    after it resolves the same way one before it does."""
    ir = _dict_path("T | project a, k | where a > 1")
    assert _tables(ir)["a"] == {"T"}


def test_project_away_and_project_keep_carry_provenance():
    away = _dict_path("T | project-away s | where a > 1")
    assert _tables(away)["a"] == {"T"}
    keep = _dict_path("T | project-keep a | where a > 1")
    assert _tables(keep)["a"] == {"T"}


def test_distinct_carries_provenance():
    ir = _dict_path("T | distinct k | where k == 'x'")
    assert _tables(ir)["k"] == {"T"}


def test_project_rename_carries_provenance_under_the_new_name():
    """The renamed column is still the source table's column.

    ``kk`` is a name no scope entry holds, the shape that files anonymously
    for a join collision or a union split variant. Here the query names the
    input column outright, so
    :func:`~kustology.ir.binder._renamed_columns` threads ``kk -> k`` into
    the overlay and ``T`` survives the rename.
    """
    ir = _dict_path("T | project-rename kk = k | where kk == 'x'")
    tables = _tables(ir)
    assert tables["kk"] == {"T"}
    assert tables["k"] == {"T"}


def test_project_rename_provenance_needs_a_real_column_on_the_right():
    """The thread is followed only where there is an input name to follow.

    Every ``project-rename`` term the parser accepts has a ``ColumnRef`` on
    the right, so this guards a hand-built or unmodeled IR. With no column
    to carry from, the target files anonymously.
    """
    from kustology.ir.binder import _renamed_columns
    from kustology.ir.expr import LiteralExpr
    from kustology.ir.query import Assignment, ProjectRenameOp
    from kustology.ir.spans import Span

    span = Span(text_start=0, width=1)
    op = ProjectRenameOp(
        span=span,
        columns=[
            Assignment(
                name="kk",
                expr=LiteralExpr(value=1, literal_kind="long", span=span),
                span=span,
            ),
        ],
    )
    assert _renamed_columns(op) == {}


def test_a_computed_column_has_no_table_and_does_not_borrow_one():
    """``origins`` must record "invented here", not inherit the neighbors'."""
    ir = _dict_path("T | project n = a + 1, k | where n > 1")
    tables = _tables(ir)
    assert tables["k"] == {"T"}
    assert tables["n"] == {None}


def test_summarize_keys_keep_provenance_and_aggregates_do_not():
    ir = _dict_path("T | summarize c = count() by k | where c > 1 and k == 'x'")
    tables = _tables(ir)
    assert tables["k"] == {"T"}
    assert tables["c"] == {None}


def test_an_ambiguous_unqualified_column_resolves_to_no_table():
    """``T | union U`` puts ``k`` in two scope entries with different tables.

    KQL treats the unqualified name as ambiguous, so the honest provenance
    is unknown.
    """
    ir = _dict_path("T | union U | where k == 'x'")
    assert _tables(ir)["k"] == {None}


# Joins: what the overlay cannot reconstruct ----------------------------------


def test_a_right_side_only_column_reports_the_right_table():
    """The entry the join walk appends is what gives a right-hand column its
    table.

    ``kind=rightsemi`` emits the right side's columns only, and ``b`` is
    ``R``'s alone, so it places there even though the operator producing it
    sits on the left of the pipeline.
    """
    from kustology.ir import FilterOp

    ir = _dict_path("L | join kind=rightsemi (R) on k | where b > 1")
    where = next(op for op in ir.main_pipeline.operators if isinstance(op, FilterOp))
    assert {c.table for c in find_all(where, ColumnRef)} == {"R"}


def test_a_post_join_collision_is_ambiguous_and_says_so():
    """An unqualified name that both join sides carry has no provenance.

    Microsoft emits ``shared`` and ``shared1`` for ``L | join (R) on k``,
    and both sides are in scope with a ``shared`` of their own, so the walk
    cannot say which is which and reports ``None``. A qualified reference is
    unaffected: ``$left`` and ``$right`` name a side outright, which is what
    the per-side entries are kept for.
    """
    from kustology.ir import JoinOp

    ir = _dict_path("L | join (R) on k | project shared, shared1")
    assert list(ir.main_pipeline.result_schema.columns) == ["shared", "shared1"]
    tables = _tables(ir)
    assert tables["shared"] == {None}
    assert tables["shared1"] == {None}

    qualified = _dict_path("L | join (R) on $left.shared == $right.shared")
    join = next(
        op for op in qualified.main_pipeline.operators if isinstance(op, JoinOp)
    )
    left, right = [c for e in join.on for c in find_all(e, ColumnRef)]
    assert (left.table, right.table) == ("L", "R")


# K10 / K11: resolving inside a join's on-clause ------------------------------


def _on_refs(ir, index: int = -1) -> list:
    """Every ``ColumnRef`` in the ``on`` clause of one join, in order."""
    from kustology.ir import ColumnRef, JoinOp, find_all

    joins = [op for op in ir.main_pipeline.operators if isinstance(op, JoinOp)]
    return [c for e in joins[index].on for c in find_all(e, ColumnRef)]


def test_dollar_left_resolves_by_name_across_the_whole_left_side():
    """``$left`` is the accumulated left row set, not the last entry in it.

    After ``L | join (R) …``, a second join's ``$left.a`` resolves to ``L``.
    Reading only the entry the previous join appended would answer ``R``,
    which has no ``a`` at all.
    """
    ir = _dict_path("L | join (R) on k | join (T) on $left.a == $right.a")
    left, right = _on_refs(ir)
    assert left.name == "a" and left.join_side == "left"
    assert left.table == "L"
    assert right.table == "T"


def test_dollar_right_resolves_against_the_appended_right_entry():
    ir = _dict_path("L | join (R) on $left.k == $right.b")
    left, right = _on_refs(ir)
    assert (left.table, right.table) == ("L", "R")


def test_dollar_right_resolves_through_the_right_pipelines_own_operators():
    """The right side is a pipeline, so its scope may be anonymous with the
    provenance carried in ``origins``. ``$right.b`` is still ``R``'s.

    A union on the right is the same question with several entries to
    reconcile. A join has one right side, so ``_flatten_side`` merges them
    and ``b``, which only one arm carries, keeps that arm's table.
    """
    ir = _dict_path("L | join (R | project b) on $left.k == $right.b")
    _left, right = _on_refs(ir)
    assert right.table == "R"

    unioned = _dict_path(
        "L | join (union (L | where a > 1), (R | where b > 1)) "
        "on $left.k == $right.b"
    )
    _left, right = _on_refs(unioned)
    assert right.table == "R"


def test_an_unresolvable_dollar_side_answers_none_and_keeps_its_side():
    """An unresolvable side answers ``table=None``. ``join_side``, which the
    builder sets even on an unbound parse, carries the side."""
    ir = _dict_path("L | join (datatable(z:long)[1]) on $left.k == $right.z")
    _left, right = _on_refs(ir)
    assert right.table is None
    assert right.join_side == "right"


def test_an_unenriched_dollar_ref_has_no_sentinel_either():
    (ref,) = [c for c in find_all(parse("L | join (R) on $left.k == $right.b").to_ir(), ColumnRef) if c.name == "k"]
    assert ref.table is None
    assert ref.join_side == "left"


def test_a_bare_on_key_resolves_against_the_left_side():
    """``on k`` is shorthand for ``$left.k == $right.k``, and both sides have
    a ``k``. The general ambiguity rule would answer ``None`` because the
    right side is in scope too; the engine keeps the column from the left."""
    ir = _dict_path("L | join (R) on k")
    (key,) = _on_refs(ir)
    assert key.table == "L"


def test_lookups_bare_on_key_resolves_to_the_left_side_too():
    """``lookup`` drops the right side's key, so the left is the only side
    whose column survives."""
    from kustology.ir import ColumnRef, LookupOp, find_all

    ir = _dict_path("L | lookup (R) on k")
    lookup = next(
        op for op in ir.main_pipeline.operators if isinstance(op, LookupOp)
    )
    (key,) = [c for e in lookup.on for c in find_all(e, ColumnRef)]
    assert key.table == "L"


# Re-enriching one IR: the schema is fixed at bind time -----------------------


def test_enriching_twice_does_not_change_result_schema():
    """Re-enriching an already-bound IR is a no-op on ``result_schema``.

    ``enrich`` writes back a copy of what Microsoft stamped and never
    recomputes the shape, so a second call leaves ``result_schema``
    unchanged whatever dict it carries. Only re-binding changes a schema.
    The operator-less branch reads the pipeline's own field back, so it is
    the one pinned here.
    """
    ir = parse("T").to_ir(attach_schema={"T": {"a": "long", "s": "string"}})
    assert not ir.main_pipeline.operators, "premise: the operator-less branch"
    assert ir.main_pipeline.result_schema.columns == {"a": "long", "s": "string"}

    SchemaAttacher({"T": {"a": "real", "s": "guid"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long", "s": "string"}

    rebound = parse("T").to_ir(attach_schema={"T": {"a": "real", "s": "guid"}})
    assert rebound.main_pipeline.result_schema.columns == {"a": "real", "s": "guid"}


def test_enriching_twice_does_not_wipe_an_operator_less_let_binding():
    """The snapshot of the builder's value is unconditional.

    ``enrich`` reads each binding's ``result_schema`` to register what the
    alias holds. With no operators there is nothing else to read the shape
    off, so skipping the snapshot on a second call would send the binding,
    and everything resolving through it, to ``None``.
    """
    ir = parse("let M = materialize(T); M | project a").to_ir(
        attach_schema={"T": {"a": "long"}},
    )
    binding = ir.let_bindings[0]
    assert not binding.rhs_pipeline.operators, "premise: the operator-less branch"
    assert binding.rhs_pipeline.result_schema.columns == {"a": "long"}

    SchemaAttacher({"T": {"a": "real"}}).enrich(ir)
    assert binding.rhs_pipeline.result_schema.columns == {"a": "long"}
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


def test_enriching_twice_with_an_operator_present_is_also_a_no_op():
    """The other branch, for the same reason: the last operator's stamp is
    the answer and a second ``enrich`` copies it again."""
    ir = parse("T | where a > 1").to_ir(attach_schema={"T": {"a": "long"}})
    SchemaAttacher({"T": {"a": "real"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


# K12: union ------------------------------------------------------------------


def test_union_split_columns_are_names_no_arm_ever_had():
    """A type-conflict split produces names no arm's scope entry carries.

    ``T.a`` is a long and ``U.a`` a string, so the engine emits ``a_long``
    and ``a_string`` and no unsuffixed ``a`` at all. Those names exist only
    in Microsoft's answer, so the overlay files them anonymously and they
    report ``None``.
    """
    ir = _dict_path("T | union U | where a_string == 'x' and a_long > 1")
    assert list(ir.main_pipeline.result_schema.columns)[:3] == [
        "k", "a_long", "a_string",
    ]
    tables = _tables(ir)
    assert tables["a_long"] == {None}
    assert tables["a_string"] == {None}


# K13 / K14: aggregate output column names ------------------------------------


def test_the_auto_name_lands_on_the_assignment_not_only_the_schema():
    """``Assignment.name`` is what a consumer reads to label the column."""
    from kustology import parse
    from kustology.ir import SummarizeOp

    ir = parse("T | summarize make_set(s), percentile(a, 95)").to_ir()
    op = next(
        o for o in ir.main_pipeline.operators if isinstance(o, SummarizeOp)
    )
    assert [a.name for a in op.aggregations] == ["set_s", "percentile_a_95"]


# search: an implicit source, so the seeding is provenance's job ---------------


def test_search_columns_keep_their_table():
    ir = _dict_path("search in (T) 'x' | where a > 1")
    assert _tables(ir)["a"] == {"T"}


def test_a_search_predicate_resolves_against_the_tables_being_searched():
    """``search`` has an implicit source, so the scope *before* it is empty.

    The walk fills the predicate after seeding the searched entries, so
    ``search in (T) a > 1`` places ``a`` in ``T``, the same as the column
    one operator later in ``search in (T) 'x' | where a > 1``.
    """
    from kustology.ir import ColumnRef, SearchOp, find_all

    ir = _dict_path("search in (T) a > 1")
    search_op = next(
        op for op in ir.main_pipeline.operators if isinstance(op, SearchOp)
    )
    assert {c.table for c in find_all(search_op, ColumnRef)} == {"T"}


def test_search_provenance_survives_an_authoritative_result_schema():
    """``search`` keeps its structural branch on a bound parse, as ``join``
    does.

    Its source is implicit, so the pre-operator scope is empty and the
    overlay alone would file every column it emits as anonymous, leaving a
    following ``where a > 1`` with no table for a column that comes from
    ``T``.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, FilterOp, find_all

    schemas = {"T": {"k": "string", "a": "long"}}
    ir = parse("search in (T) 'x' | where a > 1", schema=schemas).to_ir()
    search_op = ir.main_pipeline.operators[0]
    assert search_op.result_schema is not None, "premise: Microsoft answered"
    where = next(
        op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)
    )
    assert {c.table for c in find_all(where, ColumnRef)} == {"T"}


# find: the fourth source-bringing operator, seeded like search --------------


def test_find_seeds_its_tables_like_search():
    """``find`` is the fourth source-bringing operator: the predicate of
    ``find in (T) where a > 1`` resolves against ``T``."""
    ir = parse("find in (T) where a > 1").to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"T"}


def test_find_project_columns_resolve_too():
    ir = parse("find in (T) where a > 1 project a").to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.project[0], ColumnRef)} == {"T"}


def test_a_find_or_search_over_a_let_alias_resolves_through_it():
    q = "let A = T | where a > 1; find in (A) where a > 5"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"A"}


def test_a_search_over_a_let_alias_resolves_through_it_too():
    """``search``'s branch resolves a ``LetRef`` table's schema through
    ``_let_schemas``, the same as ``_source_entry`` does at a pipeline's own
    source position."""
    q = "let A = T | where a > 1; search in (A) a > 5"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, SearchOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, SearchOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"A"}


# K28: "nothing is known" is None, not an empty schema ------------------------


def test_a_pipeline_the_walk_learned_nothing_about_has_no_result_schema():
    """``result_schema = {}`` claims "this emits no columns".

    A query over a table nobody described emits an unknown set of columns,
    a different statement, so it reports ``None``.
    """
    ir = _dict_path("Unknown | take 1", {"T": {"a": "long"}})
    assert ir.main_pipeline.result_schema is None


def test_an_unmodelled_sub_pipeline_does_not_inherit_the_enclosing_scope():
    """A branch the builder could not model gets no schema.

    ``UnknownSource`` with no operators marks a branch the builder could not
    model at all, which is not a branch that emits its input unchanged.
    Every other implicit-source sub-pipeline (``mv-apply``, ``partition``,
    ``fork``, ``facet``) runs against the enclosing rows and inherits.
    """
    from kustology.ir.binder import ScopeEntry
    from kustology.ir.query import Pipeline, UnknownSource
    from kustology.ir.spans import Span

    span = Span(text_start=0, width=1)
    branch = Pipeline(source=UnknownSource(raw_text="?", span=span), operators=[])
    attacher = SchemaAttacher(DICT_SCHEMA)
    inherited = [ScopeEntry(table="T", columns={"a": "long"})]

    assert attacher._walk_pipeline(branch, inherited) == []
    assert branch.result_schema is None


def test_enrich_does_not_clobber_a_type_the_binder_already_resolved():
    """The walk is not the only thing that knows a column's type.

    ``evaluate`` opens the symbol, so ``project`` gets no authoritative
    schema and the pipeline reports ``None``. The binder still typed the
    ``ColumnRef`` from the parse-time schema, and that type stays on the
    node. An ``enrich`` that knows nothing about ``T`` leaves it alone.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, KustoType, ProjectOp, find_all

    schemas = {"T": {"k": "string", "a": "long", "d": "dynamic"}}
    ir = parse(
        "T | evaluate bag_unpack(d) | project a", schema=schemas,
    ).to_ir(attach_schema=False)
    project = next(
        op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp)
    )
    assert project.result_schema is None, "premise: evaluate opened the symbol"
    (ref,) = find_all(project, ColumnRef)
    assert ref.result_type == KustoType.LONG, "premise: the binder typed it"

    # The schema is on the IR; this attacher knows nothing about ``T``.
    SchemaAttacher({}).enrich(ir)
    assert ref.result_type == KustoType.LONG
    assert ir.main_pipeline.result_schema is None


# K28: schema_attached means a schema was actually available ------------------


def test_schema_attached_stays_false_when_no_schema_was_available():
    """The flag claims "these types are real", so an attacher with no
    schemas, over an IR the binder could not type either, leaves it false."""
    ir = IRBuilder().build("Unknown | take 1")
    SchemaAttacher().enrich(ir)
    assert ir.schema_attached is False


def test_schema_attached_is_true_for_a_schema_dict():
    ir = IRBuilder().build("Unknown | take 1")
    SchemaAttacher({"Unknown": {"a": "long"}}).enrich(ir)
    assert ir.schema_attached is True


def test_schema_attached_is_true_when_the_binder_answered():
    """``count`` closes its symbol whatever the source is, so the IR carries
    a real schema even though the attacher was given none."""
    ir = IRBuilder().build("Unknown | count")
    assert ir.main_pipeline.operators[-1].result_schema is not None
    SchemaAttacher().enrich(ir)
    assert ir.schema_attached is True


# Arithmetic is not a predicate ------------------------------------------------


def _syntactic(query: str, schemas: dict | None = None):
    """Enrich an IR built from a syntactic-only parse.

    ``KustoCode.Parse`` produces a tree with no semantics, so every
    ``Expr.result_type`` starts ``UNRESOLVED`` and only ``_fill``'s type
    fallback can set one. On a bound path the binder has already answered
    and the fallback never runs, so this is its harness.
    """
    from kustology.bridge import KustoCode

    ir = IRBuilder().build_from_code(KustoCode.Parse(query))
    assert all(
        op.result_schema is None for op in ir.main_pipeline.operators
    ), "premise: a syntactic-only parse leaves the type fallback to answer"
    return SchemaAttacher(
        schemas if schemas is not None else DICT_SCHEMA,
    ).enrich(ir)


def test_arithmetic_is_not_typed_as_a_boolean():
    """The type fallback answers ``bool`` for a comparison only.

    ``a > 1`` is a predicate and types ``bool``. ``a + 1`` is arithmetic on
    the same ``BinOp`` node; the fallback does no numeric promotion, so it
    stays ``UNRESOLVED``.
    """
    from kustology.ir import ExtendOp, KustoType

    ir = _syntactic("T | extend n = a + 1, flag = a > 1")
    extend = next(
        op for op in ir.main_pipeline.operators if isinstance(op, ExtendOp)
    )
    typed = {a.name: a.expr.result_type for a in extend.assignments}
    assert typed["flag"] == KustoType.BOOL
    assert typed["n"] == KustoType.UNRESOLVED


# Pipelines the builder could not model ---------------------------------------


def test_an_unparseable_query_gets_no_result_schema():
    """``UnknownSource`` with no operators is reachable from a real string.

    The builder emits it for anything it cannot model as a source, and the
    pipeline then claims nothing about its own output.
    """
    ir = IRBuilder().build("not a query at all")
    from kustology.ir.query import UnknownSource

    assert isinstance(ir.main_pipeline.source, UnknownSource)
    assert not ir.main_pipeline.operators
    SchemaAttacher(DICT_SCHEMA).enrich(ir)
    assert ir.main_pipeline.result_schema is None


@pytest.mark.parametrize("schema,query,expect", [
    # Two effects, both of which cost provenance as ambiguity: a name two
    # entries disagree about answers `None`.
    #
    # (1) An unqualified `search` seeds every table the dict describes, the
    #     dict standing in for every table in the database. Cases 2 and 3.
    # (2) The seeded entries are appended to the scope the operator
    #     inherited. Replacing it wholesale would be a statement about the
    #     operator's output, which Microsoft already makes. This applies to
    #     a qualified `search in (U)` too, which is case 1.
    #
    # Case 1 is (2) alone: `T` (inherited) and `U` (searched) both have an
    # `a`. Case 3 is (1) and (2) together. Case 2 is where neither bites,
    # because only the inherited `T` has an `a`.
    ({"T": {"a": "long", "k": "string"}, "U": {"a": "long"}},
     "T | partition by k (search in (U) a > 1)", {"k": "T", "a": None}),
    ({"T": {"a": "long", "k": "string"}, "U": {"z": "long"}},
     "T | partition by k (search a > 1)", {"k": "T", "a": "T"}),
    ({"T": {"a": "long", "k": "string"}, "U": {"a": "long"}},
     "T | partition by k (search a > 1)", {"k": "T", "a": None}),
])
def test_search_inside_partition_resolves_scope(schema, query, expect):
    ir = parse(query).to_ir()
    SchemaAttacher(schema).enrich(ir)
    got = {c.name: c.table for c in find_all(ir, ColumnRef) if c.name in expect}
    assert got == expect


def test_the_unknown_column_sentinel_is_microsofts_word_and_only_microsofts():
    """``TabularSchema.columns`` maps a column to a type string, and the
    string for "no type known" is ``"unknown"``. ``KustoType.UNRESOLVED``
    spells the same idea ``"unresolved"``.

    The split exists because ``columns`` values are Microsoft's type names:
    ``ScalarTypes.Unknown.Name`` is literally ``"unknown"``. A consumer
    reading ``Expr.result_type`` tests against ``KustoType.UNRESOLVED`` and
    finds the other spelling one field away, in a plain ``dict[str, str]``
    a ``KustoType`` never validates.

    Only the builder produces that dict, copying the binder's stamp. Both
    halves are pinned: Microsoft's word arrives, and ``enrich`` writes no
    type strings at all, so it cannot introduce a second spelling.
    """
    import warnings

    from kustology import parse
    from kustology.ir.types import KustoType

    assert KustoType.UNRESOLVED.value == "unresolved"

    # Microsoft's schema parser types `n` `ScalarTypes.Unknown` and the
    # builder publishes its `Name`.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        bound = parse("T | project n | extend m = n", schema={"T": "(n:bogus)"}).to_ir()
    assert bound.main_pipeline.result_schema.columns == {"n": "unknown", "m": "unknown"}

    # A second pass over the same IR leaves it exactly as Microsoft left it.
    SchemaAttacher({}).enrich(bound)
    assert bound.main_pipeline.result_schema.columns == {"n": "unknown", "m": "unknown"}


# Hash silence: table and result_type are volatile, join_side is not --------


def test_enrichment_is_hash_silent_for_a_join_and_a_find_query():
    """Enrichment writes ``ColumnRef.table`` and ``result_type``, both
    stripped from the hash payload by ``transforms.py``'s
    ``_VOLATILE_FIELDS``. ``join_side`` comes from the builder, not this
    walk, so enriching moves neither query's hash."""
    from kustology.ir import compute_semantic_hash
    from kustology.ir.binder import SchemaAttacher

    join_ir = parse("L | join (R) on $left.k == $right.b").to_ir(attach_schema=False)
    join_before = compute_semantic_hash(join_ir)
    SchemaAttacher(DICT_SCHEMA).enrich(join_ir)
    assert compute_semantic_hash(join_ir) == join_before

    find_ir = parse("find in (T) where a > 1").to_ir(attach_schema=False)
    find_before = compute_semantic_hash(find_ir)
    SchemaAttacher(DICT_SCHEMA).enrich(find_ir)
    assert compute_semantic_hash(find_ir) == find_before
