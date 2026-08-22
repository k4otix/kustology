# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Reflection over Kusto.Language.Functions / Aggregates / Syntax."""

from __future__ import annotations

from kustology.reflection import (
    aggregate_functions,
    all_function_names,
    plugin_functions,
    scalar_functions,
    string_functions,
    syntax_kinds,
    time_functions,
)


def test_time_functions_contains_canonical_names():
    funcs = time_functions()
    # Anchor on the canonical handful — the set is a superset (possibly larger
    # after future Kusto.Language upgrades), so test for membership not equality.
    # ``datetime`` is deliberately omitted: it is parsed as a literal, so it is
    # not a member of Kusto.Language.Functions at all.
    for canonical in ("ago", "now", "startofday", "endofweek", "todatetime"):
        assert canonical in funcs, f"expected {canonical!r} in time_functions(); got {sorted(funcs)[:20]}"


def test_time_functions_reads_every_overload_not_just_the_first():
    """``bin`` and ``bin_at`` declare no return type on their *first* signature.

    Their overload lists are ``[None, timespan, datetime, datetime]`` and
    ``[None, timespan, timespan, datetime]``: reading signature zero alone
    saw ``None`` and filed the two most common temporal functions in KQL
    under ``scalar_functions()``.
    """
    funcs = time_functions()
    assert "bin" in funcs
    assert "bin_at" in funcs
    assert {"bin", "bin_at"}.isdisjoint(scalar_functions())


def test_aggregate_functions_contains_canonical_names():
    funcs = aggregate_functions()
    for canonical in ("count", "dcount", "sum", "avg", "min", "max"):
        assert canonical in funcs, f"expected {canonical!r} in aggregate_functions(); got {sorted(funcs)[:20]}"


def test_string_functions_contains_canonical_names():
    funcs = string_functions()
    for canonical in ("strcat", "substring", "tolower", "toupper"):
        assert canonical in funcs, f"expected {canonical!r} in string_functions(); got {sorted(funcs)[:20]}"


def test_all_function_names_supersets_categories():
    everything = all_function_names()
    assert time_functions().issubset(everything)
    assert aggregate_functions().issubset(everything)
    assert string_functions().issubset(everything)
    assert scalar_functions().issubset(everything)
    assert plugin_functions().issubset(everything)


def test_all_function_names_includes_names_dir_cannot_see():
    """``dir()`` loses any static whose name collides with an object member.

    ``Functions.ToString``, ``Functions.GetType`` and ``Functions.All`` are
    shadowed by ``System.Object.ToString`` / ``GetType`` and by the symbol
    list itself, so enumerating with ``dir()`` silently dropped ``tostring``
    — the most-called scalar function in Sentinel content — along with
    ``gettype`` and ``all``. ``Functions.All`` lists all three.
    """
    everything = all_function_names()
    for shadowed in ("tostring", "gettype", "all"):
        assert shadowed in everything, f"{shadowed!r} missing from all_function_names()"


def test_scalar_and_aggregate_functions_are_disjoint():
    """``any``, ``hll_merge``, ``merge_tdigest`` and ``tdigest_merge`` are
    declared in both ``Functions`` and ``Aggregates``. They are aggregates;
    a caller asking "is this a scalar function?" got yes for all four."""
    overlap = aggregate_functions() & scalar_functions()
    assert overlap == set(), f"scalar_functions() still carries aggregates: {sorted(overlap)}"
    for agg in ("any", "hll_merge", "merge_tdigest", "tdigest_merge"):
        assert agg in aggregate_functions()


def test_plugin_functions_lists_evaluate_plugins():
    """``evaluate`` plug-ins live in ``Kusto.Language.PlugIns``, which nothing
    reflected over — so ``bag_unpack`` and ``pivot`` were absent from every
    category and from ``all_function_names()``."""
    plugins = plugin_functions()
    for canonical in ("bag_unpack", "pivot", "narrow", "sql_request"):
        assert canonical in plugins, f"expected {canonical!r} in plugin_functions(); got {sorted(plugins)[:20]}"
    assert "bag_unpack" in all_function_names()
    # Plug-ins are invoked by `evaluate`, not as scalar calls; keeping them
    # out of the scalar bucket is what makes the four categories mean
    # something.
    assert plugins.isdisjoint(scalar_functions())
    assert plugins.isdisjoint(aggregate_functions())


def test_syntax_kinds_has_expected_breadth():
    """SyntaxKind reflection: every enum member as a string. Sanity-check size
    + a few canonical members. The actual coverage audit lives elsewhere
    (scripts/audit_syntax_kinds.py)."""
    kinds = syntax_kinds()
    # The Kusto syntax grammar is broad — sanity-check that reflection
    # returned a real result, not an empty fallback. 100 is well below the
    # real number (~600).
    assert len(kinds) > 100, f"expected >100 SyntaxKinds via reflection, got {len(kinds)}"
    # SyntaxKind names are granular (AddExpression / EqualExpression / etc.)
    # rather than the Python class names ("BinaryExpression") the IR builder
    # dispatches on. Pick canonical members the enum is guaranteed to expose.
    for canonical in ("FilterOperator", "JoinOperator", "AddExpression", "AndExpression"):
        assert canonical in kinds, f"expected {canonical!r} in syntax_kinds()"


def test_plugin_functions_is_exported_from_the_package_root():
    """The brief required both exports; only ``reflection``'s was exercised.

    ``kustology.plugin_functions`` is the import path a caller actually
    writes, and a name missing from ``__all__`` is invisible to
    ``from kustology import *`` and to documentation tooling.
    """
    import kustology

    assert "plugin_functions" in kustology.__all__
    assert kustology.plugin_functions is plugin_functions
    assert "bag_unpack" in kustology.plugin_functions()
