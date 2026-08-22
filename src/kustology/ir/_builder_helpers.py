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


def read_row_schema(node: Any) -> list[tuple[str, str]]:
    """Read a ``RowSchema``-bearing node into ``[(column_name, type), …]``.

    The one reader for every ``name:type`` declaration list in the grammar:
    ``datatable(a:int, b:string)``, ``externaldata(a:string)``,
    ``| assert-schema (a:long)`` and ``| parse-kv … as (a:long)``. The two
    dict-shaped consumers wrap the result in ``dict(...)``; the shape
    difference is the *only* thing that ever differed between them.

    Sharing it is the point rather than a tidy-up. This loop existed in four
    independent copies and three of them read the column type with a bare
    ``ToString()`` — which is ``IncludeTrivia.All`` and prepends the node's
    leading trivia, so ``assert-schema (a: // note\\n long)`` recorded the
    type as ``"// note\\n long"`` and hashed differently from the identical
    query without the comment. Fixing four copies leaves a fifth copy free
    to reintroduce it; there is now nothing to copy.

    ``node`` may be either the ``RowSchema`` itself or the node that owns
    one -- the operator or expression -- and the ambiguity is deliberate,
    because the owning member has two names: ``Schema`` on ``datatable``,
    ``externaldata`` and ``assert-schema``, ``Keys`` on ``ParseKvOperator``.
    A caller that pre-extracts either one is right, and so is a caller that
    passes the owner.

    Accepting both is a correction rather than a convenience. The docstring
    used to say the owner was the required argument while the code required
    the ``RowSchema``, and getting that backwards returned an **empty list
    with no exception** -- reinstating, silently, exactly the dropped-schema
    collapse this function was extracted to prevent. A contract whose two
    readings differ only by which one silently yields nothing is not one to
    leave to documentation.
    """
    from ..utils.walker import iter_elements, node_text

    columns: list[tuple[str, str]] = []
    schema = node
    if schema is not None and not hasattr(schema, "Columns"):
        owned = getattr(node, "Schema", None)
        schema = owned if owned is not None else getattr(node, "Keys", None)
    if schema is None or not hasattr(schema, "Columns"):
        return columns
    for col in iter_elements(schema.Columns):
        type_node = getattr(col, "Type", None)
        columns.append((
            visit_name(col),
            node_text(type_node).strip() if type_node is not None else "unknown",
        ))
    return columns


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
    The fallback uses ``node_text`` (``IncludeTrivia.Minimal``) rather than
    ``ToString()`` (``IncludeTrivia.All``) so a comment before the argument
    cannot reach the hash. ``LiteralValue`` wins for every spelling a
    comment could currently precede here, so this is hardening against a
    latent hazard rather than a live one -- but it is the same defect class
    the URI fallback above really did have.
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
        from ..utils.walker import node_text
        return fname, node_text(first).strip().strip("@").strip("\"'")
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

    **The text fallback is reachable and is not a URI.** An element the
    parser cannot fold to a literal has no ``LiteralValue``, so ``uris``
    records that element's *source text* instead: a ``let``-bound feed URL
    (``let u = "https://x"; externaldata(a:string)[u]``) yields ``["u"]``,
    and ``externaldata(a:string)[strcat("https://","x")]`` yields the whole
    call as written. A consumer resolving these has to read the query, not
    just the field. The fallback uses ``node_text``
    (``IncludeTrivia.Minimal``) rather than ``ToString()``, which is
    ``IncludeTrivia.All``: with the latter, ``externaldata(a:string)[//
    note\\n u]`` recorded the URI as ``"// note\\n u"`` and hashed
    differently from the same query without the comment. A comment
    *interior* to the element -- ``strcat(// note\\n "https://","x")`` --
    still reaches the text, because no ``IncludeTrivia`` mode strips
    interior trivia; that is the same accepted boundary as
    :attr:`~kustology.ir.query.UnknownSource.raw_text`, and it splits a
    digest rather than merging two.

    Column types come from :func:`read_row_schema`, which is the single
    reader for every ``name:type`` list in the grammar.
    """
    from ..utils.walker import iter_elements, node_text

    columns = read_row_schema(getattr(node, "Schema", None))

    uris: list[str] = []
    uri_nodes = getattr(node, "URIs", None)
    if uri_nodes is not None:
        for el in iter_elements(uri_nodes):
            value = getattr(el, "LiteralValue", None)
            if value is None:
                uris.append(node_text(el).strip().strip("@").strip("\"'"))
            else:
                uris.append(str(value))

    # ``with (format="csv", ignoreFirstRecord=true)`` -- only the format is
    # modeled; the rest stay in the source text. ``node_text`` in the
    # fallback for the same reason as the URIs above: today ``LiteralValue``
    # wins for every spelling a comment could precede, so the hazard is
    # latent rather than live, but the two branches should not differ in
    # which trivia they admit.
    fmt: str | None = None
    with_clause = getattr(node, "WithClause", None)
    if with_clause is not None:
        for prop in iter_elements(with_clause.Properties):
            if visit_name(prop.Name).lower() != "format":
                continue
            value = getattr(prop.Expression, "LiteralValue", None)
            fmt = (
                str(value) if value is not None
                else node_text(prop.Expression).strip().strip("\"'")
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


def read_to_typeof(node: Any) -> str | None:
    """The type named by an ``mv-expand``/``mv-apply`` ``to typeof(T)`` clause.

    ``MvExpandExpression.ToTypeOf`` is a ``ToTypeOfClause`` whose ``TypeOf``
    is the whole ``typeof(string)`` literal expression -- rendering *that*
    node would record ``"typeof(string)"``, so the type name comes from its
    ``Types`` list, confirmed with ``dir()`` on a real parse. The clause text
    is the fallback for a shape the parser recovers differently.

    ``node_text`` (``IncludeTrivia.Minimal``) rather than ``ToString()``,
    which is ``IncludeTrivia.All``: the latter would put a comment written
    before the type into the recorded type string and from there into
    ``semantic_hash``.
    """
    from ..utils.walker import iter_elements, node_text

    clause = getattr(node, "ToTypeOf", None)
    if clause is None:
        return None
    type_of = getattr(clause, "TypeOf", None)
    types = getattr(type_of, "Types", None) if type_of is not None else None
    if types is not None and getattr(types, "Count", 0):
        rendered = ", ".join(
            text for text in (node_text(el).strip() for el in iter_elements(types)) if text
        )
        if rendered:
            return rendered
    text = node_text(clause).strip()
    if "typeof(" in text:
        return text.split("typeof(", 1)[1].strip().rstrip(")").strip()
    return text or None


def named_param_name(param: Any) -> str | None:
    """The name a ``NamedParameter`` declares, or ``None`` if it has none."""
    name_node = getattr(param, "Name", None)
    if name_node is None:
        return None
    return str(getattr(name_node, "SimpleName", None) or visit_name(name_node))


def named_param_value(param: Any) -> str | None:
    """The value a ``NamedParameter`` carries, rendered as a string.

    Three shapes, in the order they have to be tried. A bare name
    (``withsource=S``, ``kind=inner``) is a ``NameReference`` and the
    identifier is what the caller wants, not the node's text. A literal
    (``title="a"``, ``isfuzzy=true``) reads back through ``LiteralValue``,
    which is already unquoted and decoded. Anything else -- an expression
    the parser did not fold -- falls back to its source text, read with
    ``node_text`` (``IncludeTrivia.Minimal``) rather than ``ToString()``,
    which is ``IncludeTrivia.All`` and would carry a preceding comment into
    the value and from there into ``semantic_hash``.
    """
    from ..utils.walker import node_text

    expr = getattr(param, "Expression", None)
    if expr is None:
        return None
    sub = getattr(expr, "Name", None)
    if sub is not None:
        return str(getattr(sub, "SimpleName", None) or visit_name(sub))
    lit = getattr(expr, "LiteralValue", None)
    if lit is not None:
        return str(lit)
    return node_text(expr).strip()


def read_named_params(params: Any) -> dict[str, str]:
    """Read a ``NamedParameter`` list into ``{name: value}``.

    Takes the list itself (``operator.Parameters``,
    ``RenderWithClause.Properties``), since the two positions reach it under
    different member names. A duplicate name keeps the last spelling, which
    is what Kusto itself does with a repeated parameter.
    """
    from ..utils.walker import iter_elements

    out: dict[str, str] = {}
    if params is None or not getattr(params, "Count", 0):
        return out
    for param in iter_elements(params):
        name = named_param_name(param)
        value = named_param_value(param)
        if name is None or value is None:
            continue
        out[name] = value
    return out


def extract_hints(node: Any) -> dict[str, str]:
    """Every ``hint.*`` named parameter an operator carries.

    Keys keep the ``hint.`` prefix, because that is what the query wrote and
    stripping it would make ``hint.remote`` indistinguishable from a
    hypothetical plain ``remote=``. Non-hint parameters are left for the
    operator's own fields -- ``kind=`` changes what the operator *does* and
    is modelled; a hint only changes how the engine runs it.
    """
    return {
        name: value
        for name, value in read_named_params(getattr(node, "Parameters", None)).items()
        if name.lower().startswith("hint.")
    }


def extract_named_param(
    node: Any, param_name: str, default: str | None = None,
) -> str | None:
    """Walk an operator's NamedParameter list looking for ``param_name=value``.

    ``default`` is what the caller wants when the parameter is not written.
    It is ``str | None`` rather than ``str`` because the two answers are
    different statements: ``join`` has an effective default to substitute
    (``innerunique``, decision D8) while ``mv-expand``'s ``with_itemindex``
    has none -- an unwritten index column is not an index column named
    anything.
    """
    from ..utils.walker import iter_elements

    params = getattr(node, "Parameters", None)
    if not params or not getattr(params, "Count", 0):
        return default
    target = param_name.lower()
    for param in iter_elements(params):
        name = named_param_name(param)
        if name is None or name.lower() != target:
            continue
        value = named_param_value(param)
        if value is None:
            continue
        return value
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

