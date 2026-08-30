# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tests for ``subtree_hashes``.

Pins that the root entry equals ``compute_semantic_hash``, that the digest
set is invariant to ``let`` naming and to filter splitting the same way the
whole-query hash is, that ``min_size`` floors the returned bag, and that
each entry's span locates its subtree in the original source.
"""

import pathlib

import pytest

from kustology import parse
from kustology.ir import (
    SubtreeHash,
    compute_semantic_hash,
    containment,
    differing_subtrees,
    similarity,
    similarity_sketch,
    sketch_similarity,
    subtree_hashes,
)

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "complex_queries"


def _ir(q):
    return parse(q).to_ir(semantic_hash=False)


def _bag(q, **kw):
    return {h.digest for h in subtree_hashes(_ir(q), **kw)}


def test_root_entry_equals_semantic_hash():
    ir = _ir("let n = 5;\nT | where a > n | summarize count() by b")
    hashes = subtree_hashes(ir)
    assert hashes[-1].digest == compute_semantic_hash(ir)
    assert hashes[-1].size == max(h.size for h in hashes)
    assert all(isinstance(h, SubtreeHash) and h.digest.startswith("kustology-sem-v2:") for h in hashes)


def test_bag_is_let_name_invariant():
    assert _bag("let n = 5;\nT | where a > n | take 1") == _bag("let m = 5;\nT | where a > m | take 1")


def test_bag_ignores_operand_order_and_filter_splitting():
    assert _bag("T | where a == 1 and b == 2 | take 1") == _bag("T | where b == 2 | where a == 1 | take 1")


def test_min_size_floors_the_bag():
    ir = _ir("T | where a > 1 | take 1")
    assert all(h.size >= 3 for h in subtree_hashes(ir))
    assert len(subtree_hashes(ir, min_size=1)) > len(subtree_hashes(ir))


def test_shared_predicate_is_one_digest_in_both_queries():
    a, b = _ir("T | where a == 1 and b == 2 | take 1"), _ir("S | where b == 2 and a == 1 | summarize count()")
    shared = {h.digest for h in subtree_hashes(a)} & {h.digest for h in subtree_hashes(b)}
    assert "and" in {h.kind for h in subtree_hashes(a) if h.digest in shared}


def test_span_locates_the_subtree_in_the_source():
    q = "T | where a == 1 | summarize count() by b"
    summarize = next(h for h in subtree_hashes(_ir(q)) if h.kind == "summarize")
    assert summarize.span.text(q) == "summarize count() by b"


def test_pipeline_gets_an_envelope_span():
    q = "let n = 5;\nT | where a > n | take 1"
    pipeline = next(h for h in subtree_hashes(_ir(q)) if h.kind == "pipeline")
    assert pipeline.span.text(q) == "T | where a > n | take 1"


def test_min_size_must_be_positive():
    import pytest
    with pytest.raises(ValueError):
        subtree_hashes(_ir("T | take 1"), min_size=0)


def test_similarity_identity_and_disjoint():
    a, b = _ir("T | where a == 1 | take 1"), _ir("S | summarize count() by z")
    assert similarity(a, a) == 1.0
    assert similarity(a, b) == 0.0


def test_similarity_is_graded():
    a = _ir("T | where a == 1 | summarize count() by b")
    b = _ir("T | where a == 1 | summarize count() by c")
    assert 0.0 < similarity(a, b) < 1.0


def test_containment_is_directional():
    small = _ir("T | where a == 1 | take 1")
    big = _ir("T | where a == 1 | take 1 | project a, b")
    assert containment(small, small) == 1.0
    assert containment(small, big) > containment(big, small) > 0.0


def test_similarity_accepts_hash_lists_and_digest_sets():
    a, b = _ir("T | where a == 1 | take 1"), _ir("T | where a == 1 | take 1")
    assert similarity(subtree_hashes(a), {h.digest for h in subtree_hashes(b)}) == 1.0


def test_sketch_shape_and_determinism():
    ir = _ir("T | where a == 1 | take 1")
    assert similarity_sketch(ir) == similarity_sketch(_ir("T | where a == 1 | take 1"))
    assert len(similarity_sketch(ir)) == 8 + 4 * 128
    assert len(similarity_sketch(ir, k=64)) == 8 + 4 * 64


def test_sketch_tracks_exact_jaccard_on_the_corpus():
    bags = [subtree_hashes(parse(p.read_text()).to_ir(semantic_hash=False)) for p in sorted(FIXTURES.glob("*.kql"))]
    sketches = [similarity_sketch(b) for b in bags]
    errs = [
        abs(similarity(bags[i], bags[j]) - sketch_similarity(sketches[i], sketches[j]))
        for i in range(len(bags))
        for j in range(i + 1, len(bags))
    ]
    assert sum(errs) / len(errs) < 0.03


def test_sketch_rejects_mismatch_and_empty():
    ir = _ir("T | where a == 1 | take 1")
    with pytest.raises(ValueError):
        sketch_similarity(similarity_sketch(ir, k=64), similarity_sketch(ir, k=128))
    with pytest.raises(ValueError):
        similarity_sketch([])
    with pytest.raises(ValueError):
        sketch_similarity(b"nope", similarity_sketch(ir))


def test_differing_subtrees_localizes_one_changed_operator():
    qa = "T | where a == 1 | summarize count() by b | take 10"
    qb = "T | where a == 1 | summarize count() by c | take 10"
    diff = differing_subtrees(_ir(qa), _ir(qb))
    assert [(h.kind, h.span.text(qa)) for h in diff] == [("summarize", "summarize count() by b")]


def test_differing_subtrees_of_identical_queries_is_empty():
    assert differing_subtrees(_ir("T | take 1"), _ir("T | take 1")) == []


def test_leaf_change_reports_the_smallest_qualifying_ancestor():
    diff = differing_subtrees(_ir("T | where a == 1 | take 1"), _ir("T | where a == 2 | take 1"))
    assert [h.kind for h in diff] == ["bin_op"]
