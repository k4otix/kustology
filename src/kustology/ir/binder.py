# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Provenance pass over an already-built IR.

Fills ``result_type`` on expressions the .NET binder couldn't resolve and
attaches ``table`` provenance to ``ColumnRef`` nodes by walking the pipeline
with a growing scope. ``parse(query, schema=...)`` and
``to_ir(attach_schema=...)`` construct and drive it; a caller never builds
:class:`SchemaAttacher` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._builder_helpers import ARITHMETIC_OPS
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
    DataTableSource,
    ExternalDataSource,
    FindOp,
    JoinOp,
    LetBinding,
    LetFunctionParameter,
    LetRef,
    LookupOp,
    Operator,
    PatternStmt,
    Pipeline,
    ProjectRenameOp,
    QueryIR,
    SearchOp,
    TableRef,
    TabularSchema,
    UnionOp,
    UnknownSource,
)
from .types import KustoType
from .walk import _models_in, find_all


def _renamed_columns(op: Operator) -> dict[str, str]:
    """Map new to old column names for a ``project-rename``; return ``{}`` otherwise.

    ``project-rename kk = k`` is the one operator that states in the query
    itself that an output column *is* an input column renamed. The overlay
    files each column under the table the pre-operator scope had it under,
    and no scope entry holds ``kk``, so without this the rename loses ``T``.

    A term whose right-hand side is not a plain column is skipped: only a
    ``ColumnRef`` names an existing column, and anything else (a hand-built
    IR, a shape the builder did not model) has no input name to carry
    provenance from.
    """
    if not isinstance(op, ProjectRenameOp):
        return {}
    return {
        c.name: c.expr.name
        for c in op.columns
        if isinstance(c.expr, ColumnRef)
    }


def _flatten_side(entries: list[ScopeEntry]) -> ScopeEntry:
    """Collapse a join's right-hand scope into the one row set it is.

    A join has exactly one right side, whatever pipeline produced it:
    ``(R)`` leaves one entry, ``(union A, B)`` leaves one per arm behind the
    empty entry its implicit source produces. ``rhs_scope[:1]`` would pick
    that empty entry, leaving ``$right.k`` no side to resolve against, and
    appending every entry would make one row set look like several to
    ``_resolve_side``, which reads the side's entry count.

    The merged entry keeps a table when every contributing entry names the
    same one, and otherwise records provenance per column in ``origins``, so
    a right-hand ``R | project b`` still reports ``b`` as ``R``'s.

    A contributing entry counts by ``table`` alone. A named table this walk
    has no schema for still names the side unambiguously, since flattening
    leaves only one contributor, so also requiring known columns would leave
    an honest ``$right.x`` unresolved.
    """
    columns: dict[str, str] = {}
    origins: dict[str, str | None] = {}
    tables: set[str] = set()
    for entry in entries:
        if entry.table:
            tables.add(entry.table)
        for name, kind in entry.columns.items():
            if name in columns:
                continue
            columns[name] = kind
            origins[name] = entry.origin_of(name)
    return ScopeEntry(
        table=next(iter(tables)) if len(tables) == 1 else None,
        columns=columns,
        origins=origins,
    )


@dataclass
class ScopeEntry:
    """One source visible at a point in the pipeline.

    Joins, lookups, unions and searches append entries; the overlay of
    Microsoft's ``ResultType`` re-files them, and anything it cannot place
    under a table it saw lands in a synthesized anonymous entry
    (``table=None``).

    ``origins`` keeps provenance alive across that re-filing. The anonymous
    entry has no ``table``, so without ``origins`` a column reference *after*
    a ``project`` reports ``table=None`` while the same column before it
    reports the real table, and a lineage consumer reading ``ColumnRef.table``
    silently gets one of two answers for one column. ``origins`` maps a column
    name to the table it came from, which is not always ``entry.table``: a
    projected column keeps ``"T"`` while living in a table-less entry, and a
    computed one (``extend n = a + 1``) maps to ``None``, recording "invented
    here" rather than leaving it inferred from a missing key.

    A name absent from ``origins`` falls back to ``table``, the ordinary case
    of a real source table.
    """

    table: str | None
    columns: dict[str, str] = field(default_factory=dict)
    origins: dict[str, str | None] = field(default_factory=dict)

    def origin_of(self, name: str) -> str | None:
        """Return the table ``name`` came from, or ``None`` if unknown or invented."""
        if name in self.origins:
            return self.origins[name]
        return self.table


