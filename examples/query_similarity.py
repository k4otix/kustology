# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Graded similarity between queries that are alike but not identical.

``semantic_hash`` (see ``examples/semantic_hash_demo.py``) answers "is this
the same query" -- exact, on the whole query. Most of the time the real
question is softer: which of these detections share a skeleton, how much
of one's logic sits inside another, and where exactly two versions of a
rule diverge. ``kustology.ir.similarity`` answers that from the same
subtree digests ``semantic_hash`` is built from.

Three "sibling" detections below share one skeleton -- filter, aggregate,
threshold -- parameterized over table, operation, and threshold. A fourth,
unrelated query has none of that shape. Every number printed here is
computed at run time, and the file raises if a sibling pair ever stops
scoring above the unrelated one, or if two siblings turn out to have no
differing subtree.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

from _display import banner, kql, note, section, table, takeaway

from kustology import parse
from kustology.ir import (
    containment,
    differing_subtrees,
    similarity,
    similarity_sketch,
    sketch_similarity,
)


def _detection(table_name: str, operation: str, threshold: int) -> str:
    """Build a `where` + `summarize` + `where` detection over `table_name`."""
    return (
        f"{table_name}\n"
        f'| where TimeGenerated > ago(1d) and OperationName == "{operation}"\n'
        f"| summarize count_ = count() by Actor, bin(TimeGenerated, 1h)\n"
        f"| where count_ > {threshold}"
    )


QUERIES = {
    "A": _detection("SigninLogs", "UserLoggedIn", 10),
    "B": _detection("SigninLogs", "UserLoggedIn", 25),
    "C": _detection("AuditLogs", "ConsentToApplication", 5),
    "U": 'StormEvents | where EventType == "Flood" | project State, StartTime',
}


def main() -> None:
    banner(
        "Graded similarity",
        "Three detections below share one skeleton -- filter, aggregate, "
        "threshold -- over different tables, operations, and thresholds. A "
        "fourth query shares none of it. similarity(), containment(), and "
        "differing_subtrees() compare them by the subtree digests "
        "semantic_hash is built from, not by the whole-query digest.",
        "the siblings scoring well above the unrelated query on every "
        "measure, and the diff landing on the one operator that actually "
        "changed.",
    )

    ir = {name: parse(q).to_ir() for name, q in QUERIES.items()}

    section(
        "Whole-query digests",
        "semantic_hash treats these as four different queries -- including "
        "A and B, which differ only in a threshold.",
    )
    digests = {name: node.semantic_hash for name, node in ir.items()}
    for name in QUERIES:
        print(f"  {name}: {digests[name]}")
    if len(set(digests.values())) != len(digests):
        raise AssertionError("two demo queries share a semantic_hash -- they fail to make the point")
    note("semantic_hash is exact and whole-query. similarity() below is graded and per-subtree.")

    section(
        "Pairwise similarity",
        "similarity() is the symmetric overlap of subtree digests; "
        "containment(x, y) is the share of x's subtrees found inside y.",
    )
    names = list(QUERIES)
    pairs = [(x, y) for i, x in enumerate(names) for y in names[i + 1 :]]
    rows = []
    scored = []
    for x, y in pairs:
        sim = similarity(ir[x], ir[y])
        c_xy = containment(ir[x], ir[y])
        c_yx = containment(ir[y], ir[x])
        rows.append([f"{x}-{y}", f"{sim:.2f}", f"{c_xy:.2f}", f"{c_yx:.2f}"])
        scored.append((sim, x, y))
    table(["pair", "similarity", "containment(a,b)", "containment(b,a)"], rows)

    sibling_scores = [s for s, x, y in scored if "U" not in (x, y)]
    unrelated_scores = [s for s, x, y in scored if "U" in (x, y)]
    if min(sibling_scores) <= max(unrelated_scores):
        raise AssertionError("a sibling pair scored at or below a pair involving the unrelated query")

    best_sim, best_x, best_y = max(scored, key=lambda t: t[0])
    section(f"The two closest: {best_x} and {best_y}", f"similarity() = {best_sim:.2f}, the highest in the table above.")
    kql(QUERIES[best_x])
    kql(QUERIES[best_y])
    note("Same skeleton, different literal -- only a threshold moved.")

    section(
        "Where two siblings diverge",
        "differing_subtrees(a, b) reports the smallest subtrees of a "
        "that are missing from b; swap the arguments for b's side.",
    )
    a, b = ir["A"], ir["B"]
    a_not_in_b = differing_subtrees(a, b)
    b_not_in_a = differing_subtrees(b, a)
    if not a_not_in_b or not b_not_in_a:
        raise AssertionError("A and B differ by a threshold, but differing_subtrees() found nothing")
    for label, diffs, query in (
        ("A, not in B", a_not_in_b, QUERIES["A"]),
        ("B, not in A", b_not_in_a, QUERIES["B"]),
    ):
        print(f"  {label}:")
        for h in diffs:
            print(f"    {h.kind:<10} {h.span.text(query)!r}")
    note(
        "Only the threshold changed, so the smallest qualifying ancestor is "
        "the comparison itself, not the whole second `where` and not the root."
    )

    section(
        "Sketches",
        "similarity_sketch() estimates similarity() from a fixed-size "
        "sketch instead of the full digest bags -- built for comparing many "
        "queries at once, not just two.",
    )
    sketch_a = similarity_sketch(a)
    sketch_b = similarity_sketch(b)
    exact = similarity(a, b)
    estimate = sketch_similarity(sketch_a, sketch_b)
    print(f"  sketch size     : {len(sketch_a)} bytes")
    print(f"  exact           : {exact:.3f}")
    print(f"  sketch estimate : {estimate:.3f}")
    note("k=128 by default, which is why the sketch is 520 bytes. docs/similarity.md explains the choice.")

    takeaway(
        "Use similarity() and containment() to find related queries and "
        "template families, and differing_subtrees() to localize where two "
        "versions of a rule diverge -- semantic_hash alone only tells you "
        "they aren't identical.",
        more="docs/similarity.md",
    )


if __name__ == "__main__":
    main()
