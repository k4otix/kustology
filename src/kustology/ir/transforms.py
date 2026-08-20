# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Opt-in IR transforms.

The IR builder produces a *faithful* representation of the source — consecutive
``| where`` operators stay distinct, original spans are preserved, no
semantic-equivalence rewriting is applied at build time. That keeps analyzers
that care about textual structure (span tracking, redundant-where lints,
formatting hints) unobstructed.

When you instead want a *canonical* view — e.g. "give me the conjunction of
all filter predicates as a single ``And``", or "rewrite ``tolower(X) == 'y'``
to ``X =~ 'y'``" — apply a transform from this module. Each transform is
opt-in, in-place, and traverses sub-pipelines so a single call covers nested
join/lookup/union/fork branches.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from ._normalize import normalize_in_place
from .expr import And, Expr
from .query import FilterOp, Pipeline, QueryIR
from .types import KustoType
from .walk import find_all


def merge_consecutive_filters(root: Pipeline | QueryIR) -> None:
    """Collapse runs of consecutive ``FilterOp``s into a single ``FilterOp``
    whose predicate is an ``And`` of the originals.

    Operates in place on ``root`` and every ``Pipeline`` reachable from it
    (join/lookup RHS, union/fork branches, mv-apply right, materialize /
    toscalar pipelines, etc.). The first FilterOp's span is preserved on the
    merged result; the others' outer spans are dropped (inner predicate
    spans survive unchanged).

    Build a deep copy via ``model_copy(deep=True)`` first if you need to
    keep the original pre-merge IR.
    """
    for pipeline in list(find_all(root, Pipeline)):
        pipeline.operators = _merge_at_one_level(pipeline.operators)


def normalize_expressions(root: Pipeline | QueryIR) -> None:
    """Apply semantic-preserving expression rewrites everywhere in ``root``.

    Rewrites (from :mod:`kustology.ir._normalize`):

    * ``tolower(X) == "y"`` → ``X =~ "y"`` (case-insensitive equality)
    * ``tolower(X) != "y"`` → ``X !~ "y"``
    * Nested ``And`` / ``Or`` operands flattened into a single list
    * ``not(not(X))`` → ``X``

    Traversal is post-order: children normalize first, so a ``not(not(X))``
    inside a ``not(...)`` collapses cleanly even when nested many layers
    deep. The function descends into sub-pipelines, expression children,
    list-valued fields, and tuple branches alike.

    Mutates ``root`` in place. Build a deep copy via
    ``model_copy(deep=True)`` first if you need to keep the original.
    """
    _normalize_node(root)


def _normalize_node(node: Any) -> Any:
    """Recursively descend; return the (possibly replaced) node.

    Replacement only happens for ``Expr`` nodes — ``normalize_in_place`` may
    return a different object for ``not(not(X)) → X``. Parents propagate the
    replacement via the ``setattr`` below.
    """
    if not isinstance(node, BaseModel):
        return node
    for name in type(node).model_fields:
        value = getattr(node, name)
        new_value = _normalize_field(value)
        if new_value is not value:
            setattr(node, name, new_value)
    if isinstance(node, Expr):
        return normalize_in_place(node)
    return node


def _normalize_field(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_node(value)
    if isinstance(value, list):
        return [_normalize_field(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_normalize_field(v) for v in value)
    return value


def _merge_at_one_level(ops: list) -> list:
    out: list = []
    i = 0
    while i < len(ops):
        op = ops[i]
        if isinstance(op, FilterOp):
            merged = op.predicate
            j = i + 1
            while j < len(ops) and isinstance(ops[j], FilterOp):
                nxt = ops[j].predicate
                if isinstance(merged, And):
                    merged.operands.append(nxt)
                else:
                    # ``And`` always yields bool; set explicitly so the merged
                    # predicate matches what the parser would have emitted for
                    # an equivalent single ``where A and B``.
                    merged = And(
                        operands=[merged, nxt],
                        span=op.span,
                        result_type=KustoType.BOOL,
                    )
                j += 1
            if merged is not op.predicate:
                # Flatten any ``And(And(...), ...)`` introduced by the wrap.
                normalize_in_place(merged)
                op.predicate = merged
            out.append(op)
            i = j
        else:
            out.append(op)
            i += 1
    return out


# Stripped from the dump before hashing. Spans depend on character offsets so
# would defeat the purpose of a semantic hash; the type-annotation fields
# (``result_type``, ``result_type_inner``, ``nullable``) are populated by the
# binder, so including them would make the hash bind-state-dependent — two
# IRs of the same query, one pre- and one post-bind, would otherwise differ.
_VOLATILE_FIELDS = frozenset({
    "span", "result_type", "result_type_inner", "nullable",
})


# Scheme prefix declares the version of the canonicalization rules (volatile
# field set + transforms + dump format) so a future change can ship a new
# tag without silently invalidating stored hashes. Keep in lockstep with
# ``IR_SCHEMA_VERSION`` in ``kustology.ir`` — bump together.
SEMANTIC_HASH_SCHEME = "kustology-sem-v1"


def compute_semantic_hash(node: BaseModel) -> str:
    """SHA-256 of the canonical IR shape, prefixed with the scheme tag.

    Accepts any IR ``BaseModel`` subtree — a full :class:`QueryIR`, a
    standalone :class:`Pipeline`, an :class:`Expr` subtree — and returns
    a scheme-tagged hash like ``kustology-sem-v1:<64 hex chars>``.

    Two subtrees with the same semantic content collide:

    * Whitespace / formatting differences (same AST → same IR)
    * ``tolower(X) == "y"`` vs ``X =~ "y"`` (normalize_expressions)
    * ``| where A | where B`` vs ``| where A and B`` (merge_consecutive_filters)
    * Nested ``and`` grouping vs flat chain (normalize_expressions)
    * ``not(not(X))`` collapse (normalize_expressions)

    Two subtrees with different literal values, operators, identifiers,
    or operator sequences do *not* collide.

    The hash operates on a deep copy of ``node`` — does not mutate the
    input, and the result reflects the IR shape at call time. Stale if
    you mutate ``node`` after computing the hash; call again for the
    current value.
    """
    canonical = node.model_copy(deep=True)
    if isinstance(canonical, (Pipeline, QueryIR)):
        merge_consecutive_filters(canonical)
    normalize_expressions(canonical)
    if isinstance(canonical, QueryIR):
        payload: Any = {
            "let_bindings": [lb.model_dump(mode="json") for lb in canonical.let_bindings],
            "main_pipeline": canonical.main_pipeline.model_dump(mode="json"),
        }
    else:
        payload = canonical.model_dump(mode="json")
    cleaned = _strip_volatile_fields(payload)
    digest = hashlib.sha256(
        json.dumps(cleaned, sort_keys=True).encode()
    ).hexdigest()
    return f"{SEMANTIC_HASH_SCHEME}:{digest}"


def _strip_volatile_fields(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_volatile_fields(v) for k, v in obj.items()
            if k not in _VOLATILE_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_volatile_fields(v) for v in obj]
    return obj
