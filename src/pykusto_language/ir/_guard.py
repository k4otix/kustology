# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Optional-dependency guard for the IR tier.

Runs before any pydantic import so base-install users get a clear error
pointing at the ``[ir]`` extra instead of a deep ``ModuleNotFoundError``.
"""

from __future__ import annotations


def _require_pydantic() -> None:
    try:
        import pydantic  # noqa: F401
    except ImportError as e:  # pragma: no cover — exercised by base-install CI
        raise ImportError(
            "pykusto_language.ir requires pydantic. Install with:\n"
            "    pip install 'pykusto-language[ir]'\n"
            "(or directly: pip install 'pydantic>=2.6.0')."
        ) from e
