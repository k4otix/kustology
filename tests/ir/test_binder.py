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
