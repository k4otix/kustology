# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Entry points restore invariant culture when a host has assigned over it.

``tests/test_culture.py`` covers the import-time pin. Here, a host or another
.NET-interop library in the same process assigns
``Thread.CurrentThread.CurrentCulture``, and every ``LiteralValue`` read after
that parses under the new culture. ``bridge.ensure_invariant_culture`` repairs
the thread, and every kustology entry point calls it.

Culture is process-global, so a test that sets it and dies leaves every later
test on a comma-decimal locale. The ``hostile_culture`` fixture restores
invariant culture in teardown unconditionally.

Each test names the culture it switches to, since a machine already running
``de-DE`` would pass a test that forgot to switch.
"""

import pytest

from kustology import parse
from kustology.bridge import ensure_invariant_culture
from kustology.utils.analysis import collect_nodes

TICKS_PER_HOUR = 36_000_000_000


@pytest.fixture
def hostile_culture():
    """Assign a comma-decimal culture over the pin, and undo it afterwards.

    Teardown assigns ``InvariantCulture`` directly. Cleaning up through
    ``ensure_invariant_culture`` would leak the hostile culture into every
    later test the moment that code regresses.
    """
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    def _set(name: str):
        culture = CultureInfo(name)
        Thread.CurrentThread.CurrentCulture = culture
        CultureInfo.DefaultThreadCurrentCulture = culture
        assert Thread.CurrentThread.CurrentCulture.Name == name
        return culture

    try:
        yield _set
    finally:
        Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture


def _timespan_ticks(query) -> int:
    literals = collect_nodes(
        query.syntax,
        lambda n: str(n.Kind) == "TimespanLiteralExpression",
    )
    assert len(literals) == 1
    return literals[0].LiteralValue.Ticks


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_parse_restores_culture_before_the_tree_can_be_read(hostile_culture, locale):
    """``parse`` repairs the thread, so a caller's later literal read is correct."""
    hostile_culture(locale)

    query = parse("T | where ts > ago(1.5h)")

    assert _timespan_ticks(query) == int(1.5 * TICKS_PER_HOUR)


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_to_ir_restores_culture_before_the_builder_reads_literals(
    hostile_culture, locale
):
    """A parse taken under the pin, lowered after a host switched culture."""
    pytest.importorskip("pydantic")

    query = parse("T | where ts > ago(1.5h)")
    hostile_culture(locale)

    ir = query.to_ir()

    literals = [
        node
        for node in _walk_ir(ir)
        if getattr(node, "literal_kind", None) == "timespan"
    ]
    assert len(literals) == 1
    assert literals[0].ticks == int(1.5 * TICKS_PER_HOUR)


def _walk_ir(ir):
    from kustology.ir import walk

    return walk(ir)


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_validate_restores_culture(hostile_culture, locale):
    """``validate`` is an entry point too, so it repairs the thread as well."""
    from System.Threading import Thread

    from kustology import validate

    hostile_culture(locale)
    validate("T | where ts > ago(1.5h)")

    assert Thread.CurrentThread.CurrentCulture.Name == ""


@pytest.mark.parametrize("locale", ["de-DE", "fr-FR"])
def test_format_query_restores_culture(hostile_culture, locale):
    """``format_query`` is an entry point too."""
    from System.Threading import Thread

    from kustology import format_query

    hostile_culture(locale)
    format_query("T|where ts>ago(1.5h)")

    assert Thread.CurrentThread.CurrentCulture.Name == ""


def test_the_hostile_culture_fixture_really_corrupts_literals(hostile_culture):
    """Prove the fixture bites. Every test above would pass anyway if
    ``hostile_culture`` silently changed nothing."""
    hostile_culture("de-DE")

    from Kusto.Language import KustoCode

    code = KustoCode.Parse("T | where ts > ago(1.5h)")
    literals = collect_nodes(
        code.Syntax,
        lambda n: str(n.Kind) == "TimespanLiteralExpression",
    )
    assert literals[0].LiteralValue.Ticks == 15 * TICKS_PER_HOUR


def test_ensure_invariant_culture_is_idempotent():
    """Calling it on an already-invariant thread changes nothing."""
    from System.Threading import Thread

    ensure_invariant_culture()
    before = Thread.CurrentThread.CurrentCulture.Name
    ensure_invariant_culture()

    assert before == "" == Thread.CurrentThread.CurrentCulture.Name


def test_a_culture_that_only_equals_invariant_is_replaced(hostile_culture):
    """A clone of invariant with a comma separator parses differently.

    Its name matches ``InvariantCulture``, so the guard compares by reference.
    """
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    clone = CultureInfo.InvariantCulture.Clone()
    clone.NumberFormat.NumberDecimalSeparator = ","
    Thread.CurrentThread.CurrentCulture = clone
    assert Thread.CurrentThread.CurrentCulture.Name == ""

    try:
        ensure_invariant_culture()
        assert _timespan_ticks(parse("T | where ts > ago(1.5h)")) == int(
            1.5 * TICKS_PER_HOUR
        )
    finally:
        Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture
