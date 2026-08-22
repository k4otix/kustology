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
    """Copy ResultType and the dynamic-element type from the binder.

    Nullability is deliberately absent: no type in ``Kusto.Language``
    exposes it. The probe that used to read ``res_type.IsNullable`` named a
    member that does not exist, so ``Expr.nullable`` was ``True`` on every
    node ever built while its declaration claimed the binder would flip it.
    """
    res_type = getattr(node, "ResultType", None)
    if res_type is None:
        return
    try:
        type_name = res_type.Name
    except AttributeError:  # pragma: no cover
        return
    expr.result_type = map_net_type(type_name)
    # ``ElementType`` on ``DynamicArraySymbol`` is the element type of a
    # ``dynamic<T>``. The previous probe read ``Underlying`` or ``Element``:
    # neither is a property on any type in ``Kusto.Language`` -- ``Element``
    # exists only on the ``SeparatedElement`` syntax wrappers, never on a
    # Symbol -- so this field was None on every node ever built.
    inner = getattr(res_type, "ElementType", None)
    if inner is not None:
        try:
            inner_name = getattr(inner, "Name", None)
            if inner_name:
                expr.result_type_inner = map_net_type(str(inner_name))
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("inner result-type probe fell through: %s", e)


def to_span(node: Any) -> Span:
    """Convert a .NET node's TextStart/Width into a :class:`Span`."""
    return Span(text_start=node.TextStart, width=node.Width)


def is_wildcarded_name(name_node: Any) -> bool:
    """True when a name node is a ``WildcardedName`` (``T*``, ``*``).

    ``T*`` and the bracketed literal ``['T*']`` both parse to a
    ``NameReference`` whose ``visit_name`` is the string ``"T*"``; only the
    inner name node's ``Kind`` separates the pattern from the table that
    happens to be called that.
    """
    if name_node is None:
        return False
    return str(getattr(name_node, "Kind", "")) == "WildcardedName"


def _qualifier_argument(node: Any) -> tuple[str, str] | None:
    """``database('d')`` -> ``("database", "d")``; anything else -> ``None``.

    The argument is read through ``LiteralValue`` rather than the node's
    text so the quoting style (``'d'`` / ``"d"``) does not reach the IR.
    """
    if str(type(node).__name__) != "FunctionCallExpression":
        return None
    name_node = getattr(node, "Name", None)
    if name_node is None:
        return None
    fname = str(getattr(name_node, "SimpleName", None) or visit_name(name_node)).lower()
    if fname not in ("database", "cluster"):
        return None
    arg_list = getattr(node, "ArgumentList", None)
    exprs = getattr(arg_list, "Expressions", None) if arg_list is not None else None
    if exprs is None or exprs.Count == 0:
        return None
    first = exprs[0]
    first = getattr(first, "Element", first)
    value = getattr(first, "LiteralValue", None)
    if value is None:
        return fname, first.ToString().strip().strip("@").strip("\"'")
    return fname, str(value)


def extract_qualified_table_ref(
    node: Any,
) -> tuple[str | None, str | None, str, bool] | None:
    """Decompose a qualified table ``PathExpression`` into its parts.

    Returns ``(cluster, database, name, is_wildcard)``, or ``None`` when the
    node is not a path ending in a name.

    ``cluster("c").database("d").T`` nests left-associatively -- the outer
    ``PathExpression``'s ``Expression`` is itself a ``PathExpression`` whose
    ``Selector`` is the ``database(...)`` call -- so the qualifiers are
    collected by walking that left spine rather than by reading two fixed
    positions. A plain dotted path (``A.B.T``) contributes no qualifiers and
    still yields its trailing name, which is what the previous
    ``extract_qualified_table_name`` returned for every one of these shapes.
    """
    if str(type(node).__name__) != "PathExpression":
        return None
    sel = getattr(node, "Selector", None)
    if sel is None or str(type(sel).__name__) != "NameReference":
        return None
    name_node = getattr(sel, "Name", None)
    name = visit_name(name_node)
    is_wildcard = is_wildcarded_name(name_node)

    cluster: str | None = None
    database: str | None = None

    def record(candidate: Any) -> None:
        nonlocal cluster, database
        qualifier = _qualifier_argument(candidate)
        if qualifier is None:
            return
        which, value = qualifier
        if which == "cluster":
            cluster = value
        else:
            database = value

    left = getattr(node, "Expression", None)
    while left is not None:
        if str(type(left).__name__) == "PathExpression":
            record(getattr(left, "Selector", None))
            left = getattr(left, "Expression", None)
            continue
        record(left)
        break
    return cluster, database, name, is_wildcard


