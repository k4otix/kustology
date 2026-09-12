# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Build a Microsoft :class:`GlobalState` from a Python schema dict.

The IR binder, the validator's schema-aware paths, and tests all need a bound
``GlobalState`` to drive Microsoft's ``KustoCode.ParseAndAnalyze``. This module
is the one place that translates the documented Python schema shapes
(``{table: {col: type}}``, ``"(col:type, ...)"``, ``[col, ...]``, and a
:class:`FunctionSchema`) into the .NET ``TableSymbol`` / ``ColumnSymbol`` /
``FunctionSymbol`` / ``DatabaseSymbol`` tree.
"""

from __future__ import annotations

import os
import sys
import warnings
from dataclasses import dataclass

from .._text import check_utf16_encodable
from ..bridge import (
    ColumnSymbol,
    DatabaseSymbol,
    FunctionSymbol,
    GlobalState,
    Parameter,
    ScalarTypes,
    TableSymbol,
)

# The `kustology` package directory. Every frame at or below it belongs to this
# library; the first frame above it is the caller a warning should name.
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep

# Microsoft's own name for "no type": ``ScalarTypes.Unknown.Name``, which the
# ``GetSymbol`` table does not carry, so ``_resolve_scalar_type`` answers it
# before the lookup. The IR spells it the same way in ``TabularSchema.columns``;
# ``KustoType.UNRESOLVED`` ("unresolved") is the separate, enum-typed sentinel
# for an expression's type.
_UNKNOWN_TYPE_NAME = "unknown"


def _caller_stacklevel() -> int:
    """Return the ``warnings.warn`` stacklevel of the first frame outside this package.

    A hardcoded number cannot be right. The depth from
    :func:`_resolve_scalar_type` out to user code depends on the entry point,
    since ``parse`` and ``validate`` are one frame deeper than a direct
    :func:`build_global_state` call. It also depends on the Python version:
    PEP 709 inlined comprehensions in 3.12, and on the 3.10 and 3.11 this
    project also supports, the comprehension that :func:`_build_table_symbol`
    runs over a dict of columns pushes a frame of its own. A constant tuned
    on 3.12 attributes the warning back into this file on those interpreters.
    Walking out to the package boundary is correct on every version and
    survives changes to the call chain.

    ``stacklevel=1`` means the frame that calls ``warn``, which is this
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
            # import time); pointing past the top would render as "sys:1".
            return level
        frame = parent
        level += 1
    return level  # pragma: no cover — unreachable: the loop returns first


def _resolve_scalar_type(type_name: str, *, position: str):
    """Resolve a KQL type name to a ScalarSymbol via Microsoft's lookup.

    The lookup key is case-folded. ``ScalarTypes.GetSymbol`` is an exact
    dictionary lookup and every scalar type name and alias in the grammar is
    lower-case (``long``, ``int64``, ``datetime``, ``boolean``, …), so an
    unfolded lookup misses a schema transcribed from a portal column list
    (``"LONG"``, ``"DateTime"``) every time and silently types those columns
    ``string``. No two scalar types differ only by case, so folding cannot
    collide with a real name.

    A genuine miss is the caller's typo in their own schema dict, so the
    warning is attributed to the caller's own line; :func:`_caller_stacklevel`
    explains why that depth is computed. Attributed at the library's own file,
    the warning names a module the caller does not own,
    ``-W error::RuntimeWarning`` blames the wrong place, and the default
    "once per location" filter folds every caller's typo into a single report.

    ``"unknown"`` is answered directly. It is Microsoft's own name for "no
    type" (``ScalarTypes.Unknown.Name``) and what
    :func:`extract_schemas_from_global_state` emits for a column the binder
    could not type, and ``GetSymbol`` does not carry it. Left to the lookup,
    the dict form warns about a real type name and hands back ``string`` while
    ``{"T": "(c:unknown)"}`` keeps it, so round-tripping the extractor's own
    output through :func:`build_global_state` silently retypes those columns.

    A non-``str`` type name raises ``TypeError`` here, before the CLR boundary.
    ``GetSymbol(None)`` surfaces as a bare ``System.ArgumentNullException``
    with a .NET stack trace through ``System.Collections.Generic.Dictionary``,
    and ``GetSymbol(5)`` as pythonnet's "No method matches given arguments";
    neither mentions schemas.

    ``position`` is the tail of the phrase ``Schema <position>``, such as
    ``"column type for column 'c'"`` or
    ``"type for parameter 'p' of function 'f'"``. Every message raised or
    warned here carries it, so each one names the place in the caller's schema
    that produced it. It is required, because a type name arrives from a table
    column, a function parameter, and a function's scalar return, and the
    three read differently.

    The ``TypeError``'s closing hint describes the typed-column form because
    that is the only position reaching it: both function paths check for a
    ``str`` themselves before calling.
    """
    if not isinstance(type_name, str):
        raise TypeError(
            f"Schema {position} must be a KQL scalar type name as a "
            f"str; got {type(type_name).__name__}. The typed-column form is "
            "{table: {column: 'type'}}, for example {'T': {'c': 'long'}}."
        )
    folded = type_name.lower()
    if folded == _UNKNOWN_TYPE_NAME:
        return ScalarTypes.Unknown
    check_utf16_encodable(folded, f"Schema {position}")
    sym = ScalarTypes.GetSymbol(folded)
    if sym is None:
        warnings.warn(
            f"Schema {position}: Unknown KQL scalar type {type_name!r}; "
            "falling back to 'string'.",
            RuntimeWarning,
            stacklevel=_caller_stacklevel(),
        )
        return ScalarTypes.String
    return sym


