# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Shared test setup for kustology.

Mirrors the bridge's DOTNET_ROOT cascade so `pytest` works on a fresh clone
without requiring users to set DOTNET_ROOT manually. The bridge's own cascade
runs at import time, so this module only sets DOTNET_ROOT when the user has not
set one and the auto-detection would otherwise probe a non-default location.
"""

from __future__ import annotations

import os
from pathlib import Path

_HOMEBREW_OPT_PATHS = [
    Path("/opt/homebrew/opt/dotnet/libexec"),
    Path("/usr/local/opt/dotnet/libexec"),
]

_SYSTEM_PATHS = [
    Path("/usr/share/dotnet"),
    Path("/usr/local/share/dotnet"),
]


def _maybe_set_dotnet_root() -> None:
    if os.environ.get("DOTNET_ROOT"):
        return
    for candidate in _HOMEBREW_OPT_PATHS + _SYSTEM_PATHS:
        if (candidate / "host" / "fxr").is_dir():
            os.environ["DOTNET_ROOT"] = str(candidate)
            return


_maybe_set_dotnet_root()
