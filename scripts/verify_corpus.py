#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify a corpus of .kql files against the kustology IR.

Schema-loading priority:
  (B) tests/fixtures/sentinel_schemas.json — extracted ahead of time, in repo
      (gitignored). Run scripts/extract_sentinel_schemas.py to produce it.
  (C) Live workspace via the Azure CLI — requires ``az login`` and the
      ``KUSTOLOGY_WORKSPACE_ID`` environment variable (or ``--workspace-id``)
      set to a Log Analytics workspace you can read. Also requires the
      ``log-analytics`` az extension (``az extension add --name log-analytics``).
  (A) Synthesized — empty-column schema per table-name scan of the corpus.
      Stops .NET "table not found" diagnostics but provides no column info.

Per query: build → enrich → roundtrip. Classifies six structural failure
categories. Semantic diagnostics from the .NET binder are recorded as
informational metadata only; they don't count as failures.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kustology.ir import (
    IRBuilder,
    Operator,
    QueryIR,
    SchemaAttacher,
    UnknownExpr,
    UnknownSource,
)
from kustology.utils.analysis import build_global_state

CATEGORIES = [
    "builder_exception", "binder_exception",
    "bare_operator", "unknown_expr", "unknown_source",
    "roundtrip_mismatch",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "tests/fixtures/sentinel_sample"
DEFAULT_SCHEMAS = REPO_ROOT / "tests/fixtures/sentinel_schemas.json"
DEFAULT_OUTPUT = REPO_ROOT / "reports/sentinel_sample_verdict.json"

# Option C (live az lookup) is a maintainer-only fallback. The workspace ID is
# environment-specific; export KUSTOLOGY_WORKSPACE_ID or pass --workspace-id to
# use it. We do NOT hardcode an ID here — that would embed a tenant-specific
# identifier in an OSS distribution.
_WORKSPACE_ID_ENV = "KUSTOLOGY_WORKSPACE_ID"

# Resolved from PATH at startup; falls through to option A if not found.
_AZ_ON_PATH = shutil.which("az")
DEFAULT_AZ_BIN = Path(_AZ_ON_PATH) if _AZ_ON_PATH else Path()

KQL_KEYWORDS = {
    "let", "where", "project", "extend", "summarize",
    "join", "union", "take", "top", "sort", "distinct",
    "by", "on", "and", "or", "not", "in", "between",
}

TABLE_NAME_PATTERN = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\b\s*\n?\s*\|")


# ---------------------------------------------------------------------------
# Schema loading: B → C → A
# ---------------------------------------------------------------------------

def load_schemas_b(path: Path) -> dict[str, dict[str, str]] | None:
    """Option B: load extracted JSON schemas from the in-repo fixture."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _scan_table_names(corpus_root: Path) -> set[str]:
    found: set[str] = set()
    for path in corpus_root.rglob("*.kql"):
        body = path.read_text(encoding="utf-8")
        for m in TABLE_NAME_PATTERN.finditer(body):
            name = m.group(1)
            if name.lower() in KQL_KEYWORDS:
                continue
            found.add(name)
    return found


def load_schemas_c(az_bin: Path,
                   corpus_root: Path,
                   workspace_id: str | None) -> dict[str, dict[str, str]] | None:
    """Option C: query the live workspace via the Azure CLI.

    Runs ``az monitor log-analytics query --workspace <id> --analytics-query
    "T | getschema | project ColumnName, ColumnType"`` per unique table found
    in the corpus and parses the JSON response. Requires ``az login``, a
    workspace ID (env var ``KUSTOLOGY_WORKSPACE_ID`` or ``--workspace-id``), and
    the ``log-analytics`` az extension. Returns None if any prerequisite is
    missing or no tables can be resolved.
    """
    if not az_bin or not az_bin.is_file():
        return None
    if not workspace_id:
        return None

    needed = _scan_table_names(corpus_root)
    if not needed:
        return None

    schemas: dict[str, dict[str, str]] = {}
    for table in sorted(needed):
        try:
            result = subprocess.run(
                [str(az_bin),
                 "monitor", "log-analytics", "query",
                 "--workspace", workspace_id,
                 "--analytics-query",
                 f"{table} | getschema | project ColumnName, ColumnType",
                 "-o", "json"],
                capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            rows = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        cols: dict[str, str] = {}
        for row in rows:
            cname = row.get("ColumnName")
            ctype = row.get("ColumnType", "unknown")
            if cname:
                cols[cname] = ctype
        if cols:
            schemas[table] = cols

    return schemas if schemas else None


def load_schemas_a(corpus_root: Path) -> dict[str, dict[str, str]]:
    """Option A: synthesize empty-column schemas by scanning the corpus for
    table-name patterns. Stops .NET 'table not found' diagnostics."""
    return {name: {} for name in sorted(_scan_table_names(corpus_root))}


def load_schemas(in_repo: Path, corpus: Path,
                 az_bin: Path,
                 workspace_id: str | None) -> tuple[dict, str]:
    """Try B → C → A. Return (schemas, source_label)."""
    b = load_schemas_b(in_repo)
    if b is not None:
        return b, "in-repo (option B)"
    c = load_schemas_c(az_bin, corpus, workspace_id)
    if c is not None:
        return c, "live workspace via az CLI (option C)"
    return load_schemas_a(corpus), "synthesized from corpus (option A)"


# ---------------------------------------------------------------------------
# Per-query verification
# ---------------------------------------------------------------------------

def _walk(node: Any):
    """Yield every pydantic-model node reachable from `node`."""
    if not isinstance(node, BaseModel):
        return
    yield node
    for field_name in type(node).model_fields:
        try:
            val = getattr(node, field_name)
        except Exception:
            continue
        if isinstance(val, BaseModel):
            yield from _walk(val)
        elif isinstance(val, list):
            for v in val:
                if isinstance(v, BaseModel):
                    yield from _walk(v)


def verify_query(builder: IRBuilder, attacher: SchemaAttacher,
                 qid: str, body: str) -> dict | None:
    failure: dict = {"id": qid, "categories": []}

    try:
        ir = builder.build(body)
    except Exception as e:
        failure["categories"].append("builder_exception")
        failure["builder_error"] = f"{type(e).__name__}: {e}"
        return failure

    bare_ops: list[str] = []
    unknown_exprs: list[str] = []
    unknown_sources: list[str] = []
    for n in _walk(ir.main_pipeline):
        if type(n) is Operator:
            bare_ops.append("Operator")
        elif isinstance(n, UnknownExpr):
            unknown_exprs.append(n.ast_kind or "?")
        elif isinstance(n, UnknownSource):
            unknown_sources.append(n.raw_text[:80])
    if bare_ops:
        failure["categories"].append("bare_operator")
        failure["bare_operators"] = bare_ops
    if unknown_exprs:
        failure["categories"].append("unknown_expr")
        failure["unknown_exprs"] = unknown_exprs
    if unknown_sources:
        failure["categories"].append("unknown_source")
        failure["unknown_sources"] = unknown_sources

    try:
        attacher.enrich(ir)
    except Exception as e:
        failure["categories"].append("binder_exception")
        failure["binder_error"] = f"{type(e).__name__}: {e}"
        return failure

    try:
        dumped = ir.model_dump_json()
        reloaded = QueryIR.model_validate_json(dumped)
        if ir.model_dump() != reloaded.model_dump():
            failure["categories"].append("roundtrip_mismatch")
            failure["roundtrip_note"] = "model_dump() differs across roundtrip"
    except Exception as e:
        failure["categories"].append("roundtrip_mismatch")
        failure["roundtrip_error"] = f"{type(e).__name__}: {e}"

    # Informational only — semantic diagnostics from the .NET binder.
    semantic = [
        d.message for d in ir.diagnostics
        if d.severity == "error"
        and (("not a column" in (d.message or ""))
             or ("not found" in (d.message or "")))
    ]
    if semantic:
        failure.setdefault("info", {})["semantic_diagnostics"] = semantic[:5]

    return failure if failure["categories"] else None


def iter_corpus(root: Path):
    for path in sorted(root.rglob("*.kql")):
        yield str(path.relative_to(root)), path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                        help="Directory of .kql files to verify.")
    parser.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS,
                        help="Option B: in-repo JSON schemas (gitignored).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Where to write the per-query verdict JSON.")
    parser.add_argument("--az-bin", type=Path, default=DEFAULT_AZ_BIN,
                        help="Path to the Azure CLI binary (option C; defaults "
                             "to $(which az) if available). Requires "
                             "`az login` and the `log-analytics` extension "
                             "(`az extension add --name log-analytics`).")
    parser.add_argument("--workspace-id", default=os.environ.get(_WORKSPACE_ID_ENV),
                        help=f"Log Analytics workspace ID for option C (live "
                             f"az CLI lookup). Defaults to the "
                             f"{_WORKSPACE_ID_ENV} environment variable; if "
                             f"unset, option C is skipped.")
    args = parser.parse_args()

    schemas, schema_source = load_schemas(args.schemas, args.corpus,
                                          args.az_bin,
                                          args.workspace_id)
    print(f"schemas: {len(schemas)} tables loaded from {schema_source}")

    gs = build_global_state(schemas)
    builder = IRBuilder(global_state=gs)
    attacher = SchemaAttacher(schemas=schemas)

    passed: list[str] = []
    failures_by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    all_failures: list[dict] = []

    for qid, body in iter_corpus(args.corpus):
        verdict = verify_query(builder, attacher, qid, body)
        if verdict is None:
            passed.append(qid)
        else:
            all_failures.append(verdict)
            for cat in verdict["categories"]:
                failures_by_cat[cat].append(verdict)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "schema_source": schema_source,
        "schema_table_count": len(schemas),
        "passed_count": len(passed),
        "failed_count": len(all_failures),
        "category_counts": {k: len(v) for k, v in failures_by_cat.items()},
        "passed": passed,
        "failures": all_failures,
    }, indent=2), encoding="utf-8")

    print(f"passed: {len(passed)}")
    print(f"failed: {len(all_failures)}")
    for cat, hits in failures_by_cat.items():
        print(f"  {cat}: {len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
