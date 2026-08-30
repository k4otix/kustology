# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Pipeline-source fidelity: datatable, externaldata, qualifiers, wildcards.

Each source shape needs its own node. Without one, different queries
collapse together: two ``datatable`` literals onto one argument-less
``FuncCallSource``, two ``externaldata`` URI sets onto one
``ExternalDataExpr`` holding the first URI, ``database('d1').T`` and
``database('d2').T`` onto one bare ``TableRef``, and ``T*`` onto a
``TableRef`` no consumer can tell from a table literally named ``T*``.
Every assertion below is on a real parse.
"""

from __future__ import annotations

import json

import pytest

from kustology import parse
from kustology.ir import (
    ColumnRef,
    DataTableSource,
    ExternalDataExpr,
    ExternalDataSource,
    FuncCall,
    FuncCallSource,
    IRBuilder,
    LiteralExpr,
    Pipeline,
    TableRef,
    UnknownSource,
    find_all,
    to_llm_dict,
)
from kustology.ir.binder import SchemaAttacher


@pytest.fixture
def builder() -> IRBuilder:
    return IRBuilder()


def _row_values(source: DataTableSource) -> list[list[object]]:
    return [[cell.value for cell in row] for row in source.rows]


# --- datatable --------------------------------------------------------------


def test_datatable_source_records_columns_and_reshaped_rows(builder):
    """``datatable`` is an inline table literal; its values *are* the query.

    A bare ``FuncCallSource(name="datatable", args=[])`` lowering discards
    the schema and every row, collapsing two different literals onto one node.
    """
    ir = builder.build('datatable(a:int, b:string)[1,"x",2,"y"] | take 1')
    source = ir.main_pipeline.source
    assert isinstance(source, DataTableSource)
    assert source.columns == [("a", "int"), ("b", "string")]
    # The .NET node hands over a *flat* value list; rows are the reshape.
    assert _row_values(source) == [[1, "x"], [2, "y"]]
    assert all(isinstance(c, LiteralExpr) for row in source.rows for c in row)


def test_datatable_columns_seed_the_binder_scope(builder):
    """A ``datatable`` declares its own schema, so the binder needs no help."""
    ir = builder.build("datatable(a:long, b:string)[1,'x'] | project a")
    SchemaAttacher({}).enrich(ir)
    assert ir.main_pipeline.result_schema is not None
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


def test_datatable_in_expression_position_is_modeled():
    """`in ((datatable(...)))` parses clean and must not fall to UnknownExpr.

    ``HANDLED_EXPR_KINDS`` claims the kind "only ever occupies source
    position", so a coverage audit built on that claim alone misses this
    shape lowering to ``UnknownExpr(ast_kind="DataTableExpression")``, which
    hashes the raw text in place of the columns and rows.
    """
    from kustology.ir import DataTableExpr, UnknownExpr, find_all
    q = 'T | where a in ((datatable(x:string)["v", "w"]))'
    kq = parse(q)
    assert kq.diagnostics == []
    ir = kq.to_ir()
    assert not list(find_all(ir, UnknownExpr))
    (dt,) = find_all(ir, DataTableExpr)
    assert dt.columns == [("x", "string")]
    assert [cell.value for row in dt.rows for cell in row] == ["v", "w"]


# --- externaldata -----------------------------------------------------------


def test_externaldata_at_source_position_is_an_external_data_source(builder):
    """``externaldata`` in source position builds a table source."""
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


def test_externaldata_keeps_every_with_clause_property(builder):
    """Every property in the ``with (...)`` clause reaches ``properties``.

    ``ignoreFirstRecord=true`` skips the CSV header, so the feed yields one
    fewer row and a header line is not matched as data. A source node has no
    ``raw_text`` fallback, so a builder that keeps only ``format`` drops that
    text entirely and two differently parsed feeds share one
    ``semantic_hash``.

    Names are kept verbatim, in the ``dict[str, str]`` shape
    ``RenderOp.properties`` uses, through the same ``read_named_params``
    reader, so the two positions cannot drift. Values come back through
    ``LiteralValue``, which renders a KQL ``true`` as ``"True"``; that is the
    shared reader's normalization, and ``render … with (accumulate=true)``
    records ``"True"`` too.
    """
    ir = builder.build(
        'externaldata(a:string)["https://x"] '
        'with (format="csv", ignoreFirstRecord=true) | take 1'
    )
    source = ir.main_pipeline.source
    assert isinstance(source, ExternalDataSource)
    assert source.properties == {"format": "csv", "ignoreFirstRecord": "True"}
    # ``format`` is promoted because the rest of the library reads it. The
    # promotion matches case-insensitively; the dict keeps the query's casing.
    assert source.format == "csv"


def test_externaldata_in_expression_position_keeps_properties_too(builder):
    """Both positions share one reader, so neither may lag the other."""
    ir = builder.build(
        'T | where a !in ((externaldata(a:string)["https://x"] '
        'with (format="csv", ignoreFirstRecord=true)))'
    )
    found = list(find_all(ir, ExternalDataExpr))
    assert len(found) == 1
    assert found[0].properties == {"format": "csv", "ignoreFirstRecord": "True"}
    assert found[0].format == "csv"


def test_externaldata_source_keeps_every_uri(builder):
    """Two URI sets are two different queries."""
    one = builder.build('externaldata(a:string)["https://x"] | take 1')
    two = builder.build('externaldata(a:string)["https://x","https://y"] | take 1')
    assert isinstance(one.main_pipeline.source, ExternalDataSource)
    assert isinstance(two.main_pipeline.source, ExternalDataSource)
    assert one.main_pipeline.source.uris == ["https://x"]
    assert two.main_pipeline.source.uris == ["https://x", "https://y"]
    assert one.semantic_hash != two.semantic_hash


def test_uris_hold_source_text_when_an_element_is_not_a_literal(builder):
    """Documented boundary: a ``uris`` entry need not be a URI.

    A feed URL bound to a ``let``, or built with ``strcat``, has no
    ``LiteralValue``. The field records the element's source text, so the
    query stays reconstructible from the IR.
    """
    bound = builder.build(
        'let u = "https://x"; externaldata(a:string)[u] | take 1'
    )
    assert bound.main_pipeline.source.uris == ["u"]
    built = builder.build('externaldata(a:string)[strcat("https://","x")] | take 1')
    assert built.main_pipeline.source.uris == ['strcat("https://","x")']


def test_a_comment_before_a_non_literal_uri_does_not_reach_the_hash(builder):
    """The URI fallback meets the same comment leak as column-type reads.

    ``el.ToString()`` is ``IncludeTrivia.All``, so an unguarded read of
    ``externaldata(a:string)[// note<newline>u]`` records the URI as
    ``"// note\\nu"``. The branch is live on the queries the test above
    describes.
    """
    commented = builder.build(
        'let u = "https://x"; externaldata(a:string)[// note\nu] | take 1'
    )
    plain = builder.build(
        'let u = "https://x"; externaldata(a:string)[u] | take 1'
    )
    assert commented.main_pipeline.source.uris == ["u"]
    assert commented.semantic_hash == plain.semantic_hash


def test_let_externaldata_rhs_is_a_pipeline(builder):
    """D12. ``externaldata`` is tabular in KQL, so the binding is tabular.

    ``ExternalDataSource`` is the source class the pipeline is built around.
    Without it the binding lands on ``rhs_expr`` and ``rhs_pipeline is not
    None`` stops being a reliable "is this binding tabular" test.
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

    A ``let`` whose right-hand side is an ``externaldata`` takes the
    ``rhs_pipeline`` shape, so it registers real columns under its name.
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

    A ``uri: str`` field would hold only the *first* URI, making a two-URI
    feed and a one-URI feed the same node.
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


def test_all_three_table_ref_fields_land_on_one_composite(builder):
    """``cluster('c').database('d').T*`` is the only shape exercising all
    three: the left-spine walk for the two qualifiers, and the name node's
    ``Kind`` for the flag."""
    ir = builder.build("union cluster('c').database('d').T*")
    inner = ir.main_pipeline.operators[0].pipelines[0].source
    assert (inner.cluster, inner.database, inner.name, inner.is_wildcard) == (
        "c", "d", "T*", True,
    )


def test_a_wildcard_source_is_expanded_by_the_binder_not_by_the_walk():
    """A wildcard names a *set* of tables; matching them is Microsoft's job.

    ``_source_entry`` puts no columns in scope for a wildcard: the walk has
    no pattern matcher, and picking one member of the set would be a guess.
    Give the schema dict the real tables and ``union T*`` expands against
    them, so the result schema is right while a column read through the
    wildcard has ``None`` provenance, having come from a set.
    """
    ir = parse("union T* | where a > 1").to_ir(
        attach_schema={"T1": {"a": "long"}, "T2": {"a": "long"}},
    )
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}
    assert {c.table for c in find_all(ir, ColumnRef)} == {None}


