# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Run every ``src`` module's doctest examples as a standing gate.

Rendered prose is not otherwise checked: a docstring can drift from what it
shows and nothing red-flags it. This caught exactly that once already --
marking a docstring raw for ruff's D301 (a raw string escapes nothing)
doubled the backslashes in a handful of docstrings that already escaped
theirs for a plain string, and one of the doubled examples
(``utf16_to_codepoint``'s) silently stopped exercising the surrogate-pair
offset it exists to demonstrate. Running every module's doctests keeps a
repeat from passing silently.

Modules are found with ``grep -rl '>>>' src/kustology --include='*.py'``.
``kustology.ir.walk`` is not included: its ``Example:`` blocks use ``ir`` as
a stand-in for "a :class:`~kustology.ir.query.QueryIR` you already have" and
``...`` as a placeholder loop body, neither of which is a name or a
complete statement -- they read as ``>>>``-prefixed prose, not as examples
doctest can run, and forcing them to execute would mean building a fixture
this gate has no other reason to need. The two modules gated here need no
schema and no pydantic: both stay on the Tier 1, pydantic-free import path.
"""

from __future__ import annotations

import doctest

import pytest

import kustology._text
import kustology.utils.walker

_MODULES = [kustology._text, kustology.utils.walker]


@pytest.mark.parametrize("module", _MODULES, ids=[m.__name__ for m in _MODULES])
def test_module_doctests_pass(module):
    results = doctest.testmod(module, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest failure(s) in {module.__name__}"
    assert results.attempted > 0, f"no doctest examples collected in {module.__name__}"
