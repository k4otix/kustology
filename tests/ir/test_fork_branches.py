# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``fork`` branches: the sub-pipelines and the names they can be given.

Each branch is a ``ForkExpression``, a node kind a plain pipeline walk has no
case for, so a builder that hands it to ``_visit_pipeline`` unmodified drops
the branch: no operators, an ``UnknownSource``, and no name. That costs two
things.

* ``T | fork (take 1) (count)`` and ``T | fork (count) (where x == 1)`` build
  the same IR and hash alike, the lossy-lowering shape AGENTS.md warns about,
  where a populated-looking node is reached by two different queries.
* Nothing inside a branch is reachable. ``find_all(ir, FilterOp)`` on a query
  whose only ``where`` sits in a fork branch returns an empty list, so every
  analyzer built on the documented ``walk``/``find_all`` traversal is blind to
  the contents of a fork.

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
    """Return the one ``ForkOp`` in ``ir``; taking the IR lets a test that needs the whole tree parse once."""
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
    """``a=`` is a ``NameEqualsClause`` on the ``ForkExpression`` naming the
    result table the branch produces, so it is data."""
    op = _fork(_ir("T | fork a=(where x == 1 | count) (take 1)"))
    assert [b.name for b in op.branches] == ["a", None]


def test_branch_contents_are_reachable_by_find_all():
    """The only ``where`` in this query lives inside a branch, so a walk that
    skips branch pipelines cannot see it."""
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
    """A fork branch runs against the enclosing row set, which is what
    ``ImplicitSource`` means; ``UnknownSource`` would mean the builder could
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
    # Branch bodies, order, and name values live in test_hash_battery.py
    # (fork-branch-bodies, fork-branch-order, fork-branch-name); these are the
    # pairs the battery does not carry.
    ("branch-count", "T | fork (take 1) (count)", "T | fork (take 1)"),
    ("branch-name-present", "T | fork a=(count) (take 1)", "T | fork (count) (take 1)"),
    ("inside-a-branch", "T | fork (where x == 1) (count)", "T | fork (where x == 2) (count)"),
]


@pytest.mark.parametrize("case_id, a, b", MUST_DIFFER, ids=[c[0] for c in MUST_DIFFER])
def test_distinguishable_forks_hash_apart(case_id, a, b):
    assert _hash(a) != _hash(b), (
        f"{case_id}: {a!r} and {b!r} are different queries but produced the "
        f"same semantic_hash"
    )


def test_the_same_fork_written_twice_still_hashes_alike():
    """Only the whitespace differs, so branch modelling must not reach anything volatile."""
    assert _hash("T | fork a=(count) (take 1)") == _hash("T |   fork   a=(count)  (take 1)")


# -- binding --------------------------------------------------------------

def test_binder_reaches_into_fork_branches():
    """Bind through fork branches with the enclosing scope inherited.

    ``ForkBranch`` is a plain ``BaseModel`` holding a ``Pipeline``, so
    ``SchemaAttacher._fill_children`` recurses through it and hands the
    pipeline to ``_walk_pipeline``. The assertions use non-default values: the
    column's table and its type.
    """
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
    """``ForkOp`` carries ``branches`` and no ``pipelines`` alias, so
    ``extra="forbid"`` fails a dump written with that key instead of
    validating it into an IR whose branches are silently empty."""
    import pydantic

    ir = _ir("T | fork (count) (take 1)")
    assert not hasattr(_fork(ir), "pipelines")
    dumped = ir.model_dump_json().replace('"branches"', '"pipelines"')
    with pytest.raises(pydantic.ValidationError):
        QueryIR.model_validate_json(dumped)
