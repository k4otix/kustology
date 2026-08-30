# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tests for ``subtree_hashes`` and the similarity surface built on it."""

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
from kustology.ir.similarity import _HEADER, _SCHEME_TAG, _SKETCH_MAGIC

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


def test_diagnostics_do_not_split_the_bound_and_unbound_bag(sample_schema):
    """Diagnostics stay out of the subtree bag, as they stay out of the digest.

    ``T`` is absent from ``sample_schema``, so only the bound parse carries a
    "table not found" diagnostic. Both bags match anyway, at ``min_size=1``.
    """
    q = "T | where a == 1 | take 1"
    bound = parse(q, schema=sample_schema).to_ir(semantic_hash=False)
    unbound = parse(q).to_ir(semantic_hash=False)
    assert bound.diagnostics and not unbound.diagnostics
    bound_bag = subtree_hashes(bound, min_size=1)
    unbound_bag = subtree_hashes(unbound, min_size=1)
    assert {h.digest for h in bound_bag} == {h.digest for h in unbound_bag}
    assert bound_bag[-1].size == unbound_bag[-1].size
    assert differing_subtrees(bound, unbound, min_size=1) == []


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
    """Measure the estimator on the overlapping pairs a caller reads it at.

    Pairs with an exact similarity of 0.0 are 93.5% of this corpus, and there
    both the exact and the sketch value are trivially 0.0. Averaging them in
    dilutes the bound by more than an order of magnitude, enough for a badly
    broken estimator to clear it, so only overlapping pairs are sampled.
    ``scripts/eval_similarity._sketch_fidelity`` samples candidate neighbours
    for the same reason.

    The asserted bounds sit at roughly 2x the measured values (mean 0.013,
    p95 0.040 at ``k=128``). That absorbs corpus churn and still trips on a
    regression in the permutation family.
    """
    bags = [
        subtree_hashes(parse(p.read_text()).to_ir(semantic_hash=False))
        for p in sorted(FIXTURES.glob("*.kql"))
    ]
    sketches = [similarity_sketch(b) for b in bags]
    errs = sorted(
        abs(exact - sketch_similarity(sketches[i], sketches[j]))
        for i in range(len(bags))
        for j in range(i + 1, len(bags))
        if (exact := similarity(bags[i], bags[j])) > 0.0
    )
    # A corpus with no overlapping pairs would leave the bounds below
    # averaging over nothing.
    assert len(errs) >= 50, f"only {len(errs)} overlapping pairs to measure"
    assert sum(errs) / len(errs) < 0.03
    assert errs[int(0.95 * (len(errs) - 1))] < 0.08


def test_sketch_rejects_mismatch_and_empty():
    ir = _ir("T | where a == 1 | take 1")
    with pytest.raises(ValueError):
        sketch_similarity(similarity_sketch(ir, k=64), similarity_sketch(ir, k=128))
    with pytest.raises(ValueError):
        similarity_sketch([])
    with pytest.raises(ValueError):
        sketch_similarity(b"nope", similarity_sketch(ir))


def test_a_header_declaring_no_slots_raises_rather_than_dividing_by_zero():
    """``k=0`` satisfies the length check on its own -- 8 bytes of header, no slots."""
    empty = _HEADER.pack(_SKETCH_MAGIC, 0, _SCHEME_TAG)
    with pytest.raises(ValueError):
        sketch_similarity(empty, empty)


def test_a_sketch_from_another_digest_scheme_is_rejected_not_compared():
    """A stale sketch must not read as "these queries are unrelated".

    Slots are minned from ``semantic_hash`` digests, so across a
    ``SEMANTIC_HASH_SCHEME`` bump they compare to a number near 0.0 that is
    indistinguishable from a real answer. The header carries the scheme so the
    mismatch raises.
    """
    good = similarity_sketch(_ir("T | where a == 1 | take 1"))
    foreign = _HEADER.pack(_SKETCH_MAGIC, 128, (_SCHEME_TAG ^ 1) & 0xFFFF) + good[_HEADER.size:]
    with pytest.raises(ValueError, match="digest scheme"):
        sketch_similarity(foreign, good)


def test_sketches_do_not_depend_on_the_interpreter_random_module(monkeypatch):
    """The permutation family comes from a keyed hash.

    ``random.Random.randrange`` has no cross-version reproducibility promise,
    so a sketch built on it would be invalidated undetectably by an
    interpreter upgrade. Breaking ``random`` outright proves nothing reads it.
    """
    import random

    baseline = similarity_sketch(_ir("T | where a == 1 | take 1"))
    monkeypatch.delattr(random.Random, "randrange")
    assert similarity_sketch(_ir("T | where a == 1 | take 1")) == baseline


def test_digest_only_callers_skip_the_span_map():
    ir = _ir("T | where a == 1 | summarize count() by b | take 10")
    with_spans = subtree_hashes(ir)
    without = subtree_hashes(ir, spans=False)
    assert [h.digest for h in without] == [h.digest for h in with_spans]
    assert all(h.span is None for h in without)
    assert any(h.span is not None for h in with_spans)


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
