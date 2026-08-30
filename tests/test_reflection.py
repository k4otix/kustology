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
    # The set is a superset and grows with Kusto.Language upgrades, so assert
    # membership. ``datetime`` is parsed as a literal, so it is not a member of
    # Kusto.Language.Functions at all.
    for canonical in ("ago", "now", "startofday", "endofweek", "todatetime"):
        assert canonical in funcs, f"expected {canonical!r} in time_functions(); got {sorted(funcs)[:20]}"


def test_time_functions_reads_every_overload_not_just_the_first():
    """``bin`` and ``bin_at`` declare no return type on their first signature.

    Their overload lists are ``[None, timespan, datetime, datetime]`` and
    ``[None, timespan, timespan, datetime]``, so reading signature zero alone
    sees ``None`` and files both under ``scalar_functions()``.
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

    ``System.Object.ToString`` and ``GetType`` shadow ``Functions.ToString``
    and ``Functions.GetType``, and the symbol list shadows ``Functions.All``,
    so a ``dir()`` enumeration silently drops ``tostring``, ``gettype``, and
    ``all``. ``Functions.All`` lists all three.
    """
    everything = all_function_names()
    for shadowed in ("tostring", "gettype", "all"):
        assert shadowed in everything, f"{shadowed!r} missing from all_function_names()"


def test_scalar_and_aggregate_functions_are_disjoint():
    """Names declared in both ``Functions`` and ``Aggregates`` stay aggregates.

    ``any``, ``hll_merge``, ``merge_tdigest``, and ``tdigest_merge`` appear in
    both symbol lists; left in the scalar bucket they answer yes to "is this a
    scalar function?".
    """
    overlap = aggregate_functions() & scalar_functions()
    assert overlap == set(), f"scalar_functions() still carries aggregates: {sorted(overlap)}"
    for agg in ("any", "hll_merge", "merge_tdigest", "tdigest_merge"):
        assert agg in aggregate_functions()


def test_plugin_functions_lists_evaluate_plugins():
    """``evaluate`` plug-ins live in ``Kusto.Language.PlugIns``, their own list.

    Reflection that stops at ``Functions`` and ``Aggregates`` never sees
    ``bag_unpack`` or ``pivot``, in any category or in ``all_function_names()``.
    """
    plugins = plugin_functions()
    for canonical in ("bag_unpack", "pivot", "narrow", "sql_request"):
        assert canonical in plugins, f"expected {canonical!r} in plugin_functions(); got {sorted(plugins)[:20]}"
    assert "bag_unpack" in all_function_names()
    # Plug-ins are invoked by `evaluate`, so they stay out of the scalar and
    # aggregate buckets.
    assert plugins.isdisjoint(scalar_functions())
    assert plugins.isdisjoint(aggregate_functions())


def test_syntax_kinds_has_expected_breadth():
    """Check the size and a few canonical members; the coverage audit lives in ``scripts/audit_syntax_kinds.py``."""
    kinds = syntax_kinds()
    # 100 sits far below the enum's real size, so only an empty or truncated
    # reflection result trips this.
    assert len(kinds) > 100, f"expected >100 SyntaxKinds via reflection, got {len(kinds)}"
    # SyntaxKind names are granular (AddExpression, EqualExpression) while the
    # IR builder dispatches on Python class names ("BinaryExpression"), so pick
    # members the enum is guaranteed to expose.
    for canonical in ("FilterOperator", "JoinOperator", "AddExpression", "AndExpression"):
        assert canonical in kinds, f"expected {canonical!r} in syntax_kinds()"


def test_plugin_functions_is_exported_from_the_package_root():
    """``kustology.plugin_functions`` is the import path a caller writes.

    A name missing from ``__all__`` is invisible to ``from kustology import *``
    and to documentation tooling, so this asserts the root re-export directly.
    """
    import kustology

    assert "plugin_functions" in kustology.__all__
    assert kustology.plugin_functions is plugin_functions
    assert "bag_unpack" in kustology.plugin_functions()
