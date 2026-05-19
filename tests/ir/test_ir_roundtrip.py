# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Parametric IR JSON round-trip.

For each query in a representative corpus:
  build → enrich → model_dump_json → model_validate_json → deep-equal check.

Any drift between the in-memory model and the reloaded copy is a serialization
bug — usually a missing ``model_rebuild()`` on a recursive field or a default
value that differs between construction and deserialization.
"""

from __future__ import annotations

import pytest

from kustology.ir import IRBuilder, QueryIR, SchemaAttacher
from kustology.utils.analysis import build_global_state

QUERIES = [
    # Plain filters.
    "DeviceProcessEvents | where FileName == 'cmd.exe'",
    "DeviceProcessEvents | where FileName != 'powershell.exe'",
    # Boolean expressions.
    "DeviceProcessEvents | where A == 1 and B == 2",
    "DeviceProcessEvents | where A == 1 or B == 2",
    "DeviceProcessEvents | where (A == 1 or B == 2) and C == 3",
    # Case manipulation rewrite — exercises tolower== fold.
    "DeviceProcessEvents | where tolower(FileName) == 'cmd.exe'",
    "DeviceProcessEvents | where tolower(ProcessCommandLine) != 'foo'",
    # Consecutive filters — must round-trip after merge.
    "DeviceProcessEvents | where FileName == 'cmd.exe' | where ProcessCommandLine has 'enc'",
    # Joins / lookup with kind= parameters.
    "DeviceProcessEvents | join kind=inner DeviceFileEvents on DeviceId",
    "DeviceProcessEvents | join kind=leftouter DeviceFileEvents on DeviceId",
    "DeviceProcessEvents | lookup kind=leftouter DeviceFileEvents on DeviceId",
    # Aggregations.
    "DeviceProcessEvents | summarize Count = count() by FileName",
    "DeviceProcessEvents | summarize Count = dcount(AccountName), N = countif(FileName == 'cmd.exe') by DeviceName",
    # Projections.
    "DeviceProcessEvents | project FileName, AccountName, TimeGenerated",
    "DeviceProcessEvents | project-rename Account = AccountName",
    "DeviceProcessEvents | distinct AccountName",
    # Set membership + between.
    "DeviceProcessEvents | where FileName in ('cmd.exe', 'powershell.exe', 'wscript.exe')",
    "DeviceProcessEvents | where TimeGenerated between (ago(1d) .. now())",
    # Time / functions.
    "DeviceProcessEvents | where TimeGenerated > ago(1h)",
    "DeviceProcessEvents | extend Lower = tolower(FileName), Up = toupper(FileName)",
    # let-bindings.
    "let cutoff = ago(1h); DeviceProcessEvents | where TimeGenerated > cutoff",
    "let allowlist = dynamic(['svc_a','svc_b']); DeviceProcessEvents | where AccountName !in (allowlist)",
    # Union / sub-pipelines.
    "union DeviceProcessEvents, DeviceFileEvents | where FileName == 'cmd.exe'",
    # take / top / sort / search.
    "DeviceProcessEvents | take 10",
    "DeviceProcessEvents | top 5 by TimeGenerated",
    "DeviceProcessEvents | sort by TimeGenerated desc",
    "DeviceProcessEvents | search 'cmd.exe'",
    # count / print operators.
    "DeviceProcessEvents | count",
    "DeviceProcessEvents | count as Total",
    "print x = 1, y = tolower('AB')",
    # getschema / consume / serialize operators.
    "DeviceProcessEvents | getschema",
    "DeviceProcessEvents | consume",
    "DeviceProcessEvents | serialize x = 1",
    # case / iif / isnotnull / isnotempty lifts.
    "DeviceProcessEvents | extend tag = iif(FileName == 'cmd.exe', 'shell', 'other')",
    "DeviceProcessEvents | extend tag = case(FileName == 'cmd.exe', 'shell', FileName == 'pwsh.exe', 'shell', 'other')",
    "DeviceProcessEvents | where isnotnull(FileName)",
    "DeviceProcessEvents | where isnotempty(FileName)",
    # matches regex.
    "DeviceProcessEvents | where FileName matches regex '^cmd.*\\\\.exe$'",
    # `not()` lift.
    "DeviceProcessEvents | where not(FileName == 'cmd.exe')",
    # Function-call-as-source in union branches.
    "union findAnomalies('foo'), findAnomalies('bar')",
    # cluster() / database() qualified sources.
    'cluster("c").database("d").DeviceProcessEvents | take 10',
    'database("d").DeviceProcessEvents | where FileName == "cmd.exe"',
    # Scope-shaping operators (project / project-away / project-keep / project-reorder / parse / mv-expand).
    "DeviceProcessEvents | project FileName, AccountName | where FileName == 'cmd.exe'",
    "DeviceProcessEvents | project-away DeviceName, TimeGenerated",
    "DeviceProcessEvents | project-keep FileName, AccountName",
    "DeviceProcessEvents | project-reorder TimeGenerated, FileName",
    "DeviceProcessEvents | parse FileName with 'prefix_' UserName '_suffix'",
    "DeviceProcessEvents | extend Items = pack_array(FileName, AccountName) | mv-expand Items",
]


@pytest.fixture(scope="module")
def builder(sample_schema):
    gs = build_global_state(sample_schema)
    return IRBuilder(global_state=gs)


@pytest.fixture(scope="module")
def attacher(sample_schema):
    return SchemaAttacher(sample_schema)


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q[:60])
def test_ir_roundtrip(builder, attacher, query):
    ir = builder.build(query)
    attacher.enrich(ir)

    dumped = ir.model_dump_json()
    reloaded = QueryIR.model_validate_json(dumped)

    assert ir.structural_hash == reloaded.structural_hash
    assert ir.model_dump() == reloaded.model_dump(), (
        f"round-trip drift for query: {query!r}"
    )
