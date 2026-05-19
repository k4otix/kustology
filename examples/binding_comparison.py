# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Semantic binding via ``parse(query, schema=...)``.

Without a schema the parser only checks syntax. With a schema, Microsoft's
binder resolves every name to a symbol and surfaces real semantic errors
(typos, unknown columns, type mismatches) — the kind of feedback that pure
parsing cannot produce.

This example uses the canonical Azure Data Explorer ``StormEvents`` schema
and a query containing a deliberate typo (``EvenType`` instead of
``EventType``) to show the difference.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from kustology import parse, validate


# Canonical Azure Data Explorer sample table — used by every ADX tutorial.
# Each value is a KQL scalar type that kustology resolves via
# ScalarTypes.GetSymbol at parse time.
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


# `EvenType` is a typo for `EventType`. Both are syntactically valid
# identifiers, so pure parsing accepts the query. Only the binder, which
# needs the schema to resolve names, can reject it.
QUERY = (
    'StormEvents '
    '| where EvenType == "Tornado" and State == "TEXAS" '
    '| summarize count() by State'
)


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def print_diagnostics(diags: list[dict]) -> None:
    if not diags:
        print("  (none)")
        return
    for d in diags:
        print(f"  [{d['severity']} {d['code']}] at char {d['start']}: {d['message']}")


def main() -> None:
    print("Input query:")
    print(f"  {QUERY}")
    print()
    print("Schema (StormEvents — 22 columns):")
    cols = STORM_EVENTS_SCHEMA["StormEvents"]
    for name, kql_type in cols.items():
        print(f"  {name:<22s} {kql_type}")

    banner("parse(query)  — syntactic only, no schema")
    syntactic = parse(QUERY)
    print(f"  has_semantics : {syntactic.has_semantics}")
    print("  diagnostics   :")
    print_diagnostics(validate(QUERY))
    print("  → The parser cannot tell `EvenType` is a typo; it's a valid identifier.")

    banner("parse(query, schema=...)  — bound against StormEvents")
    bound = parse(QUERY, schema=STORM_EVENTS_SCHEMA)
    print(f"  has_semantics : {bound.has_semantics}")
    print("  diagnostics   :")
    print_diagnostics(validate(QUERY, schema=STORM_EVENTS_SCHEMA))
    print("  → The binder resolves names against the schema and rejects EvenType.")


if __name__ == "__main__":
    main()
