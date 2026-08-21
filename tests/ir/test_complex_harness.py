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
    SubqueryExpr,
    UnknownExpr,
    UnknownOp,
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
        # Two shapes of "dispatch fell through": the bare base class (caught by
        # strict identity -- isinstance would match every subclass) and the
        # UnknownOp the builder actually emits today. Filtering on identity
        # alone left this bucket structurally empty, because UnknownOp *is* an
        # Operator subclass.
        [
            op for op in find_all(ir, Operator)
            if type(op) is Operator or isinstance(op, UnknownOp)
        ],
        list(find_all(ir, UnknownSource)),
        _degraded_let_bindings(ir),
    )


def _degraded_let_bindings(ir) -> list[str]:
    """Tabular ``let`` right-hand sides that landed on ``rhs_expr``.

    Closes a hole the ``SubqueryExpr`` work opened. Removing the paren
    unwrap in ``_visit_let_statement`` used to degrade
    ``let X = ( T | where … );`` -- the dominant Sentinel idiom -- to an
    ``UnknownExpr``, which the first assertion caught. Now that a bare
    tabular subquery is modeled, the same regression degrades it to
    ``rhs_expr=SubqueryExpr`` with ``inner_tables=[]`` instead, which is a
    perfectly well-formed node the gate had no reason to flag.

    A ``SubqueryExpr`` on a ``let`` right-hand side always means the unwrap
    failed: the binding is tabular, so it belongs in ``rhs_pipeline``.
    """
    return [
        lb.name for lb in ir.let_bindings
        if isinstance(lb.rhs_expr, SubqueryExpr)
    ]


@pytest.mark.skipif(
    not CORPUS,
    reason="complex_queries corpus is empty — run scripts/extract_complex_corpus.py",
)
@pytest.mark.parametrize("name, query", CORPUS, ids=[name for name, _ in CORPUS])
def test_complex_kql_parsing(builder, name, query):
    ir = builder.build(query)

    unknowns, unspecialized, unknown_sources, degraded_lets = _scan(ir)

    assert not unknowns, (
        f"{name}: builder produced {len(unknowns)} UnknownExpr nodes: "
        f"{[u.ast_kind for u in unknowns]}"
    )
    assert not unspecialized, (
        f"{name}: builder produced {len(unspecialized)} undispatched operators "
        f"(bare Operator / UnknownOp): "
        f"{[getattr(op, 'ast_kind', type(op).__name__) for op in unspecialized]}"
    )
    assert not unknown_sources, (
        f"{name}: builder produced {len(unknown_sources)} UnknownSource nodes — "
        f"expected ImplicitSource for sub-pipelines"
    )
    assert not degraded_lets, (
        f"{name}: tabular let bindings {degraded_lets} landed on rhs_expr as a "
        f"SubqueryExpr — the parenthesized-RHS unwrap did not fire, so "
        f"rhs_pipeline and inner_tables are empty"
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
    unknowns, unspecialized, unknown_sources, _ = _scan(ir)

    assert [u.ast_kind for u in unknowns] == ["MadeUpExpression"]
    assert len(unknown_sources) == 1
    assert not unspecialized


def test_gate_sees_unknown_op():
    """``T | reduce by X`` is not dispatched by the builder and must trip the gate.

    ``UnknownOp`` subclasses ``Operator``, so the identity test
    ``type(op) is Operator`` that this gate used never matched it — and the
    builder stopped emitting bare ``Operator`` when ``UnknownOp`` landed. The
    bucket has been structurally empty ever since, which is exactly the blind
    spot the gate exists to prevent.
    """
    from kustology.ir import UnknownOp

    ir = IRBuilder().build("T | reduce by X")

    assert list(find_all(ir, UnknownOp)), "reduce should fall through to UnknownOp"
    _, unknown_ops, *_ = _scan(ir)
    assert unknown_ops, "the gate must surface UnknownOp, not only bare Operator"
