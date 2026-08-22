# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Literal kinds come from the .NET node; values are culture-independent."""

import os
import subprocess
import sys

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
        "2024-01-01T00:00:00.0000000Z",
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


def test_adjacent_string_literals_concatenate_into_one_literal():
    """KQL joins adjacent string literals the way C does: ``'a' 'b'`` is the
    single value ``"ab"``.

    The parser hands that over as a ``CompoundStringLiteralExpression`` with
    ``LiteralValue == "ab"`` already computed. The builder had no branch for
    the kind, so the whole right-hand side fell through to ``UnknownExpr``
    carrying the raw text ``"'a' 'b'"`` -- a filter comparing against a
    string became a filter comparing against an unmodelled blob, invisible
    to ``find_all(ir, LiteralExpr)`` and hashing apart from the identical
    query written ``'ab'``.
    """
    from kustology.ir import UnknownExpr

    lit = _first_literal("T | where x == 'a' 'b'")
    assert lit.literal_kind == "string"
    assert lit.value == "ab"
    assert lit.ticks is None

    ir = parse("T | where x == 'a' 'b'").to_ir()
    assert not list(find_all(ir, UnknownExpr))
    # The whole point of the concatenation: it means what the joined
    # spelling means, so it must hash there too.
    assert ir.semantic_hash == parse("T | where x == 'ab'").to_ir().semantic_hash


def test_a_compound_string_literal_renders_as_its_joined_value():
    lit = _first_literal('T | where x == "a" "b" "c"')
    assert lit.value == "abc"
    assert lit.canonical_form == '"abc"'


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
    # Kind-normalized to UTC (see test_datetime_literal_is_utc_and_tz_independent),
    # so the "o" round-trip format carries the "Z" suffix.
    assert lit.value == "2024-03-05T13:45:00.0000000Z"
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


def test_datetime_literal_is_utc_and_tz_independent():
    """A ``Z``-suffixed datetime literal parses through .NET's default
    ``DateTime.Parse`` as ``DateTimeKind.Local`` -- its ``Ticks`` already
    carry the *host's* UTC offset baked in, not the UTC instant the query
    text names. Rendering ``.Ticks``/``.ToString()`` straight off that value
    (the pre-fix behaviour) makes ``value``, ``ticks``, and therefore
    ``semantic_hash`` depend on the timezone of whatever machine parsed the
    query. Re-running the same parse in a subprocess pinned to a different
    timezone (``Asia/Tokyo``) proves the fixed hash does not move for either
    the ``Local``-kind (``Z``-suffixed) literal or the ``Unspecified``-kind
    (bare) one -- covering both branches separately matters because
    ``ToUniversalTime()`` is a no-op on an ``Unspecified`` value when the
    host zone already happens to be UTC, so a regression in that branch
    alone could still pass a same-zone comparison."""
    import System

    q = "T | where d > datetime(2024-01-01T00:00:00Z)"
    naive_q = "T | where d > datetime(2024-01-01)"
    lit = next(l for l in find_all(parse(q).to_ir(), LiteralExpr) if l.literal_kind == "datetime")
    # Non-vacuous even on a host whose CoreCLR ignores TZ entirely: this is
    # an absolute value the unfixed (pre-Kind-normalization) code could
    # never have produced on any host, since it always rendered whatever
    # Kind/Ticks LiteralValue happened to hand back unconverted.
    assert lit.value == "2024-01-01T00:00:00.0000000Z" and lit.ticks == 638396640000000000
    here = parse(q).to_ir().semantic_hash
    naive_here = parse(naive_q).to_ir().semantic_hash
    parent_tz = System.TimeZoneInfo.Local.Id

    child = subprocess.run(
        [sys.executable, "-c", (
            "from kustology import parse\n"
            "import System\n"
            "print(System.TimeZoneInfo.Local.Id)\n"
            f"print(parse({q!r}).to_ir().semantic_hash)\n"
            f"print(parse({naive_q!r}).to_ir().semantic_hash)\n"
        )],
        env={**os.environ, "TZ": "Asia/Tokyo"}, capture_output=True, text=True, check=False,
    )
    assert child.returncode == 0, f"subprocess failed (exit {child.returncode}):\n{child.stderr}"
    child_tz, other, naive_other = child.stdout.strip().splitlines()

    # Prove the child really did run in a different zone -- on a platform
    # where CoreCLR ignores TZ (e.g. Windows, which reads the OS zone
    # instead), the comparisons below would otherwise silently degrade into
    # a same-config comparison that cannot catch a regression.
    assert child_tz != parent_tz, (
        f"TZ=Asia/Tokyo did not change System.TimeZoneInfo.Local.Id "
        f"(stayed {child_tz!r} in both processes) -- this platform's "
        f"CoreCLR does not honor TZ, so this test cannot exercise a "
        f"different timezone and is not meaningful evidence here"
    )
    assert here == other
    assert naive_here == naive_other


def test_naive_and_zulu_datetime_hash_equal():
    """KQL datetimes are UTC by definition, so a bare (``Unspecified``-kind)
    literal and its explicit ``Z``-suffixed spelling of the same instant
    must collapse to the same hash -- one is *specified* as UTC, the other
    is *converted* to it, and both land on the same ticks and ISO string."""
    assert (
        parse("T | where d > datetime(2024-01-01)").to_ir().semantic_hash
        == parse("T | where d > datetime(2024-01-01T00:00:00Z)").to_ir().semantic_hash
    )
