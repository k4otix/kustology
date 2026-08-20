# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Fractional numeric literals must parse identically under every locale.

Microsoft's parser reads ``LiteralExpression.LiteralValue`` lazily, using the
culture live at property-access time. Under a comma-decimal locale the
decimal point is read as a group separator, so the fractional part is
swallowed and the digits are concatenated:

* ``timespan`` — ``1.5h`` yields fifteen hours, ``2.25s`` three minutes
  forty-five; under ``fr-FR`` the parse fails to zero.
* ``real`` — ``1.5`` yields ``15.0``. A predicate like
  ``| where CpuPct > 1.5`` is silently ten times too strict.
* ``decimal`` — ``decimal(1.5)`` yields ``15``.

Durations are the loudest case, not the only one: every fractional numeric
literal kind is affected the same way. The bridge pins InvariantCulture at
import to close all of them.

These tests pass on an en-US machine even without the fix. Run them under
``LANG=de-DE`` to see them fail.

They cover the pin as it stands after import. They do *not* — and cannot —
cover a host that reassigns ``CultureInfo.DefaultThreadCurrentCulture``
*after* importing kustology: ``LiteralValue`` is lazy, so that reopens the
corruption for every literal read afterwards, and no code at this layer can
prevent it. See ``bridge._pin_invariant_culture``.
"""

import pytest

from kustology import parse
from kustology.utils.analysis import collect_nodes

TICKS_PER_SECOND = 10_000_000

FRACTIONAL_CASES = [
    ("1.5h", 54_000_000_000),
    ("0.5h", 18_000_000_000),
    ("2.25s", 22_500_000),
    ("1.5d", 1_296_000_000_000),
]

REAL_CASES = [
    ("1.5", 1.5),
    ("0.25", 0.25),
    ("1.125", 1.125),
]

DECIMAL_CASES = [
    ("1.5", "1.5"),
    ("2.25", "2.25"),
]


def _single_literal(query: str, net_kind: str):
    nodes = collect_nodes(parse(query).syntax, lambda n: str(n.Kind) == net_kind)
    assert len(nodes) == 1, f"expected one {net_kind} in {query!r}"
    return nodes[0].LiteralValue


def _single_timespan_ticks(query: str) -> int:
    return _single_literal(query, "TimespanLiteralExpression").Ticks


def test_bridge_pins_invariant_culture():
    """The pin is in effect after importing kustology, on any host locale."""
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    assert CultureInfo.InvariantCulture.Name == ""
    assert Thread.CurrentThread.CurrentCulture.Name == ""
    assert CultureInfo.DefaultThreadCurrentCulture is not None
    assert CultureInfo.DefaultThreadCurrentCulture.Name == ""


@pytest.mark.parametrize("literal,expected_ticks", FRACTIONAL_CASES)
def test_fractional_timespan_literal_is_culture_independent(literal, expected_ticks):
    assert _single_timespan_ticks(f"T | where X > {literal}") == expected_ticks


@pytest.mark.parametrize("literal,expected_ticks", [("15m", 9_000_000_000), ("1h", 36_000_000_000)])
def test_integer_timespan_literal_unaffected(literal, expected_ticks):
    """Integer literals were always correct — guard against regressing them."""
    assert _single_timespan_ticks(f"T | where X > {literal}") == expected_ticks


@pytest.mark.parametrize("literal,expected", REAL_CASES)
def test_fractional_real_literal_is_culture_independent(literal, expected):
    """`real` corrupts exactly like `timespan` — `1.5` reads back as `15.0`.

    Documented separately because the README and CHANGELOG framed the defect
    as a *duration* problem, which would let a consumer writing
    ``| where CpuPct > 1.5`` conclude they were unaffected.
    """
    value = _single_literal(f"T | where CpuPct > {literal}", "RealLiteralExpression")
    assert value == expected


@pytest.mark.parametrize("literal,expected", DECIMAL_CASES)
def test_fractional_decimal_literal_is_culture_independent(literal, expected):
    """`decimal` corrupts the same way.

    Rendered through InvariantCulture so this asserts on the *parsed* value
    rather than on how the ambient culture happens to format it — the two
    failure modes are distinct and only the former is a data corruption.
    """
    from System.Globalization import CultureInfo

    value = _single_literal(
        f"T | where D > decimal({literal})", "DecimalLiteralExpression",
    )
    assert value.ToString(None, CultureInfo.InvariantCulture) == expected


def test_pin_survives_a_thread_created_after_import():
    """DefaultThreadCurrentCulture must cover threads spawned later."""
    import threading

    result = {}

    def worker():
        result["ticks"] = _single_timespan_ticks("T | where X > 1.5h")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result["ticks"] == 54_000_000_000
