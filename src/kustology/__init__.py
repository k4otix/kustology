# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__: str = _pkg_version("kustology")
except PackageNotFoundError:  # pragma: no cover — editable install without metadata
    __version__ = "0.0.0+unknown"

from .bridge import KustoCode
from .core import KustoQuery
from .reflection import (
    aggregate_functions,
    all_function_names,
    scalar_functions,
    string_functions,
    syntax_kinds,
    time_functions,
)
from .services import format_query, parse, validate

__all__ = [
    # Version
    "__version__",
    # Tier 1 — thin wrapper
    "KustoCode",
    "KustoQuery",
    "parse",
    "format_query",
    "validate",
    # Reflection — always available; reflects the loaded Kusto.Language.dll
    "time_functions",
    "aggregate_functions",
    "string_functions",
    "scalar_functions",
    "all_function_names",
    "syntax_kinds",
]
