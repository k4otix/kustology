import json
from typing import Optional
from .bridge import KustoCode
from .utils.analysis import (
    get_tables_syntactic, get_tables_semantic, mask_literals,
    get_operator_chain, node_to_dict, get_referenced_columns,
    get_structural_hash, get_time_range, replace_table
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

    def get_referenced_tables(self, schema: Optional[dict] = None) -> set[str]:
        """
        Extracts table names. 
        - If schema is None: Uses syntactic heuristics (fast).
        - If schema is provided: Uses .NET Semantic Binding (accurate).
        """
        if schema:
            return get_tables_semantic(self._code, schema)
        return get_tables_syntactic(self._code)

    def mask_literals(self) -> str:
        """
        Returns a masked version of the query string.
        """
        return mask_literals(self._code)

    def get_operator_chain(self) -> list:
        """
        Flattens the pipe expressions into a linear list of operator nodes.
        """
        return get_operator_chain(self._code)

    def to_dict(self) -> dict:
        """
        Exports the entire AST to a dictionary.
        """
        return node_to_dict(self.syntax)

    def to_json(self, indent: int = 2) -> str:
        """
        Exports the AST to a JSON string.
        """
        return json.dumps(self.to_dict(), indent=indent)

    def get_referenced_columns(self) -> set[str]:
        """
        Extracts all column names referenced in the query.
        """
        return get_referenced_columns(self._code)

    def get_structural_hash(self) -> str:
        """
        Generates a SHA256 hash representing the query's logical structure.
        """
        return get_structural_hash(self._code)

    def get_time_range(self) -> list[str]:
        """
        Extracts time-related literals and functions (ago, datetime, now).
        """
        return get_time_range(self._code)

    def replace_table(self, old_name: str, new_name: str) -> str:
        """
        Renames a table throughout the query and returns the new query string.
        """
        return replace_table(self._code, old_name, new_name)

    def __str__(self):
        return self.text
