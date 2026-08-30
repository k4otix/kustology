# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Semantic intermediate representation (IR) for KQL queries.

A pydantic model of the parsed query: typed operators and expressions, source
spans, and a schema binder. Install with ``pip install 'kustology[ir]'``;
importing without pydantic raises an ``ImportError`` naming that command.

Stability is pre-1.0. Minor versions can break the IR until it survives one DLL
upgrade cycle. See CHANGELOG.

``IR_SCHEMA_VERSION`` and ``SEMANTIC_HASH_SCHEME`` are re-exported here, the
public spelling. :mod:`kustology._ir_tags` defines them and records what each
one tags and when it moves.
"""

from .._ir_tags import IR_SCHEMA_VERSION

from ._guard import _require_pydantic

_require_pydantic()

# Order matters: types/spans → expr → query → builder.
from .types import KustoType
from .spans import Span
from .expr import (
    And, AnyExpr, Between, BinOp, BracketedExpr, CaseExpr, ColumnRef,
    CompoundNamedExpr, DataTableExpr, ElementExpr, Exists, Expr,
    ExternalDataExpr, FuncCall,
    LetValueRef, LiteralExpr, NamedExpr, Not, Or, PathExpr, RegexMatch,
    SetMembership, StarExpr, SubqueryExpr, ToScalarExpr, TypedNameDecl,
    UnaryOp, UnknownExpr,
)
from .query import (
    AliasStmt,
    AnyStatement,
    AsOp, AssertSchemaOp, Assignment, ConsumeOp, CountOp, DataTableSource,
    Diagnostic, DistinctOp, EvaluateOp, ExecuteAndCacheOp, ExtendOp,
    ExternalDataSource, FacetOp, FilterOp,
    FindOp, ForkBranch, ForkOp, FuncCallSource, GetSchemaOp,
    GraphMarkComponentsOp, GraphMatchOp, GraphShortestPathsOp,
    GraphToTableOp, GraphWhereEdgesOp,
    GraphWhereNodesOp, ImplicitSource,
    InvokeOp, JoinOp, LetBinding, LetFunction, LetFunctionParameter, LetRef,
    LookupOp, MacroExpandOp,
    MakeGraphOp, MakeSeriesAggregate, MakeSeriesOp, MvApplyOp,
    MvExpandColumn, MvExpandOp,
    Operator, ParseKvOp,
    ParseOp, ParseWhereOp, PartitionOp, PatternMatch, PatternStmt, Pipeline,
    PrintOp, ProjectAwayOp,
    ProjectByNamesOp, ProjectKeepOp, ProjectOp, ProjectRenameOp,
    ProjectReorderOp, QueryIR, QueryParametersStmt, RangeOp, RenderOp,
    ReorderKey, RestrictStmt,
    SampleDistinctOp, SampleOp,
    ScanOp, SearchOp, SerializeOp, SetOptionStmt, SortKey, SortOp, SummarizeOp,
    TableRef,
    TabularSchema, TakeOp, TopHittersOp, TopNestedOp, TopOp, UnionOp,
    UnknownOp, UnknownSource, UnknownStmt,
)
from .builder import IRBuilder
from .llm_view import to_llm_dict
from .transforms import (
    SEMANTIC_HASH_SCHEME,
    compute_semantic_hash, merge_consecutive_filters, normalize_expressions,
)
from .walk import find_all, span_of, walk
from .similarity import (
    SubtreeHash,
    containment,
    differing_subtrees,
    similarity,
    similarity_sketch,
    sketch_similarity,
    subtree_hashes,
)
from .analyzers import AnalyzerFn, Finding, Severity

__all__ = [
    # Schema-version
    "IR_SCHEMA_VERSION", "SEMANTIC_HASH_SCHEME",
    # Builder / serialization views
    "IRBuilder", "to_llm_dict",
    # Traversal & transforms
    "walk", "find_all", "span_of",
    "merge_consecutive_filters", "normalize_expressions", "compute_semantic_hash",
    # Similarity
    "SubtreeHash", "subtree_hashes", "similarity", "containment",
    "similarity_sketch", "sketch_similarity", "differing_subtrees",
    # Analyzer protocol
    "Finding", "AnalyzerFn", "Severity",
    # Top-level / container
    "QueryIR", "Pipeline", "LetBinding", "LetFunction", "LetFunctionParameter",
    "Diagnostic", "Assignment",
    "ForkBranch", "MakeSeriesAggregate", "MvExpandColumn", "ReorderKey",
    "SortKey", "Span",
    "KustoType", "TabularSchema",
    # Expressions
    "Expr", "AnyExpr", "ColumnRef", "BinOp", "SetMembership", "Between",
    "And", "Or", "Not", "Exists", "RegexMatch", "CaseExpr", "UnknownExpr",
    "LiteralExpr", "FuncCall", "PathExpr", "ElementExpr", "StarExpr",
    "NamedExpr", "UnaryOp", "BracketedExpr", "CompoundNamedExpr",
    "TypedNameDecl", "LetValueRef",
    "ToScalarExpr", "SubqueryExpr", "ExternalDataExpr", "DataTableExpr",
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
    # Statements (neither `let` nor tabular)
    "AnyStatement", "SetOptionStmt", "QueryParametersStmt", "PatternStmt",
    "PatternMatch", "AliasStmt", "RestrictStmt", "UnknownStmt",
]
