# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Source-position and pipeline-stage IR nodes, plus the top-level ``QueryIR`` model."""

from functools import cached_property
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, computed_field, model_validator

# Pydantic v2 resolves string forward refs in `AnyExpr` using the namespace of
# the consuming module, so every name in AnyExpr must be importable here.
from .expr import (  # noqa: F401 — names referenced via forward refs
    REBUILT_BY_QUERY_MODULE,
    And,
    AnyExpr,
    Between,
    BinOp,
    BracketedExpr,
    CaseExpr,
    ColumnRef,
    CompoundNamedExpr,
    DataTableExpr,
    ElementExpr,
    Exists,
    Expr,
    ExternalDataExpr,
    FuncCall,
    LetValueRef,
    LiteralExpr,
    NamedExpr,
    Not,
    Or,
    PathExpr,
    RegexMatch,
    SetMembership,
    StarExpr,
    SubqueryExpr,
    ToScalarExpr,
    TypedNameDecl,
    UnaryOp,
    UnknownExpr,
)
from .spans import Span

# Every model below declares ``kind: Literal["..."] = "..."``, a snake_case
# KQL-aligned label (``filter``, ``column_ref``) independent of the CamelCase
# class name. It discriminates ``Pipeline.source``/``.operators``,
# ``SearchOp.tables`` and ``FindOp.tables``, and ``ir.llm_view.to_llm_dict``
# reads it via ``model_fields["kind"].default`` to lead every emitted dict.

class Diagnostic(BaseModel):
    """One parser or binder message, carried through to the IR."""

    model_config = {"extra": "forbid"}
    kind: Literal["diagnostic"] = "diagnostic"
    message: str
    severity: str
    span: Span | None = None
    code: str | None = None
    category: str | None = None
    # The .NET exception text of an analyzer crash (``KUSTOLOGY001``); parser diagnostics leave this ``None``.
    detail: str | None = None


class TabularSchema(BaseModel):
    """Tabular result type: ``{column_name: kusto_type_string}``, in the order the engine emits them.

    Carried by :class:`Operator` and :class:`Pipeline`, and populated only from
    Microsoft's binder at build time. :attr:`Operator.result_schema` records
    when the binder answers, what ``None`` means, and why nothing else derives
    a schema in its place.

    A column whose type is not known is the string ``"unknown"``, Microsoft's
    own name for it (``ScalarTypes.Unknown.Name``). It is not
    ``KustoType.UNRESOLVED``, whose value is ``"unresolved"`` and which is the
    sentinel an unplaced :attr:`Expr.result_type` carries. Test a ``columns``
    value against ``"unknown"``; test a ``result_type`` against
    ``KustoType.UNRESOLVED``.

    ``columns is None`` on the enclosing :attr:`Operator.result_schema` /
    :attr:`Pipeline.result_schema` is a third thing: no schema was determined
    at all. ``columns == {}`` claims the step emits no columns.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["tabular_schema"] = "tabular_schema"
    columns: dict[str, str] = {}


class Assignment(BaseModel):
    """One ``name = expression`` pair, as ``extend`` and ``summarize`` write it."""

    model_config = {"extra": "forbid"}
    kind: Literal["assignment"] = "assignment"
    name: str
    expr: AnyExpr
    span: Span


class Operator(BaseModel):
    """Base class for every pipeline-stage IR node."""

    # ``extra="forbid"`` keeps round-trips safe: without it, Union resolution
    # absorbs unknown fields, so a FilterOp JSON validates as a fields-less
    # GetSchemaOp with the predicate dropped. Every IR BaseModel does this --
    # see ``Expr`` and ``Span``.
    model_config = {"extra": "forbid"}

    kind: Literal["operator"] = "operator"
    span: Span
    # ``hint.*`` named parameters, keys verbatim: ``hint.strategy``,
    # ``hint.shufflekey``, ``hint.spread``, ``hint.remote``. On the base class
    # because the grammar admits them on many operators (``join``,
    # ``summarize``, ``mv-expand``, ``partition``, ``evaluate``, …) and the
    # builder stamps them from one place, so a new operator cannot be added
    # without them.
    #
    # Volatile: excluded from ``semantic_hash`` (``hints`` is in
    # ``transforms._VOLATILE_FIELDS``). A hint changes how the engine executes
    # a query, not the rows it returns, so ``join hint.strategy=shuffle`` and
    # ``join`` deduplicate together. Keeping the tuning in its own field lets a
    # consumer read it without a deduplicating consumer seeing two rules.
    hints: dict[str, str] = {}
    # The columns this operator emits, straight from Microsoft's binder
    # (``<operator node>.ResultType``), captured at build time whenever the
    # parse was bound and the symbol is closed -- see
    # :func:`kustology.ir._builder_helpers.table_symbol_columns` for what
    # "closed" buys and why an open one is dropped instead of read. It is the
    # only source of an operator's output schema; ``SchemaAttacher`` overlays
    # it for provenance and derives nothing, since hand-derived per-operator
    # rules disagree with the binder. ``None`` means Microsoft did not answer
    # (no schema, or one it could not fully determine), and the enclosing
    # ``Pipeline.result_schema`` then reports ``None`` rather than a guess.
    #
    # Volatile: excluded from ``semantic_hash``, so a query's digest does not
    # depend on whether the caller supplied a schema.
    # ``transforms._VOLATILE_FIELDS`` is keyed by model field name rather than
    # by owning class, so the entry naming ``result_schema`` for ``Pipeline``
    # covers this declaration too.
    result_schema: TabularSchema | None = None


class FilterOp(Operator):
    """``where predicate``."""

    kind: Literal["filter"] = "filter"
    predicate: AnyExpr


class ExtendOp(Operator):
    """``extend name = expression, ...``."""

    kind: Literal["extend"] = "extend"
    assignments: list[Assignment]


class SummarizeOp(Operator):
    """``summarize aggregations by columns``."""

    kind: Literal["summarize"] = "summarize"
    aggregations: list[Assignment]
    by: list[ColumnRef | AnyExpr | Assignment]


class ProjectOp(Operator):
    """``project columns``."""

    kind: Literal["project"] = "project"
    columns: list[ColumnRef | Assignment | AnyExpr]


class TableRef(BaseModel):
    """A table named in source position, with whatever qualified it.

    ``name`` is the bare table name even when the query wrote
    ``cluster('c').database('d').T``. Schema lookups are keyed on it (see
    :meth:`SchemaAttacher._source_entry`), so folding the qualifiers into the
    string would stop every qualified query resolving.

    ``database`` and ``cluster`` hold the qualifiers the query stated.
    ``database('d1').T`` and ``database('d2').T`` read two different tables,
    so folding the qualifiers away would give them one node and one
    ``semantic_hash``.

    ``is_wildcard`` marks a pattern: ``union T*`` matches a set of tables, and
    without the flag it is indistinguishable from a literal table called
    ``T*`` (``union ['T*']``, a legal and different query).
    """

    model_config = {"extra": "forbid"}
    kind: Literal["table_ref"] = "table_ref"
    name: str
    database: str | None = None
    cluster: str | None = None
    is_wildcard: bool = False
    span: Span


class LetRef(BaseModel):
    """A source-position name bound by an earlier ``let`` in the same query.

    Distinct from :class:`TableRef`, which names something the cluster is
    expected to hold. The distinction is decidable from the ``let`` statements
    alone, with no schema and no binder, so it holds identically for a bound
    and an unbound parse.

    Only bindings that precede the reference count. ``let A = A | …`` and a
    reference to a binding declared further down both stay a ``TableRef``;
    resolving them to the binding would be a guess.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["let_ref"] = "let_ref"
    name: str
    span: Span


class UnknownSource(BaseModel):
    """Source the IR builder couldn't model — captures provenance.

    ``raw_text`` is the node's own source, ``ToString(IncludeTrivia.Minimal)``.
    A shared constant there would make every unmodeled source hash identically
    no matter what the query said.

    Known boundary: an unmodeled source is formatting-sensitive in the hash.
    ``Minimal`` drops the node's leading trivia but not trivia interior to it,
    and no ``IncludeTrivia`` mode does (checked against all four), so
    ``let /*c*/ x = 1;`` and ``let x = 1;`` produce different ``semantic_hash``
    values. Stripping comments textually is ruled out for the reason
    ``transforms._normalize_raw_text`` records: ``//`` is the middle of every
    URL, and a run of spaces inside a string literal is data. The residue is a
    false split, where a deduplicating consumer fails to merge two spellings of
    one query; two different queries never share a digest through it, and it
    reaches only the sources the builder already could not model.
    :class:`~kustology.ir.expr.UnknownExpr` and :class:`UnknownOp` carry the
    same property.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["unknown_source"] = "unknown_source"
    raw_text: str
    span: Span


class ImplicitSource(BaseModel):
    """A source whose rows come from a parent context, not a named table.

    Covers union-at-root subqueries, ``mv-apply``/``partition``/``fork``
    inner pipelines, and parenthesized ``join``/``lookup`` right-hand sides.
    Distinct from :class:`UnknownSource`, which means the source could not
    be determined.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["implicit_source"] = "implicit_source"
    span: Span


