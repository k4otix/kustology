# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import kustology
from kustology import TextSpan, TimeExpr


def test_find_time_expressions_returns_named_tuples():
    q = "T | where TimeGenerated > ago(1h)"
    first = kustology.parse(q).find_time_expressions()[0]
    assert isinstance(first, TimeExpr)
    assert first == ("ago(1h)", 26, 7)  # positional contract unchanged
    assert (first.text, first.start, first.length) == ("ago(1h)", 26, 7)
    assert first.span == TextSpan(26, 7)
    assert first.span.text(q) == "ago(1h)"


def test_textspan_end_and_text():
    span = TextSpan(4, 5)
    assert span.end == 9
    assert span.text("T | where x") == "where"


def test_time_expr_offsets_are_code_points():
    q = "T | where a == '😀' and TimeGenerated > ago(1h)"
    (expr,) = kustology.parse(q).find_time_expressions()
    assert expr.span.text(q) == "ago(1h)"
