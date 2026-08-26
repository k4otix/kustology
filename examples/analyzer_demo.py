# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The ``Finding`` vocabulary: composing analyzers and consuming their output.

``examples/linter.py`` shows *rules*. This file shows the shape around
them — what an analyzer is, how two of them compose, and what a caller
does with the result.

An analyzer is a function ``QueryIR -> Iterable[Finding]``. That is the
whole contract; ``AnalyzerFn`` is its type alias, and composition is
concatenation:

    def run_all(ir, analyzers):
        return [f for a in analyzers for f in a(ir)]

Three things the ``Finding`` model buys you, each demonstrated below:

* **A precise span**, where the analyzer knows one. Compare the two
  answers to the same defect in the output: Microsoft's binder reports a
  join-key type mismatch as ``KS242`` at offset 0 with length 0 — correct
  and unlocatable — while the IR rule points at the comparison itself,
  because it is holding the node.
* **``extra``**, a per-rule structured side channel. It exists so a new
  rule can carry its own data without the shared schema growing a field
  for every analyzer anyone writes.
* **A three-value ``Severity``** that is a plain ``Literal``, so ranking
  and filtering are the caller's policy rather than the model's.

Findings are pydantic models, so ``model_dump(mode="json")`` gives a
CI-shaped payload with no extra work.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

import json
from collections.abc import Iterable

from _display import banner, data, kql, note, section, severity, takeaway

from kustology import parse, validate
from kustology.ir import (
    AnalyzerFn,
    BinOp,
    ColumnRef,
    Finding,
    JoinOp,
    QueryIR,
    Severity,
    find_all,
)

SCHEMA = {
    "Alerts": {
        "DeviceId": "string",
        "AlertSeverity": "string",
        "TimeGenerated": "datetime",
    },
    "Inventory": {"DeviceId": "int", "Owner": "string"},
}

QUERY = (
    "Alerts\n"
    "| where TimeGenerated > ago(1d) and Unknown == 1\n"
    "| join (Inventory) on $left.DeviceId == $right.DeviceId\n"
    "| project Owner, AlertSeverity"
)

# Caller policy, not model policy: Severity is a plain Literal of three
# strings, so ordering them is up to whoever consumes the findings.
RANK: dict[Severity, int] = {"error": 0, "warning": 1, "info": 2}


def join_key_type_mismatch(ir: QueryIR) -> Iterable[Finding]:
    """Flag a join whose two keys are bound to different types.

    Needs a bound parse: ``result_type`` is the binder's answer, and on an
    unbound one both sides read ``unresolved`` and compare equal, so the
    rule never fires.
    """
    for op in find_all(ir, JoinOp):
        for condition in op.on:
            if not isinstance(condition, BinOp) or condition.op != "==":
                continue
            left, right = condition.left, condition.right
            if not (isinstance(left, ColumnRef) and isinstance(right, ColumnRef)):
                continue
            if left.result_type == right.result_type:
                continue
            yield Finding(
                rule_id="example.join_key_type_mismatch",
                severity="error",
                span=condition.span,
                message=f"Join keys disagree: {left.name} is "
                        f"{left.result_type}, {right.name} is "
                        f"{right.result_type}.",
                # The side channel. A dashboard can group on these without
                # re-parsing the message, and no other rule has to know
                # they exist.
                extra={
                    "left": {"column": left.name, "type": str(left.result_type),
                             "scope": left.table, "side": left.join_side},
                    "right": {"column": right.name, "type": str(right.result_type),
                              "scope": right.table, "side": right.join_side},
                },
            )


def unplaced_columns(ir: QueryIR) -> Iterable[Finding]:
    """Flag every column the binder could not place.

    On a bound parse this means the schema does not describe it — a typo,
    or a column the caller forgot to declare. ``table is None`` is the
    signal; ``result_type`` will read ``unresolved`` alongside it.
    """
    for col in find_all(ir, ColumnRef):
        if col.table is not None:
            continue
        yield Finding(
            rule_id="example.unplaced_column",
            severity="warning",
            span=col.span,
            message=f"`{col.name}` is not in the supplied schema.",
            extra={"column": col.name},
        )


ANALYZERS: list[AnalyzerFn] = [join_key_type_mismatch, unplaced_columns]


def run_all(ir: QueryIR, analyzers: list[AnalyzerFn]) -> list[Finding]:
    """Run every analyzer and concatenate what each yields.

    This is the composition recipe from ``kustology.ir.analyzers``'s
    docstring, identical but for the loop variable, which is spelled out
    here.
    """
    return [f for analyzer in analyzers for f in analyzer(ir)]


def main() -> None:
    banner(
        "The Finding vocabulary",
        "Two analyzers run over one query and their findings merge into a "
        "single list. Then the same join defect arrives from Microsoft's "
        "binder, so you can compare the two answers side by side.",
        "the span on each finding. The rule that holds the node can point "
        "at the comparison; the binder, which returns a message, cannot.",
    )

    section("The query", "The join compares a string key against an int one.")
    kql(QUERY)

    ir = parse(QUERY, schema=SCHEMA).to_ir()
    findings = sorted(run_all(ir, ANALYZERS), key=lambda f: RANK[f.severity])

    section(
        "Findings, ranked by the caller's severity order",
        "Severity is a plain Literal of three strings, so RANK above is the "
        "caller's policy and not the model's.",
    )
    for f in findings:
        at = f"{f.span.text_start}..{f.span.text_end}" if f.span else "query"
        excerpt = f.span.text(QUERY) if f.span else ""
        print(f"  [{severity(f.severity)}] {f.rule_id}")
        print(f"            at {at}: {excerpt!r}")
        print(f"            {f.message}")

    section(
        "The same join defect, as Microsoft reports it",
        "KS242 is the binder's name for the type mismatch above.",
    )
    for d in validate(QUERY, schema=SCHEMA):
        if d["code"] == "KS242":
            print(f"  [{d['severity']}] {d['code']} at "
                  f"{d['start']}..{d['start'] + d['length']}: {d['message']}")
    note(
        "Correct, and unlocatable: a zero-width span at offset 0. The IR "
        "rule above points at the comparison because it is holding the "
        "node rather than reading a message."
    )

    section(
        "extra: the per-rule side channel",
        "A dashboard can group on these fields without re-parsing the "
        "message, and no other rule has to know they exist.",
    )
    for f in findings:
        if f.extra:
            print(f"  {f.rule_id}:")
            data(json.dumps(f.extra), indent=4)

    section(
        "A CI gate",
        "Findings are pydantic models, so the payload needs no bespoke "
        "encoder.",
    )
    payload = [f.model_dump(mode="json", exclude_none=True) for f in findings]
    print(f"  {len(payload)} finding(s); "
          f"{sum(1 for f in findings if f.severity == 'error')} at error level")
    print(f"  exit code would be {1 if any(f.severity == 'error' for f in findings) else 0}")
    print("  first payload row:")
    data(json.dumps(payload[0], indent=2), indent=4)

    takeaway(
        "An analyzer is a function from QueryIR to Findings, so composing "
        "two of them is a list comprehension. The Finding model carries the "
        "span when a rule knows it, the structured extra when a rule needs "
        "it, and a severity the caller ranks.",
        more="docs/tier2-ir.md, and examples/linter.py for four rules built "
             "this way",
    )


if __name__ == "__main__":
    main()
