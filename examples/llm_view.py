# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""LLM-friendly IR serialization via ``to_llm_dict``.

The canonical ``model_dump_json`` output is round-trippable but verbose —
every node carries a span, every ``Expr`` carries default fields
(``result_type: "unresolved"`` on anything the binder could not place),
every operator on a bound parse restates the column list the one before it
emitted, and structural flags add noise that does not help a model reason
about the query.

``to_llm_dict`` produces a lossy, LLM-tailored projection of the same IR:

* Every node carries a stable ``kind`` discriminator (``filter``,
  ``column_ref``, ``bin_op``) drawn from class-level ``KIND`` constants.
* Spans are stripped (offsets aren't useful without source text).
* Default field values are dropped.
* ``polarity`` is collapsed into the operator (``!=`` reads as
  ``op: "!="``; ``!in`` likewise on ``SetMembership``).
* Redundant leaf ``canonical_form`` is dropped where it restates ``name``
  or ``value`` — but only on four of the eight literal kinds. The check
  renders ``value`` back to KQL, which double-quotes any Python ``str``, so
  it fires for ``string`` / ``long`` / ``real`` / ``bool`` and *not* for
  ``timespan`` / ``datetime`` / ``decimal`` / ``guid``, whose values are
  stored as strings that are not KQL string literals. Run this file and the
  ``ago(7d)`` literal still carries ``"canonical_form": "7.00:00:00"``
  beside an identical ``"value"``.
* ``result_schema`` is dropped from **operator** nodes and kept on
  ``Pipeline``. "What columns does this query return" is one answer per
  pipeline; repeating it per step was 35% of the whole view — that figure
  is measured across the 49-query fixture corpus bound against a schema
  naming every referenced column (295,156 of 851,224 bytes), not on the
  one query below. This query's own reduction is printed at run time.

The result is JSON-safe but lossy: pass it to a model when you want to
ask "what does this query do?", "where is the bug?", or "rewrite this
to also filter X." For round-trip serialization, keep using
``QueryIR.model_dump_json()`` — that one validates back through
``QueryIR.model_validate_json``, and this one does not.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

import json

from kustology import parse

# Binding is what turns `result_type: "unresolved"` into real types, and it
# is also what fills each pipeline's result_schema. `to_ir()` auto-attaches
# on a bound parse, so the schema is named once and never restated.
SCHEMA = {
    "StormEvents": {
        "StartTime": "datetime",
        "State": "string",
        "EventType": "string",
        "DeathsDirect": "int",
    },
    "StatePopulation": {"State": "string", "Population": "long"},
}

# A `let`, a relative time window, two negations and a join — the shapes an
# LLM most often has to reason about. The two negations show the polarity
# collapse: both nodes already carry the negated operator on `op` (`"!="`
# and `"!in"`), so the view drops the redundant `polarity` rather than
# synthesizing anything.
QUERY = (
    "let recent = StormEvents | where StartTime > ago(7d);\n"
    "recent\n"
    '| where State != "TEXAS" and EventType !in ("Tornado", "Hail")\n'
    "| join kind=inner (StatePopulation) on State\n"
    "| project StartTime, State, EventType, DeathsDirect, Population"
)


def _column_refs(node):
    """Every ``column_ref`` dict in a ``to_llm_dict`` payload."""
    if isinstance(node, dict):
        if node.get("kind") == "column_ref":
            yield node
        for value in node.values():
            yield from _column_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _column_refs(value)


def _all_columns(view) -> int:
    return sum(1 for _ in _column_refs(view))


def _typed_columns(view) -> int:
    return sum(1 for c in _column_refs(view) if "result_type" in c)


def main() -> None:
    # The whole pipeline, in one line: parse, bind, build, enrich.
    ir = parse(QUERY, schema=SCHEMA).to_ir()

    canonical = ir.model_dump_json(indent=2)
    llm = json.dumps(ir.to_llm_dict(), indent=2)

    print("Input query:")
    for line in QUERY.splitlines():
        print(f"  {line}")
    print()
    print(f"Canonical model_dump_json: {len(canonical):>6,} bytes  "
          f"({canonical.count(chr(10)) + 1} lines)")
    print(f"LLM view (to_llm_dict):    {len(llm):>6,} bytes  "
          f"({llm.count(chr(10)) + 1} lines)")
    print(f"Reduction:                 {(1 - len(llm) / len(canonical)) * 100:.0f}%")
    print()

    # The same query without a schema, to show what binding buys — and one
    # consequence of the "drop defaults" rule that surprises people.
    #
    # In `model_dump_json` an unplaced column reads `result_type:
    # "unresolved"`. That is the sentinel's name: not "unknown", and
    # unrelated to `UnknownExpr`, which means "the builder could not model
    # this shape". But `unresolved` is also the field's *default*, so the
    # LLM view drops it: an unbound column carries no `result_type` key at
    # all. Absent means unresolved here — do not read a missing key as an
    # error.
    unbound_ir = parse(QUERY).to_ir()
    print(f"model_dump_json, unbound: "
          f'{unbound_ir.model_dump_json().count(chr(34) + "unresolved" + chr(34))} '
          f'nodes say "unresolved".')
    print(f"to_llm_dict, unbound:     "
          f"{_typed_columns(unbound_ir.to_llm_dict())} of "
          f"{_all_columns(unbound_ir.to_llm_dict())} column_ref nodes carry a "
          f"result_type key.")
    print(f"to_llm_dict, bound:       "
          f"{_typed_columns(ir.to_llm_dict())} of "
          f"{_all_columns(ir.to_llm_dict())}.")
    print()
    print("LLM view:")
    print(llm)


if __name__ == "__main__":
    main()