class FuncCallSource(BaseModel):
    """A function-call pipeline source, as in ``findAnomalies('foo') | summarize ...``."""

    model_config = {"extra": "forbid"}
    kind: Literal["func_call_source"] = "func_call_source"
    name: str
    args: list[AnyExpr] = []
    span: Span


class DataTableSource(BaseModel):
    """``datatable(a:int, b:string)[1,"x",2,"y"]`` — an inline table literal.

    The values are the query: a ``datatable`` of allow-listed hashes and the
    same ``datatable`` of different hashes are two different queries. Lowering
    them to a bare ``FuncCallSource(name="datatable", args=[])`` would discard
    the schema and every row, leaving them all one ``semantic_hash``.

    ``rows`` reshapes what the parser hands over. ``Values`` on the .NET node
    is a flat list of expressions with no row structure; the row width comes
    from ``len(columns)``. Cells are expressions because KQL admits any scalar
    expression there (``datatable(t:datetime)[ago(1d)]``), and because a
    ``LiteralExpr`` carries the literal kind (``long``, ``real``,
    ``timespan``) that a bare Python value cannot.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["datatable_source"] = "datatable_source"
    columns: list[tuple[str, str]]
    rows: list[list[AnyExpr]]
    span: Span


class ExternalDataSource(BaseModel):
    """``externaldata(a:string)[uri, …] with (format="csv")`` in source position.

    Distinct from :class:`~kustology.ir.expr.ExternalDataExpr`, the same
    construct in expression position (the value set of a membership test).
    They share :func:`~kustology.ir._builder_helpers.read_external_data`, so
    the two cannot drift.

    ``uris`` is a list because the construct takes one: a feed assembled from
    two URIs is not the feed from one of them. An entry is not guaranteed to
    be a URI. When the element does not fold to a literal, such as a
    ``let``-bound feed URL or ``strcat("https://", env)``, the field records
    that element's source text instead (``"u"``, or the whole call as
    written). Resolving those needs the query.

    ``properties`` is the whole ``with (...)`` clause, keys verbatim, in the
    ``dict[str, str]`` shape :attr:`RenderOp.properties` uses. Dropping
    everything but ``format`` would collide two queries, since
    ``ignoreFirstRecord=true`` skips the CSV header row and a source node has
    no ``raw_text`` for the dropped text to survive in. ``format`` is its own
    field because the rest of the library reads it, and it stays in
    ``properties`` under the name the query wrote, so a consumer can
    reconstruct a complete clause.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["external_data_source"] = "external_data_source"
    columns: list[tuple[str, str]]
    uris: list[str]
    format: str | None = None
    properties: dict[str, str] = {}
    span: Span


class DistinctOp(Operator):
    """``distinct columns``."""

    kind: Literal["distinct"] = "distinct"
    columns: list[ColumnRef | Assignment | AnyExpr]


class TakeOp(Operator):
    """``take count`` (equivalently ``limit count``)."""

    kind: Literal["take"] = "take"
    # KQL allows any scalar expression here (`let n = 10; T | take n`,
    # `take toscalar(U | count)`), so the field admits ``AnyExpr``. ``int`` is
    # listed first so Pydantic validates a JSON literal ``5`` as a plain
    # ``int``, matching ``op.count == 5`` assertions, instead of coercing it
    # into an expression model. The sibling count and limit fields follow suit.
    count: int | AnyExpr


class SortKey(BaseModel):
    """One ordering key of ``sort by`` / ``order by`` / ``top … by``.

    The expression alone is not the key: ``sort by x asc`` and ``sort by x
    desc`` return rows in opposite orders, so the AST's ``OrderedExpression``
    cannot be unwrapped to its inner expression without dropping the half that
    decides the order.

    ``direction`` is required and has no default. KQL's unwritten default is
    ``desc``, so a bare ``sort by x`` records the effective value
    ``direction="desc"``, never ``None``. Requiring it is what makes that
    value visible: ``ir.llm_view.to_llm_dict`` drops any field still holding
    its declared default, so a defaulted ``direction`` would vanish from the
    LLM view on exactly the queries where the reader has no other way to tell
    which way the rows come back.

    ``nulls`` keeps its ``None`` when the query does not write it, the one D8
    field on this model that does not carry KQL's effective default. KQL does
    define null placement, and defines it as direction-dependent: "Default for
    ``asc`` is ``nulls first``. Default for ``desc`` is ``nulls last``"
    (`sort operator
    <https://learn.microsoft.com/en-us/kusto/query/sort-operator>`_, revised
    2025-01-21). Substituting it would go wrong twice. Every other D8 default
    is a constant that adds information the IR would otherwise lack, the way a
    bare ``join`` really is ``innerunique``; this one is derivable from
    ``direction`` on the same node, so it would put a computed value in a field
    whose remaining job is to answer "did the query write this?". It would also
    merge ``sort by x asc`` with ``sort by x asc nulls first`` in
    ``semantic_hash`` — arguably correct, but a deduplicating consumer survives
    a failure to merge and cannot survive a wrong one.

    .NET's ``OrderingClause`` carries ``AscOrDescKeyword`` and ``NullsClause``
    as independently optional peers, so the asymmetry is about what the field
    means: ``sort by x nulls first`` records ``direction="desc"`` beside
    ``nulls="first"`` even with ``AscOrDescKeyword`` at ``None``.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["sort_key"] = "sort_key"
    expression: AnyExpr
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"] | None = None
    span: Span


class SortOp(Operator):
    """``sort by expressions`` (equivalently ``order by expressions``)."""

    kind: Literal["sort"] = "sort"
    expressions: list[SortKey]


class TopOp(Operator):
    """``top count by expression``."""

    kind: Literal["top"] = "top"
    count: int | AnyExpr
    by: SortKey


class TopHittersOp(Operator):
    """``top-hitters count of column [by weight]``."""

    kind: Literal["top_hitters"] = "top_hitters"
    count: int | AnyExpr
    # ``top-hitters N of C [by V]`` has two column operands: ``of`` names the
    # column whose top values are found, ``by`` the optional weight summed to
    # rank them.
    #
    # ``of`` is required and ``by`` is not, matching the parser.
    # ``OfExpression`` is never ``None``, even for a bare ``T | top-hitters``
    # where the error-tolerant parser synthesizes a name node with
    # ``IsMissing`` set; ``ByClause`` really is ``None`` when the clause is
    # absent. A default on ``of`` would be unreachable from the builder and
    # would let a hand-written payload validate without its mandatory operand,
    # since ``extra="forbid"`` rejects unknown keys and not missing ones.
    # ``SampleDistinctOp.of`` is the same slot, declared the same way.
    of: AnyExpr
    by: AnyExpr | None = None


class SampleOp(Operator):
    """``sample count``."""

    kind: Literal["sample"] = "sample"
    count: int | AnyExpr


class SearchOp(Operator):
    """``search`` — a term match across columns, optionally scoped to tables.

    ``tables`` is the ``in (A, B)`` scope, which decides what is searched:
    ``search in (A) 'x'`` and ``search in (B) 'x'`` read different tables.
    Entries are :class:`TableRef`, or :class:`LetRef` when an earlier ``let``
    bound the name, the same reading the pipeline's own source position gets,
    so a qualifier (``database('d').T``) and a wildcard (``T*``) survive here
    too.

    ``search_kind`` is required and carries KQL's effective default
    ``"default"`` for an unwritten ``kind=`` (D8). The value set comes from
    the bundled DLL: a bound parse of ``search kind=bogus 'x'`` is diagnosed
    *"Expected one of: default, case_insensitive, case_sensitive"*. Left
    optional, the field would split a bare ``search`` from
    ``search kind=default``.

    One residual split: Microsoft documents ``case_insensitive`` as a synonym
    for ``default``, and the two are recorded verbatim, so they hash apart.
    Folding them would report a value the query did not write, a different
    decision from substituting an unwritten default.
    """

    kind: Literal["search"] = "search"
    predicate: AnyExpr | None = None
    search_kind: str
    # Discriminated on the kind literal; member order is not load-bearing.
    tables: list[Annotated[TableRef | LetRef, Field(discriminator="kind")]] = []


class UnionOp(Operator):
    """``union`` — concatenate the rows of two or more tables.

    ``union_kind`` decides the output schema: ``outer`` keeps every column any
    input has, ``inner`` keeps only the columns they all share. Two queries
    differing in it return different columns. The field is required with no
    default, holding KQL's effective value ``"outer"`` for a bare ``union``;
    :class:`ParseOp.parse_kind` records why it is declared that way.

    ``withsource=C`` adds a column naming the source table of each row, and
    ``isfuzzy=true`` downgrades a missing table from an error to a warning.
    Both change the result, and both are genuinely optional: no column and no
    fuzziness are not values either parameter can state.
    """

    kind: Literal["union"] = "union"
    pipelines: list["Pipeline"]
    union_kind: str
    is_fuzzy: bool = False
    withsource: str | None = None


class MakeSeriesAggregate(BaseModel):
    """One ``make-series`` aggregate, with the value that fills its gaps.

    ``default=0`` and ``default=1`` produce different series, since the value
    goes into every bucket with no rows, so the clause is data. Unwrapping the
    parser's ``MakeSeriesExpression`` to the inner assignment would drop it and
    fold ``default=0``, ``default=1`` and no default into one node.

    ``name`` and ``expr`` are spelled as on :class:`Assignment`, so the binder
    reads ``a.name`` / ``a.expr`` over this list without caring which of the
    two element types it holds.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["make_series_aggregate"] = "make_series_aggregate"
    name: str
    expr: AnyExpr
    default: AnyExpr | None = None
    span: Span


