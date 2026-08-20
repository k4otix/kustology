# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Literal kinds come from the .NET node; values are culture-independent."""

import pytest

from kustology import parse
from kustology.ir import LiteralExpr, find_all

# (query, expected literal_kind, expected value, expected ticks)
LITERAL_CASES = [
    ('T | where S == "abc"', "string", "abc", None),
    ("T | where B == true", "bool", True, None),
    ("T | where N == 42", "long", 42, None),
    ("T | where N == int(5)", "int", 5, None),
    ("T | where R == 1.5", "real", 1.5, None),
    ("T | where R == decimal(1.5)", "decimal", "1.5", None),
    (
        "T | where D == datetime(2024-01-01)",
        "datetime",
        "2024-01-01T00:00:00.0000000",
        638396640000000000,
    ),
    ("T | where W == 15m", "timespan", "00:15:00", 9_000_000_000),
    ("T | where W == 1.5h", "timespan", "01:30:00", 54_000_000_000),
    ("T | where W == 2tick", "timespan", "00:00:00.0000002", 2),
    (
        "T | where G == guid(74be27de-1e4e-49d9-b579-fe0b331d3642)",
        "guid",
        "74be27de-1e4e-49d9-b579-fe0b331d3642",
        None,
    ),
    ("T | where X == int(null)", "null", None, None),
]


def _first_literal(query: str) -> LiteralExpr:
    literals = list(find_all(parse(query).to_ir(), LiteralExpr))
    assert literals, f"no literal in {query!r}"
    return literals[0]


@pytest.mark.parametrize("query,kind,value,ticks", LITERAL_CASES)
def test_literal_kind_value_and_ticks(query, kind, value, ticks):
    lit = _first_literal(query)
    assert lit.literal_kind == kind
    assert lit.value == value
    assert lit.ticks == ticks


def test_dynamic_literal_still_carries_its_json_body():
    lit = _first_literal('T | where D == dynamic({"a":1})')
    assert lit.literal_kind == "dynamic"
    assert lit.value == '{"a":1}'
    assert lit.ticks is None


def test_ticks_reconstruct_an_exact_timedelta():
    """Ticks / 10 -> microseconds is exact; TotalSeconds would not be."""
    from datetime import timedelta

    lit = _first_literal("T | where W == 1microsecond")
    assert lit.ticks == 10
    assert timedelta(microseconds=lit.ticks // 10) == timedelta(microseconds=1)


def test_datetime_value_is_iso_8601_not_culture_formatted():
    lit = _first_literal("T | where D == datetime(2024-03-05 13:45:00)")
    assert lit.value == "2024-03-05T13:45:00.0000000"
    # The old culture-formatted output contained a U+202F narrow no-break space
    # under en-US. Nothing ISO-formatted ever should.
    assert " " not in lit.value


def test_ticks_is_absent_from_the_llm_view():
    """The LLM reads `value`; tick counts are noise for it."""
    ir = parse("T | where W == 15m").to_ir()
    assert "ticks" not in repr(ir.to_llm_dict())


def test_semantic_hash_is_stable_for_temporal_literals():
    """Two builds of the same query agree — the hash no longer depends on
    how .NET happens to render a DateTime for the host locale."""
    q = "T | where D > datetime(2024-01-01) and W > 1.5h"
    assert parse(q).to_ir().semantic_hash == parse(q).to_ir().semantic_hash
