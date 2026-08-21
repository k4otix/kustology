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
from pydantic_core import PydanticUndefined

from ._normalize import normalize_in_place
from .expr import And, Expr
from .query import FilterOp, Pipeline, QueryIR
from .spans import Span
from .types import KustoType
from .walk import find_all, walk


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


def normalize_expressions(root: Any) -> Any:
    """Apply semantic-preserving expression rewrites everywhere in ``root``.

    Rewrites (from :mod:`kustology.ir._normalize`):

    * ``tolower(X) == "y"`` → ``X =~ "y"`` (case-insensitive equality), and
      symmetrically ``toupper(X) == "Y"`` → ``X =~ "Y"`` — only when the
      literal is already in the folded case, and on either side of the
      comparison. A literal in the wrong case (``tolower(X) == "Y"``, always
      false) or a non-literal operand (``tolower(X) == Col``) is left alone,
      since neither is equivalent to the ``=~`` form.
    * ``tolower(X) != "y"`` → ``X !~ "y"`` (and the ``toupper`` mirror), under
      the same case-matching condition
    * Nested ``And`` / ``Or`` operands flattened into a single list
    * ``not(not(X))`` → ``X``

    Traversal is post-order: children normalize first, so a ``not(not(X))``
    inside a ``not(...)`` collapses cleanly even when nested many layers
    deep. The function descends into sub-pipelines, expression children,
    list-valued fields, and tuple branches alike.

    Mutates ``root`` in place and returns the root to keep working with —
    normally ``root`` itself, but ``not(not(X))`` replaces a node rather than
    editing it, and at the root of the tree there is no parent field to
    install the replacement into. Rebind (``ir = normalize_expressions(ir)``)
    when ``root`` may be a bare ``Expr``; a ``Pipeline`` or ``QueryIR`` root
    is never replaced, so existing call sites that ignore the return value
    stay correct.

    Operand order is left exactly as the query wrote it. This is a faithful
    public transform, not a canonicalizer: reordering a user's ``and`` chain
    would move their spans out of source order for no benefit they asked for.
    ``compute_semantic_hash`` sorts commutative operands on its own private
    copy instead.

    Build a deep copy via ``model_copy(deep=True)`` first if you need to keep
    the original.
    """
    return _normalize_node(root)


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


# Cleared on the hash's deep copy before it is dumped. Keyed by **model field
# name**, matched against ``type(node).model_fields`` at every node — not by
# key path, and deliberately not by key name in the dumped JSON. Dropping
# dictionary keys from the payload was both too broad and too narrow: too
# broad because ``AssertSchemaOp.columns`` is a ``dict[str, str]`` of the
# user's own column names, so ``assert-schema (a:long, table:long)`` lost the
# column literally called ``table`` and hashed identically to ``(a:long)``;
# too narrow because ``LetFunction.body_span`` is a span whose field is not
# named ``span``, so source offsets kept reaching the digest.
#
# To extend: add the field name here. It applies to every model that declares
# a field of that name, which is the intent — ``result_schema`` is meant to be
# cleared on ``Pipeline`` and on anything else that grows one.
#
# Spans depend on character offsets so would defeat the purpose of a semantic
# hash; the rest is everything
# :class:`~kustology.ir.binder.SchemaAttacher` writes -- ``result_type`` /
# ``result_type_inner`` (annotations), ``table`` (column provenance) and
# ``result_schema`` (the whole bound schema per pipeline). All four are
# inferred from the caller's schema, not stated by the query, so leaving any
# of them in makes the same query text hash two ways depending on whether a
# schema was passed. That only bites through ``compute_semantic_hash`` --
# ``QueryIR.semantic_hash`` is computed at build time, before the binder runs
# -- but that call is exactly what the field's own docstring tells consumers
# to make after mutating the IR.
#
# ``join_side`` is deliberately *not* stripped, and is why it exists as a
# field at all. ``table`` carries two different things: the source-derived
# ``$left`` / ``$right`` sentinel, and the table the binder resolves -- which
# it writes over the sentinel (see ``SchemaAttacher._fill``). Hashing ``table``
# made the hash bind-dependent; dropping it without recording the side
# elsewhere collapsed ``$left.a == $left.b`` into ``$left.a == $right.b``,
# which are different queries. Splitting the two apart is the standing remedy
# for lossy lowering -- see AGENTS.md.
#
# Stripping these fields does *not* make bind state invisible to the hash, and
# no field-stripping could. The builder's ``let`` dispatch is bind-dependent by
# *shape*: ``let A = OtherTable`` yields ``rhs_expr: ColumnRef`` unbound and
# ``rhs_pipeline: Pipeline(TableRef)`` once the binder proves ``OtherTable`` is
# a table (see ``IRBuilder._visit_let_statement``). Different nodes, not
# different field values — so ``semantic_hash`` differs across bind state for a
# query whose ``let`` aliases a table. That divergence is accepted and
# documented rather than papered over: the alternative is to treat every bare
# ``NameReference`` as a table without a schema to prove it, trading an honest
# difference for a silently wrong answer. Queries with no table-aliasing
# ``let`` are unaffected.
_VOLATILE_FIELDS = frozenset({
    "span", "body_span", "result_type", "result_type_inner", "table",
    "result_schema",
})