class MakeSeriesOp(Operator):
    """``make-series aggregations on_column from range_from to range_to step step by by``."""

    kind: Literal["make_series"] = "make_series"
    aggregations: list[MakeSeriesAggregate]
    by: list[Assignment]
    on_column: AnyExpr | None = None
    range_from: AnyExpr | None = None
    range_to: AnyExpr | None = None
    step: AnyExpr | None = None


class MvExpandColumn(BaseModel):
    """One expanded column of ``mv-expand``, with its declared element type.

    ``mv-expand a to typeof(string)`` tells KQL the expanded rows are strings,
    which changes the output column's type and therefore what downstream
    operators can do with it. The ``ToTypeOf`` clause is data: unwrapping the
    parser's ``MvExpandExpression`` to its inner expression would give the
    typed and untyped forms identical IR.

    ``to_typeof`` is the type as the query wrote it (``string``, ``long``),
    not a resolved :class:`~kustology.ir.types.KustoType`, following
    :class:`~kustology.ir.expr.TypedNameDecl.declared_type`.

    It is optional because the two spellings are not equivalent. Writing
    ``to typeof(dynamic)`` asserts the element type; omitting the clause
    leaves the binder to infer one from ``dynamic<T>``, and the IR should not
    claim the query stated a type it did not. The unwritten behavior is
    statable, since ``mv-expand a to typeof(dynamic)`` parses and binds with
    no diagnostic, so the two hash apart as a split.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["mv_expand_column"] = "mv_expand_column"
    expression: AnyExpr
    to_typeof: str | None = None
    span: Span


class MvExpandOp(Operator):
    """``mv-expand`` — one output row per element of a dynamic column.

    Every modifier here changes the rows the operator returns, so each is
    data: discarding them would fold ``mv-expand a``, ``mv-expand a limit 10``
    and ``mv-expand with_itemindex=i a`` into one node with one
    ``semantic_hash``.

    ``expand_kind`` is required and carries KQL's effective default ``"bag"``
    (D8). It is one field for two spellings: ``kind=bag`` and the deprecated
    ``bagexpansion=bag`` are the same modifier, which the DLL confirms by
    giving both the same value set (``kind=bogus`` and ``bagexpansion=bogus``
    are each diagnosed *"Expected one of: bag, array"*). Two fields would split
    those spellings in the hash, the way reading only ``render``'s ``with``
    clause would split its two. A query writing both parses clean in 12.4.1 and
    records ``kind``, the modern spelling, as ``render``'s merge also prefers
    its modern spelling.

    ``row_limit`` and ``with_item_index`` are optional. KQL's documented
    implicit ``limit`` is ``2147483647`` and that literal parses clean, but no
    value states the unwritten case: recording 2147483647 on every bare
    ``mv-expand`` would report a bound the query never set and collapse it onto
    the query that did set it. D8 substitutes named modes only.
    """

    kind: Literal["mv_expand"] = "mv_expand"
    columns: list[MvExpandColumn]
    # ``limit N``. ``int`` first for the same reason as ``TakeOp.count``.
    row_limit: int | AnyExpr | None = None
    with_item_index: str | None = None
    expand_kind: str


class RenderOp(Operator):
    """``render`` — the visualization hint attached to a result.

    ``properties`` is everything inside ``with (...)`` (``title``, ``ymin``,
    ``series``, …) plus the legacy bare-parameter spelling of the same thing.
    KQL accepts both ``render columnchart kind=stacked`` and
    ``render columnchart with (kind=stacked)``, and folding them into one dict
    is what makes those two spellings of one query hash alike.

    The dict is data: dropping it would make every ``render timechart`` one
    node however it is configured.
    """

    kind: Literal["render"] = "render"
    render_kind: str
    properties: dict[str, str] = {}


class ProjectAwayOp(Operator):
    """``project-away columns``."""

    kind: Literal["project_away"] = "project_away"
    columns: list[ColumnRef | AnyExpr]


class ProjectKeepOp(Operator):
    """``project-keep columns``."""

    kind: Literal["project_keep"] = "project_keep"
    columns: list[ColumnRef | AnyExpr]


class ReorderKey(BaseModel):
    """One term of ``project-reorder``: a column or wildcard, plus its order.

    Sibling to :class:`SortKey` — both wrap the parser's ``OrderedExpression``
    — and the two differ on the point D8 turns on.

    ``project-reorder``'s ``asc``/``desc`` orders columns, not rows: it decides
    the left-to-right order of the columns a term matches, which is why it
    earns its keep on wildcards (``project-reorder a* asc``). Omitting it means
    "emit these in the order I listed them", selecting no KQL default the way a
    bare ``sort by x`` selects ``desc``. With no effective value to substitute,
    ``direction`` is genuinely optional and ``None`` is the honest record.
    Stamping ``"desc"`` on a bare term would misreport it and collapse it
    against an explicit ``desc`` in the hash.

    ``None`` is a real declared default here, so ``to_llm_dict`` drops the
    field on unwritten terms and renders it on written ones.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["reorder_key"] = "reorder_key"
    expression: AnyExpr
    direction: Literal["asc", "desc"] | None = None
    span: Span


class ProjectReorderOp(Operator):
    """``project-reorder columns``."""

    kind: Literal["project_reorder"] = "project_reorder"
    columns: list[ReorderKey]


class ProjectRenameOp(Operator):
    """``project-rename new_name = old_name, ...``."""

    kind: Literal["project_rename"] = "project_rename"
    columns: list[Assignment]


class ProjectByNamesOp(Operator):
    """``project-by-names names``."""

    kind: Literal["project_by_names"] = "project_by_names"
    names: list[AnyExpr]


class MvApplyOp(Operator):
    """``mv-apply`` — expand a dynamic column and run a subquery per row.

    Every modifier here changes the rows the operator returns, the same
    register as :class:`MvExpandOp`'s: ``mv-apply x=d on (...)``,
    ``mv-apply x=d to typeof(long) on (...)`` and
    ``mv-apply x=d limit 3 on (...)`` are three nodes with three digests.

    ``to_typeof`` is one field, unlike :class:`MvExpandColumn`'s per-column
    version. Syntactically the ``to typeof(...)`` clause attaches to whichever
    comma-separated ``MvApplyExpression`` immediately precedes it, and on a real
    parse it lands on the first, the last, or (with one column, the common case)
    the only element. ``mv-apply`` has no per-column type story, so the reader
    takes the first written occurrence across ``assignments`` and adds no
    wrapper type.

    ``row_limit`` and ``item_index`` (``with_itemindex=``) are optional for the
    reason ``MvExpandOp``'s are: KQL substitutes an effective value for neither
    an unwritten ``limit`` nor an unwritten index column, so ``None`` is the
    honest record and D8 does not apply. ``item_index`` must precede the
    expansion column; ``mv-apply x=d with_itemindex=i on (...)`` is a parse
    error, where ``mv-expand``'s spelling tolerates the postfix.

    Not modeled: an undocumented ``id <expr>`` clause
    (``MvApplyContextIdClause`` in the grammar) parses cleanly between
    ``limit`` and ``on``, but is `.Hide()`-marked in the parser and carries no
    public completion entry — the same "nothing reaches it as documented
    surface" register as :class:`FindOp`'s excluded ``project-away``.
    """

    kind: Literal["mv_apply"] = "mv_apply"
    assignments: list[Assignment]
    to_typeof: str | None = None
    row_limit: int | AnyExpr | None = None
    item_index: str | None = None
    right: "Pipeline"


