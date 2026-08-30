# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier 1 query surface: :class:`KustoQuery`."""

import json

from . import lexical
from ._text import Utf16Offsets
from .bridge import GlobalState, KustoCode
from .services import _analyze_guarded, _diagnostic_dicts
from .spans import TextSpan, TimeExpr
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
    """A parsed KQL query wrapping Microsoft's ``KustoCode``.

    Construct through :func:`kustology.parse`, which decides whether the
    parse is bound (schema passed) or purely syntactic; ``has_semantics``
    reports which. Accessors that can answer both ways take the binder's
    answer on a bound parse and a syntactic walk otherwise.
    """

    def __init__(
        self, kusto_code: KustoCode, *, extra_diagnostics: list[dict] | None = None,
    ):
        """Wrap an already-parsed ``KustoCode``; use :func:`kustology.parse` instead of calling this directly."""
        self._code = kusto_code
        # kustology's own diagnostics about this parse, carrying
        # :func:`kustology.services._analyze_guarded`'s record that the
        # analyzer crashed and the tree is the unbound parse.
        self._extra_diagnostics: list[dict] = list(extra_diagnostics or [])
        self._utf16_offsets: Utf16Offsets | None = None

    @property
    def _offsets(self) -> Utf16Offsets:
        """UTF-16 to code-point translation for this query's text, built once.

        Every offset Microsoft reports counts UTF-16 code units; every offset
        this class reports counts code points, so it indexes the ``str`` the
        caller holds. Cached because the query text does not change and
        indexing it is O(len(text)).
        """
        if self._utf16_offsets is None:
            self._utf16_offsets = Utf16Offsets(self.text)
        return self._utf16_offsets

    @property
    def syntax(self):
        """The root ``SyntaxNode`` of Microsoft's parse tree (a ``QueryBlock``).

        Offsets on these nodes (``TextStart``, ``Width``, ``End``) count
        UTF-16 code units, so slicing :attr:`text` with one is wrong as soon
        as the query holds an astral character. Convert with
        :func:`kustology.utf16_to_codepoint`. Every offset kustology reports
        is already a code-point offset.
        """
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

        ``start`` and ``length`` are code-point offsets into :attr:`text`. The
        list is read off the ``KustoCode`` this object already holds, with no
        second parse, so on a bound query the binder's semantic diagnostics
        (unresolved columns, type errors) are included and on an unbound one
        the parser's are all there is. ``validate(text)`` answers the same
        question for text you have not parsed yet.

        Unfiltered: there is no way to suppress ``KS204`` here, as
        ``validate(..., ignore_unknown_tables=True)`` does. The parse is
        already done, so filtering here would only hide rows. Filter the list
        yourself: ``[d for d in q.diagnostics if d["code"] != "KS204"]``.

        One row can come from kustology: if the analyzer crashed while
        ``parse(..., schema=...)`` was binding this tree, the crash is
        reported here as an ``Error`` with the code
        :data:`kustology.services.ANALYZE_FAILED_CODE` and the tree is the
        unbound parse. :meth:`to_ir` re-analyzes and records its own row
        instead of repeating this one.
        """
        return _diagnostic_dicts(
            self._code.GetDiagnostics(), self._offsets,
        ) + self._extra_diagnostics

    def get_referenced_tables(self, force_syntactic: bool = False) -> set[str]:
        """Return the set of tables referenced by the query.

        Uses the binder when the query was parsed with a schema and the
        syntactic walk otherwise. On a bound query it runs the syntactic walk
        as well, for names the binder could not resolve, so a table the schema
        does not describe is still reported and a partial schema cannot make a
        table disappear. Pass ``force_syntactic=True`` to bypass the binder
        entirely, which is mainly useful for benchmarking and parity checks.

        ``let`` aliases, ``as`` aliases, function parameters and wildcard
        patterns are excluded. The binding's own right-hand side is included:
        in ``let T = T | where x; T | take 1`` the right-hand ``T`` is the
        real table and the rest is the alias.

        One position is bind-dependent in the other direction: the node table
        in ``make-graph``'s ``with`` clause. The syntactic walk does not reach
        it, so ``parse("Edges | make-graph src --> dst with Nodes on
        n").get_referenced_tables()`` answers ``{"Edges"}`` while the same
        query bound answers ``{"Edges", "Nodes"}``. :meth:`replace_table`
        inherits the split. Bind before migrating tables in a query that
        builds a graph.
        """
        return {
            name
            for name, _ in find_table_references(
                self._code, force_syntactic=force_syntactic
            )
        }

    def find_table_references(self, force_syntactic: bool = False):
        """Return ``[(name, node), ...]`` for every table reference in the query, in source order.

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
        ``"truncated": True``. The cap exists because the AST's depth is the
        Python stack's depth: without one, a few kilobytes of parentheses
        outrun CPython's frame limit and raise ``RecursionError``. Counting
        the root as level 0, the 49-fixture corpus has a median depth of 18
        and a deepest of 42 (``FileHashEntity_SecurityEvent.kql``), and 22 of
        the 49 go past 20. The cap of 300 is seven times that deepest
        measurement, so the key is absent from ordinary output.
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

    def find_time_expressions(self) -> list[TimeExpr]:
        """Return ``[TimeExpr(text, start, length), ...]`` in source order.

        A discovery aid; it does not resolve a lookback window. See
        :func:`kustology.utils.analysis.find_time_expressions`.
        """
        return find_time_expressions(self._code)

    def tokens(self) -> list[lexical.Token]:
        """Return Microsoft's token stream with code-point spans. See :mod:`kustology.lexical`."""
        return lexical.tokens(self._code)

    def comment_spans(self) -> list[TextSpan]:
        """Return the span of every ``//`` comment. See :func:`kustology.lexical.comment_spans`."""
        return lexical.comment_spans(self._code)

    def string_literal_spans(self, *, include_prefix: bool = True) -> list[TextSpan]:
        """Return the span of every string literal. See :func:`kustology.lexical.string_literal_spans`."""
        return lexical.string_literal_spans(self._code, include_prefix=include_prefix)

    def statement_spans(self) -> list[TextSpan]:
        """Return the span of each top-level statement, separator excluded.

        See :func:`kustology.lexical.statement_spans`.
        """
        return lexical.statement_spans(self._code)

    def get_time_range(self) -> list[TimeExpr]:
        """Return :meth:`find_time_expressions`'s result under this deprecated name."""
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

    def to_ir(
        self,
        attach_schema: bool | dict | None = None,
        *,
        semantic_hash: bool = False,
    ):
        """Build the pydantic IR from this ``KustoCode``. Requires the ``[ir]`` extra.

        Reuses the already-parsed AST, so there is no second parse. On a parse
        bound with a schema the binder's ``GlobalState`` is reused, so
        symbol-resolved nodes keep the types Microsoft's binder writes into
        ``Expr.result_type`` on the way through.

        **Without a schema the binder still runs**, against
        ``GlobalState.Default``. ``KustoCode.Analyze(globals)`` binds the tree
        in hand and hands back a *new* bound ``KustoCode``, so this object
        stays syntactic: ``has_semantics`` stays ``False`` and every Tier 1
        accessor keeps taking its syntactic path. It buys real types for
        everything that needs no table — ``1h`` is a ``timespan``, ``1.5`` a
        ``real``, ``ago(1h)`` a ``datetime``.

        Those types come from the populated half of ``GlobalState.Default``:
        Kusto's built-in functions, aggregates and plug-ins, several hundred
        of them, where ``ago`` and every other built-in resolves. Empty are
        the default **database** — no tables, no user functions, no external
        tables, materialized views, entity groups or stored query results —
        and the cluster list. Every name that has to come from a database
        therefore fails to resolve, so the whole unknown-name diagnostic
        family those failures raise
        (:data:`kustology.services._UNKNOWN_NAME_CODES`, twelve codes of which
        ``KS204`` "the name X does not refer to any known table" is one) is an
        artifact of how the types were obtained, and it is filtered out. A
        parse the caller bound with their own schema keeps every one of them,
        because there an undescribed name is a real error.

        A clean ``diagnostics`` list from a schemaless ``to_ir()`` therefore
        means "nothing wrong that default globals could see". It says nothing
        about whether the query's tables, columns or user functions exist.
        Bind with a schema to ask that question.

        Microsoft's binder supplies types and per-operator output schemas
        whenever a schema is in play, ``Expr.result_type`` and
        ``Pipeline.result_schema`` alike. That schema arrives at parse time
        (``parse(query, schema=...)``) or through ``attach_schema`` here.
        ``SchemaAttacher`` is the second, separate pass: it fills
        ``ColumnRef.table`` with the table a resolved column came from and
        sets ``QueryIR.schema_attached = True``.

        ``attach_schema`` controls whether the provenance pass
        (``SchemaAttacher``) runs, and for a non-empty ``dict`` also what the
        binder binds against for this call:

        * ``None`` (default) — auto-attach iff the parse was bound, so
          ``parse(query, schema=...).to_ir()`` returns a fully enriched
          IR without restating the schema.
        * ``True`` — force the attach pass using the schema captured at
          parse time.
        * ``False`` — skip the attach pass even on a bound parse. Use it
          when you want the binder's ``result_type`` and none of the table
          provenance.
        * non-empty ``dict`` — re-bind the *same tree* against
          ``build_global_state(dict)``, with no re-parse and the receiver
          untouched, then run the attach pass with the same dict. On an
          already-bound parse this replaces the parse-time schema for this
          call, and the resulting output schemas, types and IR shape match
          ``parse(query, schema=dict).to_ir()`` exactly: ``let A = T``
          lowers to ``rhs_pipeline`` whenever ``T`` resolves in *either*
          path. A partial dict, one that omits a table the query
          references, leaves that symbol open, and Microsoft's binder
          reports the affected operator's ``result_schema`` as ``None``
          without raising. Diagnostics do not follow that equivalence:
          unknown-name suppression tracks the *receiver's* own bind state,
          so a dict on an unbound receiver stays lenient about unknown
          names (``parse(q).to_ir(attach_schema=d)``) while the same dict
          on a bound receiver keeps them (``parse(q, schema=d).to_ir()``).
        * ``{}`` — falsy, so treated the same as ``False``: no re-bind,
          no attach pass.

        ``semantic_hash=True`` computes the digest during the build, which is
        the larger part of it. The default defers
        :attr:`QueryIR.semantic_hash` to its first read, where it is memoized.
        """
        from .ir.builder import IRBuilder  # local import: triggers the [ir] extra guard lazily

        bound_by_caller = self._code.HasSemantics
        schemas = (
            attach_schema
            if isinstance(attach_schema, dict) and attach_schema
            else None
        )
        failure: dict | None = None
        if schemas is not None:
            # ``Analyze`` re-binds the tree in hand without re-lexing the text
            # or mutating ``self._code``, so the receiver keeps its own bind
            # state. Guarded because Microsoft's binder crashes on some
            # clean-parsing input; the fallback is the tree already held.
            # ``build_global_state`` stays outside the guard: a malformed
            # schema dict is the caller's error and must still raise.
            state = build_global_state(schemas)
            code, failure = _analyze_guarded(
                lambda: self._code.Analyze(state), lambda: self._code,
            )
            # ``build_global_state`` accepts three value shapes: a
            # ``{col: type}`` dict, a Kusto schema string ``"(col:type)"``,
            # and a bare ``[col]`` list. ``parse(schema=...)`` documents all
            # three, so this entry point takes them too. ``SchemaAttacher``
            # takes only the first — it reads ``schemas[table][column]`` — so
            # a string value crashes it and a list resolves by coincidence.
            #
            # Reading the shapes back off ``code.Globals`` normalizes all
            # three through the parsing Microsoft already did, and guarantees
            # the attacher sees what the *builder* bound against: the same
            # source ``attach_schema=True`` normalizes from.
            schemas = _extract_schemas_from_global_state(code.Globals)
        elif bound_by_caller:
            code = self._code
        else:
            # ``Analyze`` binds this tree without re-lexing the text or
            # mutating ``self._code``. Guarded like the dict path above.
            code, failure = _analyze_guarded(
                lambda: self._code.Analyze(GlobalState.Default), lambda: self._code,
            )
        ir = IRBuilder(global_state=code.Globals).build_from_code(
            code,
            ignore_unknown_tables=not bound_by_caller,
            semantic_hash=semantic_hash,
        )
        if failure is not None:
            from .ir.query import Diagnostic

            # No span: a fault in the analyzer covers no region of the query.
            # ``QueryIR.diagnostics`` is outside the hash payload, so
            # appending after the build cannot stale ``semantic_hash``.
            ir.diagnostics.append(Diagnostic(
                message=failure["message"],
                severity=failure["severity"],
                code=failure["code"],
                category=failure["category"],
                detail=failure.get("detail"),
            ))

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
        """Return the query's source text."""
        return self.text

    def __repr__(self):
        """Return a debug summary: character count, operator count, and bind state."""
        n_ops = len(self.get_operator_chain())
        return (
            f"<KustoQuery {len(self.text)} chars, {n_ops} ops, "
            f"has_semantics={self.has_semantics}>"
        )
