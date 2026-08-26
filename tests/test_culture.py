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

These tests pass on an en-US machine even without the pin. Run them under
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
    """Integer literals contain no decimal separator for a culture to corrupt."""
    assert _single_timespan_ticks(f"T | where X > {literal}") == expected_ticks


@pytest.mark.parametrize("literal,expected", REAL_CASES)
def test_fractional_real_literal_is_culture_independent(literal, expected):
    """`real` corrupts exactly like `timespan` — `1.5` reads back as `15.0`.

    Pinned separately because the defect is easy to read as a duration-only
    problem, which would let a consumer writing ``| where CpuPct > 1.5``
    conclude they are unaffected.
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


# The locale CI matrix (LANG=de_DE.UTF-8 / fr_FR.UTF-8) is what proves the pin
# is load-bearing. It is only meaningful if .NET actually has data for those
# cultures — and .NET fails soft here in two different ways. In
# globalization-invariant mode (DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1, or an
# image with no ICU) constructing the culture raises; for a name it merely
# lacks data for, it silently returns invariant separators. Either way every
# fractional literal parses correctly, the locale job goes green, and it has
# tested nothing.
#
# Across every culture .NET exposes, the outcome for a fractional literal is
# decided entirely by the decimal separator: '.' parses correctly, ',' or '٫'
# corrupts. Group separator never matters — a KQL literal contains none. So the
# two matrix cultures are one representative per failure mode, and asserting
# their decimal separator is exactly the check that they still exercise it.
CORRUPTING_MATRIX_CULTURES = [
    ("de-DE", ",", "."),      # digits concatenate: 1.5h -> 15:00:00
    # NARROW NO-BREAK SPACE (U+202F), written as an escape on purpose:
    # as a literal it is invisible and reads like a plain space.
    ("fr-FR", ",", "\u202f"),  # parse fails outright: 1.5h -> 00:00:00
]


@pytest.mark.parametrize("name,decimal_sep,group_sep", CORRUPTING_MATRIX_CULTURES)
def test_ci_matrix_culture_still_corrupts_fractional_literals(name, decimal_sep, group_sep):
    """Guard the guard: the locale matrix must not be able to pass vacuously.

    Asserts on the culture constructed by name, not on the ambient one, so this
    holds under the import-time pin and in every job — not only the locale ones.
    """
    from System.Globalization import CultureInfo, CultureNotFoundException

    try:
        number_format = CultureInfo(name).NumberFormat
    except CultureNotFoundException as e:  # globalization-invariant mode / no ICU
        pytest.fail(
            f"{name} is unavailable to .NET ({e.__class__.__name__}), so the "
            f"LANG={name} CI job cannot exercise the culture bug and would pass "
            f"vacuously. Install ICU data / unset "
            f"DOTNET_SYSTEM_GLOBALIZATION_INVARIANT."
        )

    assert number_format.NumberDecimalSeparator == decimal_sep, (
        f"{name} reports decimal separator "
        f"{number_format.NumberDecimalSeparator!r}, not {decimal_sep!r} — .NET has "
        f"no real data for it, so fractional literals parse correctly and the "
        f"LANG={name} CI job proves nothing."
    )
    assert number_format.NumberGroupSeparator == group_sep
