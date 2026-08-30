# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Evaluate graded similarity's defaults against a public detection corpus.

``docs/similarity.md`` describes ``min_size=3`` and ``k=128`` qualitatively;
this script is the mechanism that produced those judgments, and lets a
maintainer re-derive them:

    python scripts/eval_similarity.py --sentinel-repo /path/to/Azure-Sentinel

Harvests ``Detections/**``, ``Hunting Queries/**``,
``Solutions/*/Analytic Rules/**`` and ``Solutions/*/Hunting Queries/**``
(case-insensitive; the checkout also spells these "Analytics Rules" and
"Hunting queries"), parses each query's IR, and measures per ``--min-size``:
retrieval quality under IDF-weighted Jaccard against two ground truths
(files sharing an ``id`` or basename whose whole-query digests differ;
regex-grouped Threat Intelligence "family" rules), and how closely
``similarity_sketch`` tracks exact Jaccard at each ``--k``. The IDF
weighting lives here, not in the library -- kustology never sees a corpus.
Read-only: it never writes into the checkout.
"""

from __future__ import annotations

import argparse
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import yaml

from kustology import parse
from kustology.ir import (
    compute_semantic_hash,
    similarity,
    similarity_sketch,
    sketch_similarity,
    subtree_hashes,
)

DF_CAP_FRACTION = 0.07  # digests posted to a bigger share of the corpus are dropped (round(DF_CAP_FRACTION * n) postings)
SAMPLE_PAIRS = 500      # pairs sampled for sketch-vs-exact fidelity, per (min_size, k)

FAMILY_DEFAULT = r"IPEntity_|DomainEntity_|URLEntity_|EmailEntity_|FileEntity_|FileHashEntity_"

_TOP_LEVEL = re.compile(r"detections|hunting\s+queries", re.IGNORECASE)
_SOLUTION_SUB = re.compile(r"analytics?\s+rules|hunting\s+queries", re.IGNORECASE)
_FAMILY_SCOPE = re.compile(r"^Solutions/[^/]*Threat Intelligence[^/]*/Analytics?\s+Rules/", re.IGNORECASE)


class Entry(NamedTuple):
    path: Path
    rel: str
    id: str | None
    query: str


class Parsed(NamedTuple):
    entry: Entry
    digest: str
    ir: object


def _harvest_paths(root: Path) -> list[Path]:
    """Every YAML under the four harvested directory shapes, matched case-insensitively."""
    paths: list[Path] = []
    for top in sorted(root.iterdir()):
        if top.is_dir() and _TOP_LEVEL.fullmatch(top.name):
            paths += sorted(top.rglob("*.yaml")) + sorted(top.rglob("*.yml"))
    solutions = root / "Solutions"
    if solutions.is_dir():
        for solution in sorted(solutions.iterdir()):
            if not solution.is_dir():
                continue
            for sub in sorted(solution.iterdir()):
                if sub.is_dir() and _SOLUTION_SUB.fullmatch(sub.name):
                    paths += sorted(sub.rglob("*.yaml")) + sorted(sub.rglob("*.yml"))
    return paths


def _load_entries(root: Path, paths: list[Path], limit: int | None) -> list[Entry]:
    """Read each YAML and keep the dicts with a non-empty ``query`` string."""
    entries: list[Entry] = []
    for path in paths:
        try:
            with path.open(encoding="utf-8-sig") as f:
                doc = yaml.safe_load(f)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        query = doc.get("query")
        if not isinstance(query, str) or not query.strip():
            continue
        rid = doc.get("id")
        entries.append(Entry(path, path.relative_to(root).as_posix(), rid if isinstance(rid, str) else None, query))
        if limit is not None and len(entries) >= limit:
            break
    return entries


def _parse_entries(entries: list[Entry]) -> tuple[list[Parsed], int]:
    parsed: list[Parsed] = []
    failures = 0
    for entry in entries:
        try:
            ir = parse(entry.query).to_ir(semantic_hash=False)
        except Exception:
            failures += 1
            continue
        parsed.append(Parsed(entry, compute_semantic_hash(ir), ir))
    return parsed, failures


