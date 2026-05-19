# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Build a Microsoft :class:`GlobalState` from a Python schema dict.

The IR binder, the validator's schema-aware paths, and tests all need a bound
``GlobalState`` to drive Microsoft's ``KustoCode.ParseAndAnalyze``. This module
is the one place that knows how to translate the documented Python schema
shapes (``{table: {col: type}}``, ``"(col:type, ...)"``, or ``[col, ...]``)
into the corresponding .NET ``TableSymbol`` / ``ColumnSymbol`` / ``DatabaseSymbol``
tree.
"""

from __future__ import annotations

import warnings

from ..bridge import (
    ColumnSymbol,
    DatabaseSymbol,
    GlobalState,
    ScalarTypes,
    TableSymbol,
)


def _resolve_scalar_type(type_name: str):
    """Resolve a KQL type name to a ScalarSymbol via Microsoft's lookup."""
    sym = ScalarTypes.GetSymbol(type_name)
    if sym is None:
        warnings.warn(
            f"Unknown KQL scalar type {type_name!r}; falling back to 'string'.",
            RuntimeWarning,
            stacklevel=3,
        )
        return ScalarTypes.String
    return sym


def _build_table_symbol(name: str, cols):
    """Build a TableSymbol from the supported schema-value forms."""
    if isinstance(cols, str):
        return TableSymbol.From(cols).WithName(name)
    if isinstance(cols, dict):
        col_symbols = [ColumnSymbol(c, _resolve_scalar_type(t)) for c, t in cols.items()]
        return TableSymbol(name, col_symbols)
    if isinstance(cols, (list, tuple)):
        col_symbols = [ColumnSymbol(c, ScalarTypes.String) for c in cols]
        return TableSymbol(name, col_symbols)
    raise TypeError(
        f"Unsupported schema value for table {name!r}: {type(cols).__name__}. "
        "Use a dict {col: type}, list [col, ...], or schema string '(col:type, ...)'."
    )


def build_global_state(schema):
    """Convert a Python schema description into a Kusto :class:`GlobalState`.

    Accepted forms:
      * dict ``{table: {col: type}}`` — typed columns
      * dict ``{table: "(col:type, ...)"}`` — per-table Kusto schema string
      * dict ``{table: [col, ...]}`` — untyped columns (treated as string)
    """
    if not isinstance(schema, dict):
        raise TypeError(
            "schema must be a dict mapping table name to a column spec; "
            f"got {type(schema).__name__}."
        )
    tables = [_build_table_symbol(name, cols) for name, cols in schema.items()]
    return GlobalState.Default.WithDatabase(DatabaseSymbol("NetDB", tables))
