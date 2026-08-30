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

import os
import sys
import warnings

from .._text import check_utf16_encodable
from ..bridge import (
    ColumnSymbol,
    DatabaseSymbol,
    GlobalState,
    ScalarTypes,
    TableSymbol,
)

# The `kustology` package directory. Every frame at or below it belongs to this
# library; the first frame above it is the caller a warning should name.
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep

# Microsoft's own name for "no type": ``ScalarTypes.Unknown.Name``. Not in the
# ``GetSymbol`` table, so it has to be answered before the lookup -- see
# ``_resolve_scalar_type``. The IR spells the same idea the same way in
# ``TabularSchema.columns``; ``KustoType.UNRESOLVED`` ("unresolved") is the
# separate, enum-typed sentinel for an expression's type.
_UNKNOWN_TYPE_NAME = "unknown"


def _caller_stacklevel() -> int:
    """Return the ``warnings.warn`` stacklevel of the first frame outside this package.

    A hardcoded number cannot be right here. The depth from
    :func:`_resolve_scalar_type` out to user code depends on which entry point
    was used — ``parse`` and ``validate`` are one frame deeper than a direct
    :func:`build_global_state` call — and, worse, on the Python version:
    PEP 709 inlined comprehensions in **3.12**, and this project supports
    3.10 and 3.11, where the two comprehensions in this module each push a
    frame of their own. A constant tuned on 3.12 is attributed back into this
    very file on those interpreters, which is the bug the level exists to
    avoid. Walking out to the package boundary is correct on every version by
    construction, and needs no maintenance when the call chain changes.

    ``stacklevel=1`` means the frame that calls ``warn``; that frame is this
    function's caller, so the walk starts there at 1 and counts outward.
    ``skip_file_prefixes=`` does the same job in one argument and is 3.12-only.
    """
    frame = sys._getframe(1)
    level = 1
    while frame is not None:
        if not frame.f_code.co_filename.startswith(_PACKAGE_ROOT):
            return level
        parent = frame.f_back
        if parent is None:
            # Nothing outside the package on this stack (an internal call at
            # import time). Naming the outermost frame we have beats pointing
            # past the top of the stack, which renders as "sys:1".
            return level
        frame = parent
        level += 1
    return level  # pragma: no cover — unreachable: the loop returns first


def _resolve_scalar_type(type_name: str, *, column: str | None = None):
    """Resolve a KQL type name to a ScalarSymbol via Microsoft's lookup.

    The lookup key is case-folded. ``ScalarTypes.GetSymbol`` is an exact
    dictionary lookup and every scalar type name and alias in the grammar is
    lower-case (``long``, ``int64``, ``datetime``, ``boolean``, …), so an
    unfolded lookup misses a schema transcribed from a portal column list —
    ``"LONG"``, ``"DateTime"`` — every time and silently mistypes those
    columns ``string``. Folding cannot collide with a real name, because
    there is no scalar type whose spelling differs from another only by case.

    A genuine miss is the caller's typo in their own schema dict, so the
    warning is reported against the caller's own line rather than against
    this file — see :func:`_caller_stacklevel` for why that depth is computed
    and not written down. Attributed at the library's own file instead, the
    warning names a module the caller does not own,
    ``-W error::RuntimeWarning`` blames the wrong place, and the default
    "once per location" filter folds every caller's typo into a single
    report.

    ``"unknown"`` is answered directly. It is Microsoft's own name for "no
    type" (``ScalarTypes.Unknown.Name``) and what
    :func:`extract_schemas_from_global_state` emits for a column the binder
    could not type — but ``GetSymbol`` does not carry it, so left to the
    lookup the dict form scolds the caller for a real type name and hands
    back ``string`` while ``{"T": "(c:unknown)"}`` keeps it, and
    round-tripping the extractor's own output through
    :func:`build_global_state` silently retypes those columns.

    A non-``str`` type name is a ``TypeError`` raised here rather than at the
    CLR boundary: ``GetSymbol(None)`` surfaces as a bare
    ``System.ArgumentNullException`` with a .NET stack trace through
    ``System.Collections.Generic.Dictionary``, and ``GetSymbol(5)`` as
    pythonnet's "No method matches given arguments",
    neither of which mentions schemas. ``column`` puts the offending key in
    the message, so every schema-shape error this module raises names its own
    position — the schema, a table name, a table's value, a column name, a
    column's type.
    """
    if not isinstance(type_name, str):
        where = f" for column {column!r}" if column is not None else ""
        raise TypeError(
            f"Schema column type{where} must be a KQL scalar type name as a "
            f"str; got {type(type_name).__name__}. The typed-column form is "
            "{table: {column: 'type'}}, for example {'T': {'c': 'long'}}."
        )
    folded = type_name.lower()
    if folded == _UNKNOWN_TYPE_NAME:
        return ScalarTypes.Unknown
    check_utf16_encodable(
        folded, f"Schema column type{f' for column {column!r}' if column else ''}",
    )
    sym = ScalarTypes.GetSymbol(folded)
    if sym is None:
        warnings.warn(
            f"Unknown KQL scalar type {type_name!r}; falling back to 'string'.",
            RuntimeWarning,
            stacklevel=_caller_stacklevel(),
        )
        return ScalarTypes.String
    return sym


