# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, Field

# Pydantic v2 resolves string forward refs in `AnyExpr` using the namespace of
# the consuming module, so every name in AnyExpr must be importable here.
from .expr import (  # noqa: F401 — names referenced via forward refs
    And, AnyExpr, Between, BinOp, BracketedExpr, CaseExpr, ColumnRef,
    CompoundNamedExpr, ElementExpr, Exists, Expr, ExternalDataExpr, FuncCall,
    LiteralExpr, MaterializeExpr, NamedExpr, Not, Or, PathExpr, RegexMatch,
    SetMembership, StarExpr, ToScalarExpr, UnaryOp, UnknownExpr,
)
from .spans import Span


# KIND is the LLM-facing discriminator surfaced by ``ir.llm_view.to_llm_dict``.
# Keeping it separate from the Python class name lets the wire format use
# snake_case KQL-aligned labels (``filter``, ``column_ref``) regardless of
# the CamelCase Python naming conventions.

class Diagnostic(BaseModel):
    KIND: ClassVar[str] = "diagnostic"
    message: str
    severity: str
    span: Optional[Span] = None
    code: Optional[str] = None
    category: Optional[str] = None


class TabularSchema(BaseModel):
    """Tabular result type: ``{column_name: kusto_type_string}``. Populated by
    ``SchemaAttacher`` after walking a pipeline."""
    KIND: ClassVar[str] = "tabular_schema"
    columns: dict[str, str] = {}


class Assignment(BaseModel):
    KIND: ClassVar[str] = "assignment"
    name: str
    expr: AnyExpr
    span: Span


class Operator(BaseModel):
    # `extra="forbid"` is required for round-trip safety: without it, Union
    # resolution silently absorbs unknown fields, so a FilterOp JSON could
    # validate as a fields-less GetSchemaOp (predicate dropped).
    model_config = {"extra": "forbid"}

    KIND: ClassVar[str] = "operator"
    span: Span


class FilterOp(Operator):
    KIND: ClassVar[str] = "filter"
    predicate: AnyExpr


class ExtendOp(Operator):
    KIND: ClassVar[str] = "extend"
    assignments: list[Assignment]


class SummarizeOp(Operator):
    KIND: ClassVar[str] = "summarize"
    aggregations: list[Assignment]
    by: list[Union[ColumnRef, AnyExpr, Assignment]]


class ProjectOp(Operator):
    KIND: ClassVar[str] = "project"
    columns: list[Union[ColumnRef, Assignment, AnyExpr]]


class TableRef(BaseModel):
    KIND: ClassVar[str] = "table_ref"
    name: str
    span: Span


class LetRef(BaseModel):
    KIND: ClassVar[str] = "let_ref"
    name: str
    span: Span


class UnknownSource(BaseModel):
    KIND: ClassVar[str] = "unknown_source"
    raw_text: str
    span: Span


class ImplicitSource(BaseModel):
    """Source whose rows come from a parent context (union-at-root subqueries,
    ``mv-apply``/``partition``/``fork`` inner pipelines, parenthesized
    ``join``/``lookup`` RHS). Distinct from :class:`UnknownSource`, which means
    the source couldn't be determined.
    """
    KIND: ClassVar[str] = "implicit_source"
    span: Span


class FuncCallSource(BaseModel):
    """Function-call-as-pipeline-source — a user-defined function that returns
    a table, e.g. ``findAnomalies('foo') | summarize ...``."""
    KIND: ClassVar[str] = "func_call_source"
    name: str
    args: list[AnyExpr] = []
    span: Span


class DistinctOp(Operator):
    KIND: ClassVar[str] = "distinct"
    columns: list[Union[ColumnRef, Assignment, AnyExpr]]


class TakeOp(Operator):
    KIND: ClassVar[str] = "take"
    count: int


class SortOp(Operator):
    KIND: ClassVar[str] = "sort"
    expressions: list[AnyExpr]


class TopOp(Operator):
    KIND: ClassVar[str] = "top"
    count: int
    by: AnyExpr


class TopHittersOp(Operator):
    KIND: ClassVar[str] = "top_hitters"
    count: int
    by: AnyExpr


