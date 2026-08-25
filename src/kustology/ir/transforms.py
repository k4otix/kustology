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
import re
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ._normalize import normalize_in_place
from .expr import And, Expr, LetValueRef, Or, SetMembership
from .query import FilterOp, LetBinding, LetRef, Pipeline, QueryIR
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


def normalize_expressions(root: Expr | Pipeline | QueryIR) -> Expr | Pipeline | QueryIR:
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
# ``hints`` is the one member of this set the binder does *not* write: it is
# source-derived, read straight off the query's ``hint.*`` named parameters.
# It is stripped because a hint is an execution instruction rather than a
# statement about the result -- ``join hint.strategy=shuffle`` and ``join``
# return the same rows -- so two rules differing only in tuning must
# deduplicate to one. That makes it the exception to the "source-derived
# information must keep hashing" rule two paragraphs down, and the exception
# is decided by what the field *means*, not by where it comes from. A future
# field only belongs here if the query would return identical rows without
# it.
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
    "result_schema", "hints",
})

# Not the only "keep a field's written value out of the digest" mechanism --
# see :func:`_strip_unwritten_fields` below for the sibling case: a field
# whose *written* value must reach the digest, but whose *unwritten* default
# must not move a query that never used it. Clearing to the default (this
# mechanism) is wrong there, because the default is the thing being hashed;
# omitting the key from the dump (that one) is wrong here, because these
# fields are never absent, only ever reset.

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
    """Fold line breaks (and their surrounding indent) in ``raw_text`` to a
    single space. Nothing else is touched.

    The handful of operators the IR keeps as source text (``scan``,
    ``top-nested``, the ``graph-*`` family, and the ``Unknown*`` fallbacks)
    hash that text directly, so ``| top-nested 3 of a`` and
    ``|   top-nested\\n3 of a`` were two different queries as far as the
    digest was concerned.

    The rule is deliberately narrow, because ``raw_text`` is source text and
    two of the things that look like formatting in it are data:

    * **Interior spacing is not collapsed.** A run of spaces can be *inside a
      string literal*, where it is part of the value: a rule matching
      ``"error  occurred"`` (two spaces) and one matching ``"error occurred"``
      are different predicates, and collapsing every whitespace run merged
      them. Outside a literal there is nothing left to collapse anyway —
      ``IncludeTrivia.Minimal`` has already normalized it, and records
      ``top-nested 3  of  a`` as ``top-nested 3 of a``. Newlines are the safe
      case precisely because a KQL string literal cannot contain a raw one,
      so this rule can never reach inside a literal.
    * **Comments are not stripped.** ``Minimal`` already drops every comment
      in and around the node, so there is nothing to remove — while ``//`` is
      also the middle of every URL a detection rule matches on. A regex from
      ``//`` to end-of-line would truncate ``Url == "http://a"`` and
      ``Url == "http://b"`` to the same text.

    Both boundaries are pinned by tests; widening this function back to
    ``" ".join(text.split())`` fails the first, and adding a comment strip
    fails the second.
    """
    return re.sub(r"\s*\n\s*", " ", text).strip()


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
                #
                # A *mutable* declared default -- ``hints`` is ``{}`` -- is
                # installed by reference, so every node carrying that field
                # ends up sharing one object. Safe for exactly the reason
                # ``_ZERO_SPAN`` is: this runs only on the hash's private
                # deep copy, which is dumped and discarded, and nothing
                # between here and the dump mutates a cleared field in
                # place. A future step that wanted to *edit* one of these
                # would have to copy it first.
                default = fields[name].default
                object.__setattr__(
                    node, name, None if default is PydanticUndefined else default,
                )
        if "raw_text" in fields:
            object.__setattr__(node, "raw_text", _normalize_raw_text(node.raw_text))


# Every model carrying a ``let``-bound name in a ``name`` field: the
# declaration, the source-position reference and the expression-position
# reference. Append to this tuple when a new one lands -- nothing else needs
# to change, since ``_canonicalize_let_names`` walks by isinstance against the
# whole tuple.
#
# ``LetValueRef`` is the reason the tuple was written to be extended.
# ``ColumnRef`` deliberately is not a member and must not become one: a real
# column named ``n`` is a different query from a ``let``-bound ``n``, so
# renaming one would collapse the two.
_LET_NAME_MODELS: tuple[type[BaseModel], ...] = (LetBinding, LetRef, LetValueRef)


