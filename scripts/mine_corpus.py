#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Bulk-process a KQL corpus through the IR builder and report coverage gaps.

For every ``.kql`` file in the corpus, build the IR and walk **all** of it
with ``find_all`` — ``main_pipeline``, ``additional_pipelines``, every
``let`` binding's right-hand side, and every nested sub-pipeline — looking
for:

* ``UnknownExpr`` — an expression kind the builder didn't recognize.
* ``UnknownSource`` — a pipeline whose source wasn't a TableRef / LetRef.
* unspecialized ``Operator`` — fall-through from ``_visit_operator``.

The script emits a JSON report with per-kind counts and a sample of source
queries that triggered each. It serves as both a manual diagnostic and a CI
signal — the ``corpus-regression`` CI job invokes it directly.

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
from collections.abc import Iterable
from pathlib import Path

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
    """Walk an IR for coverage gaps, accumulating counts and examples.

    The generic ``find_all`` iterates ``model_fields``, so every nested
    sub-pipeline — a ``toscalar(...)`` or ``materialize(...)`` argument, a
    bare subquery — is reached without a hand-maintained list of attribute
    names to recurse into.

    ``find_all`` runs over the whole ``QueryIR``, not ``main_pipeline``
    alone, so every ``let`` binding's right-hand side (a tabular
    ``rhs_pipeline`` included) and ``additional_pipelines`` are covered
    too — a gap reachable only through one of those is still a gap.
    """
    from kustology.ir import Operator, UnknownExpr, UnknownOp, UnknownSource, find_all

    for expr in find_all(ir, UnknownExpr):
        unknown_exprs[expr.ast_kind] += 1
        per_kind_examples[expr.ast_kind].append(query_name)

    for src in find_all(ir, UnknownSource):
        unknown_sources[src.raw_text or "<empty>"] += 1
        per_kind_examples["<UnknownSource>"].append(query_name)

    for op in find_all(ir, Operator):
        # Two shapes of dispatch fallthrough. Strict identity catches a bare
        # base-class Operator (isinstance would match every subclass);
        # UnknownOp -- what _visit_operator emits on fallthrough -- is an
        # Operator subclass, so it needs its own isinstance arm.
        if type(op) is Operator:
            unspecialized_ops["<bare Operator>"] += 1
            per_kind_examples["<bare Operator>"].append(query_name)
        elif isinstance(op, UnknownOp):
            unspecialized_ops["<UnknownOp>"] += 1
            per_kind_examples["<UnknownOp>"].append(f"{query_name}:{op.ast_kind}")


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
    try:
        shown = args.output.relative_to(REPO_ROOT)
    except ValueError:
        # --output may point anywhere; an absolute path outside the repo is
        # fine, it just cannot be shown relative to it.
        shown = args.output
    print(f"Wrote {shown}")
    print(f"  processed: {processed}")
    print(f"  errored:   {len(errored)}")
    print(f"  unknown expressions:   {sum(unknown_exprs.values())}")
    print(f"  unknown sources:       {sum(unknown_sources.values())}")
    print(f"  unspecialized ops:     {sum(unspecialized_ops.values())}")

    if tmp_clone is not None:
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
