# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Generic IR traversal with ``find_all``.

``find_all(ir, SomeType)`` is the building block most custom analyzers
need. Pair it with ``isinstance`` dispatch on the typed pydantic IR and
a five-line "analyzer" is usually all it takes.

Pair this with ``examples/walk_ir.py`` to see the trade-off:

* ``walk_ir.py`` walks a single pipeline by hand — typed dispatch on the
  source and operator list. Use that when you want to render or
  short-circuit on the pipeline's exact shape.
* This file uses ``find_all`` to descend through the entire IR — main
  pipeline, sub-pipelines on the right side of joins, predicate trees.
  Use this when you want every node of a given type regardless of where
  it lives.

The query joins with an explicit ``on $left.DeviceId == $right.DeviceId``,
which is what makes ``ColumnRef.join_side`` observable. ``join_side`` is a
separate field from ``table`` because ``table`` never carries the
``$left`` / ``$right`` syntax at all — an unresolvable side there is
honestly ``None``, so ``join_side`` is the only place the side survives on
a bound parse, and ``$left.a == $left.b`` is not the join ``$left.a ==
$right.b``.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

from _display import banner, kql, note, section, takeaway

from kustology import parse
from kustology.ir import ColumnRef, FilterOp, JoinOp, LetRef, TableRef, find_all

SCHEMA = {
    "DeviceProcessEvents": {
        "FileName": "string",
        "DeviceName": "string",
        "DeviceId": "string",
    },
    "DeviceNetworkEvents": {"RemoteIP": "string", "DeviceId": "string"},
}

QUERY = """
let powershell_procs = DeviceProcessEvents | where FileName == 'powershell.exe';
powershell_procs
| join (DeviceNetworkEvents | where RemoteIP != '127.0.0.1')
    on $left.DeviceId == $right.DeviceId
| project DeviceName, FileName, RemoteIP
"""


def main() -> None:
    banner(
        "Every node of a type, wherever it sits",
        "find_all(ir, SomeType) returns every node of that type anywhere in "
        "the IR: the main pipeline, a join's right-hand sub-pipeline, a let "
        "body, or a predicate tree.",
        "the answers that come from binding and the ones that do not. "
        "join_side is filled from the query text and is right on an unbound "
        "parse; result_type and table are the binder's work.",
    )

    section("The query")
    kql(QUERY.strip())

    ir = parse(QUERY, schema=SCHEMA).to_ir()

    section(
        "find_all(ir, TableRef) and find_all(ir, LetRef)",
        "Real tables and `let` aliases are separate node types, so one call "
        "answers each question without filtering the other out by hand.",
    )
    print(f"  Tables      : {sorted(n.name for n in find_all(ir, TableRef))}")
    print(f"  Let aliases : {sorted(n.name for n in find_all(ir, LetRef))}")
    note(
        "`powershell_procs` is missing from the table list because it names "
        "a `let` binding. A single node type for both would report it as a "
        "table your query never reads."
    )

    section(
        "find_all(ir, ColumnRef)",
        "Every column reference whatever its role: filter, projection, or "
        "join key.",
    )
    print(f"  {'name':<12} {'result_type':<12} {'table':<20} join_side")
    for col in find_all(ir, ColumnRef):
        side = col.join_side or "-"
        print(f"  {col.name:<12} {col.result_type!s:<12} {col.table!s:<20} {side}")
    note(
        "`table` is the scope the column resolved against, and it names the "
        "immediate source. Columns downstream of the alias resolve to "
        "`powershell_procs`, not to the DeviceProcessEvents behind it."
    )
    note(
        "`join_side` comes from the `$left.` and `$right.` the query wrote, "
        "so it survives an unbound parse. `table` never carries that syntax, "
        "which is why the side needs a field of its own: `$left.a == "
        "$left.b` is not the join `$left.a == $right.b`."
    )

    section(
        "find_all(ir, JoinOp)",
        "The query writes no `kind=`, so this reads KQL's effective default.",
    )
    for op in find_all(ir, JoinOp):
        print(f"  kind={op.join_kind}  on {op.on[0].canonical_form}")
    note(
        "The default is `innerunique`, not `inner`. It deduplicates the left "
        "side's join keys first, so a bare join and `join kind=inner` return "
        "different row counts from the same data. `join_kind` is never None, "
        "so an analyzer can compare it directly."
    )

    section(
        "find_all(ir, FilterOp)",
        "A five-line analyzer: every filter in the query, in canonical form.",
    )
    for op in find_all(ir, FilterOp):
        print(f"  {op.predicate.canonical_form}")
    note(
        "One filter sits in the `let` body and one inside the join's "
        "right-hand sub-pipeline. Neither is on the main pipeline, and "
        "find_all reaches both."
    )

    takeaway(
        "Most custom analyzers are a find_all call and an isinstance check. "
        "Walk the pipeline by hand, as examples/walk_ir.py does, when you "
        "care about the order operators run in or want to stop early.",
        more="docs/tier2-ir.md, and examples/linter.py for rules built this "
             "way",
    )


if __name__ == "__main__":
    main()
