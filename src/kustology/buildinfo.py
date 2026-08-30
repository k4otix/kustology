# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Describe what this install is made of, readable at runtime."""

from __future__ import annotations

import os
from typing import NamedTuple

from ._version import __version__


class BuildInfo(NamedTuple):
    """The library version, the bundled ``Kusto.Language.dll`` pin, and the two IR tags."""

    version: str
    kusto_language_version: str
    kusto_language_sha256: str
    ir_schema_version: str | None      # None without the [ir] extra
    semantic_hash_scheme: str | None


def build_info() -> BuildInfo:
    """Return the library version, the bundled ``Kusto.Language.dll`` pin, and the two IR tags.

    These are the values a consumer should gate behaviour on.
    """
    pin = _read_pin()
    schema: str | None = None
    scheme: str | None = None
    try:
        from . import ir
    except ImportError:
        pass
    else:
        schema, scheme = ir.IR_SCHEMA_VERSION, ir.SEMANTIC_HASH_SCHEME
    return BuildInfo(__version__, pin["version"], pin["sha256"], schema, scheme)


def _read_pin() -> dict[str, str]:
    """Parse ``key=value`` lines from the bundled ``VERSION.txt`` pin file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "VERSION.txt")
    with open(path, encoding="utf-8") as fh:
        return dict(line.strip().split("=", 1) for line in fh if "=" in line)
