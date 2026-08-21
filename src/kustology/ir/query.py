# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, Field

# Pydantic v2 resolves string forward refs in `AnyExpr` using the namespace of
# the consuming module, so every name in AnyExpr must be importable here.
from .expr import (  # noqa: F401 — names referenced via forward refs
    And,
    AnyExpr,
    Between,
    BinOp,
    BracketedExpr,
    CaseExpr,
    ColumnRef,
    CompoundNamedExpr,
    ElementExpr,
    Exists,
    Expr,
    ExternalDataExpr,
    FuncCall,
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
    UnaryOp,
    UnknownExpr,
)
from .spans import Span

# KIND is the LLM-facing discriminator surfaced by ``ir.llm_view.to_llm_dict``.
# Keeping it separate from the Python class name lets the wire format use
# snake_case KQL-aligned labels (``filter``, ``column_ref``) regardless of
# the CamelCase Python naming conventions.

class Diagnostic(BaseModel):
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "diagnostic"
    kind: Literal["diagnostic"] = "diagnostic"
    message: str
    severity: str
    span: Span | None = None
    code: str | None = None
    category: str | None = None


class TabularSchema(BaseModel):
    """Tabular result type: ``{column_name: kusto_type_string}``. Populated by
    ``SchemaAttacher`` after walking a pipeline."""
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "tabular_schema"
    kind: Literal["tabular_schema"] = "tabular_schema"
    columns: dict[str, str] = {}


class Assignment(BaseModel):
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "assignment"
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

    KIND: ClassVar[str] = "operator"
    kind: Literal["operator"] = "operator"
    span: Span


class FilterOp(Operator):
    KIND: ClassVar[str] = "filter"
    kind: Literal["filter"] = "filter"
    predicate: AnyExpr


class ExtendOp(Operator):
    KIND: ClassVar[str] = "extend"
    kind: Literal["extend"] = "extend"
    assignments: list[Assignment]


class SummarizeOp(Operator):
    KIND: ClassVar[str] = "summarize"
    kind: Literal["summarize"] = "summarize"
    aggregations: list[Assignment]
    by: list[ColumnRef | AnyExpr | Assignment]


class ProjectOp(Operator):
    KIND: ClassVar[str] = "project"
    kind: Literal["project"] = "project"
    columns: list[ColumnRef | Assignment | AnyExpr]


class TableRef(BaseModel):
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "table_ref"
    kind: Literal["table_ref"] = "table_ref"
    name: str
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
    KIND: ClassVar[str] = "let_ref"
    kind: Literal["let_ref"] = "let_ref"
    name: str
    span: Span


class UnknownSource(BaseModel):
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "unknown_source"
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
    KIND: ClassVar[str] = "implicit_source"
    kind: Literal["implicit_source"] = "implicit_source"
    span: Span


class FuncCallSource(BaseModel):
    """Function-call-as-pipeline-source — a user-defined function that returns
    a table, e.g. ``findAnomalies('foo') | summarize ...``."""
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "func_call_source"
    kind: Literal["func_call_source"] = "func_call_source"
    name: str
    args: list[AnyExpr] = []
    span: Span


class DistinctOp(Operator):
    KIND: ClassVar[str] = "distinct"
    kind: Literal["distinct"] = "distinct"
    columns: list[ColumnRef | Assignment | AnyExpr]


class TakeOp(Operator):
    KIND: ClassVar[str] = "take"
    kind: Literal["take"] = "take"
    # KQL allows any scalar expression here (`let n = 10; T | take n`,
    # `take toscalar(U | count)`), not just an integer literal, so the field
    # widens to ``AnyExpr``. ``int`` is listed first so Pydantic validates a
    # JSON literal ``5`` as a plain ``int`` -- matching existing ``op.count
    # == 5`` assertions and downstream consumers -- instead of coercing it
    # into an expression model; the four sibling ops below follow suit.
    count: int | AnyExpr


class SortOp(Operator):
    KIND: ClassVar[str] = "sort"
    kind: Literal["sort"] = "sort"
    expressions: list[AnyExpr]


class TopOp(Operator):
    KIND: ClassVar[str] = "top"
    kind: Literal["top"] = "top"
    count: int | AnyExpr
    by: AnyExpr


class TopHittersOp(Operator):
    KIND: ClassVar[str] = "top_hitters"
    kind: Literal["top_hitters"] = "top_hitters"
    count: int | AnyExpr
    by: AnyExpr


class SampleOp(Operator):
    KIND: ClassVar[str] = "sample"
    kind: Literal["sample"] = "sample"
    count: int | AnyExpr


class SearchOp(Operator):
    KIND: ClassVar[str] = "search"
    kind: Literal["search"] = "search"
    predicate: AnyExpr | None = None


class UnionOp(Operator):
    KIND: ClassVar[str] = "union"
    kind: Literal["union"] = "union"
    pipelines: list["Pipeline"]


