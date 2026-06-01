#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Sample 200 representative KQL queries from an Azure-Sentinel clone.

Phase A (50 queries): diversity picks — queries containing at least one rare
operator keyword (scan, make-graph, fork, facet, parse-kv, materialize,
mv-apply, partition, evaluate, find, top-nested, make-series,
sample-distinct, external_data, pivot, union isfuzzy, assert-schema).
Round-robin across keywords to avoid one dominating.

Phase B (150 queries): stratified random fill — Detections 50 / Hunting 60 /
Solutions 30 / Parsers 10. Random within each stratum, deterministic via
--seed.

Outputs (gitignored):
- tests/fixtures/sentinel_sample/{subfolder}/{slug}.kql — query bodies
- tests/fixtures/sentinel_sample/manifest.json — provenance
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import yaml

RARE_KEYWORDS = [
    "scan", "make-graph", "top-nested", "fork", "facet", "parse-kv",
    "materialize", "mv-apply", "partition", "evaluate", "find",
    "assert-schema", "make-series", "sample-distinct", "external_data",
    "pivot", "union isfuzzy",
]

STRATA = {
    "detections": ("Detections", 50),
    "hunting":    ("Hunting Queries", 60),
    "solutions":  ("Solutions", 30),
    "parsers":    ("Parsers", 10),
}
DIVERSITY_BUDGET = 50

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "tests/fixtures/sentinel_sample"


def extract_query_from_yaml(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as f:
            docs = list(yaml.safe_load_all(f))
    except (yaml.YAMLError, UnicodeDecodeError):
        return None
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key in ("query", "ParserQuery"):
            body = doc.get(key)
            if isinstance(body, str) and body.strip():
                return body.strip()
    return None


def iter_yaml(sentinel_root: Path, sub: str) -> Iterator[Path]:
    base = sentinel_root / sub
    if not base.exists():
        return
    for path in base.rglob("*.yaml"):
        yield path


def slugify(path: Path) -> str:
    stem = path.stem
    return re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:60]


def sample(sentinel_root: Path, seed: int) -> dict:
    rng = random.Random(seed)
    all_queries: list[tuple[Path, str, str]] = []
    for stratum_key, (sub, _) in STRATA.items():
        for path in iter_yaml(sentinel_root, sub):
            body = extract_query_from_yaml(path)
            if body is None or "moved to new location" in body.lower():
                continue
            all_queries.append((path, stratum_key, body))

    keyword_buckets: dict[str, list[int]] = {kw: [] for kw in RARE_KEYWORDS}
    for idx, (_, _, body) in enumerate(all_queries):
        low = body.lower()
        for kw in RARE_KEYWORDS:
            if kw in low:
                keyword_buckets[kw].append(idx)
    for kw in keyword_buckets:
        rng.shuffle(keyword_buckets[kw])

    diversity_picks: list[int] = []
    diversity_provenance: dict[int, str] = {}
    while len(diversity_picks) < DIVERSITY_BUDGET and any(keyword_buckets.values()):
        for kw in RARE_KEYWORDS:
            if not keyword_buckets[kw] or len(diversity_picks) >= DIVERSITY_BUDGET:
                continue
            idx = keyword_buckets[kw].pop()
            if idx in diversity_provenance:
                continue
            diversity_picks.append(idx)
            diversity_provenance[idx] = kw

    picked = set(diversity_picks)
    stratified_picks: dict[str, list[int]] = {}
    for stratum_key, (_, budget) in STRATA.items():
        pool = [i for i, (_, s, _) in enumerate(all_queries)
                if s == stratum_key and i not in picked]
        rng.shuffle(pool)
        stratified_picks[stratum_key] = pool[:budget]
        picked.update(pool[:budget])

    return {
        "all_queries": all_queries,
        "diversity_picks": diversity_picks,
        "diversity_provenance": diversity_provenance,
        "stratified_picks": stratified_picks,
    }


def _git_sha(repo: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
        ).strip()
    except Exception:
        return None


def write_sample(sentinel_root: Path, plan: dict) -> dict:
    all_q = plan["all_queries"]
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "sentinel_root": str(sentinel_root),
        "sentinel_sha": _git_sha(sentinel_root),
        "entries": [],
    }

    def emit(idx: int, subfolder: str, label: str | None) -> None:
        yaml_path, stratum, body = all_q[idx]
        out_dir = OUT_ROOT / subfolder
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = f"{slugify(yaml_path)}_{idx:05d}"
        out_path = out_dir / f"{slug}.kql"
        out_path.write_text(body + "\n", encoding="utf-8")
        manifest["entries"].append({
            "slug": slug,
            "subfolder": subfolder,
            "stratum": stratum,
            "source": str(yaml_path.relative_to(sentinel_root)),
            "rare_keyword": label,
        })

    for idx in plan["diversity_picks"]:
        emit(idx, "diversity", plan["diversity_provenance"][idx])
    for stratum_key, idxs in plan["stratified_picks"].items():
        for idx in idxs:
            emit(idx, stratum_key, None)

    (OUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sentinel-root", type=Path, required=True,
                        help="Path to a local Azure-Sentinel clone")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for diversity-based sampling (default: 42).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print counts without writing files")
    args = parser.parse_args()

    if not args.sentinel_root.exists():
        print(f"error: Azure-Sentinel not found at {args.sentinel_root}",
              file=sys.stderr)
        return 1
    plan = sample(args.sentinel_root, args.seed)
    total = len(plan["diversity_picks"]) + sum(
        len(v) for v in plan["stratified_picks"].values()
    )
    print(f"  diversity: {len(plan['diversity_picks'])}")
    for k, v in plan["stratified_picks"].items():
        print(f"  {k}: {len(v)}")
    print(f"  total: {total}")
    if args.dry_run:
        return 0
    manifest = write_sample(args.sentinel_root, plan)
    print(f"wrote {len(manifest['entries'])} queries to {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
