# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Opt-in IR transforms.

The IR builder produces a *faithful* representation of the source — consecutive
``| where`` operators stay distinct, original spans are preserved, no
semantic-equivalence rewriting is applied at build time. That keeps analyzers
that care about textual structure (span tracking, redundant-where lints,
formatting hints) unobstructed.

When you instead want a *canonical* view — for example "give me the
conjunction of all filter predicates as a single ``And``", or "rewrite
``tolower(X) == 'y'`` to ``X =~ 'y'``" — apply a transform from this module. Each transform is
opt-in, in-place, and traverses sub-pipelines so a single call covers nested
join/lookup/union/fork branches.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ._normalize import normalize_in_place
from .expr import (
    And,
    ColumnRef,
    Expr,
    FuncCall,
    LetValueRef,
    Or,
    SetMembership,
    TypedNameDecl,
)
from .query import (
    FilterOp,
    FuncCallSource,
    LetBinding,
    LetFunction,
    LetRef,
    PatternMatch,
    Pipeline,
    QueryIR,
    TableRef,
)
from .spans import Span
from .types import KustoType
from .walk import _models_in, find_all, walk


def merge_consecutive_filters(root: Pipeline | QueryIR) -> None:
    """Collapse each run of consecutive ``FilterOp``s into one ``FilterOp``.

    The merged operator's predicate is an ``And`` of the originals. Operates
    in place on ``root`` and every ``Pipeline`` reachable from it — join and
    lookup right sides, union and fork branches, mv-apply bodies, and nested
    sub-pipelines such as ``toscalar(...)`` and ``materialize(...)``
    arguments. The first ``FilterOp``'s span is preserved on the merged
    result; the others' outer spans are dropped (inner predicate spans
    survive unchanged).

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
# dictionary keys from the payload would be both too broad and too narrow:
# too broad because ``AssertSchemaOp.columns`` is a ``dict[str, str]`` of the
# user's own column names, so ``assert-schema (a:long, table:long)`` would
# lose the column literally called ``table`` and hash identically to
# ``(a:long)``; too narrow because ``LetFunction.body_span`` is a span whose
# field is not named ``span``, so source offsets would keep reaching the
# digest.
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
# information must keep hashing" rule the next paragraph applies, and the
# exception is decided by what the field *means*, not by where it comes
# from. A future field only belongs here if the query would return identical
# rows without it.
#
# ``join_side`` is deliberately *not* stripped, and is why it exists as a
# field at all. It keeps ``table`` to a single job -- the table the binder
# resolves (see ``SchemaAttacher._fill``) -- so the source-derived ``$left``
# / ``$right`` side never rides in a binder-written field. Carried there and
# hashed, the side would make the hash bind-dependent; carried there and
# stripped, ``$left.a == $left.b`` would collapse into
# ``$left.a == $right.b``, which are different queries. An unresolvable side
# is honestly ``None`` in ``table``, while the side itself lives in
# ``join_side`` and hashes on its own. Splitting the two apart is the
# standing remedy for lossy lowering -- see AGENTS.md.
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

# Cleared by the same pass on the same private copy, for a different reason,
# which is why they are a separate set rather than two more entries above.
# Nothing here is bind state: :attr:`LetBinding.inner_tables` and
# :attr:`LetBinding.inner_time_exprs` are a **derived index**, written by the
# builder from the very subtree (``rhs_pipeline`` / ``rhs_function``) that is
# already in the digest -- ``_collect_inner_tables`` / ``_collect_inner_time_exprs``
# are pure functions of it. So excluding them cannot merge anything the tree
# still splits: whatever the index recorded, the nodes it indexed are hashed
# beside it.
#
# Two things would go wrong if they were hashed, and both are the same fault
# -- an index is a copy, and a copy of a name desynchronizes from the name.
#
# * ``inner_tables`` is a list of plain ``str``. No node rename can reach a
#   string, so a body reading a tabular parameter would record ``["T"]``
#   against ``["U"]`` however thoroughly the ``TableRef`` beside it is
#   canonicalized, and two alpha-equivalent functions would split on the
#   index alone. A tabular parameter reference is indistinguishable from a
#   table name there (see :class:`~kustology.ir.query.LetBinding`), and any
#   rename over a name this index can hold has the same exposure.
# * ``inner_time_exprs`` holds the *same objects* as the right-hand side
#   beside it rather than copies, so a rename walk would reach each one
#   twice -- correctly only for as long as ``rhs_function`` happens to be
#   declared before the index on :class:`~kustology.ir.query.LetBinding`.
#   Clearing the index before the rename runs leaves no second path to reach
#   the node by, so nothing hangs on declaration order.
#
# A future index field over query content belongs here, not above: the test is
# whether the field's value is *recoverable* from what is already hashed.
_DERIVED_INDEX_FIELDS = frozenset({"inner_tables", "inner_time_exprs"})

_CLEARED_FIELDS = _VOLATILE_FIELDS | _DERIVED_INDEX_FIELDS

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
# checked, it emits ``null`` with no warning -- but the payload it produces
# fails to validate back, and the copy is a live IR that ``walk`` and
# ``model_dump`` both traverse before it is thrown away. A real ``Span`` that
# is identical at every node costs nothing and leaves no invalid state behind.
#
# One instance is shared by every node on the copy. The copy is dumped and
# discarded inside ``compute_semantic_hash``, so the aliasing is never
# observable; nothing mutates a ``Span`` in place.
_ZERO_SPAN = Span(text_start=0, width=0)


def _normalize_raw_text(text: str) -> str:
    """Fold each line break in ``raw_text``, indent included, to one space.

    Nothing else is touched. The operators the IR keeps as source text
    (``scan``, ``top-nested``, the ``graph-*`` family, and the ``Unknown*``
    fallbacks) hash that text directly, so without the fold
    ``| top-nested 3 of a`` and ``|   top-nested\\n3 of a`` would be two
    different queries as far as the digest is concerned.

    The rule is deliberately narrow, because ``raw_text`` is source text and
    two of the things that look like formatting in it are data:

    * **Interior spacing is not collapsed.** A run of spaces can be *inside a
      string literal*, where it is part of the value: a rule matching
      ``"error  occurred"`` (two spaces) and one matching ``"error occurred"``
      are different predicates, and collapsing every whitespace run would
      merge them. Outside a literal there is nothing left to collapse anyway —
      ``IncludeTrivia.Minimal`` has already normalized it, and records
      ``top-nested 3  of  a`` as ``top-nested 3 of a``. Newlines are the safe
      case precisely because a KQL string literal cannot contain a raw one,
      so this rule can never reach inside a literal.
    * **Comments are not stripped.** ``Minimal`` already drops every comment
      in and around the node, so there is nothing to remove — while ``//`` is
      also the middle of every URL a detection rule matches on. A regex from
      ``//`` to end-of-line would truncate ``Url == "http://a"`` and
      ``Url == "http://b"`` to the same text.

    Both boundaries are pinned by tests; widening this function to
    ``" ".join(text.split())`` fails the first, and adding a comment strip
    fails the second.
    """
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _clear_volatile(root: BaseModel) -> None:
    """Clear every digest-excluded field on ``root`` and its descendants.

    In place: :data:`_VOLATILE_FIELDS` (bind state and offsets) and
    :data:`_DERIVED_INDEX_FIELDS` (values recoverable from the subtree that
    is hashed anyway). Intended for the hash's private deep copy — it
    rewrites node state.
    """
    for node in walk(root):
        fields = type(node).model_fields
        for name in _CLEARED_FIELDS & fields.keys():
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
# to change, since ``_canonicalize_let_names`` renames by one isinstance
# check against the whole tuple.
#
# ``ColumnRef`` deliberately is not a member and must not become one: a real
# column named ``n`` is a different query from a ``let``-bound ``n``, so
# renaming one would collapse the two. A *function parameter* is renamed
# through those very classes, and that is not an exception to this rule but a
# consequence of it: the builder lowers a shadowed parameter reference as a
# ``ColumnRef`` / ``TableRef``, so it is reached by the scope-local parameter
# map (:data:`_PARAM_NAME_MODELS`) inside the one body that bound the name,
# never by this tuple and never anywhere else in the query.
#
# ``LetBinding`` is a member as the declaration, but it is renamed only by the
# scope walker below, never through the reference map -- see the docstring.
_LET_NAME_MODELS: tuple[type[BaseModel], ...] = (LetBinding, LetRef, LetValueRef)

# A call site's name: source position, expression position and ``invoke``'s
# callee, which is the expression-position class reached by a third builder
# path. ``let f = () { … }; f()`` writes the binding's own name at the call,
# so it is as much a local label as the declaration is.
#
# The gate on these is **narrower** than on the references above, and it has
# to be: a visible ``let`` of the call's name is not enough, because KQL keeps
# values and functions in separate namespaces. ``let abs = 5; T | extend y =
# abs(x)`` binds a value called ``abs`` and still calls the *built-in* ``abs``,
# and Microsoft's binder accepts it with no diagnostic at all -- ``abs(x) +
# abs`` resolves both readings in one expression. A name-only gate would
# therefore rename that call to the binding's ``$let<i>`` and merge the query
# with the ``sqrt`` spelling of it, which computes something else. So only a ``let``
# that bound a **function** (``rhs_function``) renames its call sites, and
# everything else -- a built-in, a stored server-side function, a call whose
# name a scalar binding happens to share -- is left exactly as written. Both
# halves are pinned: ``let-scalar-shadowing-a-builtin-call`` and
# ``let-function-body-server-call`` in ``tests/ir/test_hash_battery.py``.
_CALL_SITE_NAME_MODELS: tuple[type[BaseModel], ...] = (FuncCallSource, FuncCall)

# The two models a *parameter* reference lowers to. Kept apart from the tuples
# above because the rename that reaches them is scoped differently: they carry
# ordinary column and table names nearly everywhere they appear, so they are
# rewritten only within the body of the declaration that bound the name, and
# only against that scope's own parameter map -- never against ``visible``.
_PARAM_NAME_MODELS: tuple[type[BaseModel], ...] = (ColumnRef, TableRef)


def _canonicalize_let_names(ir: QueryIR) -> None:
    """Rename every ``let`` binding to ``$let<i>``, and every function
    parameter to ``$param<i>``, in scope-walk order.

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

    **Scope.** References resolve against the bindings *visible where they are
    written*: a binding's own right-hand side sees only the bindings declared
    before it, the main pipeline sees them all. That is what keeps a shadowed
    reference pointing at the binding it actually reads, instead of at the one
    currently being defined. A ``let``-declared function body opens a **child
    scope**, seeded from what is visible at the declaration site -- so a body
    reference to an outer binding renames through it, a ``let`` written inside
    the body extends the child scope only, and neither is visible to the query
    outside the braces. A ``declare pattern`` arm's body is the same
    construct reached by a different route (a ``FunctionBody`` owned by a
    :class:`~kustology.ir.query.PatternMatch`) and opens a child scope the
    same way.

    **A call site is a reference, when the binding is a function.**
    ``let f = () { … }; f()`` writes the binding's own name at the call, so it
    renames through ``visible`` like any other reference
    (:data:`_CALL_SITE_NAME_MODELS`) -- but only if that binding populated
    ``rhs_function``. A visible ``let`` of the name is *not* sufficient,
    because KQL resolves values and functions in separate namespaces:
    ``let abs = 5; T | extend y = abs(x)`` calls the built-in and binds a
    value, both cleanly, so renaming the call through the value binding would
    merge it with the ``sqrt`` spelling. Everything outside the narrowed gate -- a
    built-in, a stored server-side function, a call sharing a scalar
    binding's name -- is left exactly as written, so two queries calling two
    different functions still hash apart.

    One residual is accepted rather than closed: a ``let`` bound to a function
    by *alias* (``let g = f; g()``) records ``rhs_expr``, not
    ``rhs_function``, so its call sites keep the written name and two
    spellings of such a query split. That is the safe direction -- a split
    costs a dedup consumer a duplicate, where the merge this narrowing
    prevents costs it a rule.

    **Parameters are a second scope, with a counter of their own.** A
    parameter is a local label too -- ``(w:int) { … x > w }`` and
    ``(z:int) { … x > z }`` are one function -- but it is not a ``let``, and
    it is reached through different classes: the builder lowers a body
    reference under a parameter's shadow to a ``ColumnRef`` / ``TableRef``
    (see :class:`~kustology.ir.query.LetFunctionParameter`), never to a
    ``LetValueRef`` / ``LetRef``. So each declaration's parameters are
    numbered ``$param<i>`` in declaration order and a **scope-local** map is
    applied to those two classes within that body's subtree and nowhere else
    -- ``visible`` never carries a parameter, and no query outside the braces
    can see one. Numbering runs before the body's own ``let``s, so the two
    sequences do not interleave with each other's nesting.

    The shadow edge is where the two renames meet, and they do not collide:
    in ``let S = (w:int) { let w = 5; A | where x > w }`` the *declaration*
    renames as a ``let`` (``LetBinding`` → ``$let<i>``, through the child
    scope) while the *reference* renames as a parameter (``ColumnRef`` →
    ``$param<i>``, through the map), because the builder had already committed
    that reference to the shadow reading. Two disjoint node classes, so
    neither pass can reach the other's node and their order is not
    load-bearing.

    Two boundaries follow from applying the map textually, and both are the
    reading the builder already committed to rather than new guesses:

    * A body reference matching a parameter name is treated as the parameter
      even where KQL would resolve it to a row-scope column of the same name
      first -- the same text-only shadow rule, and the same accepted
      consequence, that :class:`~kustology.ir.expr.LetValueRef` documents for
      ``let`` names.
    * A ``declare query_parameters`` statement's names are never renamed, by
      either mechanism, and there the exclusion is a *decision* rather than a
      consequence: those names are the caller-facing API of a saved query, so
      renaming them would merge two queries with different call contracts --
      see :class:`~kustology.ir.query.QueryParametersStmt`. A
      ``declare pattern``'s own parameters stay verbatim too, for the reason
      :class:`~kustology.ir.query.PatternStmt` records: they are not bound as
      names inside an arm's body (the builder shadows nothing there), so this
      scope has nothing to apply and folding two spellings of a pattern
      signature would be a merge nothing here can justify.

    Both counters are **query-global**, not per scope. Numbering each scope
    from zero would let a body's own first binding and its encloser's first
    binding both be ``$let0``, so a body reading its own ``z`` and a body
    reading the outer ``n`` would render identically -- two different queries,
    one digest. Parameters are the sharper case, since nesting makes them
    cumulative by construction (the builder's ``_param_names`` accumulates
    through nested bodies): two nested ``$param0``s would be one name for two
    different parameters, both live at once. One counter each over a
    deterministic walk order keeps every name in the query distinctly
    numbered.

    A single ``seen`` id-set spans the whole traversal, guarding against a
    node two fields both reach being renamed twice. No such path exists in
    the copy this runs on: ``LetBinding.inner_time_exprs``, the field that
    aliases the ``rhs_pipeline`` / ``rhs_function`` beside it, is cleared
    before this runs (:data:`_DERIVED_INDEX_FIELDS`), so the set is
    insurance against a future aliasing field rather than something the
    rename depends on.
    """
    # The early-out has to account for ``statements`` as well as bindings: a
    # ``declare pattern`` arm can declare a ``let`` of its own while the query
    # declares none, and that name is as much a local label as a top-level one.
    if not ir.let_bindings and not ir.statements:
        return
    counter = itertools.count()
    param_counter = itertools.count()
    seen: set[int] = set()
    # The canonical names of the bindings that bound a *function*, which is
    # what a call site is allowed to rename through. One query-global set
    # rather than a second scope-threaded map: a canonical name is unique
    # across the query, so by the time ``visible`` has answered "which binding
    # does this name reach from here", scope is already resolved and all that
    # is left to ask is what that binding bound. Shadowing follows for free --
    # ``let f = () { … }; let f = 5;`` leaves ``visible["f"]`` pointing at the
    # scalar's canonical name, which is not in here.
    function_lets: set[str] = set()

    def rename(node: BaseModel, visible: dict[str, str]) -> None:
        """Rewrite every reference under ``node`` against ``visible``."""
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, LetFunction):
            # The body is a child scope. Parameters and their defaults are
            # written at the declaration site, so they resolve in the parent
            # -- the *name* half of each parameter is the body's, and
            # ``canon_body`` takes it from here.
            for parameter in node.parameters:
                rename(parameter, visible)
            for statement in node.body_query_parameters:
                rename(statement, visible)
            canon_body(node, [p.decl for p in node.parameters], visible)
            return
        if isinstance(node, PatternMatch):
            # A pattern arm's body is a ``FunctionBody`` too, so it opens a
            # child scope on exactly the same terms -- the same helper, with
            # no parameters of its own to number. The arm's own selector --
            # the matched values and the path value -- is written at the
            # declaration site and resolves in the parent, the way a
            # function's parameter defaults do. The pattern's declared
            # parameters sit on the owning ``PatternStmt`` and stay verbatim:
            # they name the arm's match slots, not values an arm's body can
            # read, so there is nothing in here for a map to rewrite.
            for value in node.values:
                rename(value, visible)
            if node.path_value is not None:
                rename(node.path_value, visible)
            canon_body(node, [], visible)
            return
        if isinstance(node, _LET_NAME_MODELS) and not isinstance(node, LetBinding):
            object.__setattr__(node, "name", visible.get(node.name, node.name))
        elif isinstance(node, _CALL_SITE_NAME_MODELS):
            # The same lookup, then one more question -- see
            # :data:`_CALL_SITE_NAME_MODELS` for why the name alone is not
            # enough. ``visible.get`` yields ``None`` for a name no binding
            # reaches, and ``None`` is never in the set.
            canonical = visible.get(node.name)
            if canonical in function_lets:
                object.__setattr__(node, "name", canonical)
        for field_name in type(node).model_fields:
            for item in _models_in(getattr(node, field_name)):
                rename(item, visible)

    def canon_scope(bindings: list[LetBinding], visible: dict[str, str]) -> None:
        """Number one scope's declarations in order, extending ``visible``."""
        for binding in bindings:
            written = binding.name
            # The right-hand side is resolved *before* this binding enters
            # scope, so a self-reference names whatever it named to the parser.
            rename(binding, visible)
            canonical = f"$let{next(counter)}"
            visible[written] = canonical
            if binding.rhs_function is not None:
                function_lets.add(canonical)
            object.__setattr__(binding, "name", canonical)

    def canon_body(
        owner: LetFunction | PatternMatch,
        decls: list[TypedNameDecl],
        visible: dict[str, str],
    ) -> None:
        """Walk one declaration's body as a child scope: parameters numbered,
        then the body's own ``let``s, then the tail -- and finally the
        parameter map applied over the whole body subtree.

        Shared by both constructs that own a ``FunctionBody``, which is what
        keeps a body reached through a ``declare pattern`` arm scoped exactly
        as one reached through a ``let``-declared function.

        The parameter map is applied *after* the recursive walk, and that
        order is what makes an inner scope win over an outer one: a nested
        function's own parameters are already ``$param<i>`` by the time this
        map is offered the subtree, and a canonical name matches no key here
        (the keys are names the query wrote, and ``$`` is not a legal KQL
        identifier character). A body reference to an *enclosing* function's
        parameter is left untouched by the inner map for the same reason, and
        picked up by this one.
        """
        param_map = canon_params(decls)
        body_visible = dict(visible)
        canon_scope(owner.body_lets, body_visible)
        # The body's own ``let``s are part of the body: a parameter read in
        # one's right-hand side is the same reference as one read in the tail.
        bodies: list[BaseModel] = [*owner.body_lets]
        for tail in (owner.body_pipeline, owner.body_expr):
            if tail is not None:
                bodies.append(tail)
                rename(tail, body_visible)
        for sub in bodies:
            apply_params(sub, param_map)

    def canon_params(decls: list[TypedNameDecl]) -> dict[str, str]:
        """Number one declaration's parameters, returning the body-local map.

        The canonical names are handed out first and the map is built from the
        names as *written*, before any declaration is overwritten -- otherwise
        the second parameter of ``(a:int, b:int)`` would map from the name the
        first one had been given.

        Declarations are renamed by position for the reason
        :func:`canon_scope`'s are: a duplicate name is a query KQL rejects but
        the error-tolerant builder still emits, and a name-keyed rename would
        stop ``$param<i>`` meaning "the ith parameter". References follow the
        map, so on such a query the last declaration of a name wins -- which
        is the same rule a shadowing redeclaration follows everywhere else.
        """
        canonical = [f"$param{next(param_counter)}" for _ in decls]
        param_map = dict(zip((d.name for d in decls), canonical, strict=True))
        for decl, name in zip(decls, canonical, strict=True):
            object.__setattr__(decl, "name", name)
        return param_map

    def apply_params(node: BaseModel, param_map: dict[str, str]) -> None:
        """Rewrite one body's parameter references, in place.

        A plain ``walk`` rather than the scope-aware ``rename`` above: by the
        time this runs every scope decision inside the subtree has been made,
        and what is left is a flat substitution over two classes.
        """
        if not param_map:
            return
        for sub in walk(node):
            if not isinstance(sub, _PARAM_NAME_MODELS):
                continue
            if sub.name not in param_map:
                continue
            # A qualified name is never a parameter reference: the builder
            # lowers a shadowed parameter to a bare ``TableRef``, so
            # ``database("d").w`` names a real table in a real database and
            # renaming it would merge two queries that read different ones.
            # ``ColumnRef`` needs no such guard -- its ``table`` is
            # binder-written and already cleared before this runs.
            if isinstance(sub, TableRef) and (sub.database or sub.cluster):
                continue
            object.__setattr__(sub, "name", param_map[sub.name])

    visible: dict[str, str] = {}
    canon_scope(ir.let_bindings, visible)
    # Every tabular statement, not only the first: a ``let`` declared once is
    # in scope for all of them, so a reference written after the second
    # semicolon has to be renamed too or the rename stops being one. The other
    # statement kinds are in the same position -- ``restrict access to (V)``
    # over a ``let``-bound view reads the binding -- and they come last so the
    # numbering of a pattern arm's own bindings is deterministic.
    for node in (ir.main_pipeline, *ir.additional_pipelines, *ir.statements):
        rename(node, visible)


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
    is positioned by whichever field is declared first, and if the index came
    first the node would be yielded above its real parent and sorted after
    it. The IR's only aliasing field is ``LetBinding.inner_time_exprs``,
    which holds the same objects as the ``rhs_pipeline`` / ``rhs_function``
    beside it -- and :func:`_clear_volatile` empties it before this runs
    (:data:`_DERIVED_INDEX_FIELDS`), so no alias survives to be
    mispositioned. A new index field over
    ``And``/``Or``/``SetMembership`` that reached the digest would need a
    genuinely post-order traversal here rather than a reversed pre-order --
    or, better, to be excluded the same way these two are.

    ``walk`` iterates ``model_fields``, so this reaches a ``let`` function's
    body as readily as a top-level pipeline: ``let f = () { T | where a and b
    }`` and the same body written ``b and a`` are one digest, with no case
    here for the body at all.

    Runs on the hash's deep copy only -- the public ``normalize_expressions``
    leaves the query's own order alone.
    """
    for node in reversed(list(walk(root))):
        if isinstance(node, (And, Or)):
            node.operands = sorted(node.operands, key=_operand_sort_key)
        elif isinstance(node, SetMembership):
            node.values = sorted(node.values, key=_operand_sort_key)


def _operand_sort_key(child: BaseModel) -> str:
    """Key an operand by its own canonical JSON dump, a total order.

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
# unreleased window: every canonicalization change that lands between two
# releases shares one increment, however many branches carry them. Bumping
# per branch would burn tags nobody ever saw and leave gaps in the released
# sequence that a later reader has to go digging to explain. Bump on the
# first change *after* a release, not on every change.
#
# The one thing never to do is reuse a tag for different rules: a stored hash
# whose prefix stops implying its canonicalization is exactly the silent
# wrong answer the prefix exists to prevent. Renumbering down into an
# unreleased window is only safe while nothing has consumed the intermediate
# value.
SEMANTIC_HASH_SCHEME = "kustology-sem-v2"


