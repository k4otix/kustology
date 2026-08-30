# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Compute graded similarity between queries.

Every subtree gets the digest ``compute_semantic_hash`` would give it in
the context of its root, and two queries are compared by the overlap of
those digest sets.

See ``docs/similarity.md``. Corpus-level concerns — IDF weighting,
clustering, LSH indexing, thresholds — stay with the consumer.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from typing import NamedTuple, TypeAlias

from pydantic import BaseModel

from .._ir_tags import SEMANTIC_HASH_SCHEME
from .spans import Span
from .transforms import _canonicalize, _digest, _payload
from .walk import _models_in, model_bearing_fields


class SubtreeHash(NamedTuple):
    """One subtree's digest, kind, size, and source span, as returned by :func:`subtree_hashes`."""

    digest: str        # same scheme and prefix as ``semantic_hash``
    kind: str          # the node's ``kind`` value, or its class name
    size: int          # model nodes in the subtree; ``Span`` not counted
    span: Span | None  # envelope in the caller's IR; ``None`` if nothing below
                       # has one, or if the digests were taken with
                       # ``subtree_hashes(..., spans=False)``


def subtree_hashes(node: BaseModel, *, min_size: int = 3, spans: bool = True) -> list[SubtreeHash]:
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

    Returns ``[]`` when ``node`` itself has fewer than ``min_size`` nodes —
    including the root, since it is entry ``[-1]`` rather than a guaranteed
    one. Index the result with ``[-1]`` only after checking it is non-empty.

    ``spans=False`` leaves every ``SubtreeHash.span`` ``None`` and skips
    building the map, which costs a ``span_of`` fold per node — each one a walk
    of that node's whole subtree, so the saving grows with query size. Pass it
    when only the digests are wanted: it takes about a fifth off the call on a
    thousand-node query, and it is what :func:`similarity`,
    :func:`containment`, :func:`similarity_sketch` and the ``b`` side of
    :func:`differing_subtrees` use.
    """
    if min_size < 1:
        raise ValueError("min_size must be at least 1")
    span_map: dict[int, Span | None] = {}
    canonical = _canonicalize(node, spans=span_map if spans else None)
    out: list[SubtreeHash] = []
    _collect(canonical, min_size, span_map, out, set())
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
    # Correct only because ``_clear_volatile`` empties
    # ``LetBinding.inner_time_exprs`` on the canonical copy first: that field
    # aliases nodes already reachable through ``rhs_pipeline``/``rhs_function``,
    # so an uncleared index could be visited before a node's real structural
    # position and short-circuit it there with the wrong subtree size.
    if id(node) in seen:
        return 0
    seen.add(id(node))
    size = 1 + sum(_collect(child, min_size, spans, out, seen) for child in _children(node))
    if size >= min_size:
        out.append(SubtreeHash(_digest(_payload(node)), _kind_of(node), size, spans.get(id(node))))
    return size


Bag: TypeAlias = BaseModel | Iterable[SubtreeHash] | Iterable[str]

_SKETCH_MAGIC = b"KSK1"
_HEADER = struct.Struct("<4sHH")  # magic, k, scheme tag
_MERSENNE = (1 << 61) - 1
_MAX32 = (1 << 32) - 1

# Which ``SEMANTIC_HASH_SCHEME`` the slots were minned from, folded to two
# bytes. ``semantic_hash`` carries its scheme as a prefix so a mismatch is
# visible rather than silently wrong; a sketch is built from those digests and
# needs the same. Without it, comparing a sketch stored under one scheme
# against one built under the next returns a number near 0.0 — which reads as
# "these queries are unrelated" rather than "this sketch is stale".
_SCHEME_TAG = int.from_bytes(
    hashlib.blake2b(SEMANTIC_HASH_SCHEME.encode(), digest_size=2).digest(), "little",
)


def _digest_set(bag: Bag) -> frozenset[str]:
    if isinstance(bag, BaseModel):
        return frozenset(h.digest for h in subtree_hashes(bag, spans=False))
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
    """Return ``k`` ``(multiplier, addend)`` pairs for the MinHash permutations.

    Derived from a keyed hash rather than ``random.Random(seed).randrange``:
    sketches must agree across processes *and* across interpreter versions, and
    CPython's reproducibility promise covers ``random()``, not ``randrange`` —
    whose ``_randbelow`` internals are free to change. A stored sketch invalid
    after an interpreter upgrade would be undetectable, since the header has no
    field that could catch it.
    """
    out = []
    for i in range(k):
        raw = hashlib.blake2b(
            i.to_bytes(4, "little"), key=b"kustology-minhash", digest_size=16,
        ).digest()
        out.append((
            int.from_bytes(raw[:8], "little") % (_MERSENNE - 1) + 1,
            int.from_bytes(raw[8:], "little") % _MERSENNE,
        ))
    return out


def _feature(digest: str) -> int:
    return int.from_bytes(hashlib.blake2b(digest.encode(), digest_size=8).digest(), "little")


def similarity_sketch(a: Bag, *, k: int = 128) -> bytes:
    """Return a MinHash sketch of ``a``'s digest bag: an 8-byte header plus ``k`` 4-byte slots.

    The default ``k=128`` yields 520 bytes. Two sketches estimate
    ``similarity`` without the IR. The header records which
    ``SEMANTIC_HASH_SCHEME`` built it, so a sketch stored across a scheme bump
    is rejected by :func:`sketch_similarity` rather than silently compared;
    recompute it from the IR.
    """
    if not 1 <= k <= 0xFFFF:
        raise ValueError("k must be between 1 and 65535")
    features = [_feature(d) for d in _digest_set(a)]
    if not features:
        raise ValueError("cannot sketch an empty bag")
    slots = [min(((m * f + c) % _MERSENNE) & _MAX32 for f in features) for m, c in _coefficients(k)]
    return _HEADER.pack(_SKETCH_MAGIC, k, _SCHEME_TAG) + struct.pack(f"<{k}I", *slots)


def _slots(sketch: bytes) -> tuple[int, ...]:
    if len(sketch) < _HEADER.size or sketch[:4] != _SKETCH_MAGIC:
        raise ValueError("not a kustology similarity sketch")
    _, k, tag = _HEADER.unpack_from(sketch)
    if tag != _SCHEME_TAG:
        raise ValueError(
            "sketch was built under a different digest scheme than this build's "
            f"{SEMANTIC_HASH_SCHEME}; recompute it from the IR",
        )
    # ``k == 0`` passes the length check on its own -- an 8-byte header and no
    # slots -- and then divides by zero in ``sketch_similarity``. Every other
    # malformed-sketch path raises ``ValueError``, so this one does too.
    if k == 0 or len(sketch) != _HEADER.size + 4 * k:
        raise ValueError("sketch length does not match its k")
    return struct.unpack_from(f"<{k}I", sketch, _HEADER.size)


def sketch_similarity(s1: bytes, s2: bytes) -> float:
    """Return the estimated Jaccard similarity: the share of slots that agree between two sketches.

    Raises ``ValueError`` when a sketch's magic, digest scheme or ``k`` does not
    match — including when the two ``k`` values differ from each other.
    """
    a, b = _slots(s1), _slots(s2)
    if len(a) != len(b):
        raise ValueError("sketches have different k")
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def differing_subtrees(a: BaseModel, b: BaseModel, *, min_size: int = 3) -> list[SubtreeHash]:
    """Return the smallest subtrees of ``a`` that are absent from ``b``.

    A changed node changes every ancestor's digest, so "largest differing
    subtree" is always the root; this reports the other end — nodes whose
    qualifying children are all present in ``b``, so ancestors of a reported
    node are not reported. A change below ``min_size`` surfaces at its
    smallest qualifying ancestor. Swap the arguments for ``b``'s side.
    """
    if min_size < 1:
        raise ValueError("min_size must be at least 1")
    other = {h.digest for h in subtree_hashes(b, min_size=min_size, spans=False)}
    spans: dict[int, Span | None] = {}
    canonical = _canonicalize(a, spans=spans)
    out: list[SubtreeHash] = []
    _collect_differing(canonical, min_size, spans, other, out, set())
    return out


def _collect_differing(
    node: BaseModel,
    min_size: int,
    spans: dict[int, Span | None],
    other: set[str],
    out: list[SubtreeHash],
    seen: set[int],
) -> tuple[int, bool]:
    """Return ``(size, differs)``; ``differs`` is true when this subtree or one below it is absent from ``other``."""
    if id(node) in seen:
        return 0, False
    seen.add(id(node))
    size, below_differs = 1, False
    for child in _children(node):
        child_size, child_differs = _collect_differing(child, min_size, spans, other, out, seen)
        size += child_size
        below_differs = below_differs or child_differs
    if size < min_size:
        return size, False
    digest = _digest(_payload(node))
    if digest in other:
        return size, False
    if not below_differs:
        out.append(SubtreeHash(digest, _kind_of(node), size, spans.get(id(node))))
    return size, True
