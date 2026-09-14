# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Coverage for the four kinds of source ``find_source_references`` reports.

A table is one of them. The other three are a function call, an
``externaldata`` literal, and a ``datatable`` literal. Each stands where a
table name stands and is invisible to ``find_table_references``.
"""

from kustology import SourceRef, parse

WATCHLIST = (
    'let allowed =\n'
    '    _GetWatchlist("AllowedRanges")\n'
    '    | summarize make_set(IPAddress);\n'
    'SignInEvents\n'
    '| where TimeGenerated > ago(1h)\n'
    '| where IPAddress !in (allowed)'
)

EXTERNAL = (
    'externaldata(Ip:string)["https://contoso.example/ranges.csv"]\n'
    '| where Ip != ""'
)

DATATABLE = (
    'datatable(Sev:string, Score:long)["high", 9, "low", 1]\n'
    '| where Score > 5'
)

GRAPH = "Edges | make-graph src --> dst with Nodes on n"

GRAPH_SCHEMA = {
    "Edges": {"src": "string", "dst": "string"},
    "Nodes": {"n": "string"},
}


def test_function_and_table_sources_in_source_order():
    """A let-bound pipeline reading a function reports the call, then the table."""
    refs = parse(WATCHLIST).find_source_references()
    assert [(r.kind, r.name) for r in refs] == [
        ("function", "_GetWatchlist"),
        ("table", "SignInEvents"),
    ]
    assert refs[0].span.text(WATCHLIST) == '_GetWatchlist("AllowedRanges")'
    assert refs[1].span.text(WATCHLIST) == "SignInEvents"


def test_one_entry_per_occurrence_with_no_dedupe_by_name():
    """Two calls to one function are two reads, each carrying its own span."""
    query = 'union _Fn("a"), _Fn("a")'
    refs = parse(query).find_source_references()
    assert [(r.kind, r.name) for r in refs] == [
        ("function", "_Fn"),
        ("function", "_Fn"),
    ]
    assert refs[0].span != refs[1].span


def test_externaldata_source_is_anonymous_and_spans_the_whole_construct():
    refs = parse(EXTERNAL).find_source_references()
    assert [(r.kind, r.name) for r in refs] == [("externaldata", None)]
    assert refs[0].span.text(EXTERNAL) == (
        'externaldata(Ip:string)["https://contoso.example/ranges.csv"]'
    )


def test_datatable_source_is_anonymous_and_spans_the_whole_construct():
    refs = parse(DATATABLE).find_source_references()
    assert [(r.kind, r.name) for r in refs] == [("datatable", None)]
    assert refs[0].span.text(DATATABLE) == (
        'datatable(Sev:string, Score:long)["high", 9, "low", 1]'
    )


def test_anonymous_kinds_are_syntactic_on_a_bound_parse():
    """Only tables follow the binder; the other three kinds read the same either way."""
    unbound = parse(DATATABLE).find_source_references()
    bound = parse(DATATABLE, schema={"Unrelated": {"a": "long"}})
    assert bound._code.HasSemantics
    assert bound.find_source_references() == unbound
    assert [(r.kind, r.name) for r in unbound] == [("datatable", None)]


def test_anonymous_source_inside_a_union_operand():
    """A parenthesized ``datatable`` operand of ``union`` is still a source."""
    query = 'union Alerts, (datatable(Sev:string)["high"])'
    refs = parse(query).find_source_references()
    assert [(r.kind, r.name) for r in refs] == [
        ("table", "Alerts"),
        ("datatable", None),
    ]
    assert refs[1].span.text(query) == 'datatable(Sev:string)["high"]'


def test_let_bound_function_call_is_not_a_source():
    """``f`` is declared by the query, so only the table it reads is a source."""
    query = "let f = (){ T | count }; f() | count"
    assert [(r.kind, r.name) for r in parse(query).find_source_references()] == [
        ("table", "T")
    ]


def test_make_graph_node_table_needs_a_bound_parse():
    """Tables inherit ``find_table_references``'s bind-state split."""
    unbound = parse(GRAPH).find_source_references()
    assert [(r.kind, r.name) for r in unbound] == [("table", "Edges")]

    bound = parse(GRAPH, schema=GRAPH_SCHEMA)
    assert [(r.kind, r.name) for r in bound.find_source_references()] == [
        ("table", "Edges"),
        ("table", "Nodes"),
    ]
    assert [
        (r.kind, r.name)
        for r in bound.find_source_references(force_syntactic=True)
    ] == [("table", "Edges")]


def test_spans_are_code_point_offsets():
    """An astral character before the source must not shift the reported span."""
    query = 'let e = "\U0001f600";\n_GetWatchlist("Ranges")\n| take 1'
    refs = parse(query).find_source_references()
    assert [(r.kind, r.name) for r in refs] == [("function", "_GetWatchlist")]
    assert refs[0].span.text(query) == '_GetWatchlist("Ranges")'


def test_a_scalar_call_is_a_function_but_not_a_source():
    """``get_referenced_functions`` answers a different question."""
    query = "T | where a > ago(1h)"
    q = parse(query)
    assert q.get_referenced_functions() == {"ago"}
    assert [(r.kind, r.name) for r in q.find_source_references()] == [("table", "T")]


def test_module_level_find_source_references():
    """The module function reaches a path expression's table without its qualifier."""
    from kustology.utils.analysis import find_source_references

    query = 'database("d").Audit | join (_Enrich("x")) on id'
    refs = find_source_references(parse(query)._code)
    assert [(r.kind, r.name) for r in refs] == [
        ("table", "Audit"),
        ("function", "_Enrich"),
    ]
    assert refs[0].span.text(query) == "Audit"
    assert isinstance(refs[0], SourceRef)
