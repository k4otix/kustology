# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Semantic intermediate representation (IR) for KQL queries.

A pydantic model of the parsed query — typed operators and expressions, source
spans, and a schema binder. Activated by ``pip install 'pykusto-language[ir]'``;
importing without pydantic raises an ``ImportError`` with the install command.

Stability: pre-1.0. Minor breaking changes are possible at minor versions
until the IR survives one DLL upgrade cycle. See CHANGELOG.
"""

from ._guard import _require_pydantic

_require_pydantic()

# Order matters: types/spans → expr → query → builder/binder.
from .types import KustoType  # noqa: E402
from .spans import Span  # noqa: E402
from .expr import (  # noqa: E402, F401
    And, AnyExpr, Between, BinOp, BracketedExpr, CaseExpr, ColumnRef,
    CompoundNamedExpr, ElementExpr, Exists, Expr, ExternalDataExpr, FuncCall,
    LiteralExpr, MaterializeExpr, NamedExpr, Not, Or, PathExpr, RegexMatch,
    SetMembership, StarExpr, ToScalarExpr, UnaryOp, UnknownExpr,
)
from .query import (  # noqa: E402, F401
    AsOp, AssertSchemaOp, Assignment, ConsumeOp, CountOp, Diagnostic,
    DistinctOp, EvaluateOp, ExecuteAndCacheOp, ExtendOp, FacetOp, FilterOp,
    FindOp, ForkOp, FuncCallSource, GetSchemaOp, GraphMarkComponentsOp,
    GraphMatchOp, GraphShortestPathsOp, GraphToTableOp, GraphWhereEdgesOp,
    GraphWhereNodesOp, ImplicitSource,
    InvokeOp, JoinOp, LetBinding, LetRef, LookupOp, MacroExpandOp,
    MakeGraphOp, MakeSeriesOp, MvApplyOp, MvExpandOp, Operator, ParseKvOp,
    ParseOp, ParseWhereOp, PartitionOp, Pipeline, PrintOp, ProjectAwayOp,
    ProjectByNamesOp, ProjectKeepOp, ProjectOp, ProjectRenameOp,
    ProjectReorderOp, QueryIR, RangeOp, RenderOp, SampleDistinctOp, SampleOp,
    ScanOp, SearchOp, SerializeOp, SortOp, SummarizeOp, TableRef,
    TabularSchema, TakeOp, TopHittersOp, TopNestedOp, TopOp, UnionOp,
    UnknownSource,
)
from .builder import IRBuilder  # noqa: E402
from .binder import BinderEnricher, SchemaAttacher  # noqa: E402
from .llm_view import to_llm_dict  # noqa: E402

__all__ = [
    # Builder / binder / serialization views
    "IRBuilder", "SchemaAttacher", "BinderEnricher", "to_llm_dict",
    # Top-level / container
    "QueryIR", "Pipeline", "LetBinding", "Diagnostic", "Assignment", "Span",
    "KustoType", "TabularSchema",
    # Expressions
    "Expr", "AnyExpr", "ColumnRef", "BinOp", "SetMembership", "Between",
    "And", "Or", "Not", "Exists", "RegexMatch", "CaseExpr", "UnknownExpr",
    "LiteralExpr", "FuncCall", "PathExpr", "ElementExpr", "StarExpr",
    "NamedExpr", "UnaryOp", "BracketedExpr", "CompoundNamedExpr",
    "MaterializeExpr", "ToScalarExpr", "ExternalDataExpr",
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
    "GraphWhereNodesOp",
    # Sources
    "TableRef", "LetRef", "FuncCallSource", "ImplicitSource", "UnknownSource",
]