class MakeSeriesOp(Operator):
    KIND: ClassVar[str] = "make_series"
    kind: Literal["make_series"] = "make_series"
    aggregations: list[Assignment]
    by: list[Assignment]
    on_column: AnyExpr | None = None
    range_from: AnyExpr | None = None
    range_to: AnyExpr | None = None
    step: AnyExpr | None = None


class MvExpandOp(Operator):
    KIND: ClassVar[str] = "mv_expand"
    kind: Literal["mv_expand"] = "mv_expand"
    columns: list[AnyExpr]


class RenderOp(Operator):
    KIND: ClassVar[str] = "render"
    kind: Literal["render"] = "render"
    render_kind: str


class ProjectAwayOp(Operator):
    KIND: ClassVar[str] = "project_away"
    kind: Literal["project_away"] = "project_away"
    columns: list[ColumnRef | AnyExpr]


class ProjectKeepOp(Operator):
    KIND: ClassVar[str] = "project_keep"
    kind: Literal["project_keep"] = "project_keep"
    columns: list[ColumnRef | AnyExpr]


class ProjectReorderOp(Operator):
    KIND: ClassVar[str] = "project_reorder"
    kind: Literal["project_reorder"] = "project_reorder"
    columns: list[ColumnRef | AnyExpr]


class ProjectRenameOp(Operator):
    KIND: ClassVar[str] = "project_rename"
    kind: Literal["project_rename"] = "project_rename"
    columns: list[Assignment]


class ProjectByNamesOp(Operator):
    KIND: ClassVar[str] = "project_by_names"
    kind: Literal["project_by_names"] = "project_by_names"
    names: list[AnyExpr]


class MvApplyOp(Operator):
    KIND: ClassVar[str] = "mv_apply"
    kind: Literal["mv_apply"] = "mv_apply"
    assignments: list[Assignment]
    right: "Pipeline"


class ParseOp(Operator):
    KIND: ClassVar[str] = "parse"
    kind: Literal["parse"] = "parse"
    target: AnyExpr
    patterns: list[AnyExpr]


class ParseWhereOp(Operator):
    KIND: ClassVar[str] = "parse_where"
    kind: Literal["parse_where"] = "parse_where"
    target: AnyExpr
    patterns: list[AnyExpr]


class EvaluateOp(Operator):
    KIND: ClassVar[str] = "evaluate"
    kind: Literal["evaluate"] = "evaluate"
    func: FuncCall


class CountOp(Operator):
    KIND: ClassVar[str] = "count"
    kind: Literal["count"] = "count"
    as_name: str | None = None


class PrintOp(Operator):
    KIND: ClassVar[str] = "print"
    kind: Literal["print"] = "print"
    columns: list[Assignment | AnyExpr]


class AsOp(Operator):
    KIND: ClassVar[str] = "as"
    kind: Literal["as"] = "as"
    name: str


class RangeOp(Operator):
    KIND: ClassVar[str] = "range"
    kind: Literal["range"] = "range"
    column: str
    start: AnyExpr
    end: AnyExpr
    step: AnyExpr


class LookupOp(Operator):
    KIND: ClassVar[str] = "lookup"
    kind: Literal["lookup"] = "lookup"
    lookup_kind: str | None = None
    right: "Pipeline"
    # KQL ``on Foo`` is sugar for ``on $left.Foo == $right.Foo``; both surface
    # here as ``AnyExpr`` (a bare ``ColumnRef`` or a full equality ``BinOp``).
    on: list[AnyExpr]


class PartitionOp(Operator):
    KIND: ClassVar[str] = "partition"
    kind: Literal["partition"] = "partition"
    by: AnyExpr
    right: "Pipeline"


class FacetOp(Operator):
    KIND: ClassVar[str] = "facet"
    kind: Literal["facet"] = "facet"
    columns: list[AnyExpr] = []
    with_pipeline: Optional["Pipeline"] = None


class GetSchemaOp(Operator):
    KIND: ClassVar[str] = "getschema"
    kind: Literal["getschema"] = "getschema"


class InvokeOp(Operator):
    KIND: ClassVar[str] = "invoke"
    kind: Literal["invoke"] = "invoke"
    func: FuncCall


class FindOp(Operator):
    KIND: ClassVar[str] = "find"
    kind: Literal["find"] = "find"
    predicate: AnyExpr | None = None
    tables: list[str] = []


class ForkOp(Operator):
    KIND: ClassVar[str] = "fork"
    kind: Literal["fork"] = "fork"
    pipelines: list["Pipeline"] = []


class ScanOp(Operator):
    KIND: ClassVar[str] = "scan"
    kind: Literal["scan"] = "scan"
    raw_text: str


class SerializeOp(Operator):
    KIND: ClassVar[str] = "serialize"
    kind: Literal["serialize"] = "serialize"
    assignments: list[Assignment] = []


