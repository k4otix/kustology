# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Python bindings over Microsoft's ``Kusto.Language`` library.

Importing this package starts the .NET runtime, loads the bundled
``Kusto.Language.dll``, and pins .NET's invariant culture process-wide —
see :mod:`kustology.bridge` for what that means for the host process.
"""

# Underscored: both names are machinery for computing `__version__`, and a
# plain import binds them into `kustology`'s namespace, where
# `kustology.PackageNotFoundError` reads as part of this library's API.
from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("kustology")
except _PackageNotFoundError:  # pragma: no cover — editable install without metadata
    __version__ = "0.0.0+unknown"

from .bridge import KustoCode
from .core import KustoQuery
from .reflection import (
    aggregate_functions,
    all_function_names,
    plugin_functions,
    scalar_functions,
    string_functions,
    syntax_kinds,
    time_functions,
)
from .services import format_query, parse, validate
from .utils.walker import iter_elements

__all__ = [
    # Version
    "__version__",
    # Tier 1 — thin wrapper
    "KustoCode",
    "KustoQuery",
    "parse",
    "format_query",
    "validate",
    "iter_elements",
    # Reflection — always available; reflects the loaded Kusto.Language.dll
    "time_functions",
    "aggregate_functions",
    "string_functions",
    "scalar_functions",
    "plugin_functions",
    "all_function_names",
    "syntax_kinds",
]