class SampleOp(Operator):
    KIND: ClassVar[str] = "sample"
    count: int


class SearchOp(Operator):
    KIND: ClassVar[str] = "search"
    predicate: Optional[AnyExpr] = None


class UnionOp(Operator):
    KIND: ClassVar[str] = "union"
    pipelines: list["Pipeline"]


class MakeSeriesOp(Operator):
    KIND: ClassVar[str] = "make_series"
    aggregations: list[Assignment]
    by: list[Assignment]
    range_from: Optional[AnyExpr] = None
    range_to: Optional[AnyExpr] = None
    step: Optional[AnyExpr] = None


class MvExpandOp(Operator):
    KIND: ClassVar[str] = "mv_expand"
    columns: list[AnyExpr]


class RenderOp(Operator):
    KIND: ClassVar[str] = "render"
    # Field collides with the LLM discriminator key; ``llm_view`` renames it
    # to ``render_kind`` on the way out. Canonical JSON keeps ``kind``.
    kind: str


class ProjectAwayOp(Operator):
    KIND: ClassVar[str] = "project_away"
    columns: list[Union[ColumnRef, AnyExpr]]


class ProjectKeepOp(Operator):
    KIND: ClassVar[str] = "project_keep"
    columns: list[Union[ColumnRef, AnyExpr]]


class ProjectReorderOp(Operator):
    KIND: ClassVar[str] = "project_reorder"
    columns: list[Union[ColumnRef, AnyExpr]]


class ProjectRenameOp(Operator):
    KIND: ClassVar[str] = "project_rename"
    columns: list[Assignment]


class ProjectByNamesOp(Operator):
    KIND: ClassVar[str] = "project_by_names"
    names: list[AnyExpr]


class MvApplyOp(Operator):
    KIND: ClassVar[str] = "mv_apply"
    assignments: list[Assignment]
    right: "Pipeline"


class ParseOp(Operator):
    KIND: ClassVar[str] = "parse"
    target: AnyExpr
    patterns: list[AnyExpr]


class ParseWhereOp(Operator):
    KIND: ClassVar[str] = "parse_where"
    target: AnyExpr
    patterns: list[AnyExpr]


class EvaluateOp(Operator):
    KIND: ClassVar[str] = "evaluate"
    func: FuncCall


class CountOp(Operator):
    KIND: ClassVar[str] = "count"
    as_name: Optional[str] = None


class PrintOp(Operator):
    KIND: ClassVar[str] = "print"
    columns: list[Union[Assignment, AnyExpr]]


class AsOp(Operator):
    KIND: ClassVar[str] = "as"
    name: str


class RangeOp(Operator):
    KIND: ClassVar[str] = "range"
    column: str
    start: AnyExpr
    end: AnyExpr
    step: AnyExpr


class LookupOp(Operator):
    KIND: ClassVar[str] = "lookup"
    # Field collides with the LLM discriminator key; ``llm_view`` renames it
    # to ``lookup_kind`` on the way out.
    kind: Optional[str] = None
    right: "Pipeline"
    on_columns: list[str]
    on_expressions: list[AnyExpr] = []


class PartitionOp(Operator):
    KIND: ClassVar[str] = "partition"
    by: AnyExpr
    right: "Pipeline"


class FacetOp(Operator):
    KIND: ClassVar[str] = "facet"
    columns: list[AnyExpr] = []
    with_pipeline: Optional["Pipeline"] = None


class GetSchemaOp(Operator):
    KIND: ClassVar[str] = "getschema"


class InvokeOp(Operator):
    KIND: ClassVar[str] = "invoke"
    func: FuncCall


class FindOp(Operator):
    KIND: ClassVar[str] = "find"
    predicate: Optional[AnyExpr] = None
    tables: list[str] = []


class ForkOp(Operator):
    KIND: ClassVar[str] = "fork"
    pipelines: list["Pipeline"] = []


class ScanOp(Operator):
    KIND: ClassVar[str] = "scan"
    raw_text: str


class SerializeOp(Operator):
    KIND: ClassVar[str] = "serialize"
    assignments: list[Assignment] = []


