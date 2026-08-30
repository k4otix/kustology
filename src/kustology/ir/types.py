# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Define ``KustoType``, the enum of Kusto scalar type names."""

from enum import Enum

try:
    from enum import StrEnum  # type: ignore[attr-defined]  # py3.11+
except ImportError:
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Back-port of :class:`enum.StrEnum` for Python versions before 3.11."""

        def __str__(self) -> str:
            """Return the member's string value."""
            return str(self.value)

        def __format__(self, format_spec: str) -> str:
            """Format the member's string value under ``format_spec``."""
            return str(self.value).__format__(format_spec)


class KustoType(StrEnum):
    """Kusto type names as wire strings.

    ``str(KustoType.LONG)`` is ``"long"``, so members compare, format, and
    serialize as the KQL spelling.
    """

    BOOL = "bool"
    INT = "int"
    LONG = "long"
    REAL = "real"
    DECIMAL = "decimal"
    DATETIME = "datetime"
    TIMESPAN = "timespan"
    GUID = "guid"
    STRING = "string"
    DYNAMIC = "dynamic"
    TABULAR = "tabular"
    # The binder hasn't placed this expression's type yet. Distinct from
    # ``UnknownExpr`` (a *shape* the builder couldn't model) and from the
    # IR-internal ``"unknown"`` placeholder for an unresolved ``Assignment.expr.result_type``.
    UNRESOLVED = "unresolved"
