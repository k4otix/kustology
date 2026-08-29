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

from .._text import Utf16Offsets
from .spans import Span
from .types import KustoType
from .walk import find_all

logger = logging.getLogger(__name__)

# ``+ - * / %``. Kept here rather than in ``builder`` because the binder needs
# the same set: an arithmetic ``BinOp`` is not a predicate, and typing one
# ``bool`` is a wrong answer rather than an incomplete one.
ARITHMETIC_OPS = frozenset({"+", "-", "*", "/", "%"})


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
    """Map a .NET Kusto type name (for example ``"long"``) to a :class:`KustoType`."""
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
    exposes it, and a ``getattr`` probe for a member like ``IsNullable``
    returns its default silently -- a field fed that way keeps its declared
    default on every node ever built while reading as implemented.
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
    # ``dynamic<T>``, and it is the member to probe: neither ``Underlying``
    # nor ``Element`` is a property on any Symbol in ``Kusto.Language``
    # (``Element`` exists only on the ``SeparatedElement`` syntax wrappers),
    # so probing either leaves this field None on every node ever built.
    inner = getattr(res_type, "ElementType", None)
    if inner is not None:
        try:
            inner_name = getattr(inner, "Name", None)
            if inner_name:
                expr.result_type_inner = map_net_type(str(inner_name))
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("inner result-type probe fell through: %s", e)


def to_span(node: Any) -> Span:
    """Convert a .NET node's TextStart/Width into a :class:`Span`.

    The offsets are Microsoft's, so they count UTF-16 code units at this
    point. :func:`retarget_spans_to_codepoints` converts the whole tree once
    the build is finished. Translating per node would need the query text at
    every call site and would save nothing.
    """
    return Span(text_start=node.TextStart, width=node.Width)


def retarget_spans_to_codepoints(root: Any, offsets: Utf16Offsets) -> None:
    """Rewrite every :class:`Span` under ``root`` from UTF-16 to code points.

    .NET counts string offsets in UTF-16 code units; a Python ``str`` is
    indexed by code point. The two agree until an astral character appears,
    after which every later offset is too large by one per astral character
    before it, and ``Span.text(query)`` returns the wrong slice with no error.

    The conversion needs the query text, which the builder holds and the
    individual visit methods do not, so it runs as one pass over the finished
    IR.

    All-BMP text returns immediately, so the common case pays one length
    comparison and no traversal.
    """
    if offsets.is_identity:
        return
    for span in find_all(root, Span):
        span.text_start, span.width = offsets.span_to_codepoints(
            span.text_start, span.width,
        )


def read_row_schema(node: Any) -> list[tuple[str, str]]:
    """Read a ``RowSchema``-bearing node into ``[(column_name, type), …]``.

    The one reader for every ``name:type`` declaration list in the grammar:
    ``datatable(a:int, b:string)``, ``externaldata(a:string)``,
    ``| assert-schema (a:long)``, ``| parse-kv … as (a:long)``, and
    ``evaluate … : (a:long)``. The two dict-shaped consumers wrap the result
    in ``dict(...)``; the shape difference is the *only* thing that differs
    between call sites.

    Sharing one reader is the point rather than a tidy-up: the column type
    is read with ``node_text`` (``IncludeTrivia.Minimal``), never a bare
    ``ToString()`` — which is ``IncludeTrivia.All`` and prepends the node's
    leading trivia, so ``assert-schema (a: // note\\n long)`` would record
    the type as ``"// note\\n long"`` and hash differently from the
    identical query without the comment. One reader keeps that rule in one
    place; a per-site copy is a copy free to lose it.

    ``node`` may be either the ``RowSchema`` itself or the node that owns
    one -- the operator or expression -- and the ambiguity is deliberate,
    because the owning member has two names: ``Schema`` on ``datatable``,
    ``externaldata`` and ``assert-schema``, ``Keys`` on ``ParseKvOperator``.
    A caller that pre-extracts either one is right, and so is a caller that
    passes the owner.

    Accepting both shapes closes a silent failure, not a convenience gap: a
    caller passing the shape this function does not take would get an
    **empty list with no exception** -- silently, exactly the dropped-schema
    collapse this function exists to prevent. A contract whose two readings
    differ only by which one silently yields nothing is not one to leave to
    documentation.
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
    """Return True when a name node is a ``WildcardedName`` (``T*``, ``*``).

    ``T*`` and the bracketed literal ``['T*']`` both parse to a
    ``NameReference`` whose ``visit_name`` is the string ``"T*"``; only the
    inner name node's ``Kind`` separates the pattern from the table that
    happens to be called that.
    """
    if name_node is None:
        return False
    return str(getattr(name_node, "Kind", "")) == "WildcardedName"


def _qualifier_argument(node: Any) -> tuple[str, str] | None:
    """Read ``database('d')`` into ``("database", "d")``; anything else is ``None``.

    The argument is read through ``LiteralValue`` rather than the node's
    text so the quoting style (``'d'`` / ``"d"``) does not reach the IR.
    The fallback uses ``node_text`` (``IncludeTrivia.Minimal``) rather than
    ``ToString()`` (``IncludeTrivia.All``) so a comment before the argument
    cannot reach the hash. ``LiteralValue`` wins for every spelling a
    comment could currently precede here, so this is hardening against a
    latent hazard rather than a live one -- the same defect class the URI
    fallback in :func:`read_external_data` guards against.
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
    still yields its trailing name.
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