def read_external_data(node: Any) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """Read an ``ExternalDataExpression`` into ``(columns, uris, format)``.

    Shared by the source-position (:class:`ExternalDataSource`) and
    expression-position (:class:`ExternalDataExpr`) branches of the builder,
    which is the point: they used to read the node separately and the two
    readings could disagree.

    URIs come from ``el.LiteralValue`` because the DLL decodes KQL's
    obfuscated string literal for us -- ``h"https://x"`` reads back as
    ``https://x``, where the node's own text keeps the ``h`` and the quotes.
    The text fallback exists for a URI expression the parser could not fold
    to a literal at all.

    Column types are read with ``node_text`` (``IncludeTrivia.Minimal``) and
    not ``ToString()``, which is ``IncludeTrivia.All`` and prepends the
    node's leading trivia: ``externaldata(a: // note\\n string)`` recorded
    the type as ``"// note\\n string"`` and hashed differently from the
    identical query without the comment.
    """
    from ..utils.walker import iter_elements, node_text

    columns: list[tuple[str, str]] = []
    schema_node = getattr(node, "Schema", None)
    if schema_node is not None and hasattr(schema_node, "Columns"):
        for col in iter_elements(schema_node.Columns):
            type_node = getattr(col, "Type", None)
            columns.append((
                visit_name(col),
                node_text(type_node).strip() if type_node is not None else "unknown",
            ))

    uris: list[str] = []
    uri_nodes = getattr(node, "URIs", None)
    if uri_nodes is not None:
        for el in iter_elements(uri_nodes):
            value = getattr(el, "LiteralValue", None)
            if value is None:
                uris.append(el.ToString().strip().strip("@").strip("\"'"))
            else:
                uris.append(str(value))

    # ``with (format="csv", ignoreFirstRecord=true)`` -- only the format is
    # modeled; the rest stay in the source text.
    fmt: str | None = None
    with_clause = getattr(node, "WithClause", None)
    if with_clause is not None:
        for prop in iter_elements(with_clause.Properties):
            if visit_name(prop.Name).lower() != "format":
                continue
            value = getattr(prop.Expression, "LiteralValue", None)
            fmt = (
                str(value) if value is not None
                else prop.Expression.ToString().strip().strip("\"'")
            )
            break
    return columns, uris, fmt


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


# Microsoft gives every literal its own SyntaxKind. Reading it is exact;
# re-inferring from the Python type of LiteralValue is not — it cannot tell
# long from real, and collapses datetime, timespan and guid into "string".
_LITERAL_KIND_BY_NET_KIND = {
    "StringLiteralExpression": "string",
    "BooleanLiteralExpression": "bool",
    "LongLiteralExpression": "long",
    "IntLiteralExpression": "int",
    "RealLiteralExpression": "real",
    "DecimalLiteralExpression": "decimal",
    "DateTimeLiteralExpression": "datetime",
    "TimespanLiteralExpression": "timespan",
    "GuidLiteralExpression": "guid",
    "NullLiteralExpression": "null",
}


def literal_kind_for(node: Any) -> str:
    """Map a .NET literal node's ``SyntaxKind`` to an IR ``literal_kind``.

    Typed nulls (``int(null)``, ``datetime(null)``) keep the SyntaxKind of
    their declared type but carry a null ``LiteralValue``; they report as
    ``"null"`` so consumers need only one check.
    """
    if node.LiteralValue is None:
        return "null"
    return _LITERAL_KIND_BY_NET_KIND.get(str(node.Kind), "string")


