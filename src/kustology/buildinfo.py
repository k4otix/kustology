# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Describe what this install is made of, readable at runtime."""

from __future__ import annotations

import os
from typing import NamedTuple

from ._ir_tags import IR_SCHEMA_VERSION, SEMANTIC_HASH_SCHEME
from ._version import __version__


class BuildInfo(NamedTuple):
    """The library version, the bundled ``Kusto.Language.dll`` pin, and the two IR tags."""

    version: str
    kusto_language_version: str
    kusto_language_sha256: str
    ir_schema_version: str
    semantic_hash_scheme: str


def build_info() -> BuildInfo:
    """Return the library version, the bundled ``Kusto.Language.dll`` pin, and the two IR tags.

    These are the values a consumer should gate behaviour on.

    The IR tags are always reported. They describe the IR shape *this version
    of kustology* defines, which is a fact about the installed source and not
    about whether the ``[ir]`` extra brought pydantic along; both come from
    :mod:`kustology._ir_tags`, which imports nothing, so reading them here
    does not pull the IR into a Tier 1-only process.
    """
    pin = _read_pin()
    return BuildInfo(
        __version__, pin["version"], pin["sha256"], IR_SCHEMA_VERSION, SEMANTIC_HASH_SCHEME,
    )


def _read_pin() -> dict[str, str]:
    """Parse ``key=value`` lines from the bundled ``VERSION.txt`` pin file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "VERSION.txt")
    with open(path, encoding="utf-8") as fh:
        return dict(line.strip().split("=", 1) for line in fh if "=" in line)