# ``span`` and ``body_span`` are required and have no default, so unlike the
# other volatile fields there is nothing to clear them *to*. A zero span is
# the answer rather than ``None``: it keeps the copy a valid IR. Pydantic 2.13
# does not object to serializing ``None`` through a ``Span``-typed field --
# checked, it emits ``null`` with no warning -- but the payload it produces no
# longer validates back, and the copy is a live IR that ``walk`` and
# ``model_dump`` both traverse before it is thrown away. A real ``Span`` that
# is identical at every node costs nothing and leaves no invalid state behind.
#
# One instance is shared by every node on the copy. The copy is dumped and
# discarded inside ``compute_semantic_hash``, so the aliasing is never
# observable; nothing mutates a ``Span`` in place.
_ZERO_SPAN = Span(text_start=0, width=0)


def _normalize_raw_text(text: str) -> str:
    """Collapse runs of whitespace in a ``raw_text`` field to single spaces.

    The handful of operators the IR keeps as source text (``scan``,
    ``top-nested``, the ``graph-*`` family, and the ``Unknown*`` fallbacks)
    hash that text directly, so ``| top-nested 3 of a`` and
    ``|   top-nested\\n3 of a`` were two different queries as far as the
    digest was concerned.

    Comments are *not* stripped here, and must not be: the builder records
    ``ToString(IncludeTrivia.Minimal)``, which already drops every comment in
    and around the node, so there is nothing left to remove — while ``//`` is
    also the middle of every URL a detection rule matches on. A regex from
    ``//`` to end-of-line would truncate ``Url == "http://a"`` and
    ``Url == "http://b"`` to the same text and collide two different queries,
    which is the exact defect class this strip exists to close.
    """
    return " ".join(text.split())


def _clear_volatile(root: BaseModel) -> None:
    """Clear every volatile field on ``root`` and its descendants, in place.

    Intended for the hash's private deep copy — it rewrites node state.
    """
    for node in walk(root):
        fields = type(node).model_fields
        for name in _VOLATILE_FIELDS & fields.keys():
            if name in ("span", "body_span"):
                object.__setattr__(node, name, _ZERO_SPAN)
            else:
                # Back to the declared default, so a cleared field is
                # indistinguishable from one the binder never touched.
                # A field with no default (none currently) clears to None.
                default = fields[name].default
                object.__setattr__(
                    node, name, None if default is PydanticUndefined else default,
                )
        if "raw_text" in fields:
            object.__setattr__(node, "raw_text", _normalize_raw_text(node.raw_text))


# Scheme prefix declares the version of the canonicalization rules (volatile
# field set + transforms + dump format) so a future change can ship a new
# tag without silently invalidating stored hashes. Keep in lockstep with
# ``IR_SCHEMA_VERSION`` in ``kustology.ir`` — bump together.
#
# The lockstep rule is about *released* versions. One tag covers one
# unreleased window: ``v2`` accounts for every canonicalization change since
# ``v0.1.0``, however many branches landed in between. Bumping per branch
# would burn tags nobody ever saw and leave gaps in the released sequence
# that a later reader has to go digging to explain. Bump on the first change
# *after* a release, not on every change.
#
# The one thing never to do is reuse a tag for different rules: a stored hash
# whose prefix no longer implies its canonicalization is exactly the silent
# wrong answer the prefix exists to prevent. Renumbering down into an
# unreleased window is only safe while nothing has consumed the intermediate
# value.
SEMANTIC_HASH_SCHEME = "kustology-sem-v2"


def compute_semantic_hash(node: BaseModel) -> str:
    """SHA-256 of the canonical IR shape, prefixed with the scheme tag.

    Accepts any IR ``BaseModel`` subtree — a full :class:`QueryIR`, a
    standalone :class:`Pipeline`, an :class:`Expr` subtree — and returns
    a scheme-tagged hash like ``kustology-sem-v2:<64 hex chars>``.

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
    # Rebind: a bare ``Not(Not(X))`` root is *replaced* by ``X``, and there is
    # no parent field for the replacement to be installed into.
    canonical = normalize_expressions(canonical)
    _clear_volatile(canonical)
    if isinstance(canonical, QueryIR):
        payload: Any = {
            "let_bindings": [lb.model_dump(mode="json") for lb in canonical.let_bindings],
            "main_pipeline": canonical.main_pipeline.model_dump(mode="json"),
        }
    else:
        payload = canonical.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return f"{SEMANTIC_HASH_SCHEME}:{digest}"