class ConsumeOp(Operator):
    KIND: ClassVar[str] = "consume"
    kind: Literal["consume"] = "consume"


class AssertSchemaOp(Operator):
    KIND: ClassVar[str] = "assert_schema"
    kind: Literal["assert_schema"] = "assert_schema"
    columns: dict[str, str] = {}


class ExecuteAndCacheOp(Operator):
    KIND: ClassVar[str] = "execute_and_cache"
    kind: Literal["execute_and_cache"] = "execute_and_cache"


class ParseKvOp(Operator):
    KIND: ClassVar[str] = "parse_kv"
    kind: Literal["parse_kv"] = "parse_kv"
    target: AnyExpr
    # ``as (b:string, c:long)`` -- a name:type schema, modeled the same way
    # as :class:`AssertSchemaOp`. It was ``list[Assignment]``, which had no
    # expression to hold: a declared key has a type, not a value.
    columns: dict[str, str] = {}


class SampleDistinctOp(Operator):
    KIND: ClassVar[str] = "sample_distinct"
    kind: Literal["sample_distinct"] = "sample_distinct"
    count: int | AnyExpr
    of: AnyExpr


class TopNestedOp(Operator):
    KIND: ClassVar[str] = "top_nested"
    kind: Literal["top_nested"] = "top_nested"
    raw_text: str


class MakeGraphOp(Operator):
    KIND: ClassVar[str] = "make_graph"
    kind: Literal["make_graph"] = "make_graph"
    raw_text: str


class MacroExpandOp(Operator):
    KIND: ClassVar[str] = "macro_expand"
    kind: Literal["macro_expand"] = "macro_expand"
    raw_text: str
    pipeline: Optional["Pipeline"] = None


class GraphMatchOp(Operator):
    KIND: ClassVar[str] = "graph_match"
    kind: Literal["graph_match"] = "graph_match"
    raw_text: str


class GraphMarkComponentsOp(Operator):
    KIND: ClassVar[str] = "graph_mark_components"
    kind: Literal["graph_mark_components"] = "graph_mark_components"
    raw_text: str


class GraphShortestPathsOp(Operator):
    KIND: ClassVar[str] = "graph_shortest_paths"
    kind: Literal["graph_shortest_paths"] = "graph_shortest_paths"
    raw_text: str


class GraphToTableOp(Operator):
    KIND: ClassVar[str] = "graph_to_table"
    kind: Literal["graph_to_table"] = "graph_to_table"
    raw_text: str


class GraphWhereEdgesOp(Operator):
    KIND: ClassVar[str] = "graph_where_edges"
    kind: Literal["graph_where_edges"] = "graph_where_edges"
    predicate: AnyExpr


class GraphWhereNodesOp(Operator):
    KIND: ClassVar[str] = "graph_where_nodes"
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
    KIND: ClassVar[str] = "unknown_op"
    kind: Literal["unknown_op"] = "unknown_op"
    raw_text: str
    ast_kind: str
    reason: str


class Pipeline(BaseModel):
    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "pipeline"
    kind: Literal["pipeline"] = "pipeline"
    source: Union[TableRef, LetRef, FuncCallSource, ImplicitSource, UnknownSource, "Pipeline"]
    # Left-to-right Union mode is load-bearing. ORDERING RULE: fields-less
    # operator subclasses (only ``span`` + ``kind``) MUST appear before any
    # subclass that adds optional or defaulted fields. Pydantic's default
    # "smart" union mode would otherwise prefer a defaulted-fields class
    # (e.g. ``FindOp`` with ``predicate=None``) when given JSON containing
    # only a span+kind, breaking round-trip for the true fields-less class
    # (e.g. ``GetSchemaOp``). New ops: add to the right of fields-less ops
    # but to the left of ``UnknownOp``.
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
    ], Field(union_mode="left_to_right")]]
    # Final scope after walking ops. Populated by SchemaAttacher.enrich().
    result_schema: TabularSchema | None = None


class JoinOp(Operator):
    KIND: ClassVar[str] = "join"
    kind: Literal["join"] = "join"
    join_kind: str | None = None
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
    """

    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "let_function"
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
    KIND: ClassVar[str] = "let_binding"
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
    KIND: ClassVar[str] = "query"
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
    diagnostics: list[Diagnostic] = []
    schema_attached: bool = False

    def to_llm_dict(self) -> dict[str, Any]:
        """LLM-friendly serialization. See :mod:`kustology.ir.llm_view`."""
        from .llm_view import to_llm_dict
        return to_llm_dict(self)


Pipeline.model_rebuild()
LetBinding.model_rebuild()
LetFunction.model_rebuild()
UnionOp.model_rebuild()
MvApplyOp.model_rebuild()
LookupOp.model_rebuild()
PartitionOp.model_rebuild()
FacetOp.model_rebuild()
ForkOp.model_rebuild()
MacroExpandOp.model_rebuild()
