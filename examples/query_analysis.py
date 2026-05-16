# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Static analysis on a parsed KQL query.

Demonstrates the four ``KustoQuery`` methods that turn a query into
queryable structure:

  - ``get_referenced_tables()``  — every table source, including joined,
                                   union'd, and ``database()``-qualified
                                   references, not just the leftmost.
  - ``get_referenced_columns()`` — every column reference, with function
                                   callees and ``$``-prefixed join sides
                                   filtered out.
  - ``get_structural_hash()``    — SHA-256 over the AST shape; stable
                                   across literal/whitespace changes.
  - ``replace_table()``          — surgical AST-aware rename across every
                                   reference position.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import parse


QUERY = """\
StormEvents
| where StartTime > ago(7d) and State == "TEXAS"
| join kind=inner (
    StormEvents
    | where EventType == "Tornado"
    | summarize TornadoesPerEpisode = count() by EpisodeId
) on EpisodeId
| project StartTime, State, EventType, TornadoesPerEpisode"""


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def show_query(label: str, query: str) -> None:
    print(f"{label}:")
    for line in query.splitlines():
        print(f"  {line}")


def main() -> None:
    show_query("Input query", QUERY)

    result = parse(QUERY)

    banner("get_referenced_tables()")
    tables = sorted(result.get_referenced_tables())
    print(f"  {tables}")
    print("  Note: StormEvents appears twice in the source (outer pipeline + joined)")
    print("        sub-pipeline) but is reported once.")

    banner("get_referenced_columns()")
    columns = sorted(result.get_referenced_columns())
    print(f"  {columns}")

    banner("get_structural_hash()")
    print(f"  {result.get_structural_hash()}")
    print('  → unchanged if you swap "TEXAS" for "OHIO" or rewhitespace the query.')

    banner('replace_table("StormEvents", "StormEvents_v2")')
    rewritten = result.replace_table("StormEvents", "StormEvents_v2")
    show_query("Output", rewritten)
    print()
    print("  Both occurrences (outer source AND joined sub-pipeline) are renamed.")
    print("  A naïve string replace would also (incorrectly) rewrite a column or")
    print("  literal containing 'StormEvents'.")


if __name__ == "__main__":
    main()
