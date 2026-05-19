# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import os
import sys
from pathlib import Path

import pythonnet


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
    except Exception:
        pass

    for root in _candidate_dotnet_roots():
        try:
            pythonnet.load("coreclr", dotnet_root=str(root))
            return
        except Exception:
            continue

    hint_paths = "\n  ".join(str(p) for p in _HOMEBREW_OPT_PATHS + _SYSTEM_PATHS)
    raise RuntimeError(
        "Failed to initialize the .NET runtime for kustology.\n"
        "Install .NET 8.0+ and either set DOTNET_ROOT or place dotnet at one of:\n"
        f"  {hint_paths}\n"
        "On macOS: `brew install dotnet` (auto-detected) or set "
        "DOTNET_ROOT=/opt/homebrew/opt/dotnet/libexec for Apple Silicon."
    )


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


_initialize_bridge()

from Kusto.Language import KustoCode, GlobalState  # noqa: E402
from Kusto.Language.Editor import KustoCodeService, FormattingOptions  # noqa: E402
from Kusto.Language.Symbols import (  # noqa: E402
    TableSymbol,
    ColumnSymbol,
    DatabaseSymbol,
    ScalarTypes,
)


__all__ = [
    "KustoCode",
    "GlobalState",
    "KustoCodeService",
    "FormattingOptions",
    "TableSymbol",
    "ColumnSymbol",
    "DatabaseSymbol",
    "ScalarTypes",
]