def read_external_data(
    node: Any,
) -> tuple[list[tuple[str, str]], list[str], str | None, dict[str, str]]:
    """Read an ``ExternalDataExpression`` into ``(columns, uris, format, props)``.

    Shared by the source-position (:class:`ExternalDataSource`) and
    expression-position (:class:`ExternalDataExpr`) branches of the builder,
    which is the point: one reader means the two positions cannot disagree
    about what the node says.

    URIs come from ``el.LiteralValue`` because the DLL decodes KQL's
    obfuscated string literal for us -- ``h"https://x"`` reads back as
    ``https://x``, where the node's own text keeps the ``h`` and the quotes.

    **The text fallback is reachable and is not a URI.** An element the
    parser cannot fold to a literal has no ``LiteralValue``, so ``uris``
    records that element's *source text* instead: a ``let``-bound feed URL
    (``let u = "https://x"; externaldata(a:string)[u]``) yields ``["u"]``,
    and ``externaldata(a:string)[strcat("https://","x")]`` yields the whole
    call as written. A consumer resolving these has to read the query, not
    only the field. The fallback uses ``node_text``
    (``IncludeTrivia.Minimal``) rather than ``ToString()``, which is
    ``IncludeTrivia.All``: the latter would record the URI of
    ``externaldata(a:string)[// note\\n u]`` as ``"// note\\n u"``, hashing
    it apart from the same query without the comment. A comment
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

    # ``with (format="csv", ignoreFirstRecord=true)``. Every property is
    # kept, not only the format: ``ignoreFirstRecord`` skips the CSV header,
    # so it changes the rows the feed yields -- and a source node carries no
    # ``raw_text``, so a property dropped here reaches nothing at all and
    # two feeds parsed differently build one node with one ``semantic_hash``.
    #
    # ``read_named_params`` is the same reader ``RenderOp.properties`` uses
    # for the same job, so the two cannot drift; beyond ``LiteralValue`` and
    # a text fallback it also resolves a bare ``NameReference`` value and
    # guards the ``FunctionCallExpression``-has-a-``Name`` trap.
    with_clause = getattr(node, "WithClause", None)
    properties = read_named_params(
        getattr(with_clause, "Properties", None) if with_clause is not None else None
    )

    # ``format`` is promoted to its own field: it is the one property the
    # rest of the library reads. The name is matched case-insensitively
    # because the grammar is, while the dict keeps whatever casing the query
    # wrote -- the same verbatim-keys rule ``hints`` and ``RenderOp`` follow.
    fmt: str | None = next(
        (v for k, v in properties.items() if k.lower() == "format"), None
    )
    return columns, uris, fmt, properties


def is_table_symbol(sym: Any) -> bool:
    """Return True when ``sym`` is a Kusto TableSymbol (or a structural equivalent)."""
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


def table_symbol_columns(sym: Any) -> dict[str, str] | None:
    """Return ``{column: type}`` for a **closed** ``TableSymbol``, else ``None``.

    This is the whole of "ask Microsoft what this operator returns": the
    binder puts a ``TableSymbol`` on every tabular node's ``ResultType``, and
    its ``Columns`` are the names and types the node emits, in order.

    ``IsOpen`` is the reason for the guard, and it is Microsoft's own flag
    rather than a heuristic of ours: an *open* symbol means the binder could
    not determine the full column set, which is the state every symbol
    downstream of an unknown table is in. An open symbol still lists
    columns -- the ones the query happened to name, typed ``unknown`` -- so
    reading ``Columns`` without checking would dress a partial guess up as
    the binder's answer. It would also be actively harmful downstream:
    ``IRBuilder().build(q)`` binds against ``GlobalState.Default``, which
    knows no tables at all, so every operator would carry an
    ``unknown``-typed schema that then overrode the real one a caller hands
    to :class:`~kustology.ir.binder.SchemaAttacher` afterwards.

    Returns ``None`` -- not ``{}`` -- for anything that is not a closed
    table symbol, because an empty schema is a legitimate answer
    (``T | project-away *``) and the caller has to be able to tell "no
    columns" from "no answer".

    The ``is_table_symbol`` call is what makes the first sentence true.
    ``TupleSymbol`` -- what ``arg_max(a, *)`` puts on the aggregate
    expression -- also carries ``Columns``, and carries no ``IsOpen`` at all,
    so ``getattr(sym, "IsOpen", False)`` reads it as a *closed table*. Only
    operator and pipeline nodes call in here today and those carry a
    ``TableSymbol``, so nothing routes one in; the guard is here because the
    docstring promises it, not because a call site currently needs it.
    """
    if not is_table_symbol(sym):
        return None
    columns = getattr(sym, "Columns", None)
    if columns is None:
        return None
    if getattr(sym, "IsOpen", False):
        return None
    out: dict[str, str] = {}
    for i in range(columns.Count):
        column = columns[i]
        out[str(column.Name)] = str(column.Type.Name)
    return out


def read_to_typeof(node: Any) -> str | None:
    """Return the type named by an expression's ``to typeof(T)`` clause, if any.

    Called on an ``MvExpandExpression`` or an ``MvApplyExpression`` -- both
    carry the same ``ToTypeOf`` member, which is why the reader is written
    against the member rather than either node class. ``MvExpandOp`` stores
    one result per column (``MvExpandColumn.to_typeof``); ``MvApplyOp``
    folds every column's clause into a single field, since the operator has
    no per-column type story of its own -- see ``MvApplyOp`` for why.

    ``MvExpandExpression.ToTypeOf`` is a ``ToTypeOfClause`` whose ``TypeOf``
    is the whole ``typeof(string)`` literal expression -- rendering *that*
    node would record ``"typeof(string)"``, so the type name comes from its
    ``Types`` list. The clause text is the fallback for a shape the parser
    recovers differently.

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
    """Return the name a ``NamedParameter`` declares, or ``None`` if it has none."""
    name_node = getattr(param, "Name", None)
    if name_node is None:
        return None
    return str(getattr(name_node, "SimpleName", None) or visit_name(name_node))


