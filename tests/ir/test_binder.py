# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Scope-propagation tests for ``SchemaAttacher``.

Each test parses a query, enriches with a schema, then asks the binder
for the resulting scope. The contract is that scope-mutating operators
(project, project-away, parse, mv-expand, …) *actually mutate* the scope
list — without it, downstream column refs see the wrong view.
"""

import pytest

from kustology.ir import IRBuilder, SchemaAttacher


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


def _final_columns(attacher, pipeline) -> set[str]:
    scope = attacher._walk_pipeline(pipeline)
    return {c for entry in scope for c in entry.columns}


# Scope-narrowing operators: project / project-rename / summarize / extend / distinct ---

def test_project_narrows_scope(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents "
        "| project FileName, AccountName "
        "| where FileName == 'cmd.exe'"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "FileName" in cols
    assert "AccountName" in cols
    assert "DeviceName" not in cols
    assert "TimeGenerated" not in cols


def test_project_rename_renames_in_scope(builder, attacher):
    ir = builder.build("DeviceProcessEvents | project-rename Proc = FileName")
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "Proc" in cols
    assert "FileName" not in cols


def test_extend_adds_columns(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | extend Lower = tolower(FileName)"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "Lower" in cols
    assert "FileName" in cols  # original still visible


def test_summarize_replaces_scope_with_aggregations_and_grouping(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | summarize Count = count() by FileName"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "Count" in cols
    assert "FileName" in cols
    # Other columns from the source table are gone after summarize
    assert "DeviceName" not in cols
    assert "TimeGenerated" not in cols


def test_distinct_narrows_scope_to_listed_columns(builder, attacher):
    ir = builder.build("DeviceProcessEvents | distinct FileName")
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    # `distinct C1, C2` is semantically equivalent to `summarize by C1, C2`;
    # the output schema is exactly the listed columns.
    assert cols == {"FileName"}


def test_distinct_star_preserves_full_scope(builder, attacher):
    ir = builder.build("DeviceProcessEvents | distinct *")
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    # `distinct *` keeps every source column.
    assert "FileName" in cols
    assert "DeviceName" in cols
    assert "TimeGenerated" in cols


# project-away / project-keep / project-reorder -------------------------

def test_project_away_subtracts_from_scope(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | project-away DeviceName, TimeGenerated"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "FileName" in cols
    assert "AccountName" in cols
    assert "DeviceName" not in cols
    assert "TimeGenerated" not in cols


def test_project_keep_retains_only_named(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | project-keep FileName, AccountName"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert cols == {"FileName", "AccountName"}


def test_project_reorder_preserves_all(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | project-reorder TimeGenerated, FileName"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "FileName" in cols
    assert "AccountName" in cols
    assert "DeviceName" in cols
    assert "TimeGenerated" in cols


# parse / parse-where capture groups -------------------------------------

def test_parse_adds_capture_columns(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents "
        "| parse FileName with 'prefix_' UserName '_suffix' "
        "| where UserName == 'admin'"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "UserName" in cols


def test_parse_where_also_adds_capture_columns(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | parse-where FileName with 'prefix_' Tag '_suffix'"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "Tag" in cols


# mv-expand element-type swap --------------------------------------------

def test_mvexpand_preserves_column_visibility(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents "
        "| extend Items = pack_array(FileName, AccountName) "
        "| mv-expand Items "
        "| where Items == 'cmd.exe'"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "Items" in cols


# make-series synthesis --------------------------------------------------

def test_makeseries_synthesizes_aggregate_and_group_columns(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents "
        "| make-series Count = count() default = 0 on TimeGenerated "
        "from datetime(2026-01-01) to datetime(2026-01-02) step 1h "
        "by FileName"
    )
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    assert "Count" in cols
    assert "FileName" in cols


# Pipeline.result_schema population --------------------------------------

def test_pipeline_result_schema_populated_after_enrich(builder, attacher):
    from kustology.ir import TabularSchema
    ir = builder.build(
        "DeviceProcessEvents | project FileName, AccountName"
    )
    attacher.enrich(ir)
    schema = ir.main_pipeline.result_schema
    assert isinstance(schema, TabularSchema)
    assert set(schema.columns.keys()) == {"FileName", "AccountName"}


def test_summarize_result_schema_grouping_keys_before_aggregations(builder, attacher):
    """KQL summarize emits ``by`` columns before aggregations — match that ordering."""
    ir = builder.build(
        "DeviceProcessEvents | summarize attempts = count() by DeviceName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["DeviceName", "attempts"]


def test_summarize_result_schema_multiple_keys_and_aggs(builder, attacher):
    ir = builder.build(
        "DeviceProcessEvents | summarize c = count(), p = dcount(ProcessId) by DeviceName, FileName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["DeviceName", "FileName", "c", "p"]


def test_make_series_result_schema_keys_aggs_then_on_axis(builder, attacher):
    """make-series emits: by-keys, aggregation series, on-axis (dynamic)."""
    ir = builder.build(
        "DeviceProcessEvents "
        "| make-series c = count(), s = sum(ProcessId) default=0 "
        "on TimeGenerated step 1h by DeviceName, FileName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["DeviceName", "FileName", "c", "s", "TimeGenerated"]
    types = ir.main_pipeline.result_schema.columns
    assert types["c"] == "dynamic"
    assert types["s"] == "dynamic"
    assert types["TimeGenerated"] == "dynamic"


def test_project_keep_result_schema_uses_source_order(builder, attacher):
    """project-keep preserves the source-table column order, not the user-list order."""
    ir = builder.build(
        "DeviceProcessEvents | project-keep AccountName, FileName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    # FileName precedes AccountName in the schema fixture, so source-order wins.
    assert cols == ["FileName", "AccountName"]


def test_project_reorder_result_schema_listed_first_then_rest(builder, attacher):
    """project-reorder puts listed columns first (in listed order), rest follow in source order."""
    ir = builder.build(
        "DeviceProcessEvents | project-reorder DeviceName, FileName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols[:2] == ["DeviceName", "FileName"]
    # remaining source columns follow in their original order
    assert cols[2:] == ["AccountName", "TimeGenerated", "ProcessId"]


def test_project_rename_result_schema_preserves_position(builder, attacher):
    """project-rename keeps the renamed column in its original position."""
    ir = builder.build(
        "DeviceProcessEvents | project-rename Proc = FileName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    # FileName was at index 0 in the schema fixture; Proc should now be at index 0.
    assert cols[0] == "Proc"
    assert "FileName" not in cols


# Auto-naming parity -----------------------------------------------------

def test_summarize_unnamed_aggregation_canonical_name(builder, attacher):
    """KQL names unnamed aggregations like ``count()`` -> ``count_``, ``avg(X)`` -> ``avg_X``."""
    ir = builder.build(
        "DeviceProcessEvents | summarize count(), avg(ProcessId), dcount(AccountName)"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["count_", "avg_ProcessId", "dcount_AccountName"]


def test_summarize_bin_in_by_clause_extracts_inner_column(builder, attacher):
    """``summarize by bin(C, ...)`` projects ``C`` as the output column name."""
    ir = builder.build(
        "DeviceProcessEvents | summarize count() by bin(TimeGenerated, 1h), DeviceName"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["TimeGenerated", "DeviceName", "count_"]


def test_summarize_function_wrapped_grouping_extracts_inner_column(builder, attacher):
    """In a by-clause, any function call over a single column ref auto-names to the inner column."""
    ir = builder.build(
        "DeviceProcessEvents | summarize count() by tostring(DeviceName)"
    )
    attacher.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["DeviceName", "count_"]


# Join column suffixing --------------------------------------------------

def test_join_inner_suffixes_colliding_right_columns(builder, attacher):
    """KQL suffixes right-side columns that collide with left-side names (Foo -> Foo1)."""
    schemas = {
        "T": {"K": "string", "V": "long"},
        "U": {"K": "string", "V": "long", "Other": "string"},
    }
    attacher2 = SchemaAttacher(schemas)
    ir = builder.build("T | join kind=inner U on K")
    attacher2.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["K", "V", "K1", "V1", "Other"]


def test_lookup_drops_right_join_key_but_suffixes_other_collisions(builder, attacher):
    """Lookup drops the right's join key (merged into left's) but suffixes other collisions."""
    schemas = {
        "T": {"K": "string", "Shared": "string"},
        "L": {"K": "string", "Shared": "string", "Extra": "long"},
    }
    attacher2 = SchemaAttacher(schemas)
    ir = builder.build("T | lookup L on K")
    attacher2.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["K", "Shared", "Shared1", "Extra"]


def test_multi_join_increments_suffix_per_collision(builder, attacher):
    """Repeated joins on a colliding key produce Foo, Foo1, Foo2 in order."""
    schemas = {
        "T": {"K": "string"},
        "A": {"K": "string"},
        "B": {"K": "string"},
    }
    attacher2 = SchemaAttacher(schemas)
    ir = builder.build("T | join A on K | join B on K")
    attacher2.enrich(ir)
    cols = list(ir.main_pipeline.result_schema.columns.keys())
    assert cols == ["K", "K1", "K2"]


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
# These four shapes all failed the same way before the walker was derived
# from model_fields: a ColumnRef the binder never visited keeps table=None,
# so the *same* column resolves inconsistently within one query. Any lineage
# analyzer reading ColumnRef.table gets a silently wrong answer.


def _refs(ir):
    """{name: {table, ...}} for every ColumnRef in the IR."""
    from kustology.ir import ColumnRef, find_all

    out: dict[str, set] = {}
    for c in find_all(ir, ColumnRef):
        out.setdefault(c.name, set()).add(c.table)
    return out


def test_columns_resolve_inside_toscalar(builder, attacher):
    """A pipeline nested in an expression resolves against its own source.

    ``_fill`` recursed a hardcoded attribute tuple that had no ``pipeline``
    entry, so ToScalarExpr / MaterializeExpr / SubqueryExpr subtrees were
    never entered.
    """
    ir = builder.build(
        "DeviceProcessEvents "
        "| where ProcessId > toscalar(DeviceProcessEvents | summarize max(ProcessId)) "
        "| project AccountName"
    )
    attacher.enrich(ir)
    # The same column inside and outside the toscalar must agree.
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_columns_resolve_inside_case_arms(builder, attacher):
    """``CaseExpr.branches`` is tuple-nested and ``default`` was unlisted."""
    ir = builder.build(
        "DeviceProcessEvents "
        "| extend Risk = iif(ProcessId > 100, AccountName, DeviceName)"
    )
    attacher.enrich(ir)
    refs = _refs(ir)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}
    assert refs["DeviceName"] == {"DeviceProcessEvents"}


def test_columns_resolve_under_operators_without_a_scope_rule(builder, attacher):
    """`sort` and `top` carry expressions but reshape nothing.

    ``_walk_operator`` was an isinstance chain over 17 of 53 operator types
    with no fallback -- it fell off the end, so the other 36 filled nothing.
    """
    ir = builder.build(
        "DeviceProcessEvents "
        "| sort by ProcessId desc "
        "| top 5 by TimeGenerated "
        "| project AccountName"
    )
    attacher.enrich(ir)
    refs = _refs(ir)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["TimeGenerated"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}


def test_columns_resolve_through_a_nested_pipeline_source(builder, attacher):
    """``Pipeline.source`` may itself be a ``Pipeline``.

    ``let M = materialize(T | where X)`` is the shape that produces one --
    ``materialize(...)`` at the head of a bare statement is not accepted by
    Microsoft's parser at all. ``_source_entry`` returned an empty anonymous
    scope for the nested case and never walked the inner pipeline, so the
    outer pipeline started from nothing.
    """
    from kustology.ir.query import Pipeline

    ir = builder.build(
        "let M = materialize(DeviceProcessEvents | where ProcessId > 1); "
        "M | count"
    )
    inner = ir.let_bindings[0].rhs_pipeline
    assert isinstance(inner.source, Pipeline), "expected a nested pipeline source"

    attacher._walk_pipeline(inner)
    # The nested source's table reached the outer pipeline's scope, and the
    # ColumnRef inside it was visited.
    assert set(inner.result_schema.columns) >= {"ProcessId", "AccountName"}
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_count_reshapes_scope_to_a_single_column(builder, attacher):
    """``count`` replaces the schema entirely; ``count as N`` names it."""
    ir = builder.build("DeviceProcessEvents | where ProcessId > 1 | count")
    attacher.enrich(ir)
    assert set(ir.main_pipeline.result_schema.columns) == {"Count"}

    ir2 = builder.build("DeviceProcessEvents | count as Hits")
    attacher.enrich(ir2)
    assert set(ir2.main_pipeline.result_schema.columns) == {"Hits"}


# --- let threading ---------------------------------------------------------


def test_let_pipeline_is_enriched(builder, attacher):
    """``enrich`` walked ``main_pipeline`` only, so a tabular binding's
    ``result_schema`` stayed None and the ColumnRefs inside it kept
    ``table=None`` even on a fully bound parse."""
    ir = builder.build(
        "let Base = DeviceProcessEvents | where ProcessId > 1; Base | count"
    )
    attacher.enrich(ir)
    binding = ir.let_bindings[0]
    assert binding.rhs_pipeline.result_schema is not None
    assert "AccountName" in binding.rhs_pipeline.result_schema.columns
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_main_pipeline_resolves_columns_through_a_let_name(builder, attacher):
    """The whole point: `Base | project AccountName` knows what Base holds."""
    ir = builder.build(
        "let Base = DeviceProcessEvents | where ProcessId > 1; "
        "Base | project AccountName"
    )
    attacher.enrich(ir)
    refs = _refs(ir)
    assert refs["AccountName"] == {"Base"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_let_threading_follows_a_chain(builder, attacher):
    ir = builder.build(
        "let A = DeviceProcessEvents | project AccountName, ProcessId; "
        "let B = A | where ProcessId > 2; "
        "B | project AccountName"
    )
    attacher.enrich(ir)
    a, b = ir.let_bindings
    assert set(a.rhs_pipeline.result_schema.columns) == {"AccountName", "ProcessId"}
    assert set(b.rhs_pipeline.result_schema.columns) == {"AccountName", "ProcessId"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_let_threading_does_not_resolve_a_forward_reference(builder, attacher):
    """A binding naming one declared later is not a LetRef at all, so there
    is nothing to thread -- it stays an opaque table.

    Its ``result_schema`` is ``None``: nothing determined what the binding
    emits. An empty ``TabularSchema`` would state that it emits no columns,
    which is a different and false claim.
    """
    ir = builder.build(
        "let Early = Later | take 1; "
        "let Later = DeviceProcessEvents | take 1; "
        "Early | project AccountName"
    )
    attacher.enrich(ir)
    assert ir.let_bindings[0].rhs_pipeline.result_schema is None


def test_scalar_binding_is_untouched_by_threading(builder, attacher):
    ir = builder.build("let lookback = 1h; DeviceProcessEvents | count")
    attacher.enrich(ir)
    assert ir.let_bindings[0].rhs_pipeline is None
    assert ir.let_bindings[0].rhs_expr is not None


def test_let_names_do_not_leak_between_enrich_calls(attacher, builder):
    """The let scope is per-call state; a reused attacher must not carry
    one query's binding names into the next."""
    first = builder.build(
        "let Base = DeviceProcessEvents | project AccountName; Base | count"
    )
    attacher.enrich(first)
    # `Base` here is an unrelated, unknown table -- not the previous binding.
    second = builder.build("Base | project AccountName")
    attacher.enrich(second)
    assert second.main_pipeline.result_schema.columns.get("AccountName") == "unknown"


