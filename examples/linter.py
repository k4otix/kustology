# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""A working KQL linter in ~100 lines, built on the IR.

An *analyzer* in kustology is just a function that takes a ``QueryIR`` and
yields :class:`~kustology.ir.Finding` objects — no base class, no
registration, no rule engine. ``AnalyzerFn`` is the type alias; composing
analyzers means concatenating their outputs. This file defines three of
them plus one that surfaces the binder's own opinion, and runs all four
over three queries.

Each rule is a different shape of question:

* **no-time-filter** — a *whole-query* question, answered with tier 1's
  ``find_time_expressions()``. There is no single node to point at, so the
  finding carries no span, which is exactly why ``Finding.span`` is
  optional. What that absence costs depends on where the query runs; see
  ``no_time_filter`` below.
* **contains-vs-has** — a *node* question: ``BinOp`` with ``op="contains"``
  against a string literal. ``has`` matches whole terms and can use the
  term index; ``contains`` is a substring scan and cannot. Found with
  ``find_all``, so it fires inside join sub-pipelines and ``let`` bodies
  too, not just the main pipeline.
* **wildcard-selection** — a *node* question one level up:
  ``StarExpr`` anywhere. ``search *`` scans every table in the workspace;
  ``project-keep *`` keeps every column and is a no-op.
* **semantic diagnostics** — not our opinion at all. ``validate(q,
  schema=...)`` runs Microsoft's binder, which is the only thing that can
  tell a typo'd column from a real one. Lifted into the same ``Finding``
  vocabulary so a caller handles one list.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

from collections.abc import Iterable

from _display import banner, kql, note, section, severity, table, takeaway

from kustology import parse, validate
from kustology.ir import (
    BinOp,
    Finding,
    LiteralExpr,
    QueryIR,
    Severity,
    StarExpr,
    find_all,
)

SCHEMA = {
    "SigninLogs": {
        "TimeGenerated": "datetime",
        "UserPrincipalName": "string",
        "ResultType": "string",
        "IPAddress": "string",
    }
}

# Title, query, and the point the query makes. The commentary sits in the
# data so the run explains itself.
QUERIES = [
    (
        "Three IR rules fire, the binder stays quiet",
        'SigninLogs | where UserPrincipalName contains "admin" | project-keep *',
        ("The query binds cleanly, so the semantic rule finds nothing. The "
         "rules in this file and the binder's answer are independent, and a "
         "query can pass one while failing the other."),
    ),
    (
        "A typo only the binder can see",
        'SigninLogs | where TimeGenerated > ago(1d) | where ResultTypo == "0"',
        ("`ResultTypo` is a valid identifier, so the parser accepts it. "
         "Resolving it against the schema is what turns it into an error."),
    ),
    (
        "Clean under all four rules",
        ('SigninLogs | where TimeGenerated > ago(1d) '
         '| where UserPrincipalName has "admin" | project UserPrincipalName'),
        "A time filter, an indexed string match, and a named projection.",
    ),
]

# `has` matches whole terms against the term index; `contains` is an
# unindexed substring scan. Both case variants belong here and each has its
# own indexed counterpart — `has` for `contains`, `has_cs` for
# `contains_cs`. The negated forms (`!contains`, `!contains_cs`) are left
# out deliberately: `!has` is not equivalent, so there is no drop-in
# replacement to suggest.
_UNINDEXED_STRING_OPS = {"contains", "contains_cs"}

# What supplies the time bound when the query text does not.
_TIME_BOUND_BY_CONTEXT = [
    ["Azure Data Explorer", "Nothing. The query reads the table's full retention."],
    ["Log Analytics and Sentinel logs",
     ("The time picker, which starts at the last 24 hours and reads "
      "'Set in query' once you filter the standard time column.")],
    ["Sentinel hunting",
     ("The range the analyst selects, injected at run time. Microsoft's "
      "guidance is to leave the filter out of a hunting query.")],
    ["azure-monitor-query for Python",
     ("The `timespan` argument, which is required. Passing None runs the "
      "query unbounded.")],
]


def no_time_filter(ir: QueryIR) -> Iterable[Finding]:
    """Flag a query with no temporal expression anywhere.

    Whole-query, so it reparses the recorded source rather than walking
    nodes. ``QueryIR.raw_text`` is the query as the builder saw it. A
    finding with no span is the honest shape: the defect is the *absence*
    of something, which has no location.

    The message stops at the absence because that is all the text can
    prove. A time bound can arrive from outside it, and whether one does
    depends on the caller. ``_TIME_BOUND_BY_CONTEXT`` above lists what
    common callers supply. Rank or drop this rule for the context you lint
    for.
    """
    if parse(ir.raw_text).find_time_expressions():
        return
    yield Finding(
        rule_id="example.no_time_filter",
        severity="warning",
        message="No time filter in the query text; any time bound comes "
                "from the caller.",
    )


