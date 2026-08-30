# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Graded similarity between queries: every subtree gets the digest
``compute_semantic_hash`` would give it in the context of its root, and two
queries are compared by the overlap of those digest sets.

See ``docs/similarity.md``. Corpus-level concerns — IDF weighting,
clustering, LSH indexing, thresholds — stay with the consumer.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import BaseModel

from .spans import Span
from .transforms import _canonicalize, _digest, _payload
from .walk import _models_in, model_bearing_fields


class SubtreeHash(NamedTuple):
    digest: str        # same scheme and prefix as ``semantic_hash``
    kind: str          # the node's ``kind`` value, or its class name
    size: int          # model nodes in the subtree; ``Span`` not counted
    span: Span | None  # envelope in the caller's IR; ``None`` if nothing below has one


def subtree_hashes(node: BaseModel, *, min_size: int = 3) -> list[SubtreeHash]:
    """Return one entry per subtree of ``node`` with at least ``min_size`` nodes.

    Entries come in post-order — children before parents, root last — and the
    root's digest equals ``compute_semantic_hash(node)``. Pass the ``QueryIR``
    root, not a bare ``Pipeline``, for ``let``-name-invariant digests: a bare
    ``Pipeline`` keeps ``let`` names as written, the same rule
    ``compute_semantic_hash`` follows.

    Digests are taken from one canonical copy of ``node``, so ``let`` names
    are renamed and consecutive filters merged exactly as for the whole-query
    digest — which is why a subtree's entry here can differ from
    ``compute_semantic_hash`` called on that subtree by itself.
    """
    if min_size < 1:
        raise ValueError("min_size must be at least 1")
    spans: dict[int, Span | None] = {}
    canonical = _canonicalize(node, spans=spans)
    out: list[SubtreeHash] = []
    _collect(canonical, min_size, spans, out, set())
    return out


def _children(node: BaseModel) -> list[BaseModel]:
    return [
        child
        for field in model_bearing_fields(type(node))
        for child in _models_in(getattr(node, field))
        if not isinstance(child, Span)
    ]


def _kind_of(node: BaseModel) -> str:
    kind = getattr(node, "kind", None)
    return kind if isinstance(kind, str) else type(node).__name__


def _collect(
    node: BaseModel,
    min_size: int,
    spans: dict[int, Span | None],
    out: list[SubtreeHash],
    seen: set[int],
) -> int:
    if id(node) in seen:
        return 0
    seen.add(id(node))
    size = 1 + sum(_collect(child, min_size, spans, out, seen) for child in _children(node))
    if size >= min_size:
        out.append(SubtreeHash(_digest(_payload(node)), _kind_of(node), size, spans.get(id(node))))
    return size