# Microsoft's per-operator schema (K-ARCH-1, Task 5.2) --------------------

def test_provenance_survives_an_authoritative_result_schema():
    """Taking names and types from the binder must not cost ``ColumnRef.table``.

    Microsoft's ``ResultType`` says which columns exist and how they are
    typed; it does not say which table each came from, and this walk is the
    only thing in the library that can. Replacing the scope with one
    anonymous entry carrying Microsoft's columns — the obvious shortcut —
    would silently drop provenance for every operator the binder can type.
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

    # The shortcut really did fire — otherwise this proves nothing.
    assert all(op.result_schema is not None for op in ir.main_pipeline.operators)

    project = next(op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp))
    sort = next(op for op in ir.main_pipeline.operators if isinstance(op, SortOp))
    assert {c.table for c in find_all(project, ColumnRef)} == {"DeviceProcessEvents"}
    assert {c.table for c in find_all(sort, ColumnRef)} == {"DeviceProcessEvents"}


def test_join_provenance_survives_an_authoritative_result_schema():
    """The per-side scope entries a join builds are still built.

    ``$left`` / ``$right`` resolve against a scope that has both sides in it,
    and a right-hand column has a table only because the join rule appended
    an entry for it. Both are things the overlay cannot reconstruct, so the
    hand-rolled rule keeps running for ``join`` / ``lookup`` / ``union`` even
    when Microsoft answered.
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


