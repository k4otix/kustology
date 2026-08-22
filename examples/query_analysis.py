# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""End-to-end analysis of a non-trivial KQL query.

Walks every analyzer on ``KustoQuery`` against a multi-let,
join-bearing query:

  - ``validate()``                 — structured parser diagnostics.
  - ``get_structural_hash()``      — SHA-256 over the AST skeleton;
                                     stable across literal/whitespace
                                     changes.
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
    print(f"  {result.get_structural_hash()}")
    print('  → unchanged if you swap "TEXAS" for "OHIO" or rewhitespace the query.')

    banner("get_referenced_tables()")
    tables = sorted(result.get_referenced_tables())
    print(f"  {tables}")

    banner("get_referenced_columns()")
    columns = sorted(result.get_referenced_columns())
    for col in columns:
        print(f"  {col}")

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
    print(f"  {len(chain)} operators, reading {', '.join(sources)}:")
    print("  " + " -> ".join(flow))

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