def _strip_unwritten_fields(value: Any, kind: str, defaults: dict[str, Any]) -> None:
    """Delete an operator dict's keys when every one is at its unwritten default.

    Operates in place, on the *dumped* JSON structure. The sibling of
    :data:`_VOLATILE_FIELDS`/:func:`_clear_volatile` above, for the opposite
    kind of field: one that is plain and non-volatile (a written value must
    reach the digest -- clearing it to a canonical default the way
    ``_clear_volatile`` clears bind state would hide real differences) but
    whose *unwritten* default must not move the digest of a query that never
    uses it, the way :class:`~kustology.ir.query.EvaluateOp`'s
    ``declared_schema``/``declared_schema_star`` must not. A Pydantic field
    cannot be conditionally *absent* from one ``model_dump`` call: the key
    is always present at its default, so merely declaring such a field would
    move the digest of every query using that operator, written or not --
    and only the written case is a real collision closing. Operating after
    the dump, on the plain dict/list tree rather than the model, is what
    makes the omission conditional: an operator dict whose fields are all
    still at ``defaults`` dumps exactly as if the fields were never
    declared, and only a dict with at least one written field keeps the new
    keys at all.

    ``defaults`` maps field name to its *exact* unwritten value, compared by
    identity (``is``) for a scalar -- evaluate's is ``None``/``False``, and
    ``0 == False`` would wrongly match a future int-typed field whose real
    default is ``0`` under equality. A ``list``/``dict`` default
    (``ParseKvOp.properties``'s ``[]``, for example) is compared by ``==``
    instead: a freshly dumped empty list is never the *same object* as a
    literal ``[]`` written at a call site, so ``is`` could never match one
    and the field would never strip -- exactly the corpus-movement bug this
    call exists to prevent, only moved from "wrong" to "silently does
    nothing". Equality carries none of the ``0 == False`` risk here, because
    that risk is a cross-type collision between scalars and no
    ``list``/``dict`` value equals a bool, an int or ``None``. Every field
    in ``defaults`` must match for the dict's keys to be dropped: a
    partially-written modifier (only one of several flags set, for example)
    keeps every key, so the digest still reflects what was actually written.
    The modifier fields on :class:`~kustology.ir.query.MvApplyOp`,
    :class:`~kustology.ir.query.ParseKvOp`,
    :class:`~kustology.ir.query.GetSchemaOp` and
    :class:`~kustology.ir.query.ConsumeOp` all flow through this same path
    -- reuse it for the next such field rather than writing a new one-off
    dict walk.

    The gate also requires ``field in value`` for every field, not merely
    ``value.get(field) is default``: a dict that lacks the keys entirely
    dumps ``.get`` as ``None`` too, and some of the field sets stripped here
    are ``None`` all the way down (mv-apply's, for one), so without the
    presence check a *foreign* dict that merely lacks the keys (wrong shape,
    not an unwritten instance of this one -- say, a nested ``properties``
    dict that happens to carry the same ``kind`` tag) would match the gate
    and then crash on ``del``. Requiring the keys costs a genuine operator
    dict nothing -- every real dump carries every declared key, written or
    not -- while a shape that never had the keys at all fails the gate
    instead of being misread as "all defaults". That is the correct read: a
    dict with nothing to strip is left alone, not silently matched and then
    no-opped.
    """
    def _at_default(actual: Any, default: Any) -> bool:
        if isinstance(default, (list, dict)):
            return actual == default
        return actual is default

    if isinstance(value, dict):
        if value.get("kind") == kind and all(
            field in value and _at_default(value.get(field), default)
            for field, default in defaults.items()
        ):
            for field in defaults:
                del value[field]
        for child in value.values():
            _strip_unwritten_fields(child, kind, defaults)
    elif isinstance(value, list):
        for item in value:
            _strip_unwritten_fields(item, kind, defaults)


