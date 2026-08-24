# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Semantic intermediate representation (IR) for KQL queries.

A pydantic model of the parsed query — typed operators and expressions, source
spans, and a schema binder. Activated by ``pip install 'kustology[ir]'``;
importing without pydantic raises an ``ImportError`` with the install command.

Stability: pre-1.0. Minor breaking changes are possible at minor versions
until the IR survives one DLL upgrade cycle. See CHANGELOG.

``IR_SCHEMA_VERSION`` is the IR shape's own version, distinct from the
``kustology`` package version. Bump on any breaking field-shape change so
serialized IR JSON can carry a version tag (e.g. via wrapper envelope) and
consumers can refuse to load an incompatible payload.
"""

IR_SCHEMA_VERSION = "0.2"

from ._guard import _require_pydantic

_require_pydantic()

# Order matters: types/spans → expr → query → builder.
from .types import KustoType
from .spans import Span
from .expr import (
    And, AnyExpr, Between, BinOp, BracketedExpr, CaseExpr, ColumnRef,
    CompoundNamedExpr, ElementExpr, Exists, Expr, ExternalDataExpr, FuncCall,
    LetValueRef, LiteralExpr, NamedExpr, Not, Or, PathExpr, RegexMatch,
    SetMembership, StarExpr, SubqueryExpr, ToScalarExpr, TypedNameDecl,
    UnaryOp, UnknownExpr,
)
from .query import (
    AsOp, AssertSchemaOp, Assignment, ConsumeOp, CountOp, DataTableSource,
    Diagnostic, DistinctOp, EvaluateOp, ExecuteAndCacheOp, ExtendOp,
    ExternalDataSource, FacetOp, FilterOp,
    FindOp, ForkBranch, ForkOp, FuncCallSource, GetSchemaOp,
    GraphMarkComponentsOp, GraphMatchOp, GraphShortestPathsOp,
    GraphToTableOp, GraphWhereEdgesOp,
    GraphWhereNodesOp, ImplicitSource,
    InvokeOp, JoinOp, LetBinding, LetFunction, LetRef, LookupOp, MacroExpandOp,
    MakeGraphOp, MakeSeriesAggregate, MakeSeriesOp, MvApplyOp,
    MvExpandColumn, MvExpandOp,
    Operator, ParseKvOp,
    ParseOp, ParseWhereOp, PartitionOp, Pipeline, PrintOp, ProjectAwayOp,
    ProjectByNamesOp, ProjectKeepOp, ProjectOp, ProjectRenameOp,
    ProjectReorderOp, QueryIR, RangeOp, RenderOp, ReorderKey,
    SampleDistinctOp, SampleOp,
    ScanOp, SearchOp, SerializeOp, SortKey, SortOp, SummarizeOp, TableRef,
    TabularSchema, TakeOp, TopHittersOp, TopNestedOp, TopOp, UnionOp,
    UnknownOp, UnknownSource,
)
from .builder import IRBuilder
from .llm_view import to_llm_dict
from .transforms import (
    SEMANTIC_HASH_SCHEME,
    compute_semantic_hash, merge_consecutive_filters, normalize_expressions,
)
from .walk import find_all, walk
from .analyzers import AnalyzerFn, Finding, Severity

__all__ = [
    # Schema-version
    "IR_SCHEMA_VERSION", "SEMANTIC_HASH_SCHEME",
    # Builder / serialization views
    "IRBuilder", "to_llm_dict",
    # Traversal & transforms
    "walk", "find_all",
    "merge_consecutive_filters", "normalize_expressions", "compute_semantic_hash",
    # Analyzer protocol
    "Finding", "AnalyzerFn", "Severity",
    # Top-level / container
    "QueryIR", "Pipeline", "LetBinding", "LetFunction", "Diagnostic", "Assignment",
    "ForkBranch", "MakeSeriesAggregate", "MvExpandColumn", "ReorderKey",
    "SortKey", "Span",
    "KustoType", "TabularSchema",
    # Expressions
    "Expr", "AnyExpr", "ColumnRef", "BinOp", "SetMembership", "Between",
    "And", "Or", "Not", "Exists", "RegexMatch", "CaseExpr", "UnknownExpr",
    "LiteralExpr", "FuncCall", "PathExpr", "ElementExpr", "StarExpr",
    "NamedExpr", "UnaryOp", "BracketedExpr", "CompoundNamedExpr",
    "TypedNameDecl", "LetValueRef",
    "ToScalarExpr", "SubqueryExpr", "ExternalDataExpr",
    # Operators
    "Operator", "FilterOp", "ExtendOp", "SummarizeOp", "ProjectOp",
    "ProjectAwayOp", "ProjectKeepOp", "ProjectReorderOp", "ProjectRenameOp",
    "ProjectByNamesOp", "DistinctOp", "TakeOp", "SortOp", "TopOp",
    "TopHittersOp", "SampleOp", "SearchOp", "UnionOp", "MakeSeriesOp",
    "MvExpandOp", "MvApplyOp", "ParseOp", "ParseWhereOp", "EvaluateOp",
    "CountOp", "PrintOp", "AsOp", "RangeOp", "LookupOp", "PartitionOp",
    "RenderOp", "JoinOp",
    # Advanced operators (modeled stubs)
    "FacetOp", "GetSchemaOp", "InvokeOp", "FindOp", "ForkOp", "ScanOp",
    "SerializeOp", "ConsumeOp", "AssertSchemaOp", "ExecuteAndCacheOp",
    "ParseKvOp", "SampleDistinctOp", "TopNestedOp", "MakeGraphOp",
    "MacroExpandOp", "GraphMatchOp", "GraphMarkComponentsOp",
    "GraphShortestPathsOp", "GraphToTableOp", "GraphWhereEdgesOp",
    "GraphWhereNodesOp", "UnknownOp",
    # Sources
    "TableRef", "LetRef", "FuncCallSource", "DataTableSource",
    "ExternalDataSource", "ImplicitSource", "UnknownSource",
]
