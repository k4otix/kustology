# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Canonical formatting and parser diagnostics.

``format_query`` runs Microsoft's public ``KustoCodeService.GetFormattedText``
to reformat a query into canonical shape — the same formatter Azure Data
Explorer's web UI uses. ``validate`` returns structured parser diagnostics
with codes, severities, and source offsets, suitable for plugging into a
linter or CI step.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from pykusto_language import format_query, validate


MESSY_QUERY = (
    'StormEvents|where EventType=="Tornado"|   summarize TotalDeaths=sum(DeathsDirect),'
    'TotalInjuries=sum(InjuriesDirect) by State|order by TotalDeaths desc'
)

BROKEN_QUERY = "StormEvents | where EventType ==  | project State, DeathsDirect"


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def show_query(label: str, query: str) -> None:
    print(f"{label}:")
    for line in query.splitlines() or [""]:
        print(f"  {line}")


def main() -> None:
    banner("format_query() — canonical formatting")
    show_query("Input (single line, no spacing)", MESSY_QUERY)
    print()
    show_query("Output (Microsoft's KustoCodeService)", format_query(MESSY_QUERY))

    banner("validate() — structured parser diagnostics")
    show_query("Input (intentionally broken)", BROKEN_QUERY)
    print()
    diagnostics = validate(BROKEN_QUERY)
    if not diagnostics:
        print("Diagnostics: (none)")
    else:
        print("Diagnostics:")
        for d in diagnostics:
            print(f"  [{d['severity']} {d['code']}] at char {d['start']} "
                  f"(length {d['length']}): {d['message']}")


if __name__ == "__main__":
    main()
