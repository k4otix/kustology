# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Sentinel detection queries — coverage signal for the builder.

Most `.kql` files under ``tests/fixtures/complex_queries/`` come from a
published Azure-Sentinel analytic rule, extracted by
``scripts/extract_complex_corpus.py``. The test parametrizes over every file
and asserts that the builder does not fall back to ``UnknownExpr`` or to a
bare ``Operator``, each of which means a node or operator missed the
builder's dispatch and came through raw.

The walk covers ``ir.let_bindings`` as well as ``ir.main_pipeline``. Walking
the main pipeline alone passes an unpopulated tabular ``let`` right-hand
side, the dominant Sentinel ``let X = ( T | where … );`` idiom, green while
it holds a bare ``UnknownExpr``.

When a real-world query trips one of these assertions, add the missing case
to ``ir/builder.py`` instead of relaxing the assertion.

## Why some fixtures are synthetic

A gate parametrized over found queries covers only what the sample happens
to contain, and the run is green either way, so the shapes it misses are
invisible. ``project-reorder x asc`` can regress to an ``UnknownExpr`` and
sail through where every extracted fixture writes ``project-reorder`` bare.
The extracted sample also reaches no ``fork``, ``lookup``, ``find``,
``search``, ``render``, ``nulls`` ordering, ``externaldata`` in source
position, no wildcard or ``database()``/``cluster()``-qualified table name,
and no ``datatable`` carrying a schema and rows.

The corpus is therefore two things: the Sentinel-derived files for realistic
shape and scale, and one small hand-written file per construct the real
sample does not reach, named for the construct (``Fork_NamedBranches``,
``Sort_BareColumn``, ``Render_WithProperties``, …) and written in Sentinel
idiom to make one modifier reachable by this gate.

When the IR grows a field and no fixture makes that field take a non-default
value, this gate cannot see the field regress and a fixture belongs here.
``scripts/mine_corpus.py`` reports the same scan across the corpus.
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
    # No schema needed: this gate exercises the syntactic-to-IR mapping, not
    # schema binding. The builder produces a valid IR for every well-formed
    # query whether or not its tables are loaded.
    return IRBuilder()


def _scan(ir):
    """Return every coverage gap in ``ir``, found with the generic walker.

    ``find_all`` iterates ``model_fields``, so this gate gains no blind spot
    when a model grows a field. A traversal over a hardcoded tuple of
    attribute names goes blind exactly there: omit ``pipeline`` and nothing
    inside ``toscalar(...)``, ``materialize(...)``, or a bare subquery is
    inspected; probe operator fields by ``hasattr`` from a fixed list and
    every field the list misses is exempt. The same walk covers ``let``
    bindings too.
    """
    return (
        list(find_all(ir, UnknownExpr)),
        # Two shapes of "dispatch fell through": a bare ``Operator``, caught
        # by strict identity because isinstance matches every subclass, and
        # the ``UnknownOp`` the builder emits, which identity never matches.
        [
            op for op in find_all(ir, Operator)
            if type(op) is Operator or isinstance(op, UnknownOp)
        ],
        list(find_all(ir, UnknownSource)),
        _degraded_let_bindings(ir),
    )


def _degraded_let_bindings(ir) -> list[str]:
    """Return tabular ``let`` right-hand sides that landed on ``rhs_expr``.

    A ``SubqueryExpr`` there means the parenthesized-RHS unwrap in
    ``_visit_let_statement`` did not fire: the binding is tabular, so it
    belongs in ``rhs_pipeline``. The failure is quiet, since a bare tabular
    subquery is a modeled shape: ``let X = ( T | where … );`` degrades to
    ``rhs_expr=SubqueryExpr`` with ``inner_tables=[]``, a well-formed node
    the ``UnknownExpr`` assertion has no reason to flag.
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
    """The gate itself has to reach into `let` right-hand sides.

    Walking only ``main_pipeline`` passes an unpopulated tabular ``let``, and
    the ``UnknownExpr`` standing in for it, silently. Built from a synthetic
    IR so the coverage survives the builder handling more shapes.
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
    """``T | reduce by X`` is undispatched and must trip the gate.

    ``UnknownOp`` subclasses ``Operator``, so an identity-only filter
    (``type(op) is Operator``) never matches it, and the builder emits
    ``UnknownOp`` for an undispatched kind. Identity alone leaves the bucket
    structurally empty.
    """
    from kustology.ir import UnknownOp

    ir = IRBuilder().build("T | reduce by X")

    assert list(find_all(ir, UnknownOp)), "reduce should fall through to UnknownOp"
    _, unknown_ops, *_ = _scan(ir)
    assert unknown_ops, "the gate must surface UnknownOp, not only bare Operator"
