# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""End-to-end audit of a non-trivial KQL query.

Combines every analyzer on ``KustoQuery`` to print a structural breakdown
of a multi-let, multi-filter, join-bearing query: parser diagnostics,
structural fingerprint, table and column inventory, time-window list,
pipeline flow, operator counts, and a coarse complexity score.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

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


def audit(query_text: str) -> None:
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

    banner("get_referenced_tables()")
    tables = sorted(result.get_referenced_tables())
    print(f"  {tables}")

    banner("get_referenced_columns()")
    columns = sorted(result.get_referenced_columns())
    for col in columns:
        print(f"  {col}")

    banner("get_time_range()")
    time_windows = result.get_time_range()
    if not time_windows:
        print("  (no temporal expressions)")
    for text, start, length in time_windows:
        print(f"  {text!r:25s}  start={start:4d}  length={length}")

    banner("get_operator_chain()")
    chain = result.get_operator_chain()
    flow = []
    for node in chain:
        kind = str(node.Kind).replace("Operator", "")
        if kind == "NameReference":
            flow.append(f"[{node.ToString().strip()}]")
        else:
            flow.append(kind)
    print(f"  {len(chain)} steps:")
    print("  " + " -> ".join(flow))

    banner("get_operator_stats()")
    stats = result.get_operator_stats()
    for op, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
        print(f"  {op.replace('Operator', ''):16s} {count}")

    banner("complexity score (heuristic)")
    let_count = query_text.lower().count("let ")
    summarize_count = stats.get("SummarizeOperator", 0)
    join_count = stats.get("JoinOperator", 0)
    score = (let_count * 2) + (summarize_count * 3) + (join_count * 5) + (len(chain) // 2)
    band = "HIGH" if score > 25 else "MEDIUM" if score > 10 else "LOW"
    print(f"  {score} ({band})")
    print(f"    lets={let_count}  summarizes={summarize_count}  "
          f"joins={join_count}  chain={len(chain)}")

    banner("format_query() — canonical formatting (full output)")
    show_query("Output", format_query(query_text))


if __name__ == "__main__":
    audit(QUERY)
