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
    """First signature's return-type name, lowercased. None if unavailable.

    ``Signature.DeclaredReturnType`` is the property that carries the
    primitive type symbol for fixed-return functions (``strcat`` → string,
    ``ago`` → datetime). Computed / parameterized return kinds expose it as
    ``None`` and need ``GetReturnType()`` resolution at call-site — not
    something we can do offline here. Those land in the ``scalar`` bucket.
    """
    try:
        signatures = getattr(sym, "Signatures", None)
        if signatures is None or signatures.Count == 0:
            return None
        rt = getattr(signatures[0], "DeclaredReturnType", None)
        if rt is None:
            return None
        rt_name = getattr(rt, "Name", None)
        if rt_name is None:
            return None
        return str(rt_name).lower()
    except Exception:  # pragma: no cover
        return None


def _safe_first_param_type_name(sym: object) -> str | None:
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
            except Exception as e:
                logger.debug("skipping %s.%s: %s", container_name, attr, e)
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

    try:
        funcs.update(_enumerate_static_symbols("Functions"))
        aggs.update(_enumerate_static_symbols("Aggregates"))
    except Exception as e:  # pragma: no cover
        logger.warning("Reflection on Kusto.Language failed: %s", e)

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

    _CATEGORIES["time"] = frozenset(time_set)
    _CATEGORIES["aggregate"] = frozenset(agg_set)
    _CATEGORIES["string"] = frozenset(string_set)
    _CATEGORIES["scalar"] = frozenset(scalar_set)
    _CATEGORIES["all"] = frozenset(all_set)


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
        from Kusto.Language.Syntax import SyntaxKind
        from System import Enum

        return frozenset(str(k) for k in Enum.GetValues(SyntaxKind))
    except Exception as e:  # pragma: no cover
        logger.debug("SyntaxKind reflection failed: %s", e)
        return frozenset()


__all__ = [
    "aggregate_functions",
    "all_function_names",
    "scalar_functions",
    "string_functions",
    "syntax_kinds",
    "time_functions",
]