class SchemaAttacher:
    """The provenance pass; every schema it publishes is Microsoft's.

    ``schemas`` is a flat ``{table_name: {column_name: kusto_type_string}}``.
    It seeds the walk's *starting* scope, which columns a source table brings
    in so a reference can be placed, and types a reference the binder left
    unresolved. Tables not present are opaque.

    ``Operator.result_schema`` carries Microsoft's own ``ResultType``,
    stamped by the builder wherever the binder closed the symbol, and it is
    the only thing this class publishes as a schema. It re-derives no
    operator output: no ``project`` narrowing, no join-collision suffixes, no
    ``arg_max(t, *)`` expansion, no union type-conflict splitting. A
    hand-derived rule for any of those disagrees with the engine sooner or
    later.

    * ``result_schema`` present: Microsoft's names, types and column order,
      overlaid onto the walked scope so provenance survives (see
      :meth:`_overlay_result_schema`).
    * ``result_schema`` absent: the symbol is open. The scope passes through
      un-reshaped, downstream references still resolve against the last known
      shape (stale wherever the operator really reshaped, and visibly so),
      and the pipeline reports ``result_schema=None``.

    ``None`` and ``TabularSchema(columns={})`` are different claims and both
    are reachable: ``None`` is "not determined", ``{}`` is "determined, and
    it really emits nothing" (a bound ``T | project-away *``). Only Microsoft
    says the second. Closure is per node rather than per query: ``T | count``
    and ``T | getschema`` close mid-pipeline over a table nobody described,
    because their output does not depend on their input, and a ``datatable``
    root is closed with no schema dict at all.

    Nothing else in the IR supplies:

    * ``ColumnRef.table``, which table each reference came from.
      ``ResultType`` does not carry it.
    * ``ScopeEntry.origins``, the same fact per column, across the operators
      that re-file the scope.
    * ``$left`` / ``$right`` resolution inside a join's ``on`` clause, and
      the bare-key left-first rule.
    * ``let`` threading: a tabular binding's output columns registered under
      its name, so ``Base | project Account`` resolves.
    * ``Expr.result_type`` backfill for the exactly-knowable cases: a
      literal's own kind, a comparison's ``bool``. Arithmetic stays
      unresolved.

    Each operator gets its provenance filled, then the overlay. The four
    constructs that bring *sources* into scope (``join``/``lookup``,
    ``union``, ``search``/``find``) get a structural branch, because the
    overlay cannot recover where a column came from once the sources are
    forgotten; see :meth:`_walk_operator_provenance`.

    Three narrowings are accepted, each a loss of provenance rather than of a
    column. In each the walk cannot say which source a name belongs to, so it
    answers ``None``. An unplaceable ``$left``/``$right`` reference lands
    here too, since ``ColumnRef.table`` cannot hold ``"$left"`` /
    ``"$right"`` at all.

    * A post-join collision pair, ``shared`` and Microsoft's ``shared1``.
      Both sides are in scope with a ``shared`` of their own, and neither of
      Microsoft's two output names is one an entry holds, so the unqualified
      names are genuinely ambiguous. A qualified ``$left.shared`` /
      ``$right.shared`` in the ``on`` clause still keeps its side.
    * A union type-conflict's split variants, ``a_long`` / ``a_string``.
      Neither arm carries a column under either name.
    * A ``search``'s or ``find``'s seeded tables, where the operator is not
      the first thing in its pipeline. The entries are *appended* to whatever
      scope the operator inherited, so a column both the inherited scope and
      a searched or found table carry reads as ambiguous:
      ``T | partition by k (search in (U) a > 1)`` answers ``None`` for ``a``
      where the search alone answers ``U``. Replacing the scope would be a
      claim about the operator's output, which is Microsoft's to make.

    ``project-rename`` is not in that list. Its target is a name no scope
    entry holds, but the query states outright which input column it renames,
    so :func:`_renamed_columns` threads that through and the provenance
    survives. What separates the two is whether the fact is written down
    anywhere.

    ``tests/ir/test_binder_oracle.py`` compares ``result_schema`` against
    ``ResultType`` over an operator matrix and the corpus, on both entry
    points, and is what keeps the overlay honest.
    """

    def __init__(self, schemas: dict[str, dict[str, str]] | None = None):
        """Seed the walk's starting scope with ``{table_name: {column_name: kusto_type_string}}``."""
        self.schemas: dict[str, dict[str, str]] = dict(schemas or {})
        # {let name: {column: type}} for the enrich() call in progress. Reset
        # per call, so a reused attacher cannot carry one query's binding
        # names into the next.
        self._let_schemas: dict[str, dict[str, str]] = {}
        # Parameter names masked for the length of the let-function or
        # pattern-arm body being walked, shadowing any same-named real table
        # the way the builder's ``_param_names`` does for naming (see
        # ``_walk_function_body``). ``_table_schema`` answers ``{}`` for a
        # masked name, so a tabular parameter's ``TableRef`` resolves to an
        # honestly-empty scope instead of a same-named caller table's
        # columns. Reset per call, and saved and restored around each body.
        self._masked_tables: set[str] = set()
        # (left entries, right entries) while filling a join's ``on`` clause,
        # None everywhere else. Saved and restored around the fill and around
        # every nested pipeline walk, so a join inside an ``on`` clause
        # cannot leak its sides to the enclosing one or the other way round.
        self._join_sides: tuple[list[ScopeEntry], list[ScopeEntry]] | None = None
        # {id(pipeline): the schema the *builder* left on it}, snapshotted
        # at ``enrich`` entry. Empty outside a call.
        self._builder_schemas: dict[int, TabularSchema | None] = {}

    def enrich(self, ir: QueryIR) -> QueryIR:
        """Enrich the whole IR in place and mark it attached.

        Tabular ``let`` bindings are walked first, in declaration order, and
        each one's output columns are registered under its name. A later
        binding or the main pipeline reading that name through a
        :class:`LetRef` then resolves against those columns, so

            let Base = SecurityEvent | where EventID > 4624;
            Base | project Account

        gives ``Account`` the type ``string`` and the provenance ``"Base"``.

        A let-function's and a pattern arm's bodies are walked too, each as
        its own scope (:meth:`_walk_function_body`): every parameter name is
        masked for the length of the body, so a same-named real table's
        columns cannot leak into what is really a parameter reference, and a
        tabular body ``let`` threads into the tail the same way a top-level
        one threads into the main pipeline. ``find_all(ir, Pipeline)`` reaches
        these nested pipelines, so the ``_builder_schemas`` snapshot below
        covers them as well.

        Four boundaries remain:

        * A binding naming one declared *later* is not a ``LetRef`` at all
          (see :class:`LetRef`), so there is nothing to thread and it stays
          an opaque table.
        * A call site (``f(1)``, ``P("x")``) acquires nothing from the body
          it calls: ``let``-declared functions and ``declare pattern`` arms
          are recorded, not expanded, so the body is walked once from the
          declaration and never again from where it is invoked. A pattern arm
          has no schema of its own to give a call site even in principle,
          since it runs under the values that call site supplies.
        * A scalar parameter is not told apart from a same-named column of a
          table the body reads: the builder lowers both to the identical
          ``ColumnRef(name=...)`` (see :class:`~kustology.ir.query.LetFunctionParameter`),
          so a reference meant as the parameter can resolve against the
          table's real column and report a table it never came from. Masking
          closes this for a *table* name, so a tabular parameter's own
          ``TableRef`` never resolves to a caller table. A scalar parameter
          shadowing a *column* is a narrower gap this pass does not close.
        * An alias may shadow a real table name -- ``let SecurityEvent =
          SecurityEvent | where …`` is a common Sentinel idiom -- so
          ``ColumnRef.table`` alone cannot say which namespace its string
          came from.

        The other four statement kinds ``QueryIR.statements`` can hold --
        ``set``, ``declare query_parameters``, ``alias database``,
        ``restrict access to`` -- are not walked at all; whatever a bound
        parse left on their expressions is what stays. ``ir.schema_attached``
        reads across the whole IR, statements included, since it asks whether
        anything in this IR came from somewhere real.

        Shadowing does not mislead the binder: types come from the binding's
        own output schema, so ``let SecurityEvent = Other | …`` gives columns
        read through the alias ``Other``'s types. Read ``result_type`` and
        the question does not arise. A consumer that re-derives types from
        the name (``my_schema[col.table][col.name]``) does need to tell the
        two apart, and *position* does it exactly: a reference inside a
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
        self._masked_tables = set()
        # A pipeline with no operators has nothing to read its output shape
        # off, so ``_walk_pipeline`` falls back to ``Pipeline.result_schema``,
        # the builder's record of what Microsoft made of the source. That
        # method also *writes* the field, so snapshot it before walking, and
        # snapshot unconditionally: what goes back is a copy of an
        # authoritative value or ``None``, and skipping the snapshot on an IR
        # that already has ``schema_attached`` set would wipe every
        # operator-less pipeline's schema to ``None``.
        self._builder_schemas = {
            id(p): p.result_schema for p in find_all(ir, Pipeline)
        }
        try:
            for binding in ir.let_bindings:
                if binding.rhs_pipeline is not None:
                    self._walk_pipeline(binding.rhs_pipeline)
                    schema = binding.rhs_pipeline.result_schema
                    if schema is not None:
                        self._let_schemas[binding.name] = dict(schema.columns)
                elif binding.rhs_function is not None:
                    self._walk_function_body(
                        binding.rhs_function.parameters,
                        binding.rhs_function.body_lets,
                        binding.rhs_function.body_pipeline,
                    )
                # A scalar binding (``rhs_expr``) carries no output schema to
                # thread and nothing here resolves against it by name.
            for stmt in ir.statements:
                if isinstance(stmt, PatternStmt):
                    for match in stmt.matches:
                        self._walk_function_body(
                            [], match.body_lets, match.body_pipeline,
                        )
            self._walk_pipeline(ir.main_pipeline)
            # Later tabular statements are pipelines like any other. Skipping
            # them leaves a column resolved in one statement and unresolved
            # in the next.
            for pipeline in ir.additional_pipelines:
                self._walk_pipeline(pipeline)
        finally:
            # A direct ``_walk_pipeline`` call after this one is not part of
            # any enrich, and must not consult a snapshot taken for one.
            self._builder_schemas = {}
        # ``schema_attached`` claims the types in this IR came from somewhere
        # real. Either source counts: the dict handed to this attacher, or
        # the binder's own per-operator answer, which a bound parse leaves on
        # the tree.
        ir.schema_attached = bool(self.schemas) or any(
            op.result_schema is not None for op in find_all(ir, Operator)
        )
        return ir

    def _walk_function_body(
        self,
        parameters: list[LetFunctionParameter],
        body_lets: list[LetBinding],
        body_pipeline: Pipeline | None,
    ) -> None:
        """Walk a let-function's or pattern arm's body as its own scope.

        Mirrors the builder's own body-scope pattern
        (``IRBuilder._visit_function_body``). Every parameter name is masked
        for the length of the body, so a same-named real table cannot leak
        its columns into a reference that is really the parameter (see
        ``_table_schema``). A tabular body ``let`` is walked and its output
        columns registered under its name exactly as a top-level tabular
        binding is, so a later body ``let`` or the tail resolves against it.
        Both ``self._let_schemas`` and ``self._masked_tables`` are restored
        in ``finally``, so nothing this call discovers or masks survives past
        the closing brace, an exception included.

        ``body_lets`` gets the same three-way dispatch :meth:`enrich`'s own
        let-binding loop does -- tabular threads, a nested function binding
        recurses through this method, scalar is skipped -- because a ``let``
        written inside a function body can itself be a ``FunctionDeclaration``
        (``let outer = (x:long) { let inner = (y:long) { ... }; ... }``). The
        recursive call unions its own mask onto this call's (mirroring the
        builder's ``saved_params | shadowed``), so a name ``outer``'s
        parameter shadows stays shadowed while ``inner`` runs.

        ``parameters`` is empty for a pattern arm: a pattern's own parameters
        are matched against the arm's *values* and are not bound as names
        inside the body (see :class:`~kustology.ir.query.PatternStmt`), so it
        has nothing of its own to mask.

        A scalar body ``let`` (``rhs_expr``) is skipped, as a top-level
        scalar binding is in :meth:`enrich`: its expression is not walked and
        its name resolves against nothing.
        """
        saved_let_schemas = dict(self._let_schemas)
        saved_masked = self._masked_tables
        self._masked_tables = saved_masked | {p.decl.name for p in parameters}
        try:
            for binding in body_lets:
                if binding.rhs_pipeline is not None:
                    self._walk_pipeline(binding.rhs_pipeline)
                    schema = binding.rhs_pipeline.result_schema
                    if schema is not None:
                        self._let_schemas[binding.name] = dict(schema.columns)
                elif binding.rhs_function is not None:
                    self._walk_function_body(
                        binding.rhs_function.parameters,
                        binding.rhs_function.body_lets,
                        binding.rhs_function.body_pipeline,
                    )
                # A scalar body `let` (`rhs_expr`) carries no output schema
                # to thread and nothing here resolves against it by name.
            if body_pipeline is not None:
                self._walk_pipeline(body_pipeline)
        finally:
            self._let_schemas = saved_let_schemas
            self._masked_tables = saved_masked

    def _table_schema(self, name: str | None) -> dict[str, str]:
        if not name or name in self._masked_tables:
            return {}
        return self.schemas.get(name, {})

    def _entry_table(self, name: str | None) -> str | None:
        """Return the label a bare-name :class:`ScopeEntry` is honestly entitled to.

        A masked name gets ``None``. ``_table_schema`` already answers ``{}``
        for one, but a ``ScopeEntry`` built straight from a name
        (``_source_entry``'s plain-``TableRef`` fallback, ``search``/``find``'s
        table seeding) would still write that name into ``table``, and the
        label is not harmless: ``_resolve_side``'s single-entry fallback reads
        ``entries[0].table`` back out, and a join's ``on`` clause is where a
        single masked entry is the whole side. Emptying it here covers both
        call sites without teaching that fallback about masking.
        """
        return None if name in self._masked_tables else name

    def _let_alias_entry(self, name: str) -> ScopeEntry:
        """Return the scope entry a ``let`` alias is honestly entitled to.

        Shared by ``_source_entry``'s ``LetRef`` branch (a pipeline's own
        source) and ``search``/``find``'s table seeding, which both name a
        ``let`` alias rather than a real table. The alias belongs in ``table``
        when ``_let_schemas`` has its columns (a tabular binding the walk
        closed), since the alias *is* what the pipeline reads and reporting
        the underlying table would lose the step the query wrote. With the
        alias absent (a scalar or function binding, or one this walk could not
        close) nothing backs the label, the unearned name ``_entry_table``
        closes for a masked table.
        """
        columns = self._let_schemas.get(name)
        if columns is not None:
            return ScopeEntry(table=name, columns=dict(columns))
        return ScopeEntry(table=None, columns={})

    def _source_entry(self, pipeline: Pipeline) -> ScopeEntry:
        """Return the scope a pipeline starts from, derived from its source.

        Schema lookups are keyed on the **bare** table name, a qualified
        source included: ``database('d').T`` resolves against
        ``schemas["T"]``. The qualifiers stay on the ``TableRef`` for
        consumers that track provenance. A ``"d.T"`` key convention here
        would silently stop resolving every qualified query written against
        a schema dict keyed the ordinary way.
        """
        source = pipeline.source
        if isinstance(source, (DataTableSource, ExternalDataSource)):
            # These declare their own schema inline, so no lookup is needed
            # -- and none would succeed, since neither has a table name.
            return ScopeEntry(table=None, columns=dict(source.columns))
        if isinstance(source, Pipeline):
            # ``materialize(P) | …`` nests a whole pipeline in source
            # position. Walk it so its own operators shape the scope the
            # outer pipeline starts from; an empty anonymous entry leaves
            # everything downstream unresolvable.
            self._walk_pipeline(source)
            columns = dict(source.result_schema.columns) if source.result_schema else {}
            return ScopeEntry(table=None, columns=columns)
        if isinstance(source, LetRef):
            return self._let_alias_entry(source.name)
        if isinstance(source, TableRef) and source.is_wildcard:
            # ``union T*`` names a *set* of tables. Resolving it against a
            # schema entry literally called ``T*`` would be a coincidence,
            # and picking one member of the set would be a guess.
            return ScopeEntry(table=None, columns={})
        name = source.name if isinstance(source, TableRef) else None
        return ScopeEntry(table=self._entry_table(name), columns=self._table_schema(name))

    def _walk_pipeline(
        self,
        pipeline: Pipeline,
        inherited: list[ScopeEntry] | None = None,
    ) -> list[ScopeEntry]:
        """Walk ``pipeline``, returning the scope its last operator leaves.

        ``inherited`` seeds the scope for a sub-pipeline whose source is
        implicit: the ``mv-apply`` / ``partition`` / ``fork`` / ``facet``
        bodies, which run against the enclosing row set and must see its
        columns. A sub-pipeline with its own explicit source ignores it.
        ``toscalar(...)`` and ``materialize(...)`` are uncorrelated in KQL,
        so seeding them would invent provenance the query never expressed.
        """
        if isinstance(pipeline.source, UnknownSource) and not pipeline.operators:
            # The builder could not model this at all, which a real string
            # reaches: `IRBuilder().build("not a query at all")` produces
            # exactly this shape. The pipeline claims nothing about its own
            # output, where `columns={}` would claim it emits none, and it
            # does not inherit, since an unmodeled pipeline emitting the
            # enclosing columns is a guess. (Defensive only -- a fork or
            # mv-apply body is an ``ImplicitSource``, never this.) Whatever
            # the builder left on ``result_schema`` stays: a symbol the binder
            # closed here is the one real answer about this pipeline.
            return []
        source_entry = self._source_entry(pipeline)
        if inherited is not None and not source_entry.table and not source_entry.columns:
            scope: list[ScopeEntry] = [
                ScopeEntry(
                    table=e.table, columns=dict(e.columns), origins=dict(e.origins),
                )
                for e in inherited
            ]
        else:
            scope = [source_entry]
        # ``$left`` / ``$right`` name the sides of the enclosing join's
        # ``on`` clause. A pipeline nested in that clause (a
        # ``toscalar(...)`` subquery) is a new row scope, so neither the
        # sides nor the "left side first" rule for a bare key reaches it.
        outer_sides = self._join_sides
        self._join_sides = None
        try:
            for op in pipeline.operators:
                self._walk_operator(op, scope)
        finally:
            self._join_sides = outer_sides
        # The pipeline's schema is Microsoft's answer or nobody's. The walked
        # scope is provenance structure, which source each visible column came
        # from, so merging it would re-order a join's output by side instead
        # of by the order the engine emits, and would restate a stale shape
        # from an open operator as this pipeline's output.
        #
        # The last operator answers when it has an answer. With no operators
        # the value the *builder* left on the pipeline -- Microsoft's reading
        # of the source -- stands in, taken from the snapshot rather than the
        # live field this method overwrites. Outside an ``enrich`` the
        # snapshot is empty.
        #
        # ``None`` and ``TabularSchema(columns={})`` stay different claims: a
        # bound ``T | project-away *`` closes to an empty symbol,
        # ``table_symbol_columns`` returns ``{}``, and the stamp carries that
        # through unchanged.
        authoritative = (
            pipeline.operators[-1].result_schema if pipeline.operators
            else self._builder_schemas.get(id(pipeline))
        )
        pipeline.result_schema = (
            TabularSchema(columns=dict(authoritative.columns))
            if authoritative is not None
            else None
        )
        return scope

    # --- scope helpers ------------------------------------------------------

    def _column_origins(self, scope: list[ScopeEntry]) -> dict[str, str | None]:
        """``{column: originating table}`` over the whole scope.

        A name carried by two entries that disagree about where it came from
        is **ambiguous** and maps to ``None``. That is KQL's own answer:
        after ``T | union U`` an unqualified ``k`` is neither table's ``k``.
        Join collisions reach this branch too: after ``L | join (R) on k``
        the scope holds both sides' ``shared``, so the unqualified name
        answers ``None`` and the engine's ``shared`` and ``shared1`` both
        land in the anonymous bucket. A qualified ``$left.shared`` /
        ``$right.shared`` resolves by side and is unaffected.
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

    def _overlay_result_schema(
        self,
        columns: dict[str, str],
        scope: list[ScopeEntry],
        renames: dict[str, str] | None = None,
    ) -> None:
        """Rewrite ``scope`` to carry exactly ``columns``, keeping provenance.

        Microsoft's ``ResultType`` says which columns exist after an operator
        and how they are typed. It says nothing about which table each one
        came from, and ``ColumnRef.table`` is the one thing in the IR only
        this walk can supply, so the two are combined: each column is filed
        under the table the *pre-operator* scope had it under, and anything
        new (a summarize aggregate, a join's suffixed ``Foo1``) lands in the
        anonymous entry.

        ``renames`` maps an output name back to the input name it is the same
        column as, and exists for ``project-rename``. Without it ``kk`` in
        ``project-rename kk = k`` files anonymously, since the pre-operator
        scope never held that name. The operator is the only place the query
        states that ``kk`` *is* ``T``'s ``k``, so the fact has to be threaded
        through.

        The table order of the incoming scope is preserved, because
        :meth:`_resolve_column_table` reads it as precedence.

        Replacing the scope outright (one anonymous entry with Microsoft's
        columns) would silently delete provenance for every operator the
        binder can type: ``T | where a > 1 | project a`` would stop reporting
        ``a.table == "T"``.
        """
        origin = self._column_origins(scope)
        renames = renames or {}
        tables: list[str | None] = []
        for entry in scope:
            if entry.table not in tables:
                tables.append(entry.table)
        if None not in tables:
            tables.append(None)
        buckets: dict[str | None, dict[str, str]] = {t: {} for t in tables}
        # A column whose origin is a table no *entry* is keyed on (one a
        # ``project`` already moved into the anonymous entry) goes to the
        # anonymous bucket and keeps its provenance in ``origins``. A fresh
        # table entry for it would reorder the scope, which
        # ``_resolve_column_table`` reads as precedence.
        anon_origins: dict[str, str | None] = {}
        for name, type_name in columns.items():
            table = origin.get(renames.get(name, name))
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
        """Fill provenance, then overlay Microsoft's schema where there is one.

        The provenance step fills every expression the operator carries and
        adds the scope *structure* the multi-source operators need (a join's
        right side, a union's arms, a search's tables). It never derives the
        operator's output columns. ``op.result_schema`` -- Microsoft's
        ``ResultType``, stamped exactly where the symbol is closed -- is
        overlaid onto that structure, so names and types are the binder's and
        each column keeps the table the walk knows it came from. Where it is
        ``None`` the scope passes through untouched and the pipeline's own
        ``result_schema`` says ``None``.
        """
        self._walk_operator_provenance(op, scope)
        if op.result_schema is not None:
            self._overlay_result_schema(
                dict(op.result_schema.columns), scope, _renamed_columns(op),
            )

    def _walk_operator_provenance(
        self, op: Operator, scope: list[ScopeEntry],
    ) -> None:
        """Extend ``scope`` with the structure provenance needs; never the output columns.

        Four operator families get a branch, because they bring *sources*
        into scope and the overlay cannot recover where a column came from
        once the sources are forgotten:

        * ``join`` / ``lookup``: the right-hand pipeline is walked and
          appended as one flattened entry, and ``_join_sides`` is set around
          the ``on`` clause so ``$left`` / ``$right`` and the bare-key
          left-first rule resolve. Join-kind column selection, the ``Foo1``
          collision suffixes and ``lookup``'s right-key drop stay
          unreproduced: ``result_schema`` states the surviving columns and
          the overlay applies them.
        * ``union``: each arm's entries are appended so a column only one arm
          carries keeps that arm's table. Type-conflict splitting (``a_long``
          / ``a_string``) and ``withsource`` are Microsoft's to state.
        * ``search`` / ``find``: both an implicit source, so without seeding
          here the predicate, and for ``find`` every ``project`` column, would
          resolve against nothing. One entry is appended per named table, or
          per table in ``self.schemas`` for an unqualified search or find, the
          dict standing in for "every table in the database". One entry per
          ``let``-alias table follows, from ``_let_alias_entry``, so an alias
          absent from ``self._let_schemas`` seeds ``table=None`` here exactly
          as it does in source position. The predicate and ``project`` are
          filled *after*, against the seeded scope. ``search``'s ``$table``
          and ``find``'s ``withsource``, the column each names for its own
          found-in-table marker, are Microsoft's to state.

        Everything else fills its expressions and walks its nested pipelines,
        scope untouched. Implicit-source sub-pipelines (the ``mv-apply`` /
        ``partition`` / ``fork`` / ``facet`` bodies) inherit the current
        scope; ones with their own source ignore it.
        """
        if isinstance(op, (JoinOp, LookupOp)):
            rhs_scope = self._walk_pipeline(op.right)
            # Snapshot the left before the right entry is appended: ``$left``
            # is the accumulated left row set, which after an earlier join is
            # several entries, so a positional resolution would name whichever
            # table that join added.
            left_side = list(scope)
            right_side = [_flatten_side(rhs_scope)]
            on_scope = left_side + right_side
            previous_sides = self._join_sides
            self._join_sides = (left_side, right_side)
            try:
                for e in op.on:
                    self._fill(e, on_scope)
            finally:
                self._join_sides = previous_sides
            scope.append(right_side[0])
            return
        if isinstance(op, UnionOp):
            for sub in op.pipelines:
                for entry in self._walk_pipeline(sub):
                    if entry not in scope:
                        scope.append(entry)
            return
        if isinstance(op, (SearchOp, FindOp)):
            refs = op.tables
            names = [t.name for t in refs if isinstance(t, TableRef)]
            aliases = [t.name for t in refs if isinstance(t, LetRef)]
            if not names and not aliases:
                names = list(self.schemas)
            scope.extend(
                ScopeEntry(
                    table=self._entry_table(n), columns=dict(self._table_schema(n)),
                )
                for n in names
            )
            scope.extend(self._let_alias_entry(a) for a in aliases)
            self._fill(op.predicate, scope)
            for expr in getattr(op, "project", []):
                self._fill(expr, scope)
            return
        self._fill_children(op, scope, inherited=scope)

    def _resolve_side(self, name: str, entries: list[ScopeEntry]) -> str | None:
        """Place ``name`` within one side of a join.

        By name first, which is what ``$left`` needs: after ``L | join (R) …``
        the left side is two entries and only one of them has the column.
        Where the name is unknown (an unbound right-hand table, a column the
        schema dict does not describe) a *single* entry still names the side
        unambiguously, so its table stands in. With two or more entries there
        is nothing to pick between them and no sentinel to fall back on, so
        the caller leaves ``.table`` at ``None``.

        A masked function or pattern parameter needs no special case here:
        ``_entry_table`` answers ``None`` for its ``ScopeEntry.table``, so
        the single-entry fallback stands in with that answer instead of
        surfacing the parameter's own name.
        """
        resolved = self._resolve_column_table(name, entries)
        if resolved:
            return resolved
        if len(entries) == 1:
            return entries[0].table
        return None

    def _resolve_column_table(self, name: str, scope: list[ScopeEntry]) -> str | None:
        # ``_column_origins`` reads ``ScopeEntry.origins`` as well as
        # ``table``, so a column a ``project`` moved into the anonymous entry
        # still resolves, and a name two entries disagree about gets ``None``.
        return self._column_origins(scope).get(name)

    def _fill_children(
        self,
        node: object,
        scope: list[ScopeEntry],
        inherited: list[ScopeEntry] | None = None,
    ) -> None:
        """Fill every expression and pipeline ``node`` holds, at any depth.

        Children come from ``model_fields`` rather than a hardcoded tuple of
        attribute names, which widens a hole every time a model grows a
        field: a missed ``pipeline``, ``branches`` or ``default`` leaves
        ``toscalar(...)`` subtrees and ``case(...)`` arms unvisited, and the
        same column then resolves inside one operator and not inside another,
        silently. ``model_fields`` cannot drift.

        A nested pipeline goes to ``_walk_pipeline``, which builds a scope
        from that pipeline's own source. ``Pipeline`` is not an ``Expr``, so
        an ``isinstance(child, Expr)`` test alone never reaches it.
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
            side = expr.join_side
            sides = self._join_sides
            if side is not None:
                if sides is not None:
                    resolved = self._resolve_side(
                        expr.name, sides[0] if side == "left" else sides[1],
                    )
                    if resolved:
                        expr.table = resolved
            elif expr.table is None:
                if sides is not None:
                    # A bare ``on k`` is shorthand for ``$left.k == $right.k``
                    # and both sides usually have a ``k``, so the general
                    # ambiguity rule would answer None. The engine keeps the
                    # left side's column (``lookup`` drops the right's and
                    # ``join`` suffixes it), so the left side answers first.
                    resolved = self._resolve_side(
                        expr.name, sides[0],
                    ) or self._resolve_side(expr.name, sides[1])
                else:
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
        elif isinstance(expr, BinOp):
            # Typing every ``BinOp`` as ``bool`` would record
            # ``extend n = a + 1`` as ``n:bool``, the same answer the node
            # gives for the predicate ``a > 1``. Arithmetic stays unresolved:
            # its type is the promotion of its operands' (``long + real`` is
            # a real, ``datetime - datetime`` a timespan), which this
            # fallback does not model.
            if expr.op not in ARITHMETIC_OPS:
                expr.result_type = KustoType.BOOL
        elif isinstance(expr, (SetMembership, Between, And, Or, Not)):
            expr.result_type = KustoType.BOOL