def contains_where_has_would_index(ir: QueryIR) -> Iterable[Finding]:
    """Flag ``col contains "literal"`` — ``has`` would use the term index."""
    for node in find_all(ir, BinOp):
        if node.op not in _UNINDEXED_STRING_OPS:
            continue
        if not isinstance(node.right, LiteralExpr):
            continue
        if node.right.literal_kind != "string":
            continue
        indexed = "has" if node.op == "contains" else "has_cs"
        yield Finding(
            rule_id="example.contains_vs_has",
            severity="info",
            span=node.span,
            message=f"`{node.op}` is an unindexed scan; "
                    f"`{indexed}` matches whole terms via the index.",
            extra={"operator": node.op, "suggested": indexed},
        )


def wildcard_selection(ir: QueryIR) -> Iterable[Finding]:
    """Flag any ``*`` in a column or table position.

    One node type covers the family: ``search *``, ``project-keep *``,
    ``project-away *``, ``project-reorder *`` and ``arg_max(t, *)`` all
    build a ``StarExpr``. (``project *`` is not in this list because KQL
    does not accept it — the parser reports KS198.)
    """
    for node in find_all(ir, StarExpr):
        yield Finding(
            rule_id="example.wildcard_selection",
            severity="warning",
            span=node.span,
            message="Wildcard selection reads every column (and, under "
                    "`search *`, every table).",
        )


ANALYZERS = [no_time_filter, contains_where_has_would_index, wildcard_selection]

_DIAGNOSTIC_SEVERITY: dict[str, Severity] = {
    "Error": "error", "Warning": "warning", "Suggestion": "info",
}


def semantic_findings(query: str, schema: dict) -> list[Finding]:
    """Lift Microsoft's binder diagnostics into the ``Finding`` vocabulary.

    A schema is required: without one, ``validate`` checks syntax only and
    a typo'd column name is a perfectly valid identifier.
    """
    return [
        Finding(
            rule_id=f"kusto.{d['code']}",
            severity=_DIAGNOSTIC_SEVERITY.get(d["severity"], "info"),
            span={"text_start": d["start"], "width": d["length"]},
            message=d["message"],
        )
        for d in validate(query, schema=schema)
    ]


def lint(query: str, schema: dict) -> list[Finding]:
    """Run every analyzer plus the binder over one query and merge the findings."""
    ir = parse(query, schema=schema).to_ir()
    findings = [f for analyzer in ANALYZERS for f in analyzer(ir)]
    findings.extend(semantic_findings(query, schema))
    return findings


def report(query: str, findings: list[Finding]) -> None:
    """Print one query's findings, one line each plus the text they cover."""
    if not findings:
        print("  clean")
        return
    for f in findings:
        where = f"@{f.span.text_start}" if f.span else "@query"
        print(f"  [{severity(f.severity)}] {f.rule_id:<28} {where:<6} {f.message}")
        if f.span:
            print(f"             <- {f.span.text(query)!r}")


def main() -> None:
    banner(
        "A KQL linter built on the IR",
        "Three analyzers read the typed IR and a fourth lifts Microsoft's "
        "binder diagnostics into the same Finding vocabulary. All four run "
        "over three queries.",
        "one merged list per query, holding findings from two very "
        "different sources. Some carry a span into the query text and one "
        "cannot, because it reports something that isn't there.",
    )

    for index, (title, query, why) in enumerate(QUERIES, start=1):
        section(f"Query {index}: {title}", why)
        kql(query)
        print()
        report(query, lint(query, SCHEMA))

    section(
        "Where a missing time filter costs you",
        "`example.no_time_filter` reads the query text, which is the only "
        "thing an analyzer has. The bound it reports missing often arrives "
        "from the caller instead.",
    )
    table(["Caller", "Time bound it supplies"], _TIME_BOUND_BY_CONTEXT)
    note(
        "So the same finding is a real cost in Azure Data Explorer and "
        "noise in a Sentinel hunting query. Severity is a plain Literal "
        "and analyzers are plain functions, so ranking this rule, or "
        "leaving it out of ANALYZERS, is a decision you make per context."
    )

    takeaway(
        "Every rule above is a function from QueryIR to Findings. Composing "
        "them is concatenation, and the binder's diagnostics join the same "
        "list once you map their severities. Nothing here registers a rule "
        "or subclasses a base class.",
        more="docs/tier2-ir.md, and examples/analyzer_demo.py for the "
             "Finding model itself",
    )


if __name__ == "__main__":
    main()
