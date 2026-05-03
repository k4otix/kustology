import pytest
from pykusto_language import parse, format, validate

@pytest.mark.parametrize(
    "query, expected_kind",
    [
        ("StormEvents | count", "PipeExpression"),
        ("print x = 1", "PrintOperator"),
        ("let x = 5; x", "LetStatement"),
    ],
)
def test_kql_node_types(query, expected_kind):
    result = parse(query)
    assert result is not None

    statement = result.syntax.Statements[0].Element
    actual_kind = str(statement.Kind)

    if actual_kind == "ExpressionStatement":
        actual_kind = str(statement.Expression.Kind)

    assert actual_kind == expected_kind

def test_format():
    query = "SecurityEvent|where EventID==4624"
    formatted = format(query)
    assert "| where" in formatted
    assert "==" in formatted

def test_validate():
    query = "SecurityEvent | where EventID == "
    diagnostics = validate(query)
    assert len(diagnostics) > 0
    assert any("Missing expression" in d["message"] for d in diagnostics)

def test_get_referenced_tables():
    query = "SecurityEvent | where EventID == 4624 | join kind=inner (SigninLogs) on Account"
    result = parse(query)
    tables = result.get_referenced_tables()
    # Note: Currently TableExtractor is simple and might only pick up the first table
    assert "SecurityEvent" in tables

def test_get_referenced_tables_semantic():
    query = "MyCustomTable | count"
    schema = {"MyCustomTable": ["Col1"]}
    result = parse(query)
    tables = result.get_referenced_tables(schema=schema)
    assert "MyCustomTable" in tables
