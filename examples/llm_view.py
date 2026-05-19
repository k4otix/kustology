# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""LLM-friendly IR serialization via ``to_llm_dict``.

The canonical ``model_dump_json`` output is round-trippable but verbose —
every node carries a span, every Expr carries default fields
(``nullable=True``, ``result_type=unknown``), and structural hashes /
schema-attached flags add noise that doesn't help an LLM reason about
the query.

``to_llm_dict`` produces a lossy, LLM-tailored projection of the same IR:

* Every node carries a stable ``kind`` discriminator (``filter``,
  ``column_ref``, ``bin_op``) drawn from class-level KIND constants.
* Spans are stripped (offsets aren't useful without source text).
* Default field values are dropped.
* ``polarity`` is collapsed into the operator (``!=`` reads as
  ``op: "!="``; ``!in`` materializes ``op: "!in"`` on SetMembership).
* Redundant leaf ``canonical_form`` is dropped when it just restates
  ``name`` / ``value``.

The result is JSON-safe but lossy: pass it to a model when you want to
ask "what does this query do?", "where is the bug?", or "rewrite this
to also filter X." For round-trip serialization, keep using
``QueryIR.model_dump_json()``.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from kustology.ir import IRBuilder, SchemaAttacher
from kustology.utils.analysis import build_global_state


# StormEvents schema lets the binder resolve column types so the LLM view
# shows ``result_type: datetime`` / ``string`` / ``int`` rather than
# ``unknown``. Without a schema the example still runs — types just stay
# unresolved on the way out.
STORM_EVENTS_SCHEMA = {
    "StormEvents": {
        "StartTime": "datetime",
        "State": "string",
        "EventType": "string",
        "DeathsDirect": "int",
    }
}


# Two negations exercise both polarity-collapse paths:
#   * ``State != "TEXAS"`` → BinOp emits ``op: "!="`` directly
#   * ``EventType !in (...)`` → SetMembership synthesizes ``op: "!in"``
QUERY = (
    'StormEvents '
    '| where State != "TEXAS" and EventType !in ("Tornado", "Hail") '
    '| project StartTime, State, EventType, DeathsDirect'
)


def main() -> None:
    gs = build_global_state(STORM_EVENTS_SCHEMA)
    ir = IRBuilder(global_state=gs).build(QUERY)
    SchemaAttacher(STORM_EVENTS_SCHEMA).enrich(ir)

    canonical = ir.model_dump_json(indent=2)
    llm = json.dumps(ir.to_llm_dict(), indent=2)

    print("Input query:")
    print(f"  {QUERY}")
    print()
    print(f"Canonical model_dump_json: {len(canonical):>6,} bytes  "
          f"({canonical.count(chr(10)) + 1} lines)")
    print(f"LLM view (to_llm_dict):    {len(llm):>6,} bytes  "
          f"({llm.count(chr(10)) + 1} lines)")
    print(f"Reduction:                 {(1 - len(llm) / len(canonical)) * 100:.0f}%")
    print()
    print("LLM view:")
    print(llm)


if __name__ == "__main__":
    main()