def _warn_on_untyped_schema_string_columns(name: str, table) -> None:
    """Warn for each column Microsoft's schema-string parser left ``unknown``.

    ``TableSymbol.From("(n:bogus)")`` accepts the unrecognized name, types the
    column ``ScalarTypes.Unknown``, and returns, so without a warning the typo
    reaches the binder and resolves nothing. The equivalent dict form
    ``{"n": "bogus"}`` warns in the same category with the same attribution.
    It differs in the fallback: Microsoft's ``unknown`` is kept here, since
    substituting ``string`` would invent a type the caller never wrote.

    A bare name (``"(a)"``) lands in the same place. The documented way to
    say "untyped" is the list form ``{"T": ["a"]}``, which means ``string``;
    a name with no type inside a schema string means neither.

    The stack is walked once for the whole table. The depth is a property of
    this frame, so it cannot differ between the columns reported.
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
    arguments for ColumnSymbol..ctor", the same unnameable, schema-silent
    wording :func:`_resolve_scalar_type` heads off for the type position one
    argument to the right.
    """
    if not isinstance(column, str):
        raise TypeError(
            f"Schema column name in table {table!r} must be a str; got "
            f"{type(column).__name__} ({column!r}). Keys become the "
            "column symbol's name verbatim."
        )
    check_utf16_encodable(column, f"Schema column name in table {table!r}")
    return column


@dataclass(frozen=True)
class FunctionSchema:
    """Declare a function for the binder: its parameters and what it returns.

    Put one in the schema dict under the function's name, beside the table
    entries::

        {"SignInEvents": {"IPAddress": "string"},
         "imProcessCreate": FunctionSchema(
             parameters=(("starttime", "datetime"), ("endtime", "datetime")),
             returns="(TimeGenerated:datetime, ActorUsername:string)",
             required=0)}

    ``returns`` accepts any table value form :func:`build_global_state` takes
    for a table (a ``{column: type}`` dict, a ``[column, ...]`` list, or a
    ``"(col:type, ...)"`` schema string) and declares a tabular function whose
    result carries those columns. Any other string is a KQL scalar type name
    and declares a scalar function. ``None`` declares a tabular function whose
    result columns are open, so every column a caller reads off the result
    resolves and none of them is checked.

    ``parameters`` is ``(name, scalar type name)`` pairs in declaration order.
    ``required`` is how many leading parameters a call has to pass; ``None``
    means all of them. A call that passes fewer gets Microsoft's arity
    diagnostic.

    The key this schema sits under becomes the function symbol's name
    verbatim. Microsoft's binder resolves a built-in of the same name ahead of
    the declaration, so pick a name that is not already a built-in.
    """

    parameters: tuple[tuple[str, str], ...] = ()
    returns: str | dict[str, str] | list[str] | None = None
    required: int | None = None


