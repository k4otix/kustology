# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Schema-driven enrichment of an already-built IR.

Fills ``result_type`` on expressions the .NET binder couldn't resolve and
attaches ``table`` provenance to ``ColumnRef`` nodes by walking the pipeline
with a growing scope. The constructor takes a ``{table: {column: type}}``
dict directly — callers handle JSON/YAML/IO themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .expr import (
    And, Between, BinOp, ColumnRef, Expr, LiteralExpr, Not, Or, SetMembership,
)
from .query import (
    Assignment, DistinctOp, ExtendOp, FilterOp, JoinOp, LookupOp,
    MakeSeriesOp, MvExpandOp, Operator, ParseOp, ParseWhereOp, Pipeline,
    ProjectAwayOp, ProjectByNamesOp, ProjectKeepOp, ProjectOp, ProjectRenameOp,
    ProjectReorderOp, QueryIR, SummarizeOp, TableRef, TabularSchema, UnionOp,
)
from .types import KustoType


@dataclass
class ScopeEntry:
    """One source visible at a point in the pipeline.

    Joins, lookups, and unions append entries; project / summarize replace
    them with a synthesized anonymous entry (``table=None``).
    """

    table: Optional[str]
    columns: Dict[str, str] = field(default_factory=dict)


class SchemaAttacher:
    """Walks an IR pipeline and fills column provenance + result types.

    ``schemas`` is a flat ``{table_name: {column_name: kusto_type_string}}``.
    Tables not present here are treated as opaque (no enrichment).
    """

    def __init__(self, schemas: Optional[Dict[str, Dict[str, str]]] = None):
        self.schemas: Dict[str, Dict[str, str]] = dict(schemas or {})

    def enrich(self, ir: QueryIR) -> QueryIR:
        self._walk_pipeline(ir.main_pipeline)
        ir.schema_attached = True
        return ir

    def _table_schema(self, name: Optional[str]) -> Dict[str, str]:
        if not name:
            return {}
        return self.schemas.get(name, {})

    def _source_entry(self, pipeline: Pipeline) -> ScopeEntry:
        source = pipeline.source
        name = source.name if isinstance(source, TableRef) else None
        return ScopeEntry(table=name, columns=self._table_schema(name))

    def _walk_pipeline(self, pipeline: Pipeline) -> List[ScopeEntry]:
        scope: List[ScopeEntry] = [self._source_entry(pipeline)]
        for op in pipeline.operators:
            self._walk_operator(op, scope)
        # Snapshot final scope so downstream consumers don't re-walk operators.
        merged: Dict[str, str] = {}
        for entry in scope:
            merged.update(entry.columns)
        pipeline.result_schema = TabularSchema(columns=merged)
        return scope

    # --- scope-mutation helpers ---------------------------------------------

    def _scope_columns(self, scope: List[ScopeEntry]) -> Dict[str, str]:
        """Flatten scope into a single {column: type} map (most recent wins)."""
        merged: Dict[str, str] = {}
        for entry in scope:
            merged.update(entry.columns)
        return merged

    def _set_scope(self, scope: List[ScopeEntry], columns: Dict[str, str]) -> None:
        """Replace scope with a single anonymous entry containing `columns`."""
        scope.clear()
        scope.append(ScopeEntry(table=None, columns=columns))

    def _extract_target_name(self, expr) -> Optional[str]:
        """Pull a bare column name from a ColumnRef / Assignment / similar."""
        if isinstance(expr, ColumnRef):
            return expr.name
        name = getattr(expr, "name", None)
        if isinstance(name, str):
            return name
        return None

    def _walk_operator(self, op: Operator, scope: List[ScopeEntry]) -> None:
        if isinstance(op, FilterOp):
            self._fill(op.predicate, scope)
            return
        if isinstance(op, (JoinOp, LookupOp)):
            rhs_scope = self._walk_pipeline(op.right)
            on_scope = scope + rhs_scope[:1]
            for e in op.on_expressions:
                self._fill(e, on_scope)
            scope.extend(rhs_scope[:1])
            return
        if isinstance(op, UnionOp):
            for sub in op.pipelines:
                sub_scope = self._walk_pipeline(sub)
                for entry in sub_scope:
                    if entry not in scope:
                        scope.append(entry)
            return
        if isinstance(op, ExtendOp):
            new_cols: Dict[str, str] = {}
            for a in op.assignments:
                self._fill(a.expr, scope)
                kt = getattr(a.expr, "result_type", KustoType.UNKNOWN)
                new_cols[a.name] = kt.value if kt != KustoType.UNKNOWN else "unknown"
            scope.append(ScopeEntry(table=None, columns=new_cols))
            return
        if isinstance(op, SummarizeOp):
            out_cols: Dict[str, str] = {}
            for a in op.aggregations:
                self._fill(a.expr, scope)
                kt = getattr(a.expr, "result_type", KustoType.UNKNOWN)
                out_cols[a.name] = kt.value if kt != KustoType.UNKNOWN else "unknown"
            for b in op.by:
                inner = getattr(b, "expr", b)
                self._fill(inner, scope)
                name = self._extract_target_name(b) or self._extract_target_name(inner)
                if name:
                    kt = getattr(inner, "result_type", KustoType.UNKNOWN)
                    out_cols[name] = kt.value if kt != KustoType.UNKNOWN else "unknown"
            self._set_scope(scope, out_cols)
            return
        if isinstance(op, ProjectOp):
            current = self._scope_columns(scope)
            kept: Dict[str, str] = {}
            for c in op.columns:
                self._fill(getattr(c, "expr", c), scope)
                if isinstance(c, Assignment):
                    kt = getattr(c.expr, "result_type", KustoType.UNKNOWN)
                    kept[c.name] = kt.value if kt != KustoType.UNKNOWN else "unknown"
                elif isinstance(c, ColumnRef):
                    kept[c.name] = current.get(c.name, "unknown")
                else:
                    name = self._extract_target_name(c)
                    if name:
                        kept[name] = current.get(name, "unknown")
            self._set_scope(scope, kept)
            return
        if isinstance(op, ProjectRenameOp):
            current = self._scope_columns(scope)
            for c in op.columns:
                self._fill(c.expr, scope)
                old = self._extract_target_name(c.expr)
                if old and old in current:
                    current[c.name] = current.pop(old)
            self._set_scope(scope, current)
            return
        if isinstance(op, DistinctOp):
            # Row-filter — scope unchanged.
            for c in op.columns:
                self._fill(getattr(c, "expr", c), scope)
            return
        if isinstance(op, ProjectAwayOp):
            current = self._scope_columns(scope)
            for c in op.columns:
                self._fill(getattr(c, "expr", c) if not isinstance(c, Expr) else c, scope)
                name = self._extract_target_name(c)
                if name and name in current:
                    current.pop(name)
            self._set_scope(scope, current)
            return
        if isinstance(op, ProjectKeepOp):
            current = self._scope_columns(scope)
            kept = {}
            for c in op.columns:
                self._fill(getattr(c, "expr", c) if not isinstance(c, Expr) else c, scope)
                name = self._extract_target_name(c)
                if name and name in current:
                    kept[name] = current[name]
            self._set_scope(scope, kept)
            return
        if isinstance(op, ProjectReorderOp):
            # Reorder is non-destructive on scope.
            for c in op.columns:
                self._fill(getattr(c, "expr", c) if not isinstance(c, Expr) else c, scope)
            return
        if isinstance(op, ProjectByNamesOp):
            # Dynamic names — can't statically reshape scope.
            for n_expr in op.names:
                self._fill(n_expr, scope)
            return
        if isinstance(op, (ParseOp, ParseWhereOp)):
            self._fill(op.target, scope)
            new_cap: Dict[str, str] = {}
            for p in op.patterns:
                self._fill(p, scope)
                if isinstance(p, ColumnRef):
                    kt = getattr(p, "result_type", KustoType.UNKNOWN)
                    new_cap[p.name] = kt.value if kt != KustoType.UNKNOWN else "string"
            if new_cap:
                scope.append(ScopeEntry(table=None, columns=new_cap))
            return
        if isinstance(op, MvExpandOp):
            current = self._scope_columns(scope)
            for c in op.columns:
                self._fill(c, scope)
                name = self._extract_target_name(c)
                if not name:
                    continue
                inner = getattr(c, "result_type_inner", None)
                if inner is not None:
                    current[name] = inner.value if hasattr(inner, "value") else str(inner)
                else:
                    # Post-expand row has unspecified element type.
                    current[name] = current.get(name, "dynamic")
            self._set_scope(scope, current)
            return
        if isinstance(op, MakeSeriesOp):
            out_cols2: Dict[str, str] = {}
            for a in op.aggregations:
                # make-series aggregates produce dynamic arrays.
                self._fill(a.expr, scope)
                out_cols2[a.name] = "dynamic"
            for b in op.by:
                self._fill(b.expr, scope)
                kt = getattr(b.expr, "result_type", KustoType.UNKNOWN)
                out_cols2[b.name] = kt.value if kt != KustoType.UNKNOWN else "unknown"
            for attr in ("range_from", "range_to", "step"):
                e = getattr(op, attr, None)
                if e is not None:
                    self._fill(e, scope)
            self._set_scope(scope, out_cols2)
            return

    def _resolve_column_table(self, name: str, scope: List[ScopeEntry]) -> Optional[str]:
        # Most recently joined side wins on collisions (matches KQL binding).
        matches = [e.table for e in scope if e.table and name in e.columns]
        if not matches:
            return None
        return matches[-1]

    def _fill(self, expr: Optional[Expr], scope: List[ScopeEntry]) -> None:
        if expr is None:
            return
        for attr in (
            "left", "right", "operand", "target", "expression", "selector",
            "column", "low", "high",
        ):
            child = getattr(expr, attr, None)
            if child is not None and isinstance(child, Expr):
                self._fill(child, scope)
        for attr in ("operands", "args", "values"):
            children = getattr(expr, attr, None)
            if not children:
                continue
            for item in children:
                if isinstance(item, Expr):
                    self._fill(item, scope)

        if isinstance(expr, ColumnRef):
            if expr.table == "$left" and len(scope) >= 2:
                expr.table = scope[-2].table or expr.table
            elif expr.table == "$right" and len(scope) >= 1:
                expr.table = scope[-1].table or expr.table
            if expr.table is None:
                resolved = self._resolve_column_table(expr.name, scope)
                if resolved:
                    expr.table = resolved
            if expr.result_type == KustoType.UNKNOWN and expr.table:
                t = self._table_schema(expr.table).get(expr.name)
                if t:
                    try:
                        expr.result_type = KustoType(t)
                    except ValueError:
                        pass
            return

        if expr.result_type != KustoType.UNKNOWN:
            return

        if isinstance(expr, LiteralExpr):
            try:
                expr.result_type = KustoType(expr.literal_kind)
            except ValueError:
                pass
        elif isinstance(expr, (BinOp, SetMembership, Between, And, Or, Not)):
            expr.result_type = KustoType.BOOL


BinderEnricher = SchemaAttacher
