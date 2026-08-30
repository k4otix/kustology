# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``compute_semantic_hash`` is exactly ``_digest(_payload(_canonicalize(node)))``.

Pins the three-way split so a future edit to any one stage cannot silently
change what ``compute_semantic_hash`` returns without a test noticing, and
pins that ``_canonicalize`` records spans before ``_clear_volatile`` wipes
them -- the one hook :mod:`kustology.ir.similarity` needs and the hash
pipeline itself never asks for.
"""

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