def _build_table_symbol(name: str, cols):
    """Build a TableSymbol from the supported schema-value forms."""
    if not isinstance(name, str):
        raise TypeError(
            f"Schema table name must be a str; got {type(name).__name__} "
            f"({name!r}). Keys become the table symbol's name verbatim."
        )
    check_utf16_encodable(name, "Schema table name")
    if isinstance(cols, str):
        # ``TableSymbol.From`` accepts ``"("``, ``"junk"`` and ``"(a:long"``,
        # but raises ``System.InvalidOperationException`` on an empty or
        # whitespace-only string: a CLR type a caller cannot name without
        # importing from the CLR, or catch except by bare ``except Exception``.
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
            ColumnSymbol(
                _check_column_name(c, name),
                _resolve_scalar_type(t, position=f"column type for column {c!r}"),
            )
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


def _build_function_parameters(name: str, spec: FunctionSchema):
    """Build the ``Parameter`` list for one function declaration.

    ``minOccurring=0`` is what makes a parameter optional to Microsoft's
    binder. With the constructor's own default, a call that omits the
    parameter gets Microsoft's arity diagnostic.

    The name and the type are checked here, before either one reaches
    :func:`_resolve_scalar_type` or the CLR. ``Parameter(5, ...)`` surfaces as
    pythonnet's "No method matches given arguments", and the ``TypeError``
    :func:`_resolve_scalar_type` raises closes with a hint about the
    typed-column form, which is the wrong advice for a parameter.
    """
    required = spec.required
    try:
        params = tuple(spec.parameters)
    except TypeError:
        raise TypeError(
            f"FunctionSchema.parameters for function {name!r} must be a "
            "sequence of (name, type) pairs; got "
            f"{type(spec.parameters).__name__}. Pass () for a function that "
            "takes no parameters."
        ) from None
    if required is not None:
        if isinstance(required, bool) or not isinstance(required, int):
            raise TypeError(
                f"FunctionSchema.required for function {name!r} must be an int "
                f"or None; got {type(required).__name__}."
            )
        if not 0 <= required <= len(params):
            raise ValueError(
                f"FunctionSchema.required for function {name!r} is {required}, "
                f"but the declaration has {len(params)} parameter(s). It counts "
                "the leading parameters a call has to pass."
            )
    out = []
    for index, item in enumerate(params):
        try:
            pname, ptype = item
        except (TypeError, ValueError):
            raise TypeError(
                f"Parameter {index} of function {name!r} must be a "
                f"(name, type) pair; got {item!r}."
            ) from None
        if not isinstance(pname, str):
            raise TypeError(
                f"Parameter {index} name of function {name!r} must be a str; "
                f"got {type(pname).__name__}. It becomes the parameter "
                "symbol's name verbatim."
            )
        if not isinstance(ptype, str):
            raise TypeError(
                f"Parameter {pname!r} of function {name!r} must name a KQL "
                f"scalar type as a str; got {type(ptype).__name__}."
            )
        check_utf16_encodable(pname, f"Parameter name in function {name!r}")
        min_occurring = 0 if required is not None and index >= required else 1
        out.append(
            Parameter(
                pname,
                _resolve_scalar_type(
                    ptype,
                    position=f"type for parameter {pname!r} of function {name!r}",
                ),
                minOccurring=min_occurring,
            )
        )
    return out


