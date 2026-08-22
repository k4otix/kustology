# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``fork`` branches: the sub-pipelines and the names they can be given.

``ForkOp`` declared a ``pipelines`` list and the builder handed each element
to ``_visit_pipeline`` — but the element is a ``ForkExpression``, a node kind
``_visit_pipeline``'s walker has no case for, so the walk fell straight
through: every branch came back with no operators and an ``UnknownSource``,
and the branch's name was never looked at. The consequences are two, and the
second is the one that bites hardest:

* ``T | fork (take 1) (count)`` and ``T | fork (count) (where x == 1)`` built
  the same IR and hashed alike, which is the lossy-lowering shape AGENTS.md
  warns about — a populated-looking node two different queries reach.
* Nothing *inside* a branch was reachable. ``find_all(ir, FilterOp)`` on a
  query whose only ``where`` sits in a fork branch returned an empty list, so
  every analyzer built on the documented ``walk``/``find_all`` traversal was
  silently blind to the contents of a fork.

The tests below assert both, plus the branch name, on real parses.
"""

import pytest

from kustology import parse
from kustology.ir import (
    CountOp,
    FilterOp,
    ForkBranch,
    ForkOp,
    ImplicitSource,
    Pipeline,
    QueryIR,
    TakeOp,
    UnknownSource,
    find_all,
)


def _ir(query: str, **kwargs) -> QueryIR:
    return parse(query, **kwargs).to_ir()


def _fork(ir: QueryIR) -> ForkOp:
    """The one ``ForkOp`` in ``ir``. Takes the IR rather than the query so a
    test that needs both the operator and the whole tree parses once."""
    (op,) = find_all(ir, ForkOp)
    return op


def _hash(query: str) -> str:
    return _ir(query).semantic_hash


# -- the branches exist and carry their contents --------------------------

def test_fork_builds_one_branch_per_parenthesized_pipeline():
    op = _fork(_ir("T | fork a=(where x == 1 | count) (take 1)"))
    assert [type(b).__name__ for b in op.branches] == ["ForkBranch", "ForkBranch"]
    assert [len(b.pipeline.operators) for b in op.branches] == [2, 1]


def test_fork_records_the_branch_name_and_leaves_an_unnamed_branch_none():
    """``a=`` is a ``NameEqualsClause`` on the ``ForkExpression``; it names the
    result table the branch produces, so it is data, not formatting."""
    op = _fork(_ir("T | fork a=(where x == 1 | count) (take 1)"))
    assert [b.name for b in op.branches] == ["a", None]


def test_branch_contents_are_reachable_by_find_all():
    """The RED case. Before fork branches were built, the only ``where`` in
    this query lived inside a branch and ``find_all`` could not see it."""
    ir = _ir("T | fork a=(where x == 1 | count) (take 1)")
    assert [type(o).__name__ for o in find_all(ir, FilterOp)] == ["FilterOp"]
    assert [type(o).__name__ for o in find_all(ir, CountOp)] == ["CountOp"]
    assert [type(o).__name__ for o in find_all(ir, TakeOp)] == ["TakeOp"]
    assert len(list(find_all(ir, ForkBranch))) == 2


def test_branch_operators_are_in_source_order():
    op = _fork(_ir("T | fork (where x == 1 | take 3 | count) (take 1)"))
    assert [type(o).__name__ for o in op.branches[0].pipeline.operators] == [
        "FilterOp", "TakeOp", "CountOp",
    ]


def test_branch_pipeline_has_an_implicit_source_not_an_unknown_one():
    """A fork branch runs against the enclosing row set — that is exactly what
    ``ImplicitSource`` means. ``UnknownSource`` would claim the builder could
    not work the source out."""
    ir = _ir("T | fork (where x == 1) (count)")
    op = _fork(ir)
    assert all(isinstance(b.pipeline.source, ImplicitSource) for b in op.branches), [
        type(b.pipeline.source).__name__ for b in op.branches
    ]
    assert list(find_all(ir, UnknownSource)) == []


def test_a_single_operator_branch_still_builds_a_pipeline():
    """``(count)`` is a bare ``CountOperator``, not a ``PipeExpression``."""
    op = _fork(_ir("T | fork (count) (take 1)"))
    assert all(isinstance(b.pipeline, Pipeline) for b in op.branches)
    assert [type(b.pipeline.operators[0]).__name__ for b in op.branches] == [
        "CountOp", "TakeOp",
    ]


def test_nested_fork_branches_are_built_too():
    ir = _ir("T | fork (where x == 1 | fork (count) (take 2)) (take 1)")
    outer, inner = find_all(ir, ForkOp)
    assert [type(o).__name__ for o in outer.branches[0].pipeline.operators] == [
        "FilterOp", "ForkOp",
    ]
    assert [type(b.pipeline.operators[0]).__name__ for b in inner.branches] == [
        "CountOp", "TakeOp",
    ]


# -- hashing --------------------------------------------------------------

MUST_DIFFER = [
    ("branch-bodies", "T | fork (take 1) (count)", "T | fork (count) (where x == 1)"),
    ("branch-order", "T | fork (take 1) (count)", "T | fork (count) (take 1)"),
    ("branch-count", "T | fork (take 1) (count)", "T | fork (take 1)"),
    ("branch-name-present", "T | fork a=(count) (take 1)", "T | fork (count) (take 1)"),
    ("branch-name-value", "T | fork a=(count) (take 1)", "T | fork b=(count) (take 1)"),
    ("inside-a-branch", "T | fork (where x == 1) (count)", "T | fork (where x == 2) (count)"),
]


@pytest.mark.parametrize("case_id, a, b", MUST_DIFFER, ids=[c[0] for c in MUST_DIFFER])
def test_distinguishable_forks_hash_apart(case_id, a, b):
    assert _hash(a) != _hash(b), (
        f"{case_id}: {a!r} and {b!r} are different queries but produced the "
        f"same semantic_hash"
    )


def test_the_same_fork_written_twice_still_hashes_alike():
    """Guard against the fix over-reaching into something volatile: only the
    whitespace differs here."""
    assert _hash("T | fork a=(count) (take 1)") == _hash("T |   fork   a=(count)  (take 1)")


# -- binding --------------------------------------------------------------

def test_binder_reaches_into_fork_branches():
    """``ForkBranch`` is a plain ``BaseModel`` holding a ``Pipeline``, so
    ``SchemaAttacher._fill_children`` recurses through it and hands the
    pipeline to ``_walk_pipeline`` with the enclosing scope inherited.
    Asserted on non-default values: the column's table *and* its type."""
    schema = {"T": {"x": "string", "n": "long"}}
    ir = _ir("T | fork a=(where x == 'v' | count) (top 2 by n)", schema=schema)
    assert ir.schema_attached
    (filt,) = find_all(ir, FilterOp)
    assert (filt.predicate.left.table, filt.predicate.left.result_type.value) == ("T", "string")
    key = _fork(ir).branches[1].pipeline.operators[0].by
    assert (key.expression.table, key.expression.result_type.value) == ("T", "long")


# -- serialization --------------------------------------------------------

def test_fork_branches_round_trip_through_json():
    ir = _ir("T | fork a=(where x == 1 | count) (take 1)")
    again = QueryIR.model_validate_json(ir.model_dump_json())
    assert again == ir
    (op,) = find_all(again, ForkOp)
    assert [b.name for b in op.branches] == ["a", None]
    assert [len(b.pipeline.operators) for b in op.branches] == [2, 1]


def test_the_old_pipelines_field_is_gone():
    """``ForkOp.pipelines`` is replaced, not aliased -- ``extra="forbid"``
    means a dump written against the old shape must fail loudly rather than
    validate into an IR whose branches are silently empty again."""
    import pydantic

    ir = _ir("T | fork (count) (take 1)")
    assert not hasattr(_fork(ir), "pipelines")
    dumped = ir.model_dump_json().replace('"branches"', '"pipelines"')
    with pytest.raises(pydantic.ValidationError):
        QueryIR.model_validate_json(dumped)