def _canonicalize_let_names(ir: QueryIR) -> None:
    """Rename every ``let`` binding to ``$let<i>`` by declaration index.

    A ``let`` name is a local label with no meaning outside the query, so
    ``let X = …; X | take 1`` and ``let Y = …; Y | take 1`` are the same
    query and must hash alike. The rename is *positional* rather than a
    blanket erasure precisely so the rest stays distinguishable: with two
    bindings in play, reading from the first and reading from the second are
    different queries, and ``$let0`` / ``$let1`` keeps them apart.

    ``$`` cannot appear in a KQL identifier, so a canonical name can never
    collide with one the query chose. Runs on the hash's deep copy only.

    Declarations are renamed by **position**, not by looking their old name up
    in a map, because names are not unique: KQL diagnoses a redeclaration
    (``KS201``) but the error-tolerant builder still emits both bindings, and
    ``compute_semantic_hash`` also accepts hand-built IR with no parser in the
    loop at all. A name-keyed map gives two same-named bindings one canonical
    name, and ``$letN`` stops meaning "the Nth declaration".

    References resolve against the bindings **visible where they are
    written** -- a binding's own right-hand side sees only the bindings
    declared before it, the main pipeline sees them all. That is what keeps a
    shadowed reference pointing at the binding it actually reads, instead of
    at the one currently being defined.
    """
    if not ir.let_bindings:
        return
    # Name -> canonical name, rebuilt as each declaration comes into scope.
    visible: dict[str, str] = {}
    for i, binding in enumerate(ir.let_bindings):
        canonical = f"$let{i}"
        # The right-hand side is resolved *before* this binding enters scope.
        for node in walk(binding):
            if node is not binding and isinstance(node, _LET_NAME_MODELS):
                object.__setattr__(node, "name", visible.get(node.name, node.name))
        visible[binding.name] = canonical
        object.__setattr__(binding, "name", canonical)
    # Every tabular statement, not just the first: a ``let`` declared once is
    # in scope for all of them, so a reference written after the second
    # semicolon has to be renamed too or the rename stops being one.
    for pipeline in (ir.main_pipeline, *ir.additional_pipelines):
        for node in walk(pipeline):
            if isinstance(node, _LET_NAME_MODELS):
                object.__setattr__(node, "name", visible.get(node.name, node.name))


def _sort_commutative(root: BaseModel) -> None:
    """Sort the operands of every commutative node into a canonical order.

    ``and`` / ``or`` operands and the value list of a set-membership test are
    the only places in the IR where source order carries no meaning. Sorting
    them makes ``a and b`` and ``b and a`` one digest. Nothing else is
    touched: ``a < b`` and ``b < a`` are opposite predicates, so a sort over
    ``BinOp`` operands would merge two queries that disagree.

    Two ordering dependencies, both load-bearing:

    * The key is the child's dumped JSON, so this must run *after*
      ``_clear_volatile`` -- otherwise the spans inside each operand order the
      list by where it happened to be written, which is the opposite of the
      point.
    * Children must be sorted before their parents, since a parent's key is
      computed from a child's dump. ``walk`` is pre-order, so iterating it
      reversed visits every descendant before its ancestor. The bottom-up
      order bites when a sibling's key falls *between* the two spellings of
      an operand: ``(b or a) and (a or z)`` and ``(a or z) and (a or b)`` are
      the same predicate, but top-down the first ``And`` keys on
      ``(b or a)`` and the second on ``(a or b)``, which sort to opposite
      sides of ``(a or z)`` -- so the two ``And``s end up in opposite orders
      and stay different once the ``Or``s are fixed.

    One caveat on that second point, since ``walk`` yields a shared object
    only at the *first* path that reaches it. A node an index field aliases
    (``LetBinding.inner_time_exprs`` holds the same objects as
    ``rhs_pipeline``) is positioned by whichever field is declared first, and
    if the index came first the node would be yielded above its real parent
    and sorted after it. Nothing hits that today: the only aliasing fields
    hold ``FuncCall`` and ``TableRef``, which this function never sorts, and
    they are declared after ``rhs_pipeline`` anyway. A new index field over
    ``And``/``Or``/``SetMembership`` would need a genuinely post-order
    traversal here rather than a reversed pre-order.

    Runs on the hash's deep copy only -- the public ``normalize_expressions``
    leaves the query's own order alone.
    """
    for node in reversed(list(walk(root))):
        if isinstance(node, (And, Or)):
            node.operands = sorted(node.operands, key=_operand_sort_key)
        elif isinstance(node, SetMembership):
            node.values = sorted(node.values, key=_operand_sort_key)


