# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import json

from .bridge import KustoCode
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
from .utils.schema_state import extract_schemas_from_global_state as _extract_schemas_from_global_state


class KustoQuery:
    def __init__(self, kusto_code: KustoCode):
        self._code = kusto_code

    @property
    def syntax(self):
        return self._code.Syntax

    @property
    def text(self) -> str:
        return self._code.Text

    @property
    def has_semantics(self) -> bool:
        """True when the underlying KustoCode was bound (parsed with a schema)."""
        return self._code.HasSemantics

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
        return get_operator_chain(self._code)

    def get_operator_stats(self) -> dict[str, int]:
        """Return a {OperatorKind: count} map across the query's AST."""
        return get_operator_stats(self._code)

    def to_dict(self) -> dict:
        return node_to_dict(self.syntax)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def get_referenced_columns(self, force_syntactic: bool = False) -> set[str]:
        return get_referenced_columns(self._code, force_syntactic=force_syntactic)

    def get_referenced_functions(self, force_syntactic: bool = False) -> set[str]:
        return get_referenced_functions(self._code, force_syntactic=force_syntactic)

    def get_structural_hash(self) -> str:
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

        Rewrites exactly the spans :meth:`find_table_references` reports —
        including, on a bound query, tables the schema does not describe,
        and excluding aliases, function parameters and wildcard patterns.
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

        Two passes populate the IR's type / provenance information:

        * **Microsoft binder** (runs when ``parse(query, schema=...)`` is
          used) — fills ``Expr.result_type``.
        * **``SchemaAttacher``** (separate pass) — fills
          ``ColumnRef.table`` and ``Pipeline.result_schema`` and sets
          ``QueryIR.schema_attached = True``.

        ``attach_schema`` controls the second pass:

        * ``None`` (default) — auto-attach iff the parse was bound, so
          ``parse(query, schema=...).to_ir()`` returns a fully enriched
          IR without restating the schema.
        * ``True`` — force the attach pass using the schema captured at
          parse time.
        * ``False`` — skip the attach pass even on a bound parse. Use
          when you only want the binder's ``result_type`` and none of
          the table provenance.
        * ``dict`` — force the attach pass using the supplied schema
          dict, overriding the parse-time schema for this step only.
        """
        from .ir.builder import IRBuilder  # local import: triggers the [ir] extra guard lazily

        global_state = self._code.Globals if self._code.HasSemantics else None
        ir = IRBuilder(global_state=global_state).build_from_code(self._code)

        # Default: attach iff we have a bound parse to extract schemas from.
        # Explicit True/False/dict always wins.
        if attach_schema is None:
            attach_schema = self._code.HasSemantics

        if attach_schema:
            from .ir.binder import SchemaAttacher

            schemas = attach_schema if isinstance(attach_schema, dict) else None
            if schemas is None and self._code.HasSemantics:
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