class ParseOp(Operator):
    """``parse`` — extract capture columns from a string expression.

    ``parse_kind`` selects the matching engine, and the three values differ:
    ``simple`` matches the pattern literally, ``regex`` treats it as a regular
    expression, ``relaxed`` tolerates a failed match instead of nulling the
    row.

    It is required and has no default, carrying KQL's effective value (D8): a
    bare ``parse`` records ``"simple"``, never ``None``. Requiring it is what
    keeps the value visible in ``to_llm_dict`` — see
    :class:`SortKey.direction`.

    ``flags`` is the ``flags='i'`` regex modifier and is genuinely optional: no
    flags is not a flag string.
    """

    kind: Literal["parse"] = "parse"
    target: AnyExpr
    patterns: list[AnyExpr]
    parse_kind: str
    flags: str | None = None


class ParseWhereOp(Operator):
    """``parse-where`` — ``parse`` that drops rows the pattern misses.

    Same parameters as :class:`ParseOp`, including the required
    ``parse_kind`` with its effective default of ``"simple"``.
    """

    kind: Literal["parse_where"] = "parse_where"
    target: AnyExpr
    patterns: list[AnyExpr]
    parse_kind: str
    flags: str | None = None


class EvaluateOp(Operator):
    """``evaluate`` — run a plug-in, with its declared output-schema clause.

    ``evaluate bag_unpack(d) : (x:string)`` attaches an
    ``EvaluateSchemaClause`` (the .NET property is ``Schema``) declaring the
    columns the plug-in returns. ``declared_schema`` holds those
    ``(name, type)`` pairs in clause order. ``None`` means no clause was
    written at all, distinct from ``[]``, an empty clause ``: (*)``.
    ``declared_schema_star`` is ``True`` when the clause opens with ``*``,
    which appends the plug-in's columns to the schema instead of replacing it.
    The binder derives the real ``result_schema`` from the clause; these fields
    are the query's own declaration of it, carried into ``semantic_hash`` so
    two spellings with different declared schemas hash apart.
    """

    kind: Literal["evaluate"] = "evaluate"
    func: FuncCall
    declared_schema: list[tuple[str, str]] | None = None
    declared_schema_star: bool = False


class CountOp(Operator):
    """``count`` (optionally ``count as as_name``)."""

    kind: Literal["count"] = "count"
    as_name: str | None = None


class PrintOp(Operator):
    """``print columns``."""

    kind: Literal["print"] = "print"
    columns: list[Assignment | AnyExpr]


class AsOp(Operator):
    """``as name`` — binds the pipeline's current result under a name."""

    kind: Literal["as"] = "as"
    name: str


class RangeOp(Operator):
    """``range column from start to end step step``."""

    kind: Literal["range"] = "range"
    column: str
    start: AnyExpr
    end: AnyExpr
    step: AnyExpr


class LookupOp(Operator):
    """``lookup`` — enrich the left table from a dimension table.

    ``lookup_kind`` is required and carries KQL's effective default
    ``"leftouter"`` for an unwritten ``kind=`` (D8). ``leftouter`` and
    ``inner`` are different operators: ``leftouter`` keeps left rows with no
    match and ``inner`` drops them, so a bare ``lookup`` recorded as ``inner``
    would claim it discards unmatched rows.
    """

    kind: Literal["lookup"] = "lookup"
    lookup_kind: str
    right: "Pipeline"
    # KQL ``on Foo`` is sugar for ``on $left.Foo == $right.Foo``; both surface
    # here as ``AnyExpr`` (a bare ``ColumnRef`` or a full equality ``BinOp``).
    on: list[AnyExpr]


class PartitionOp(Operator):
    """``partition by column (subquery)``."""

    kind: Literal["partition"] = "partition"
    by: AnyExpr
    right: "Pipeline"


class FacetOp(Operator):
    """``facet by columns [with (subquery)]``."""

    kind: Literal["facet"] = "facet"
    columns: list[AnyExpr] = []
    with_pipeline: Optional["Pipeline"] = None


class GetSchemaOp(Operator):
    """``getschema`` — report a table's column schema as rows.

    ``output_kind`` is the ``kind=`` modifier (``csl``/``full`` in current
    Kusto docs), read from the operator's own singular ``KindParameter``
    member: ``GetSchemaOperator`` has no ``.Parameters`` list to walk, only
    this one optional slot, which is why the shared named-parameter reader
    does not apply. The DLL does not validate the value set the way it does
    for ``mv-expand kind=``, so an unrecognized spelling parses clean and
    ``output_kind`` records whatever text was written. ``None`` means the
    clause was absent; KQL substitutes no default here.
    """

    kind: Literal["getschema"] = "getschema"
    output_kind: str | None = None


class InvokeOp(Operator):
    """``invoke func(...)``."""

    kind: Literal["invoke"] = "invoke"
    func: FuncCall


class FindOp(Operator):
    """``find`` — search rows across a set of tables.

    ``tables`` is the ``in (T, U)`` scope, read the same way as the pipeline's
    own source position and :class:`SearchOp.tables`, so a qualifier, a
    wildcard and a ``let`` alias each survive. Entries are structured nodes,
    never ``ToString()`` text: the no-argument overload is
    ``IncludeTrivia.All``, which folds a comment written before a table name
    into the name itself and makes ``find in (// note`` ↵ ``T)`` hash
    differently from ``find in (T)``.

    ``project`` is the ``project a, b`` column list, which decides the output
    schema; a typed column (``project a:string``) arrives as a
    :class:`~kustology.ir.expr.TypedNameDecl`. ``withsource=C`` names the
    column recording which table each row came from.

    There is no ``project_away`` field. ``FindOperator.ProjectAway`` exists on
    the .NET node, but no spelling of the clause reaches it in the bundled
    12.4.1 parser: the eight forms probed, Microsoft's own documented example
    included, all leave it ``None`` and parse ``project-away`` as a separate
    ``ProjectAwayOperator`` statement with an ``Expected: ;`` diagnostic. A
    declared field nothing can populate reads as implemented and cannot be
    tested (AGENTS.md), so it waits on a DLL refresh.
    """

    kind: Literal["find"] = "find"
    predicate: AnyExpr | None = None
    # Discriminated on the kind literal; member order is not load-bearing.
    tables: list[Annotated[TableRef | LetRef, Field(discriminator="kind")]] = []
    withsource: str | None = None
    project: list[AnyExpr] = []


