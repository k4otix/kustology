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
a bound parse, and `$left.a == $left.b` is not the join `$left.a ==
$right.b`.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

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
    ir = parse(QUERY, schema=SCHEMA).to_ir()

    # Every *table* referenced anywhere in the query — including inside
    # join right-side sub-pipelines and let-binding bodies. Note what is
    # absent: `powershell_procs` is a let alias, not a table, so it is a
    # LetRef and never shows up here.
    tables = {n.name for n in find_all(ir, TableRef)}
    print(f"Tables: {tables}")

    # The aliases, found the same way. Keeping the two node types distinct
    # is what stops a `let` name from being reported as a real table.
    aliases = {n.name for n in find_all(ir, LetRef)}
    print(f"Let aliases: {aliases}")

    # Every column reference, regardless of role (filter, project, join
    # key), with three fields — and only two of them come from binding.
    #
    # `result_type` is Microsoft's binder's answer; unbound it reads
    # `unresolved`. `table` is the scope the column resolved against, and
    # names the *immediate* source: columns downstream of the alias resolve
    # to `powershell_procs`, not to the DeviceProcessEvents behind it.
    #
    # `join_side` is **not** binder-filled — it is read off the `$left.` /
    # `$right.` the query wrote, so it is already correct on an unbound
    # parse. What binding changes is `table`, which goes from `None`
    # (`table` never carries the `$left`/`$right` syntax itself) to the
    # resolved `'powershell_procs'`. That is exactly why `join_side` has to
    # be its own field: it is the only place the side is ever recorded.
    print("Columns:")
    print(f"  {'name':<12} {'result_type':<12} {'table':<20} join_side")
    for col in find_all(ir, ColumnRef):
        side = col.join_side or "-"
        print(f"  {col.name:<12} {col.result_type!s:<12} {col.table!s:<20} {side}")

    # The join itself. This query writes no `kind=`, and `join_kind` reads
    # `innerunique` — KQL's effective default, which is *not* `inner`:
    # innerunique deduplicates the left side's join keys first, so a bare
    # join and `join kind=inner` return different row counts from the same
    # data. The field is never None, so an analyzer can compare it directly.
    for op in find_all(ir, JoinOp):
        print(f"Join: kind={op.join_kind}  on {op.on[0].canonical_form}")

    # A five-line "analyzer": every filter in the query, with its
    # canonical predicate form. `find_all` reaches the one inside the
    # join's right-hand sub-pipeline as well as the one in the let body.
    print("Filters:")
    for op in find_all(ir, FilterOp):
        print(f"  {op.predicate.canonical_form}")


if __name__ == "__main__":
    main()
