# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Real-world Sentinel detection queries — coverage signal for the builder.

Each `.kql` file under ``tests/fixtures/complex_queries/`` was extracted from
a published Azure-Sentinel analytic rule (see
``scripts/extract_complex_corpus.py``). The test parametrizes over every file
and asserts that the builder doesn't fall back to ``UnknownExpr`` or to a
bare ``Operator`` — both indicate "this kind of node/operator wasn't handled
by the builder's dispatch and slipped through as raw."

When a new gap surfaces (a real-world query trips one of these assertions),
the right fix is to add the missing case to ``ir/builder.py``, not to relax
the assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kustology.ir import (
    CompoundNamedExpr,
    IRBuilder,
    NamedExpr,
    Operator,
    Pipeline,
    UnknownExpr,
    UnknownSource,
)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "complex_queries"


def _load_corpus() -> list[tuple[str, str]]:
    if not CORPUS_DIR.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(CORPUS_DIR.glob("*.kql")):
        text = path.read_text().strip()
        if text:
            out.append((path.stem, text))
    return out


CORPUS = _load_corpus()


@pytest.fixture(scope="module")
def builder():
    # No schema needed — this test exercises the syntactic→IR mapping, not
    # schema binding. The corpus contains references to many tables, none of
    # which are loaded here, but the builder still produces a valid IR for
    # every well-formed query.
    return IRBuilder()


def _walk_expr(expr, unknowns: list):
    if expr is None:
        return
    if isinstance(expr, UnknownExpr):
        unknowns.append(expr)
    for attr in (
        "left", "right", "operand", "expression", "selector",
        "target", "column", "low", "high",
    ):
        child = getattr(expr, attr, None)
        if child is not None:
            _walk_expr(child, unknowns)
    for attr in ("operands", "args", "values"):
        children = getattr(expr, attr, None) or []
        for c in children:
            _walk_expr(c, unknowns)
    if isinstance(expr, (NamedExpr, CompoundNamedExpr)):
        _walk_expr(expr.expression, unknowns)


def _walk_pipeline(pipeline, unknowns: list, unspecialized: list, unknown_sources: list):
    # An UnknownSource at any pipeline position is a coverage gap — every
    # sub-pipeline whose source is implicit (union-at-root, mv-apply / partition
    # subquery, join/lookup RHS) should resolve to ImplicitSource.
    if isinstance(pipeline.source, UnknownSource):
        unknown_sources.append(pipeline.source)
    if isinstance(pipeline.source, Pipeline):
        _walk_pipeline(pipeline.source, unknowns, unspecialized, unknown_sources)
    for op in pipeline.operators:
        # Strict identity catches the bare-base-class fallthrough in _visit_operator.
        if type(op) is Operator:
            unspecialized.append(op)
        if hasattr(op, "predicate"):
            _walk_expr(op.predicate, unknowns)
        if hasattr(op, "assignments"):
            for a in op.assignments:
                _walk_expr(a.expr, unknowns)
        if hasattr(op, "aggregations"):
            for a in op.aggregations:
                _walk_expr(a.expr, unknowns)
        if hasattr(op, "columns"):
            for c in op.columns:
                if hasattr(c, "expr"):
                    _walk_expr(c.expr, unknowns)
                else:
                    _walk_expr(c, unknowns)
        if hasattr(op, "right") and op.right is not None and hasattr(op.right, "operators"):
            _walk_pipeline(op.right, unknowns, unspecialized, unknown_sources)
        if hasattr(op, "pipelines") and op.pipelines:
            for sub in op.pipelines:
                _walk_pipeline(sub, unknowns, unspecialized, unknown_sources)


@pytest.mark.skipif(
    not CORPUS,
    reason="complex_queries corpus is empty — run scripts/extract_complex_corpus.py",
)
@pytest.mark.parametrize("name, query", CORPUS, ids=[name for name, _ in CORPUS])
def test_complex_kql_parsing(builder, name, query):
    ir = builder.build(query)

    unknowns: list = []
    unspecialized: list = []
    unknown_sources: list = []
    _walk_pipeline(ir.main_pipeline, unknowns, unspecialized, unknown_sources)

    assert not unknowns, (
        f"{name}: builder produced {len(unknowns)} UnknownExpr nodes: "
        f"{[u.ast_kind for u in unknowns]}"
    )
    assert not unspecialized, (
        f"{name}: builder produced {len(unspecialized)} unspecialized Operators"
    )
    assert not unknown_sources, (
        f"{name}: builder produced {len(unknown_sources)} UnknownSource nodes — "
        f"expected ImplicitSource for sub-pipelines"
    )