class ForkBranch(BaseModel):
    """One parenthesized sub-pipeline of ``fork``, with the name it was given.

    ``fork`` runs each branch over the same input rows and returns one result
    table per branch. ``name`` is the ``a=`` prefix, a ``NameEqualsClause`` in
    the AST, which names that result table, so two forks differing only in a
    branch name are two different queries.

    The branch is a wrapper rather than a bare :class:`Pipeline` because the
    name has nowhere else to live. Hanging a parallel ``list[str | None]`` off
    :class:`ForkOp` would pair name to pipeline by index, which misaligns the
    first time a branch is dropped or reordered.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["fork_branch"] = "fork_branch"
    name: str | None = None
    pipeline: "Pipeline"
    span: Span


class ForkOp(Operator):
    """``fork`` — one branch per parenthesized sub-pipeline.

    Each entry pairs the sub-pipeline with the result-table name the query gave
    it; :class:`ForkBranch` records why the wrapper exists. Under
    ``extra="forbid"`` a serialized payload keyed ``pipelines``, the shape an
    older ``IR_SCHEMA_VERSION`` writes, fails validation loudly instead of
    loading with data dropped.
    """

    kind: Literal["fork"] = "fork"
    branches: list[ForkBranch]


class ScanOp(Operator):
    """``scan`` — kept as its own source text; the step machine is not modeled.

    This is the first of eight modeled operators the IR records on ``raw_text``
    rather than in typed fields, the same register as :class:`LetFunction`'s:
    the boundary is stated in the model instead of being left as fields that
    read as implemented and are not. The other seven are :class:`TopNestedOp`,
    :class:`MakeGraphOp`, :class:`MacroExpandOp`, :class:`GraphMatchOp`,
    :class:`GraphMarkComponentsOp`, :class:`GraphShortestPathsOp` and
    :class:`GraphToTableOp`.

    Two exclusions, so the count is checkable. ``graph-where-edges`` and
    ``graph-where-nodes`` carry a real ``predicate`` and no ``raw_text``.
    :class:`UnknownOp` does carry one, so enumerating ``Operator`` subclasses
    with a ``raw_text`` field gives nine; it is the builder's fallback for a
    shape it could not dispatch rather than a modeling choice. Eight is the
    register, nine the field count.

    ``raw_text`` is ``ToString(IncludeTrivia.Minimal)``, so these operators
    round-trip through ``model_dump_json`` and participate in ``semantic_hash``
    as text. Nothing typed is inside them to walk: ``find_all(ir, ColumnRef)``
    will not report a column that appears only in a ``scan`` step. Downstream
    scope splits by bind state. ``Operator.result_schema`` carries Microsoft's
    ``ResultType``, which knows the columns a ``scan``'s ``declare`` adds, and
    :class:`SchemaAttacher` overlays it; an unbound parse has no such answer,
    and nothing re-derives one, so the scope downstream is the one they
    inherited. Hashing text also brings the formatting sensitivity
    :class:`UnknownSource` documents: interior comments and interior spacing
    are part of the digest.

    ``scan``'s own body is a state machine: ``declare`` variables plus ``step``
    rules with guards and assignments. Modeling it would be a new feature.
    """

    kind: Literal["scan"] = "scan"
    raw_text: str


class SerializeOp(Operator):
    """``serialize [assignments]`` — marks the stream as row-ordered."""

    kind: Literal["serialize"] = "serialize"
    assignments: list[Assignment] = []


class ConsumeOp(Operator):
    """``consume`` — read and discard rows, for benchmarking a pipeline.

    ``decodeblocks`` is the operator's only named parameter, read through the
    shared :func:`~kustology.ir._builder_helpers.extract_named_param`.
    ``None`` means unwritten; KQL substitutes no effective value here. A
    written boolean is not normalized: the DLL renders its own ``true`` and
    ``false`` as the text ``"True"``/``"False"`` (Python's ``str(bool)``
    spelling), and this field records that text as written.
    """

    kind: Literal["consume"] = "consume"
    decodeblocks: str | None = None


class AssertSchemaOp(Operator):
    """``assert-schema columns``."""

    kind: Literal["assert_schema"] = "assert_schema"
    columns: dict[str, str] = {}


class ExecuteAndCacheOp(Operator):
    """``__executeAndCache`` — cache the pipeline's result set for reuse."""

    kind: Literal["execute_and_cache"] = "execute_and_cache"


class ParseKvOp(Operator):
    """``parse-kv`` — split a string into key/value columns.

    ``properties`` is the ``with (...)`` clause (``pair_delimiter``,
    ``kv_delimiter``, ``quote``, …), read from ``WithClause.Properties``, a
    different member from every other operator's named parameters:
    ``extract_named_param`` is built against ``.Parameters`` and does not reach
    it, so this reader walks the list directly. It is a list of pairs because
    ``quote`` legally repeats (probed clean on a real parse) and a dict would
    keep only the last spelling. ``[]`` means the clause was absent, the
    convention :class:`RenderOp`'s ``properties`` uses; that one folds a
    duplicate name onto its last value, and this one must not.
    """

    kind: Literal["parse_kv"] = "parse_kv"
    target: AnyExpr
    # ``as (b:string, c:long)`` -- a name:type schema, modeled the same way
    # as :class:`AssertSchemaOp`: a declared key has a type, not a value, so
    # there is no expression for an ``Assignment`` shape to hold.
    columns: dict[str, str] = {}
    properties: list[tuple[str, str]] = []


class SampleDistinctOp(Operator):
    """``sample-distinct count of column``."""

    kind: Literal["sample_distinct"] = "sample_distinct"
    count: int | AnyExpr
    of: AnyExpr


class TopNestedOp(Operator):
    """``top-nested`` — source text only; see :class:`ScanOp` for the register.

    A chained ``top-nested … by … with others=…`` clause is a list of levels,
    each with its own key expression, aggregate and ``others`` label. None of
    that is broken out, so a nested key is invisible to ``find_all``.
    """

    kind: Literal["top_nested"] = "top_nested"
    raw_text: str


class MakeGraphOp(Operator):
    """``make-graph`` — source text only; see :class:`ScanOp` for the register.

    The edge columns, the ``with``-clause node table and its key are all inside
    ``raw_text``, so ``find_all(ir, TableRef)`` does not report the node table.
    Tier 1 does, but only on a bound parse: over ``Edges | make-graph src -->
    dst with Nodes on n``, ``parse(q).get_referenced_tables()`` answers
    ``{"Edges"}`` and ``parse(q, schema=…)`` answers ``{"Edges", "Nodes"}``,
    because the bound path reads the resolved symbol instead of the syntactic
    source positions. ``replace_table("Nodes", …)`` splits the same way: a
    no-op unbound, a correct rewrite bound.
    """

    kind: Literal["make_graph"] = "make_graph"
    raw_text: str


class MacroExpandOp(Operator):
    """``macro-expand`` — source text, plus the inner pipeline.

    The one member of the :class:`ScanOp` register that is not opaque all the
    way down. The entity-group name and the ``as`` alias stay in ``raw_text``,
    and the parenthesized body is built as a real :class:`Pipeline` on
    ``pipeline``, so its operators and columns are walkable. The scope it runs
    against is not: the alias resolves to one entity per expansion, which the
    IR has no way to enumerate.
    """

    kind: Literal["macro_expand"] = "macro_expand"
    raw_text: str
    pipeline: Optional["Pipeline"] = None


class GraphMatchOp(Operator):
    """``graph-match`` — source text only; see :class:`ScanOp` for the register.

    The pattern, its ``where`` constraint and its ``project`` list are all
    text, so a column named only in a graph pattern does not reach
    ``find_all(ir, ColumnRef)``, and the columns this operator emits are in no
    downstream scope. That second half is a boundary of the graph surface:
    Microsoft's binder does not place them either, and reports KS142 for a
    ``| project`` naming one on a bound parse.
    """

    kind: Literal["graph_match"] = "graph_match"
    raw_text: str


class GraphMarkComponentsOp(Operator):
    """``graph-mark-components`` — text only; see :class:`ScanOp`.

    ``with_component_id=`` names a column this operator adds. It is inside
    ``raw_text``, so the added column is in no downstream scope, Microsoft's
    included.
    """

    kind: Literal["graph_mark_components"] = "graph_mark_components"
    raw_text: str


class GraphShortestPathsOp(Operator):
    """``graph-shortest-paths`` — text only; see :class:`ScanOp`.

    Same shape as :class:`GraphMatchOp`: pattern, constraint and
    projection are one string.
    """

    kind: Literal["graph_shortest_paths"] = "graph_shortest_paths"
    raw_text: str


class GraphToTableOp(Operator):
    """``graph-to-table`` — text only; see :class:`ScanOp`.

    Whether it emits ``nodes``, ``edges`` or both, and under which column
    names, is in ``raw_text``. That is the information a downstream scope would
    need, so the scope stays whatever the graph operators inherited.
    """

    kind: Literal["graph_to_table"] = "graph_to_table"
    raw_text: str


class GraphWhereEdgesOp(Operator):
    """``graph-where-edges (…)`` — modeled, with a real predicate.

    Not part of the :class:`ScanOp` register despite the family name: the
    parenthesized condition is an ordinary expression over edge properties, so
    it is built as one and its columns are walkable.
    """

    kind: Literal["graph_where_edges"] = "graph_where_edges"
    predicate: AnyExpr


class GraphWhereNodesOp(Operator):
    """``graph-where-nodes (…)`` — modeled, with a real predicate.

    The node-side twin of :class:`GraphWhereEdgesOp`.
    """

    kind: Literal["graph_where_nodes"] = "graph_where_nodes"
    predicate: AnyExpr


class UnknownOp(Operator):
    """Operator the IR builder couldn't dispatch — captures provenance.

    Symmetric to :class:`kustology.ir.UnknownExpr`. When operator dispatch
    falls through (``BadQueryOperator``, or a new operator kind from a
    Kusto.Language upgrade), the builder emits this instead of a bare
    ``Operator(span=...)``, so analyzers can detect coverage gaps and the
    coverage audit has something to grow against.
    """

    kind: Literal["unknown_op"] = "unknown_op"
    raw_text: str
    ast_kind: str
    reason: str