def _ground_truth(
    parsed: list[Parsed], family_pattern: re.Pattern[str],
) -> tuple[dict[int, set[int]], dict[int, tuple[set[int], int]]]:
    """Map each query index to its positives: dup/near-dup counterparts, and family group.

    A dup/near-dup group shares an ``id`` or a basename, kept only where
    the whole-query digest differs -- an exact duplicate is already caught
    by ``semantic_hash`` and carries no retrieval signal here. A family
    group is entries under a Threat Intelligence ``Analytic Rules``
    directory whose basename matches ``family_pattern`` the same way; its
    int is precision@k's denominator (group size - 1).
    """
    by_id: dict[str, list[int]] = defaultdict(list)
    by_name: dict[str, list[int]] = defaultdict(list)
    by_family: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(parsed):
        if p.entry.id:
            by_id[p.entry.id].append(i)
        by_name[p.entry.path.name].append(i)
        if _FAMILY_SCOPE.search(p.entry.rel):
            m = family_pattern.match(p.entry.path.name)
            if m:
                by_family[m.group(0)].append(i)

    dup_queries: dict[int, set[int]] = defaultdict(set)
    for group in (*by_id.values(), *by_name.values()):
        for i in (g for g in group if len(group) >= 2):
            dup_queries[i] |= {j for j in group if j != i and parsed[j].digest != parsed[i].digest}
    dup_queries = {i: positives for i, positives in dup_queries.items() if positives}

    family_queries = {
        i: ({j for j in group if j != i}, len(group) - 1)
        for group in by_family.values() if len(group) >= 2
        for i in group
    }
    return dup_queries, family_queries


def _idf_candidates(bags: list[frozenset[str]]) -> tuple[dict[int, dict[int, float]], dict[int, float]]:
    """Weighted digest overlaps per pair, via an inverted index capped at a share of the corpus.

    A digest posted to more than ``DF_CAP_FRACTION`` of the corpus is
    dropped -- not because its idf weight is negligible (it usually isn't:
    at the cap boundary it can still be a third or more of the top idf
    weight), but because accumulating every pair that shares it would make
    this quadratic in corpus size for a shrinking marginal return. A
    dropped digest is excluded from both the intersection (``neighbors``)
    and the union (``weight``): it can never contribute to an intersection
    once its postings are capped, so leaving it in the union denominator
    would only deflate every score touching it for no compensating gain.
    A digest posted to exactly one document stays in the union weight --
    it belongs in the denominator the way ``docs/similarity.md``'s IDF
    snippet computes one -- but never appears in ``neighbors``, since a
    posting list of length one has no pairs to accumulate.
    """
    n = len(bags)
    df_cap = max(2, round(DF_CAP_FRACTION * n))
    inverted: dict[str, list[int]] = defaultdict(list)
    for i, bag in enumerate(bags):
        for digest in bag:
            inverted[digest].append(i)
    below_cap = {digest: docs for digest, docs in inverted.items() if len(docs) <= df_cap}
    idf = {digest: math.log(n / len(docs)) for digest, docs in below_cap.items()}
    neighbors: dict[int, dict[int, float]] = defaultdict(dict)
    for digest, docs in below_cap.items():
        if len(docs) < 2:
            continue
        w = idf[digest]
        for a in range(len(docs)):
            for b in range(a + 1, len(docs)):
                i, j = docs[a], docs[b]
                neighbors[i][j] = neighbors[i].get(j, 0.0) + w
                neighbors[j][i] = neighbors[j].get(i, 0.0) + w
    weight = {i: sum(idf[d] for d in bag if d in idf) for i, bag in enumerate(bags)}
    return neighbors, weight


def _ranked(neighbors, weight, i: int, top_n: int) -> list[int]:
    """The up-to-``top_n`` candidates for ``i`` with the highest weighted Jaccard score."""
    def score(j: int, inter: float) -> float:
        union = weight[i] + weight[j] - inter
        return inter / union if union else 0.0

    scored = sorted(((score(j, inter), j) for j, inter in neighbors.get(i, {}).items()), reverse=True)
    return [j for _, j in scored[:top_n]]


def _mean_over(neighbors, weight, items: dict[int, tuple[set[int], int]], score) -> tuple[float, int]:
    """Average ``score(top_k, positives, k)`` over each query's ranked neighbors."""
    if not items:
        return 0.0, 0
    total = sum(score(_ranked(neighbors, weight, i, k), positives, k) for i, (positives, k) in items.items())
    return total / len(items), len(items)


def _hit(top: list[int], positives: set[int], _k: int) -> float:
    return 1.0 if positives.intersection(top) else 0.0


def _precision(top: list[int], positives: set[int], k: int) -> float:
    return sum(1 for j in top if j in positives) / k if k else 0.0


def _sample_pairs(indices: list[int], count: int, seed: int) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    n = len(indices)
    if n < 2:
        return []
    count = min(count, n * (n - 1) // 2)
    seen: set[tuple[int, int]] = set()
    while len(seen) < count:
        a, b = rng.randrange(n), rng.randrange(n)
        if a != b:
            i, j = indices[a], indices[b]
            seen.add((i, j) if i < j else (j, i))
    return sorted(seen)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))]


