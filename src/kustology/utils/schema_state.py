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
    """Resolve a KQL type name to a ScalarSymbol via Microsoft's lookup.

    A miss is the caller's typo in their own schema dict, so the warning is
    reported against the caller's line, five frames up: this function ←
    :func:`_build_table_symbol` ← :func:`build_global_state` ←
    ``services.parse`` / ``services.validate`` ← the caller. (The two
    comprehensions in that chain add no frame — PEP 709 inlines them.)
    Attributed at the library's own file instead, the warning names a module
    the caller does not own and the default "once per location" filter folds
    every caller's typo into a single report.

    The tuned depth is for the documented entry points. Calling
    :func:`build_global_state` directly overshoots by one, which costs a
    warning pointed one frame too far out — the trade the two supported
    paths are worth.
    """
    sym = ScalarTypes.GetSymbol(type_name)
    if sym is None:
        warnings.warn(
            f"Unknown KQL scalar type {type_name!r}; falling back to 'string'.",
            RuntimeWarning,
            stacklevel=5,
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


def extract_schemas_from_global_state(global_state) -> dict[str, dict[str, str]]:
    """Walk a Microsoft ``GlobalState`` and return ``{table: {col: type}}``.

    Inverse of :func:`build_global_state`. Used by ``KustoQuery.to_ir(attach_schema=True)``
    to recover the schema dict ``SchemaAttacher`` wants without forcing the
    caller to keep a Python copy alongside the bound ``KustoCode``.
    """
    out: dict[str, dict[str, str]] = {}
    db = getattr(global_state, "Database", None)
    if db is None:
        return out
    tables = getattr(db, "Tables", None)
    if tables is None:
        return out
    for i in range(tables.Count):
        table = tables[i]
        cols: dict[str, str] = {}
        col_list = getattr(table, "Columns", None)
        if col_list is not None:
            for j in range(col_list.Count):
                col = col_list[j]
                cname = str(col.Name)
                ctype = getattr(col, "Type", None)
                ctype_name = str(getattr(ctype, "Name", "unknown"))
                cols[cname] = ctype_name
        out[str(table.Name)] = cols
    return out


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
