# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Pipeline-source fidelity: datatable, externaldata, qualifiers, wildcards.

The source position used to be the lossiest slot in the IR. Four genuinely
different queries collapsed onto indistinguishable nodes -- two ``datatable``
literals onto the same argument-less ``FuncCallSource``, two ``externaldata``
URI sets onto the same ``ExternalDataExpr`` holding only the first URI,
``database('d1').T`` and ``database('d2').T`` onto the same bare ``TableRef``,
and a wildcard ``T*`` onto a ``TableRef`` no consumer could tell from a
literal table called ``T*``. Every assertion below is on a real parse.
"""

from __future__ import annotations

import json

import pytest

from kustology.ir import (
    DataTableSource,
    ExternalDataExpr,
    ExternalDataSource,
    IRBuilder,
    LiteralExpr,
    Pipeline,
    SchemaAttacher,
    TableRef,
    UnknownSource,
    find_all,
    to_llm_dict,
)


@pytest.fixture
def builder() -> IRBuilder:
    return IRBuilder()


def _row_values(source: DataTableSource) -> list[list[object]]:
    return [[cell.value for cell in row] for row in source.rows]


# --- datatable --------------------------------------------------------------


def test_datatable_source_records_columns_and_reshaped_rows(builder):
    """``datatable`` is an inline table literal; its values *are* the query.

    The builder used to emit ``FuncCallSource(name="datatable", args=[])``,
    so the schema and every row were discarded and two different literals
    were the same IR node.
    """
    ir = builder.build('datatable(a:int, b:string)[1,"x",2,"y"] | take 1')
    source = ir.main_pipeline.source
    assert isinstance(source, DataTableSource)
    assert source.columns == [("a", "int"), ("b", "string")]
    # The .NET node hands over a *flat* value list; rows are the reshape.
    assert _row_values(source) == [[1, "x"], [2, "y"]]
    assert all(isinstance(c, LiteralExpr) for row in source.rows for c in row)


def test_two_different_datatables_hash_differently(builder):
    """The collapse this closes: same shape, different data, one hash."""
    a = builder.build("datatable(a:long)[1] | take 1").semantic_hash
    b = builder.build("datatable(a:long)[2] | take 1").semantic_hash
    assert a != b


def test_datatable_columns_seed_the_binder_scope(builder):
    """A ``datatable`` declares its own schema, so the binder needs no help."""
    ir = builder.build("datatable(a:long, b:string)[1,'x'] | project a")
    SchemaAttacher({}).enrich(ir)
    assert ir.main_pipeline.result_schema is not None
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


# --- externaldata -----------------------------------------------------------


def test_externaldata_at_source_position_is_an_external_data_source(builder):
    """``externaldata`` in source position is a table, not an expression."""
    ir = builder.build(
        'externaldata(a:string)[h"https://x"] with (format="csv") | where a == "x"'
    )
    source = ir.main_pipeline.source
    assert isinstance(source, ExternalDataSource)
    # ``h"..."`` is KQL's obfuscated string literal. The DLL decodes it on
    # ``LiteralValue``; reading the node's text back would keep the ``h`` and
    # the quotes.
    assert source.uris == ["https://x"]
    assert source.columns == [("a", "string")]
    assert source.format == "csv"


def test_externaldata_source_keeps_every_uri(builder):
    """Two URI sets are two different queries."""
    one = builder.build('externaldata(a:string)["https://x"] | take 1')
    two = builder.build('externaldata(a:string)["https://x","https://y"] | take 1')
    assert one.main_pipeline.source.uris == ["https://x"]
    assert two.main_pipeline.source.uris == ["https://x", "https://y"]
    assert one.semantic_hash != two.semantic_hash


def test_let_externaldata_rhs_is_a_pipeline(builder):
    """D12. ``externaldata`` is tabular in KQL, so the binding is tabular.

    It used to land on ``rhs_expr`` because there was no source class to
    build a pipeline around -- which made ``rhs_pipeline is not None`` an
    unreliable "is this binding tabular" test. There is one now.
    """
    ir = builder.build(
        'let X = externaldata(a:string)["https://x","https://y"]; X | take 1'
    )
    binding = ir.let_bindings[0]
    assert binding.rhs_expr is None
    assert isinstance(binding.rhs_pipeline, Pipeline)
    assert isinstance(binding.rhs_pipeline.source, ExternalDataSource)
    assert binding.rhs_pipeline.source.uris == ["https://x", "https://y"]
    assert binding.rhs_pipeline.operators == []
    # ``externaldata`` reads a URI, not a table.
    assert binding.inner_tables == []


def test_externaldata_columns_seed_the_binder_scope(builder):
    """The declared schema is the feed's schema; no table lookup applies.

    A tabular ``let`` whose right-hand side is an ``externaldata`` therefore
    registers real columns under its name, which only became possible once
    the binding took the ``rhs_pipeline`` shape.
    """
    ir = builder.build(
        'let Feed = externaldata(id:string, n:long)["https://x"]; Feed | project id'
    )
    SchemaAttacher({}).enrich(ir)
    assert ir.let_bindings[0].rhs_pipeline.result_schema.columns == {
        "id": "string", "n": "long",
    }
    assert ir.main_pipeline.result_schema.columns == {"id": "string"}


def test_externaldata_expression_records_every_uri(builder):
    """The expression-position node keeps the same list-valued field.

    ``uri: str`` held the *first* URI only, so a two-URI feed and a one-URI
    feed were the same node.
    """
    ir = builder.build(
        'T | where C in ((externaldata(a:string)["https://x","https://y"]))'
    )
    e = next(iter(find_all(ir, ExternalDataExpr)))
    assert e.uris == ["https://x", "https://y"]
    assert e.canonical_form == "externaldata(a:string)[https://x, https://y]"


# --- database / cluster qualifiers -----------------------------------------


def test_database_qualifier_is_recorded(builder):
    ir = builder.build("database('d').T | take 1")
    source = ir.main_pipeline.source
    assert isinstance(source, TableRef)
    assert source.name == "T"
    assert source.database == "d"
    assert source.cluster is None


def test_cluster_qualifier_is_recorded(builder):
    ir = builder.build("cluster('c').database('d').T | take 1")
    source = ir.main_pipeline.source
    assert (source.cluster, source.database, source.name) == ("c", "d", "T")


def test_different_databases_hash_differently(builder):
    """``database('d1').T`` and ``database('d2').T`` read different tables."""
    a = builder.build("database('d1').T | take 1").semantic_hash
    b = builder.build("database('d2').T | take 1").semantic_hash
    assert a != b


def test_unqualified_table_leaves_both_qualifiers_none(builder):
    source = builder.build("T | take 1").main_pipeline.source
    assert (source.cluster, source.database, source.is_wildcard) == (None, None, False)


# --- wildcards --------------------------------------------------------------


def test_bare_wildcard_table_is_flagged(builder):
    ir = builder.build("union T*")
    inner = ir.main_pipeline.operators[0].pipelines[0].source
    assert isinstance(inner, TableRef)
    assert inner.name == "T*"
    assert inner.is_wildcard is True


def test_qualified_wildcard_keeps_the_database(builder):
    ir = builder.build("union database('d').*")
    inner = ir.main_pipeline.operators[0].pipelines[0].source
    assert inner.name == "*"
    assert inner.database == "d"
    assert inner.is_wildcard is True


def test_wildcard_and_literal_table_hash_differently(builder):
    """``T*`` matches a set of tables; a table *named* ``T*`` is one table."""
    a = builder.build("union T*").semantic_hash
    b = builder.build("union ['T*']").semantic_hash
    assert a != b


def test_wildcard_source_resolves_to_an_empty_binder_scope(builder):
    """A wildcard names a set, so no single table's columns are in scope."""
    ir = builder.build("union T*")
    SchemaAttacher({"T*": {"a": "long"}}).enrich(ir)
    inner = ir.main_pipeline.operators[0].pipelines[0]
    assert inner.result_schema is not None
    assert inner.result_schema.columns == {}