def _sketch_fidelity(bags: list[frozenset[str]], k_values: list[int]) -> tuple[dict[int, tuple[float, float]], int]:
    """Return {k: (mean |exact - sketch|, p95 |exact - sketch|)} over sampled pairs."""
    pairs = _sample_pairs([i for i, bag in enumerate(bags) if bag], SAMPLE_PAIRS, seed=0)
    needed = {i for pair in pairs for i in pair}
    results: dict[int, tuple[float, float]] = {}
    for k in k_values:
        sketches = {i: similarity_sketch(bags[i], k=k) for i in needed}
        errs = [abs(similarity(bags[i], bags[j]) - sketch_similarity(sketches[i], sketches[j])) for i, j in pairs]
        results[k] = (statistics.fmean(errs) if errs else 0.0, _p95(errs))
    return results, len(pairs)


def _report_min_size(parsed: list[Parsed], min_size: int, k_values: list[int], dup_queries, family_queries) -> None:
    bags = [frozenset(h.digest for h in subtree_hashes(p.ir, min_size=min_size)) for p in parsed]
    mean_bag_size = statistics.fmean(len(bag) for bag in bags) if bags else 0.0
    neighbors, weight = _idf_candidates(bags)

    dup_1 = {i: (pos, 1) for i, pos in dup_queries.items()}
    dup_5 = {i: (pos, 5) for i, pos in dup_queries.items()}
    recall_1 = _mean_over(neighbors, weight, dup_1, _hit)
    recall_5 = _mean_over(neighbors, weight, dup_5, _hit)
    precision = _mean_over(neighbors, weight, family_queries, _precision)

    print(f"\n== min_size={min_size} (mean bag size {mean_bag_size:.1f}) ==")
    print(f"  recall@1              (n={recall_1[1]:4d} dup/near-dup queries): {recall_1[0]:.3f}")
    print(f"  recall@5              (n={recall_5[1]:4d} dup/near-dup queries): {recall_5[0]:.3f}")
    print(f"  precision@(family-1)  (n={precision[1]:4d} family queries)     : {precision[0]:.3f}")

    fidelity, n_pairs = _sketch_fidelity(bags, k_values)
    print(f"  sketch fidelity (exact vs. sketch, {n_pairs} sampled pairs):")
    print(f"    {'k':>5}  {'mean|err|':>10}  {'p95|err|':>10}")
    for k in k_values:
        mean_err, p95_err = fidelity[k]
        print(f"    {k:>5}  {mean_err:>10.4f}  {p95_err:>10.4f}")


def _int_list(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sentinel-repo", required=True,
        help="Path to a local Azure-Sentinel checkout (the directory containing 'Solutions/').",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of queries evaluated (default: all).")
    parser.add_argument("--min-size", type=_int_list, default=[1, 2, 3, 4, 6, 8], help="Comma-separated min_size values.")
    parser.add_argument("--k", type=_int_list, default=[64, 128, 256], help="Comma-separated similarity_sketch k values.")
    parser.add_argument(
        "--family", default=FAMILY_DEFAULT,
        help="Regex matched against basenames under a Threat Intelligence Analytic Rules/ "
             "directory; entries whose match text agrees form a family group.",
    )
    args = parser.parse_args(argv)

    sentinel_root = Path(args.sentinel_repo).resolve()
    if not (sentinel_root / "Solutions").is_dir():
        print(
            f"error: {sentinel_root} doesn't look like an Azure-Sentinel checkout (no Solutions/)",
            file=sys.stderr,
        )
        return 2

    paths = _harvest_paths(sentinel_root)
    entries = _load_entries(sentinel_root, paths, args.limit)
    parsed, failures = _parse_entries(entries)
    print(f"harvested {len(paths)} yaml files, loaded {len(entries)} with a query, "
          f"parsed {len(parsed)} ({failures} failed to parse)")
    if len(parsed) < 2:
        print("error: fewer than 2 parsed queries -- nothing to compare", file=sys.stderr)
        return 1

    dup_queries, family_queries = _ground_truth(parsed, re.compile(args.family))
    print(f"ground truth: {len(dup_queries)} dup/near-dup queries (shared id or basename, "
          f"digests differ), {len(family_queries)} family-grouped queries")

    for min_size in args.min_size:
        _report_min_size(parsed, min_size, args.k, dup_queries, family_queries)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