def _operand_sort_key(child: BaseModel) -> str:
    """Total order over IR subtrees: their own canonical JSON dump.

    ``sort_keys=True`` makes it independent of field declaration order, and
    dumping the whole subtree rather than a rendered form means two operands
    only tie when every field matches -- in which case their order genuinely
    does not matter.
    """
    return json.dumps(child.model_dump(mode="json"), sort_keys=True)


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


def _strip_unwritten_fields(value: Any, kind: str, defaults: dict[str, Any]) -> None:
    """Delete an operator dict's keys when every one is still at its
    unwritten default, in place, on the *dumped* JSON structure.

    The sibling of :data:`_VOLATILE_FIELDS`/:func:`_clear_volatile` above,
    for the opposite kind of field: one that is plain and non-volatile
    (a written value must reach the digest -- clearing it to a canonical
    default the way ``_clear_volatile`` clears bind state would hide real
    differences) but that was *added* to an operator IR node with no field
    for it before, the way :class:`~kustology.ir.query.EvaluateOp`'s
    ``declared_schema``/``declared_schema_star`` were. A Pydantic field
    cannot be conditionally *absent* from one ``model_dump`` call: the key
    is always present at its default, so merely adding such a field moves
    the digest of every query using that operator, written or not -- only
    the written case is a real collision closing. Operating after the dump,
    on the plain dict/list tree rather than the model, is what makes the
    omission conditional: an operator dict whose fields are all still at
    ``defaults`` dumps exactly as it did before those fields existed, and
    only a dict with at least one written field keeps the new keys at all.

    ``defaults`` maps field name to its *exact* unwritten value, compared by
    identity (``is``) rather than ``==`` -- evaluate's is ``None``/``False``,
    and ``0 == False`` would wrongly match a future int-typed field whose
    real default is ``0`` under equality. Every field in ``defaults`` must
    match for the dict's keys to be dropped: a partially-written modifier
    (e.g. only one of several flags set) keeps every key, so the digest
    still reflects what was actually written. Reuse this for the next such
    field -- README's "Four operators still discard a modifier" list
    (``mv-apply``, ``parse-kv``, ``getschema``, ``consume``) is the same
    problem shape -- rather than writing a new one-off dict walk.
    """
    if isinstance(value, dict):
        if value.get("kind") == kind and all(
            value.get(field) is default for field, default in defaults.items()
        ):
            for field in defaults:
                del value[field]
        for child in value.values():
            _strip_unwritten_fields(child, kind, defaults)
    elif isinstance(value, list):
        for item in value:
            _strip_unwritten_fields(item, kind, defaults)