def test_qualified_table_still_looks_up_on_the_bare_name(builder):
    """Documented boundary: schema keys are bare table names.

    ``database('d').T`` resolves against ``schemas["T"]``; there is no
    ``"d.T"`` key convention. The column still gets its provenance and its
    type, while Microsoft leaves the result schema ``None`` for a qualified
    source it does not know, on either entry point.
    """
    ir = builder.build("database('d').T | project a")
    SchemaAttacher({"T": {"a": "long"}}).enrich(ir)
    assert ir.main_pipeline.result_schema is None
    (ref,) = find_all(ir, ColumnRef)
    assert (ref.table, ref.result_type.value) == ("T", "long")


# --- UnknownSource ----------------------------------------------------------


def test_unknown_source_records_the_real_source_text(builder):
    """``raw_text`` carries the source text, never the literal "unknown".

    A constant would hash every unmodeled source alike, the failure mode
    ``UnknownExpr`` and ``UnknownOp`` avoid by carrying their own text.
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


@pytest.mark.parametrize("n", [3, 20, 21])
def test_llm_view_row_cap_boundary(builder, n):
    """Exactly at the cap nothing is omitted; one over, one is."""
    source = to_llm_dict(builder.build(_big_datatable(n)))["main_pipeline"]["source"]
    assert len(source["rows"]) == min(n, 20)
    assert source.get("rows_omitted") == (n - 20 if n > 20 else None)


def test_model_dump_json_keeps_every_datatable_row(builder):
    """The cap is a view concern only -- the canonical dump stays complete."""
    ir = builder.build(_big_datatable(25))
    payload = json.loads(ir.model_dump_json())
    assert len(payload["main_pipeline"]["source"]["rows"]) == 25
    assert "rows_omitted" not in payload["main_pipeline"]["source"]


# --- comments must not reach the hash ---------------------------------------


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
    """``ToString()`` is ``IncludeTrivia.All``, so it prepends the comment.

    These column dicts feed ``semantic_hash``, so reading them that way lets
    a ``//`` comment between the colon and the type name change the digest.
    ``node_text`` (``IncludeTrivia.Minimal``) reads the node's own source.
    """
    assert builder.build(commented).semantic_hash == builder.build(plain).semantic_hash


def test_read_row_schema_accepts_the_schema_and_its_owner():
    """The one reader takes either the ``RowSchema`` or the node holding it.

    The owning member is ``Schema`` on three of the four call sites and
    ``Keys`` on ``parse-kv``, and confusing the two gives an empty column
    list with no exception. Both shapes are pinned so the contract does not
    live only in a docstring.
    """
    from kustology import parse
    from kustology.ir._builder_helpers import read_row_schema
    from kustology.utils.analysis import collect_nodes

    (owner,) = collect_nodes(
        parse('datatable(a:int, b:string)[1, "x"] | take 1').syntax,
        lambda n: str(n.Kind) == "DataTableExpression",
    )
    expected = [("a", "int"), ("b", "string")]
    assert read_row_schema(owner.Schema) == expected
    assert read_row_schema(owner) == expected

    (kv,) = collect_nodes(
        parse("T | parse-kv Msg as (a:long)").syntax,
        lambda n: str(n.Kind) == "ParseKvOperator",
    )
    assert read_row_schema(kv.Keys) == [("a", "long")]
    assert read_row_schema(kv) == [("a", "long")]


def test_func_call_name_reads_agree_across_positions():
    """One reader, two positions: the expression path resolves through the
    binder (safe, since both bind states start from ``GlobalState.Default``)
    and the source path stays syntactic. Pinned so neither can drift."""
    ir = parse("materialized_view('MV') | where tostring(a) == 'x'").to_ir()
    (src,) = find_all(ir, FuncCallSource)
    assert src.name == "materialized_view"
    names = {f.name for f in find_all(ir, FuncCall)}
    assert "tostring" in names
