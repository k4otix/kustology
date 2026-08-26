# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Runtime introspection of the loaded ``Kusto.Language`` assembly.

Categorized KQL function name lookups and the full ``SyntaxKind`` enum. Cached
after first call. Reflection failure is loud, not silent: categories return
empty rather than fall back to a stale hard-coded set that would silently
drift from upstream.
"""

from __future__ import annotations

import logging

# Import for side effect: triggers `_initialize_bridge()` (CLR + Kusto.Language).
from . import bridge  # noqa: F401

logger = logging.getLogger(__name__)


_FUNCS_BY_NAME: dict[str, object] = {}  # symbol-name → FunctionSymbol
_CATEGORIES: dict[str, frozenset[str]] = {}


def _safe_name(sym: object) -> str | None:
    try:
        name = getattr(sym, "Name", None)
        if name is None:
            return None
        return str(name)
    except Exception:  # pragma: no cover
        return None


def _safe_return_type_name(sym: object) -> str | None:
    """Return a function's return-type name, lowercased, across every overload.

    ``Signature.DeclaredReturnType`` carries the primitive type symbol for
    fixed-return functions (``strcat`` → string, ``ago`` → datetime).
    Computed / parameterized return kinds expose it as ``None`` and would
    need ``GetReturnType()`` resolution at the call site — not something we
    can do offline here.

    A function has one ``Signature`` per overload and they need not agree:
    ``bin`` declares ``[None, timespan, datetime, datetime]`` and ``bin_at``
    ``[None, timespan, timespan, datetime]``, so reading signature zero alone
    reports ``None`` for the two functions almost every Sentinel query uses
    to bucket time and files both under ``scalar``. ``datetime`` /
    ``timespan`` wins as soon as any overload declares it; otherwise the
    first declared name is used, and ``None`` means no overload declared one
    at all.
    """
    try:
        signatures = getattr(sym, "Signatures", None)
        if signatures is None:
            return None
        fallback: str | None = None
        for i in range(signatures.Count):
            rt = getattr(signatures[i], "DeclaredReturnType", None)
            if rt is None:
                continue
            rt_name = getattr(rt, "Name", None)
            if rt_name is None:
                continue
            name = str(rt_name).lower()
            if name in ("datetime", "timespan"):
                return name
            if fallback is None:
                fallback = name
        return fallback
    except Exception:  # pragma: no cover
        return None


def _enumerate_static_symbols(container_name: str) -> dict[str, object]:
    """Return every ``FunctionSymbol`` static on ``Kusto.Language.<container_name>``, keyed by symbol name.

    ``container.All`` — an ``IReadOnlyList[FunctionSymbol]`` — is the
    authoritative list and is read first. Enumerating with ``dir()`` alone
    loses any static whose .NET name collides with a member of
    ``System.Object`` or with the list itself: ``Functions.ToString``,
    ``Functions.GetType`` and ``Functions.All`` are shadowed, hiding
    ``tostring`` — the most-called scalar function in Sentinel content —
    ``gettype`` and ``all`` from every category this module publishes.

    The ``dir()`` sweep supplements ``All`` rather than yielding to it,
    because ``All`` is not exhaustive either: ``PlugIns.SchemaMerge`` is a
    real ``evaluate`` plug-in that ``PlugIns.All`` omits. Neither list
    subsumes the other, so both are read. What each contributes moves with
    the assembly and is not a reason to drop either: on the bundled 12.4.1
    the ``dir()`` half adds only ``schema_merge``, to ``PlugIns`` — on 12.3.2
    it contributed to ``Functions`` as well, and 12.4.1 folded those into
    ``Functions.All`` — while ``All`` still recovers ``tostring``,
    ``gettype`` and ``all``, which ``dir()`` alone cannot see.
    """
    out: dict[str, object] = {}
    try:
        module = __import__("Kusto.Language", fromlist=[container_name])
        container = getattr(module, container_name, None)
        if container is None:
            return out
        listed = getattr(container, "All", None)
        if listed is not None:
            for i in range(listed.Count):
                sym = listed[i]
                name = _safe_name(sym)
                if name:
                    out[name] = sym
        for attr in dir(container):
            if attr.startswith("_"):
                continue
            try:
                sym = getattr(container, attr, None)
            except Exception as e:
                logger.debug("skipping %s.%s: %s", container_name, attr, e)
                continue
            if sym is None:
                continue
            name = _safe_name(sym)
            if name:
                out.setdefault(name, sym)
    except Exception as e:  # pragma: no cover
        logger.debug("Reflection on Kusto.Language.%s failed: %s", container_name, e)
    return out


def _load() -> None:
    """Populate caches from the loaded DLL. Idempotent.

    On reflection failure the categories stay empty so consumers fail
    visibly rather than against a stale fallback. The DLL load failing
    is a much louder error anyway (raised at import time from
    ``bridge.py``), so reaching here with empty reflection results
    means the upstream Functions/Aggregates surface itself changed.
    """
    if _CATEGORIES:
        return

    funcs: dict[str, object] = {}
    aggs: dict[str, object] = {}
    plugins: dict[str, object] = {}

    try:
        funcs.update(_enumerate_static_symbols("Functions"))
        aggs.update(_enumerate_static_symbols("Aggregates"))
        plugins.update(_enumerate_static_symbols("PlugIns"))
    except Exception as e:  # pragma: no cover
        logger.warning("Reflection on Kusto.Language failed: %s", e)

    _FUNCS_BY_NAME.update(funcs)
    _FUNCS_BY_NAME.update(aggs)
    _FUNCS_BY_NAME.update(plugins)

    time_set: set[str] = set()
    string_set: set[str] = set()
    scalar_set: set[str] = set()
    all_set: set[str] = set()

    for name, sym in funcs.items():
        all_set.add(name)
        rt = _safe_return_type_name(sym)
        if rt in ("datetime", "timespan"):
            time_set.add(name)
        elif rt == "string":
            string_set.add(name)
        else:
            scalar_set.add(name)

    agg_set: set[str] = set()
    for name, sym in aggs.items():
        all_set.add(name)
        agg_set.add(name)

    plugin_set: set[str] = set()
    for name, sym in plugins.items():
        all_set.add(name)
        plugin_set.add(name)

    # Four names — `any`, `hll_merge`, `merge_tdigest`, `tdigest_merge` —
    # are declared in both `Functions` and `Aggregates`. They are aggregates,
    # and `scalar` is defined as the leftovers, so the aggregate list wins.
    # Without the subtraction a caller asking "is this a scalar function?"
    # gets yes for all four. `time` and `string` need none: neither
    # intersects `Aggregates` on the bundled assembly.
    scalar_set -= agg_set

    _CATEGORIES["time"] = frozenset(time_set)
    _CATEGORIES["aggregate"] = frozenset(agg_set)
    _CATEGORIES["string"] = frozenset(string_set)
    _CATEGORIES["scalar"] = frozenset(scalar_set)
    _CATEGORIES["plugin"] = frozenset(plugin_set)
    _CATEGORIES["all"] = frozenset(all_set)


def time_functions() -> frozenset[str]:
    """Names of KQL functions that return ``datetime`` or ``timespan``.

    "Return" here means *any* overload declares one, so ``bin``, ``bin_at``
    and ``floor`` are members — and so is ``abs``, whose timespan overload
    returns a timespan even though the function is not about time. The
    converse gap is larger: ``format_datetime`` returns a string and
    ``datetime_diff`` a number, so neither is here. A caller wanting
    "functions a query uses to work with time" wants a wider set than a
    return type can express; ``utils.analysis.find_time_expressions`` unions
    this one with a hand-curated list for exactly that reason.
    """
    _load()
    return _CATEGORIES["time"]


def aggregate_functions() -> frozenset[str]:
    """Names of KQL aggregate functions (members of ``Kusto.Language.Aggregates``)."""
    _load()
    return _CATEGORIES["aggregate"]


def string_functions() -> frozenset[str]:
    """Names of KQL scalar functions whose return type is ``string``."""
    _load()
    return _CATEGORIES["string"]


def scalar_functions() -> frozenset[str]:
    """Names of KQL scalar functions not classified as time/string/aggregate.

    Disjoint from :func:`aggregate_functions`: the four names declared in
    both ``Functions`` and ``Aggregates`` are reported only as aggregates.
    """
    _load()
    return _CATEGORIES["scalar"]


def plugin_functions() -> frozenset[str]:
    """Names of the ``evaluate`` plug-ins (``Kusto.Language.PlugIns``).

    ``bag_unpack``, ``pivot``, ``narrow``, ``sql_request`` and the rest are
    invoked as ``| evaluate name(...)``, never as scalar calls, so they are
    their own category rather than part of :func:`scalar_functions`. They are
    included in :func:`all_function_names`.
    """
    _load()
    return _CATEGORIES["plugin"]


def all_function_names() -> frozenset[str]:
    """Every KQL function name discoverable on the loaded ``Kusto.Language``.

    The union of ``Functions``, ``Aggregates`` and ``PlugIns`` — so it is a
    superset of all five categories this module publishes.
    """
    _load()
    return _CATEGORIES["all"]


def syntax_kinds() -> frozenset[str]:
    """Every member of ``Kusto.Language.Syntax.SyntaxKind`` as a string."""
    try:
        from Kusto.Language.Syntax import SyntaxKind
        from System import Enum

        return frozenset(str(k) for k in Enum.GetValues(SyntaxKind))
    except Exception as e:  # pragma: no cover
        logger.debug("SyntaxKind reflection failed: %s", e)
        return frozenset()


__all__ = [
    "aggregate_functions",
    "all_function_names",
    "plugin_functions",
    "scalar_functions",
    "string_functions",
    "syntax_kinds",
    "time_functions",
]