def test_qualified_table_still_looks_up_on_the_bare_name(builder):
    """Documented boundary: schema keys are bare table names.

    ``database('d').T`` resolves against ``schemas["T"]`` -- there is no
    ``"d.T"`` key convention, and inventing one would silently stop
    resolving every qualified query.
    """
    ir = builder.build("database('d').T | project a")
    SchemaAttacher({"T": {"a": "long"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


# --- UnknownSource ----------------------------------------------------------


def test_unknown_source_records_the_real_source_text(builder):
    """``raw_text`` was the literal string "unknown" on every node.

    Every unmodelled source therefore hashed the same, which is the exact
    failure mode ``UnknownExpr``/``UnknownOp`` avoid by carrying their text.
    """
    a = builder.build("let x = 1;")
    b = builder.build("let y = 2;")
    sa = next(iter(find_all(a, UnknownSource)))
    sb = next(iter(find_all(b, UnknownSource)))
    assert sa.raw_text != "unknown"
    assert "let x = 1;" in sa.raw_text
    assert sa.raw_text != sb.raw_text


# --- LLM view cap -----------------------------------------------------------


def _big_datatable(n: int) -> str:
    return "datatable(a:long)[" + ",".join(str(i) for i in range(n)) + "] | take 1"


def test_llm_view_caps_datatable_rows(builder):
    """Real IOC datatables run to thousands of rows; an LLM needs a sample."""
    ir = builder.build(_big_datatable(25))
    view = to_llm_dict(ir)
    source = view["main_pipeline"]["source"]
    assert source["kind"] == "datatable_source"
    assert len(source["rows"]) == 20
    assert source["rows_omitted"] == 5


def test_llm_view_leaves_a_short_datatable_uncapped(builder):
    ir = builder.build(_big_datatable(3))
    source = to_llm_dict(ir)["main_pipeline"]["source"]
    assert len(source["rows"]) == 3
    assert "rows_omitted" not in source


def test_model_dump_json_keeps_every_datatable_row(builder):
    """The cap is a view concern only -- the canonical dump stays complete."""
    ir = builder.build(_big_datatable(25))
    payload = json.loads(ir.model_dump_json())
    assert len(payload["main_pipeline"]["source"]["rows"]) == 25
    assert "rows_omitted" not in payload["main_pipeline"]["source"]


# --- routed from Task 2.3: comments must not reach the hash -----------------


@pytest.mark.parametrize(
    ("commented", "plain"),
    [
        ("T | assert-schema (a: // note\n long)", "T | assert-schema (a:long)"),
        ("T | parse-kv Msg as (a: // note\n long)", "T | parse-kv Msg as (a:long)"),
        (
            'T | where C in ((externaldata(a: // note\n string)["https://x"]))',
            'T | where C in ((externaldata(a:string)["https://x"]))',
        ),
        (
            'datatable(a: // note\n long)[1] | take 1',
            "datatable(a:long)[1] | take 1",
        ),
    ],
)
def test_column_type_reads_do_not_carry_comments(builder, commented, plain):
    """``ToString()`` is ``IncludeTrivia.All`` -- it prepends the comment.

    Task 2.3 made these column dicts load-bearing for ``semantic_hash``, so
    a ``//`` comment between the colon and the type name changed the digest.
    ``node_text`` (``IncludeTrivia.Minimal``) reads the node's own source.
    """
    assert builder.build(commented).semantic_hash == builder.build(plain).semantic_hash
