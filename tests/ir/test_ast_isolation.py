# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""IR isolation contract.

The whole point of the IR layer is that downstream consumers (analyzers, API
serialization, UI renderers) never touch the .NET runtime. If a pydantic
model accidentally carries a ``System.Object`` reference, JSON serialization
explodes at runtime and tests get flaky in non-obvious ways. The contract:

1. Every field in the built IR is a primitive, list, dict, pydantic model,
   or a known enum. Nothing else.
2. ``model_dump_json`` succeeds and round-trips back to an equal hash.
"""

from __future__ import annotations

import pytest

from pykusto_language.ir import (
    Expr,
    IRBuilder,
    KustoType,
    LetBinding,
    LetRef,
    Operator,
    Pipeline,
    QueryIR,
    Span,
    TableRef,
    UnknownSource,
)

CORPUS = [
    "DeviceProcessEvents | where FileName == 'cmd.exe'",
    "DeviceProcessEvents | where tolower(FileName) == 'cmd.exe'",
    "DeviceProcessEvents | where FileName == 'cmd.exe' | where ProcessCommandLine has 'enc'",
    "DeviceProcessEvents | join kind=inner DeviceFileEvents on DeviceId",
    # Parenthesized RHS — visitor must unwrap, or the right pipeline gets UnknownSource.
    "DeviceProcessEvents | join (SecurityEvent) on $left.ProcessId == $right.ProcessId",
    "DeviceProcessEvents | join (SecurityEvent | where TimeGenerated > ago(1h)) on $left.DeviceName == $right.Computer",
    "DeviceProcessEvents | lookup kind=leftouter DeviceFileEvents on DeviceId",
    "DeviceProcessEvents | summarize Count = count() by FileName",
    "DeviceProcessEvents | extend Lower = tolower(FileName)",
    "DeviceProcessEvents | where FileName in ('cmd.exe', 'powershell.exe')",
    "DeviceProcessEvents | where TimeGenerated between (ago(1d) .. now())",
    "DeviceProcessEvents | project FileName, AccountName",
    "DeviceProcessEvents | top 5 by TimeGenerated",
    "let cutoff = ago(1h); DeviceProcessEvents | where TimeGenerated > cutoff",
    "union DeviceProcessEvents, DeviceFileEvents | where FileName == 'cmd.exe'",
    "DeviceProcessEvents | mv-expand FileName",
    "DeviceProcessEvents | search 'cmd.exe'",
    "DeviceProcessEvents | distinct AccountName",
]


@pytest.fixture(scope="module")
def builder():
    return IRBuilder()


ALLOWED_PYDANTIC_BASES = (
    Expr, Operator, Pipeline, QueryIR, TableRef, LetRef, UnknownSource, LetBinding, Span,
)


def _is_allowed_scalar(value) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, KustoType))


def _walk_for_dotnet(obj, path: str, problems: list):
    """Recurse over every attribute reachable from `obj`. Anything that isn't
    a Python primitive, pydantic model, list/tuple, dict, or known enum is a
    .NET leak — flag with its path."""
    if _is_allowed_scalar(obj):
        return
    if isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _walk_for_dotnet(item, f"{path}[{i}]", problems)
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_for_dotnet(v, f"{path}[{k!r}]", problems)
        return
    cls = type(obj)
    if hasattr(cls, "model_fields") or isinstance(obj, ALLOWED_PYDANTIC_BASES):
        for fname in cls.model_fields:
            _walk_for_dotnet(getattr(obj, fname, None), f"{path}.{fname}", problems)
        return
    problems.append(f"{path}: unexpected type {type(obj).__module__}.{type(obj).__name__}")


@pytest.mark.parametrize("query", CORPUS, ids=lambda q: q[:50])
def test_ir_has_no_dotnet_refs(builder, query):
    ir = builder.build(query)
    problems: list[str] = []
    _walk_for_dotnet(ir, "ir", problems)
    assert not problems, "IR carries non-Python values:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("query", CORPUS, ids=lambda q: q[:50])
def test_ir_serializes_cleanly(builder, query):
    """Cheaper smoke test: model_dump_json must succeed and round-trip back to an equal hash."""
    ir = builder.build(query)
    dumped = ir.model_dump_json()
    reloaded = QueryIR.model_validate_json(dumped)
    assert ir.structural_hash == reloaded.structural_hash
