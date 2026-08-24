# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field

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
# Python class name. It is the discriminator behind ``Pipeline.source``/
# ``.operators``, ``SearchOp.tables`` and ``FindOp.tables``, and what
# ``ir.llm_view.to_llm_dict`` reads via ``model_fields["kind"].default`` to
# lead every emitted dict.

class Diagnostic(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["diagnostic"] = "diagnostic"
    message: str
    severity: str
    span: Span | None = None
    code: str | None = None
    category: str | None = None


class TabularSchema(BaseModel):
    """Tabular result type: ``{column_name: kusto_type_string}``, in the order
    the engine emits them.

    Carried by :class:`Operator` and :class:`Pipeline`. On a bound parse it
    is Microsoft's binder's answer, captured at build time; otherwise
    ``SchemaAttacher``'s, derived from its own walk.

    **A column whose type is not known is the string ``"unknown"``** — not
    ``KustoType.UNRESOLVED``, whose value is ``"unresolved"``. The two
    sentinels are not interchangeable and live in different fields:
    :attr:`Expr.result_type` is a :class:`~kustology.ir.types.KustoType`, so
    an unplaced expression type is ``KustoType.UNRESOLVED``; ``columns``
    values are Microsoft's type *names* as strings, and Microsoft's own name
    for the absent one is ``ScalarTypes.Unknown.Name`` == ``"unknown"``.
    Both producers therefore agree on Microsoft's word: a bound parse
    propagating a column the binder could not type, and
    ``SchemaAttacher`` falling back on an expression whose ``result_type``
    stayed ``KustoType.UNRESOLVED``. Test a ``columns`` value against
    ``"unknown"``; test a ``result_type`` against ``KustoType.UNRESOLVED``.

    Distinct again from ``columns is None`` on the enclosing
    :attr:`Operator.result_schema` / :attr:`Pipeline.result_schema`, which
    means *no schema was determined at all*, and from ``columns == {}``,
    which claims the step emits no columns."""
    model_config = {"extra": "forbid"}
    kind: Literal["tabular_schema"] = "tabular_schema"
    columns: dict[str, str] = {}


class Assignment(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["assignment"] = "assignment"
    name: str
    expr: AnyExpr
    span: Span


class Operator(BaseModel):
    # ``extra="forbid"`` is load-bearing for round-trip safety: without it,
    # Union resolution silently absorbs unknown fields, so a FilterOp JSON
    # could validate as a fields-less GetSchemaOp (predicate dropped). The
    # policy is consistent across every IR BaseModel — see ``Expr`` and
    # ``Span`` for the rest.
    model_config = {"extra": "forbid"}

    kind: Literal["operator"] = "operator"
    span: Span
    # ``hint.*`` named parameters, verbatim keys included: ``hint.strategy``,
    # ``hint.shufflekey``, ``hint.spread``, ``hint.remote``. Declared on the
    # base class because the grammar admits them on many operators
    # (``join``, ``summarize``, ``mv-expand``, ``partition``, ``evaluate``,
    # …) and the builder stamps them from one place, so a new operator
    # cannot be added without them.
    #
    # **Volatile: excluded from ``semantic_hash``** (``hints`` is in
    # ``transforms._VOLATILE_FIELDS``). A hint asks the engine to execute a
    # query differently -- shuffle the join, spread the expansion -- and does
    # not change the rows it returns, so ``join hint.strategy=shuffle`` and
    # ``join`` are the same query for deduplication purposes. That is the
    # opposite call from every other field in this task, and it is the whole
    # reason the field is here rather than being folded into an operator's
    # own parameters: a consumer that wants to see the tuning can read it,
    # and a consumer deduplicating rules does not see two rules.
    hints: dict[str, str] = {}
    # The columns this operator *emits*, straight from Microsoft's binder
    # (``<operator node>.ResultType``), captured at build time whenever the
    # parse was bound and the symbol is closed -- see
    # :func:`kustology.ir._builder_helpers.table_symbol_columns` for what
    # "closed" buys and why an open one is dropped instead of read.
    #
    # ``SchemaAttacher`` prefers this over its own per-operator rule, which
    # is the point: the rules were re-deriving an answer the binder already
    # had, and a dozen of them disagreed with it. ``None`` means Microsoft
    # did not answer for this operator (no schema, or a schema it could not
    # fully determine) and the hand-rolled rule is what runs.
    #
    # **Volatile: excluded from ``semantic_hash``.** The field name is
    # already in ``transforms._VOLATILE_FIELDS`` for ``Pipeline``, and that
    # set is keyed by model field name rather than by owning class, so this
    # declaration is covered by the same entry. It has to be: a query's
    # digest must not depend on whether the caller supplied a schema.
    result_schema: TabularSchema | None = None


class FilterOp(Operator):
    kind: Literal["filter"] = "filter"
    predicate: AnyExpr


class ExtendOp(Operator):
    kind: Literal["extend"] = "extend"
    assignments: list[Assignment]


class SummarizeOp(Operator):
    kind: Literal["summarize"] = "summarize"
    aggregations: list[Assignment]
    by: list[ColumnRef | AnyExpr | Assignment]


class ProjectOp(Operator):
    kind: Literal["project"] = "project"
    columns: list[ColumnRef | Assignment | AnyExpr]


class TableRef(BaseModel):
    """A table named in source position, with whatever qualified it.

    ``name`` is the bare table name, and stays the bare name even when the
    query wrote ``cluster('c').database('d').T`` -- schema lookups are keyed
    on it (see :meth:`SchemaAttacher._source_entry`), so folding the
    qualifiers into the string would stop every qualified query resolving.

    ``database`` and ``cluster`` are the qualifiers the query stated, and
    they are data rather than decoration: ``database('d1').T`` and
    ``database('d2').T`` read two different tables and used to build the
    same node, so they carried the same ``semantic_hash``.

    ``is_wildcard`` marks a pattern rather than a name -- ``union T*``
    matches a *set* of tables, and without the flag it was indistinguishable
    from a literal table that happens to be called ``T*`` (``union ['T*']``,
    which is a legal and different query).
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
    expected to hold. The distinction is decidable from the ``let``
    statements alone -- no schema and no binder -- so it holds identically
    for a bound and an unbound parse.

    Only bindings that *precede* the reference count. ``let A = A | …`` and
    a reference to a binding declared further down both stay a ``TableRef``:
    resolving them to the binding would be a guess rather than a reading.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["let_ref"] = "let_ref"
    name: str
    span: Span


class UnknownSource(BaseModel):
    """Source the IR builder couldn't model — captures provenance.

    ``raw_text`` is the node's own source, ``ToString(IncludeTrivia.Minimal)``.
    It used to be the constant string ``"unknown"``, which made every
    unmodelled source hash identically no matter what the query said.

    **Known boundary: an unmodelled source is formatting-sensitive in the
    hash.** ``Minimal`` drops the node's *leading* trivia but not trivia
    *interior* to it — no ``IncludeTrivia`` mode does, checked against all
    four — so ``let /*c*/ x = 1;`` and ``let x = 1;`` produce different
    ``semantic_hash`` values. Stripping comments textually is ruled out for
    the reason ``transforms._normalize_raw_text`` records: ``//`` is the
    middle of every URL, and a run of spaces inside a string literal is
    data. This is accepted rather than fixed because the direction is safe:
    it is a false *split* (a dedup consumer fails to merge two spellings of
    one query), never a false *merge* (two different queries sharing a
    digest), and it reaches only the sources the builder already could not
    model. :class:`~kustology.ir.expr.UnknownExpr` and :class:`UnknownOp`
    have carried the same property since they were written.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["unknown_source"] = "unknown_source"
    raw_text: str
    span: Span


class ImplicitSource(BaseModel):
    """Source whose rows come from a parent context (union-at-root subqueries,
    ``mv-apply``/``partition``/``fork`` inner pipelines, parenthesized
    ``join``/``lookup`` RHS). Distinct from :class:`UnknownSource`, which means
    the source couldn't be determined.
    """
    model_config = {"extra": "forbid"}
    kind: Literal["implicit_source"] = "implicit_source"
    span: Span


class FuncCallSource(BaseModel):
    """Function-call-as-pipeline-source — a user-defined function that returns
    a table, e.g. ``findAnomalies('foo') | summarize ...``."""
    model_config = {"extra": "forbid"}
    kind: Literal["func_call_source"] = "func_call_source"
    name: str
    args: list[AnyExpr] = []
    span: Span


class DataTableSource(BaseModel):
    """``datatable(a:int, b:string)[1,"x",2,"y"]`` — an inline table literal.

    The values *are* the query: a ``datatable`` of allow-listed hashes and
    the same ``datatable`` of different hashes are two different queries.
    The builder used to lower every one of them to
    ``FuncCallSource(name="datatable", args=[])``, discarding the schema and
    every row, so they all shared one ``semantic_hash``.

    ``rows`` is the reshape of what the parser hands over. ``Values`` on the
    .NET node is a *flat* list of expressions with no row structure at all;
    the row width comes from ``len(columns)``. Cells are expressions rather
    than plain Python values because KQL admits any scalar expression there
    (``datatable(t:datetime)[ago(1d)]``), and because a ``LiteralExpr``
    carries the literal kind — ``long`` vs ``real`` vs ``timespan`` — that a
    bare Python value cannot.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["datatable_source"] = "datatable_source"
    columns: list[tuple[str, str]]
    rows: list[list[AnyExpr]]
    span: Span


class ExternalDataSource(BaseModel):
    """``externaldata(a:string)[uri, …] with (format="csv")`` in source position.

    Distinct from :class:`~kustology.ir.expr.ExternalDataExpr`, which is the
    same construct sitting in *expression* position (the value set of a
    membership test). They share
    :func:`~kustology.ir._builder_helpers.read_external_data` so the two
    cannot drift.

    ``uris`` is a list because the construct takes one: a feed assembled
    from two URIs is not the feed from one of them. An entry is **not
    guaranteed to be a URI**: when the element does not fold to a literal —
    a ``let``-bound feed URL, or ``strcat("https://", env)`` — the field
    records that element's source text instead (``"u"``, or the whole call
    as written). Resolving those needs the query, not just this field.

    ``properties`` is the whole ``with (...)`` clause, keys verbatim, in the
    same ``dict[str, str]`` shape :attr:`RenderOp.properties` uses. Only
    ``format`` used to be read and the rest were dropped, which was a
    collision rather than a cosmetic gap: ``ignoreFirstRecord=true`` skips
    the CSV header row, so it changes the rows the feed returns, and a
    source node has no ``raw_text`` for the dropped text to survive in.
    ``format`` remains as its own field because the rest of the library
    reads it; it is *also* present in ``properties`` under the name the
    query wrote, so a consumer reconstructing the clause sees a complete
    one.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["external_data_source"] = "external_data_source"
    columns: list[tuple[str, str]]
    uris: list[str]
    format: str | None = None
    properties: dict[str, str] = {}
    span: Span


class DistinctOp(Operator):
    kind: Literal["distinct"] = "distinct"
    columns: list[ColumnRef | Assignment | AnyExpr]


class TakeOp(Operator):
    kind: Literal["take"] = "take"
    # KQL allows any scalar expression here (`let n = 10; T | take n`,
    # `take toscalar(U | count)`), not just an integer literal, so the field
    # widens to ``AnyExpr``. ``int`` is listed first so Pydantic validates a
    # JSON literal ``5`` as a plain ``int`` -- matching existing ``op.count
    # == 5`` assertions and downstream consumers -- instead of coercing it
    # into an expression model; the four sibling ops below follow suit.
    count: int | AnyExpr


class SortKey(BaseModel):
    """One ordering key of ``sort by`` / ``order by`` / ``top … by``.

    The expression alone is not the key: ``sort by x asc`` and
    ``sort by x desc`` return rows in opposite orders and used to build
    identical IR, because the builder unwrapped the AST's
    ``OrderedExpression`` and dropped its ordering clause on the floor.

    ``direction`` is **required and has no default**, which is deliberate and
    is not the same statement as "the query always writes it". KQL's
    unwritten default is ``desc``, so a bare ``sort by x`` records
    ``direction="desc"`` — the *effective* value, never ``None``. Declaring
    it required is what makes that value visible: ``ir.llm_view.to_llm_dict``
    drops any field still holding its declared default, so a defaulted
    ``direction`` would vanish from the LLM view exactly on the queries where
    the reader has no other way to tell which way the rows come back.

    ``nulls`` keeps its ``None`` when the query does not write it, and it is
    the only D8 field on this model that does not carry KQL's effective
    default. Not because KQL leaves null placement undefined — it does not.
    Microsoft documents it, and documents it as *direction-dependent*:
    "Default for ``asc`` is ``nulls first``. Default for ``desc`` is ``nulls
    last``" (`sort operator
    <https://learn.microsoft.com/en-us/kusto/query/sort-operator>`_, revised
    2025-01-21). Two things follow from that, and together they are the
    reason:

    * Every other D8 default is a **constant that adds information the IR
      would otherwise lack** — a bare ``join`` really is ``innerunique``, and
      nothing else on the node says so. This one is derivable from
      ``direction``, which is already on the same node, so substituting it
      would put a computed value in a field whose remaining job is to answer
      "did the query write this?".
    * Substituting it would **merge two spellings** in ``semantic_hash``:
      ``sort by x asc`` currently splits from ``sort by x asc nulls first``,
      and would stop. That merge is arguably correct, but a dedup consumer
      survives a failure to merge and cannot survive a wrong one, so the
      split is the safe side of a call we did not have to make for 0.2.0.

    An earlier version of this docstring justified the asymmetry structurally
    — the nulls clause being "grammatically independent" of ``asc``/``desc``.
    That is true of the parse tree and is not a reason: .NET's
    ``OrderingClause`` carries ``AscOrDescKeyword`` and ``NullsClause`` as
    independently optional peers, and ``sort by x nulls first`` — where
    ``AscOrDescKeyword`` *is* ``None`` — already records ``direction="desc"``
    right beside ``nulls="first"``. The grammar treats them alike; the
    asymmetry is our choice about what the field means.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["sort_key"] = "sort_key"
    expression: AnyExpr
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"] | None = None
    span: Span


class SortOp(Operator):
    kind: Literal["sort"] = "sort"
    expressions: list[SortKey]


class TopOp(Operator):
    kind: Literal["top"] = "top"
    count: int | AnyExpr
    by: SortKey


class TopHittersOp(Operator):
    kind: Literal["top_hitters"] = "top_hitters"
    count: int | AnyExpr
    # ``top-hitters N of C [by V]`` has two column operands, not one: ``of``
    # is the column whose top values are being found, ``by`` the optional
    # weight summed to rank them. Only ``by`` existed, and the builder filled
    # it from a member that exists on no node -- so both operands were
    # unrepresentable and the operator raised instead of lowering.
    #
    # ``of`` is required and ``by`` is not, because the grammar says so and
    # the parser agrees: ``OfExpression`` is never ``None`` even for a bare
    # ``T | top-hitters`` (the error-tolerant parser synthesizes a name node
    # with ``IsMissing`` set), while ``ByClause`` really is a plain ``None``
    # when the clause is absent. A default on ``of`` would be unreachable
    # from the builder and would let a hand-written payload validate without
    # its mandatory operand -- ``extra="forbid"`` rejects unknown keys, not
    # missing ones. ``SampleDistinctOp.of`` is the same ``N of C`` grammar
    # slot and is declared the same way.
    of: AnyExpr
    by: AnyExpr | None = None


class SampleOp(Operator):
    kind: Literal["sample"] = "sample"
    count: int | AnyExpr


class SearchOp(Operator):
    """``search`` — a term match across columns, optionally scoped to tables.

    ``tables`` is the ``in (A, B)`` scope, which decides *what is searched*:
    ``search in (A) 'x'`` and ``search in (B) 'x'`` read different tables and
    used to build the same node, since the in-clause was not read at all.
    Entries are :class:`TableRef`, or :class:`LetRef` when an earlier ``let``
    bound the name -- the same reading the pipeline's own source position
    gets, so a qualifier (``database('d').T``) and a wildcard (``T*``) survive
    here too.

    ``search_kind`` is required and carries KQL's effective default
    ``"default"`` for an unwritten ``kind=`` (D8). The value set is not a
    documentation guess: a bound parse of ``search kind=bogus 'x'`` is
    diagnosed *"Expected one of: default, case_insensitive,
    case_sensitive"*, so the grammar in the bundled DLL names ``default``
    itself. Leaving the field optional split two spellings of one query --
    a bare ``search`` and ``search kind=default`` hashed apart.

    One residual, and it is a *split* rather than a merge: Microsoft
    documents ``case_insensitive`` as a synonym for ``default``, and the two
    are recorded verbatim, so they still hash apart. Folding them would mean
    reporting a value the query did not write, which is a different decision
    from substituting an unwritten default and is not made here.
    """

    kind: Literal["search"] = "search"
    predicate: AnyExpr | None = None
    search_kind: str
    # Discriminated on the kind literal; member order is not load-bearing.
    tables: list[Annotated[TableRef | LetRef, Field(discriminator="kind")]] = []


class UnionOp(Operator):
    """``union`` — concatenate the rows of two or more tables.

    ``union_kind`` decides the *output schema*: ``outer`` keeps every column
    any input has, ``inner`` keeps only the columns they all share. Two
    queries differing in it return different columns, and the builder read
    neither, so they shared a node and a hash.

    Required with no default, holding KQL's effective value ``"outer"`` for
    a bare ``union`` — see :class:`ParseOp.parse_kind` for why the field is
    declared this way rather than defaulted.

    ``withsource=C`` adds a column naming the source table of each row and
    ``isfuzzy=true`` downgrades a missing table from an error to a warning;
    both change the result, and both are genuinely optional (no column and
    no fuzziness are not values either parameter can state).
    """

    kind: Literal["union"] = "union"
    pipelines: list["Pipeline"]
    union_kind: str
    is_fuzzy: bool = False
    withsource: str | None = None


class MakeSeriesAggregate(BaseModel):
    """One ``make-series`` aggregate, with the value that fills its gaps.

    ``default=0`` and ``default=1`` produce different series -- the value
    substituted into every bucket with no rows -- and the builder unwrapped
    the parser's ``MakeSeriesExpression`` to the inner assignment, so the
    clause was dropped and all three of ``default=0``, ``default=1`` and no
    default built the same node.

    ``name`` and ``expr`` are spelled as on :class:`Assignment` deliberately:
    the binder reads ``a.name`` / ``a.expr`` over this list and needs no
    change for the element type having grown a third field.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["make_series_aggregate"] = "make_series_aggregate"
    name: str
    expr: AnyExpr
    default: AnyExpr | None = None
    span: Span


class MakeSeriesOp(Operator):
    kind: Literal["make_series"] = "make_series"
    aggregations: list[MakeSeriesAggregate]
    by: list[Assignment]
    on_column: AnyExpr | None = None
    range_from: AnyExpr | None = None
    range_to: AnyExpr | None = None
    step: AnyExpr | None = None


class MvExpandColumn(BaseModel):
    """One expanded column of ``mv-expand``, with its declared element type.

    ``mv-expand a to typeof(string)`` tells KQL the expanded rows are
    strings, which changes the output column's type and therefore what
    downstream operators can do with it. The builder unwrapped the parser's
    ``MvExpandExpression`` to its inner expression and dropped the
    ``ToTypeOf`` clause, so the typed and untyped forms built identical IR.

    ``to_typeof`` is the type as the query wrote it (``string``, ``long``),
    not a resolved :class:`~kustology.ir.types.KustoType` -- the same
    reasoning as :class:`~kustology.ir.expr.TypedNameDecl.declared_type`.

    It stays optional, and *not* on the argument that the unwritten
    behaviour is unstatable: ``mv-expand a to typeof(dynamic)`` parses and
    binds with no diagnostic, so the clause can name what an unwritten one
    leaves behind. It stays optional because the two are not equivalent --
    writing ``to typeof(dynamic)`` asserts the element type where omitting
    the clause leaves the binder to infer one from ``dynamic<T>``, and the
    IR should not claim the query stated a type it did not. The two spellings
    hash apart, which is a split rather than a merge.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["mv_expand_column"] = "mv_expand_column"
    expression: AnyExpr
    to_typeof: str | None = None
    span: Span


class MvExpandOp(Operator):
    """``mv-expand`` — one output row per element of a dynamic column.

    Every modifier here changes the rows the operator returns and every one
    of them used to be discarded, so ``mv-expand a``,
    ``mv-expand a limit 10`` and ``mv-expand with_itemindex=i a`` were one
    node with one ``semantic_hash``.

    ``expand_kind`` is required and carries KQL's effective default
    ``"bag"`` (D8). It is **one field for two spellings**: ``kind=bag`` and
    the deprecated ``bagexpansion=bag`` are the same modifier, which the DLL
    confirms by giving both the same value set (``kind=bogus`` and
    ``bagexpansion=bogus`` are each diagnosed *"Expected one of: bag,
    array"*). Two fields split those spellings in the hash, the way reading
    only ``render``'s ``with`` clause would have split *its* two spellings.
    A query writing both -- which parses clean in 12.3.2 -- records
    ``kind``, the modern spelling, as ``render``'s merge prefers its modern
    spelling too.

    ``row_limit`` and ``with_item_index`` stay optional. That is not because
    KQL has no unwritten behaviour -- the documented implicit ``limit`` is
    ``2147483647``, and that literal parses clean -- but because the
    unwritten case is not *stated* by any value: recording 2147483647 on
    every bare ``mv-expand`` would report a bound the query never set and
    would collapse it onto the query that really did set it. D8 substitutes
    a *named mode*, not a magic number.
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
    ``series``, …) plus the legacy bare-parameter spelling of the same
    thing: KQL accepts both ``render columnchart kind=stacked`` and
    ``render columnchart with (kind=stacked)``, and folding them into one
    dict is what makes those two spellings of one query hash alike.

    None of it was read, so every ``render timechart`` was one node however
    it was configured.
    """

    kind: Literal["render"] = "render"
    render_kind: str
    properties: dict[str, str] = {}


class ProjectAwayOp(Operator):
    kind: Literal["project_away"] = "project_away"
    columns: list[ColumnRef | AnyExpr]


class ProjectKeepOp(Operator):
    kind: Literal["project_keep"] = "project_keep"
    columns: list[ColumnRef | AnyExpr]


class ReorderKey(BaseModel):
    """One term of ``project-reorder``: a column or wildcard, plus its order.

    Sibling to :class:`SortKey` — both wrap the parser's ``OrderedExpression``
    — but the two differ on exactly the point D8 turns on, and reusing
    ``SortKey`` here would be wrong twice over.

    ``project-reorder``'s ``asc``/``desc`` orders **columns**, not rows: it
    decides the left-to-right order of the columns a term matches, which is
    why it earns its keep on wildcards (``project-reorder a* asc``). Omitting
    it does not select a KQL default the way a bare ``sort by x`` selects
    ``desc``; it means "emit these in the order I listed them". There is no
    effective value to substitute, so ``direction`` is genuinely optional and
    ``None`` is the honest record. Stamping ``"desc"`` on a bare term would
    both misreport it and collapse it against an explicit ``desc`` — a new
    hash collision in the act of fixing one.

    Because ``None`` is a real declared default here, ``to_llm_dict`` drops
    the field on unwritten terms and renders it on written ones, which is the
    correct reading in both directions.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["reorder_key"] = "reorder_key"
    expression: AnyExpr
    direction: Literal["asc", "desc"] | None = None
    span: Span


class ProjectReorderOp(Operator):
    kind: Literal["project_reorder"] = "project_reorder"
    columns: list[ReorderKey]


class ProjectRenameOp(Operator):
    kind: Literal["project_rename"] = "project_rename"
    columns: list[Assignment]


class ProjectByNamesOp(Operator):
    kind: Literal["project_by_names"] = "project_by_names"
    names: list[AnyExpr]


class MvApplyOp(Operator):
    kind: Literal["mv_apply"] = "mv_apply"
    assignments: list[Assignment]
    right: "Pipeline"


class ParseOp(Operator):
    """``parse`` — extract capture columns from a string expression.

    ``parse_kind`` selects the matching engine and the three values are not
    interchangeable: ``simple`` matches the pattern literally, ``regex``
    treats it as a regular expression, ``relaxed`` tolerates a failed match
    instead of nulling the row. The builder read none of them, so all three
    built the same node.

    It is **required and has no default**, carrying KQL's *effective* value
    (D8): a bare ``parse`` records ``"simple"``, never ``None``. As on
    :class:`SortKey.direction`, declaring it required is what makes the
    value visible — ``to_llm_dict`` drops a field still holding its declared
    default, so a defaulted ``parse_kind`` would vanish exactly where the
    reader has no other way to tell which engine is in force.

    ``flags`` is the ``flags='i'`` regex modifier and is genuinely optional:
    no flags is not a flag string.
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
    """``evaluate`` — run a plug-in. Its output-schema clause is not modeled.

    ``evaluate bag_unpack(d) : (x:string)`` attaches an
    ``EvaluateSchemaClause`` (the .NET property is ``Schema``) declaring the
    columns the plug-in returns. There is no field for it here, so the
    builder drops it and the clause reaches neither the IR nor
    ``semantic_hash``: two spellings with different declared schemas, and
    one with none at all, are one digest. The binder still derives the real
    ``result_schema`` from the clause, so ``result_schema`` and the digest
    disagree about whether the queries differ. Documented as a known
    collision in :func:`~kustology.ir.transforms.compute_semantic_hash`;
    modelling it is post-0.2.0 work.
    """

    kind: Literal["evaluate"] = "evaluate"
    func: FuncCall


class CountOp(Operator):
    kind: Literal["count"] = "count"
    as_name: str | None = None


class PrintOp(Operator):
    kind: Literal["print"] = "print"
    columns: list[Assignment | AnyExpr]


class AsOp(Operator):
    kind: Literal["as"] = "as"
    name: str


class RangeOp(Operator):
    kind: Literal["range"] = "range"
    column: str
    start: AnyExpr
    end: AnyExpr
    step: AnyExpr


class LookupOp(Operator):
    """``lookup`` — enrich the left table from a dimension table.

    ``lookup_kind`` is required and carries KQL's effective default
    ``"leftouter"`` for an unwritten ``kind=`` (D8). The builder used to
    substitute ``"inner"``, which is a *different* operator: ``leftouter``
    keeps left rows with no match and ``inner`` drops them, so a bare
    ``lookup`` was recorded as the one thing it is not.
    """

    kind: Literal["lookup"] = "lookup"
    lookup_kind: str
    right: "Pipeline"
    # KQL ``on Foo`` is sugar for ``on $left.Foo == $right.Foo``; both surface
    # here as ``AnyExpr`` (a bare ``ColumnRef`` or a full equality ``BinOp``).
    on: list[AnyExpr]


class PartitionOp(Operator):
    kind: Literal["partition"] = "partition"
    by: AnyExpr
    right: "Pipeline"


class FacetOp(Operator):
    kind: Literal["facet"] = "facet"
    columns: list[AnyExpr] = []
    with_pipeline: Optional["Pipeline"] = None


class GetSchemaOp(Operator):
    kind: Literal["getschema"] = "getschema"


class InvokeOp(Operator):
    kind: Literal["invoke"] = "invoke"
    func: FuncCall


class FindOp(Operator):
    """``find`` — search rows across a set of tables.

    ``tables`` is the ``in (T, U)`` scope, read the same way as the
    pipeline's own source position and :class:`SearchOp.tables`, so a
    qualifier, a wildcard and a ``let`` alias each survive. It was
    ``list[str]`` filled with ``el.ToString().strip()`` -- the no-argument
    overload, which is ``IncludeTrivia.All`` -- so a comment written before
    a table name became *part of the name* and ``find in (// note`` ↵ ``T)``
    hashed differently from ``find in (T)``.

    ``project`` is the ``project a, b`` column list, which decides the
    output schema; a typed column (``project a:string``) arrives as a
    :class:`~kustology.ir.expr.TypedNameDecl`. ``withsource=C`` names the
    column recording which table each row came from.

    There is no ``project_away`` field. ``FindOperator.ProjectAway`` exists
    as a member on the .NET node, but no spelling of the clause reaches it
    in the bundled parser (Kusto.Language 12.3.2): the eight forms probed --
    including Microsoft's own documented example -- all parse
    ``project-away`` as a *separate* ``ProjectAwayOperator`` statement with
    an ``Expected: ;`` diagnostic. A declared field nothing can populate
    reads as implemented and cannot be tested (AGENTS.md), so it is left out
    until a DLL refresh makes the clause reachable.
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
    table per branch. ``name`` is the ``a=`` prefix — a ``NameEqualsClause``
    in the AST — which names that result table, so it is data rather than
    formatting and two forks differing only in a branch name are two
    different queries.

    The branch is a wrapper rather than a bare :class:`Pipeline` because the
    name has nowhere else to live: hanging a parallel ``list[str | None]``
    off :class:`ForkOp` would pair name to pipeline by index, which is the
    kind of coupling that silently misaligns the first time a branch is
    dropped or reordered.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["fork_branch"] = "fork_branch"
    name: str | None = None
    pipeline: "Pipeline"
    span: Span


class ForkOp(Operator):
    """``fork`` — one branch per parenthesized sub-pipeline.

    The field is ``branches`` and not ``pipelines`` because it used to be
    ``pipelines`` and it used to be empty: the builder handed each
    ``ForkExpression`` to ``_visit_pipeline``, whose walker had no case for
    that node kind, so every branch came back with no operators and an
    ``UnknownSource``. Renaming the field is what stops a dump written
    against the old shape from validating -- under ``extra="forbid"`` a
    stored ``pipelines`` key now fails loudly instead of quietly producing
    the empty branches it recorded.
    """

    kind: Literal["fork"] = "fork"
    branches: list[ForkBranch]


class ScanOp(Operator):
    """``scan`` — kept as its own source text; the step machine is not modeled.

    This is the first of eight *modelled* operators the IR records on
    ``raw_text`` rather than in typed fields, and the register is the same
    as :class:`LetFunction`'s: the boundary is stated in the model instead
    of being left as fields that read as implemented and are not. The other
    seven are :class:`TopNestedOp`, :class:`MakeGraphOp`,
    :class:`MacroExpandOp`, :class:`GraphMatchOp`,
    :class:`GraphMarkComponentsOp`, :class:`GraphShortestPathsOp` and
    :class:`GraphToTableOp`.

    Two exclusions, so the count is checkable. ``graph-where-edges`` and
    ``graph-where-nodes`` have no ``raw_text`` at all — both carry a real
    ``predicate``. And :class:`UnknownOp` *does* have one, so enumerating
    ``Operator`` subclasses with a ``raw_text`` field gives **nine**, not
    eight; it is excluded here because it is the builder's fallback for a
    shape it could not dispatch rather than an operator anyone chose to
    model this way. Eight is the register; nine is the field count.

    What ``raw_text`` buys and what it does not. It is
    ``ToString(IncludeTrivia.Minimal)``, so these operators round-trip
    through ``model_dump_json`` and they participate in ``semantic_hash``
    as text rather than as structure. There is nothing typed inside them
    to walk: ``find_all(ir, ColumnRef)`` will not report a column that
    appears only in a ``scan`` step. Downstream *scope* is the better
    case, and it splits by bind state — ``Operator.result_schema`` carries
    Microsoft's ``ResultType``, which knows the columns a ``scan``'s
    ``declare`` adds, and :class:`SchemaAttacher` prefers it; on an
    unbound parse there is no such answer and none of these operators has
    a scope rule, so the scope downstream is the one they inherited.
    Hashing text also means the formatting sensitivity
    :class:`UnknownSource` documents applies here too — interior comments
    and interior spacing are part of the digest.

    ``scan``'s own body is a state machine: ``declare`` variables plus
    ``step`` rules with guards and assignments. Modeling it is a feature,
    not a fix.
    """

    kind: Literal["scan"] = "scan"
    raw_text: str


class SerializeOp(Operator):
    kind: Literal["serialize"] = "serialize"
    assignments: list[Assignment] = []


class ConsumeOp(Operator):
    kind: Literal["consume"] = "consume"


class AssertSchemaOp(Operator):
    kind: Literal["assert_schema"] = "assert_schema"
    columns: dict[str, str] = {}


class ExecuteAndCacheOp(Operator):
    kind: Literal["execute_and_cache"] = "execute_and_cache"


class ParseKvOp(Operator):
    kind: Literal["parse_kv"] = "parse_kv"
    target: AnyExpr
    # ``as (b:string, c:long)`` -- a name:type schema, modeled the same way
    # as :class:`AssertSchemaOp`. It was ``list[Assignment]``, which had no
    # expression to hold: a declared key has a type, not a value.
    columns: dict[str, str] = {}


class SampleDistinctOp(Operator):
    kind: Literal["sample_distinct"] = "sample_distinct"
    count: int | AnyExpr
    of: AnyExpr


class TopNestedOp(Operator):
    """``top-nested`` — source text only; see :class:`ScanOp` for the register.

    A chained ``top-nested … by … with others=…`` clause is a list of
    levels, each with its own key expression, aggregate and ``others``
    label. None of that is broken out, so a nested key is invisible to
    ``find_all``.
    """

    kind: Literal["top_nested"] = "top_nested"
    raw_text: str


class MakeGraphOp(Operator):
    """``make-graph`` — source text only; see :class:`ScanOp` for the register.

    The edge columns, the ``with``-clause node table and its key are all
    inside ``raw_text``, so ``find_all(ir, TableRef)`` does not report the
    node table. Tier 1 *does*, but only on a bound parse: over ``Edges |
    make-graph src --> dst with Nodes on n``,
    ``parse(q).get_referenced_tables()`` answers ``{"Edges"}`` and
    ``parse(q, schema=…)`` answers ``{"Edges", "Nodes"}``, because the
    bound path reads the resolved symbol rather than the syntactic
    source positions. ``replace_table("Nodes", …)`` follows the same
    split -- a no-op unbound, a correct rewrite bound.
    """

    kind: Literal["make_graph"] = "make_graph"
    raw_text: str


class MacroExpandOp(Operator):
    """``macro-expand`` — source text, plus the inner pipeline.

    The one member of the :class:`ScanOp` register that is not opaque all
    the way down: the entity-group name and the ``as`` alias stay in
    ``raw_text``, but the parenthesized body is built as a real
    :class:`Pipeline` on ``pipeline``, so its operators and columns are
    walkable. The scope it runs against is not — the alias resolves to one
    entity per expansion, which the IR has no way to enumerate.
    """

    kind: Literal["macro_expand"] = "macro_expand"
    raw_text: str
    pipeline: Optional["Pipeline"] = None


class GraphMatchOp(Operator):
    """``graph-match`` — source text only; see :class:`ScanOp` for the register.

    The pattern, its ``where`` constraint and its ``project`` list are all
    text, so a column named only in a graph pattern does not reach
    ``find_all(ir, ColumnRef)``, and the columns this operator emits are
    not in any downstream scope. That second half is a boundary of the
    graph surface rather than a gap here: Microsoft's binder does not
    place them either, and reports KS142 for a ``| project`` naming one on
    a bound parse.
    """

    kind: Literal["graph_match"] = "graph_match"
    raw_text: str


class GraphMarkComponentsOp(Operator):
    """``graph-mark-components`` — text only; see :class:`ScanOp`.

    ``with_component_id=`` names a column this operator adds. It is inside
    ``raw_text``, so the added column is not in the downstream scope — and,
    as with :class:`GraphMatchOp`, nor is it in Microsoft's.
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
    names, is in ``raw_text`` — which is exactly the information a
    downstream scope would need, so the scope stays whatever the graph
    operators inherited.
    """

    kind: Literal["graph_to_table"] = "graph_to_table"
    raw_text: str


class GraphWhereEdgesOp(Operator):
    """``graph-where-edges (…)`` — modeled, with a real predicate.

    Not part of the :class:`ScanOp` register despite the family name: the
    parenthesized condition is an ordinary expression over edge
    properties, so it is built as one and its columns are walkable.
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

    Symmetric to :class:`kustology.ir.UnknownExpr`. When operator
    dispatch falls through (e.g. ``BadQueryOperator`` or a new operator
    kind introduced in a Kusto.Language upgrade), the builder emits this
    instead of a bare ``Operator(span=...)`` so analyzers can detect
    coverage gaps and the coverage audit has something to grow against.
    """
    kind: Literal["unknown_op"] = "unknown_op"
    raw_text: str
    ast_kind: str
    reason: str


class Pipeline(BaseModel):
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
    # The columns this pipeline emits. Set at build time from the pipe
    # chain's own ``ResultType`` when the parse was bound and Microsoft's
    # symbol is closed -- so ``to_ir(attach_schema=False)`` has the shape
    # without the provenance pass -- and by ``SchemaAttacher.enrich()``
    # otherwise, from the scope its walk leaves. Volatile: see
    # ``Operator.result_schema``.
    result_schema: TabularSchema | None = None


class JoinOp(Operator):
    """``join`` — combine rows from two tables on a condition.

    ``join_kind`` is required and carries KQL's effective default
    ``"innerunique"`` for an unwritten ``kind=`` (D8). That default is not
    ``inner``: ``innerunique`` deduplicates the *left* side's join keys
    first, so a bare ``join`` and ``join kind=inner`` return different row
    counts from the same data. The builder substituted ``"inner"``, which
    both mislabelled every bare join and collapsed it onto the explicit
    ``kind=inner`` spelling in the hash.

    Required with no pydantic default so ``to_llm_dict`` renders it -- see
    :class:`ParseOp.parse_kind`.
    """

    kind: Literal["join"] = "join"
    join_kind: str
    right: Pipeline
    # KQL ``on Foo`` is sugar for ``on $left.Foo == $right.Foo``; both surface
    # here as ``AnyExpr`` (a bare ``ColumnRef`` or a full equality ``BinOp``).
    on: list[AnyExpr]


class LetFunction(BaseModel):
    """A ``let``-declared function's shape. The body is not modeled.

    ``let f = (x:int) { ... }`` yields a .NET ``FunctionDeclaration``, which is
    neither an expression nor a pipeline and so cannot ride on ``rhs_expr`` or
    ``rhs_pipeline``. Recording it explicitly keeps the unmodeled boundary
    legible instead of leaving three silent ``None``s that read as a bug.

    Parameter types, defaults, tabular-vs-scalar bodies and call-site expansion
    are out of scope; ``body_span`` locates the body in the source for callers
    that want the text.

    Two consequences a caller has to know about, both documented at length in
    :func:`~kustology.ir.transforms.compute_semantic_hash`. ``body_span`` is
    volatile, so nothing here except the parameter names reaches
    ``semantic_hash``: two functions with matching names whose bodies do
    entirely different things collide, as do two differing only in a
    parameter's type or default. And the body's tables and columns are
    reachable from Tier 1 (``get_referenced_tables`` walks Microsoft's tree,
    which has the body in it) but not from Tier 2 — ``find_all(ir, TableRef)``
    over a query whose only source is a ``let`` function call comes back
    empty.
    """

    model_config = {"extra": "forbid"}
    kind: Literal["let_function"] = "let_function"
    # Parameter names in declaration order. The function's own name is on the
    # owning LetBinding.
    parameters: list[str] = []
    body_span: Span


class LetBinding(BaseModel):
    """One ``let`` statement. Exactly one ``rhs_*`` field is populated.

    There is no ``category`` discriminator: which field is set already says
    whether the binding is tabular, scalar or a function, and finer labels
    (time-scalar, alias, scalar-subquery) are recoverable from the populated
    right-hand side — ``rhs_expr.literal_kind == "timespan"``, a ``TableRef``
    source with no operators, a ``ToScalarExpr``. A stored label would also
    have entered ``semantic_hash``, making the hash sensitive to our
    classification choices rather than to query semantics.
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
    # through a ``LetRef`` resolves against those columns.
    #
    # Two boundaries remain: a binding naming one declared *later* is not a
    # ``LetRef`` (see that class), so there is nothing to thread; and
    # ``let``-declared functions are recorded, not expanded, so a call site
    # does not acquire the body's schema.
    rhs_pipeline: Pipeline | None = None
    rhs_function: LetFunction | None = None
    # Tables and time expressions found inside rhs_pipeline; empty otherwise.
    # ``inner_tables`` is real tables only -- a hop to an earlier binding
    # (``let B = A | …``) is a ``LetRef``, reachable via
    # ``find_all(rhs_pipeline, LetRef)``. Keeping aliases out means the field
    # answers "which tables does this binding read", which is what a lineage
    # consumer wants, rather than mixing the two kinds of name.
    inner_tables: list[str] = []
    inner_time_exprs: list[AnyExpr] = []


class QueryIR(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["query"] = "query"
    raw_text: str
    # SHA-256 over the canonical IR shape (post merge_consecutive_filters +
    # normalize_expressions, spans and bind-time annotations stripped) — two
    # queries with the same semantic content collide. Distinct from Tier 1's
    # :meth:`KustoQuery.get_structural_hash`, which hashes the AST node-kind
    # sequence only and is literal/identifier-blind. Computed once at build;
    # stale if you mutate the IR afterward — call
    # :func:`kustology.ir.compute_semantic_hash` to refresh.
    semantic_hash: str
    let_bindings: list[LetBinding]
    main_pipeline: Pipeline
    # The second and later tabular statements of a multi-statement query, in
    # source order. KQL separates statements with ``;`` and a query may hold
    # several tabular ones -- ``T | count; U | count`` -- of which the builder
    # used to keep only the first, so that query built exactly the IR of
    # ``T | count`` and carried its ``semantic_hash``. Everything past the
    # first semicolon was gone: unreachable through ``walk``/``find_all``,
    # invisible to the binder, and absent from the digest.
    #
    # ``main_pipeline`` stays the first statement rather than becoming
    # ``pipelines[0]``: the overwhelmingly common query has exactly one, and
    # a required field naming it keeps that case a direct read. Consumers
    # that want every statement iterate
    # ``[ir.main_pipeline, *ir.additional_pipelines]``.
    additional_pipelines: list[Pipeline] = []
    diagnostics: list[Diagnostic] = []
    schema_attached: bool = False

    def to_llm_dict(self) -> dict[str, Any]:
        """LLM-friendly serialization. See :mod:`kustology.ir.llm_view`."""
        from .llm_view import to_llm_dict
        return to_llm_dict(self)


# The expression classes first, and here rather than in ``expr.py``:
# ``ToScalarExpr.pipeline`` and ``SubqueryExpr.pipeline`` are forward
# references to ``Pipeline``, which does not exist until this module has run,
# and both classes are members of ``AnyExpr`` so *every* expression class with
# an ``AnyExpr`` field needs them resolved too. See
# ``expr.REBUILT_BY_QUERY_MODULE``.
#
# The rebuild resolves the reference from the **calling** module's namespace,
# which is this one -- ``expr.py``'s own globals do not hold ``Pipeline``,
# since its import there is ``TYPE_CHECKING``-only. That is the standard
# pydantic idiom for a cycle, and it is load-bearing rather than incidental:
# ``test_nested_pipelines`` asserts both classes come out complete with the
# resolved annotation, so a pydantic change that stopped reaching the caller's
# namespace fails loudly instead of silently reinstating an untyped field.
for _expr_cls in REBUILT_BY_QUERY_MODULE:
    _expr_cls.model_rebuild()

Pipeline.model_rebuild()
LetBinding.model_rebuild()
LetFunction.model_rebuild()
UnionOp.model_rebuild()
MvApplyOp.model_rebuild()
LookupOp.model_rebuild()
PartitionOp.model_rebuild()
FacetOp.model_rebuild()
ForkBranch.model_rebuild()
ForkOp.model_rebuild()
MacroExpandOp.model_rebuild()
