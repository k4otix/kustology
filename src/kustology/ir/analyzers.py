# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Minimal analyzer shape for IR-driven static analysis.

An *analyzer* is a function that walks a :class:`kustology.ir.QueryIR` and
yields zero or more :class:`Finding` instances. That's it — no class
hierarchy, no registration machinery, no rule engine. The point of
declaring this shape now is to give the v2 analyzer ecosystem a common
``Finding`` vocabulary so independently-developed analyzers compose.

Example:
-------

.. code-block:: python

    from kustology import parse
    from kustology.ir import (
        BinOp, ColumnRef, Finding, FilterOp, find_all,
    )

    def detect_case_insensitive_equality(ir) -> list[Finding]:
        # Flag every ``X =~ "y"`` (case-insensitive equality) on a column.
        findings = []
        for binop in find_all(ir, BinOp):
            if binop.op == "=~" and isinstance(binop.left, ColumnRef):
                findings.append(Finding(
                    rule_id="kustology.case_insensitive_eq",
                    severity="info",
                    span=binop.span,
                    message=f"{binop.left.name} compared case-insensitively",
                ))
        return findings

    ir = parse("DeviceProcessEvents | where tolower(FileName) == 'cmd.exe'").to_ir()
    # After normalize_expressions, the tolower== folds to =~. Run the analyzer.

The :class:`Finding` shape is minimal. ``extra`` exists for
rule-specific structured data without forcing the core schema to evolve
every time a new analyzer wants a side-channel field.

"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel

from .query import QueryIR
from .spans import Span

Severity = Literal["info", "warning", "error"]


class Finding(BaseModel):
    """One analyzer hit. Stable wire shape — minor versions add fields only.

    ``span`` is optional because some findings are project-wide (for example,
    "no time filter found anywhere in the query") and don't anchor to a
    single source location. Analyzer authors should populate it when
    they can — surfacing a precise span turns a finding from a label
    into something an IDE can highlight.
    """

    model_config = {"extra": "forbid"}

    rule_id: str
    severity: Severity
    message: str
    span: Span | None = None
    extra: dict[str, Any] = {}


AnalyzerFn = Callable[[QueryIR], Iterable[Finding]]
"""Type alias for a function-shaped analyzer.

Composition: combine analyzers by chaining their outputs —

.. code-block:: python

    def run_all(ir: QueryIR, analyzers: list[AnalyzerFn]) -> list[Finding]:
        return [f for a in analyzers for f in a(ir)]
"""
