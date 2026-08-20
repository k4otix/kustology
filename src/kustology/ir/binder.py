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

from .expr import (
    And,
    Between,
    BinOp,
    ColumnRef,
    Expr,
    LiteralExpr,
    Not,
    Or,
    SetMembership,
)
from .query import (
    Assignment,
    CountOp,
    DistinctOp,
    ExtendOp,
    FilterOp,
    JoinOp,
    LookupOp,
    MakeSeriesOp,
    MvExpandOp,
    Operator,
    ParseOp,
    ParseWhereOp,
    Pipeline,
    ProjectAwayOp,
    ProjectByNamesOp,
    ProjectKeepOp,
    ProjectOp,
    ProjectRenameOp,
    ProjectReorderOp,
    QueryIR,
    SummarizeOp,
    TableRef,
    TabularSchema,
    UnionOp,
)
from .types import KustoType
from .walk import _models_in


@dataclass
class ScopeEntry:
    """One source visible at a point in the pipeline.

    Joins, lookups, and unions append entries; project / summarize replace
    them with a synthesized anonymous entry (``table=None``).
    """

    table: str | None
    columns: dict[str, str] = field(default_factory=dict)


class SchemaAttacher:
    """Walks an IR pipeline and fills column provenance + result types.

    ``schemas`` is a flat ``{table_name: {column_name: kusto_type_string}}``.
    Tables not present here are treated as opaque (no enrichment).

    **Two levels of coverage, deliberately distinguished.** Every operator
    gets its expressions filled and its sub-pipelines walked — that part is
    derived from ``model_fields`` and cannot drift as the model grows. Only
    some operators additionally *reshape* the scope, because only some have
    an output schema we can derive without guessing:

    ``project`` / ``project-away`` / ``project-keep`` / ``project-rename`` /
    ``project-reorder`` / ``summarize`` / ``extend`` / ``distinct`` /
    ``count`` / ``parse`` / ``parse-where`` / ``mv-expand`` / ``make-series``
    / ``join`` / ``lookup`` / ``union`` / ``where`` / ``project-by-names``.

    Operators outside that set pass the scope through unchanged. For the
    ones that genuinely preserve their input schema (``sort``, ``top``,
    ``take``, ``search``, the graph predicates) that is exact; for ones that
    do reshape (``print``, ``range``, ``evaluate``, ``facet``, ``fork``,
    ``mv-apply``, ``partition``, ``parse-kv``, ``serialize``, ``top-nested``)
    the scope downstream is stale. Stale is worse than exact and better than
    the previous behavior, which was to skip those operators entirely so
    their own column references never resolved at all.
    """

    def __init__(self, schemas: dict[str, dict[str, str]] | None = None):
        self.schemas: dict[str, dict[str, str]] = dict(schemas or {})

    def enrich(self, ir: QueryIR) -> QueryIR:
        """Enrich ``ir.main_pipeline`` in place and mark the IR attached.

        Scope boundary: ``ir.let_bindings`` are *not* walked, so a tabular
        binding's ``rhs_pipeline.result_schema`` stays ``None`` and the
        ``ColumnRef`` nodes inside it keep ``table=None``. Extending the walk
        there requires threading let-bound names through this class's scope
        model so a later ``let`` (or the main pipeline) sees an earlier one's
        output columns — a behavior change, not a loop addition. See the note
        on ``LetBinding.rhs_pipeline``.
        """
        self._walk_pipeline(ir.main_pipeline)
        ir.schema_attached = True
        return ir

    def _table_schema(self, name: str | None) -> dict[str, str]:
        if not name:
            return {}
        return self.schemas.get(name, {})

    def _source_entry(self, pipeline: Pipeline) -> ScopeEntry:
        source = pipeline.source
        if isinstance(source, Pipeline):
            # ``materialize(P) | …`` nests a whole pipeline in source
            # position. Walk it so its own operators shape the scope the
            # outer pipeline starts from; returning an empty anonymous
            # entry left everything downstream unresolvable.
            self._walk_pipeline(source)
            columns = dict(source.result_schema.columns) if source.result_schema else {}
            return ScopeEntry(table=None, columns=columns)
        name = source.name if isinstance(source, TableRef) else None
        return ScopeEntry(table=name, columns=self._table_schema(name))

    def _walk_pipeline(
        self,
        pipeline: Pipeline,
        inherited: list[ScopeEntry] | None = None,
    ) -> list[ScopeEntry]:
        """Walk ``pipeline``, returning the scope its last operator leaves.

        ``inherited`` seeds the scope for a sub-pipeline whose source is
        implicit — the ``mv-apply`` / ``partition`` / ``fork`` / ``facet``
        bodies, which run against the enclosing row set and so must see its
        columns. A sub-pipeline with its own explicit source ignores it:
        ``toscalar(...)`` and ``materialize(...)`` are uncorrelated in KQL,
        and seeding them would invent provenance the query never expressed.
        """
        source_entry = self._source_entry(pipeline)
        if inherited is not None and not source_entry.table and not source_entry.columns:
            scope: list[ScopeEntry] = [
                ScopeEntry(table=e.table, columns=dict(e.columns)) for e in inherited
            ]
        else:
            scope = [source_entry]
        for op in pipeline.operators:
            self._walk_operator(op, scope)
        # Snapshot final scope so downstream consumers don't re-walk operators.
        merged: dict[str, str] = {}
        for entry in scope:
            merged.update(entry.columns)
        pipeline.result_schema = TabularSchema(columns=merged)
        return scope

    # --- scope-mutation helpers ---------------------------------------------

    def _scope_columns(self, scope: list[ScopeEntry]) -> dict[str, str]:
        """Flatten scope into a single {column: type} map (most recent wins)."""
        merged: dict[str, str] = {}
        for entry in scope:
            merged.update(entry.columns)
        return merged

    def _set_scope(self, scope: list[ScopeEntry], columns: dict[str, str]) -> None:
        """Replace scope with a single anonymous entry containing `columns`."""
        scope.clear()
        scope.append(ScopeEntry(table=None, columns=columns))

    def _extract_target_name(self, expr) -> str | None:
        """Pull a bare column name from a ColumnRef / Assignment / similar."""
        if isinstance(expr, ColumnRef):
            return expr.name
        name = getattr(expr, "name", None)
        if isinstance(name, str):
            return name
        return None

    def _walk_operator(self, op: Operator, scope: list[ScopeEntry]) -> None:
        if isinstance(op, FilterOp):
            self._fill(op.predicate, scope)
            return
        if isinstance(op, (JoinOp, LookupOp)):
            rhs_scope = self._walk_pipeline(op.right)
            on_scope = scope + rhs_scope[:1]
            for e in op.on:
                self._fill(e, on_scope)
            # KQL renames colliding right-side columns with numeric suffixes
            # (Foo, Foo1, Foo2, ...). Lookup additionally drops the right-side
            # join-key columns since they merge into the left's.
            left_names: set[str] = {
                c for entry in scope for c in entry.columns
            }
            drop_keys: set[str] = set()
            if isinstance(op, LookupOp):
                drop_keys = {
                    e.name for e in op.on if isinstance(e, ColumnRef)
                }
            for entry in rhs_scope[:1]:
                renamed: dict[str, str] = {}
                for name, kt in entry.columns.items():
                    if name in drop_keys:
                        continue
                    if name not in left_names:
                        renamed[name] = kt
                        left_names.add(name)
                        continue
                    n = 1
                    candidate = f"{name}{n}"
                    while candidate in left_names:
                        n += 1
                        candidate = f"{name}{n}"
                    renamed[candidate] = kt
                    left_names.add(candidate)
                scope.append(ScopeEntry(table=entry.table, columns=renamed))
            return
        if isinstance(op, UnionOp):
            for sub in op.pipelines:
                sub_scope = self._walk_pipeline(sub)
                for entry in sub_scope:
                    if entry not in scope:
                        scope.append(entry)
            return
        if isinstance(op, ExtendOp):
            new_cols: dict[str, str] = {}
            for a in op.assignments:
                self._fill(a.expr, scope)
                kt = getattr(a.expr, "result_type", KustoType.UNRESOLVED)
                new_cols[a.name] = kt.value if kt != KustoType.UNRESOLVED else "unknown"
            scope.append(ScopeEntry(table=None, columns=new_cols))
            return
        if isinstance(op, SummarizeOp):
            out_cols: dict[str, str] = {}
            # KQL summarize emits grouping keys before aggregations in the output schema.
            for b in op.by:
                inner = getattr(b, "expr", b)
                self._fill(inner, scope)
                name = self._extract_target_name(b) or self._extract_target_name(inner)
                if name:
                    kt = getattr(inner, "result_type", KustoType.UNRESOLVED)
                    out_cols[name] = kt.value if kt != KustoType.UNRESOLVED else "unknown"
            for a in op.aggregations:
                self._fill(a.expr, scope)
                kt = getattr(a.expr, "result_type", KustoType.UNRESOLVED)
                out_cols[a.name] = kt.value if kt != KustoType.UNRESOLVED else "unknown"
            self._set_scope(scope, out_cols)
            return
        if isinstance(op, ProjectOp):
            current = self._scope_columns(scope)
            kept: dict[str, str] = {}
            for c in op.columns:
                self._fill(getattr(c, "expr", c), scope)
                if isinstance(c, Assignment):
                    kt = getattr(c.expr, "result_type", KustoType.UNRESOLVED)
                    kept[c.name] = kt.value if kt != KustoType.UNRESOLVED else "unknown"
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
            # Build {old: new} so we can rebuild the dict preserving original positions.
            rename_map: dict[str, str] = {}
            for c in op.columns:
                self._fill(c.expr, scope)
                old = self._extract_target_name(c.expr)
                if old and old in current:
                    rename_map[old] = c.name
            if rename_map:
                rebuilt: dict[str, str] = {}
                for k, v in current.items():
                    rebuilt[rename_map.get(k, k)] = v
                current = rebuilt
            self._set_scope(scope, current)
            return
        if isinstance(op, DistinctOp):
            # KQL: `distinct *` keeps the full scope; `distinct C1, C2` narrows
            # the output schema to the listed columns in listed order
            # (semantically equivalent to ``summarize by C1, C2``).
            from .expr import StarExpr

            has_star = any(
                isinstance(c, StarExpr) or isinstance(getattr(c, "expr", None), StarExpr)
                for c in op.columns
            )
            for c in op.columns:
                self._fill(getattr(c, "expr", c), scope)
            if has_star:
                return
            current = self._scope_columns(scope)
            kept: dict[str, str] = {}
            for c in op.columns:
                name = self._extract_target_name(c)
                if name and name in current:
                    kept[name] = current[name]
            self._set_scope(scope, kept)
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
            keep_names: set[str] = set()
            for c in op.columns:
                self._fill(getattr(c, "expr", c) if not isinstance(c, Expr) else c, scope)
                name = self._extract_target_name(c)
                if name:
                    keep_names.add(name)
            # KQL preserves source-table column order.
            kept = {k: v for k, v in current.items() if k in keep_names}
            self._set_scope(scope, kept)
            return
        if isinstance(op, ProjectReorderOp):
            # KQL emits listed columns first (in listed order), then remaining
            # columns in source order. Wildcards/asc/desc aren't reordered here —
            # they fall through unmodified.
            current = self._scope_columns(scope)
            listed: list[str] = []
            for c in op.columns:
                self._fill(getattr(c, "expr", c) if not isinstance(c, Expr) else c, scope)
                name = self._extract_target_name(c)
                if name and name in current and name not in listed:
                    listed.append(name)
            if listed:
                reordered: dict[str, str] = {n: current[n] for n in listed}
                for k, v in current.items():
                    if k not in reordered:
                        reordered[k] = v
                self._set_scope(scope, reordered)
            return
        if isinstance(op, ProjectByNamesOp):
            # Dynamic names — can't statically reshape scope.
            for n_expr in op.names:
                self._fill(n_expr, scope)
            return
        if isinstance(op, (ParseOp, ParseWhereOp)):
            self._fill(op.target, scope)
            new_cap: dict[str, str] = {}
            for p in op.patterns:
                self._fill(p, scope)
                if isinstance(p, ColumnRef):
                    kt = getattr(p, "result_type", KustoType.UNRESOLVED)
                    new_cap[p.name] = kt.value if kt != KustoType.UNRESOLVED else "string"
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
            # KQL make-series emits: by-keys, then aggregation series, then the
            # on-axis as a trailing dynamic column.
            out_cols2: dict[str, str] = {}
            for b in op.by:
                self._fill(b.expr, scope)
                kt = getattr(b.expr, "result_type", KustoType.UNRESOLVED)
                out_cols2[b.name] = kt.value if kt != KustoType.UNRESOLVED else "unknown"
            for a in op.aggregations:
                # make-series aggregates produce dynamic arrays.
                self._fill(a.expr, scope)
                out_cols2[a.name] = "dynamic"
            if op.on_column is not None:
                self._fill(op.on_column, scope)
                on_name = self._extract_target_name(op.on_column)
                if on_name:
                    out_cols2[on_name] = "dynamic"
            for attr in ("range_from", "range_to", "step"):
                e = getattr(op, attr, None)
                if e is not None:
                    self._fill(e, scope)
            self._set_scope(scope, out_cols2)
            return
        if isinstance(op, CountOp):
            # ``count`` discards the input schema for a single long column;
            # ``count as N`` names it.
            self._set_scope(scope, {op.as_name or "Count": KustoType.LONG.value})
            return

        # No bespoke scope rule for this operator kind. Fill provenance on
        # every expression it carries and walk every pipeline it nests,
        # leaving the scope unchanged.
        #
        # The branches above are the operators whose *output schema* we can
        # derive; there are 17 of them against 53 operator subclasses. Before
        # this fallback existed the function simply fell off the end for the
        # other 36, so `| sort by X` left X with no table while a `| project`
        # in the same query resolved fine. Filling without reshaping is the
        # honest position: correct for the operators that pass their schema
        # through (sort, top, take, search, the graph predicates), and for
        # the ones that do reshape (print, range, evaluate, facet, fork,
        # mv-apply, partition, parse-kv, serialize, top-nested) it leaves a
        # stale scope rather than an empty one — strictly closer than
        # skipping them, and visible here rather than silent.
        #
        # Sub-pipelines inherit the current scope: an mv-apply / partition /
        # fork / facet body has an implicit source and runs against the
        # enclosing row set.
        self._fill_children(op, scope, inherited=scope)

    def _resolve_column_table(self, name: str, scope: list[ScopeEntry]) -> str | None:
        # Most recently joined side wins on collisions (matches KQL binding).
        matches = [e.table for e in scope if e.table and name in e.columns]
        if not matches:
            return None
        return matches[-1]

    def _fill_children(
        self,
        node: object,
        scope: list[ScopeEntry],
        inherited: list[ScopeEntry] | None = None,
    ) -> None:
        """Fill every expression and pipeline ``node`` holds, at any depth.

        Derived from ``model_fields`` rather than a hardcoded tuple of
        attribute names. The tuple this replaced omitted ``pipeline``,
        ``branches`` and ``default``, so ``toscalar(...)`` subtrees and both
        arms of every ``case(...)`` went unvisited — the same column then
        resolved inside one operator and not inside another, silently.
        A hand-maintained list widens that hole every time the model grows a
        field; ``model_fields`` cannot drift.

        Note that adding ``"pipeline"`` to the old tuple would have changed
        nothing: it guarded on ``isinstance(child, Expr)`` and ``Pipeline``
        is not an ``Expr``. A nested pipeline needs ``_walk_pipeline``, which
        builds a scope from its own source.
        """
        for name in type(node).model_fields:  # type: ignore[attr-defined]
            for child in _models_in(getattr(node, name)):
                if isinstance(child, Pipeline):
                    self._walk_pipeline(child, inherited)
                elif isinstance(child, Expr):
                    self._fill(child, scope)
                else:
                    # Structural wrappers (Assignment, SortExpr, …) carry the
                    # expressions rather than being one.
                    self._fill_children(child, scope, inherited)

    def _fill(self, expr: Expr | None, scope: list[ScopeEntry]) -> None:
        if expr is None:
            return
        self._fill_children(expr, scope)

        if isinstance(expr, ColumnRef):
            if expr.table == "$left" and len(scope) >= 2:
                expr.table = scope[-2].table or expr.table
            elif expr.table == "$right" and len(scope) >= 1:
                expr.table = scope[-1].table or expr.table
            if expr.table is None:
                resolved = self._resolve_column_table(expr.name, scope)
                if resolved:
                    expr.table = resolved
            if expr.result_type == KustoType.UNRESOLVED and expr.table:
                t = self._table_schema(expr.table).get(expr.name)
                if t:
                    try:
                        expr.result_type = KustoType(t)
                    except ValueError:
                        pass
            return

        if expr.result_type != KustoType.UNRESOLVED:
            return

        if isinstance(expr, LiteralExpr):
            try:
                expr.result_type = KustoType(expr.literal_kind)
            except ValueError:
                pass
        elif isinstance(expr, (BinOp, SetMembership, Between, And, Or, Not)):
            expr.result_type = KustoType.BOOL


BinderEnricher = SchemaAttacher
