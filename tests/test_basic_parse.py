import pytest

from pykusto_language.parser import parse_query


@pytest.mark.parametrize(
    "query, expected_kind",
    [
        ("StormEvents | count", "PipeExpression"),
        ("print x = 1", "PrintOperator"),  # Changed from PrintStatement
        ("let x = 5; x", "LetStatement"),
    ],
)
def test_kql_node_types(query, expected_kind):
    result = parse_query(query)
    assert result is not None

    statement = result.Syntax.Statements[0].Element

    # We want to find the 'expected_kind' regardless of whether
    # it's the Statement itself or the Expression inside it.
    actual_kind = str(statement.Kind)

    if actual_kind == "ExpressionStatement":
        actual_kind = str(statement.Expression.Kind)

    assert actual_kind == expected_kind
