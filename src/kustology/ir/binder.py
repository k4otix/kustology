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
    TypedNameDecl,
)
from .query import (
    Assignment,
    CountOp,
    DataTableSource,
    DistinctOp,
    ExtendOp,
    ExternalDataSource,
    FilterOp,
    JoinOp,
    LetRef,
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

# Join kinds that emit one side's columns only. ``anti``/``leftantisemi`` and
# ``rightantisemi`` are Microsoft's own aliases -- the parser lists all twelve
# spellings in its KS005 message -- and each pair returns the same schema, so
# they are grouped by the side that survives rather than by name.
_LEFT_ONLY_JOIN_KINDS = frozenset({
    "anti", "leftanti", "leftantisemi", "leftsemi",
})
_RIGHT_ONLY_JOIN_KINDS = frozenset({
    "rightanti", "rightantisemi", "rightsemi",
})


def _join_kind(text: str | None) -> str:
    """Normalize ``JoinOp.join_kind`` for matching.

    ``join_kind`` records the text the query wrote, and a bare ``join`` is
    recorded as ``innerunique`` -- KQL's effective default, and the one this
    falls back to for an empty value so a hand-built ``JoinOp`` cannot land
    on "no rule matched, therefore widen".

    Case is folded, which is *wider* than Microsoft's parser: it rejects
    ``kind=LeftAnti`` with KS005 rather than accepting it. Folding cannot
    change the answer for a valid query, and for an IR built or edited
    directly it avoids reading an anti join as a widening one.
    """
    return (text or "innerunique").strip().lower()


@dataclass
class ScopeEntry:
    """One source visible at a point in the pipeline.

    Joins, lookups, and unions append entries; project / summarize replace
    them with a synthesized anonymous entry (``table=None``).

    ``origins`` is what keeps provenance alive across that replacement. The
    anonymous entry has no ``table``, so before it existed every column
    reference *after* a ``project`` reported ``table=None`` while the same
    column before it reported the real table — one query, two answers for one
    column, and any lineage consumer reading ``ColumnRef.table`` silently got
    the wrong one. ``origins`` maps a column name to the table it came from,
    which is not always ``entry.table``: a projected column keeps ``"T"``
    while living in a table-less entry, and a computed one (``extend n =
    a + 1``) maps to ``None`` explicitly, so "invented here" is recorded
    rather than inferred from a missing key.

    A name absent from ``origins`` falls back to ``table`` — the ordinary
    case of a real source table, where restating every column would be noise.
    """

    table: str | None
    columns: dict[str, str] = field(default_factory=dict)
    origins: dict[str, str | None] = field(default_factory=dict)

    def origin_of(self, name: str) -> str | None:
        """The table ``name`` came from, or None if unknown/invented."""
        if name in self.origins:
            return self.origins[name]
        return self.table


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
        # {let name: {column: type}} for the enrich() call in progress.
        # Reset per call, not per instance: a reused attacher must not carry
        # one query's binding names into the next.
        self._let_schemas: dict[str, dict[str, str]] = {}

    def enrich(self, ir: QueryIR) -> QueryIR:
        """Enrich the whole IR in place and mark it attached.

        Tabular ``let`` bindings are walked first, in declaration order, and
        each one's output columns are registered under its name. A later
        binding or the main pipeline reading that name through a
        :class:`LetRef` then resolves against those columns, so

            let Base = SecurityEvent | where EventID > 4624;
            Base | project Account

        gives ``Account`` the type ``string`` and the provenance ``"Base"``
        rather than leaving both unresolved.

        Two boundaries remain, and are boundaries rather than bugs:

        * A binding naming one declared *later* is not a ``LetRef`` at all
          (see :class:`LetRef`), so there is nothing to thread — it stays an
          opaque table.
        * ``let``-declared functions are recorded, not expanded, so a call
          site does not acquire the body's schema.
        * An alias may shadow a real table name -- ``let SecurityEvent =
          SecurityEvent | where …`` is a common Sentinel idiom -- so
          ``ColumnRef.table`` alone cannot say which namespace its string
          came from.

        Shadowing does not mislead the binder: types come from the binding's
        own output schema, so ``let SecurityEvent = Other | …`` gives columns
        read through the alias ``Other``'s types, not the real
        ``SecurityEvent``'s. Read ``result_type`` and the question does not
        arise. A consumer that instead re-derives types from the name
        (``my_schema[col.table][col.name]``) does need to tell the two apart,
        and *position* is what does it exactly -- a reference inside a
        binding's body reads real tables, one outside resolves against
        whatever the ``LetRef`` brought in::

            inner = {id(c) for b in ir.let_bindings if b.rhs_pipeline
                     for c in find_all(b.rhs_pipeline, ColumnRef)}
            aliases = {b.name for b in ir.let_bindings}
            is_alias = id(col) not in inner and col.table in aliases

        Matching on ``aliases`` alone is not enough: under self-shadowing it
        also flags the binding body's own columns, which read the table.
        """
        self._let_schemas = {}
        for binding in ir.let_bindings:
            if binding.rhs_pipeline is None:
                continue
            self._walk_pipeline(binding.rhs_pipeline)
            schema = binding.rhs_pipeline.result_schema
            if schema is not None:
                self._let_schemas[binding.name] = dict(schema.columns)
        self._walk_pipeline(ir.main_pipeline)
        # Later tabular statements are pipelines like any other; skipping them
        # would leave the same column resolved in statement one and unresolved
        # in statement two.
        for pipeline in ir.additional_pipelines:
            self._walk_pipeline(pipeline)
        ir.schema_attached = True
        return ir

    def _table_schema(self, name: str | None) -> dict[str, str]:
        if not name:
            return {}
        return self.schemas.get(name, {})

    def _source_entry(self, pipeline: Pipeline) -> ScopeEntry:
        """The scope a pipeline starts from, derived from its source.

        Schema lookups are keyed on the **bare** table name, and stay that
        way for a qualified source: ``database('d').T`` resolves against
        ``schemas["T"]``. The qualifiers are recorded on the ``TableRef``
        for consumers that care about provenance, but adopting a ``"d.T"``
        key convention here would silently stop resolving every qualified
        query written against a schema dict keyed the ordinary way.
        """
        source = pipeline.source
        if isinstance(source, (DataTableSource, ExternalDataSource)):
            # These declare their own schema inline, so no lookup is needed
            # -- and none would succeed, since neither has a table name.
            return ScopeEntry(table=None, columns=dict(source.columns))
        if isinstance(source, Pipeline):
            # ``materialize(P) | …`` nests a whole pipeline in source
            # position. Walk it so its own operators shape the scope the
            # outer pipeline starts from; returning an empty anonymous
            # entry left everything downstream unresolvable.
            self._walk_pipeline(source)
            columns = dict(source.result_schema.columns) if source.result_schema else {}
            return ScopeEntry(table=None, columns=columns)
        if isinstance(source, LetRef):
            # A let alias carries the binding's output columns, and keeps its
            # own name as provenance -- the alias *is* what the pipeline
            # reads, and reporting the underlying table would lose the step
            # the query actually wrote.
            columns = self._let_schemas.get(source.name)
            if columns is not None:
                return ScopeEntry(table=source.name, columns=dict(columns))
            return ScopeEntry(table=None, columns={})
        if isinstance(source, TableRef) and source.is_wildcard:
            # ``union T*`` names a *set* of tables. Resolving it against a
            # schema entry literally called ``T*`` would be a coincidence,
            # and picking one member of the set would be a guess.
            return ScopeEntry(table=None, columns={})
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
        #
        # The last operator's own ``result_schema`` is preferred when it has
        # one, and not merely as an optimization: it is Microsoft's answer
        # *in Microsoft's column order*, and the merge below cannot
        # reproduce that order. ``ScopeEntry`` groups columns by originating
        # table so provenance survives, which means merging them re-orders a
        # join's output by side rather than by the order the engine emits.
        #
        # With no operators there is nothing to read it off, and the value
        # the builder left on the pipeline -- Microsoft's reading of the
        # source -- stands in. When the builder had no answer either, that
        # slot holds whatever a previous walk of this pipeline computed,
        # which is the same merge this one would redo.
        authoritative = (
            pipeline.operators[-1].result_schema if pipeline.operators
            else pipeline.result_schema
        )
        if authoritative is not None:
            pipeline.result_schema = TabularSchema(
                columns=dict(authoritative.columns)
            )
        else:
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

    def _column_origins(self, scope: list[ScopeEntry]) -> dict[str, str | None]:
        """``{column: originating table}`` over the whole scope.

        A name carried by two entries that disagree about where it came from
        is **ambiguous**, and maps to ``None``. That is KQL's own answer:
        after ``T | union U`` an unqualified ``k`` is neither table's ``k``,
        and the previous rule -- "the most recently appended side wins" --
        was a guess dressed as provenance. Join collisions do not reach this
        branch, because the join rule renames the right side's ``k`` to
        ``k1`` before the scope ever holds both.
        """
        seen: dict[str, str | None] = {}
        ambiguous: set[str] = set()
        for entry in scope:
            for name in entry.columns:
                origin = entry.origin_of(name)
                if name in seen and seen[name] != origin:
                    ambiguous.add(name)
                seen[name] = origin
        for name in ambiguous:
            seen[name] = None
        return seen

    def _set_scope(
        self,
        scope: list[ScopeEntry],
        columns: dict[str, str],
        origins: dict[str, str | None] | None = None,
    ) -> None:
        """Replace scope with a single anonymous entry containing `columns`.

        Provenance is carried over by default: a column the replaced scope
        knew survives ``project`` / ``distinct`` / ``project-keep`` under the
        same name, so it keeps the table it had. Pass ``origins`` explicitly
        where the operator renames (``project-rename``).
        """
        if origins is None:
            carried = self._column_origins(scope)
            origins = {n: carried[n] for n in columns if n in carried}
        scope.clear()
        scope.append(ScopeEntry(table=None, columns=columns, origins=origins))

    def _extract_target_name(self, expr) -> str | None:
        """Pull a bare column name from a ColumnRef / Assignment / similar."""
        if isinstance(expr, ColumnRef):
            return expr.name
        name = getattr(expr, "name", None)
        if isinstance(name, str):
            return name
        return None

    def _overlay_result_schema(
        self, columns: dict[str, str], scope: list[ScopeEntry],
    ) -> None:
        """Rewrite ``scope`` to carry exactly ``columns``, keeping provenance.

        Microsoft's ``ResultType`` says which columns exist after an operator
        and what they are typed; it says nothing about which table each one
        came from, and ``ColumnRef.table`` is the one thing in the IR that
        only this walk can supply. So the two are combined rather than one
        replacing the other: each column is filed under the table the
        *pre-operator* scope had it under, and anything new (a summarize
        aggregate, a join's suffixed ``Foo1``) lands in the anonymous entry.

        The table order of the incoming scope is preserved, because
        :meth:`_resolve_column_table` reads it as precedence -- most recently
        joined side wins a name collision, which is KQL's own rule.

        Dropping to ``_set_scope`` here instead (one anonymous entry with
        Microsoft's columns) would be simpler and would silently delete
        provenance for every operator the binder can type: ``T | where a > 1
        | project a`` would stop reporting ``a.table == "T"``.
        """
        origin = self._column_origins(scope)
        tables: list[str | None] = []
        for entry in scope:
            if entry.table not in tables:
                tables.append(entry.table)
        if None not in tables:
            tables.append(None)
        buckets: dict[str | None, dict[str, str]] = {t: {} for t in tables}
        # A column whose origin is a table no *entry* is keyed on -- one that
        # a ``project`` already moved into the anonymous entry -- goes to the
        # anonymous bucket and keeps its provenance in ``origins``. Adding a
        # fresh table entry for it instead would reorder the merged scope,
        # which ``project-keep`` / ``project-away`` read as column order.
        anon_origins: dict[str, str | None] = {}
        for name, type_name in columns.items():
            table = origin.get(name)
            if table in buckets:
                buckets[table][name] = type_name
            else:
                buckets[None][name] = type_name
                anon_origins[name] = table
        scope.clear()
        for table in tables:
            scope.append(ScopeEntry(
                table=table,
                columns=buckets[table],
                origins=anon_origins if table is None else {},
            ))

    def _walk_operator(self, op: Operator, scope: list[ScopeEntry]) -> None:
        if op.result_schema is not None:
            # Microsoft already computed this operator's output. Its answer
            # is authoritative for *names and types*; provenance is still
            # ours, so the expressions are filled first and the scope is
            # then overlaid rather than replaced.
            #
            # ``join`` / ``lookup`` / ``union`` keep their hand-rolled rule
            # even here, for two things the overlay cannot recover: ``on``
            # clauses resolve against a scope that includes the right side
            # (``$left`` / ``$right`` depend on it), and the per-side
            # ``ScopeEntry`` it appends is what gives a right-hand column a
            # table at all. The overlay then corrects the names and types
            # that rule guesses at -- which is the whole point of the task.
            if isinstance(op, (JoinOp, LookupOp, UnionOp)):
                self._walk_operator_rules(op, scope)
            else:
                self._fill_children(op, scope, inherited=scope)
            self._overlay_result_schema(dict(op.result_schema.columns), scope)
            return
        self._walk_operator_rules(op, scope)

    def _walk_operator_rules(self, op: Operator, scope: list[ScopeEntry]) -> None:
        """The hand-rolled per-operator scope rules.

        Runs when Microsoft did not answer for this operator — an unbound
        parse, or a schema the binder could not fully determine — and, for
        the multi-source operators, alongside the answer when it did. See
        :meth:`_walk_operator`.
        """
        if isinstance(op, FilterOp):
            self._fill(op.predicate, scope)
            return
        if isinstance(op, (JoinOp, LookupOp)):
            rhs_scope = self._walk_pipeline(op.right)
            on_scope = scope + rhs_scope[:1]
            for e in op.on:
                self._fill(e, on_scope)
            if isinstance(op, JoinOp):
                # A semi or anti join is a *filter*, not a widening: it emits
                # rows of one side and columns of one side. ``lookup`` has no
                # such kind (only ``leftouter`` / ``inner``), which is why the
                # check is on ``JoinOp`` rather than shared.
                kind = _join_kind(op.join_kind)
                if kind in _LEFT_ONLY_JOIN_KINDS:
                    return
                if kind in _RIGHT_ONLY_JOIN_KINDS:
                    kept = [
                        ScopeEntry(
                            table=e.table,
                            columns=dict(e.columns),
                            origins=dict(e.origins),
                        )
                        for e in rhs_scope[:1]
                    ]
                    scope.clear()
                    scope.extend(kept)
                    return
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
                renamed_origins: dict[str, str | None] = {}
                for name, kt in entry.columns.items():
                    if name in drop_keys:
                        continue
                    if name not in left_names:
                        renamed[name] = kt
                        renamed_origins[name] = entry.origin_of(name)
                        left_names.add(name)
                        continue
                    n = 1
                    candidate = f"{name}{n}"
                    while candidate in left_names:
                        n += 1
                        candidate = f"{name}{n}"
                    renamed[candidate] = kt
                    # ``shared1`` is the right side's ``shared``, so it keeps
                    # the right side's provenance under the new name.
                    renamed_origins[candidate] = entry.origin_of(name)
                    left_names.add(candidate)
                scope.append(ScopeEntry(
                    table=entry.table, columns=renamed, origins=renamed_origins,
                ))
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
            # A renamed column is still the source table's column, so its
            # provenance is carried under the *new* name -- ``_set_scope``'s
            # default carry works by name and would drop it.
            carried = self._column_origins(scope)
            origins = {rename_map.get(k, k): v for k, v in carried.items()}
            if rename_map:
                rebuilt: dict[str, str] = {}
                for k, v in current.items():
                    rebuilt[rename_map.get(k, k)] = v
                current = rebuilt
            self._set_scope(scope, current, origins)
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
            # columns in source order. A wildcard term contributes no name to
            # ``listed`` (``*`` and ``a*`` are not columns in ``current``), so
            # the columns it matches keep their source order — which is also
            # why the term's ``asc``/``desc``, being a rule for ordering those
            # matches, is not modelled in the scope here.
            #
            # ``columns`` is uniformly ``list[ReorderKey]``, so the expression
            # is reached through ``.expression`` rather than the old
            # ``getattr(c, "expr", c)`` guess. That guess is what would break
            # here: ``ReorderKey`` has no ``expr``, so it would have handed
            # the wrapper itself to ``_fill``, which reads ``result_type`` off
            # an ``Expr``.
            current = self._scope_columns(scope)
            listed: list[str] = []
            for c in op.columns:
                self._fill(c.expression, scope)
                name = self._extract_target_name(c.expression)
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
                if isinstance(p, TypedNameDecl):
                    # ``parse a with 'x' b:long`` states the capture's type,
                    # so there is nothing to infer. Before typed captures had
                    # their own node they arrived as a bare ``ColumnRef`` and
                    # fell into the ``"string"`` default below -- the type the
                    # query wrote was not merely unused, it was unreadable.
                    new_cap[p.name] = p.declared_type
                    continue
                if isinstance(p, ColumnRef):
                    kt = getattr(p, "result_type", KustoType.UNRESOLVED)
                    new_cap[p.name] = kt.value if kt != KustoType.UNRESOLVED else "string"
            if new_cap:
                scope.append(ScopeEntry(table=None, columns=new_cap))
            return
        if isinstance(op, MvExpandOp):
            current = self._scope_columns(scope)
            for col in op.columns:
                # ``col`` is an ``MvExpandColumn`` wrapper, not the
                # expression: it carries the ``to typeof(...)`` the query
                # wrote. Handing the wrapper to ``_fill`` would reach for
                # ``result_type`` on a node that has none.
                c = col.expression
                self._fill(c, scope)
                name = self._extract_target_name(c)
                if not name:
                    continue
                if col.to_typeof:
                    # The query states the expanded element's type, so there
                    # is nothing to infer from the binder's dynamic<T>.
                    current[name] = col.to_typeof
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
        # ``_column_origins`` reads ``ScopeEntry.origins`` as well as
        # ``table``, so a column a ``project`` moved into the anonymous entry
        # still resolves, and a name two entries disagree about resolves to
        # None rather than to whichever side was appended last.
        return self._column_origins(scope).get(name)

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
