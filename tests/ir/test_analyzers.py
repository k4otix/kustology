# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Analyzer-protocol smoke tests.

The protocol is intentionally minimal — these tests pin the wire shape
of ``Finding`` and exercise the canonical compose-by-iteration pattern
so it can't drift silently.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import (
    AnalyzerFn,
    BinOp,
    ColumnRef,
    Finding,
    Span,
    find_all,
    normalize_expressions,
)


def test_finding_round_trips_through_json():
    f = Finding(
        rule_id="kustology.test.rule",
        severity="warning",
        message="something",
        span=Span(text_start=0, width=10),
        extra={"k": "v"},
    )
    dumped = f.model_dump_json()
    back = Finding.model_validate_json(dumped)
    assert back == f


def test_finding_rejects_unknown_fields():
    """``extra="forbid"`` mirrors the rest of the IR — unknown wire fields fail."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Finding.model_validate({
            "rule_id": "x", "severity": "info", "message": "y", "rogue": True,
        })


def test_analyzer_protocol_composes():
    """The documented pattern: each analyzer returns an Iterable[Finding];
    callers chain them with a flat list comprehension. No registry, no
    base class — just function composition.
    """
    ir = parse(
        "DeviceProcessEvents "
        "| where tolower(FileName) == 'cmd.exe' "  # normalize_expressions folds to =~
        "| where AccountName == 'svc'"
    ).to_ir()
    # The IR is faithful by default — apply the opt-in normalize transform
    # so the case-insensitive analyzer below has something to match on.
    normalize_expressions(ir)

    def detect_case_insensitive_eq(qir) -> list[Finding]:
        return [
            Finding(
                rule_id="kustology.test.ci_eq",
                severity="info",
                message=f"{b.left.name} compared case-insensitively",
                span=b.span,
            )
            for b in find_all(qir, BinOp)
            if b.op == "=~" and isinstance(b.left, ColumnRef)
        ]

    def detect_literal_eq(qir) -> list[Finding]:
        return [
            Finding(
                rule_id="kustology.test.lit_eq",
                severity="info",
                message=f"{b.left.name} compared literally",
                span=b.span,
            )
            for b in find_all(qir, BinOp)
            if b.op == "==" and isinstance(b.left, ColumnRef)
        ]

    analyzers: list[AnalyzerFn] = [detect_case_insensitive_eq, detect_literal_eq]
    findings = [f for a in analyzers for f in a(ir)]
    rule_ids = sorted(f.rule_id for f in findings)
    assert rule_ids == ["kustology.test.ci_eq", "kustology.test.lit_eq"]
