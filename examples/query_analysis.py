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


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def show_query(label: str, query: str) -> None:
    print(f"{label}:")
    for line in query.splitlines():
        print(f"  {line}")


def analyze(query_text: str) -> None:
    show_query("Input query", query_text)

    result = parse(query_text)
    diagnostics = validate(query_text)

    banner("validate()")
    print(f"  {len(diagnostics)} diagnostic(s)"
          + (" — query is syntactically valid" if not diagnostics else ""))
    for d in diagnostics:
        print(f"    [{d['severity']} {d['code']}] at char {d['start']}: {d['message']}")

    banner("get_structural_hash()")
    baseline = result.get_structural_hash()
    print(f"  {baseline}")
    # Demonstrated rather than asserted: each variant is hashed here, and
    # the verdict is computed. The hash is over the AST *skeleton* — node
    # kinds and shape — so it is blind to literal values and to identifiers,
    # and sensitive to the sequence of operators.
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
    for label, variant in variants.items():
        same = parse(variant).get_structural_hash() == baseline
        print(f"  {'same' if same else 'DIFF'}  {label}")
    print("  → Identifiers do not move it either, which is the part that")
    print("    surprises people: `where State == 'x'` and `where Region == 'x'`")
    print("    are one structural hash. For a digest that separates those,")
    print("    use tier 2's `semantic_hash` (see examples/semantic_hash_demo.py).")

    banner("get_referenced_tables()")
    tables = sorted(result.get_referenced_tables())
    print(f"  {tables}")
    print("  → `high_impact_states` is a `let` alias, not a table, and is")
    print("    correctly absent. So is the join's right-hand side, because")
    print("    that side reads the alias too.")

    banner("get_referenced_columns()")
    columns = sorted(result.get_referenced_columns())
    for col in columns:
        print(f"  {col}")
    print("  → Every name in a column position, which includes the ones this")
    print("    query *creates*: Casualties, TotalLoss, EventCount, TotalDamage")
    print("    are summarize/extend outputs, not columns of StormEvents. This")
    print("    is a syntactic answer; bind a schema and use tier 2's")
    print("    `find_all(ir, ColumnRef)` to get provenance per reference.")

    banner("get_referenced_functions()")
    functions = sorted(result.get_referenced_functions())
    print(f"  {functions}")

    banner("find_time_expressions()")
    time_windows = result.find_time_expressions()
    if not time_windows:
        print("  (no temporal expressions)")
    for text, start, length in time_windows:
        print(f"  {text!r:25s}  start={start:4d}  length={length}")

    banner("get_operator_chain()")
    # Operators only: the source table the pipeline reads from is not one,
    # so print it separately rather than expecting it at the head of the list.
    chain = result.get_operator_chain()
    flow = [str(node.Kind).replace("Operator", "") for node in chain]
    sources = sorted(result.get_referenced_tables())
    reading = f", reading {', '.join(sources)}" if sources else ""
    print(f"  {len(chain)} operators{reading}:")
    print("  " + (" -> ".join(flow) if flow else "(none)"))

    banner("get_operator_stats()")
    stats = result.get_operator_stats()
    for op, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {op.replace('Operator', ''):16s} {count}")

    banner('replace_table("StormEvents", "StormEvents_v2")')
    rewritten = result.replace_table("StormEvents", "StormEvents_v2")
    show_query("Output", rewritten)
    print()
    print("  Every occurrence (let-bound sub-pipeline AND outer source) is renamed.")
    print("  A naïve string replace would also (incorrectly) rewrite a column or")
    print("  literal containing 'StormEvents'.")

    banner("format_query() — canonical formatting")
    show_query("Output", format_query(query_text))


def main() -> None:
    analyze(QUERY)


if __name__ == "__main__":
    main()
