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
    IRBuilder,
    Operator,
    UnknownExpr,
    UnknownSource,
    find_all,
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


def _scan(ir):
    """Every coverage gap in ``ir``, found with the generic walker.

    This replaced three hand-rolled traversals -- one here, one in
    ``scripts/mine_corpus.py``, one in ``SchemaAttacher`` -- that each
    recursed a hardcoded tuple of attribute names. All three omitted
    ``pipeline``, so nothing inside ``toscalar(...)`` / ``materialize(...)``
    / a bare subquery was ever inspected, and this one also probed operator
    fields by ``hasattr`` from a fixed list, missing ``SortOp.expressions``,
    ``TopOp.by``, ``RangeOp.start``/``end``/``step``,
    ``FacetOp.with_pipeline``, ``MacroExpandOp.pipeline`` and
    ``MakeSeriesOp.on_column`` among others.

    ``find_all`` iterates ``model_fields``, so a gate built on it cannot
    develop a blind spot when the model grows a field -- which is the whole
    reason this gate exists. It also covers ``let`` bindings for free,
    without the separate walk that used to be needed.
    """
    return (
        list(find_all(ir, UnknownExpr)),
        # Strict identity catches the bare-base-class fallthrough in
        # _visit_operator; isinstance would match every subclass.
        [op for op in find_all(ir, Operator) if type(op) is Operator],
        list(find_all(ir, UnknownSource)),
    )


@pytest.mark.skipif(
    not CORPUS,
    reason="complex_queries corpus is empty — run scripts/extract_complex_corpus.py",
)
@pytest.mark.parametrize("name, query", CORPUS, ids=[name for name, _ in CORPUS])
def test_complex_kql_parsing(builder, name, query):
    ir = builder.build(query)

    unknowns, unspecialized, unknown_sources = _scan(ir)

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
    from kustology.ir import (
        LetBinding,
        Pipeline,
        QueryIR,
        Span,
        TableRef,
        UnknownExpr,
    )

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

    ir = QueryIR(
        raw_text="",
        semantic_hash="",
        let_bindings=bindings,
        main_pipeline=Pipeline(
            source=TableRef(name="Main", span=span), operators=[],
        ),
    )
    unknowns, unspecialized, unknown_sources = _scan(ir)

    assert [u.ast_kind for u in unknowns] == ["MadeUpExpression"]
    assert len(unknown_sources) == 1
    assert not unspecialized
