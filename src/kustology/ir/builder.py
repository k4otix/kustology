# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Build a :class:`QueryIR` from Microsoft's parsed KustoCode.

Two entry points: :meth:`IRBuilder.build` (string in, IR out, parses+binds)
and :meth:`IRBuilder.build_from_code` (use when the caller already has a
``KustoCode``, e.g. via :meth:`kustology.KustoQuery.to_ir`).

The handled-SyntaxKind sets are exposed as :attr:`_HANDLED_OPERATOR_KINDS`
and :attr:`_HANDLED_EXPR_KINDS` for the coverage audit script.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional, Union

from ..bridge import GlobalState, KustoCode  # noqa: F401 — re-export-friendly + CLR init
from .expr import (
    And, AnyExpr, Between, BinOp, BracketedExpr, CaseExpr, ColumnRef,
    CompoundNamedExpr, ElementExpr, Exists, ExternalDataExpr, FuncCall,
    LiteralExpr, MaterializeExpr, NamedExpr, Not, Or, PathExpr, RegexMatch,
    SetMembership, StarExpr, ToScalarExpr, UnaryOp, UnknownExpr,
)
from .query import (
    AsOp, AssertSchemaOp, Assignment, ConsumeOp, CountOp, Diagnostic,
    DistinctOp, EvaluateOp, ExecuteAndCacheOp, ExtendOp, FacetOp, FilterOp,
    FindOp, ForkOp, FuncCallSource, GetSchemaOp, GraphMarkComponentsOp,
    GraphMatchOp, GraphShortestPathsOp, GraphToTableOp, GraphWhereEdgesOp,
    GraphWhereNodesOp, ImplicitSource, InvokeOp, JoinOp, LetBinding, LetRef,
    LookupOp, MacroExpandOp, MakeGraphOp, MakeSeriesOp, MvApplyOp, MvExpandOp,
    Operator, ParseKvOp, ParseOp, ParseWhereOp, PartitionOp, Pipeline, PrintOp,
    ProjectAwayOp, ProjectByNamesOp, ProjectKeepOp, ProjectOp, ProjectRenameOp,
    ProjectReorderOp, QueryIR, RangeOp, RenderOp, SampleDistinctOp, SampleOp,
    ScanOp, SearchOp, SerializeOp, SortOp, SummarizeOp, TableRef, TakeOp,
    TopHittersOp, TopNestedOp, TopOp, UnionOp, UnknownSource,
)
from ._builder_helpers import (
    extract_named_param,
    extract_qualified_table_name,
    is_table_symbol,
    map_semantic_info,
    merge_filter_ops,
    safe_int,
    to_span,
    visit_name,
)
from ._normalize import canonical, normalize_in_place
from .spans import Span

logger = logging.getLogger(__name__)

# Bridge import above already triggered AddReference("Kusto.Language").
from Kusto.Language.Syntax import (  # noqa: E402
    ExpressionStatement,
    LetStatement,
)


def _is_time_func_name(name: str) -> bool:
    """True when ``name`` is a known KQL time function.

    Reflects ``Kusto.Language.Functions`` for the answer; falls back to a
    substring check (``time``/``ago``/``now``) if reflection is unavailable.
    """
    try:
        from ..reflection import time_functions
        return name in time_functions()
    except Exception:  # pragma: no cover — defensive
        lower = name.lower()
        return "time" in lower or "ago" in lower or "now" in lower


def _iter_elements(expr_list):
    """Yield the ``.Element`` of each entry in a .NET ``SeparatedSyntaxList``."""
    for i in range(expr_list.Count):
        yield expr_list[i].Element


