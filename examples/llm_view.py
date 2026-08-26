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
  or ``value`` — but not on every literal. The test is
  ``cf == _canonical_literal_repr(value)``, and that re-render
  double-quotes any Python ``str``. So the drop fires when ``value`` is not
  a string at all, and when it is a string that really is a KQL string
  literal; it does not fire for a kind whose value is a *non-string stored
  as a string*, which keeps a ``canonical_form`` identical to its
  ``value``. Which kinds fall on which side is measured and printed when
  this file runs, rather than written down here — the run below probes
  every member of ``LiteralExpr.literal_kind``.
* ``result_schema`` is dropped from **operator** nodes and kept on
  ``Pipeline``. "What columns does this query return" is one answer per
  pipeline; repeating it per step is 35% of the whole view — that figure
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
import typing

from kustology import parse
from kustology.ir import LiteralExpr

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
    """Yield every ``column_ref`` dict in a ``to_llm_dict`` payload."""
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


# One probe query per member of ``LiteralExpr.literal_kind``. The section
# in main() derives the drop/keep split from these rather than restating a
# list that would go stale the first time a literal kind is added — the
# check itself is enumerated from the model, so a new kind with no probe
# here is reported as missing instead of silently skipped.
_LITERAL_PROBES = {
    "string": 'T | where s == "x"',
    "int": "T | where n == int(5)",
    "long": "T | where n == 42",
    "real": "T | where r > 1.5",
    "decimal": "T | where m > decimal(1.5)",
    "bool": "T | where b == true",
    "datetime": "T | where t > datetime(2024-01-01)",
    "timespan": "T | where d > 7d",
    "dynamic": "T | extend x = dynamic([1,2,3])",
    "guid": "T | where g == guid(74be27de-1e4e-49d9-b579-fe0b331d3642)",
    "null": "T | where a > real(null)",
}


def _literals(node):
    """Yield every ``literal`` dict in a ``to_llm_dict`` payload."""
    if isinstance(node, dict):
        if node.get("kind") == "literal":
            yield node
        for value in node.values():
            yield from _literals(value)
    elif isinstance(node, list):
        for value in node:
            yield from _literals(value)


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
    # Which literal kinds keep a canonical_form identical to their value.
    # Derived, not written down: the kinds come from the model, the verdict
    # from a real parse of each.
    kinds = typing.get_args(LiteralExpr.model_fields["literal_kind"].annotation)
    kept, dropped, unprobed = [], [], []
    for kind in kinds:
        query = _LITERAL_PROBES.get(kind)
        if query is None:
            unprobed.append(kind)
            continue
        found = [
            lit for lit in _literals(parse(query).to_ir().to_llm_dict())
            if lit["literal_kind"] == kind
        ]
        if not found:
            unprobed.append(kind)
        elif "canonical_form" in found[0]:
            kept.append(kind)
        else:
            dropped.append(kind)
    print(f"canonical_form dropped ({len(dropped)} of {len(kinds)} literal kinds): "
          f"{', '.join(dropped)}")
    print(f"canonical_form kept    ({len(kept)} of {len(kinds)}): {', '.join(kept)}")
    if unprobed:
        print(f"no probe for: {', '.join(unprobed)}  <-- add one to _LITERAL_PROBES")
    print("  → The kept ones are exactly the kinds whose `value` is a")
    print("    non-string stored as a string, so re-rendering it as KQL adds")
    print("    quotes the canonical form does not have and the equality fails.")
    print()

    print("LLM view:")
    print(llm)


if __name__ == "__main__":
    main()
