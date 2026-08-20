# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Real-world Sentinel detection queries — coverage signal for the builder.

Each `.kql` file under ``tests/fixtures/complex_queries/`` was extracted from
a published Azure-Sentinel analytic rule (see
``scripts/extract_complex_corpus.py``). The test parametrizes over every file
and asserts that the builder doesn't fall back to ``UnknownExpr`` or to a
bare ``Operator`` — both indicate "this kind of node/operator wasn't handled
by the builder's dispatch and slipped through as raw."

The walk covers ``ir.let_bindings`` as well as ``ir.main_pipeline``. It did
not until 0.2, which is why an unpopulated tabular ``let`` right-hand side —
the dominant Sentinel ``let X = ( T | where … );`` idiom — passed this gate
green while producing a bare ``UnknownExpr``.

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


def _walk_let_bindings(let_bindings, unknowns: list, unspecialized: list, unknown_sources: list):
    """Cover every ``let`` right-hand side, not just the main pipeline.

    A gap reachable only through a ``let`` binding is still a gap: the whole
    point of populating ``LetBinding`` is that consumers traverse into it.
    Walking only ``ir.main_pipeline`` is how an unpopulated ``rhs_pipeline``
    (and the ``UnknownExpr`` that stood in for it) shipped green.
    """
    for lb in let_bindings:
        if lb.rhs_expr is not None:
            _walk_expr(lb.rhs_expr, unknowns)
        if lb.rhs_pipeline is not None:
            _walk_pipeline(lb.rhs_pipeline, unknowns, unspecialized, unknown_sources)


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
    _walk_let_bindings(ir.let_bindings, unknowns, unspecialized, unknown_sources)

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


def test_gate_walks_let_bindings():
    """The gate itself must reach into `let` right-hand sides.

    This is the structural fix: walking only ``main_pipeline`` is what let an
    unpopulated tabular ``let`` — and the ``UnknownExpr`` standing in for it —
    ship green through six task reviews. Built from a synthetic IR rather than
    a query so the coverage survives the builder growing better.
    """
    from kustology.ir import LetBinding, Pipeline, Span, TableRef, UnknownExpr

    span = Span(text_start=0, width=1)
    bindings = [
        LetBinding(
            name="scalar_gap",
            span=span,
            rhs_expr=UnknownExpr(
                span=span, raw_text="?", ast_kind="MadeUpExpression", reason="test",
            ),
        ),
        LetBinding(
            name="tabular_gap",
            span=span,
            rhs_pipeline=Pipeline(
                source=UnknownSource(raw_text="?", span=span), operators=[],
            ),
        ),
        LetBinding(
            name="clean",
            span=span,
            rhs_pipeline=Pipeline(source=TableRef(name="T", span=span), operators=[]),
        ),
    ]

    unknowns: list = []
    unspecialized: list = []
    unknown_sources: list = []
    _walk_let_bindings(bindings, unknowns, unspecialized, unknown_sources)

    assert [u.ast_kind for u in unknowns] == ["MadeUpExpression"]
    assert len(unknown_sources) == 1
    assert not unspecialized
