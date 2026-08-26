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

from _display import banner, kql, note, paint, section, takeaway

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


def print_diagnostics(diags: list[dict]) -> None:
    if not diags:
        print("  (none)")
        return
    for d in diags:
        level = paint(d["severity"], d["severity"].lower())
        print(f"  [{level} {d['code']}] at char {d['start']}: {d['message']}")


def main() -> None:
    banner(
        "What a schema adds",
        "One query with one typo, parsed twice. The first parse checks "
        "syntax. The second binds every name against a schema and runs "
        "Microsoft's binder.",
        "the diagnostics list under each parse. The typo is invisible to "
        "the first and an error to the second.",
    )

    section("The query", "`EvenType` is a typo for `EventType`.")
    kql(QUERY)

    cols = STORM_EVENTS_SCHEMA["StormEvents"]
    # Counted, not written down: a hard-coded column count would silently
    # go stale as the dict above changes.
    section(
        f"The schema: StormEvents, {len(cols)} columns",
        "Each value is a KQL scalar type name, resolved through "
        "ScalarTypes.GetSymbol at parse time.",
    )
    for name, kql_type in cols.items():
        print(f"  {name:<22s} {kql_type}")

    section(
        "parse(query): syntax only, no schema",
        "Nothing resolves a name here, so nothing can judge one.",
    )
    syntactic = parse(QUERY)
    print(f"  has_semantics : {syntactic.has_semantics}")
    print("  diagnostics   :")
    # `KustoQuery.diagnostics` reads off the parse this object already
    # holds, in `validate()`'s dict shape. `validate(QUERY)` returns the
    # same list but parses the text a second time — and for a bound query
    # re-runs the binder against a schema you have to supply again.
    print_diagnostics(syntactic.diagnostics)
    note("`EvenType` is a valid identifier, so a syntactic parse has no "
         "grounds to reject it.")

    section(
        "parse(query, schema=...): bound against StormEvents",
        "The binder now has names to resolve the query against.",
    )
    bound = parse(QUERY, schema=STORM_EVENTS_SCHEMA)
    print(f"  has_semantics : {bound.has_semantics}")
    print("  diagnostics   :")
    print_diagnostics(bound.diagnostics)
    note("`has_semantics` is all-or-nothing on tier 1. An unbound parse "
         "resolves no symbol at all, built-in functions included.")

    section(
        "validate(): the same answer without holding a KustoQuery",
        "Same shape, same codes, one difference in cost.",
    )
    print(f"  validate(QUERY)                        -> "
          f"{len(validate(QUERY))} diagnostic(s)")
    print(f"  validate(QUERY, schema=...)            -> "
          f"{len(validate(QUERY, schema=STORM_EVENTS_SCHEMA))} diagnostic(s)")
    note("validate parses the text again, and for a bound query it re-runs "
         "the binder against a schema you supply a second time. Read the "
         "`diagnostics` property when you already hold the parse.")

    takeaway(
        "Pass a schema whenever you want the binder's opinion: unknown "
        "columns, type mismatches, and resolved symbols. Leave it out when "
        "you only need to know the text parses.",
        more="docs/tier1-syntax-tree.md",
    )


if __name__ == "__main__":
    main()
