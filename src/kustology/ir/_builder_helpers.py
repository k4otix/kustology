# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Stateless helpers used by :class:`kustology.ir.builder.IRBuilder`.

Each function operates on a .NET AST node, a primitive, or already-built IR
nodes. None of them needs ``self`` or the visitor — splitting them out keeps
``builder.py`` focused on the visitor itself.
"""

from __future__ import annotations

import logging
from typing import Any

from .spans import Span
from .types import KustoType

logger = logging.getLogger(__name__)


def safe_int(node: Any) -> int:
    """Parse ``node.ToString()`` as an integer; raise ValueError with context."""
    try:
        return int(node.ToString().strip())
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"Expected integer literal for take/sample/top count, got {node.ToString()!r}: {e}",
        ) from e


def visit_name(node: Any) -> str:
    """Extract a simple-name string from a .NET name node, recursing into wrappers."""
    if not node:
        return "unknown"
    if hasattr(node, "Text"):
        return node.Text.strip()
    kind = str(type(node).__name__)
    if kind == "TokenName":
        return visit_name(node.Name)
    if kind == "BracketedName":
        return node.Name.ToString().strip(" '\"")
    if hasattr(node, "Name") and not isinstance(node.Name, str):
        return visit_name(node.Name)
    return node.ToString().strip()


def map_net_type(type_name: str) -> KustoType:
    """Map a .NET Kusto type name (e.g. ``"long"``) to a :class:`KustoType`."""
    type_map = {
        "bool": KustoType.BOOL,
        "int": KustoType.INT,
        "long": KustoType.LONG,
        "real": KustoType.REAL,
        "decimal": KustoType.DECIMAL,
        "datetime": KustoType.DATETIME,
        "timespan": KustoType.TIMESPAN,
        "guid": KustoType.GUID,
        "string": KustoType.STRING,
        "dynamic": KustoType.DYNAMIC,
        "tabular": KustoType.TABULAR,
    }
    return type_map.get(type_name.lower(), KustoType.UNRESOLVED)


def map_semantic_info(node: Any, expr: Any) -> None:
    """Copy ResultType, dynamic-element type, and nullability from the binder."""
    res_type = getattr(node, "ResultType", None)
    if res_type is None:
        return
    try:
        type_name = res_type.Name
    except AttributeError:  # pragma: no cover
        return
    expr.result_type = map_net_type(type_name)
    inner = getattr(res_type, "Underlying", None) or getattr(res_type, "Element", None)
    if inner is not None:
        try:
            inner_name = getattr(inner, "Name", None)
            if inner_name:
                expr.result_type_inner = map_net_type(str(inner_name))
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("inner result-type probe fell through: %s", e)
    try:
        is_nullable = getattr(res_type, "IsNullable", None)
        if is_nullable is not None:
            expr.nullable = bool(is_nullable)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("nullability probe fell through: %s", e)


def to_span(node: Any) -> Span:
    """Convert a .NET node's TextStart/Width into a :class:`Span`."""
    return Span(text_start=node.TextStart, width=node.Width)


def extract_qualified_table_name(node: Any) -> str | None:
    """Return the rightmost simple name of a qualified table ``PathExpression``.

    Handles ``cluster("c").database("d").T``, ``database("d").T``, and ``A.B.T``.
    """
    kind = str(type(node).__name__)
    if kind != "PathExpression":
        return None
    sel = getattr(node, "Selector", None)
    if sel is None or str(type(sel).__name__) != "NameReference":
        return None
    return visit_name(sel.Name)


def is_table_symbol(sym: Any) -> bool:
    """True iff ``sym`` is a Kusto TableSymbol (or a structural equivalent)."""
    if sym is None:
        return False
    try:
        if str(type(sym).__name__).endswith("TableSymbol"):
            return True
    except Exception as e:  # pragma: no cover
        logger.debug("TableSymbol type probe fell through: %s", e)
    try:
        return str(getattr(sym, "Kind", "")) == "Table"
    except Exception:  # pragma: no cover
        return False


def extract_named_param(node: Any, param_name: str, default: str) -> str:
    """Walk an operator's NamedParameter list looking for ``param_name=value``."""
    params = getattr(node, "Parameters", None)
    if not params or not getattr(params, "Count", 0):
        return default
    target = param_name.lower()
    for i in range(params.Count):
        param = getattr(params[i], "Element", params[i])
        name_node = getattr(param, "Name", None)
        if name_node is None:
            continue
        pname = getattr(name_node, "SimpleName", None) or visit_name(name_node)
        if str(pname).lower() != target:
            continue
        expr = getattr(param, "Expression", None)
        if expr is None:
            continue
        sub = getattr(expr, "Name", None)
        if sub is not None:
            return str(getattr(sub, "SimpleName", None) or visit_name(sub))
        lit = getattr(expr, "LiteralValue", None)
        if lit is not None:
            return str(lit)
        return expr.ToString().strip()
    return default