def _warn_on_untyped_schema_string_columns(name: str, table) -> None:
    """Warn for each column Microsoft's schema-string parser left ``unknown``.

    ``TableSymbol.From("(n:bogus)")`` does not reject the unrecognized name:
    it types the column ``ScalarTypes.Unknown`` and returns, so without a
    warning the typo reaches the binder and resolves nothing while the
    equivalent dict form ``{"n": "bogus"}`` warns. Same mistake, same
    category of warning, same attribution — the only difference is the
    fallback, and Microsoft's
    ``unknown`` is kept because substituting ``string`` here would invent a
    type the caller never wrote.

    A bare name (``"(a)"``) lands in the same place. The documented way to
    say "untyped" is the list form ``{"T": ["a"]}``, which means ``string``;
    a name with no type inside a schema string means neither.

    The stack is walked once for the whole table: the depth is a property of
    this frame, not of the column being reported, so it cannot differ
    between iterations.
    """
    unresolved = [col for col in table.Columns if col.Type == ScalarTypes.Unknown]
    if not unresolved:
        return
    stacklevel = _caller_stacklevel()
    for col in unresolved:
        warnings.warn(
            f"Column {str(col.Name)!r} in the schema string for table "
            f"{name!r} has no resolvable KQL scalar type; Microsoft's "
            "schema parser typed it 'unknown'.",
            RuntimeWarning,
            stacklevel=stacklevel,
        )


def _check_column_name(column, table: str):
    """Check that a column key is a str; it becomes the ``ColumnSymbol.Name`` verbatim.

    ``ColumnSymbol(5, …)`` surfaces as pythonnet's "No method matches given
    arguments for ColumnSymbol..ctor" — the same unnameable, schema-silent
    wording :func:`_resolve_scalar_type` heads off for the *type* position
    one argument to the right.
    """
    if not isinstance(column, str):
        raise TypeError(
            f"Schema column name in table {table!r} must be a str; got "
            f"{type(column).__name__} ({column!r}). Keys become the "
            "column symbol's name verbatim."
        )
    check_utf16_encodable(column, f"Schema column name in table {table!r}")
    return column


def _build_table_symbol(name: str, cols):
    """Build a TableSymbol from the supported schema-value forms."""
    if not isinstance(name, str):
        raise TypeError(
            f"Schema table name must be a str; got {type(name).__name__} "
            f"({name!r}). Keys become the table symbol's name verbatim."
        )
    check_utf16_encodable(name, "Schema table name")
    if isinstance(cols, str):
        # ``TableSymbol.From`` is permissive to a fault -- ``"("``, ``"junk"``
        # and ``"(a:long"`` are all accepted -- but it raises
        # ``System.InvalidOperationException`` on an empty or whitespace-only
        # string, a CLR type a caller cannot name without importing from the
        # CLR and cannot catch except by bare ``except Exception``. That one
        # input is the whole difference this guard makes.
        if not cols.strip():
            raise ValueError(
                f"Empty schema string for table {name!r}. Use "
                "'(col:type, ...)', or the dict form {col: type}; for a "
                "table with no columns pass an empty list, []."
            )
        check_utf16_encodable(cols, f"Schema string for table {name!r}")
        table = TableSymbol.From(cols).WithName(name)
        _warn_on_untyped_schema_string_columns(name, table)
        return table
    if isinstance(cols, dict):
        col_symbols = [
            ColumnSymbol(_check_column_name(c, name), _resolve_scalar_type(t, column=c))
            for c, t in cols.items()
        ]
        return TableSymbol(name, col_symbols)
    if isinstance(cols, (list, tuple)):
        col_symbols = [
            ColumnSymbol(_check_column_name(c, name), ScalarTypes.String) for c in cols
        ]
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

    **Every key is a raw name, not query text.** Table keys and column keys
    become the ``Name`` of a ``TableSymbol`` / ``ColumnSymbol`` verbatim.
    The bracket-quoting ``["my col"]`` / ``['my col']`` is KQL *query* syntax
    for referring to a name that is not a bare identifier; written as a key
    it is taken literally, so ``{"T": {"['my col']": "string"}}`` declares a
    column whose name is the ten characters ``['my col']`` and no query can
    reach it. Write ``{"T": {"my col": "string"}}`` and let the query do the
    quoting.

    Type names are case-insensitive (``"LONG"`` is ``long``) and
    ``"unknown"`` is accepted as Microsoft's own name for "no type", so the
    output of :func:`extract_schemas_from_global_state` round-trips. An
    unrecognized name falls back to ``string`` with a ``RuntimeWarning``;
    inside a schema string it is Microsoft's ``unknown`` — also a
    ``RuntimeWarning``, but without the fallback.

    Wrong-typed input raises rather than reaching the CLR: a non-``str``
    table name, column name or type name is a ``TypeError``, a table value
    that is none of the three forms is a ``TypeError``, and an empty or
    whitespace-only schema string is a ``ValueError``. A name, type, or schema
    string holding an unpaired surrogate is a ``ValueError`` too: UTF-16
    cannot encode one, and pythonnet's failure to marshal it aborts the
    process. Every message names the position it rejects.
    """
    if not isinstance(schema, dict):
        raise TypeError(
            "schema must be a dict mapping table name to a column spec; "
            f"got {type(schema).__name__}."
        )
    tables = [_build_table_symbol(name, cols) for name, cols in schema.items()]
    return GlobalState.Default.WithDatabase(DatabaseSymbol("NetDB", tables))