def _function_return_symbol(name: str, returns):
    """Resolve a :attr:`FunctionSchema.returns` value to a return symbol.

    A tabular return is a ``TableSymbol`` and goes through
    :func:`_build_table_symbol`, so the message for a malformed one names the
    declaration as a table.
    """
    if returns is None:
        # An open table symbol resolves every column a caller reads off the
        # result and checks none of them, so a declaration that names only the
        # signature produces no false diagnostics.
        return TableSymbol.From("()").WithIsOpen(True)
    if isinstance(returns, (dict, list, tuple)) or (
        isinstance(returns, str) and returns.lstrip().startswith("(")
    ):
        return _build_table_symbol(name, returns)
    if isinstance(returns, str):
        return _resolve_scalar_type(
            returns, position=f"return type of function {name!r}"
        )
    raise TypeError(
        f"Unsupported FunctionSchema.returns for function {name!r}: "
        f"{type(returns).__name__}. Use a KQL scalar type name, a tabular "
        "spec (dict {col: type}, list [col, ...], or '(col:type, ...)'), or "
        "None for an open tabular result."
    )


def _build_function_symbol(name: str, spec: FunctionSchema):
    """Build a FunctionSymbol from a :class:`FunctionSchema`.

    ``FunctionSymbol`` reads the tabularity off the return symbol: a
    ``TableSymbol`` return declares a tabular function and a scalar type
    declares a scalar one, so no tabularity argument is passed.
    """
    if not isinstance(name, str):
        raise TypeError(
            f"Schema function name must be a str; got {type(name).__name__} "
            f"({name!r}). Keys become the function symbol's name verbatim."
        )
    check_utf16_encodable(name, "Schema function name")
    return FunctionSymbol(
        name,
        _function_return_symbol(name, spec.returns),
        _build_function_parameters(name, spec),
    )


def extract_schemas_from_global_state(global_state) -> dict[str, dict[str, str]]:
    """Walk a Microsoft ``GlobalState`` and return ``{table: {col: type}}``.

    Inverse of :func:`build_global_state`. ``KustoQuery.to_ir(attach_schema=True)``
    uses it to recover the schema dict ``SchemaAttacher`` wants without making
    the caller keep a Python copy alongside the bound ``KustoCode``.
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
      * dict ``{name: FunctionSchema(...)}`` — a declared function

    One dict carries both kinds. A :class:`FunctionSchema` value declares a
    function under its key; every other value declares a table.

    Every key is a raw name. Table keys and column keys become the ``Name`` of
    a ``TableSymbol`` or ``ColumnSymbol`` verbatim. The bracket-quoting
    ``["my col"]`` / ``['my col']`` is KQL query syntax for a name that is not
    a bare identifier; as a key it is taken literally, so
    ``{"T": {"['my col']": "string"}}`` declares a column named by the ten
    characters ``['my col']`` that no query can reach. Write
    ``{"T": {"my col": "string"}}`` and let the query do the quoting.

    Type names are case-insensitive (``"LONG"`` is ``long``), and ``"unknown"``
    is accepted as Microsoft's own name for "no type", so the output of
    :func:`extract_schemas_from_global_state` round-trips. An unrecognized name
    falls back to ``string`` with a ``RuntimeWarning``. Inside a schema string
    it stays Microsoft's ``unknown``, also with a ``RuntimeWarning``.

    Wrong-typed input raises before reaching the CLR. A non-``str`` table name,
    column name or type name is a ``TypeError``, as is a table value that is
    none of the table forms above; an empty or whitespace-only schema string is
    a ``ValueError``. A :class:`FunctionSchema` raises the same two from the
    same positions inside the declaration: a parameter that is not a
    ``(name, type)`` pair of strings is a ``TypeError``, and a ``required``
    outside ``0..len(parameters)`` is a ``ValueError``. A name, type, or schema
    string holding an unpaired surrogate is a ``ValueError`` too: UTF-16 cannot
    encode one, and pythonnet's failure to marshal it aborts the process. Every
    message names the position it rejects.
    """
    if not isinstance(schema, dict):
        raise TypeError(
            "schema must be a dict mapping each name to a column spec or a "
            f"FunctionSchema; got {type(schema).__name__}."
        )
    tables = []
    functions = []
    for name, value in schema.items():
        if isinstance(value, FunctionSchema):
            functions.append(_build_function_symbol(name, value))
        else:
            tables.append(_build_table_symbol(name, value))
    return GlobalState.Default.WithDatabase(
        DatabaseSymbol("NetDB", tables + functions)
    )