def test_the_hand_rolled_rule_still_runs_when_microsoft_declines(builder, attacher):
    """``IRBuilder().build`` binds against globals with no tables at all.

    Every symbol it produces is open, so nothing is recorded and the scope
    rules answer alone — which is what keeps a caller's own schema, handed
    to the attacher afterwards, from being overridden by the binder's
    table-less reading.
    """
    ir = builder.build(
        "DeviceProcessEvents | project FileName, AccountName "
        "| where FileName == 'cmd.exe'"
    )
    assert all(op.result_schema is None for op in ir.main_pipeline.operators)
    attacher.enrich(ir)
    # Narrowed by the hand-rolled ``project`` rule, and typed from the
    # schema the *attacher* was given -- which the binder never saw.
    assert ir.main_pipeline.result_schema.columns == {
        "FileName": "string", "AccountName": "string",
    }


def test_count_closes_a_symbol_even_from_an_unknown_table(builder):
    """``IsOpen`` is per node, not per query, and ``count`` is the proof.

    ``T | count`` returns exactly ``Count:long`` whatever ``T`` is, so
    Microsoft closes the symbol there even though the source is unknown --
    which is why the decline is read off each operator rather than decided
    once for the whole parse.
    """
    ir = builder.build("Whatever | where x > 1 | count")
    where_op, count_op = ir.main_pipeline.operators
    assert where_op.result_schema is None
    assert count_op.result_schema.columns == {"Count": "long"}


