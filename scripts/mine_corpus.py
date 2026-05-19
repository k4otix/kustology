#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Bulk-process a KQL corpus through the IR builder and report coverage gaps.

For every ``.kql`` file in the corpus, build the IR and walk it looking for:

* ``UnknownExpr`` — an expression kind the builder didn't recognize.
* ``UnknownSource`` — a pipeline whose source wasn't a TableRef / LetRef.
* unspecialized ``Operator`` — fall-through from ``_visit_operator``.

Emits a JSON report with per-kind counts and a sample of source queries that
triggered each. Used as both a manual diagnostic and a CI signal — see
``tests/test_corpus_unknowns.py``.

Usage
-----
    python scripts/mine_corpus.py                                  # bundled fixtures
    python scripts/mine_corpus.py --corpus path/to/queries
    python scripts/mine_corpus.py --remote-microsoft               # clone Microsoft's repo
    python scripts/mine_corpus.py --output reports/unknowns.json   # custom output
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "complex_queries"
DEFAULT_OUTPUT = REPO_ROOT / "reports" / "unknowns.json"
MICROSOFT_REPO = "https://github.com/microsoft/Kusto-Query-Language.git"


def _iter_kql(path: Path) -> Iterable[tuple[str, str]]:
    """Yield ``(name, text)`` for every readable .kql under `path`."""
    for kql in sorted(path.rglob("*.kql")):
        try:
            text = kql.read_text(errors="replace").strip()
        except OSError:
            continue
        if text:
            yield (str(kql.relative_to(path)), text)


def _walk(ir, unknown_exprs: Counter, unknown_sources: Counter, unspecialized_ops: Counter,
          per_kind_examples: dict, query_name: str) -> None:
    """Walk an IR for coverage gaps, accumulating counts and examples."""
    from kustology.ir import (
        CompoundNamedExpr, NamedExpr, Operator, UnknownExpr, UnknownSource,
    )

    def walk_expr(expr):
        if expr is None:
            return
        if isinstance(expr, UnknownExpr):
            unknown_exprs[expr.ast_kind] += 1
            per_kind_examples[expr.ast_kind].append(query_name)
        for attr in (
            "left", "right", "operand", "expression", "selector",
            "target", "column", "low", "high",
        ):
            child = getattr(expr, attr, None)
            if child is not None:
                walk_expr(child)
        for attr in ("operands", "args", "values"):
            children = getattr(expr, attr, None) or []
            for c in children:
                walk_expr(c)
        if isinstance(expr, (NamedExpr, CompoundNamedExpr)):
            walk_expr(expr.expression)

    def walk_pipeline(pipeline):
        if isinstance(pipeline.source, UnknownSource):
            unknown_sources[pipeline.source.raw_text or "<empty>"] += 1
            per_kind_examples["<UnknownSource>"].append(query_name)
        for op in pipeline.operators:
            if type(op) is Operator:
                unspecialized_ops["<bare Operator>"] += 1
                per_kind_examples["<bare Operator>"].append(query_name)
            if hasattr(op, "predicate"):
                walk_expr(op.predicate)
            if hasattr(op, "assignments"):
                for a in op.assignments:
                    walk_expr(a.expr)
            if hasattr(op, "aggregations"):
                for a in op.aggregations:
                    walk_expr(a.expr)
            if hasattr(op, "columns"):
                for c in op.columns:
                    walk_expr(getattr(c, "expr", c))
            if hasattr(op, "right") and op.right is not None and hasattr(op.right, "operators"):
                walk_pipeline(op.right)
            if hasattr(op, "pipelines") and op.pipelines:
                for sub in op.pipelines:
                    walk_pipeline(sub)

    walk_pipeline(ir.main_pipeline)


def _clone_microsoft(into: Path) -> Path:
    """Shallow-clone microsoft/Kusto-Query-Language into `into` and return the path."""
    print(f"Cloning {MICROSOFT_REPO} (shallow) into {into}…", file=sys.stderr)
    subprocess.run(
        ["git", "clone", "--depth", "1", MICROSOFT_REPO, str(into)],
        check=True,
    )
    return into


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--corpus", type=Path, default=DEFAULT_CORPUS,
        help="Directory containing .kql files (default: bundled fixtures).",
    )
    parser.add_argument(
        "--remote-microsoft", action="store_true",
        help="Shallow-clone microsoft/Kusto-Query-Language and mine its fixtures.",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Where to write the JSON report.",
    )
    parser.add_argument(
        "--soft", action="store_true",
        help="Always exit 0, even if unknowns appear (annotation-only mode).",
    )
    parser.add_argument(
        "--max-examples-per-kind", type=int, default=3,
        help="Trim the example list per ast_kind.",
    )
    args = parser.parse_args()

    from kustology.ir import IRBuilder

    builder = IRBuilder()

    unknown_exprs: Counter[str] = Counter()
    unknown_sources: Counter[str] = Counter()
    unspecialized_ops: Counter[str] = Counter()
    per_kind_examples: dict[str, list[str]] = defaultdict(list)

    processed = 0
    errored: list[tuple[str, str]] = []

    sources: list[tuple[str, Path]] = [(str(args.corpus), args.corpus)]
    tmp_clone: Path | None = None

    if args.remote_microsoft:
        tmp_clone = Path(tempfile.mkdtemp(prefix="ms-kql-corpus-"))
        try:
            _clone_microsoft(tmp_clone)
            sources.append(("microsoft/Kusto-Query-Language", tmp_clone))
        except subprocess.CalledProcessError as e:
            print(f"warn: microsoft corpus clone failed: {e}", file=sys.stderr)

    for label, root in sources:
        if not root.is_dir():
            print(f"warn: corpus {label!r} not found at {root}", file=sys.stderr)
            continue
        for name, query in _iter_kql(root):
            qname = f"{label}:{name}"
            try:
                ir = builder.build(query)
            except Exception as e:
                errored.append((qname, f"{type(e).__name__}: {e}"))
                continue
            processed += 1
            _walk(ir, unknown_exprs, unknown_sources, unspecialized_ops,
                  per_kind_examples, qname)

    report = {
        "processed_queries": processed,
        "errored_queries": [{"name": n, "error": err} for n, err in errored],
        "unknown_expr_counts": dict(unknown_exprs),
        "unknown_source_counts": dict(unknown_sources),
        "unspecialized_op_counts": dict(unspecialized_ops),
        "examples": {
            k: v[: args.max_examples_per_kind]
            for k, v in per_kind_examples.items()
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.output.relative_to(REPO_ROOT) if args.output.is_absolute() else args.output}")
    print(f"  processed: {processed}")
    print(f"  errored:   {len(errored)}")
    print(f"  unknown expressions:   {sum(unknown_exprs.values())}")
    print(f"  unknown sources:       {sum(unknown_sources.values())}")
    print(f"  unspecialized ops:     {sum(unspecialized_ops.values())}")

    if tmp_clone is not None:
        # Best-effort cleanup; ignore failures.
        import shutil
        shutil.rmtree(tmp_clone, ignore_errors=True)

    if args.soft:
        return 0
    # Fail only on coverage gaps the IR builder *should* close: UnknownExpr
    # (an expression kind that wasn't dispatched) and bare-Operator
    # fallthrough. UnknownSource is a known limitation for sub-pipeline
    # sources (materialize, parenthesized sub-queries without a leading
    # table) — it's surfaced in the report but doesn't fail the build.
    real_gaps = bool(unknown_exprs or unspecialized_ops)
    return 1 if real_gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
