# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from kustology import parse, format_query


def test_get_operator_chain():
    query = "SecurityEvent | where EventID == 4624 | count"
    result = parse(query)
    chain = result.get_operator_chain()
    assert len(chain) == 3
    assert "SecurityEvent" in str(chain[0])
    assert "FilterOperator" in str(chain[1].Kind)
    assert "CountOperator" in str(chain[2].Kind)


def test_to_dict_basic():
    data = parse("T | count").to_dict()
    assert data["kind"] == "QueryBlock"
    assert len(data["children"]) > 0


def test_to_dict_pipe_expression_descends():
    """Non-trivial query: confirm Project / Filter operators appear in the dict."""
    data = parse("T | where x == 1 | project x, y").to_dict()
    flat = []

    def walk(d):
        flat.append(d["kind"])
        for c in d["children"]:
            walk(c)

    walk(data)
    assert "FilterOperator" in flat
    assert "ProjectOperator" in flat
    assert flat.count("PipeExpression") >= 1


def test_referenced_columns_returns_columns_not_table_names():
    query = "SecurityEvent | where EventID == 4624 | project TimeGenerated, Account"
    cols = parse(query).get_referenced_columns()
    assert {"EventID", "TimeGenerated", "Account"}.issubset(cols)
    assert "SecurityEvent" not in cols


def test_referenced_columns_excludes_function_names():
    """Function callees like strcat/ago/bin must not be reported as columns."""
    query = "T | extend a = strcat(x, y) | project a, ago(1h), bin(TimeGenerated, 5m)"
    cols = parse(query).get_referenced_columns()
    assert {"strcat", "ago", "bin"}.isdisjoint(cols)
    assert {"x", "y"}.issubset(cols)


def test_referenced_columns_excludes_dollar_join_refs():
    """`$left` and `$right` are KQL join-side references, not columns."""
    query = "A | join (B) on $left.x == $right.y"
    cols = parse(query).get_referenced_columns()
    assert {"$left", "$right"}.isdisjoint(cols)
    assert {"x", "y"}.issubset(cols)

    schema = {"A": {"x": "string"}, "B": {"y": "string"}}
    sem_cols = parse(query, schema=schema).get_referenced_columns()
    assert {"$left", "$right"}.isdisjoint(sem_cols)
    assert {"x", "y"}.issubset(sem_cols)


def test_referenced_columns_semantic_resolves_aliases():
    """Semantic mode includes both columns and extend-aliases as ColumnSymbols."""
    schema = {"T": {"x": "long", "y": "long"}}
    cols = parse("T | extend a = x + y | project a, x", schema=schema).get_referenced_columns()
    assert {"x", "y", "a"}.issubset(cols)


def test_structural_hash_ignores_literal_values():
    h1 = parse("T | where x == 1").get_structural_hash()
    h2 = parse("T | where x == 5").get_structural_hash()
    h3 = parse("T | project x").get_structural_hash()
    assert h1 == h2
    assert h1 != h3


def test_get_time_range_returns_tuples_in_source_order():
    query = "T | where TimeGenerated > ago(1h) and TimeGenerated < datetime(2023-01-01)"
    times = parse(query).get_time_range()
    assert all(isinstance(t, tuple) and len(t) == 3 for t in times)
    starts = [start for _, start, _ in times]
    assert starts == sorted(starts)
    texts = [text for text, _, _ in times]
    assert any("ago(1h)" in t for t in texts)
    assert any("2023-01-01" in t for t in texts)


def test_get_time_range_ignores_string_literal_text():
    """Substring 'ago(' embedded in a string literal must not match."""
    query = "T | where Note == 'this query uses ago()' | count"
    assert parse(query).get_time_range() == []


def test_get_time_range_does_not_double_count_nested_literals():
    """ago(1h) reports the call once; the inner 1h timespan is suppressed."""
    times = parse("T | where t > ago(1h)").get_time_range()
    texts = [text for text, _, _ in times]
    assert texts == ["ago(1h)"]


def test_format_query_round_trip():
    queries = [
        "T | count",
        "SecurityEvent|where EventID==4624|project Account",
        "let x = 5; T | where y == x",
        "T | join kind=inner (U) on Id",
        "union A, B | summarize count() by Type",
    ]
    for q in queries:
        once = format_query(q)
        twice = format_query(once)
        assert once == twice, f"format_query is not idempotent for: {q!r}"


def test_format_query_accepts_options():
    """The optional FormattingOptions argument is part of the public surface."""
    from Kusto.Language.Editor import PlacementStyle

    from kustology.bridge import FormattingOptions

    smart = FormattingOptions.Default.WithPipeOperatorStyle(PlacementStyle.Smart)
    newline = FormattingOptions.Default.WithPipeOperatorStyle(PlacementStyle.NewLine)
    smart_out = format_query("T|count", options=smart)
    newline_out = format_query("T|count", options=newline)
    assert smart_out == "T | count"
    assert newline_out == "T\n| count"


def test_repr_reports_op_count_and_binding():
    syntactic = parse("T | where x == 1 | count")
    rep = repr(syntactic)
    assert "ops" in rep
    assert "has_semantics=False" in rep

    bound = parse("T | count", schema={"T": {"x": "long"}})
    assert "has_semantics=True" in repr(bound)
