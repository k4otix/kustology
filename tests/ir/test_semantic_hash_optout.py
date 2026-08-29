# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``to_ir(semantic_hash=False)`` skips the digest without changing the IR.

Computing the digest is the larger part of a build, and a caller who only
wants the tree pays for it on every query. The opt-out leaves the field
``""`` and never calls ``compute_semantic_hash``.

The field keeps its type and its place in the model, so a dump still
round-trips under ``extra="forbid"``. What the tests below pin is that
nothing *else* about the IR moves: skipping the digest must not change a
single other field, and the digest computed later must equal the one an
eager build produced.
"""

import pytest

pytest.importorskip("pydantic")

from kustology import parse
from kustology.ir import compute_semantic_hash
from kustology.ir.builder import IRBuilder
from kustology.ir.transforms import SEMANTIC_HASH_SCHEME

QUERIES = [
    "T | where x > 1",
    "T | where ts > ago(1.5h) | summarize c = count() by bin(ts, 1h)",
    "let cutoff = ago(7d); T | where ts > cutoff | project a, b | take 10",
    "T | join kind=inner (U | where y == 'z') on $left.k == $right.k",
    "union T, U | summarize dcount(id) by tostring(kind)",
]

SCHEMA = {"T": {"x": "long", "ts": "datetime", "a": "string", "b": "string"}}


def _is_a_digest(value: str) -> bool:
    """Test for a real digest: the scheme prefix plus 64 hex characters."""
    prefix = f"{SEMANTIC_HASH_SCHEME}:"
    return value.startswith(prefix) and len(value) == len(prefix) + 64


@pytest.mark.parametrize("query", QUERIES)
def test_the_field_is_empty_when_the_digest_is_skipped(query):
    assert parse(query).to_ir(semantic_hash=False).semantic_hash == ""


@pytest.mark.parametrize("query", QUERIES)
def test_the_default_still_computes_it(query):
    assert _is_a_digest(parse(query).to_ir().semantic_hash)


@pytest.mark.parametrize("query", QUERIES)
def test_computing_it_later_gives_the_eager_value(query):
    lazy = parse(query).to_ir(semantic_hash=False)
    eager = parse(query).to_ir()

    assert compute_semantic_hash(lazy) == eager.semantic_hash


@pytest.mark.parametrize("query", QUERIES)
def test_nothing_but_the_digest_differs(query):
    """The skip must not perturb any other field, on any code path."""
    lazy = parse(query, schema=SCHEMA).to_ir(semantic_hash=False)
    eager = parse(query, schema=SCHEMA).to_ir()

    lazy_dump = lazy.model_dump()
    eager_dump = eager.model_dump()
    assert lazy_dump.pop("semantic_hash") == ""
    assert _is_a_digest(eager_dump.pop("semantic_hash"))
    assert lazy_dump == eager_dump


def test_compute_semantic_hash_is_never_called(monkeypatch):
    """Assert the saving is real rather than a value thrown away after the fact."""
    from kustology.ir import builder

    def _fail(_ir):
        raise AssertionError("compute_semantic_hash was called")

    monkeypatch.setattr(builder, "compute_semantic_hash", _fail)

    assert parse(QUERIES[1]).to_ir(semantic_hash=False).semantic_hash == ""

    with pytest.raises(AssertionError, match="was called"):
        parse(QUERIES[1]).to_ir()


@pytest.mark.parametrize("query", QUERIES)
def test_the_builder_takes_the_flag_too(query):
    """``IRBuilder.build`` is public, and reaches the same code path."""
    assert IRBuilder().build(query, semantic_hash=False).semantic_hash == ""
    assert _is_a_digest(IRBuilder().build(query).semantic_hash)


def test_a_skipped_ir_still_round_trips():
    """``extra="forbid"`` accepts the dump, because the field shape is unchanged."""
    from kustology.ir import QueryIR

    ir = parse(QUERIES[2]).to_ir(semantic_hash=False)

    assert QueryIR.model_validate(ir.model_dump()) == ir