def compute_semantic_hash(node: BaseModel) -> str:
    """SHA-256 of the canonical IR shape, prefixed with the scheme tag.

    Accepts any IR ``BaseModel`` subtree — a full :class:`QueryIR`, a
    standalone :class:`Pipeline`, an :class:`Expr` subtree — and returns
    a scheme-tagged hash like ``kustology-sem-v2:<64 hex chars>``.

    Two subtrees with the same semantic content collide:

    * Whitespace / formatting differences, comments included (same AST → same
      IR, plus ``raw_text`` and every span cleared before the dump)
    * ``tolower(X) == "y"`` vs ``X =~ "y"`` (normalize_expressions)
    * ``| where A | where B`` vs ``| where A and B`` (merge_consecutive_filters)
    * Nested ``and`` grouping vs flat chain (normalize_expressions)
    * ``not(not(X))`` collapse (normalize_expressions)
    * ``A and B`` vs ``B and A``, and the same for ``or`` — commutative
      operands are sorted into a canonical order
    * ``X in ("a", "b")`` vs ``X in ("b", "a")`` — a set test, so the order
      the values were written in carries no meaning
    * ``let X = …; X | take 1`` vs ``let Y = …; Y | take 1`` — a ``let`` name
      is a local label, replaced by its declaration index (``$let0``, …)
    * ``| where A | where B`` vs ``| where B | where A``, and either against
      ``| where B and A`` — the merge and the sort *compose*: consecutive
      filters become one ``And`` and that ``And``'s operands are then sorted,
      so the order of a run of filters stops mattering as well as its
      grouping. This falls out of the two rules above rather than being a
      rule of its own, which is why it is easy to miss.

    Two subtrees with different literal values, operators, identifiers,
    or operator sequences do *not* collide. Nor do the near-misses of the
    rules above: ``A < B`` and ``B < A`` are opposite predicates and only
    genuinely commutative operands are sorted; the ``let`` rename is
    positional, so reading from the first binding and reading from the
    second stay apart; and only a run of *consecutive* filters merges, so
    ``| where A | take 5`` and ``| take 5 | where A`` — which return
    different rows — still hash apart.

    The three canonicalization rules, stated once so a consumer can reason
    about what a stored digest promises:

    * **Sorting** touches exactly three places — ``And.operands``,
      ``Or.operands`` and ``SetMembership.values`` — keyed on each
      operand's own dumped JSON (:func:`_sort_commutative`). Nothing else
      in the IR is reordered; ``Expr.canonical_form`` sorts the same three
      places by *rendered string*, which is a different key for the same
      set, and ``normalize_expressions`` sorts nothing at all.
    * **``let`` renaming** is positional. Each visible binding name is
      replaced by its declaration index, so the names are labels and the
      wiring is not (:func:`_canonicalize_let_names`).
    * **Datetime literals are UTC.** The builder Kind-normalizes every
      ``datetime`` literal before recording ``value`` and ``ticks``
      (``_builder_helpers.literal_value_and_ticks``), and numeric and timespan
      literals render under ``InvariantCulture``, so the same query
      digests identically in Tokyo and New York and under ``de-DE``.

    **Bind state.** Everything the binder *writes* is stripped before the
    dump — ``result_type``, ``result_type_inner``, ``table``,
    ``result_schema`` and ``hints``, plus ``span`` / ``body_span``
    (:data:`_VOLATILE_FIELDS`) — so passing a schema does not move the
    digest. One divergence survives that, because it is a difference of
    *shape* rather than of a field's value: a ``let`` whose right-hand
    side aliases a table records ``rhs_expr`` unbound and ``rhs_pipeline``
    once the binder has proved the name is a table, and no field-clearing
    can make two different nodes into one. Queries with no table-aliasing
    ``let`` are unaffected. See the note above :data:`_VOLATILE_FIELDS`
    for why that is preferred to guessing.

    **Equal digests are not a proof of equivalence.** Several kinds of thing
    still merge, and they differ in whether that is a decision or a gap:

    * **Deliberate**, in literals — typed nulls and obfuscated strings; see
      :class:`~kustology.ir.expr.LiteralExpr` for why neither is a
      difference in what a query returns.
    * **Operator modifiers the builder drops.** Where an operator's IR node
      has no field for a modifier, the modifier cannot reach the digest, so
      two queries differing only there collide — ``mv-apply``'s
      ``to typeof(…)``, ``limit`` and ``with_itemindex=``, ``parse-kv``'s
      ``with (…)`` properties, ``getschema kind=csl``, ``consume
      decodeblocks=`` as of 0.2.0.
    * **A ``let``-declared function's body.** ``let f = (x:int) { … }``
      records a :class:`~kustology.ir.query.LetFunction` holding the
      parameter *names* and a ``body_span``; the body is not built and
      ``body_span`` is volatile, so nothing inside the braces reaches the
      digest. Two functions with the same name and parameter names but
      entirely different bodies collide, as do two that differ only in a
      parameter's declared type or default (neither is recorded). Parameter
      names and their count *do* split. The gap predates ``body_span``
      becoming volatile — before that the digest keyed on a source offset,
      which split two identical bodies over one extra space — so clearing
      it removed a wrong discriminator rather than creating this one.
      Modelling the body is post-0.2.0 work.

      This is also the one gap that makes the two tiers disagree about the
      *same* query rather than merely hashing it coarsely: on
      ``let f = () { SecurityEvent | where Account=="root" | project
      Computer }; f()`` (zero diagnostics), Tier 1's
      ``get_referenced_tables()`` reports ``{'SecurityEvent'}`` and
      ``get_referenced_columns()`` reports ``{'Account', 'Computer'}``,
      while Tier 2's ``find_all(ir, TableRef)`` and
      ``find_all(ir, ColumnRef)`` both return empty. Tier 1 walks
      Microsoft's tree, which has the body in it. Do not read a Tier 2
      lineage walk as exhaustive over a query that declares functions.
    * **Statements that are neither ``let`` nor tabular.** The builder
      collects ``let`` statements and pipelines; a statement of any other
      kind contributes nothing of its own, so whatever it said is absent
      from the digest. Two *different* values of one such statement
      therefore collide with each other, not merely with a query that omits
      it, and they parse with zero diagnostics so nothing signals the loss.
      ``set``, ``declare query_parameters``, ``declare pattern``,
      ``alias database`` and ``restrict access`` all behave this way today.

      **It is "contributes nothing of its own", not "is skipped".** The
      ``let`` collection walks ``GetDescendants[LetStatement]``, which is
      recursive, so a ``let`` *nested inside* one of these statements is
      hoisted into top-level ``let_bindings`` and does reach the digest —
      ``declare pattern P = (a:string) { ("x") = { let z = 5; T | take z };
      }; T | take 1`` splits both from the bare query and from the same
      pattern with ``z = 9``. That is a split where the paragraph above
      promises a merge, which is the safe direction (a dedup consumer fails
      to merge rather than merging wrongly), but it means the enclosing
      statement is not an opaque blank: only its own syntax is.

    A dedup consumer that must not merge across any of these has to compare
    more than the hash. Rather than trusting the lists above,
    run ``examples/semantic_hash_demo.py``: it hashes every case named here
    and raises if any stops behaving as filed.

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
        # Must run before _sort_commutative: renaming changes the JSON sort keys of operands containing LetValueRefs; sorted-then-renamed, two spellings of the same query order differently and split.
        _canonicalize_let_names(canonical)
    # After ``_clear_volatile``, so the sort key cannot see a span offset.
    _sort_commutative(canonical)
    if isinstance(canonical, QueryIR):
        # Named field by field rather than dumping the whole model, so that
        # ``raw_text``, ``semantic_hash`` and ``schema_attached`` stay out of
        # the digest. The cost of that choice is that a new field is invisible
        # here until it is added -- ``additional_pipelines`` hashed to nothing
        # at all while the builder filled it faithfully, so
        # ``T | count; U | count`` and ``T | count; V | count`` were one
        # digest. Add every field that carries query meaning.
        payload: Any = {
            "let_bindings": [lb.model_dump(mode="json") for lb in canonical.let_bindings],
            "main_pipeline": canonical.main_pipeline.model_dump(mode="json"),
            "additional_pipelines": [
                p.model_dump(mode="json") for p in canonical.additional_pipelines
            ],
        }
    else:
        payload = canonical.model_dump(mode="json")
    # See :func:`_strip_unwritten_fields` -- an unwritten evaluate schema
    # clause must dump exactly as it did before EvaluateOp had fields for it.
    _strip_unwritten_fields(
        payload, "evaluate", {"declared_schema": None, "declared_schema_star": False},
    )
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return f"{SEMANTIC_HASH_SCHEME}:{digest}"
