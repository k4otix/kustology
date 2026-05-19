# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import json

from .bridge import KustoCode
from .utils.analysis import (
    get_tables_syntactic,
    get_tables_semantic,
    get_operator_chain,
    get_operator_stats,
    node_to_dict,
    get_referenced_columns,
    get_structural_hash,
    get_time_range,
    replace_table,
    find_table_references,
)


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
        walk otherwise. Pass ``force_syntactic=True`` to bypass the binder on a
        bound query (mainly useful for benchmarking and parity checks).
        """
        if not force_syntactic and self._code.HasSemantics:
            return get_tables_semantic(self._code)
        return get_tables_syntactic(self._code)

    def find_table_references(self, force_syntactic: bool = False):
        """Return [(name, node), ...] for every table reference in the query."""
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

    def get_structural_hash(self) -> str:
        return get_structural_hash(self._code)

    def get_time_range(self) -> list[tuple[str, int, int]]:
        """Return [(text, start, length), ...] in source order."""
        return get_time_range(self._code)

    def replace_table(
        self,
        old_name: str,
        new_name: str,
        force_syntactic: bool = False,
    ) -> str:
        return replace_table(
            self._code, old_name, new_name, force_syntactic=force_syntactic
        )

    def to_ir(self):
        """Build the pydantic IR from this ``KustoCode``. Requires the ``[ir]`` extra.

        Reuses the already-parsed AST (no second parse). If bound with a schema,
        the binder's ``GlobalState`` is reused so symbol-resolved nodes keep
        their types.
        """
        from .ir.builder import IRBuilder  # noqa: PLC0415 — triggers [ir] guard lazily

        global_state = self._code.Globals if self._code.HasSemantics else None
        return IRBuilder(global_state=global_state).build_from_code(self._code)

    def __str__(self):
        return self.text

    def __repr__(self):
        n_ops = len(self.get_operator_chain())
        return (
            f"<KustoQuery {len(self.text)} chars, {n_ops} ops, "
            f"has_semantics={self.has_semantics}>"
        )