def literal_value_and_ticks(node: Any) -> tuple[Any, int | None]:
    """Return ``(value, ticks)`` for a .NET literal node, culture-independently.

    ``.ToString()`` with no format specifier renders through the ambient
    culture: the same datetime becomes ``1/1/2024 12:00:00 AM`` under en-US
    (with a U+202F narrow no-break space), ``01.01.2024 00:00:00`` under de-DE
    and ``2024/01/01 0:00:00`` under ja-JP. Those strings reach
    ``semantic_hash`` through ``_normalize.canonical``, which made the hash
    depend on the host locale. Explicit format specifiers remove the
    dependency and, for datetimes, make the value round-trippable:

    * ``"o"`` — ISO 8601 round-trip, e.g. ``2024-01-01T00:00:00.0000000Z``
      (every datetime literal is Kind-normalized to UTC before rendering, so
      the ``Z`` suffix is unconditional -- see the datetime branch below)
    * ``"c"`` — invariant TimeSpan constant form, tick-precise, e.g.
      ``1.12:00:00`` and ``00:00:00.0000002``

    ``ticks`` is populated for datetime and timespan only.
    """
    from System import DateTime, DateTimeKind
    from System.Globalization import CultureInfo

    raw = node.LiteralValue
    if raw is None:
        return None, None

    net_kind = str(node.Kind)
    if net_kind == "DateTimeLiteralExpression":
        # .NET's default ``DateTime.Parse`` (what ``LiteralValue`` uses under
        # the hood) hands back one of two kinds, and they need opposite
        # treatment. A bare literal like ``datetime(2024-01-01)`` has no
        # offset in the source text, so it parses as ``Unspecified`` --
        # correct as-is, since KQL datetimes are UTC by definition; it only
        # needs the ``Kind`` tag *set*, not the value touched, or ``.Ticks``
        # would render un-suffixed and collide with nothing. A ``Z``- or
        # offset-suffixed literal like ``datetime(2024-01-01T00:00:00Z)``
        # instead parses as ``Local`` -- .NET silently converts it to the
        # *host's* wall-clock time and stamps it accordingly, so ``.Ticks``
        # already carries the host's UTC offset baked in and must be
        # *converted* back to UTC, not just relabelled. Swapping these two
        # branches would silently shift every non-UTC-offset timestamp by
        # the host's offset while leaving ones that already said "Z" alone.
        if raw.Kind == DateTimeKind.Local:
            raw = raw.ToUniversalTime()
        elif raw.Kind == DateTimeKind.Unspecified:
            raw = DateTime.SpecifyKind(raw, DateTimeKind.Utc)
        return raw.ToString("o", CultureInfo.InvariantCulture), raw.Ticks
    if net_kind == "TimespanLiteralExpression":
        return raw.ToString("c", CultureInfo.InvariantCulture), raw.Ticks
    if isinstance(raw, (str, int, float, bool)):
        return raw, None
    # Covers decimal and guid (and anything else with no dedicated branch
    # above). ``None`` as the format keeps each type's default format ("G"
    # for decimal, "D" for guid); the two-argument overload is required
    # because Guid has no single-argument ToString(IFormatProvider).
    #
    # Passing InvariantCulture here only pins how the value *renders* — it
    # does not protect the value itself. LiteralValue is parsed lazily on
    # first property access, so for decimal (as for the timespan literals
    # Task 1 fixed) a non-invariant ambient culture already in effect at
    # *parse* time can corrupt the parsed value before this function ever
    # runs (e.g. treating the decimal point as a group separator); no
    # ToString() argument can recover a value that was mis-parsed upstream.
    # What actually protects decimal is the import-time culture pin in
    # ``bridge._pin_invariant_culture`` — this argument only removes one
    # more ambient-culture dependency from the rendering step itself.
    return raw.ToString(None, CultureInfo.InvariantCulture), None

