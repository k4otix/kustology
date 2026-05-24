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

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from kustology import parse
from kustology.ir import ColumnRef, FilterOp, SchemaAttacher, TableRef, find_all


SCHEMA = {
    "DeviceProcessEvents": {
        "FileName": "string",
        "DeviceName": "string",
        "DeviceId": "string",
    },
    "DeviceNetworkEvents": {"RemoteIP": "string", "DeviceId": "string"},
}

QUERY = """
DeviceProcessEvents
| where FileName == 'powershell.exe'
| join (DeviceNetworkEvents | where RemoteIP != '127.0.0.1') on DeviceId
| project DeviceName, FileName, RemoteIP
"""


def main() -> None:
    ir = parse(QUERY, schema=SCHEMA).to_ir()
    SchemaAttacher(SCHEMA).enrich(ir)

    # Every table referenced anywhere in the query — including inside
    # join right-side sub-pipelines.
    tables = {n.name for n in find_all(ir, TableRef)}
    print(f"Tables: {tables}")

    # Every column reference, regardless of role (filter, project,
    # join key). Binder-resolved table provenance is on each node.
    provenance = {(n.name, n.table) for n in find_all(ir, ColumnRef)}
    print("Column provenance:")
    for col, table in sorted(provenance):
        print(f"  {col} <- {table}")

    # A five-line "analyzer": every filter in the query, with its
    # canonical predicate form.
    print("Filters:")
    for op in find_all(ir, FilterOp):
        print(f"  {op.predicate.canonical_form}")


if __name__ == "__main__":
    main()