class IRBuilder:
    """Builds a :class:`QueryIR` from a Microsoft Kusto syntax tree.

    Dispatch tables are explicit (sets, not method-ref dicts) so the audit
    script can read them statically without instantiating the builder.
    """

    _HANDLED_OPERATOR_KINDS = frozenset({
        "FilterOperator", "ExtendOperator", "SummarizeOperator", "JoinOperator",
        "LookupOperator", "PartitionByOperator", "PartitionOperator", "ProjectOperator",
        "ProjectAwayOperator", "ProjectKeepOperator", "ProjectReorderOperator",
        "ProjectRenameOperator", "ProjectByNamesOperator", "DistinctOperator",
        "TakeOperator", "SampleOperator", "SortOperator", "TopOperator",
        "TopHittersOperator", "SearchOperator", "UnionOperator",
        "MakeSeriesOperator", "MvExpandOperator", "MvApplyOperator",
        "ParseOperator", "ParseWhereOperator", "AsOperator", "RangeOperator",
        "RenderOperator", "EvaluateOperator",
        "CountOperator", "PrintOperator",
        "FacetOperator", "GetSchemaOperator", "InvokeOperator", "FindOperator",
        "ForkOperator", "ScanOperator", "SerializeOperator", "ConsumeOperator",
        "AssertSchemaOperator", "ExecuteAndCacheOperator", "ParseKvOperator",
        "SampleDistinctOperator", "TopNestedOperator", "MakeGraphOperator",
        "MacroExpandOperator", "GraphMatchOperator",
        "GraphMarkComponentsOperator", "GraphShortestPathsOperator",
        "GraphToTableOperator", "GraphWhereEdgesOperator",
        "GraphWhereNodesOperator",
    })

    _HANDLED_EXPR_KINDS = frozenset({
        "ParenthesizedExpression", "NameReference", "NameDeclaration",
        "NameAndTypeDeclaration", "PathExpression", "ElementExpression",
        "SimpleNamedExpression", "CompoundNamedExpression", "BracketedExpression",
        "PrefixUnaryExpression", "StarExpression", "LiteralExpression",
        "DynamicExpression",
        "AndExpression", "OrExpression", "OrderedExpression", "BinaryExpression",
        "InExpression", "HasAnyExpression", "HasAllExpression",
        "BetweenExpression", "FunctionCallExpression", "MaterializeExpression",
        "ToScalarExpression", "ExternalDataExpression", "MakeSeriesExpression",
    })

    def __init__(self, global_state: Optional["GlobalState"] = None):
        self.global_state = global_state or GlobalState.Default

    # -- entry points ----------------------------------------------------

    def build(self, query: str) -> QueryIR:
        """Parse, bind, build. Use ``build_from_code`` when the caller already
        has a ``KustoCode``."""
        code = KustoCode.ParseAndAnalyze(query, self.global_state)
        return self.build_from_code(code)

    def build_from_code(self, code: "KustoCode") -> QueryIR:
        """Build the IR from an already-parsed ``KustoCode``."""
        raw_text = str(code.Text)

        diagnostics: list[Diagnostic] = []
        for diag in code.GetDiagnostics():
            code_val: Optional[str] = None
            category_val: Optional[str] = None
            try:
                if diag.Code:
                    code_val = str(diag.Code)
            except Exception:  # pragma: no cover
                pass
            try:
                if diag.Category:
                    category_val = str(diag.Category)
            except Exception:  # pragma: no cover
                pass
            diagnostics.append(Diagnostic(
                message=str(diag.Message),
                severity=str(diag.Severity),
                span=Span(text_start=diag.Start, width=diag.Length),
                code=code_val,
                category=category_val,
            ))

        root = code.Syntax
        let_bindings: list[LetBinding] = [
            LetBinding(
                name=visit_name(ls.Name),
                span=to_span(ls),
                category="alias",
            )
            for ls in root.GetDescendants[LetStatement]()
        ]

        main_pipeline: Optional[Pipeline] = None
        expr_stmts = root.GetDescendants[ExpressionStatement]()
        if expr_stmts is not None and expr_stmts.Count > 0:
            main_pipeline = self._visit_pipeline(expr_stmts[0].Expression)
        if not main_pipeline:
            main_pipeline = self._visit_pipeline(root)

        # Whitespace-normalize the syntax tree before hashing so two queries
        # differing only in formatting collide.
        text = re.sub(r"\s+", " ", code.Syntax.ToString())
        text = re.sub(r"\s*([|,()])\s*", r"\1", text).strip()
        structural_hash = hashlib.sha256(text.encode()).hexdigest()

        return QueryIR(
            raw_text=raw_text,
            structural_hash=structural_hash,
            let_bindings=let_bindings,
            main_pipeline=main_pipeline,
            diagnostics=diagnostics,
        )

    # -- pipeline / operator dispatch ------------------------------------

    def _visit_pipeline(self, node: Any) -> Pipeline:
        operators: list[Any] = []
        source: Union[TableRef, LetRef, UnknownSource, Pipeline] = UnknownSource(
            raw_text="unknown", span=to_span(node),
        )

        def walk(n: Any) -> None:
            nonlocal source
            if not n:
                return
            kind = str(type(n).__name__)

            if kind == "PipeExpression":
                walk(n.Expression)
                walk(n.Operator)
                return

            if kind == "ParenthesizedExpression":
                # `join (T)` and `join (T | where X)` arrive wrapped in parens;
                # without unwrapping the RHS pipeline gets UnknownSource.
                walk(n.Expression)
                return

            if kind == "MaterializeExpression":
                # `materialize(P)` at source position becomes a nested Pipeline.
                if isinstance(source, UnknownSource):
                    source = self._visit_pipeline(n.Expression)
                return

            if kind == "PathExpression" and isinstance(source, UnknownSource):
                # `cluster("c").database("d").T` / `database("d").T`.
                tbl = extract_qualified_table_name(n)
                if tbl:
                    source = TableRef(name=tbl, span=to_span(n))
                    return

            if kind == "FunctionCallExpression":
                # User-defined table-valued function at source position
                # (e.g. `findAnomalies(field) | summarize ...`).
                if isinstance(source, UnknownSource):
                    name = "unknown"
                    name_node = getattr(n, "Name", None)
                    if name_node is not None:
                        if hasattr(name_node, "SimpleName"):
                            name = str(name_node.SimpleName)
                        else:
                            name = visit_name(name_node)
                    args: list[AnyExpr] = []
                    arg_list = getattr(n, "ArgumentList", None)
                    if arg_list is not None and hasattr(arg_list, "Expressions"):
                        for el in _iter_elements(arg_list.Expressions):
                            args.append(self._visit_expr(el))
                    source = FuncCallSource(name=name, args=args, span=to_span(n))
                return

            if kind == "DataTableExpression":
                # `datatable(schema)[values]` — inline tabular literal.
                if isinstance(source, UnknownSource):
                    source = FuncCallSource(
                        name="datatable", args=[], span=to_span(n),
                    )
                return

            if kind.endswith("Operator"):
                op = self._visit_operator(n)
                if op:
                    operators.append(op)
                return

            if kind in ("TableReference", "NameReference") or "Reference" in kind:
                if isinstance(source, UnknownSource):
                    name = ""
                    if hasattr(n, "Name"):
                        name = visit_name(n.Name)
                    elif hasattr(n, "SimpleName"):
                        name = n.SimpleName.strip()
                    else:
                        name = n.ToString().strip()
                    if name.lower() not in ("and", "or", "in", "in~", "has", "has_any", "not", "search"):
                        source = TableRef(name=name, span=to_span(n))

        walk(node)
        operators = merge_filter_ops(operators)
        # Operators-but-no-explicit-source means the source is implicit (parent
        # rows: union-at-root, mv-apply/partition/fork subqueries, join RHS).
        if isinstance(source, UnknownSource) and operators:
            source = ImplicitSource(span=to_span(node))
        return Pipeline(source=source, operators=operators)

    def _visit_operator(self, node: Any) -> Optional[Operator]:
        kind = str(type(node).__name__)
        span = to_span(node)
        n = node

        if kind == "FilterOperator":
            return FilterOp(predicate=self._visit_expr(n.Condition), span=span)

        if kind == "ExtendOperator":
            assigns = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    assigns.append(self._visit_assignment(el))
            return ExtendOp(assignments=assigns, span=span)

        if kind == "SummarizeOperator":
            aggs = []
            if hasattr(n, "Aggregates"):
                for el in _iter_elements(n.Aggregates):
                    aggs.append(self._visit_assignment(el))
            by = []
            if hasattr(n, "ByClause") and n.ByClause:
                if hasattr(n.ByClause, "Expressions"):
                    for el in _iter_elements(n.ByClause.Expressions):
                        by.append(self._visit_expr_as_assignment(el))
            return SummarizeOp(aggregations=aggs, by=by, span=span)

        if kind == "JoinOperator":
            rhs = self._visit_pipeline(n.Expression)
            on_cols: list[str] = []
            on_exprs: list[AnyExpr] = []
            if hasattr(n, "ConditionClause") and n.ConditionClause:
                cc = n.ConditionClause
                cc_kind = str(type(cc).__name__)
                if cc_kind == "JoinOnClause":
                    for expr_node in _iter_elements(cc.Expressions):
                        on_cols.append(expr_node.ToString().strip())
                        on_exprs.append(self._visit_expr(expr_node))
            return JoinOp(
                kind=extract_named_param(n, "kind", default="inner"),
                right=rhs,
                on_columns=on_cols,
                on_expressions=on_exprs,
                span=span,
            )

        if kind == "LookupOperator":
            rhs = self._visit_pipeline(n.Expression)
            on_cols = []
            on_exprs = []
            # On-clause surfaces as `LookupClause` or `ConditionClause` by build.
            cc = getattr(n, "LookupClause", None) or getattr(n, "ConditionClause", None)
            if cc and str(type(cc).__name__) == "JoinOnClause":
                for expr_node in _iter_elements(cc.Expressions):
                    on_cols.append(expr_node.ToString().strip())
                    on_exprs.append(self._visit_expr(expr_node))
            return LookupOp(
                kind=extract_named_param(n, "kind", default="inner"),
                right=rhs,
                on_columns=on_cols,
                on_expressions=on_exprs,
                span=span,
            )

        if kind == "PartitionByOperator":
            return PartitionOp(
                by=self._visit_expr(n.Expression),
                right=self._visit_pipeline(n.Subquery),
                span=span,
            )

        if kind == "PartitionOperator":
            # `partition [hint.strategy=…] by C (...)`. AST exposes ByExpression
            # and Operand.Subquery (PartitionSubquery → .Subquery). Hints in
            # Parameters are not surfaced by PartitionOp.
            by_node = getattr(n, "ByExpression", None)
            operand = getattr(n, "Operand", None)
            sub_node = getattr(operand, "Subquery", None) if operand is not None else None
            return PartitionOp(
                by=self._visit_expr(by_node) if by_node is not None else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None", reason="Missing partition by",
                ),
                right=self._visit_pipeline(sub_node) if sub_node is not None else Pipeline(
                    source=UnknownSource(raw_text="?", span=span), operators=[],
                ),
                span=span,
            )

        if kind == "ProjectOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr_as_assignment(el))
            return ProjectOp(columns=cols, span=span)

        if kind == "ProjectAwayOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            return ProjectAwayOp(columns=cols, span=span)

        if kind == "ProjectKeepOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            return ProjectKeepOp(columns=cols, span=span)

        if kind == "ProjectReorderOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            return ProjectReorderOp(columns=cols, span=span)

        if kind == "ProjectRenameOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_assignment(el))
            return ProjectRenameOp(columns=cols, span=span)

        if kind == "ProjectByNamesOperator":
            names = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    names.append(self._visit_expr(el))
            return ProjectByNamesOp(names=names, span=span)

        if kind == "DistinctOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr_as_assignment(el))
            return DistinctOp(columns=cols, span=span)

        if kind == "TakeOperator":
            return TakeOp(count=safe_int(n.Expression), span=span)

        if kind == "SampleOperator":
            return SampleOp(count=safe_int(n.Expression), span=span)

        if kind == "SortOperator":
            exprs = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    inner = getattr(el, "Expression", el)
                    exprs.append(self._visit_expr(inner))
            return SortOp(expressions=exprs, span=span)

        if kind == "TopOperator":
            return TopOp(count=safe_int(n.Expression), by=self._visit_expr(n.ByExpression), span=span)

        if kind == "TopHittersOperator":
            return TopHittersOp(count=safe_int(n.Expression), by=self._visit_expr(n.ValueExpression), span=span)

        if kind == "SearchOperator":
            return SearchOp(predicate=self._visit_expr(n.Condition) if hasattr(n, "Condition") else None, span=span)

        if kind == "UnionOperator":
            pipes = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    pipes.append(self._visit_pipeline(el))
            return UnionOp(pipelines=pipes, span=span)

        if kind == "MakeSeriesOperator":
            aggs = []
            if hasattr(n, "Aggregates"):
                for el in _iter_elements(n.Aggregates):
                    # Aggregates arrive as MakeSeriesExpression wrapping the
                    # actual SimpleNamedExpression (Count = count()).
                    inner = getattr(el, "Expression", el)
                    aggs.append(self._visit_assignment(inner))
            by = []
            if hasattr(n, "ByClause") and n.ByClause:
                for el in _iter_elements(n.ByClause.Expressions):
                    by.append(self._visit_assignment(el))
            return MakeSeriesOp(aggregations=aggs, by=by, span=span)

        if kind == "MvExpandOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for mve in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(mve.Expression))
            return MvExpandOp(columns=cols, span=span)

        if kind == "MvApplyOperator":
            assigns = []
            if hasattr(n, "Expressions"):
                for mve in _iter_elements(n.Expressions):
                    assigns.append(self._visit_assignment(mve.Expression))
            # n.Subquery wraps the real pipe/operator at .Expression.
            sub = getattr(n, "Subquery", None)
            inner = getattr(sub, "Expression", sub) if sub is not None else None
            return MvApplyOp(
                assignments=assigns,
                right=self._visit_pipeline(inner) if inner is not None else Pipeline(
                    source=UnknownSource(raw_text="?", span=span), operators=[],
                ),
                span=span,
            )

        if kind == "ParseOperator":
            patterns = []
            if hasattr(n, "Patterns"):
                for i in range(n.Patterns.Count):
                    p = n.Patterns[i]
                    patterns.append(self._visit_expr(p))
            return ParseOp(
                target=self._visit_expr(n.Expression) if hasattr(n, "Expression")
                else UnknownExpr(span=span, raw_text="?", ast_kind="None", reason="Missing parse target"),
                patterns=patterns, span=span,
            )

        if kind == "ParseWhereOperator":
            patterns = []
            if hasattr(n, "Patterns"):
                for i in range(n.Patterns.Count):
                    p = n.Patterns[i]
                    patterns.append(self._visit_expr(p))
            return ParseWhereOp(
                target=self._visit_expr(n.Expression) if hasattr(n, "Expression")
                else UnknownExpr(span=span, raw_text="?", ast_kind="None", reason="Missing parse target"),
                patterns=patterns, span=span,
            )

        if kind == "AsOperator":
            return AsOp(name=visit_name(n.Name), span=span)

        if kind == "RangeOperator":
            return RangeOp(
                column=visit_name(n.Name),
                start=self._visit_expr(n.From),
                end=self._visit_expr(n.To),
                step=self._visit_expr(n.Step),
                span=span,
            )

        if kind == "RenderOperator":
            return RenderOp(kind=n.ChartType.ToString().strip() if hasattr(n, "ChartType") else "table", span=span)

        if kind == "EvaluateOperator":
            # `evaluate <plugin>(...)` — .NET node exposes FunctionCall.
            func_expr = self._visit_expr(n.FunctionCall) if hasattr(n, "FunctionCall") else None
            if not isinstance(func_expr, FuncCall):
                func_expr = FuncCall(name="<unparsed>", args=[], span=span)
            return EvaluateOp(func=func_expr, span=span)

        if kind == "CountOperator":
            as_name: Optional[str] = None
            clause = getattr(n, "AsIdentifier", None) or getattr(n, "AsClause", None)
            if clause is not None:
                name_node = getattr(clause, "Identifier", None) or getattr(clause, "Name", None)
                if name_node is not None:
                    as_name = visit_name(name_node)
            return CountOp(as_name=as_name, span=span)

        if kind == "PrintOperator":
            cols: list = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr_as_assignment(el))
            return PrintOp(columns=cols, span=span)

        if kind == "GetSchemaOperator":
            return GetSchemaOp(span=span)

        if kind == "ConsumeOperator":
            return ConsumeOp(span=span)

        if kind == "ExecuteAndCacheOperator":
            return ExecuteAndCacheOp(span=span)

        if kind == "SerializeOperator":
            assigns = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    assigns.append(self._visit_assignment(el))
            return SerializeOp(assignments=assigns, span=span)

        if kind == "InvokeOperator":
            func_node = getattr(n, "Function", None) or getattr(n, "FunctionCall", None)
            func_expr = self._visit_expr(func_node) if func_node is not None else None
            if not isinstance(func_expr, FuncCall):
                func_expr = FuncCall(name="<unparsed>", args=[], span=span)
            return InvokeOp(func=func_expr, span=span)

        if kind == "FindOperator":
            pred = (
                self._visit_expr(n.Condition)
                if hasattr(n, "Condition") and n.Condition else None
            )
            tables: list[str] = []
            in_clause = getattr(n, "InClause", None)
            if in_clause is not None and hasattr(in_clause, "Expressions"):
                for el in _iter_elements(in_clause.Expressions):
                    tables.append(el.ToString().strip())
            return FindOp(predicate=pred, tables=tables, span=span)

        if kind == "FacetOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            with_pipeline = None
            with_clause = getattr(n, "WithClause", None) or getattr(n, "Subquery", None)
            if with_clause is not None:
                # WithClause may wrap the pipeline at .Expression.
                inner = getattr(with_clause, "Expression", with_clause)
                with_pipeline = self._visit_pipeline(inner)
            return FacetOp(columns=cols, with_pipeline=with_pipeline, span=span)

        if kind == "ForkOperator":
            pipes = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    pipes.append(self._visit_pipeline(el))
            return ForkOp(pipelines=pipes, span=span)

        if kind == "AssertSchemaOperator":
            asserted: dict[str, str] = {}
            schema_node = getattr(n, "Schema", None)
            if schema_node is not None and hasattr(schema_node, "Columns"):
                for col in _iter_elements(schema_node.Columns):
                    cname = visit_name(col)
                    ctype_node = getattr(col, "Type", None)
                    asserted[cname] = ctype_node.ToString().strip() if ctype_node else "unknown"
            return AssertSchemaOp(columns=asserted, span=span)

        if kind == "ParseKvOperator":
            target = (
                self._visit_expr(n.Expression)
                if hasattr(n, "Expression") else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None",
                    reason="Missing parse-kv target",
                )
            )
            cols = []
            keys = getattr(n, "Keys", None)
            if keys is not None and hasattr(keys, "Count"):
                for el in _iter_elements(keys):
                    cols.append(self._visit_assignment(el))
            return ParseKvOp(target=target, columns=cols, span=span)

        if kind == "SampleDistinctOperator":
            count = safe_int(n.Expression) if hasattr(n, "Expression") else 0
            of_node = getattr(n, "OfExpression", None) or getattr(n, "Of", None)
            of = self._visit_expr(of_node) if of_node is not None else UnknownExpr(
                span=span, raw_text="?", ast_kind="None",
                reason="Missing sample-distinct 'of'",
            )
            return SampleDistinctOp(count=count, of=of, span=span)

        if kind == "GraphWhereEdgesOperator":
            return GraphWhereEdgesOp(predicate=self._visit_expr(n.Condition), span=span)

        if kind == "GraphWhereNodesOperator":
            return GraphWhereNodesOp(predicate=self._visit_expr(n.Condition), span=span)

        # Preserve-raw-text ops for elaborate state-machine operators.
        if kind == "ScanOperator":
            return ScanOp(raw_text=node.ToString(), span=span)
        if kind == "TopNestedOperator":
            return TopNestedOp(raw_text=node.ToString(), span=span)
        if kind == "MakeGraphOperator":
            return MakeGraphOp(raw_text=node.ToString(), span=span)
        if kind == "MacroExpandOperator":
            inner = None
            inner_node = getattr(n, "Subquery", None) or getattr(n, "Body", None)
            if inner_node is not None:
                inner = self._visit_pipeline(inner_node)
            return MacroExpandOp(raw_text=node.ToString(), pipeline=inner, span=span)
        if kind == "GraphMatchOperator":
            return GraphMatchOp(raw_text=node.ToString(), span=span)
        if kind == "GraphMarkComponentsOperator":
            return GraphMarkComponentsOp(raw_text=node.ToString(), span=span)
        if kind == "GraphShortestPathsOperator":
            return GraphShortestPathsOp(raw_text=node.ToString(), span=span)
        if kind == "GraphToTableOperator":
            return GraphToTableOp(raw_text=node.ToString(), span=span)

        return Operator(span=span)

    # -- expression dispatch ---------------------------------------------

    def _visit_expr(self, node: Any) -> AnyExpr:
        if not node:
            return UnknownExpr(
                span=Span(text_start=0, width=0),
                raw_text="", ast_kind="None", reason="Empty node",
            )

        span = to_span(node)
        kind = str(type(node).__name__)
        res: Optional[AnyExpr] = None

        if kind == "ParenthesizedExpression":
            res = self._visit_expr(node.Expression)

        elif kind == "NameReference":
            name = visit_name(node.Name)
            res = ColumnRef(name=name, span=span)

        elif kind == "NameDeclaration":
            res = ColumnRef(name=visit_name(node), span=span)

        elif kind == "NameAndTypeDeclaration":
            res = ColumnRef(name=visit_name(node), span=span)

        elif kind == "PathExpression":
            # Collapse `$left.X` / `$right.X` (syntactic) and `T.X` (via binder
            # ReferencedSymbol) into ColumnRef. Dynamic property access stays
            # as PathExpr.
            expr_node = node.Expression
            sel_node = node.Selector
            expr_kind = str(type(expr_node).__name__)
            sel_kind = str(type(sel_node).__name__)
            if expr_kind == "NameReference" and sel_kind == "NameReference":
                lhs_name = visit_name(expr_node.Name)
                rhs_name = visit_name(sel_node.Name)
                if lhs_name in ("$left", "$right"):
                    res = ColumnRef(name=rhs_name, table=lhs_name, span=span)
                elif is_table_symbol(getattr(expr_node, "ReferencedSymbol", None)):
                    res = ColumnRef(name=rhs_name, table=lhs_name, span=span)
                else:
                    res = PathExpr(
                        expression=self._visit_expr(expr_node),
                        selector=self._visit_expr(sel_node),
                        span=span,
                    )
            else:
                res = PathExpr(
                    expression=self._visit_expr(expr_node),
                    selector=self._visit_expr(sel_node),
                    span=span,
                )

        elif kind == "ElementExpression":
            res = ElementExpr(
                expression=self._visit_expr(node.Expression),
                selector=self._visit_expr(node.Selector),
                span=span,
            )

        elif kind == "SimpleNamedExpression":
            res = NamedExpr(
                name=visit_name(node.Name),
                expression=self._visit_expr(node.Expression),
                span=span,
            )

        elif kind == "CompoundNamedExpression":
            names: list[str] = []
            if hasattr(node, "Names") and node.Names:
                sub_list = node.Names.Names
                for el in _iter_elements(sub_list):
                    names.append(visit_name(el))
            res = CompoundNamedExpr(
                names=names,
                expression=self._visit_expr(node.Expression),
                span=span,
            )

        elif kind == "BracketedExpression":
            res = BracketedExpr(expression=self._visit_expr(node.Expression), span=span)

        elif kind == "PrefixUnaryExpression":
            op_str = node.Operator.ToString().strip()
            operand = self._visit_expr(node.Expression)
            if op_str == "!":
                res = Not(operand=operand, span=span)
            else:
                res = UnaryOp(op=op_str, operand=operand, span=span)

        elif kind == "StarExpression":
            res = StarExpr(span=span)

        elif kind == "LiteralExpression":
            val = node.LiteralValue
            if hasattr(val, "ToString"):
                if not isinstance(val, (str, int, float, bool, type(None))):
                    val = val.ToString()
            lit_kind = "string"
            if isinstance(val, bool):
                lit_kind = "bool"
            elif isinstance(val, (int, float)):
                lit_kind = "int"
            res = LiteralExpr(value=val, literal_kind=lit_kind, span=span)

        elif kind == "DynamicExpression":
            # LiteralValue is the JSON body as a string; consumers can json.loads.
            body = node.LiteralValue if hasattr(node, "LiteralValue") else node.ToString()
            res = LiteralExpr(value=str(body), literal_kind="dynamic", span=span)

        elif kind in ("AndExpression", "OrExpression"):
            left = self._visit_expr(node.Left)
            right = self._visit_expr(node.Right)
            if kind == "AndExpression":
                res = And(operands=[left, right], span=span)
            else:
                res = Or(operands=[left, right], span=span)

        elif kind == "OrderedExpression":
            res = self._visit_expr(node.Expression)

        elif kind == "BinaryExpression":
            op = node.Operator.ToString().strip().lower()
            left = self._visit_expr(node.Left)
            right = self._visit_expr(node.Right)
            if op == "and":
                res = And(operands=[left, right], span=span)
            elif op == "or":
                res = Or(operands=[left, right], span=span)
            elif op == "matches regex":
                if isinstance(right, LiteralExpr) and isinstance(right.value, str):
                    pattern_str = right.value
                else:
                    pattern_str = node.Right.ToString().strip(" \"'")
                res = RegexMatch(
                    target=left,
                    pattern=pattern_str,
                    case_sensitive=True,
                    span=span,
                )
            else:
                case_sensitive = True
                if op.endswith("~"):
                    case_sensitive = False
                elif op in ("has", "contains", "startswith", "endswith", "has_any", "has_all"):
                    case_sensitive = False
                elif op in ("==", "in", "!in"):
                    case_sensitive = True
                elif "_cs" in op:
                    case_sensitive = True
                res = BinOp(
                    op=op,
                    polarity="inclusion" if "!" not in op else "exclusion",
                    case_sensitive=case_sensitive,
                    left=left,
                    right=right,
                    span=span,
                )

        elif kind in ("InExpression", "HasAnyExpression", "HasAllExpression"):
            res = SetMembership(
                column=self._visit_expr(node.Left),
                values=self._visit_list(node.Right),
                polarity="inclusion" if "!" not in node.Operator.ToString() else "exclusion",
                case_sensitive=False,
                span=span,
            )

        elif kind == "BetweenExpression":
            # Bounds live in an `ExpressionCouple` via `First`/`Second` —
            # not `Left`/`Right` like other binary expressions.
            couple = node.Right
            low_node = getattr(couple, "First", None)
            high_node = getattr(couple, "Second", None)
            res = Between(
                target=self._visit_expr(node.Left),
                low=self._visit_expr(low_node) if low_node is not None else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None", reason="Missing between low",
                ),
                high=self._visit_expr(high_node) if high_node is not None else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None", reason="Missing between high",
                ),
                polarity="inclusion" if "!" not in node.Operator.ToString() else "exclusion",
                span=span,
            )

        elif kind == "FunctionCallExpression":
            # Prefer binder-resolved name; fall back to syntactic.
            name = "unknown"
            ref_sym = getattr(node, "ReferencedSymbol", None)
            if ref_sym is not None:
                try:
                    name = ref_sym.Name
                except AttributeError:
                    ref_sym = None
            if not ref_sym:
                try:
                    name_node = node.Name
                    if hasattr(name_node, "SimpleName"):
                        name = str(name_node.SimpleName)
                    else:
                        name = visit_name(name_node)
                except AttributeError as e:  # pragma: no cover
                    logger.debug("FunctionCall name resolution fell through: %s", e)

            args: list[AnyExpr] = []
            if hasattr(node, "ArgumentList") and node.ArgumentList:
                if hasattr(node.ArgumentList, "Expressions"):
                    for el in _iter_elements(node.ArgumentList.Expressions):
                        args.append(self._visit_expr(el))

            res = FuncCall(
                name=name, args=args,
                is_time_func=_is_time_func_name(name),
                span=span,
            )

            # Lift case()/iif()/isnotnull()/not() into structural nodes.
            lname = name.lower()
            if lname == "case" and len(args) >= 3 and len(args) % 2 == 1:
                branches = [(args[i], args[i + 1]) for i in range(0, len(args) - 1, 2)]
                res = CaseExpr(branches=branches, default=args[-1], span=span)
            elif lname == "iif" and len(args) == 3:
                res = CaseExpr(branches=[(args[0], args[1])], default=args[2], span=span)
            elif lname in ("isnotnull", "isnotempty") and len(args) == 1:
                res = Exists(target=args[0], span=span)
            elif lname == "not" and len(args) == 1:
                res = Not(operand=args[0], span=span)

        elif kind == "MaterializeExpression":
            res = MaterializeExpr(pipeline=self._visit_pipeline(node.Expression), span=span)

        elif kind == "ToScalarExpression":
            res = ToScalarExpr(pipeline=self._visit_pipeline(node.Expression), span=span)

        elif kind == "ExternalDataExpression":
            cols: list[tuple[str, str]] = []
            uri = "url"
            if hasattr(node, "Uris") and node.Uris.Count > 0:
                uri = node.Uris[0].Element.ToString().strip(" @\"")
            res = ExternalDataExpr(columns=cols, uri=uri, format="unknown", span=span)

        elif kind == "MakeSeriesExpression":
            res = self._visit_expr(node.Expression)

        if not res:
            res = UnknownExpr(
                span=span, raw_text=node.ToString(),
                ast_kind=kind, reason="Unsupported expression type",
            )

        map_semantic_info(node, res)
        res = normalize_in_place(res)
        res.canonical_form = canonical(res)
        return res

    # -- visitor-stateful helpers ----------------------------------------

    def _visit_assignment(self, node: Any) -> Assignment:
        kind = str(type(node).__name__)
        if kind == "SimpleNamedExpression":
            return Assignment(
                name=visit_name(node.Name),
                expr=self._visit_expr(node.Expression),
                span=to_span(node),
            )
        return Assignment(
            name=node.ToString().strip(),
            expr=self._visit_expr(node),
            span=to_span(node),
        )

    def _visit_expr_as_assignment(self, node: Any) -> Union[ColumnRef, Assignment, AnyExpr]:
        kind = str(type(node).__name__)
        if kind == "SimpleNamedExpression":
            return self._visit_assignment(node)
        return self._visit_expr(node)

    def _visit_list(self, node: Any) -> list[AnyExpr]:
        exprs: list[AnyExpr] = []
        if not node:
            return exprs
        kind = str(type(node).__name__)
        if kind == "ParenthesizedExpression":
            return self._visit_list(node.Expression)
        if kind == "ExpressionList":
            if hasattr(node, "Expressions"):
                return self._visit_list(node.Expressions)
        if "SyntaxList" in kind or hasattr(node, "Count"):
            for i in range(node.Count):
                element = node[i]
                if hasattr(element, "Element"):
                    exprs.append(self._visit_expr(element.Element))
                else:
                    exprs.append(self._visit_expr(element))
        else:
            r = self._visit_expr(node)
            if r:
                exprs.append(r)
        return exprs

