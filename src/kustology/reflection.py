# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Runtime introspection of the loaded ``Kusto.Language`` assembly.

Categorized KQL function name lookups and the full ``SyntaxKind`` enum. Cached
after first call; falls back to hard-coded sets if reflection fails.
"""

from __future__ import annotations

import logging
from typing import Optional

# Import for side effect: triggers `_initialize_bridge()` (CLR + Kusto.Language).
from . import bridge  # noqa: F401

logger = logging.getLogger(__name__)


# Conservative fallbacks; reflected lists at runtime are typically supersets.
_FALLBACK_TIME_FUNCS = frozenset({
    "ago", "now", "datetime", "todatetime", "totimespan",
    "startofday", "endofday", "startofweek", "endofweek",
    "startofmonth", "endofmonth", "startofyear", "endofyear",
    "bin", "bin_at", "bin_auto",
    "format_datetime", "format_timespan",
    "datetime_add", "datetime_diff", "datetime_part",
    "make_datetime", "make_timespan",
    "monthofyear", "dayofmonth", "dayofweek", "dayofyear",
    "hourofday", "weekofyear", "getyear", "getmonth",
    "unixtime_seconds_todatetime", "unixtime_milliseconds_todatetime",
    "unixtime_microseconds_todatetime", "unixtime_nanoseconds_todatetime",
})

_FALLBACK_AGG_FUNCS = frozenset({
    "count", "countif", "dcount", "dcountif", "sum", "sumif",
    "avg", "avgif", "min", "minif", "max", "maxif",
    "make_set", "make_set_if", "make_list", "make_list_if",
    "make_bag", "make_bag_if", "percentile", "percentiles",
    "stdev", "stdevif", "stdevp", "variance", "varianceif", "variancep",
    "any", "anyif", "arg_max", "arg_min", "take_any", "take_anyif",
    "hll", "hll_merge", "tdigest", "tdigest_merge",
})

_FALLBACK_STRING_FUNCS = frozenset({
    "strcat", "strcat_array", "strcat_delim", "strlen", "substring",
    "tolower", "toupper", "trim", "trim_start", "trim_end",
    "replace", "replace_string", "replace_regex", "split", "extract",
    "extract_all", "indexof", "reverse", "isempty", "isnotempty",
    "isnull", "isnotnull", "format_ipv4", "format_ipv6",
    "parse_csv", "parse_json", "parse_xml", "parse_url", "parse_urlquery",
    "parse_path", "parse_user_agent", "url_decode", "url_encode",
    "base64_encodestring", "base64_decodestring",
})


_FUNCS_BY_NAME: dict[str, object] = {}  # symbol-name → FunctionSymbol
_CATEGORIES: dict[str, frozenset[str]] = {}
_LOAD_FAILED: bool = False


def _safe_name(sym: object) -> Optional[str]:
    try:
        name = getattr(sym, "Name", None)
        if name is None:
            return None
        return str(name)
    except Exception:  # pragma: no cover
        return None


def _safe_return_type_name(sym: object) -> Optional[str]:
    """First signature's return-type name, lowercased. None if unavailable."""
    try:
        signatures = getattr(sym, "Signatures", None)
        if signatures is None or signatures.Count == 0:
            return None
        rt = getattr(signatures[0], "ReturnType", None)
        if rt is None:
            return None
        rt_name = getattr(rt, "Name", None)
        if rt_name is None:
            return None
        return str(rt_name).lower()
    except Exception:  # pragma: no cover
        return None


def _safe_first_param_type_name(sym: object) -> Optional[str]:
    """First signature's first parameter type name, lowercased."""
    try:
        signatures = getattr(sym, "Signatures", None)
        if signatures is None or signatures.Count == 0:
            return None
        params = getattr(signatures[0], "Parameters", None)
        if params is None or params.Count == 0:
            return None
        ptype = getattr(params[0], "Type", None) or getattr(params[0], "TypeKind", None)
        if ptype is None:
            return None
        ptype_name = getattr(ptype, "Name", None) or str(ptype)
        return str(ptype_name).lower()
    except Exception:  # pragma: no cover
        return None


