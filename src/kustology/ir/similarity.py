# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Graded similarity between queries: every subtree gets the digest
``compute_semantic_hash`` would give it in the context of its root, and two
queries are compared by the overlap of those digest sets.

See ``docs/similarity.md``. Corpus-level concerns — IDF weighting,
clustering, LSH indexing, thresholds — stay with the consumer.
"""

from __future__ import annotations

import hashlib
import random
import struct
from collections.abc import Iterable
from typing import NamedTuple, TypeAlias

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


Bag: TypeAlias = BaseModel | Iterable[SubtreeHash] | Iterable[str]

_SKETCH_MAGIC = b"KSK1"
_HEADER = struct.Struct("<4sHH")  # magic, k, reserved
_MERSENNE = (1 << 61) - 1
_MAX32 = (1 << 32) - 1


def _digest_set(bag: Bag) -> frozenset[str]:
    if isinstance(bag, BaseModel):
        return frozenset(h.digest for h in subtree_hashes(bag))
    return frozenset(h.digest if isinstance(h, SubtreeHash) else h for h in bag)


def similarity(a: Bag, b: Bag) -> float:
    """Return the Jaccard overlap of two subtree-digest bags; 0.0 when both are empty."""
    x, y = _digest_set(a), _digest_set(b)
    union = len(x | y)
    return len(x & y) / union if union else 0.0


def containment(a: Bag, b: Bag) -> float:
    """Return the share of ``a``'s subtrees found in ``b`` — how much of ``a``'s logic is inside ``b``.

    Directional; returns ``0.0`` when ``a`` is empty. The root and pipeline
    entries of a larger ``b`` never match, so filter by ``kind`` or ``size``
    for a strict subsumption test.
    """
    x, y = _digest_set(a), _digest_set(b)
    return len(x & y) / len(x) if x else 0.0


def _coefficients(k: int) -> list[tuple[int, int]]:
    rng = random.Random(0x6B7573746F)  # fixed seed: sketches must agree across processes
    return [(rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE)) for _ in range(k)]


def _feature(digest: str) -> int:
    return int.from_bytes(hashlib.blake2b(digest.encode(), digest_size=8).digest(), "little")


def similarity_sketch(a: Bag, *, k: int = 128) -> bytes:
    """Return a MinHash sketch of ``a``'s digest bag: an 8-byte header plus ``k`` 4-byte slots.

    The default ``k=128`` yields 520 bytes. Two sketches estimate
    ``similarity`` without the IR; store them alongside the scheme that
    produced them and recompute after a ``SEMANTIC_HASH_SCHEME`` bump.
    """
    if not 1 <= k <= 0xFFFF:
        raise ValueError("k must be between 1 and 65535")
    features = [_feature(d) for d in _digest_set(a)]
    if not features:
        raise ValueError("cannot sketch an empty bag")
    slots = [min(((m * f + c) % _MERSENNE) & _MAX32 for f in features) for m, c in _coefficients(k)]
    return _HEADER.pack(_SKETCH_MAGIC, k, 0) + struct.pack(f"<{k}I", *slots)


def _slots(sketch: bytes) -> tuple[int, ...]:
    if len(sketch) < _HEADER.size or sketch[:4] != _SKETCH_MAGIC:
        raise ValueError("not a kustology similarity sketch")
    _, k, _ = _HEADER.unpack_from(sketch)
    if len(sketch) != _HEADER.size + 4 * k:
        raise ValueError("sketch length does not match its k")
    return struct.unpack_from(f"<{k}I", sketch, _HEADER.size)


def sketch_similarity(s1: bytes, s2: bytes) -> float:
    """Return the estimated Jaccard similarity: the share of slots that agree between two sketches.

    Raises ``ValueError`` when the sketches' headers or ``k`` values differ.
    """
    a, b = _slots(s1), _slots(s2)
    if len(a) != len(b):
        raise ValueError("sketches have different k")
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)