def compute_semantic_hash(node: BaseModel) -> str:
    """Return the scheme-tagged SHA-256 of the IR's canonical shape.

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
      is a local label, replaced by its position in a scope-ordered walk
      (``$let0``, …). Holds for a ``let`` written inside a function body or a
      ``declare pattern`` arm too, and for the name at a **call site** when
      the binding is a function: ``let f = () { … }; f()`` and
      ``let g = () { … }; g()`` are one digest, while any other call — a
      built-in, a server-side function, one sharing a *scalar* binding's
      name, which KQL resolves in a separate namespace, or one whose binding
      only *aliases* a function (``let g = f; g()``) — is left as written
    * ``let f = (w:int) { T | where a > w }`` vs the same function written
      ``(z:int) { T | where a > z }`` — a *parameter* name is a local label
      too, replaced by its position in the signature (``$param0``, …) along
      with every reference to it in that body. A ``declare pattern``'s own
      parameters and a ``declare query_parameters`` name are **not** local
      labels and are never renamed — see
      :class:`~kustology.ir.query.PatternStmt` and
      :class:`~kustology.ir.query.QueryParametersStmt`
    * ``| where A | where B`` vs ``| where B | where A``, and either against
      ``| where B and A`` — the merge and the sort *compose*: consecutive
      filters become one ``And`` and that ``And``'s operands are then sorted,
      so the order of a run of filters stops mattering as well as its
      grouping. This falls out of the two rules above rather than being a
      rule of its own, which is why it is easy to miss.

    Two subtrees with different literal values, operators, identifiers,
    or operator sequences do *not* collide. Nor do the near-misses of the
    rules above: ``A < B`` and ``B < A`` are opposite predicates and only
    genuinely commutative operands are sorted; both renames are positional,
    so reading from the first binding and reading from the second stay apart,
    as do reading the first parameter and reading the second; and only a run
    of *consecutive* filters merges, so
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
    * **Name renaming** is positional and scope-ordered, over two sequences.
      Each ``let`` binding is replaced by its position in one query-global
      ``$let<i>`` sequence, taken in scope order — top-level declarations,
      then a function body's or a pattern arm's own declarations as that body
      is reached — and each reference, call sites included, by whatever was
      visible where it was written. Each *parameter* is replaced by its
      position in a second query-global ``$param<i>`` sequence, and its
      references are rewritten within that one body only. The names are
      labels and the wiring is not (:func:`_canonicalize_let_names`). Two
      kinds of name are deliberately outside both rules: a ``declare
      pattern``'s own parameters, which name an arm's match slots rather than
      anything an arm's body reads, and a ``declare query_parameters`` name,
      which is a caller-facing API name.
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
    can make two different nodes into one. The tail of a ``let``-function
    body is the same grammatical position read by the same predicate
    (``builder._is_tabular_rhs``), so ``let f = () { OtherTable }`` diverges
    the same way, on ``body_expr`` vs ``body_pipeline``. Queries that do not
    alias a bare table name in either position are unaffected. See the note
    above :data:`_VOLATILE_FIELDS` for why that is preferred to guessing.

    **Derived indexes.** Two more fields are excluded, for an unrelated
    reason: :attr:`~kustology.ir.query.LetBinding.inner_tables` and
    :attr:`~kustology.ir.query.LetBinding.inner_time_exprs` are an index the
    builder writes over the right-hand side sitting beside them
    (:data:`_DERIVED_INDEX_FIELDS`), so hashing them added nothing the tree
    does not already carry — while, being a *copy* of names the renames above
    rewrite, they cost the collisions that copy desynchronized on. They are
    still on your own IR, populated as always; only the digest ignores them.

    **Equal digests are not a proof of equivalence.** What still merges is
    listed here, and each entry is a decision rather than a gap:

    * **Deliberate**, in literals — typed nulls and obfuscated strings; see
      :class:`~kustology.ir.expr.LiteralExpr` for why neither is a
      difference in what a query returns.
    * **A ``let``-declared function's call sites.** ``let f = () { … }; f();
      f()`` records the body once, on the declaration, and leaves each ``f()``
      a call. So a query that calls a function twice does not hash as one that
      inlines the body twice — which is the right answer for dedup (the two
      queries *are* the same query) but means a caller counting a table's
      occurrences in the digest is counting declarations, not reads.
    * **A local name that shadows a real column.** Both renames above decide
      what is local from the query text, where KQL resolves an unqualified
      name to a row-scope column first, so ``let Count = 5; T | where Count >
      1`` against a ``T`` that really has ``Count`` merges with the same query
      written ``let Other = 5; … Other > 1``. A function parameter shadowing a
      column behaves the same way. Deciding by symbol needs a bound parse and
      would put the digest back under bind state — see
      :class:`~kustology.ir.expr.LetValueRef` for the trade.

    A dedup consumer that must not merge across any of these has to compare
    more than the hash. Each entry is pinned, though not all in one place, so
    read the pin rather than trusting the prose above. The literal-level
    merges are ``KNOWN_MERGES`` in ``examples/semantic_hash_demo.py``, which
    hashes every row it holds when it runs and raises if one stops behaving
    as filed. Recording the body once at the declaration is pinned by
    ``test_a_call_site_is_still_not_expanded`` in
    ``tests/ir/test_let_bindings.py``, and the shadow merge by
    ``test_the_shadowing_case_collapses_two_queries_onto_one_hash`` in
    ``tests/ir/test_let_value_ref.py``. Neither of those is in
    ``KNOWN_MERGES``, so the demo's table is not the whole list.

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
        # Must run before ``_sort_commutative``: renaming changes the JSON
        # sort key of any operand containing a ``LetValueRef`` / ``LetRef``,
        # so sorted-then-renamed, two spellings of the same query order
        # differently and split.
        #
        # The rename recurses into every ``LetFunction`` scope, so this
        # ordering covers a function body as well as the top level: by the
        # time ``_sort_commutative`` reaches ``let f = () { T | where a > $let0
        # and b > 1 }``, the body's references are already canonical and key
        # the same way whatever the query called the binding.
        _canonicalize_let_names(canonical)
    # After ``_clear_volatile``, so the sort key cannot see a span offset.
    _sort_commutative(canonical)
    if isinstance(canonical, QueryIR):
        # Named field by field rather than dumping the whole model, so that
        # ``raw_text``, ``semantic_hash`` and ``schema_attached`` stay out of
        # the digest. The cost of that choice is that a new ``QueryIR`` field
        # is invisible here until it is added -- the builder can fill one
        # faithfully while it hashes to nothing at all, and two queries
        # differing only there become one digest. Add every field that
        # carries query meaning.
        payload: Any = {
            "let_bindings": [lb.model_dump(mode="json") for lb in canonical.let_bindings],
            # Present even when empty, the way ``additional_pipelines`` is:
            # a key whose presence depended on its own contents would make
            # the payload's shape data. ``_strip_unwritten_fields`` is for
            # an operator *field* whose unwritten default must dump as
            # though undeclared; a top-level payload key is part of the
            # payload's fixed shape and gets no such treatment.
            "statements": [s.model_dump(mode="json") for s in canonical.statements],
            "main_pipeline": canonical.main_pipeline.model_dump(mode="json"),
            "additional_pipelines": [
                p.model_dump(mode="json") for p in canonical.additional_pipelines
            ],
        }
    else:
        payload = canonical.model_dump(mode="json")
    # See :func:`_strip_unwritten_fields` -- an unwritten evaluate schema
    # clause must dump as though ``EvaluateOp`` had no fields for it.
    _strip_unwritten_fields(
        payload, "evaluate", {"declared_schema": None, "declared_schema_star": False},
    )
    # Same mechanism for the other modeled-modifier fields: an unwritten
    # one dumps as though undeclared, so a query that writes no modifier --
    # every mv-apply corpus fixture, among others -- digests as the bare
    # operator.
    _strip_unwritten_fields(
        payload, "mv_apply",
        {"to_typeof": None, "row_limit": None, "item_index": None},
    )
    _strip_unwritten_fields(payload, "parse_kv", {"properties": []})
    _strip_unwritten_fields(payload, "getschema", {"output_kind": None})
    _strip_unwritten_fields(payload, "consume", {"decodeblocks": None})
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return f"{SEMANTIC_HASH_SCHEME}:{digest}"
