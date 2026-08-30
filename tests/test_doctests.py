# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Run every ``src`` module's doctest examples as a standing gate.

Nothing else checks that a docstring's examples still run. Escaping is the
quiet way they break: marking a docstring raw for ruff's D301 doubles the
backslashes in one that already escaped them for a plain string, so an example
such as ``utf16_to_codepoint``'s surrogate-pair offset still reads as correct
while exercising nothing.

Modules come from ``grep -rl '>>>' src/kustology --include='*.py'``.
``kustology.ir.walk`` is out: its ``Example:`` blocks use ``ir`` as a stand-in
for "a :class:`~kustology.ir.query.QueryIR` you already have" and ``...`` as a
placeholder loop body, so they are ``>>>``-prefixed prose that doctest cannot
run, and executing them would mean a fixture this gate has no other reason to
need. The two modules gated here need no schema and no pydantic, so both stay
on the Tier 1 import path.
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