def test_microsoft_decides_the_column_order_not_the_scope_grouping():
    """A join's output is ordered by the engine, not by which side it came from.

    ``ScopeEntry`` groups columns by originating table so provenance
    survives, which means merging the scope orders a join's output
    left-side-first. Microsoft emits ``DeviceId, FileName, DeviceId1,
    TimeGenerated`` — the pipeline schema has to be its list, in its order.
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


# --- Task 5.3: the fallback scope walk ------------------------------------
#
# Everything below exercises ``SchemaAttacher({...}).enrich(parse(q).to_ir())``
# -- the *unbound* path. ``parse(q)`` alone binds against ``GlobalState.Default``,
# which describes no tables, so every operator's ``result_schema`` is ``None``
# and the hand-rolled rules answer alone. That is a supported public entry
# point (a caller who has a schema dict but no cluster to bind against), and
# it is also the only way to reach these rules now that a bound parse takes
# Microsoft's answer.

FALLBACK_SCHEMA = {
    "L": {"k": "string", "a": "long", "shared": "string"},
    "R": {"k": "string", "b": "real", "shared": "string"},
    "T": {
        "k": "string", "a": "long", "t": "datetime",
        "d": "dynamic", "s": "string", "g": "guid",
    },
    "U": {"k": "string", "a": "string", "z": "long"},
}


def _fallback(query: str, schemas: dict | None = None):
    """``SchemaAttacher(schemas).enrich(parse(query).to_ir())``.

    Asserts the premise as it goes: if any operator carried Microsoft's own
    ``result_schema`` the hand-rolled rule under test never ran and the
    assertions downstream would be proving the wrong thing.
    """
    from kustology import parse

    ir = parse(query).to_ir()
    assert all(
        op.result_schema is None for op in ir.main_pipeline.operators
    ), "premise: the unbound path must leave the fallback rules to answer"
    return SchemaAttacher(schemas if schemas is not None else FALLBACK_SCHEMA).enrich(ir)


def _columns(ir) -> list[tuple[str, str]]:
    schema = ir.main_pipeline.result_schema
    return list(schema.columns.items()) if schema is not None else None


def _tables(ir) -> dict[str, set]:
    """``{column name: {table, ...}}`` over every ``ColumnRef`` in the IR."""
    from kustology.ir import ColumnRef, find_all

    out: dict[str, set] = {}
    for c in find_all(ir, ColumnRef):
        out.setdefault(c.name, set()).add(c.table)
    return out


# K28 (provenance): ScopeEntry.origins ---------------------------------------


def test_project_carries_provenance_into_a_later_operator():
    """``project`` replaced the scope with a table-less entry, so every
    column reference *after* it lost its table -- the same column resolved
    to ``T`` before the project and to ``None`` after it, in one query."""
    ir = _fallback("T | project a, k | where a > 1")
    assert _tables(ir)["a"] == {"T"}


def test_project_away_and_project_keep_carry_provenance():
    away = _fallback("T | project-away s | where a > 1")
    assert _tables(away)["a"] == {"T"}
    keep = _fallback("T | project-keep a | where a > 1")
    assert _tables(keep)["a"] == {"T"}


def test_distinct_carries_provenance():
    ir = _fallback("T | distinct k | where k == 'x'")
    assert _tables(ir)["k"] == {"T"}


def test_project_rename_carries_provenance_under_the_new_name():
    """The renamed column is still the source table's column."""
    ir = _fallback("T | project-rename kk = k | where kk == 'x'")
    assert _tables(ir)["kk"] == {"T"}


def test_a_computed_column_has_no_table_and_does_not_borrow_one():
    """``origins`` must record "invented here", not inherit the neighbours'."""
    ir = _fallback("T | project n = a + 1, k | where n > 1")
    tables = _tables(ir)
    assert tables["k"] == {"T"}
    assert tables["n"] == {None}


def test_summarize_keys_keep_provenance_and_aggregates_do_not():
    ir = _fallback("T | summarize c = count() by k | where c > 1 and k == 'x'")
    tables = _tables(ir)
    assert tables["k"] == {"T"}
    assert tables["c"] == {None}


def test_an_ambiguous_unqualified_column_resolves_to_no_table():
    """``T | union U`` puts ``k`` in two scope entries with different tables.

    Picking the most recently appended side was a guess: KQL's own answer is
    that the unqualified name is ambiguous, so the honest provenance is
    "unknown", not "U".
    """
    ir = _fallback("T | union U | where k == 'x'")
    assert _tables(ir)["k"] == {None}


# K07: join kinds ------------------------------------------------------------


def test_left_semi_and_anti_joins_emit_the_left_side_only():
    """A semi/anti join is a *filter*, not a widening.

    The rule appended the right side's columns for every kind, so
    ``L | join kind=leftanti (R) on k`` claimed six output columns where the
    engine emits three — and invented ``k1``/``shared1`` that no downstream
    operator can reference.
    """
    for kind in ("leftanti", "leftsemi", "anti", "leftantisemi"):
        ir = _fallback(f"L | join kind={kind} (R) on k")
        assert _columns(ir) == [
            ("k", "string"), ("a", "long"), ("shared", "string"),
        ], kind


def test_right_semi_and_anti_joins_emit_the_right_side_only():
    for kind in ("rightanti", "rightsemi", "rightantisemi"):
        ir = _fallback(f"L | join kind={kind} (R) on k")
        assert _columns(ir) == [
            ("k", "string"), ("b", "real"), ("shared", "string"),
        ], kind


def test_a_right_semi_join_reports_the_right_table_as_provenance():
    """The surviving rows are the right side's, so its ``k`` is too.

    Both tables have a ``k``; before the fix the scope still held the left
    entry, so the filter's ``k`` resolved to ``L`` — a column the operator
    above it had just discarded.
    """
    from kustology.ir import ColumnRef, FilterOp, find_all

    ir = _fallback("L | join kind=rightsemi (R) on k | where k == 'x'")
    where = next(op for op in ir.main_pipeline.operators if isinstance(op, FilterOp))
    assert {c.table for c in find_all(where, ColumnRef)} == {"R"}


def test_a_bare_join_is_innerunique_and_still_widens():
    ir = _fallback("L | join (R) on k")
    assert _columns(ir) == [
        ("k", "string"), ("a", "long"), ("shared", "string"),
        ("k1", "string"), ("b", "real"), ("shared1", "string"),
    ]


def test_join_kind_matching_is_case_insensitive():
    """``JoinOp.join_kind`` is the text the query wrote.

    Microsoft's *parser* rejects ``kind=LeftAnti`` outright (KS005, "Expected
    one of: inner, fullouter, …"), so this is not a shape a valid query
    reaches. It is reachable by a caller who builds or edits the IR directly,
    and answering a mixed-case anti join as a widening join is the worst of
    the available answers.
    """
    ir = _fallback("L | join kind=LeftAnti (R) on k")
    assert _columns(ir) == [
        ("k", "string"), ("a", "long"), ("shared", "string"),
    ]


def test_lookup_is_never_semi_or_anti():
    """``lookup`` takes only ``leftouter`` / ``inner``; both keep both sides."""
    ir = _fallback("L | lookup (R) on k")
    assert _columns(ir) == [
        ("k", "string"), ("a", "long"), ("shared", "string"),
        ("b", "real"), ("shared1", "string"),
    ]


# K08: wildcard project-keep / project-away ----------------------------------


def test_project_keep_matches_a_wildcard_term():
    """``a*`` contributed no name, so the term matched nothing and was
    silently dropped from the kept set."""
    ir = _fallback("T | project-keep k, a*")
    assert _columns(ir) == [("k", "string"), ("a", "long")]


def test_project_away_matches_a_wildcard_term():
    ir = _fallback("T | project-away a*")
    assert _columns(ir) == [
        ("k", "string"), ("t", "datetime"), ("d", "dynamic"),
        ("s", "string"), ("g", "guid"),
    ]


