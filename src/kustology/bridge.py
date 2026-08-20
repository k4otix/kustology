# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import logging
import os
import sys
from pathlib import Path

import pythonnet

logger = logging.getLogger(__name__)

_HOMEBREW_OPT_PATHS = [
    Path("/opt/homebrew/opt/dotnet/libexec"),
    Path("/usr/local/opt/dotnet/libexec"),
]

_SYSTEM_PATHS = [
    Path("/usr/share/dotnet"),
    Path("/usr/local/share/dotnet"),
]

_USER_PATHS = [Path.home() / ".dotnet"]


def _is_dotnet_root(path: Path) -> bool:
    return path.is_dir() and (path / "host" / "fxr").is_dir()


def _candidate_dotnet_roots():
    for p in _HOMEBREW_OPT_PATHS + _SYSTEM_PATHS + _USER_PATHS:
        if _is_dotnet_root(p):
            yield p


def _load_runtime() -> None:
    if pythonnet.get_runtime_info():
        return

    if os.environ.get("DOTNET_ROOT"):
        pythonnet.load("coreclr")
        return

    try:
        pythonnet.load("coreclr")
        return
    except Exception as e:
        logger.debug("coreclr load without an explicit root failed: %s", e)

    for root in _candidate_dotnet_roots():
        try:
            pythonnet.load("coreclr", dotnet_root=str(root))
            return
        except Exception as e:
            logger.debug("coreclr load from %s failed: %s", root, e)
            continue

    hint_paths = "\n  ".join(
        str(p) for p in _HOMEBREW_OPT_PATHS + _SYSTEM_PATHS + _USER_PATHS
    )
    raise RuntimeError(
        "Failed to initialize the .NET runtime for kustology.\n"
        "Install .NET 8.0+ and either set DOTNET_ROOT or place dotnet at one of:\n"
        f"  {hint_paths}\n"
        "On macOS: `brew install dotnet` (auto-detected) or set "
        "DOTNET_ROOT=/opt/homebrew/opt/dotnet/libexec for Apple Silicon."
    )


def _pin_invariant_culture() -> None:
    """Pin .NET's culture to invariant, process-wide, before any parsing.

    Kusto's ``LiteralValue`` is evaluated lazily on property access, using the
    culture live at that moment — not the one active during ``parse()``. Under
    ``de-DE`` the decimal point is read as a group separator, so ``1.5h``
    yields fifteen hours and ``2.25s`` yields three minutes forty-five; under
    ``fr-FR`` the parse fails to zero. Because the corruption happens inside
    caller code, arbitrarily far from any kustology call, a pin scoped around
    our own entry points would not close it — only a process-wide pin does.

    ``DefaultThreadCurrentCulture`` covers threads created after import;
    ``CurrentThread.CurrentCulture`` covers the importing thread, which the
    default does not retroactively affect. ``CurrentUICulture`` is deliberately
    left alone: it selects exception and diagnostic message language, not value
    parsing.

    This is a deliberate process-global effect of importing kustology, with no
    opt-out. An escape hatch would let a host silently reintroduce 10x and 100x
    duration errors, which is worse than the co-tenancy cost it would avoid.
    """
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture
    Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture


def _initialize_bridge() -> None:
    _load_runtime()

    import clr

    base_dir = os.path.dirname(os.path.abspath(__file__))
    bin_dir = os.path.join(base_dir, "bin")

    if bin_dir not in sys.path:
        sys.path.append(bin_dir)

    try:
        clr.AddReference("Kusto.Language")
    except Exception as e:
        raise ImportError(
            f"Could not load Kusto.Language assembly. "
            f"Ensure Kusto.Language.dll is in {bin_dir}. Error: {e}"
        ) from e

    _pin_invariant_culture()


_initialize_bridge()

from Kusto.Language import GlobalState, KustoCode
from Kusto.Language.Editor import FormattingOptions, KustoCodeService
from Kusto.Language.Symbols import (
    ColumnSymbol,
    DatabaseSymbol,
    FunctionSymbol,
    ScalarTypes,
    TableSymbol,
)

__all__ = [
    "ColumnSymbol",
    "DatabaseSymbol",
    "FormattingOptions",
    "FunctionSymbol",
    "GlobalState",
    "KustoCode",
    "KustoCodeService",
    "ScalarTypes",
    "TableSymbol",
]