class Pipeline(BaseModel):
    """One tabular statement: a source plus the operators piped after it."""

    model_config = {"extra": "forbid"}
    kind: Literal["pipeline"] = "pipeline"
    # Discriminated on the kind literal; member order is not load-bearing.
    source: Annotated[Union[
        TableRef, LetRef, FuncCallSource, DataTableSource, ExternalDataSource,
        ImplicitSource, UnknownSource, "Pipeline",
    ], Field(discriminator="kind")]
    # Discriminated on the kind literal; member order is not load-bearing.
    operators: list[Annotated[Union[
        GetSchemaOp, ConsumeOp, ExecuteAndCacheOp,
        FilterOp, ExtendOp, SummarizeOp, ProjectOp, ProjectAwayOp,
        ProjectKeepOp, ProjectReorderOp, ProjectRenameOp, ProjectByNamesOp,
        DistinctOp, TakeOp, SortOp, TopOp, TopHittersOp, SampleOp, SearchOp,
        UnionOp, MakeSeriesOp, MvExpandOp, MvApplyOp, ParseOp, ParseWhereOp,
        EvaluateOp, CountOp, PrintOp, AsOp, RangeOp, LookupOp, PartitionOp,
        RenderOp, "JoinOp",
        FacetOp, InvokeOp, FindOp, ForkOp, ScanOp, SerializeOp,
        AssertSchemaOp, ParseKvOp,
        SampleDistinctOp, TopNestedOp, MakeGraphOp, MacroExpandOp,
        GraphMatchOp, GraphMarkComponentsOp, GraphShortestPathsOp,
        GraphToTableOp, GraphWhereEdgesOp, GraphWhereNodesOp,
        UnknownOp,
    ], Field(discriminator="kind")]]
    # The columns this pipeline emits. Set at build time from the pipe chain's
    # own ``ResultType`` when the parse was bound and Microsoft's symbol is
    # closed, so ``to_ir(attach_schema=False)`` has the shape without the
    # provenance pass; otherwise by ``SchemaAttacher.enrich()``, from the scope
    # its walk leaves. Volatile: see ``Operator.result_schema``.
    result_schema: TabularSchema | None = None


class JoinOp(Operator):
    """``join`` — combine rows from two tables on a condition.

    ``join_kind`` is required and carries KQL's effective default
    ``"innerunique"`` for an unwritten ``kind=`` (D8). ``innerunique``
    deduplicates the left side's join keys first, so a bare ``join`` and
    ``join kind=inner`` return different row counts from the same data.
    Substituting ``"inner"`` would mislabel every bare join and collapse it
    onto the explicit ``kind=inner`` spelling in the hash.

    Required with no pydantic default so ``to_llm_dict`` renders it; see
    :class:`ParseOp.parse_kind`.
    """

    kind: Literal["join"] = "join"
    join_kind: str
    right: Pipeline
    # KQL ``on Foo`` is sugar for ``on $left.Foo == $right.Foo``; both surface
    # here as ``AnyExpr`` (a bare ``ColumnRef`` or a full equality ``BinOp``).
    on: list[AnyExpr]


class LetFunctionParameter(BaseModel):
    """One declared parameter of a ``let``-bound function.

    ``decl`` reuses :class:`~kustology.ir.expr.TypedNameDecl`, the node the
    builder produces for every other ``name:type`` shape in the grammar (a
    ``parse`` capture, a typed ``find … project`` column), so a parameter's
    declared type is read by the same code path and cannot drift from those.
    That is also why this class is a plain ``BaseModel`` rather than an
    ``Expr`` subclass: it is a slot holding a declaration and its default, with
    nothing for anything to evaluate.

    ``default`` is the ``=3`` of ``(w:int=3)``. The grammar restricts it to a
    literal; the field is ``AnyExpr`` for uniformity. Both its presence and its
    value reach ``semantic_hash``: a parameter that may be omitted is a
    different signature from one that may not, and two different defaults are
    two different functions for every call that omits the argument.

    A parameter name is not a ``let`` name: the declaration binds it, so inside
    the body it shadows any same-named ``let`` from the enclosing query and a
    reference to it lowers as a ``ColumnRef``/``TableRef``, never a
    ``LetValueRef``/``LetRef`` (see :class:`LetFunction`). The name is still
    alpha-canonicalized, renamed to ``$param<i>`` by declaration position under
    the rule a ``let`` name gets, because it is bound inside one call's body
    (see :func:`~kustology.ir.transforms._canonicalize_let_names`). Its two
    ``declare``-statement siblings stay verbatim, since each names a
    caller-facing contract: see :class:`QueryParametersStmt` and
    :class:`PatternStmt`.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["let_function_parameter"] = "let_function_parameter"
    decl: TypedNameDecl
    default: AnyExpr | None = None


class LetFunction(BaseModel):
    """A ``let``-declared function: its signature and its body.

    ``let f = (x:int) { ... }`` yields a .NET ``FunctionDeclaration``, which is
    neither an expression nor a pipeline and so cannot ride on ``rhs_expr`` or
    ``rhs_pipeline``. It gets its own field on :class:`LetBinding` and its own
    class here.

    The body is built, and its tail dispatches by the rule a ``let`` right-hand
    side uses: a tabular expression becomes ``body_pipeline``, a scalar one
    ``body_expr``, at most one ever set. A ``let`` written inside the body lands
    in ``body_lets`` and a ``declare query_parameters`` in
    ``body_query_parameters``, both scoped here rather than hoisted into
    :attr:`QueryIR.let_bindings`, since neither is in scope for the query that
    declared the function. The ``FunctionBody`` grammar admits exactly those two
    statement kinds, so the two lists cover every body. ``body_span`` locates
    the body in the source for callers that want the text; it stays volatile, so
    it does not reach ``semantic_hash``.

    ``is_view`` records the ``view`` keyword, which decides whether a wildcard
    ``union *`` picks the function up — a difference in which rows a query
    returns.

    Two boundaries remain:

    * Call sites are not expanded. ``f(1)`` stays a :class:`FuncCallSource` (or
      a ``FuncCall``); the body is reachable once, through the declaration.
      Inlining it would report the body's tables once per call and make the
      digest grow with the call count.
    * Parameter references are textual. A parameter shadows a same-named
      ``let`` from the enclosing query for the length of the body, decided from
      the declaration text alone so it cannot depend on whether a schema was
      supplied. A shadowed name lowers as a ``ColumnRef`` / ``TableRef``, which
      is what the parameter is from the body's point of view, rather than as a
      ``LetValueRef`` / ``LetRef`` pointing at a binding the body cannot see.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["let_function"] = "let_function"
    is_view: bool = False
    # Declaration order, which is call order -- `parameters[0]` is the
    # parameter `f(1, 2)` passes `1` to. The function's own name is on the
    # owning LetBinding.
    parameters: list[LetFunctionParameter] = []
    # ``let``s written inside the body, in declaration order. Scoped here:
    # each is visible to the ones after it and to the tail, and to nothing
    # outside the braces.
    body_lets: list["LetBinding"] = []
    # ``declare query_parameters`` written inside the body -- the only other
    # statement kind the ``FunctionBody`` grammar admits. Scoped here like
    # ``body_lets``, and read by the code path the top-level statement takes,
    # so the two cannot drift.
    body_query_parameters: list["QueryParametersStmt"] = []
    body_pipeline: Pipeline | None = None
    body_expr: AnyExpr | None = None
    body_span: Span