def test_a_bare_star_keeps_or_drops_everything():
    """A bare ``*`` lowers to ``StarExpr``, not a column named ``*``."""
    assert _columns(_fallback("T | project-keep *")) == [
        ("k", "string"), ("a", "long"), ("t", "datetime"),
        ("d", "dynamic"), ("s", "string"), ("g", "guid"),
    ]
    assert _columns(_fallback("T | project-away *")) == []


def test_wildcard_matching_is_case_sensitive():
    """KQL column names are case-sensitive and so is the pattern: Microsoft
    answers ``[]`` for ``project-keep A*`` over a table whose column is
    ``a``. ``fnmatch.fnmatch`` folds case on macOS and Windows, which would
    have made this pass by accident on two of the three CI platforms."""
    assert _columns(_fallback("T | project-keep A*")) == []


def test_wildcard_and_plain_terms_combine_in_source_order():
    ir = _fallback("T | project-keep s*, k")
    assert _columns(ir) == [("k", "string"), ("s", "string")]


# K09: mv-expand -------------------------------------------------------------


def test_mv_expand_with_itemindex_adds_the_index_column():
    """``with_itemindex=i`` emits a trailing ``long``; the rule ignored it."""
    ir = _fallback("T | mv-expand with_itemindex=i d")
    assert _columns(ir)[-1] == ("i", "long")


def test_mv_expand_with_itemindex_and_to_typeof_together():
    ir = _fallback("T | mv-expand with_itemindex=idx d to typeof(long)")
    cols = dict(_columns(ir))
    assert cols["d"] == "long"
    assert cols["idx"] == "long"


def test_mv_expand_does_not_read_the_element_type_off_result_type_inner():
    """``result_type_inner`` is the *element* type of the array expression,
    and the expanded column is not typed as its element.

    ``extend arr = pack_array(1, 2) | mv-expand arr`` records
    ``result_type_inner == long``, and the branch that read it typed ``arr``
    as ``long``. Microsoft leaves it ``dynamic``: without ``to typeof(...)``
    each expanded row still holds a dynamic value.
    """
    ir = _fallback("T | extend arr = pack_array(1, 2) | mv-expand arr")
    assert dict(_columns(ir))["arr"] == "dynamic"


def test_mv_expand_keeps_the_column_type_it_already_had():
    """Expanding a typed column does not retype it -- ``s`` stays ``string``
    -- and a column the scope does not know defaults to ``dynamic``."""
    assert dict(_columns(_fallback("T | mv-expand s")))["s"] == "string"
    assert dict(_columns(_fallback("Unknown | mv-expand q")))["q"] == "dynamic"


# K10 / K11: resolving inside a join's on-clause ------------------------------


def _on_refs(ir, index: int = -1) -> list:
    """Every ``ColumnRef`` in the ``on`` clause of one join, in order."""
    from kustology.ir import ColumnRef, JoinOp, find_all

    joins = [op for op in ir.main_pipeline.operators if isinstance(op, JoinOp)]
    return [c for e in joins[index].on for c in find_all(e, ColumnRef)]


def test_dollar_left_resolves_by_name_across_the_whole_left_side():
    """``$left`` is the accumulated left row set, not the last entry in it.

    The rule read ``scope[-2]``, which is the entry appended by the *previous*
    join. After ``L | join (R) …`` a second join's ``$left.a`` therefore
    reported ``R`` — a table that does not have an ``a`` at all — while the
    column plainly comes from ``L``.
    """
    ir = _fallback("L | join (R) on k | join (T) on $left.a == $right.a")
    left, right = _on_refs(ir)
    assert left.name == "a" and left.join_side == "left"
    assert left.table == "L"
    assert right.table == "T"


def test_dollar_right_resolves_against_the_appended_right_entry():
    ir = _fallback("L | join (R) on $left.k == $right.b")
    left, right = _on_refs(ir)
    assert (left.table, right.table) == ("L", "R")


def test_dollar_right_resolves_through_the_right_pipelines_own_operators():
    """The right side is a pipeline, so its scope may be anonymous with the
    provenance carried in ``origins`` -- ``$right.b`` is still ``R``'s."""
    ir = _fallback("L | join (R | project b) on $left.k == $right.b")
    _left, right = _on_refs(ir)
    assert right.table == "R"


def test_an_unresolvable_dollar_side_keeps_its_marker():
    """A right side that is not a table leaves ``$right`` in place rather
    than inventing a name -- the marker is the honest answer."""
    ir = _fallback("L | join (datatable(z:long)[1]) on $left.k == $right.z")
    _left, right = _on_refs(ir)
    assert right.table == "$right"


def test_a_bare_on_key_resolves_against_the_left_side():
    """``on k`` is shorthand for ``$left.k == $right.k``, and both sides have
    a ``k``. The scope holds the right side too at that point, so the general
    ambiguity rule would answer ``None``; the left is the side the engine
    keeps the column from."""
    ir = _fallback("L | join (R) on k")
    (key,) = _on_refs(ir)
    assert key.table == "L"


def test_lookups_bare_on_key_resolves_to_the_left_side_too():
    """``lookup`` drops the right side's key outright, so the left is the
    only side whose column survives -- the same answer for a stronger
    reason."""
    from kustology.ir import ColumnRef, LookupOp, find_all

    ir = _fallback("L | lookup (R) on k")
    lookup = next(
        op for op in ir.main_pipeline.operators if isinstance(op, LookupOp)
    )
    (key,) = [c for e in lookup.on for c in find_all(e, ColumnRef)]
    assert key.table == "L"


# Re-enriching one IR: the builder's schema is the only carry-over ------------


def test_enriching_twice_with_different_schemas_takes_the_second():
    """An operator-less pipeline read its *own* ``result_schema`` back.

    ``_walk_pipeline`` prefers the last operator's schema and, with no
    operators, the pipeline's own — which the builder sets from Microsoft's
    reading of the source. But a previous ``enrich`` writes that same field,
    so the second call read the first call's answer and the new schema was
    ignored. With an operator present the same sequence is correct, which is
    what makes the bug invisible to almost every test.
    """
    first = {"T": {"a": "long", "s": "string"}}
    second = {"T": {"a": "real", "s": "guid"}}

    ir = IRBuilder().build("T")
    assert not ir.main_pipeline.operators, "premise: the operator-less branch"
    SchemaAttacher(first).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long", "s": "string"}
    SchemaAttacher(second).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "real", "s": "guid"}


