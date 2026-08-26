# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""End-to-end analysis of a non-trivial KQL query.

Walks every analyzer on ``KustoQuery`` against a multi-let,
join-bearing query:

  - ``validate()``                 — structured parser diagnostics.
  - ``get_structural_hash()``      — SHA-256 over the AST skeleton;
                                     blind to literals *and* identifiers,
                                     sensitive to operator sequence. The
                                     example computes the invariance
                                     rather than claiming it.
  - ``get_referenced_tables()``    — every table source, including
                                     joined, union'd, and
                                     ``database()``-qualified refs.
  - ``get_referenced_columns()``   — every column reference, with
                                     function callees and ``$``-prefixed
                                     join sides filtered out.
  - ``get_referenced_functions()`` — every function callee, including
                                     KQL built-ins and user-defined
                                     callables in semantic mode.
  - ``find_time_expressions()``    — temporal expressions with source
                                     offsets.
  - ``get_operator_chain()``       — ordered pipeline of operators.
  - ``get_operator_stats()``       — operator-kind counts across the
                                     full AST (including sub-pipelines).
  - ``replace_table()``            — AST-aware rename across every
                                     reference position.
  - ``format_query()``             — canonical formatting via
                                     Microsoft's ``KustoCodeService``.
"""

from _display import banner, kql, note, paint, section, table, takeaway

from kustology import format_query, parse, validate

QUERY = """\
let lookback = 7d;
let high_impact_states = StormEvents
    | where StartTime > ago(lookback)
    | where DeathsDirect > 0 or InjuriesDirect > 0
    | summarize Casualties = sum(DeathsDirect + InjuriesDirect) by State
    | where Casualties > 5;
StormEvents
| where StartTime > ago(lookback)
| join kind=inner (high_impact_states) on State
| extend TotalLoss = DamageProperty + DamageCrops
| summarize EventCount = count(), TotalDamage = sum(TotalLoss) by State, EventType
| project State, EventType, EventCount, TotalDamage"""


def analyze(query_text: str) -> None:
    """Run every ``KustoQuery`` analyzer over ``query_text`` and print each result."""
    banner(
        "Every tier 1 analyzer, over one query",
        "A query with two `let` statements, a join, and a summarize, put "
        "through each analyzer on KustoQuery in turn. Everything here runs "
        "on the base install, with no schema and no IR.",
        "how much of this is syntax and nothing more. The notes below say "
        "where that runs out and what tier 2 adds.",
    )

    section("The query")
    kql(query_text)

    result = parse(query_text)
    diagnostics = validate(query_text)

    section("validate()", "Microsoft's parser diagnostics, as dicts.")
    print(f"  {len(diagnostics)} diagnostic(s)"
          + (" — query is syntactically valid" if not diagnostics else ""))
    for d in diagnostics:
        level = paint(d["severity"], d["severity"].lower())
        print(f"    [{level} {d['code']}] at char {d['start']}: {d['message']}")

    section(
        "get_structural_hash()",
        "SHA-256 over the AST skeleton: node kinds and shape, nothing else. "
        "Each row below is hashed here and compared, rather than claimed.",
    )
    baseline = result.get_structural_hash()
    print(f"  {baseline}")
    print()
    variants = {
        "literals changed (5 -> 500, 0 -> 3)":
            query_text.replace("Casualties > 5", "Casualties > 500")
                      .replace("DeathsDirect > 0", "DeathsDirect > 3"),
        "reflowed onto one line":
            " ".join(query_text.split()),
        "a comment added at the top":
            "// high-impact states\n" + query_text,
        "every State renamed to Region":
            query_text.replace("State", "Region"),
        "one more operator (| take 10)":
            query_text + "\n| take 10",
    }
    table(
        ["Change to the query", "Hash"],
        [
            [label, "same" if parse(v).get_structural_hash() == baseline else "differs"]
            for label, v in variants.items()
        ],
    )
    note(
        "Identifiers do not move it either, which is the part that catches "
        "people off guard: `where State == 'x'` and `where Region == 'x'` "
        "share one structural hash. Tier 2's `semantic_hash` is the digest "
        "that separates them."
    )

    section(
        "get_referenced_tables()",
        "Every table source, including joined, union'd, and "
        "database()-qualified references.",
    )
    tables = sorted(result.get_referenced_tables())
    print(f"  {tables}")
    note(
        "`high_impact_states` is a `let` alias rather than a table, so it is "
        "absent. So is the join's right-hand side, which reads that alias."
    )

    section(
        "get_referenced_columns()",
        "Every name in a column position, with function callees and "
        "$-prefixed join sides filtered out.",
    )
    for col in sorted(result.get_referenced_columns()):
        print(f"  {col}")
    note(
        "This list includes the names the query creates: Casualties, "
        "TotalLoss, EventCount, and TotalDamage are summarize and extend "
        "outputs, not columns of StormEvents. Bind a schema and use tier "
        "2's `find_all(ir, ColumnRef)` for provenance per reference."
    )

    section("get_referenced_functions()", "Callees, built-in and user-defined.")
    print(f"  {sorted(result.get_referenced_functions())}")

    section(
        "find_time_expressions()",
        "Temporal expressions with their offsets into the source text.",
    )
    time_windows = result.find_time_expressions()
    if not time_windows:
        print("  (no temporal expressions)")
    for text, start, length in time_windows:
        print(f"  {text!r:25s}  start={start:4d}  length={length}")
    note(
        "`ago(lookback)` appears twice, at two offsets, because the `let` "
        "value is read in two places. The offsets are what a linter needs "
        "to point at the text."
    )

    section("get_operator_chain()", "The main pipeline, in order.")
    # Operators only: the source table the pipeline reads from is not one,
    # so print it separately rather than expecting it at the head of the list.
    chain = result.get_operator_chain()
    flow = [str(node.Kind).replace("Operator", "") for node in chain]
    sources = sorted(result.get_referenced_tables())
    reading = f", reading {', '.join(sources)}" if sources else ""
    print(f"  {len(chain)} operators{reading}:")
    print("  " + (" -> ".join(flow) if flow else "(none)"))

    section(
        "get_operator_stats()",
        "Counts across the whole AST, sub-pipelines included, which is why "
        "the totals here exceed the chain above.",
    )
    stats = result.get_operator_stats()
    table(
        ["Operator", "Count"],
        [[op.replace("Operator", ""), str(count)]
         for op, count in sorted(stats.items(), key=lambda x: x[1], reverse=True)],
    )

    section(
        'replace_table("StormEvents", "StormEvents_v2")',
        "An AST-aware rename across every reference position.",
    )
    kql(result.replace_table("StormEvents", "StormEvents_v2"))
    note(
        "Both occurrences change: the one inside the `let` body and the "
        "outer source. A string replace would also rewrite any column or "
        "literal that happens to contain 'StormEvents'."
    )

    section(
        "format_query()",
        "Canonical formatting through Microsoft's KustoCodeService.",
    )
    kql(format_query(query_text))
    note("The formatter ships in the same Kusto.Language library as the "
         "parser, so the layout follows Microsoft's conventions rather than "
         "this project's. Note `kind = inner`, which the formatter spaces "
         "out.")

    takeaway(
        "Tier 1 answers syntactic questions about a query without a schema "
        "and without .NET knowledge on your side. Reach for tier 2 when you "
        "need types, column provenance, or a digest that separates two "
        "queries by meaning.",
        more="docs/tier1-syntax-tree.md and docs/semantic-hash.md",
    )


def main() -> None:
    analyze(QUERY)


if __name__ == "__main__":
    main()