class LetBinding(BaseModel):
    """One ``let`` statement. Exactly one ``rhs_*`` field is populated.

    There is no ``category`` discriminator. Which field is set already says
    whether the binding is tabular, scalar or a function, and finer labels
    (time-scalar, alias, scalar-subquery) are recoverable from the populated
    right-hand side: ``rhs_expr.literal_kind == "timespan"``, a ``TableRef``
    source with no operators, a ``ToScalarExpr``. A stored label would enter
    ``semantic_hash`` too, making the digest sensitive to classification
    choices instead of query semantics.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["let_binding"] = "let_binding"
    name: str
    span: Span
    rhs_expr: AnyExpr | None = None
    # Set for a tabular right-hand side. ``SchemaAttacher.enrich`` walks
    # bindings in declaration order and registers each one's output columns
    # under its name, so on a bound parse ``rhs_pipeline.result_schema`` is
    # populated and a later binding or the main pipeline reading the name
    # through a ``LetRef`` resolves against those columns. Two boundaries
    # remain: a binding naming one declared later is not a ``LetRef`` (see that
    # class), so there is nothing to thread; and ``let``-declared functions are
    # recorded rather than expanded, so a call site does not acquire the body's
    # schema.
    rhs_pipeline: Pipeline | None = None
    rhs_function: LetFunction | None = None
    # Tables and time expressions found inside whichever of ``rhs_pipeline``
    # and ``rhs_function`` is populated; empty for a scalar binding. A
    # function's body is one of the places a query reads a table from, so it
    # counts here too -- otherwise the field would answer "which tables does
    # this binding read, unless it is a function".
    #
    # ``inner_tables`` holds real tables only, so the field answers "which
    # tables does this binding read". A hop to an earlier binding
    # (``let B = A | …``) is a ``LetRef``, reachable via
    # ``find_all(rhs_pipeline, LetRef)``. One exception survives inside
    # ``rhs_function``: a tabular parameter (``(T:(*))``) is not a real table,
    # but a reference to it inside the body shadows textually the way any other
    # parameter does (see ``LetFunctionParameter``) and lowers as a
    # ``TableRef``, indistinguishable here from a genuine table name.
    #
    # Both fields are a digest-excluded derived index: they are written from
    # the right-hand side beside them and hold the same objects
    # (``inner_time_exprs``) or a copy of their names (``inner_tables``), so
    # ``compute_semantic_hash`` clears them before it dumps -- see
    # ``transforms._DERIVED_INDEX_FIELDS``. Nothing is lost, since the nodes
    # they index are hashed through the right-hand side. Hashing the index
    # would lose something: a copy of a name cannot be alpha-canonicalized, so
    # a body reading a tabular parameter would carry its written name into the
    # digest after every ``TableRef`` beside it had been renamed, splitting two
    # spellings of one function. Read them freely; only the digest ignores them.
    inner_tables: list[str] = []
    inner_time_exprs: list[AnyExpr] = []


# -- statements that are neither ``let`` nor tabular -------------------------
#
# KQL's statement list admits five more kinds, each modeled below. What they
# say is part of the query, so it must reach the IR and ``semantic_hash``: two
# different values of one statement are two different queries. They live on one
# ordered :attr:`QueryIR.statements` list rather than five per-kind fields. The
# hash payload, the canonicalizer and the binder each enumerate ``QueryIR``'s
# fields by name, so one field is three registration points and five would be
# fifteen places for a new kind to go silently unhashed.
#
# None of them carries a ``span``, though operator and expression nodes all do:
# a span is volatile (stripped before the digest) and these nodes have no
# consumer that triangulates back to source text.
# :attr:`PatternMatch.body_span` is the single exception, there for the reason
# :attr:`LetFunction.body_span` is -- a body is a region a caller may want to
# read the text of.


class SetOptionStmt(BaseModel):
    """``set`` — one query option, and the value it was set to.

    ``set query_now=datetime(2020-01-01)`` pins what ``now()`` returns for the
    whole query, so two different values are two different queries, and a query
    that pins it differs from one that does not.

    ``value`` is ``None`` for the valueless spelling (``set notruncation``),
    where the .NET ``ValueClause`` really is absent rather than defaulted. KQL
    substitutes no effective value here, so there is nothing to put in its
    place, and the flag form and an explicitly-valued form are different
    statements.

    The value is an expression because the grammar admits one
    (``set query_now=datetime(...)``), and because a
    :class:`~kustology.ir.expr.LiteralExpr` carries the literal kind
    (``datetime`` against ``string``) that a rendered value would lose.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["set_option_statement"] = "set_option_statement"
    name: str
    value: AnyExpr | None = None


class QueryParametersStmt(BaseModel):
    """``declare query_parameters(p:long = 1, q:string)`` — the query's own declared parameters.

    ``parameters`` reuses :class:`LetFunctionParameter` because the .NET node's
    ``Parameters`` is the identical
    ``SyntaxList[SeparatedElement[FunctionParameter]]`` a ``let``-declared
    function's is, read by the same builder path, so the two readings cannot
    drift. A parameter's declared type and its default both reach
    ``semantic_hash``: a parameter that may be omitted is a different contract
    from one that may not, and two defaults are two different queries for every
    caller that omits the value.

    A parameter name here is never alpha-canonicalized, an asymmetry with
    ``let``. A ``let`` name is a local label with no meaning outside the query,
    so ``compute_semantic_hash`` replaces it with its position
    (:func:`~kustology.ir.transforms._canonicalize_let_names`). A ``declare
    query_parameters`` name is the caller-facing API of a saved query or
    dashboard tile, the key a caller passes the value under, so
    ``query_parameters(p:long)`` and ``query_parameters(q:long)`` accept
    different requests and must not merge.

    The exclusion is by scope, not by class: a ``let``-declared function's
    parameters are ``TypedNameDecl`` too, and are alpha-canonicalized to
    ``$param<i>``. The rename runs per declaration body, numbering the
    parameters of the signature whose body it is about to walk, and a
    ``declare query_parameters`` statement opens no body. A rename that became
    class-keyed would silently merge two different call contracts;
    ``query-parameters-shadowed-by-let`` in ``tests/ir/test_hash_battery.py``
    is the pair that fails when it does.

    One boundary: a reference to a parameter inside the query is not linked
    back to this declaration. ``declare query_parameters(n:long); T | take n``
    lowers ``n`` as an ordinary column reference, or as that binding if a
    ``let`` of the name precedes it, the same textual reading a
    :class:`LetFunction` parameter gets outside its own body.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["query_parameters_statement"] = "query_parameters_statement"
    parameters: list[LetFunctionParameter] = []


class PatternMatch(BaseModel):
    """One arm of a ``declare pattern`` body: the values it matches, and what it expands to.

    ``("Kusto").["Info"] = { T | take 1 }`` is reached by
    ``P("Kusto").["Info"]`` and by nothing else, so ``values`` and
    ``path_value`` are the arm's selector. ``path_value`` is ``None`` for the
    comma spelling, which has no path segment at all.

    The body is a ``FunctionBody``, the same node a ``let``-declared function's
    body is, so the same rule builds it: :attr:`body_pipeline` for a tabular
    tail, :attr:`body_expr` for a scalar one, at most one of the two ever set.
    ``body_span`` locates the body for callers that want the text; it stays
    volatile, so it does not reach ``semantic_hash``.

    ``body_lets`` is where a ``let`` written inside the braces lives, scoped
    here rather than hoisted into :attr:`QueryIR.let_bindings`: the query
    declares it inside the body, so listing it at top level would put it in a
    scope the query never wrote it in, and with this field populated would
    declare it twice.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["pattern_match"] = "pattern_match"
    values: list[AnyExpr] = []
    path_value: AnyExpr | None = None
    body_lets: list[LetBinding] = []
    body_pipeline: Pipeline | None = None
    body_expr: AnyExpr | None = None
    body_span: Span


class PatternStmt(BaseModel):
    """``declare pattern`` — a named table of expansions.

    ``declared_only`` marks the forward declaration ``declare pattern P;``,
    where the .NET ``Pattern`` child is ``None``. That is a different statement
    from a pattern whose body happens to be empty, and a bool is what keeps the
    two apart once ``parameters`` and ``matches`` are both ``[]`` either way.

    ``parameters`` and ``path_parameter`` are the pattern's call shape,
    ``(a:string)[L:string]``, read as
    :class:`~kustology.ir.expr.TypedNameDecl` through the path every other
    ``name:type`` position in the grammar uses. Their names are recorded
    verbatim, an asymmetry with a ``let``-declared function's parameters, which
    become ``$param<i>``. What differs is what the name binds. A function
    parameter is bound inside the body, where the builder shadows it (see
    :class:`LetFunctionParameter`), so every reference is identifiable and
    renaming declaration and references together is a spelling fold. A pattern
    parameter names a match slot: an arm supplies values positionally through
    :attr:`PatternMatch.values` and the body reads none of them, so the builder
    shadows nothing for a pattern arm. With nothing to rename alongside the
    declaration, renaming it alone would fold two spellings of a signature and
    nothing else, a merge a deduplicating consumer cannot recover from.

    Microsoft's binder crashes outright on a pattern whose match arm supplies
    more values than the declaration has parameters
    (``IndexOutOfRangeException`` from ``VisitPatternDeclaration``, in
    Kusto.Language 12.3.2 through 12.4.1). The parse is clean, so nothing warns
    first. kustology contains it: every analyze call falls back to the
    unanalyzed parse and records an ``Error`` diagnostic of its own — see
    :func:`kustology.services._analyze_guarded`.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["pattern_statement"] = "pattern_statement"
    name: str
    declared_only: bool = False
    parameters: list[TypedNameDecl] = []
    path_parameter: TypedNameDecl | None = None
    matches: list[PatternMatch] = []


class AliasStmt(BaseModel):
    """``alias database db1 = cluster('c').database('d')`` — a local name for a database.

    ``expression`` is what the alias points at, and it is the whole point of
    the statement: two aliases naming different databases send every qualified
    reference in the query somewhere else, so dropping it would give them one
    digest, shared with a query that declares no alias at all.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["alias_statement"] = "alias_statement"
    name: str
    expression: AnyExpr


