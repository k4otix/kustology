# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import json

from .bridge import GlobalState, KustoCode
from .services import _diagnostic_dicts
from .utils.analysis import (
    find_table_references,
    find_time_expressions,
    get_operator_chain,
    get_operator_stats,
    get_referenced_columns,
    get_referenced_functions,
    get_structural_hash,
    node_to_dict,
    replace_table,
)
from .utils.schema_state import build_global_state
from .utils.schema_state import extract_schemas_from_global_state as _extract_schemas_from_global_state


class KustoQuery:
    def __init__(self, kusto_code: KustoCode):
        self._code = kusto_code

    @property
    def syntax(self):
        """The root ``SyntaxNode`` of Microsoft's parse tree (a ``QueryBlock``)."""
        return self._code.Syntax

    @property
    def text(self) -> str:
        """The query text this object was parsed from, verbatim."""
        return self._code.Text

    @property
    def has_semantics(self) -> bool:
        """True when the underlying KustoCode was bound (parsed with a schema)."""
        return self._code.HasSemantics

    @property
    def diagnostics(self) -> list[dict]:
        """This query's diagnostics, in :func:`kustology.validate`'s dict shape.

        Read off the ``KustoCode`` this object already holds — no second
        parse — so on a bound query the binder's semantic diagnostics
        (unresolved columns, type errors) are included and on an unbound one
        the parser's are all there is. ``validate(text)`` answers the same
        question for text you have not parsed yet.

        Unfiltered: unlike ``validate(..., ignore_unknown_tables=True)``
        there is no way to suppress ``KS204`` here. Filter the list yourself
        — ``[d for d in q.diagnostics if d["code"] != "KS204"]`` — since the
        parse is already done and doing it here would only hide rows.
        """
        return _diagnostic_dicts(self._code.GetDiagnostics())

    def get_referenced_tables(self, force_syntactic: bool = False) -> set[str]:
        """Return the set of tables referenced by the query.

        Uses the binder when the query was parsed with a schema, the syntactic
        walk otherwise — and on a bound query, the syntactic walk as well, for
        the names the binder could not resolve. Pass ``force_syntactic=True``
        to bypass the binder entirely (mainly useful for benchmarking and
        parity checks).

        ``let`` aliases, ``as`` aliases, function parameters and wildcard
        patterns are not tables and are excluded; the binding's own
        right-hand side is included — in ``let T = T | where x; T | take 1``
        the right-hand ``T`` is the real table and the rest is the alias.

        On a bound query the result is *not* limited to what the binder
        resolved: a table the schema does not describe is still reported, so
        a partial schema cannot make a table disappear.

        One position is bind-dependent in the other direction: the node
        table in ``make-graph``'s ``with`` clause. The syntactic walk does
        not reach it, so ``parse("Edges | make-graph src --> dst with Nodes
        on n").get_referenced_tables()`` answers ``{"Edges"}`` while the
        same query bound answers ``{"Edges", "Nodes"}``.
        :meth:`replace_table` inherits the split — a no-op unbound, a
        correct rewrite bound. Bind before migrating tables in a query that
        builds a graph.
        """
        return {
            name
            for name, _ in find_table_references(
                self._code, force_syntactic=force_syntactic
            )
        }

    def find_table_references(self, force_syntactic: bool = False):
        """Return [(name, node), ...] for every table reference in the query,
        in source order.

        On a bound query this is the binder's references plus the syntactic
        ones it left unresolved — see
        :func:`kustology.utils.analysis.find_table_references`.
        """
        return find_table_references(self._code, force_syntactic=force_syntactic)

    def get_operator_chain(self) -> list:
        """Return the main pipeline's operator nodes, left to right.

        Operator nodes only — the source table is not one — and the main
        pipeline only; use :meth:`get_operator_stats` for the whole AST. See
        :func:`kustology.utils.analysis.get_operator_chain`.
        """
        return get_operator_chain(self._code)

    def get_operator_stats(self) -> dict[str, int]:
        """Return a {OperatorKind: count} map across the query's AST."""
        return get_operator_stats(self._code)

    def to_dict(self) -> dict:
        """Serialize the syntax tree to a recursive ``{kind, text, children}``.

        Descent stops at :data:`kustology.utils.walker.MAX_AST_DEPTH`; a node
        at the cap is emitted with no children and an extra
        ``"truncated": True``. Without the cap this raised ``RecursionError``
        on deeply nested input, since the AST's depth is the Python stack's
        depth and a few kilobytes of parentheses outrun CPython's frame
        limit. Real queries stay well inside it, but not as far inside as
        they look: counting the root as level 0, the 49-fixture corpus has a
        median depth of 18 and a deepest of 42
        (``FileHashEntity_SecurityEvent.kql``), and 22 of the 49 go past 20.
        The cap of 300 is still seven times that deepest measurement, so the
        key is absent from ordinary output.
        """
        return node_to_dict(self.syntax)

    def to_json(self, indent: int = 2) -> str:
        """``to_dict()`` as JSON, including its truncation marker if any."""
        return json.dumps(self.to_dict(), indent=indent)

    def get_referenced_columns(self, force_syntactic: bool = False) -> set[str]:
        """Return the set of column names the query references.

        The two modes differ on a column the query creates and never reads
        back — see
        :func:`kustology.utils.analysis.get_referenced_columns`.
        """
        return get_referenced_columns(self._code, force_syntactic=force_syntactic)

    def get_referenced_functions(self, force_syntactic: bool = False) -> set[str]:
        """Return the set of function names the query calls.

        Semantic mode reads the binder's symbols, syntactic mode the callee
        positions — see
        :func:`kustology.utils.analysis.get_referenced_functions`.
        """
        return get_referenced_functions(self._code, force_syntactic=force_syntactic)

    def get_structural_hash(self) -> str:
        """SHA256 over the AST shape — "same query modulo the data".

        Blind to literal values and identifiers, sensitive to named-parameter
        keywords and the ``evaluate`` plug-in; see
        :func:`kustology.utils.analysis.get_structural_hash` for the full
        contract.
        """
        return get_structural_hash(self._code)

    def find_time_expressions(self) -> list[tuple[str, int, int]]:
        """Return ``[(text, start, length), ...]`` in source order.

        A discovery aid, not a lookback extractor — see
        :func:`kustology.utils.analysis.find_time_expressions`.
        """
        return find_time_expressions(self._code)

    def get_time_range(self) -> list[tuple[str, int, int]]:
        """Deprecated alias for :meth:`find_time_expressions`."""
        import warnings

        warnings.warn(
            "KustoQuery.get_time_range() is deprecated; use "
            "find_time_expressions(). It returns a source-ordered discovery "
            "list of time expressions, not a resolved time range.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_time_expressions()

    def replace_table(
        self,
        old_name: str,
        new_name: str,
        force_syntactic: bool = False,
    ) -> str:
        """Rename every reference to ``old_name``; return the rewritten query.

        Rewrites the spans :meth:`find_table_references` reports — including,
        on a bound query, tables the schema does not describe — and leaves
        aliases and function parameters alone. A wildcard pattern is never
        rewritten even where the binder expanded it to the name you passed:
        see :func:`kustology.utils.analysis.replace_table`.

        Because it rewrites what :meth:`find_table_references` reports, it
        inherits that method's bind-state split on ``make-graph``'s ``with``
        clause — unbound, renaming the node table there is a silent no-op.
        """
        return replace_table(
            self._code, old_name, new_name, force_syntactic=force_syntactic
        )

    def to_ir(self, attach_schema: bool | dict | None = None):
        """Build the pydantic IR from this ``KustoCode``. Requires the ``[ir]`` extra.

        Reuses the already-parsed AST (no second parse). If bound with a
        schema, the binder's ``GlobalState`` is reused so symbol-resolved
        nodes keep their types — Microsoft's binder populates
        ``Expr.result_type`` on the way through.

        **Without a schema the binder still runs**, against
        ``GlobalState.Default``. ``KustoCode.Analyze(globals)`` binds the
        tree already in hand and hands back a *new* bound ``KustoCode``, so
        this costs no second parse and leaves this object syntactic —
        ``has_semantics`` stays ``False`` and every Tier 1 accessor keeps
        taking its syntactic path. What it buys is real types for everything
        that does not need a table: ``1h`` is a ``timespan``, ``1.5`` a
        ``real``, ``ago(1h)`` a ``datetime``.

        Those types come from the half of ``GlobalState.Default`` that is
        *populated*: Kusto's built-in functions, aggregates and plug-ins,
        several hundred of them. ``ago`` resolves there, and so does every
        other built-in. What is empty is the default **database** — no
        tables, no user functions, no external tables, materialized views,
        entity groups or stored query results — and the cluster list. So
        every name that has to come from a database fails to resolve, and
        the whole unknown-name diagnostic family those failures raise
        (:data:`kustology.services._UNKNOWN_NAME_CODES`, twelve codes of
        which ``KS204`` "the name X does not refer to any known table" is
        one) is an artifact of how the types were obtained rather than
        anything the caller wrote. It is filtered out. A parse the caller
        bound with their own schema keeps every one of them, because there
        an undescribed name is a real error.

        A clean ``diagnostics`` list from a schemaless ``to_ir()`` therefore
        means "nothing wrong that default globals could see" — it does not
        mean the query's tables, columns or user functions exist. Bind with
        a schema to ask that question.

        Microsoft's binder is where types and per-operator output schemas
        come from whenever a schema is in play — ``Expr.result_type`` and
        ``Pipeline.result_schema`` alike. That schema can arrive at parse
        time (``parse(query, schema=...)``) or right here: a non-empty
        ``dict`` passed as ``attach_schema`` re-binds the *same tree*
        against it (``self._code.Analyze(build_global_state(dict))`` — no
        re-parse, and the receiver stays untouched) rather than merely
        decorating whatever the parse already knew. ``SchemaAttacher`` is
        the second, separate pass: it fills ``ColumnRef.table`` — which
        table a resolved column came from — and sets
        ``QueryIR.schema_attached = True``.

        ``attach_schema`` controls whether the provenance pass
        (``SchemaAttacher``) runs — and, only for a non-empty ``dict``,
        what the binder binds against for this call too:

        * ``None`` (default) — auto-attach iff the parse was bound, so
          ``parse(query, schema=...).to_ir()`` returns a fully enriched
          IR without restating the schema.
        * ``True`` — force the attach pass using the schema captured at
          parse time.
        * ``False`` — skip the attach pass even on a bound parse. Use
          when you only want the binder's ``result_type`` and none of
          the table provenance.
        * non-empty ``dict`` — re-bind against ``build_global_state(dict)``
          and run the attach pass with the same dict. This is a real
          re-bind, not an overlay: on an already-bound parse it replaces
          the parse-time schema for this call rather than layering on top
          of it, and the resulting IR — shape included — is now identical
          to ``parse(query, schema=dict).to_ir()``. ``let A = T`` lowers
          to ``rhs_pipeline`` whenever ``T`` resolves in *either* path;
          the unbound dict path used to fall back to ``rhs_expr`` because
          the tree was never actually bound. A partial dict — one that
          omits a table the query references — leaves that symbol open:
          Microsoft's binder does not raise, it reports the affected
          operator's ``result_schema`` as ``None`` rather than guessing.
        * ``{}`` — falsy, so treated the same as ``False``: no re-bind,
          no attach pass.
        """
        from .ir.builder import IRBuilder  # local import: triggers the [ir] extra guard lazily

        bound_by_caller = self._code.HasSemantics
        schemas = (
            attach_schema
            if isinstance(attach_schema, dict) and attach_schema
            else None
        )
        if schemas is not None:
            # A dict is a real binding request: re-bind the tree in hand
            # against it. ``Analyze`` does not re-lex the text and does not
            # mutate ``self._code``, so the receiver stays syntactic (or
            # keeps its parse-time binding) regardless.
            code = self._code.Analyze(build_global_state(schemas))
        elif bound_by_caller:
            code = self._code
        else:
            # D5/K27. ``Analyze`` binds this tree; it does not re-lex the
            # text and it does not mutate ``self._code``.
            code = self._code.Analyze(GlobalState.Default)
        ir = IRBuilder(global_state=code.Globals).build_from_code(
            code, ignore_unknown_tables=not bound_by_caller,
        )

        # Default: attach iff we have a bound parse to extract schemas from.
        # Explicit True/False/dict always wins.
        if attach_schema is None:
            attach_schema = bound_by_caller

        if attach_schema:
            from .ir.binder import SchemaAttacher

            if schemas is None and bound_by_caller:
                schemas = _extract_schemas_from_global_state(self._code.Globals)
            SchemaAttacher(schemas or {}).enrich(ir)

        return ir

    def __str__(self):
        return self.text

    def __repr__(self):
        n_ops = len(self.get_operator_chain())
        return (
            f"<KustoQuery {len(self.text)} chars, {n_ops} ops, "
            f"has_semantics={self.has_semantics}>"
        )