def test_enriching_twice_refreshes_an_operator_less_let_binding():
    """Same shape one level down, where it also decides the *name*'s schema.

    ``enrich`` reads each binding's ``result_schema`` to register what the
    alias holds, so a stale one is not confined to the binding — every
    column the main pipeline resolves through ``M`` gets the previous
    schema's type.
    """
    first = {"T": {"a": "long"}}
    second = {"T": {"a": "real"}}

    ir = IRBuilder().build("let M = materialize(T); M | project a")
    binding = ir.let_bindings[0]
    assert not binding.rhs_pipeline.operators, "premise: the operator-less branch"
    SchemaAttacher(first).enrich(ir)
    assert binding.rhs_pipeline.result_schema.columns == {"a": "long"}
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}
    SchemaAttacher(second).enrich(ir)
    assert binding.rhs_pipeline.result_schema.columns == {"a": "real"}
    assert ir.main_pipeline.result_schema.columns == {"a": "real"}


def test_enriching_twice_already_worked_with_an_operator_present():
    """The must-not-change direction: the operator branch was never stale."""
    ir = IRBuilder().build("T | where a > 1")
    SchemaAttacher({"T": {"a": "long"}}).enrich(ir)
    SchemaAttacher({"T": {"a": "real"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "real"}


# K12: union ------------------------------------------------------------------


def test_union_splits_a_name_the_two_sides_type_differently():
    """``T.a`` is a long and ``U.a`` a string, so the engine emits both under
    suffixed names and no unsuffixed ``a`` at all.

    The rule merged the entries and let the later side's type win, so the
    output claimed one ``a`` typed ``string`` — a column that does not exist
    and a lost one that does.
    """
    ir = _fallback("T | union U")
    assert _columns(ir) == [
        ("k", "string"), ("a_long", "long"), ("a_string", "string"),
        ("t", "datetime"), ("d", "dynamic"), ("s", "string"),
        ("g", "guid"), ("z", "long"),
    ]


def test_union_leaves_an_agreeing_name_alone():
    """Only a *type* disagreement splits: ``L.k`` and ``R.k`` are both
    strings, so ``k`` stays one column."""
    ir = _fallback("L | union R")
    assert _columns(ir) == [
        ("k", "string"), ("a", "long"), ("shared", "string"), ("b", "real"),
    ]


def test_union_withsource_prepends_the_source_column():
    ir = _fallback("T | union withsource=src U")
    assert _columns(ir)[0] == ("src", "string")


def test_union_split_columns_keep_the_side_they_came_from():
    ir = _fallback("T | union U | where a_string == 'x' and a_long > 1")
    tables = _tables(ir)
    assert tables["a_long"] == {"T"}
    assert tables["a_string"] == {"U"}


# K13 / K14: aggregate output columns and their names -------------------------


def _names(ir) -> list[str]:
    return [n for n, _ in _columns(ir)]


def test_arg_max_star_emits_the_ordering_column_then_the_rest():
    """``arg_max(t, *)`` returns a whole row, not one value.

    The rule emitted a single column named after the function, so five of
    the six columns the engine returns were missing from the scope.
    """
    assert _columns(_fallback("T | summarize arg_max(t, *)")) == [
        ("t", "datetime"), ("k", "string"), ("a", "long"),
        ("d", "dynamic"), ("s", "string"), ("g", "guid"),
    ]
    assert _names(_fallback("T | summarize arg_min(t, *)")) == [
        "t", "k", "a", "d", "s", "g",
    ]


def test_arg_max_with_listed_columns_emits_exactly_those():
    assert _columns(_fallback("T | summarize arg_max(t, a, s)")) == [
        ("t", "datetime"), ("a", "long"), ("s", "string"),
    ]


def test_arg_max_star_excludes_the_grouping_keys():
    """A ``by`` key is already emitted, so ``*`` does not repeat it."""
    assert _names(_fallback("T | summarize arg_max(t, *) by k")) == [
        "k", "t", "a", "d", "s", "g",
    ]


def test_a_named_arg_max_names_only_its_first_column():
    assert _names(_fallback("T | summarize m = arg_max(t, *)")) == [
        "m", "k", "a", "d", "s", "g",
    ]


def test_take_any_emits_its_columns_under_their_own_names():
    assert _columns(_fallback("T | summarize take_any(a)")) == [("a", "long")]
    assert _names(_fallback("T | summarize take_any(a, s)")) == ["a", "s"]
    assert _names(_fallback("T | summarize take_any(*)")) == [
        "k", "a", "t", "d", "s", "g",
    ]


def test_take_anyif_ignores_its_predicate_argument():
    """The second argument is a filter, not a column to emit."""
    assert _names(_fallback("T | summarize take_anyif(a, a > 1)")) == ["a"]


def test_percentiles_emits_one_column_per_percentile():
    assert _names(_fallback("T | summarize percentiles(a, 5, 50, 95)")) == [
        "percentile_a_5", "percentile_a_50", "percentile_a_95",
    ]


def test_make_set_list_and_bag_use_kqls_own_prefixes():
    """KQL drops the ``make_`` and any ``_if``: ``make_set_if(s, …)`` is
    ``set_s``, not ``make_set_if_s``."""
    assert _names(_fallback("T | summarize make_set(s)")) == ["set_s"]
    assert _names(_fallback("T | summarize make_list(s)")) == ["list_s"]
    assert _names(_fallback("T | summarize make_bag(d)")) == ["bag_d"]
    assert _names(_fallback("T | summarize make_set_if(s, a > 1)")) == ["set_s"]
    assert _names(_fallback("T | summarize make_list_if(s, a > 1)")) == ["list_s"]


def test_percentile_names_carry_the_percentile_value():
    assert _names(_fallback("T | summarize percentile(a, 95)")) == [
        "percentile_a_95",
    ]
    # A fractional percentile spells the point as an underscore.
    assert _names(_fallback("T | summarize percentile(a, 95.5)")) == [
        "percentile_a_95_5",
    ]


def test_the_auto_name_lands_on_the_assignment_not_only_the_schema():
    """``Assignment.name`` is what a consumer reads to label the column."""
    from kustology import parse
    from kustology.ir import SummarizeOp

    ir = parse("T | summarize make_set(s), percentile(a, 95)").to_ir()
    op = next(
        o for o in ir.main_pipeline.operators if isinstance(o, SummarizeOp)
    )
    assert [a.name for a in op.aggregations] == ["set_s", "percentile_a_95"]


def test_unaffected_aggregate_names_stay_as_they_were():
    """The must-not-change direction: the generic ``fname_column`` rule is
    already what the engine does for most aggregates."""
    assert _names(_fallback("T | summarize countif(a > 1)")) == ["countif_"]
    assert _names(_fallback("T | summarize dcountif(a, a > 1)")) == ["dcountif_a"]
    assert _names(_fallback("T | summarize any(a)")) == ["any_a"]
    assert _names(_fallback("T | summarize hll(a)")) == ["hll_a"]
    assert _names(_fallback("T | summarize c = count() by k")) == ["k", "c"]


# K28: schema-replacing operators the fallback had no rule for ----------------


def _syntactic(query: str, schemas: dict | None = None):
    """Enrich an IR built from a *syntactic-only* parse.

    ``getschema`` / ``print`` / ``range`` / ``count`` return the same columns
    whatever their input, so Microsoft closes their symbols even against
    globals that know no tables and the builder records the answer. That is
    the right outcome and it means the unbound-``to_ir`` path cannot reach
    the hand-rolled rule for them. ``KustoCode.Parse`` produces a tree with
    no semantics at all, which can.
    """
    from kustology.bridge import KustoCode

    ir = IRBuilder().build_from_code(KustoCode.Parse(query))
    assert all(
        op.result_schema is None for op in ir.main_pipeline.operators
    ), "premise: a syntactic-only parse leaves the fallback rules to answer"
    return SchemaAttacher(schemas if schemas is not None else FALLBACK_SCHEMA).enrich(ir)


def test_search_scopes_to_its_tables_and_adds_the_table_column():
    """``search in (T) 'x'`` had no scope rule, so it produced nothing.

    The operator replaces the scope with the searched tables' columns and
    prepends ``$table``, which names the table each row came from.
    """
    ir = _fallback("search in (T) 'x'")
    assert _columns(ir) == [
        ("$table", "string"), ("k", "string"), ("a", "long"),
        ("t", "datetime"), ("d", "dynamic"), ("s", "string"), ("g", "guid"),
    ]


def test_search_over_several_tables_splits_a_type_conflict():
    """The same rule ``union`` uses: ``T.a`` is a long and ``U.a`` a string."""
    ir = _fallback("search in (T, U) 'x'")
    assert _names(ir) == [
        "$table", "k", "a_long", "a_string", "t", "d", "s", "g", "z",
    ]


def test_an_unqualified_search_covers_every_table_it_was_given():
    """``search 'x'`` names no table, so the schema dict is the database.

    Microsoft closes this one against table-less globals -- with no tables to
    search, ``$table`` is the whole answer -- so the syntactic path is the
    one that reaches the rule.
    """
    ir = _syntactic("search 'x'", {"L": {"k": "string"}, "R": {"b": "real"}})
    assert _names(ir) == ["$table", "k", "b"]


def test_search_columns_keep_their_table():
    ir = _fallback("search in (T) 'x' | where a > 1")
    assert _tables(ir)["a"] == {"T"}


def test_parse_kv_adds_its_declared_columns():
    """``ParseKvOp.columns`` is populated and was being ignored."""
    ir = _fallback("T | parse-kv s as (n:long, m:string)")
    assert _columns(ir)[-2:] == [("n", "long"), ("m", "string")]


def test_getschema_replaces_the_scope_with_its_fixed_four_columns():
    ir = _syntactic("T | getschema")
    assert _columns(ir) == [
        ("ColumnName", "string"), ("ColumnOrdinal", "long"),
        ("DataType", "string"), ("ColumnType", "string"),
    ]


def test_print_derives_its_columns_from_the_printed_expressions():
    ir = _syntactic("print x = 1, y = 'a', z = 1.5")
    assert _columns(ir) == [("x", "long"), ("y", "string"), ("z", "real")]


def test_an_unnamed_print_column_is_named_by_its_position():
    ir = _syntactic("print x = 1, 2")
    assert _names(ir) == ["x", "print_1"]


def test_range_names_one_column_typed_by_its_start():
    assert _columns(_syntactic("range n from 1 to 10 step 1")) == [("n", "long")]
    assert _columns(
        _syntactic(
            "range n from datetime(2020-01-01) to datetime(2020-01-05) step 1d"
        )
    ) == [("n", "datetime")]


# K28: "nothing is known" is None, not an empty schema ------------------------


def test_a_pipeline_the_walk_learned_nothing_about_has_no_result_schema():
    """``result_schema = {}`` is a claim: "this emits no columns".

    A query over a table nobody described emits an unknown set of columns,
    which is a different statement, and stamping ``{}`` made the two
    indistinguishable to a consumer.
    """
    ir = _fallback("Unknown | take 1", {})
    assert ir.main_pipeline.result_schema is None


def test_an_operator_that_really_emits_nothing_still_says_so():
    """The must-not-change direction. ``project-away *`` genuinely produces
    an empty schema, and that is a determination, not an absence."""
    ir = _fallback("T | project-away *")
    assert ir.main_pipeline.result_schema is not None
    assert ir.main_pipeline.result_schema.columns == {}


def test_an_unmodelled_sub_pipeline_does_not_inherit_the_enclosing_scope():
    """A branch the builder could not model is not a branch that emits the
    input unchanged, so it gets no schema rather than the caller's.

    ``UnknownSource`` with no operators is the builder's "I could not model
    this at all"; every other implicit-source sub-pipeline (``mv-apply``,
    ``partition``, ``fork``, ``facet``) does run against the enclosing rows
    and does inherit.
    """
    from kustology.ir.binder import ScopeEntry
    from kustology.ir.query import Pipeline, UnknownSource
    from kustology.ir.spans import Span

    span = Span(text_start=0, width=1)
    branch = Pipeline(source=UnknownSource(raw_text="?", span=span), operators=[])
    attacher = SchemaAttacher(FALLBACK_SCHEMA)
    inherited = [ScopeEntry(table="T", columns={"a": "long"})]

    assert attacher._walk_pipeline(branch, inherited) == []
    assert branch.result_schema is None


def test_project_falls_back_to_the_type_the_binder_already_resolved():
    """The walk is not the only thing that knows a column's type.

    ``evaluate`` opens the symbol, so ``project`` gets no authoritative
    schema and the fallback answers -- but the binder still typed the
    ``ColumnRef`` from the parse-time schema. Reporting ``unknown`` for a
    column whose type is right there on the node discards it.
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

    # An attacher that knows nothing about ``T`` -- the schema is on the IR,
    # not in this dict.
    SchemaAttacher({}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


# K28: schema_attached means a schema was actually available ------------------


def test_schema_attached_stays_false_when_no_schema_was_available():
    """``enrich`` set the flag unconditionally, so an attacher with no
    schemas, over an IR the binder could not type either, still reported the
    IR as enriched -- the flag said "these types are real" about nothing."""
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


# Arithmetic is not a predicate, and serialize can add a column ---------------


def test_arithmetic_is_not_typed_as_a_boolean():
    """Every ``BinOp`` was typed ``bool``, arithmetic included.

    ``extend n = a + 1`` recorded ``n:bool`` -- the same answer the node
    gives for ``a > 1``, which is a predicate and this is not. ``bool`` is a
    wrong answer where ``unknown`` is merely an incomplete one; the fallback
    does not do numeric promotion, and saying so is the honest position.
    """
    cols = dict(_columns(_fallback("T | extend n = a + 1, flag = a > 1")))
    assert cols["flag"] == "bool"
    assert cols["n"] == "unknown"


def test_serialize_with_an_assignment_adds_its_column():
    """``serialize rn = row_number()`` is ``serialize`` plus an ``extend``.

    ``SerializeOp.assignments`` is populated and the rule ignored it, so the
    column existed in the engine's output and not in the scope.
    """
    ir = _fallback("T | serialize rn = row_number()")
    assert _columns(ir)[-1] == ("rn", "long")


# Union split and join right-hand side, corrected against the corpus ----------


def test_union_does_not_split_on_a_type_it_simply_does_not_know():
    """``unknown`` is the absence of a type, not a type that disagrees.

    The fallback cannot type ``a + 1``, so one arm reports ``s:unknown`` and
    the other ``s:string``. Treating that as a conflict produced
    ``s_string`` and ``s_unknown`` -- two columns the engine never emits --
    where the honest answer is one ``s`` whose type we do not know.
    """
    ir = _fallback("T | union (T | project s = a + 1)")
    columns = dict(_columns(ir))
    assert "s" in columns
    assert columns["s"] == "unknown"
    assert not [n for n in columns if n.startswith("s_")]


def test_a_join_whose_right_side_is_a_union_appends_it_once():
    """A union is one row set, and a join has one right side.

    Taking ``rhs_scope[0]`` picked the empty entry a union's implicit source
    leaves and appended nothing; keeping every entry suffixed each arm
    separately and invented an ``a2`` for a union that emits ``a1`` once.
    """
    ir = _fallback(
        "L | join (union (L | where a > 1), (R | where b > 1)) on k"
    )
    assert _columns(ir) == [
        ("k", "string"), ("a", "long"), ("shared", "string"),
        ("k1", "string"), ("a1", "long"), ("shared1", "string"),
        ("b", "real"),
    ]


# evaluate bag_unpack: the packed column is consumed --------------------------


def test_evaluate_bag_unpack_drops_the_packed_column():
    """``EvaluateOp`` had no scope rule, so the scope passed through whole.

    A plug-in can add columns the binder cannot enumerate, which is why
    Microsoft leaves the symbol *open* here -- but it still knows
    ``bag_unpack(d)`` consumes ``d``, and reports the other five columns. The
    keys the bag adds stay unknown to both of us; keeping ``d`` was a
    divergence on the part Microsoft did answer.
    """
    ir = _fallback("T | evaluate bag_unpack(d)")
    assert _names(ir) == ["k", "a", "t", "s", "g"]


def test_another_evaluate_plugin_leaves_the_scope_alone():
    """The must-not-change direction: only ``bag_unpack`` has a known rule."""
    ir = _fallback("T | evaluate autocluster()")
    assert _names(ir) == ["k", "a", "t", "d", "s", "g"]


def test_search_provenance_survives_an_authoritative_result_schema():
    """``search`` keeps its rule on a bound parse, as ``join`` does.

    It has an implicit source, so the pre-operator scope is empty and the
    overlay would file every column it emits as anonymous -- a following
    ``where a > 1`` would report no table for a column that plainly comes
    from ``T``.
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


def test_a_search_predicate_resolves_against_the_tables_being_searched():
    """``search`` has an implicit source, so the scope *before* it is empty.

    Its own predicate was filled against that empty scope, so
    ``search in (T) a > 1`` left ``a`` with no table while the same column one
    operator later (``search in (T) 'x' | where a > 1``) resolved to ``T``.
    The rule has the searched entries in hand; filling the predicate after it
    installs them is what makes the two agree.
    """
    from kustology.ir import ColumnRef, SearchOp, find_all

    ir = _fallback("search in (T) a > 1")
    search_op = next(
        op for op in ir.main_pipeline.operators if isinstance(op, SearchOp)
    )
    assert {c.table for c in find_all(search_op, ColumnRef)} == {"T"}


def test_an_unparseable_query_gets_no_result_schema():
    """``UnknownSource`` with no operators is reachable from a real string.

    The builder emits it for anything it cannot model as a source at all, and
    the pipeline then claims nothing about its own output -- where before it
    claimed, with ``columns={}``, that the query emits no columns.
    """
    ir = IRBuilder().build("not a query at all")
    from kustology.ir.query import UnknownSource

    assert isinstance(ir.main_pipeline.source, UnknownSource)
    assert not ir.main_pipeline.operators
    SchemaAttacher(FALLBACK_SCHEMA).enrich(ir)
    assert ir.main_pipeline.result_schema is None


def test_the_unknown_column_type_sentinel_is_documented_on_tabular_schema():
    """``TabularSchema.columns`` maps a column to a type *string*, and the
    string for "no type known" is ``"unknown"`` — not
    ``KustoType.UNRESOLVED.value``, which is ``"unresolved"``.

    Two sentinels for one idea, and nothing said which lived where: a
    consumer reading ``Expr.result_type`` learns to test against
    ``KustoType.UNRESOLVED`` and then finds the *other* spelling one field
    away, in a plain ``dict[str, str]`` a `KustoType` never validates. The
    reason for the split is that ``columns`` values are Microsoft's type
    *names*: ``ScalarTypes.Unknown.Name`` is literally ``"unknown"``, so a
    bound parse propagating the binder's answer and ``SchemaAttacher``
    falling back on an expression it could not type must agree, and they
    agree on Microsoft's word rather than on the IR enum's.

    Both producers are exercised here so the docstring is pinned to
    behaviour and not to itself.
    """
    import warnings

    from kustology import parse
    from kustology.ir.query import TabularSchema
    from kustology.ir.types import KustoType

    assert KustoType.UNRESOLVED.value == "unresolved"

    # Producer 1 — the bound path. Microsoft's schema parser types `n`
    # `ScalarTypes.Unknown` and the builder publishes its `Name`.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        bound = parse("T | project n | extend m = n", schema={"T": "(n:bogus)"}).to_ir()
    assert bound.main_pipeline.result_schema.columns == {"n": "unknown", "m": "unknown"}

    # Producer 2 — `SchemaAttacher`'s own fallback for an expression whose
    # `result_type` stayed `KustoType.UNRESOLVED`. Same string.
    ir = IRBuilder().build("T | extend n = some_fn(x) | project n")
    SchemaAttacher({"T": {"x": "long"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"n": "unknown"}

    doc = TabularSchema.__doc__
    assert '"unknown"' in doc, doc
    assert "KustoType.UNRESOLVED" in doc, doc