def _enumerate_static_symbols(container_name: str) -> dict[str, object]:
    """Return ``{symbol_name: symbol}`` for all FunctionSymbol-shaped static
    members of the named ``Kusto.Language.<container>`` class.
    """
    out: dict[str, object] = {}
    try:
        module = __import__("Kusto.Language", fromlist=[container_name])
        container = getattr(module, container_name, None)
        if container is None:
            return out
        for attr in dir(container):
            if attr.startswith("_"):
                continue
            try:
                sym = getattr(container, attr, None)
            except Exception:
                continue
            if sym is None:
                continue
            name = _safe_name(sym)
            if name:
                out[name] = sym
    except Exception as e:  # pragma: no cover
        logger.debug("Reflection on Kusto.Language.%s failed: %s", container_name, e)
    return out


def _load() -> None:
    """Populate caches from the loaded DLL. Idempotent."""
    global _LOAD_FAILED
    if _CATEGORIES or _LOAD_FAILED:
        return

    funcs: dict[str, object] = {}
    aggs: dict[str, object] = {}

    try:
        funcs.update(_enumerate_static_symbols("Functions"))
        aggs.update(_enumerate_static_symbols("Aggregates"))
    except Exception as e:  # pragma: no cover
        logger.debug("Reflection bootstrap failed: %s", e)

    if not funcs and not aggs:
        _LOAD_FAILED = True
        _CATEGORIES["time"] = _FALLBACK_TIME_FUNCS
        _CATEGORIES["aggregate"] = _FALLBACK_AGG_FUNCS
        _CATEGORIES["string"] = _FALLBACK_STRING_FUNCS
        _CATEGORIES["scalar"] = frozenset()
        _CATEGORIES["all"] = (
            _FALLBACK_TIME_FUNCS | _FALLBACK_AGG_FUNCS | _FALLBACK_STRING_FUNCS
        )
        return

    _FUNCS_BY_NAME.update(funcs)
    _FUNCS_BY_NAME.update(aggs)

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

    # Union with fallback so anything reflection missed still surfaces.
    _CATEGORIES["time"] = frozenset(time_set | _FALLBACK_TIME_FUNCS)
    _CATEGORIES["aggregate"] = frozenset(agg_set | _FALLBACK_AGG_FUNCS)
    _CATEGORIES["string"] = frozenset(string_set | _FALLBACK_STRING_FUNCS)
    _CATEGORIES["scalar"] = frozenset(scalar_set)
    _CATEGORIES["all"] = frozenset(all_set | _CATEGORIES["time"] | _CATEGORIES["aggregate"] | _CATEGORIES["string"])


def time_functions() -> frozenset[str]:
    """Names of KQL functions that return ``datetime`` or ``timespan``."""
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
    """Names of KQL scalar functions not classified as time/string/aggregate."""
    _load()
    return _CATEGORIES["scalar"]


def all_function_names() -> frozenset[str]:
    """Every KQL function name discoverable on the loaded ``Kusto.Language``."""
    _load()
    return _CATEGORIES["all"]


def syntax_kinds() -> frozenset[str]:
    """Every member of ``Kusto.Language.Syntax.SyntaxKind`` as a string."""
    try:
        from System import Enum

        from Kusto.Language.Syntax import SyntaxKind

        return frozenset(str(k) for k in Enum.GetValues(SyntaxKind))
    except Exception as e:  # pragma: no cover
        logger.debug("SyntaxKind reflection failed: %s", e)
        return frozenset()


__all__ = [
    "time_functions",
    "aggregate_functions",
    "string_functions",
    "scalar_functions",
    "all_function_names",
    "syntax_kinds",
]