def named_param_value(param: Any) -> str | None:
    """Return the value a ``NamedParameter`` carries, rendered as a string.

    Also called on ``GetSchemaOperator.KindParameter``, which is not a
    ``NamedParameter`` -- it is the operator's one singular slot rather than
    a list element -- but carries the same ``Name``/``Expression`` shape, so
    nothing here has to change to read it.

    Three shapes, in the order they have to be tried. A bare name
    (``withsource=S``, ``kind=inner``) is a ``NameReference`` and the
    identifier is what the caller wants, not the node's text. A literal
    (``title="a"``, ``isfuzzy=true``) reads back through ``LiteralValue``,
    which is already unquoted and decoded. Anything else -- an expression
    the parser did not fold -- falls back to its source text, read with
    ``node_text`` (``IncludeTrivia.Minimal``) rather than ``ToString()``,
    which is ``IncludeTrivia.All`` and would carry a preceding comment into
    the value and from there into ``semantic_hash``.

    **The bare-name branch is gated on the node class, not on the presence
    of a ``Name`` member**, and that is the whole reason the check is
    written this way. ``FunctionCallExpression`` also has a ``Name`` -- the
    function's -- so an unguarded probe would record
    ``with (title=strcat("a","b"))`` as ``title="strcat"``: not what the
    query says, and identical for every call whatever its arguments, so two
    differently-titled charts would share a ``semantic_hash``. Only a
    ``NameReference`` means "the identifier is the value"; everything else
    belongs to the two branches below, and ``node_text`` renders the call as
    written.
    """
    from ..utils.walker import node_text

    expr = getattr(param, "Expression", None)
    if expr is None:
        return None
    if str(type(expr).__name__) == "NameReference":
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
    """Return every ``hint.*`` named parameter an operator carries.

    Keys keep the ``hint.`` prefix, because that is what the query wrote and
    stripping it would make ``hint.remote`` indistinguishable from a
    hypothetical plain ``remote=``. Non-hint parameters are left for the
    operator's own fields -- ``kind=`` changes what the operator *does* and
    is modeled; a hint only changes how the engine runs it.

    The prefix match is **case-sensitive**, matching the grammar rather than
    being lenient about it. ``HINT.strategy=shuffle``, ``hint.STRATEGY=``
    and ``Hint.Strategy=`` are none of them named parameters in the bundled
    grammar: each fails to parse as one and is diagnosed rather than
    accepted (verified on 12.4.1). A
    case-insensitive match therefore cannot admit anything extra, and
    pairing one with verbatim keys would be the worse of both -- two
    dictionary entries for one hint, the first time a parser accepts a
    second casing.
    """
    return {
        name: value
        for name, value in read_named_params(getattr(node, "Parameters", None)).items()
        if name.startswith("hint.")
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
    ``semantic_hash`` through ``_normalize.canonical``, so an unpinned
    rendering makes the hash a function of the host locale. Explicit format
    specifiers remove the dependency and, for datetimes, make the value
    round-trippable:

    * ``"o"`` — ISO 8601 round-trip, for example
      ``2024-01-01T00:00:00.0000000Z`` (every datetime literal is
      Kind-normalized to UTC before rendering, so the ``Z`` suffix is
      unconditional -- see the datetime branch below)
    * ``"c"`` — invariant TimeSpan constant form, tick-precise, for example
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
        # *converted* back to UTC, not only relabeled. Swapping these two
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
    # first property access, so for decimal a non-invariant ambient culture
    # in effect at *parse* time can corrupt the parsed value before this
    # function ever runs (for example treating the decimal point as a group
    # separator); no ToString() argument can recover a value that was
    # mis-parsed upstream. What actually protects decimal is the import-time
    # culture pin in ``bridge._pin_invariant_culture`` — this argument only
    # removes one more ambient-culture dependency from the rendering step
    # itself.
    return raw.ToString(None, CultureInfo.InvariantCulture), None


# --- aggregate output naming -------------------------------------------------
#
# Most of an unnamed aggregate's auto-name comes from Microsoft's own
# ``ResultNameKind``/``ResultNamePrefix`` symbol properties (see
# ``IRBuilder._function_result_name`` in builder.py, a port of
# ``GetFunctionResultName``). The two sets below are the cases that symbol
# read cannot answer -- not because the port is incomplete, but because
# Microsoft's own ``ResultNameKind`` is ``None`` for these functions too, so
# they have to be listed by hand.

# Aggregates that emit their argument columns under the columns' own names,
# optionally with the symbol's own ``ResultNamePrefix`` in front: ``take_any(a)``
# is ``a``, ``arg_max(t, *)`` starts with ``t``, and ``argmin(s)`` -- the
# same multi-output shape as ``arg_min``, but with ``ResultNamePrefix="min"``
# -- is ``min_s``. These are the multi-output (``TupleSymbol``) aggregates:
# Microsoft's own ``ResultNameKind`` is ``None`` for every one of them,
# because no single symbol property can name a star's worth of columns
# (``AddFunctionTupleResultColumn`` names each member instead), so which
# column the ``Assignment`` borrows its name from stays a hand-written rule
# -- the prefix, when there is one, is still read off the symbol.
COLUMN_NAMED_AGGREGATES = frozenset({
    "arg_max", "arg_min", "argmin", "argmax", "take_any", "take_anyif",
})

# The whole percentile family's ``ResultNameKind`` is ``None`` too: the
# value suffix (``percentile(a, 95)`` -> ``percentile_a_95``) is not a symbol
# property Microsoft exposes, so its spelling stays hand-written as well.
PERCENTILE_AGGREGATES = frozenset({
    "percentile", "percentilew", "percentiles", "percentilesw",
})


def percentile_token(value: Any) -> str:
    """Spell a percentile argument the way KQL spells it in a column name.

    ``percentile(a, 95)`` is ``percentile_a_95`` and ``percentile(a, 95.5)``
    is ``percentile_a_95_5`` -- the decimal point becomes an underscore,
    since a column name cannot hold one.
    """
    text = str(value)
    text = text.removesuffix(".0")
    return text.replace(".", "_")
