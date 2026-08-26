# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from enum import Enum

try:
    from enum import StrEnum  # type: ignore[attr-defined]  # py3.11+
except ImportError:
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        def __str__(self) -> str:
            return str(self.value)

        def __format__(self, format_spec: str) -> str:
            return str(self.value).__format__(format_spec)


class KustoType(StrEnum):
    """Kusto type names as wire strings.

    ``str(KustoType.LONG)`` is ``"long"``, not ``"KustoType.LONG"``, so
    members compare, format, and serialize as the KQL spelling.
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
    # The binder hasn't placed this expression's type yet — distinct from
    # ``UnknownExpr`` (a *shape* the IR builder couldn't model) and from
    # the IR-internal ``"unknown"`` string used as a placeholder when an
    # ``Assignment.expr.result_type`` couldn't be resolved.
    UNRESOLVED = "unresolved"
