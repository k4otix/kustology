# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Python bindings over Microsoft's ``Kusto.Language`` library.

Importing this package starts the .NET runtime, loads the bundled
``Kusto.Language.dll``, and pins .NET's invariant culture process-wide. See
:mod:`kustology.bridge` for what that means for the host process.
"""

from ._text import codepoint_to_utf16, utf16_to_codepoint
from ._version import __version__
from .bridge import KustoCode, ensure_invariant_culture
from .buildinfo import BuildInfo, build_info
from .core import KustoQuery
from .lexical import Token
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
from .spans import TextSpan, TimeExpr
from .utils.walker import iter_elements

__all__ = [
    # Version
    "__version__",
    "BuildInfo",
    "build_info",
    # Tier 1 — thin wrapper
    "KustoCode",
    "KustoQuery",
    "parse",
    "format_query",
    "validate",
    "iter_elements",
    # Spans
    "TextSpan",
    "TimeExpr",
    "Token",
    # Offset translation. .NET reports UTF-16 code units, Python indexes code
    # points; see :mod:`kustology._text`.
    "utf16_to_codepoint",
    "codepoint_to_utf16",
    # Culture. Importing pins .NET to invariant; this repairs a host that
    # assigned over the pin. See :func:`kustology.bridge._pin_invariant_culture`.
    "ensure_invariant_culture",
    # Reflection over the loaded Kusto.Language.dll; always available
    "time_functions",
    "aggregate_functions",
    "string_functions",
    "scalar_functions",
    "plugin_functions",
    "all_function_names",
    "syntax_kinds",
]
