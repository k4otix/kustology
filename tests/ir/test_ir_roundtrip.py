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

from kustology.ir import IRBuilder, QueryIR
from kustology.ir.binder import SchemaAttacher
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
    # let-declared functions. `LetFunction` holds a whole second IR --
    # parameters with their declared types and defaults, body-scoped bindings,
    # and a tail that is either a nested `Pipeline` or an `AnyExpr`. Each of
    # those is a recursive field reached only through this node, so a missing
    # `model_rebuild()` on any of them surfaces here and nowhere else.
    (
        "let Recent = (win:timespan=1h) { let cutoff = ago(win); "
        "DeviceProcessEvents | where TimeGenerated > cutoff }; Recent(2h)"
    ),
    "let Doubled = (n:long) { n * 2 }; DeviceProcessEvents | extend P = Doubled(ProcessId)",
    "let AllDevices = view () { DeviceProcessEvents | project DeviceId }; AllDevices()",
    # Parameter names and call-site names are alpha-canonicalized in the
    # digest and recorded verbatim in the IR, and this file pins the join
    # between those two facts: the reloaded copy must reproduce its own
    # `semantic_hash`, which it cannot if a parameter's `TypedNameDecl`, a
    # tabular parameter's body `TableRef` or a call site's name comes back
    # differently. Two parameters, one of them tabular, so the rename has to
    # be positional and has to reach both classes it lowers to.
    (
        "let Filtered = (Src:(*), n:long) { Src | where ProcessId > n }; "
        "Filtered(DeviceProcessEvents, 5) | take 1"
    ),
    # A call-site pair: source position and expression position in one query,
    # which are two IR classes built by two different paths.
    (
        "let Half = (n:long) { n / 2 }; "
        "let Rows = () { DeviceProcessEvents | take 1 }; "
        "Rows() | extend H = Half(ProcessId)"
    ),
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

    assert ir.semantic_hash == reloaded.semantic_hash
    assert ir.model_dump() == reloaded.model_dump(), (
        f"round-trip drift for query: {query!r}"
    )


def test_dumps_carrying_removed_fields_are_rejected():
    """``extra="forbid"`` makes the removal visible instead of silent.

    ``QueryIR.parse_warnings``, ``Span.source_text`` and ``Expr.nullable``
    were declared and never populated by any code path -- the first two by
    nothing at all, the third by a probe naming a .NET member that does not
    exist. A stored dump written by an older release carries them, and must
    fail to load rather than quietly dropping data. That is what the
    IR_SCHEMA_VERSION bump is for.
    """
    import json

    import pytest
    from pydantic import ValidationError

    from kustology.ir import IRBuilder, QueryIR

    ir = IRBuilder().build("DeviceProcessEvents | where FileName == 'a.exe'")
    payload = json.loads(ir.model_dump_json())

    # Round-trips cleanly as written today.
    assert QueryIR.model_validate(payload).semantic_hash == ir.semantic_hash

    for mutate in (
        lambda p: p.update(parse_warnings=[]),
        lambda p: p["main_pipeline"]["source"]["span"].update(source_text="T"),
        lambda p: p["main_pipeline"]["operators"][0]["predicate"].update(nullable=True),
    ):
        stale = json.loads(ir.model_dump_json())
        mutate(stale)
        with pytest.raises(ValidationError):
            QueryIR.model_validate(stale)


def test_dumps_missing_a_field_added_this_release_are_rejected():
    """The other direction: a required field the older shape did not have.

    ``extra="forbid"`` rejects a dump carrying a field that no longer
    exists, which the test above pins. It says nothing about a dump written
    before a field was *added* -- that one is short a key, not carrying a
    spare, and only the field being required (no pydantic default) makes it
    fail. WS4 added several such fields, and ``SortKey`` is the sharpest
    case: ``SortOp.expressions`` used to be a ``list[AnyExpr]`` holding the
    bare ordering expression, and is now a ``list[SortKey]`` wrapping it as
    ``expression`` alongside a required ``direction``.

    That shape change matters precisely because the old dump is *lossy* --
    it predates the model recording which way the rows come back. Loading it
    by treating the missing direction as some default would invent the half
    of the query the old builder threw away, and reproduce the collision
    ``SortKey`` exists to close. So it must fail loudly instead, and the
    error has to name the field a reader would have to add.

    Built by demoting a real dump rather than by hand, so it stays a
    genuine 0.2-dev payload: ``expressions[0]`` is replaced with the very
    expression node the current shape carries inside it.
    """
    import json

    import pytest
    from pydantic import ValidationError

    from kustology.ir import IRBuilder, QueryIR

    ir = IRBuilder().build("DeviceProcessEvents | sort by TimeGenerated desc")
    payload = json.loads(ir.model_dump_json())
    sort_op = payload["main_pipeline"]["operators"][0]
    assert sort_op["kind"] == "sort", sort_op["kind"]
    # Sanity: the current shape wraps the expression in a SortKey that
    # states the direction. Asserting the non-default value here is what
    # makes the demotion below meaningful.
    assert sort_op["expressions"][0]["kind"] == "sort_key"
    assert sort_op["expressions"][0]["direction"] == "desc"

    # The 0.2-dev shape: the ordering expression sat directly in the list.
    sort_op["expressions"] = [sort_op["expressions"][0]["expression"]]

    with pytest.raises(ValidationError) as excinfo:
        QueryIR.model_validate_json(json.dumps(payload))

    # `Pipeline.operators` is a discriminated union (on `kind`), so pydantic
    # picks the `SortOp` member by its `"sort"` tag directly instead of
    # trying every member and reporting a branch of noise per failed one --
    # the tag value itself shows up as a `loc` segment in place of the old
    # per-member class-name segment. Matching on the leaf name alone would
    # also accept a `missing` on some unrelated member's own `expression`
    # field, so the path is still checked in full.
    missing = {
        ".".join(str(part) for part in err["loc"])
        for err in excinfo.value.errors()
        if err["type"] == "missing"
    }
    named = sorted(
        loc for loc in missing if ".sort." in loc and loc.endswith(".expression")
    )
    assert named, (
        f"the ValidationError must name the field the old dump lacks, by "
        f"path, so a reader knows what to add; the missing-field paths it "
        f"reported were {sorted(missing)}"
    )
