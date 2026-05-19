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


def test_distinct_preserves_scope(builder, attacher):
    ir = builder.build("DeviceProcessEvents | distinct FileName")
    attacher.enrich(ir)
    cols = _final_columns(attacher, ir.main_pipeline)
    # distinct is a row-filter; binder keeps the source scope intact.
    assert "FileName" in cols


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
