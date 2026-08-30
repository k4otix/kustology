# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Leading comments must not corrupt Tier 1 syntactic analysis.

Microsoft's ``SyntaxNode.ToString()`` includes leading trivia, comments among
it, so an analyzer that reads a node name that way picks up any comment
sitting immediately before a table, column, or function name. Real Sentinel
detection rules are full of leading comments, so that corruption lands on the
queries this library exists to analyse.

``replace_table`` is the tricky case: the name is read without trivia while
the replacement span (``TextStart``/``Width``, still offset-based) covers only
the identifier, so the leading comment survives in the output. A newline or
``//`` inside an extracted table name is the signature of trivia leaking,
which is what the sweep over the Sentinel-derived corpus looks for.
"""

from pathlib import Path

import pytest

from kustology import parse

CASES = [
    (
        "SecurityEvent | where EventID == 4625 | project Account",
        "// lead\nSecurityEvent\n| where\n  // only failed\n  EventID == 4625\n| project\n// c\nAccount",
    ),
    (
        "T | join (U) on a | union V",
        "T | join (\n// rhs\nU) on a | union\n // first\n V",
    ),
    (
        "T | where t > ago(1d) and x == 1h",
        "T | where t > // c\nago(1d) and x == // c2\n1h",
    ),
]


@pytest.mark.parametrize("plain,commented", CASES)
def test_analyzers_ignore_comments(plain, commented):
    a, b = parse(plain), parse(commented)
    assert a.get_referenced_tables() == b.get_referenced_tables()
    assert a.get_referenced_columns() == b.get_referenced_columns()
    assert a.get_referenced_functions() == b.get_referenced_functions()
    assert [t for t, *_ in a.find_time_expressions()] == [
        t for t, *_ in b.find_time_expressions()
    ]


def test_replace_table_after_leading_comment():
    q = "// a comment\nSecurityEvent | take 1"
    assert parse(q).replace_table("SecurityEvent", "X") == "// a comment\nX | take 1"


def test_fixture_tables_are_identifiers():
    fixtures = list(Path("tests/fixtures/complex_queries").glob("*.kql"))
    assert fixtures, "expected the Sentinel-derived fixture corpus to be non-empty"
    for f in fixtures:
        for t in parse(f.read_text()).get_referenced_tables():
            assert "\n" not in t and "//" not in t, (f.name, t)
