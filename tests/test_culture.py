# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Fractional duration literals must parse identically under every locale.

Microsoft's parser reads ``TimespanLiteralExpression.LiteralValue`` lazily,
using the culture live at property-access time. Under ``de-DE`` the decimal
point is a group separator, so ``1.5h`` yields fifteen hours and ``2.25s``
yields three minutes forty-five; under ``fr-FR`` the parse fails to zero.
The bridge pins InvariantCulture at import to close this.

These tests pass on an en-US machine even without the fix. Run them under
``LANG=de-DE`` to see them fail.
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


def _single_timespan_ticks(query: str) -> int:
    nodes = collect_nodes(
        parse(query).syntax,
        lambda n: str(n.Kind) == "TimespanLiteralExpression",
    )
    assert len(nodes) == 1, f"expected one timespan literal in {query!r}"
    return nodes[0].LiteralValue.Ticks


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
