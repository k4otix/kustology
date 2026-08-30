# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Start the CLR, load ``Kusto.Language``, and re-export its core types.

Importing this module has process-wide side effects, in order: CoreCLR is
initialized, the bundled ``Kusto.Language.dll`` is referenced, and .NET's
culture is pinned to invariant. See :func:`_pin_invariant_culture` for why
the pin is global and permanent. Downstream modules import from here, or
import this module for its side effects, so initialization precedes any use
of the CLR.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any

import pythonnet

logger = logging.getLogger(__name__)

# `_pin_invariant_culture` binds these at import so `ensure_invariant_culture`
# compares a cached object instead of importing `System.Globalization` per call.
_INVARIANT: Any = None
_OBJECT: Any = None
_CULTURE_TYPE: Any = None

# Use the `opt` symlink. A `Cellar/X.Y.Z/` path breaks on `brew upgrade`.
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
    """Test whether ``path`` holds a dotnet install with its ``host/fxr`` tree."""
    return path.is_dir() and (path / "host" / "fxr").is_dir()


def _candidate_dotnet_roots():
    for p in _HOMEBREW_OPT_PATHS + _SYSTEM_PATHS + _USER_PATHS:
        if _is_dotnet_root(p):
            yield p


def _load_runtime() -> None:
    """Initialize CoreCLR, probing known dotnet roots when the default fails.

    pythonnet defaults to Mono off-Windows, so coreclr is always requested
    explicitly. An explicit ``DOTNET_ROOT`` is honored with no fallback, so a
    host that pins a root fails loudly there. Otherwise the default load runs
    first, then each candidate root in turn. The probes cover Homebrew, whose
    ``libhostfxr.dylib`` lives under ``<dotnet>/libexec/host/fxr/``;
    ``clr_loader.find_dotnet_root()`` falls back to the parent of
    ``which dotnet`` and looks in ``<dotnet>/bin/host/fxr/``.
    """
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
    culture live at that moment rather than the one active during ``parse()``.
    Under a comma-decimal locale the decimal point reads as a group separator,
    so the fractional part is swallowed:

    * ``timespan`` — ``1.5h`` yields fifteen hours, ``2.25s`` three minutes
      forty-five; under ``fr-FR`` the parse fails to zero.
    * ``real`` — ``1.5`` yields ``15.0``, so ``| where CpuPct > 1.5`` becomes
      ten times too strict.
    * ``decimal`` — ``decimal(1.5)`` yields ``15``.

    Every fractional numeric literal kind is affected identically. The
    corruption happens inside caller code, arbitrarily far from any kustology
    call, so only a process-wide pin closes it.

    ``DefaultThreadCurrentCulture`` covers threads created after import, and
    ``CurrentThread.CurrentCulture`` covers the importing thread, which the
    default does not retroactively affect. ``CurrentUICulture`` is left alone
    because it selects exception and diagnostic message language, not value
    parsing.

    Importing kustology carries this process-global effect with no opt-out. An
    escape hatch would let a host silently reintroduce 10x and 100x duration
    errors.

    Residual risk: the pin runs once, at import, so a host that assigns
    ``CultureInfo.DefaultThreadCurrentCulture`` or
    ``Thread.CurrentThread.CurrentCulture`` afterwards, directly or through
    any other .NET-interop library in the same process, re-opens the
    corruption for every ``LiteralValue`` not yet read. The value is computed
    on first access and cached, so that includes literals in a tree parsed
    while the pin still held; only literals already read keep their correct
    value. Measured under a post-import switch to ``de-DE``, all three
    corruptions above reproduce on unread literals.

    :func:`ensure_invariant_culture` narrows that window.
    """
    from System import Object
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    global _INVARIANT, _OBJECT, _CULTURE_TYPE
    _INVARIANT = CultureInfo.InvariantCulture
    _OBJECT = Object
    _CULTURE_TYPE = CultureInfo

    CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture
    Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture


def ensure_invariant_culture() -> None:
    """Restore invariant culture on the calling thread if something changed it.

    Importing kustology pins .NET's culture to invariant. A host, or any other
    .NET-interop library in the same process, can assign over that pin, which
    corrupts every fractional numeric literal not yet read. See
    :func:`_pin_invariant_culture` for the mechanism and the measured values.

    Every kustology entry point calls this, so a query parsed and lowered
    through the library reads its literals under invariant culture whatever
    the host did in between. Call it yourself before reading ``LiteralValue``
    off a raw syntax node in a process where culture may have moved.

    The check is a reference comparison against the cached
    ``InvariantCulture`` singleton and assigns only when it fails, so the
    common case is one interop property read. A culture object that merely
    *equals* invariant is replaced too: a clone of invariant carrying a
    modified ``NumberFormat`` compares equal by name and parses differently.
    """
    if _INVARIANT is None:  # pragma: no cover - the bridge always initializes
        return

    from System.Threading import Thread

    thread = Thread.CurrentThread
    if not _OBJECT.ReferenceEquals(thread.CurrentCulture, _INVARIANT):
        thread.CurrentCulture = _INVARIANT
        _CULTURE_TYPE.DefaultThreadCurrentCulture = _INVARIANT


def _initialize_bridge() -> None:
    """Load the runtime, reference ``Kusto.Language.dll``, and pin culture.

    The pin comes last because it imports ``System.*``, which needs a live
    CLR; it still runs at import time, before anything can parse, so the
    first read of any ``LiteralValue`` happens under invariant culture.
    """
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

from Kusto.Language import GlobalState, KustoCode, KustoFacts
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
    "KustoFacts",
    "ScalarTypes",
    "TableSymbol",
]
