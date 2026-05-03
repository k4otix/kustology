import pytest
from pykusto_language import parse

def test_get_operator_chain():
    query = "SecurityEvent | where EventID == 4624 | count"
    result = parse(query)
    chain = result.get_operator_chain()
    # Expect: [NameReference(SecurityEvent), FilterOperator, CountOperator]
    assert len(chain) == 3
    assert "SecurityEvent" in str(chain[0])
    assert "FilterOperator" in str(chain[1].Kind)
    assert "CountOperator" in str(chain[2].Kind)

def test_to_dict():
    query = "T | count"
    result = parse(query)
    data = result.to_dict()
    assert data["kind"] == "QueryBlock"
    assert len(data["children"]) > 0

def test_referenced_columns():
    query = "SecurityEvent | where EventID == 4624 | project TimeGenerated, Account"
    result = parse(query)
    cols = result.get_referenced_columns()
    assert "EventID" in cols
    assert "TimeGenerated" in cols
    assert "Account" in cols
    assert "SecurityEvent" not in cols

def test_structural_hash():
    q1 = "T | where x == 1"
    q2 = "T | where x == 5"
    q3 = "T | project x"
    
    h1 = parse(q1).get_structural_hash()
    h2 = parse(q2).get_structural_hash()
    h3 = parse(q3).get_structural_hash()
    
    assert h1 == h2
    assert h1 != h3

def test_get_time_range():
    query = "T | where TimeGenerated > ago(1h) and TimeGenerated < datetime(2023-01-01)"
    result = parse(query)
    times = result.get_time_range()
    assert any("ago(1h)" in t for t in times)
    assert any("2023-01-01" in t for t in times)

def test_replace_table():
    query = "SecurityEvent | where x == 1 | join (OtherTable) on x"
    result = parse(query)
    new_query = result.replace_table("SecurityEvent", "NewTable")
    assert "NewTable | where" in new_query
    assert "OtherTable" in new_query
    assert "SecurityEvent" not in new_query
