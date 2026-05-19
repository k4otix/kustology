# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Logical pipeline traversal via the IR.

The IR is a typed, normalized model of a query: ``Pipeline.source`` is a
known kind (``TableRef``, ``LetRef``, ``ImplicitSource``, …) and
``Pipeline.operators`` is a list of tagged operators (``FilterOp``,
``ProjectOp``, …). Walking it is ``isinstance`` dispatch on real Python
classes — no node-kind strings, no wrapper filtering, no token skipping.

Pair this with ``examples/walk_tree.py`` to see the trade-off:

* IR walk (this file) — typed, terse, serializable. Use this for
  pipeline analysis, cross-query comparison, anything you'd want to
  ``model_dump_json()`` and ship.
* AST walk (``walk_tree.py``) — full grammar including tokens, comments,
  and operators the IR doesn't dispatch yet. Use that for refactoring,
  syntax highlighting, or when you need token-level positions.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from kustology.ir import (
    Assignment, ColumnRef, FilterOp, IRBuilder, ProjectOp, TableRef,
)
from kustology.utils.analysis import build_global_state


# Schema binds StormEvents so the .NET binder fills result types and emits no
# "unknown table" diagnostics. Without it the build still succeeds — the IR
# just carries an error diagnostic and leaves column types as "unknown".
STORM_EVENTS_SCHEMA = {
    "StormEvents": {
        "StartTime": "datetime",
        "EndTime": "datetime",
        "EpisodeId": "int",
        "EventId": "int",
        "State": "string",
        "EventType": "string",
        "InjuriesDirect": "int",
        "InjuriesIndirect": "int",
        "DeathsDirect": "int",
        "DeathsIndirect": "int",
        "DamageProperty": "int",
        "DamageCrops": "int",
        "Source": "string",
        "BeginLocation": "string",
        "EndLocation": "string",
        "BeginLat": "real",
        "BeginLon": "real",
        "EndLat": "real",
        "EndLon": "real",
        "EpisodeNarrative": "string",
        "EventNarrative": "string",
        "StormSummary": "dynamic",
    }
}


def describe_column(c) -> str:
    if isinstance(c, ColumnRef):
        return c.name
    if isinstance(c, Assignment):
        return f"{c.name} = <expr>"
    return type(c).__name__


def walk_pipeline(pipeline) -> None:
    source = pipeline.source
    if isinstance(source, TableRef):
        print(f"  Source: {source.name}")
    else:
        print(f"  Source: {type(source).__name__}")

    for op in pipeline.operators:
        if isinstance(op, FilterOp):
            print(f"  Filter: predicate = {type(op.predicate).__name__}")
        elif isinstance(op, ProjectOp):
            cols = ", ".join(describe_column(c) for c in op.columns)
            print(f"  Project: {len(op.columns)} columns = [{cols}]")
        else:
            print(f"  {type(op).__name__}")


QUERY = (
    "StormEvents "
    '| where State == "TEXAS" and EventType == "Tornado" '
    "| project StartTime, State, EventType, DeathsDirect"
)


def main() -> None:
    print("Input query:")
    print(f"  {QUERY}")
    print()

    gs = build_global_state(STORM_EVENTS_SCHEMA)
    ir = IRBuilder(global_state=gs).build(QUERY)
    print("IR walk (typed operators):")
    walk_pipeline(ir.main_pipeline)

    print()
    print("Serialized IR:")
    print(ir.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