class RestrictStmt(BaseModel):
    """``restrict access to (database("d"), T) with (a=1)`` — narrow what the rest of the query can see.

    ``expressions`` is the entity list, built after the ``let`` sweep so a name
    an earlier ``let`` bound resolves to that binding rather than to a column
    of whatever the pipeline reads.

    They are :data:`~kustology.ir.expr.AnyExpr` because that is the position
    the grammar puts them in, with a consequence worth stating: a bare entity
    name arrives as a :class:`~kustology.ir.expr.ColumnRef` (or a
    :class:`~kustology.ir.expr.LetValueRef` when bound), so
    ``find_all(ir, TableRef)`` does not report a restrict target and a lineage
    consumer reading only that will not see one. ``database("d")`` arrives as
    the :class:`~kustology.ir.expr.FuncCall` it is written as.

    ``properties`` is the ``with (...)`` clause as a list of pairs, the shape
    :attr:`ParseKvOp.properties` uses and for the same reason: a repeated name
    would collapse onto its last value in a dict. ``[]`` means the clause was
    absent.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["restrict_statement"] = "restrict_statement"
    expressions: list[AnyExpr] = []
    properties: list[tuple[str, str]] = []


class UnknownStmt(BaseModel):
    """Statement the IR builder couldn't dispatch — captures provenance.

    The statement-position sibling of :class:`UnknownOp` and
    :class:`~kustology.ir.expr.UnknownExpr`, defensive in the same register: it
    exists so a statement kind a future Kusto.Language adds lands somewhere
    visible instead of vanishing. No statement the bundled parser emits reaches
    it.

    It is the one statement model that carries ``raw_text``. Recorded source
    text hashes as text, so a node carrying it discriminates on formatting and
    would let a test pair pass for a reason unrelated to the field it claims to
    guard, which is why
    ``test_hash_battery.py::test_no_battery_pair_discriminates_on_an_unmodelled_blob``
    asserts no battery query reaches a text-carrying node, this one included.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["unknown_stmt"] = "unknown_stmt"
    raw_text: str


# Discriminated on the kind literal; member order is not load-bearing.
AnyStatement = Annotated[
    SetOptionStmt
    | QueryParametersStmt
    | PatternStmt
    | AliasStmt
    | RestrictStmt
    | UnknownStmt,
    Field(discriminator="kind"),
]


class QueryIR(BaseModel):
    """Root of the IR: one parsed query, its statements, and its digest."""

    model_config = {"extra": "forbid"}
    kind: Literal["query"] = "query"
    raw_text: str
    let_bindings: list[LetBinding]
    # Every statement that is neither a ``let`` nor a tabular expression, in
    # source order. The order is semantic -- ``set`` scopes the query that
    # follows it -- and a list keeps it for free where a per-kind field could
    # not.
    #
    # The field is always present in the ``semantic_hash`` payload, empty list
    # included, mirroring ``additional_pipelines``. Omitting the key when the
    # list is empty, the way ``_strip_unwritten_fields`` omits an unwritten
    # operator modifier, would buy a stable digest for statement-free queries at
    # the cost of a payload whose shape depends on its own contents.
    statements: list[AnyStatement] = []
    main_pipeline: Pipeline
    # The second and later tabular statements of a multi-statement query, in
    # source order. KQL separates statements with ``;`` and a query may hold
    # several tabular ones (``T | count; U | count``), so everything past the
    # first semicolon is part of the query: reachable through
    # ``walk``/``find_all``, visible to the binder, and in the digest. Keeping
    # only the first would give that query exactly the IR and ``semantic_hash``
    # of ``T | count``.
    #
    # ``main_pipeline`` stays the first statement rather than becoming
    # ``pipelines[0]``: the overwhelmingly common query has exactly one, and a
    # required field naming it keeps that case a direct read. Consumers that
    # want every statement iterate ``[ir.main_pipeline, *ir.additional_pipelines]``.
    additional_pipelines: list[Pipeline] = []
    diagnostics: list[Diagnostic] = []
    schema_attached: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_stored_digest(cls, data: Any) -> Any:
        # A dump carries ``semantic_hash`` as a computed field; on the way back
        # in it is recomputed, so the stored value is dropped rather than
        # rejected by ``extra="forbid"``. A keyword constructor call builds the
        # same dict shape internally, so ``QueryIR(..., semantic_hash=<x>)``
        # drops it identically instead of raising: under ``extra="forbid"``
        # there is no way to accept a stored dump without accepting the keyword.
        if isinstance(data, dict) and "semantic_hash" in data:
            return {k: v for k, v in data.items() if k != "semantic_hash"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def semantic_hash(self) -> str:
        """Return the SHA-256 digest of the canonical IR shape, computed on first read and memoized.

        Two queries with the same semantic content collide; see
        ``docs/semantic-hash.md`` for what the digest ignores. Mutating the IR
        afterwards leaves the memoized value stale — call
        :func:`kustology.ir.compute_semantic_hash` for a fresh one. A copy
        computes its own: ``model_copy()`` in either depth and
        ``copy.deepcopy`` all hand back an IR with no memo. Distinct from Tier
        1's :meth:`KustoQuery.get_structural_hash`, which is literal- and
        identifier-blind.
        """
        from .transforms import compute_semantic_hash

        return compute_semantic_hash(self)

    def __deepcopy__(self, memo: dict | None = None) -> "QueryIR":
        """Deep-copy without carrying the memoized digest into the copy.

        ``cached_property`` stores ``semantic_hash`` in the instance
        ``__dict__``, which pydantic copies along with the fields, so the copy
        would answer its first read with the source's digest. A copy is a
        different tree the moment the caller mutates it, which is the point of
        the copy-then-mutate workflow
        :func:`~kustology.ir.merge_consecutive_filters` documents, so it
        recomputes.
        """
        copied = super().__deepcopy__(memo)
        copied.__dict__.pop("semantic_hash", None)
        return copied

    def __copy__(self) -> "QueryIR":
        """Shallow-copy without carrying the memoized digest; see :meth:`__deepcopy__`.

        A shallow copy shares the operator lists, so its digest goes stale
        under exactly the mutation a deep copy's does.
        """
        copied = super().__copy__()
        copied.__dict__.pop("semantic_hash", None)
        return copied

    def to_llm_dict(self) -> dict[str, Any]:
        """LLM-friendly serialization. See :mod:`kustology.ir.llm_view`."""
        from .llm_view import to_llm_dict
        return to_llm_dict(self)


# The expression classes rebuild first, and here rather than in ``expr.py``:
# ``ToScalarExpr.pipeline`` and ``SubqueryExpr.pipeline`` are forward
# references to ``Pipeline``, which does not exist until this module has run,
# and both classes are members of ``AnyExpr``, so every expression class with
# an ``AnyExpr`` field needs them resolved too. See
# ``expr.REBUILT_BY_QUERY_MODULE``.
#
# The rebuild resolves the reference from the calling module's namespace, which
# is this one; ``expr.py``'s own globals do not hold ``Pipeline``, since its
# import there is ``TYPE_CHECKING``-only. ``test_nested_pipelines`` asserts
# both classes come out complete with the resolved annotation, so a pydantic
# change that stopped reaching the caller's namespace fails loudly instead of
# leaving an untyped field.
for _expr_cls in REBUILT_BY_QUERY_MODULE:
    _expr_cls.model_rebuild()

Pipeline.model_rebuild()
LetBinding.model_rebuild()
LetFunctionParameter.model_rebuild()
SetOptionStmt.model_rebuild()
QueryParametersStmt.model_rebuild()
PatternMatch.model_rebuild()
PatternStmt.model_rebuild()
AliasStmt.model_rebuild()
RestrictStmt.model_rebuild()
# After the statement models: ``LetFunction.body_query_parameters`` is a
# forward reference to one of them, and ``QueryIR.statements`` a union over
# all six.
LetFunction.model_rebuild()
QueryIR.model_rebuild()
UnionOp.model_rebuild()
MvApplyOp.model_rebuild()
LookupOp.model_rebuild()
PartitionOp.model_rebuild()
FacetOp.model_rebuild()
ForkBranch.model_rebuild()
ForkOp.model_rebuild()
MacroExpandOp.model_rebuild()