class ConsumeOp(Operator):
    KIND: ClassVar[str] = "consume"


class AssertSchemaOp(Operator):
    KIND: ClassVar[str] = "assert_schema"
    columns: dict[str, str] = {}


class ExecuteAndCacheOp(Operator):
    KIND: ClassVar[str] = "execute_and_cache"


class ParseKvOp(Operator):
    KIND: ClassVar[str] = "parse_kv"
    target: AnyExpr
    columns: list[Assignment] = []


class SampleDistinctOp(Operator):
    KIND: ClassVar[str] = "sample_distinct"
    count: int
    of: AnyExpr


class TopNestedOp(Operator):
    KIND: ClassVar[str] = "top_nested"
    raw_text: str


class MakeGraphOp(Operator):
    KIND: ClassVar[str] = "make_graph"
    raw_text: str


class MacroExpandOp(Operator):
    KIND: ClassVar[str] = "macro_expand"
    raw_text: str
    pipeline: Optional["Pipeline"] = None


class GraphMatchOp(Operator):
    KIND: ClassVar[str] = "graph_match"
    raw_text: str


class GraphMarkComponentsOp(Operator):
    KIND: ClassVar[str] = "graph_mark_components"
    raw_text: str


class GraphShortestPathsOp(Operator):
    KIND: ClassVar[str] = "graph_shortest_paths"
    raw_text: str


class GraphToTableOp(Operator):
    KIND: ClassVar[str] = "graph_to_table"
    raw_text: str


class GraphWhereEdgesOp(Operator):
    KIND: ClassVar[str] = "graph_where_edges"
    predicate: AnyExpr


class GraphWhereNodesOp(Operator):
    KIND: ClassVar[str] = "graph_where_nodes"
    predicate: AnyExpr


class Pipeline(BaseModel):
    KIND: ClassVar[str] = "pipeline"
    source: Union[TableRef, LetRef, FuncCallSource, ImplicitSource, UnknownSource, "Pipeline"]
    # Left-to-right Union mode with fields-less ops listed first. Pydantic's
    # default "smart" mode would otherwise prefer a defaulted-fields class
    # (e.g. FindOp with predicate=None) over a true fields-less class
    # (e.g. GetSchemaOp) for JSON containing only a span — breaking round-trip.
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
        Operator,
    ], Field(union_mode="left_to_right")]]
    # Final scope after walking ops. Populated by SchemaAttacher.enrich().
    result_schema: Optional[TabularSchema] = None


class JoinOp(Operator):
    KIND: ClassVar[str] = "join"
    # Field collides with the LLM discriminator key; ``llm_view`` renames it
    # to ``join_kind`` on the way out.
    kind: Optional[str] = None
    right: Pipeline
    on_columns: list[str]
    on_expressions: list[AnyExpr] = []


class LetBinding(BaseModel):
    KIND: ClassVar[str] = "let_binding"
    name: str
    span: Span
    category: Literal[
        "time_scalar", "literal_constant", "dynamic_constant",
        "scalar_subquery", "baseline", "subquery", "alias",
    ]
    rhs_expr: Optional[AnyExpr] = None
    rhs_pipeline: Optional[Pipeline] = None
    inner_tables: list[str] = []
    inner_time_exprs: list[AnyExpr] = []


class QueryIR(BaseModel):
    KIND: ClassVar[str] = "query"
    raw_text: str
    structural_hash: str
    let_bindings: list[LetBinding]
    main_pipeline: Pipeline
    diagnostics: list[Diagnostic] = []
    schema_attached: bool = False
    parse_warnings: list[str] = []

    def to_llm_dict(self) -> dict[str, Any]:
        """LLM-friendly serialization. See :mod:`pykusto_language.ir.llm_view`."""
        from .llm_view import to_llm_dict
        return to_llm_dict(self)


Pipeline.model_rebuild()
UnionOp.model_rebuild()
MvApplyOp.model_rebuild()
LookupOp.model_rebuild()
PartitionOp.model_rebuild()
FacetOp.model_rebuild()
ForkOp.model_rebuild()
MacroExpandOp.model_rebuild()
