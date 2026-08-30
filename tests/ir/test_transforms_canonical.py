# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``compute_semantic_hash`` is exactly ``_digest(_payload(_canonicalize(node)))``.

Pins the three-way split so a future edit to any one stage cannot silently
change what ``compute_semantic_hash`` returns without a test noticing, and
pins that ``_canonicalize`` records spans before ``_clear_volatile`` wipes
them -- the one hook :mod:`kustology.ir.similarity` needs and the hash
pipeline itself never asks for.
"""

import copy

from kustology import parse
from kustology.ir import FilterOp, Span, compute_semantic_hash, merge_consecutive_filters, walk
from kustology.ir.transforms import _canonicalize, _digest, _payload


def _ir(q):
    return parse(q).to_ir(semantic_hash=False)


def test_split_pipeline_equals_compute_semantic_hash():
    ir = _ir("let n = 5;\nT | where a > n | where b == 1 | summarize count() by c")
    assert _digest(_payload(_canonicalize(ir))) == compute_semantic_hash(ir)


def test_canonicalize_records_spans_before_clearing_them():
    q = "let n = 5;\nT | where a > n | take 1"
    ir = _ir(q)
    spans: dict[int, Span | None] = {}
    canonical = _canonicalize(ir, spans=spans)
    assert all(isinstance(n, Span) or id(n) in spans for n in walk(canonical))
    assert spans[id(canonical.main_pipeline)].text(q) == "T | where a > n | take 1"
    assert canonical.main_pipeline.operators[0].span == Span(text_start=0, width=0)  # volatile, cleared


def test_merged_filter_span_covers_every_merged_where():
    q = "T | where a > 1 | where b > 2 | take 1"
    ir = _ir(q)
    merge_consecutive_filters(ir)
    (merged,) = [op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)]
    assert merged.span.text(q) == "where a > 1 | where b > 2"


def test_merged_filter_span_widens_when_the_first_predicate_is_already_an_and():
    q = "T | where a > 1 and b > 2 | where c > 3 | take 1"
    ir = _ir(q)
    merge_consecutive_filters(ir)
    (merged,) = [op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)]
    assert merged.span.text(q) == "where a > 1 and b > 2 | where c > 3"


def test_merged_filter_span_widens_regardless_of_which_where_carries_the_and():
    q = "T | where c > 3 | where a > 1 and b > 2 | take 1"
    ir = _ir(q)
    merge_consecutive_filters(ir)
    (merged,) = [op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)]
    assert merged.span.text(q) == "where c > 3 | where a > 1 and b > 2"


def test_the_merged_predicate_spans_what_the_merged_operator_does():
    """One node, one answer about where it came from.

    The ``And`` holds the operands of every merged ``where``, so a consumer
    highlighting ``predicate.span`` -- a linter underlining the offending
    condition -- must not be pointed at only the first one.
    """
    for q in (
        "T | where a > 1 | where b > 2 | take 1",              # fresh ``And``
        "T | where a > 1 and c > 3 | where b > 2 | take 1",    # appended in place
    ):
        ir = _ir(q)
        merge_consecutive_filters(ir)
        (merged,) = [op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)]
        assert merged.predicate.span.text(q) == merged.span.text(q)


def test_an_unmerged_filter_keeps_its_predicates_own_span():
    """No run to merge, no widening: the predicate still spans just the expression."""
    q = "T | where a > 1 | take 1"
    ir = _ir(q)
    merge_consecutive_filters(ir)
    (only,) = [op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)]
    assert only.span.text(q) == "where a > 1"
    assert only.predicate.span.text(q) == "a > 1"


def test_a_copy_computes_its_own_digest_rather_than_inheriting_one():
    """``cached_property`` memoizes into ``__dict__``, which pydantic copies.

    Copy-then-mutate is the workflow ``merge_consecutive_filters`` and
    ``normalize_expressions`` document, so a copy that answered its first read
    with the source's digest would be wrong exactly where the docs send people.
    """
    ir = parse("T | where a > 1 | take 1").to_ir()
    source = ir.semantic_hash

    copied = ir.model_copy(deep=True)
    copied.main_pipeline.operators.pop()
    assert copied.semantic_hash == compute_semantic_hash(copied)
    assert copied.semantic_hash != source
    assert ir.semantic_hash == source  # the source keeps its own memo

    shallow = ir.model_copy()
    assert shallow.semantic_hash == source  # unmutated, so it recomputes to the same value

    deep = copy.deepcopy(ir)
    deep.main_pipeline.operators.pop()
    assert deep.semantic_hash == compute_semantic_hash(deep) != source


def test_the_digest_still_serializes_after_a_copy():
    """Evicting the memo must not make the computed field disappear from a dump."""
    ir = parse("T | where a > 1 | take 1").to_ir()
    assert ir.model_dump()["semantic_hash"] == ir.semantic_hash
    assert ir.model_copy(deep=True).model_dump()["semantic_hash"] == ir.semantic_hash
