# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``QueryIR.semantic_hash`` is a computed field over a lazy, memoized property.

Computing the digest is the larger part of a build, and a caller who only
wants the tree pays for it on every query. The default build leaves the
digest uncomputed until something reads ``QueryIR.semantic_hash``, at which
point it is computed once and memoized. ``to_ir(semantic_hash=True)`` (and
``IRBuilder.build(..., semantic_hash=True)``) compute it during the build
instead, for a caller who wants to fail fast or pre-warm the value before
handing the IR to other threads.

The field stays present in ``model_dump()`` either way, and a stored dump
carrying the key still loads under ``extra="forbid"``: a ``mode="before"``
validator drops the stored key so the value is recomputed on load rather
than rejected as an unrecognized field.
"""

import pytest

from kustology import parse
from kustology.ir import IRBuilder, QueryIR, compute_semantic_hash, transforms

QUERY = "let n = 5;\nT | where a > n | summarize count() by b"


@pytest.fixture
def counter(monkeypatch):
    calls = []
    real = transforms.compute_semantic_hash

    def counted(ir):
        calls.append(1)
        return real(ir)

    monkeypatch.setattr(transforms, "compute_semantic_hash", counted)
    return calls


def test_the_digest_is_not_computed_until_read(counter):
    ir = parse(QUERY).to_ir()
    assert counter == []
    first = ir.semantic_hash
    assert counter == [1] and first.startswith("kustology-sem-v2:")
    assert ir.semantic_hash == first and counter == [1]  # memoized


def test_the_eager_flag_computes_during_the_build(counter):
    ir = parse(QUERY).to_ir(semantic_hash=True)
    assert counter == [1]
    assert ir.semantic_hash == compute_semantic_hash(ir) and counter == [1]


def test_the_builder_takes_the_flag_too(counter):
    IRBuilder().build(QUERY, semantic_hash=True)
    assert counter == [1]


def test_a_stored_dump_carrying_the_key_still_loads():
    ir = parse(QUERY).to_ir()
    payload = ir.model_dump(mode="json")
    assert payload["semantic_hash"] == ir.semantic_hash
    assert QueryIR.model_validate(payload).semantic_hash == ir.semantic_hash


def test_a_dump_without_the_key_loads_and_recomputes():
    ir = parse(QUERY).to_ir()
    payload = ir.model_dump(mode="json")
    del payload["semantic_hash"]
    assert QueryIR.model_validate(payload).semantic_hash == ir.semantic_hash


def test_dumping_without_the_digest_costs_nothing(counter):
    parse(QUERY).to_ir().model_dump(mode="json", exclude={"semantic_hash"})
    assert counter == []


def test_a_mutated_ir_keeps_the_memoized_value_until_recomputed():
    ir = parse(QUERY).to_ir()
    before = ir.semantic_hash
    ir.main_pipeline.operators.pop()
    assert ir.semantic_hash == before          # documented staleness
    assert compute_semantic_hash(ir) != before  # the fresh digest moves
