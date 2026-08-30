# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Opt-in IR transforms.

The IR builder represents the source faithfully: consecutive ``| where``
operators stay distinct, original spans survive, and nothing is rewritten for
semantic equivalence at build time. Analyzers that read textual structure (span
tracking, redundant-where lints, formatting hints) depend on that.

Apply a transform from this module when you want a canonical view instead, such
as the conjunction of all filter predicates as one ``And``, or
``tolower(X) == 'y'`` rewritten to ``X =~ 'y'``. Each transform is opt-in and
in-place, and traverses sub-pipelines, so one call covers nested join, lookup,
union and fork branches.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from .._ir_tags import SEMANTIC_HASH_SCHEME
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
from .walk import _models_in, find_all, span_of, walk


def merge_consecutive_filters(root: Pipeline | QueryIR) -> None:
    """Collapse each run of consecutive ``FilterOp``s into one ``FilterOp``.

    The merged operator's predicate is an ``And`` of the originals. Operates in
    place on ``root`` and every ``Pipeline`` reachable from it: join and lookup
    right sides, union and fork branches, mv-apply bodies, and nested
    sub-pipelines such as ``toscalar(...)`` and ``materialize(...)`` arguments.
    The merged span covers the whole run, from the first ``FilterOp``'s start
    to the last one's end. The other outer spans are dropped, and inner
    predicate spans survive unchanged.

    Deep-copy with ``model_copy(deep=True)`` first if you need the original.
    """
    for pipeline in list(find_all(root, Pipeline)):
        pipeline.operators = _merge_at_one_level(pipeline.operators)


def normalize_expressions(root: Expr | Pipeline | QueryIR) -> Expr | Pipeline | QueryIR:
    """Apply semantic-preserving expression rewrites everywhere in ``root``.

    Rewrites (from :mod:`kustology.ir._normalize`):

    * ``tolower(X) == "y"`` → ``X =~ "y"`` (case-insensitive equality), and
      ``toupper(X) == "Y"`` → ``X =~ "Y"``, on either side of the comparison
      and only when the literal is already in the folded case. Neither a
      literal in the wrong case (``tolower(X) == "Y"``, always false) nor a
      non-literal operand (``tolower(X) == Col``) is equivalent to the ``=~``
      form, so both are left alone.
    * ``tolower(X) != "y"`` → ``X !~ "y"`` and the ``toupper`` mirror, under
      the same case-matching condition
    * Nested ``And`` / ``Or`` operands flattened into a single list
    * ``not(not(X))`` → ``X``

    Traversal is post-order, children first, so a ``not(not(X))`` nested many
    layers inside a ``not(...)`` collapses cleanly. It descends into
    sub-pipelines, expression children, list-valued fields, and tuple branches
    alike.

    Mutates ``root`` in place and returns the root to keep working with,
    normally ``root`` itself. ``not(not(X))`` replaces a node instead of
    editing it, and a root has no parent field to install the replacement into,
    so rebind (``ir = normalize_expressions(ir)``) when ``root`` may be a bare
    ``Expr``. A ``Pipeline`` or ``QueryIR`` root is never replaced, so call
    sites that ignore the return value stay correct.

    Operand order stays exactly as the query wrote it; reordering an ``and``
    chain would move the user's spans out of source order.
    ``compute_semantic_hash`` sorts commutative operands on its own private
    copy.

    Deep-copy with ``model_copy(deep=True)`` first if you need the original.
    """
    return _normalize_node(root)


def _normalize_node(node: Any) -> Any:
    """Recursively descend; return the (possibly replaced) node.

    Only ``Expr`` nodes are replaced: ``normalize_in_place`` may return a
    different object for ``not(not(X)) → X``. The ``setattr`` below propagates
    the replacement to the parent.
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
                    # ``And`` always yields bool. Set it explicitly so the
                    # merge matches what the parser emits for a single
                    # ``where A and B``.
                    merged = And(
                        operands=[merged, nxt],
                        span=op.span,
                        result_type=KustoType.BOOL,
                    )
                j += 1
            if j > i + 1:
                # A run of more than one ``FilterOp`` merged, either into a
                # fresh ``And`` above or onto ``op``'s own already-``And``
                # predicate in place, which leaves ``merged is op.predicate``
                # true below. Either way the span must widen to the last
                # merged ``where``.
                last = ops[j - 1]
                op.span = Span(
                    text_start=op.span.text_start,
                    width=last.span.text_end - op.span.text_start,
                )
                # The predicate widens with the operator: ``merged`` is an
                # ``And`` on both paths and holds every merged ``where``'s
                # operands, so a consumer highlighting the offending condition
                # needs the whole run. Assign after the widening -- the ``And``
                # above captured the narrow ``op.span``, and the line above
                # rebinds ``op.span`` to a new object without mutating that one.
                merged.span = op.span
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
# name**, matched against ``type(node).model_fields`` at every node, not by key
# path and not by key name in the dumped JSON. Dropping dictionary keys from
# the payload would be too broad and too narrow at once:
# ``AssertSchemaOp.columns`` is a ``dict[str, str]`` of the user's own column
# names, so ``assert-schema (a:long, table:long)`` would lose the column
# literally called ``table`` and hash identically to ``(a:long)``, while
# ``LetFunction.body_span`` is a span whose field is not named ``span``, so
# source offsets would keep reaching the digest.
#
# To extend, add the field name here. It applies to every model that declares a
# field of that name, which is the intent: ``result_schema`` is meant to clear
# on ``Pipeline`` and on anything else that grows one.
#
# Spans carry character offsets, which a semantic hash must ignore. The rest is
# everything :class:`~kustology.ir.binder.SchemaAttacher` writes --
# ``result_type`` / ``result_type_inner`` (annotations), ``table`` (column
# provenance) and ``result_schema`` (the whole bound schema per pipeline). All
# four are inferred from the caller's schema rather than stated by the query,
# so leaving any of them in makes the same query text hash two ways depending
# on whether a schema was passed. ``QueryIR.semantic_hash`` computes and
# memoizes on first read, so it can run before or after ``SchemaAttacher``
# enriches the tree; clearing these fields keeps the digest the same either
# way, for the property and for a direct ``compute_semantic_hash`` call.
#
# ``hints`` is the one member of this set the binder does *not* write: it is
# source-derived, read straight off the query's ``hint.*`` named parameters. A
# hint tunes execution without changing the result -- ``join
# hint.strategy=shuffle`` and ``join`` return the same rows -- so two rules
# differing only in tuning must deduplicate to one. A future field belongs here
# only if the query returns identical rows without it.
#
# ``join_side`` is *not* stripped, which is why it exists as a field at all. It
# keeps ``table`` to a single job, the table the binder resolves (see
# ``SchemaAttacher._fill``), so the source-derived ``$left`` / ``$right`` side
# never rides in a binder-written field. Carried in ``table`` and hashed, the
# side would make the hash bind-dependent; carried there and stripped,
# ``$left.a == $left.b`` would collapse into ``$left.a == $right.b``, a
# different query. An unresolvable side is honestly ``None`` in ``table``,
# while the side itself lives in ``join_side`` and hashes on its own. Splitting
# the two apart is the standing remedy for lossy lowering -- see AGENTS.md.
#
# No field-stripping can hide bind state entirely: the builder's ``let``
# dispatch is bind-dependent by *shape*, so ``let A = OtherTable`` yields
# ``rhs_expr: ColumnRef`` unbound and ``rhs_pipeline: Pipeline(TableRef)`` once
# the binder proves ``OtherTable`` is a table (see
# ``IRBuilder._visit_let_statement``). That divergence is accepted; the
# alternative treats every bare ``NameReference`` as a table with no schema to
# prove it, trading an honest difference for a silently wrong answer.
_VOLATILE_FIELDS = frozenset({
    "span", "body_span", "result_type", "result_type_inner", "table",
    "result_schema", "hints",
})

# Cleared by the same pass on the same private copy, for a different reason,
# hence a separate set. Nothing here is bind state:
# :attr:`LetBinding.inner_tables` and :attr:`LetBinding.inner_time_exprs` are a
# **derived index**, written by the builder from the very subtree
# (``rhs_pipeline`` / ``rhs_function``) that is already in the digest, so
# excluding them cannot merge anything the tree still splits. Hashing the index
# instead breaks two ways, since an index copies a name and the copy
# desynchronizes from it.
#
# * ``inner_tables`` is a list of plain ``str``, which no node rename reaches,
#   so a body reading a tabular parameter records ``["T"]`` against ``["U"]``
#   however thoroughly the ``TableRef`` beside it is canonicalized, and two
#   alpha-equivalent functions split on the index alone. A tabular parameter
#   reference is indistinguishable from a table name there (see
#   :class:`~kustology.ir.query.LetBinding`).
# * ``inner_time_exprs`` holds the *same objects* as the right-hand side beside
#   it, so a rename walk would reach each one twice, correctly only for as long
#   as ``rhs_function`` stays declared before the index on
#   :class:`~kustology.ir.query.LetBinding`. Clearing the index first leaves no
#   second path to the node, so nothing hangs on declaration order.
#
# A future index field over query content belongs here. The test is whether the
# field's value is *recoverable* from what is already hashed.
_DERIVED_INDEX_FIELDS = frozenset({"inner_tables", "inner_time_exprs"})

_CLEARED_FIELDS = _VOLATILE_FIELDS | _DERIVED_INDEX_FIELDS

# :func:`_strip_unwritten_fields` below is the sibling mechanism, for a field
# whose *written* value must reach the digest while its *unwritten* default
# must not move a query that never used it. Clearing to the default is wrong
# there, since the default is the thing being hashed; omitting the key from the
# dump is wrong here, since these fields are never absent, only ever reset.

# ``span`` and ``body_span`` are required and have no default, so there is
# nothing to clear them *to*. A zero span keeps the copy a valid IR: Pydantic
# 2.13 will serialize ``None`` through a ``Span``-typed field, emitting
# ``null`` with no warning, but that payload fails to validate back, and the
# copy is a live IR that ``walk`` and ``model_dump`` both traverse. One
# instance is shared by every node on the copy, never observably -- the copy is
# dumped and discarded inside ``compute_semantic_hash``, and nothing mutates a
# ``Span`` in place.
_ZERO_SPAN = Span(text_start=0, width=0)


def _normalize_raw_text(text: str) -> str:
    r"""Fold each line break in ``raw_text``, indent included, to one space.

    Nothing else is touched. The operators the IR keeps as source text
    (``scan``, ``top-nested``, the ``graph-*`` family, and the ``Unknown*``
    fallbacks) hash that text directly, so without the fold the digest reads
    ``| top-nested 3 of a`` and ``|   top-nested\n3 of a`` as two queries. The
    rule stays narrow because two things that look like formatting in source
    text are data:

    * Interior spacing survives. A run of spaces can sit *inside a string
      literal*, where it is part of the value: ``"error  occurred"`` (two
      spaces) and ``"error occurred"`` are different predicates. Outside a
      literal ``IncludeTrivia.Minimal`` has already collapsed it, recording
      ``top-nested 3  of  a`` as ``top-nested 3 of a``. Newlines are safe
      because a KQL string literal cannot contain a raw one, so this fold never
      reaches inside a literal.
    * Comments survive. ``Minimal`` already drops every comment in and around
      the node, and ``//`` is the middle of every URL a detection rule matches
      on: a regex from ``//`` to end-of-line would truncate
      ``Url == "http://a"`` and ``Url == "http://b"`` to the same text.

    Both boundaries are pinned by tests. Widening this function to
    ``" ".join(text.split())`` fails the first; adding a comment strip fails
    the second.
    """
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _clear_volatile(root: BaseModel) -> None:
    """Clear every digest-excluded field on ``root`` and its descendants.

    In place: :data:`_VOLATILE_FIELDS` (bind state and offsets) and
    :data:`_DERIVED_INDEX_FIELDS` (values recoverable from the subtree that is
    hashed anyway). Rewrites node state, so run it on the hash's private deep
    copy.
    """
    for node in walk(root):
        fields = type(node).model_fields
        for name in _CLEARED_FIELDS & fields.keys():
            if name in ("span", "body_span"):
                object.__setattr__(node, name, _ZERO_SPAN)
            else:
                # Back to the declared default, so a cleared field is
                # indistinguishable from one the binder never touched. A field
                # with no default (none currently) clears to ``None``. A
                # *mutable* default -- ``hints`` is ``{}`` -- installs by
                # reference, so every node carrying that field shares one
                # object; safe for the reason ``_ZERO_SPAN`` is, and a future
                # step that edits one must copy it first.
                default = fields[name].default
                object.__setattr__(
                    node, name, None if default is PydanticUndefined else default,
                )
        if "raw_text" in fields:
            object.__setattr__(node, "raw_text", _normalize_raw_text(node.raw_text))


# Every model carrying a ``let``-bound name in a ``name`` field: the
# declaration, the source-position reference and the expression-position
# reference. Append to this tuple when a new one lands; nothing else changes,
# since ``_canonicalize_let_names`` renames by one isinstance check against the
# whole tuple.
#
# ``ColumnRef`` is not a member and must not become one: a real column named
# ``n`` is a different query from a ``let``-bound ``n``, and renaming one would
# collapse the two. A *function parameter* still renames through ``ColumnRef``
# / ``TableRef``, because the builder lowers a shadowed parameter reference to
# one of them, but only the scope-local parameter map
# (:data:`_PARAM_NAME_MODELS`) reaches it, inside the one body that bound the
# name.
#
# ``LetBinding`` is a member as the declaration, and only the scope walker
# below renames it, never the reference map -- see the docstring.
_LET_NAME_MODELS: tuple[type[BaseModel], ...] = (LetBinding, LetRef, LetValueRef)

# A call site's name: source position and expression position, the latter also
# reached by ``invoke``'s callee on a third builder path.
# ``let f = () { … }; f()`` writes the binding's own name at the call, so it is
# as much a local label as the declaration is.
#
# The gate on these is **narrower** than on the references above, because KQL
# keeps values and functions in separate namespaces: ``let abs = 5; T | extend
# y = abs(x)`` binds a value called ``abs`` and still calls the *built-in*
# ``abs``, and Microsoft's binder resolves both readings of ``abs(x) + abs``
# with no diagnostic at all. A name-only gate would rename that call to the
# binding's ``$let<i>`` and merge the query with the ``sqrt`` spelling of it,
# which computes something else. So only a ``let`` that bound a **function**
# (``rhs_function``) renames its call sites; a built-in, a stored server-side
# function, and a call whose name a scalar binding happens to share are left
# exactly as written. Both halves are pinned by
# ``let-scalar-shadowing-a-builtin-call`` and
# ``let-function-body-server-call`` in ``tests/ir/test_hash_battery.py``.
_CALL_SITE_NAME_MODELS: tuple[type[BaseModel], ...] = (FuncCallSource, FuncCall)

# The two models a *parameter* reference lowers to. Kept apart from the tuples
# above because the rename that reaches them is scoped differently: they carry
# ordinary column and table names nearly everywhere they appear, so they are
# rewritten only within the body of the declaration that bound the name, and
# only against that scope's own parameter map -- never against ``visible``.
_PARAM_NAME_MODELS: tuple[type[BaseModel], ...] = (ColumnRef, TableRef)


def _canonicalize_let_names(ir: QueryIR) -> None:
    """Rename every ``let`` binding to ``$let<i>`` and every function parameter to ``$param<i>``.

    Renaming follows scope-walk order and runs on the hash's deep copy only. A
    ``let`` name is a local label with no meaning outside the query, so
    ``let X = …; X | take 1`` and ``let Y = …; Y | take 1`` must hash alike.
    Numbering by position keeps the rest distinguishable, since reading from
    the first of two bindings and reading from the second are different
    queries. ``$`` cannot appear in a KQL identifier, so a canonical name can
    never collide with one the query chose.

    Declarations are renamed by **position** rather than by looking their old
    name up in a map, because names are not unique. KQL diagnoses a
    redeclaration (``KS201``), but the error-tolerant builder still emits both
    bindings, and ``compute_semantic_hash`` also accepts hand-built IR with no
    parser in the loop at all. A name-keyed map gives two same-named bindings
    one canonical name, and ``$letN`` stops meaning "the Nth declaration".

    References resolve against the bindings *visible where they are written*: a
    binding's own right-hand side sees only the bindings declared before it,
    and the main pipeline sees them all. That keeps a shadowed reference
    pointing at the binding it actually reads. A ``let``-declared function body
    opens a child scope seeded from what is visible at the declaration site, so
    a body reference to an outer binding renames through it, a ``let`` written
    inside the body extends the child scope only, and neither is visible
    outside the braces. A ``declare pattern`` arm's body is the same construct
    reached by a different route (a ``FunctionBody`` owned by a
    :class:`~kustology.ir.query.PatternMatch`) and opens a child scope the same
    way.

    A call site is a reference when the binding is a function, so
    ``let f = () { … }; f()`` renames through ``visible`` like any other
    reference, but only if that binding populated ``rhs_function``. See
    :data:`_CALL_SITE_NAME_MODELS` for why a visible ``let`` of the name is not
    sufficient. One residual is accepted: a ``let`` bound to a function by
    *alias* (``let g = f; g()``) records ``rhs_expr``, so its call sites keep
    the written name and two spellings of such a query split. A split costs a
    dedup consumer a duplicate, where the merge this narrowing prevents costs
    it a rule.

    Parameters are a second scope with a counter of their own, reached through
    different classes: the builder lowers a body reference under a parameter's
    shadow to a ``ColumnRef`` / ``TableRef`` (see
    :class:`~kustology.ir.query.LetFunctionParameter`), never to a
    ``LetValueRef`` / ``LetRef``. Each declaration's parameters are numbered
    ``$param<i>`` in declaration order, and a **scope-local** map is applied to
    those two classes within that body's subtree and nowhere else. ``visible``
    never carries a parameter. Numbering runs before the body's own ``let``s,
    so the two sequences do not interleave with each other's nesting.

    The two renames meet at the shadow edge without colliding: in
    ``let S = (w:int) { let w = 5; A | where x > w }`` the *declaration*
    renames as a ``let`` (``LetBinding`` → ``$let<i>``, through the child
    scope) while the *reference* renames as a parameter (``ColumnRef`` →
    ``$param<i>``, through the map), the builder having already committed that
    reference to the shadow reading. The two node classes are disjoint, so
    neither pass reaches the other's node and their order is not load-bearing.

    Applying the map textually draws two boundaries, both of them the reading
    the builder already committed to:

    * A body reference matching a parameter name is treated as the parameter
      even where KQL would resolve it to a row-scope column of the same name
      first -- the same text-only shadow rule, and the same accepted
      consequence, that :class:`~kustology.ir.expr.LetValueRef` documents for
      ``let`` names.
    * A ``declare query_parameters`` name is never renamed by either
      mechanism, and that exclusion is a *decision*: those names are the
      caller-facing API of a saved query, so renaming them would merge two
      queries with different call contracts (see
      :class:`~kustology.ir.query.QueryParametersStmt`). A ``declare
      pattern``'s own parameters stay verbatim too, for the reason
      :class:`~kustology.ir.query.PatternStmt` records: nothing binds them as
      names inside an arm's body, so this scope has nothing to apply to them.

    Both counters are **query-global**. Numbering each scope from zero would
    let a body's own first binding and its encloser's first binding both be
    ``$let0``, so a body reading its own ``z`` and a body reading the outer
    ``n`` would render identically. Parameters are the sharper case: nesting
    makes them cumulative by construction (the builder's ``_param_names``
    accumulates through nested bodies), and two nested ``$param0``s would be
    one name for two different parameters, both live at once.

    A single ``seen`` id-set spans the whole traversal, guarding against a node
    two fields both reach being renamed twice. No such path exists in the copy
    this runs on, since ``LetBinding.inner_time_exprs`` is cleared before this
    runs (:data:`_DERIVED_INDEX_FIELDS`), so the set is insurance against a
    future aliasing field.
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
    # suffices because a canonical name is unique across the query: once
    # ``visible`` has answered which binding a name reaches from here, scope is
    # resolved and only "what did that binding bind" is left. Shadowing follows
    # -- ``let f = () { … }; let f = 5;`` leaves ``visible["f"]`` pointing at
    # the scalar's canonical name, which is not in here.
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
            # child scope through the same helper, with no parameters of its
            # own to number. The arm's own selector (the matched values and the
            # path value) is written at the declaration site and resolves in
            # the parent, the way a function's parameter defaults do. The
            # pattern's declared parameters sit on the owning ``PatternStmt``
            # and stay verbatim: they name the arm's match slots, not values an
            # arm's body can read, so a map has nothing in here to rewrite.
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
        """Assign canonical names to one scope's ``let`` declarations in order, extending ``visible``."""
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
        """Walk one declaration's body as a child scope, applying the parameter map last.

        Number the parameters, then the body's own ``let``s, then the tail,
        then apply the parameter map over the whole body subtree. Both
        constructs that own a ``FunctionBody`` share this helper, so a body
        reached through a ``declare pattern`` arm is scoped exactly as one
        reached through a ``let``-declared function.

        Applying the parameter map *after* the recursive walk is what makes an
        inner scope win over an outer one: a nested function's own parameters
        are already ``$param<i>`` by the time this map is offered the subtree,
        and a canonical name matches no key here, since the keys are names the
        query wrote and ``$`` is not a legal KQL identifier character. A body
        reference to an *enclosing* function's parameter is left untouched by
        the inner map for the same reason, and picked up by this one.
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
        """Assign canonical names to one declaration's parameters, returning the body-local map.

        The canonical names are handed out first and the map is built from the
        names as *written*, before any declaration is overwritten. Otherwise
        the second parameter of ``(a:int, b:int)`` would map from the name the
        first one had been given.

        Declarations are renamed by position for the reason
        :func:`canon_scope`'s are: a duplicate name is a query KQL rejects and
        the error-tolerant builder still emits, and a name-keyed rename would
        stop ``$param<i>`` meaning "the ith parameter". References follow the
        map, so the last declaration of a duplicated name wins, the rule a
        shadowing redeclaration follows everywhere else.
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
            # ``database("d").w`` names a real table and renaming it would
            # merge two queries that read different ones. ``ColumnRef`` needs
            # no such guard, its ``table`` being binder-written and cleared
            # before this runs.
            if isinstance(sub, TableRef) and (sub.database or sub.cluster):
                continue
            object.__setattr__(sub, "name", param_map[sub.name])

    visible: dict[str, str] = {}
    canon_scope(ir.let_bindings, visible)
    # Every tabular statement, not only the first: a ``let`` declared once is
    # in scope for all of them, so a reference written after the second
    # semicolon needs renaming too. The other statement kinds are in the same
    # position (``restrict access to (V)`` over a ``let``-bound view reads the
    # binding), and they come last so a pattern arm's own bindings number
    # deterministically.
    for node in (ir.main_pipeline, *ir.additional_pipelines, *ir.statements):
        rename(node, visible)


def _sort_commutative(root: BaseModel) -> None:
    """Sort the operands of every commutative node into a canonical order.

    ``and`` / ``or`` operands and the value list of a set-membership test are
    the only places in the IR where source order carries no meaning. Sorting
    them makes ``a and b`` and ``b and a`` one digest. Nothing else is touched:
    a sort over ``BinOp`` operands would merge ``a < b`` with ``b < a``, which
    are opposite predicates.

    Two ordering dependencies, both load-bearing:

    * The key is the child's dumped JSON, so this must run *after*
      ``_clear_volatile``. Otherwise the spans inside each operand order the
      list by where it happened to be written.
    * Children must be sorted before their parents, since a parent's key is
      computed from a child's dump. ``walk`` is pre-order, so iterating it
      reversed visits every descendant before its ancestor. Bottom-up order
      bites when a sibling's key falls *between* the two spellings of an
      operand: ``(b or a) and (a or z)`` and ``(a or z) and (a or b)`` are the
      same predicate, but top-down the first ``And`` keys on ``(b or a)`` and
      the second on ``(a or b)``, which sort to opposite sides of ``(a or z)``,
      leaving the two ``And``s in opposite orders once the ``Or``s are fixed.

    One caveat on that second point: ``walk`` yields a shared object only at
    the *first* path that reaches it, so an index field declared before the
    real parent would position the aliased node above that parent and sort it
    after. :func:`_clear_volatile` empties the IR's only aliasing field,
    ``LetBinding.inner_time_exprs``, before this runs
    (:data:`_DERIVED_INDEX_FIELDS`). A new index field over
    ``And``/``Or``/``SetMembership`` that reached the digest would need a
    genuinely post-order traversal here, or the same exclusion.

    ``walk`` iterates ``model_fields``, so this reaches a ``let`` function's
    body as readily as a top-level pipeline, with no case here for the body at
    all. Runs on the hash's deep copy only; the public
    ``normalize_expressions`` leaves the query's own order alone.
    """
    for node in reversed(list(walk(root))):
        if isinstance(node, (And, Or)):
            node.operands = sorted(node.operands, key=_operand_sort_key)
        elif isinstance(node, SetMembership):
            node.values = sorted(node.values, key=_operand_sort_key)


def _operand_sort_key(child: BaseModel) -> str:
    """Key an operand by its own canonical JSON dump, a total order.

    ``sort_keys=True`` makes it independent of field declaration order.
    Dumping the whole subtree rather than a rendered form means two operands
    tie only when every field matches, and then their order genuinely does not
    matter.
    """
    return json.dumps(child.model_dump(mode="json"), sort_keys=True)


# Operator ``kind`` -> the fields whose *unwritten* defaults must dump as
# though the field were never declared, mapped to those exact defaults. Add a
# row here for the next modeled modifier; :func:`_strip_unwritten_fields` reads
# the whole table in one pass.
_UNWRITTEN_DEFAULTS: dict[str, dict[str, Any]] = {
    "evaluate": {"declared_schema": None, "declared_schema_star": False},
    "mv_apply": {"to_typeof": None, "row_limit": None, "item_index": None},
    "parse_kv": {"properties": []},
    "getschema": {"output_kind": None},
    "consume": {"decodeblocks": None},
}


def _at_default(actual: Any, default: Any) -> bool:
    """Test one field against its unwritten default, by identity or equality.

    A scalar default is compared by identity (``is``): evaluate's are
    ``None``/``False``, and under equality ``0 == False`` would wrongly match a
    future int-typed field whose real default is ``0``. A ``list``/``dict``
    default (:class:`~kustology.ir.query.ParseKvOp`'s ``properties``, for one)
    is compared by ``==`` instead, because a freshly dumped empty list is never
    the *same object* as the literal ``[]`` in :data:`_UNWRITTEN_DEFAULTS`, so
    ``is`` could never match and the field would never strip. The
    ``0 == False`` risk is a cross-type collision between scalars, and no
    ``list`` or ``dict`` equals a bool, an int, or ``None``.
    """
    if isinstance(default, (list, dict)):
        return actual == default
    return actual is default


def _strip_unwritten_fields(payload: Any) -> None:
    """Delete an operator dict's modifier keys when every one is unwritten.

    Operates in place on the *dumped* JSON structure, in a single pass that
    dispatches on each dict's ``kind`` against :data:`_UNWRITTEN_DEFAULTS`.
    This is the sibling of :func:`_clear_volatile` above, for the opposite kind
    of field: a plain, non-volatile one whose *written* value must reach the
    digest, so clearing it to a canonical default would hide real differences,
    while its *unwritten* default must not move the digest of a query that
    never uses it -- as :class:`~kustology.ir.query.EvaluateOp`'s
    ``declared_schema``/``declared_schema_star`` must not. A Pydantic field
    cannot be conditionally *absent* from one ``model_dump`` call, since the
    key is always present at its default, so merely declaring such a field
    would move the digest of every query using that operator, written or not,
    when only the written case closes a real collision. Operating after the
    dump, on the plain dict/list tree, makes the omission conditional: an
    operator dict still at all its defaults dumps exactly as if the fields were
    never declared.

    Every field in a row must be at its default for that dict's keys to be
    dropped, so a partially-written modifier keeps every key and the digest
    still reflects what was actually written. A dict carries one ``kind``, so
    at most one row of the table can apply to it.

    The gate also requires ``field in value``, beyond
    ``value.get(field) is default``. Some of the field sets stripped here are
    ``None`` all the way down (mv-apply's, for one), and a dict that lacks the
    keys entirely reads the same way through ``.get``, so without the presence
    check a *foreign* dict carrying the same ``kind`` tag would match the gate
    and then crash on ``del``. Every real operator dump carries every declared
    key, so requiring them costs a genuine dict nothing.
    """
    stack: list[Any] = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            defaults = _UNWRITTEN_DEFAULTS.get(value.get("kind"))
            if defaults is not None and all(
                field in value and _at_default(value[field], default)
                for field, default in defaults.items()
            ):
                for field in defaults:
                    del value[field]
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _canonicalize(node: BaseModel, *, spans: dict[int, Span | None] | None = None) -> BaseModel:
    """Return the private canonical copy every digest is computed from.

    Deep-copies ``node``, empties ``diagnostics`` (:class:`QueryIR` roots
    only), merges consecutive filters (:class:`Pipeline` and :class:`QueryIR`
    roots only), normalizes expressions, clears volatile fields,
    canonicalizes ``let`` names (:class:`QueryIR` roots only), and sorts
    commutative operands, in that order. When ``spans`` is given,
    records ``spans[id(n)] = span_of(n)`` for every non-``Span`` model in the
    copy, taken after normalization and before volatile fields are cleared —
    the last point at which the copy's spans still reflect source offsets.
    """
    canonical = node.model_copy(deep=True)
    if isinstance(canonical, QueryIR):
        # Digest-inert, since ``_payload``'s ``QueryIR`` branch never reads
        # this field, but every other consumer of the copy (``walk``,
        # ``similarity._children``) would see the ``Diagnostic`` nodes and
        # split a bound query's subtree bag from its unbound twin at a low
        # ``min_size``, even though their whole-query digests match.
        canonical.diagnostics = []
    if isinstance(canonical, (Pipeline, QueryIR)):
        merge_consecutive_filters(canonical)
    # Rebind: a bare ``Not(Not(X))`` root is *replaced* by ``X``, and there is
    # no parent field for the replacement to be installed into.
    canonical = normalize_expressions(canonical)
    if spans is not None:
        for n in walk(canonical):
            if not isinstance(n, Span):
                spans[id(n)] = span_of(n)
    _clear_volatile(canonical)
    if isinstance(canonical, QueryIR):
        # Must run before ``_sort_commutative``: renaming changes the JSON sort
        # key of any operand containing a ``LetValueRef`` / ``LetRef``, so
        # sorted-then-renamed, two spellings of the same query order
        # differently and split. The rename recurses into every ``LetFunction``
        # scope, so a function body's references are canonical by then too.
        _canonicalize_let_names(canonical)
    # After ``_clear_volatile``, so the sort key cannot see a span offset.
    _sort_commutative(canonical)
    return canonical


def _payload(canonical: BaseModel) -> dict[str, Any]:
    """Return ``canonical``'s digest payload, with unwritten fields stripped.

    A :class:`QueryIR` dumps as a hand-built four-key dict rather than
    ``model_dump()``; every other root dumps whole.
    """
    payload: Any
    if isinstance(canonical, QueryIR):
        # Named field by field rather than dumping the whole model, so
        # ``raw_text``, ``semantic_hash`` and ``schema_attached`` stay out of
        # the digest. The cost: a new ``QueryIR`` field is invisible here until
        # it is added, so the builder can fill one faithfully while it hashes
        # to nothing at all and two queries differing only there become one
        # digest. Add every field that carries query meaning.
        payload = {
            "let_bindings": [lb.model_dump(mode="json") for lb in canonical.let_bindings],
            # Present even when empty, the way ``additional_pipelines`` is: a
            # key whose presence depended on its own contents would make the
            # payload's shape data. ``_strip_unwritten_fields`` covers an
            # operator *field* whose unwritten default must dump as though
            # undeclared; a top-level payload key is part of the payload's
            # fixed shape and gets no such treatment.
            "statements": [s.model_dump(mode="json") for s in canonical.statements],
            "main_pipeline": canonical.main_pipeline.model_dump(mode="json"),
            "additional_pipelines": [
                p.model_dump(mode="json") for p in canonical.additional_pipelines
            ],
        }
    else:
        payload = canonical.model_dump(mode="json")
    # A query writing no modeled modifier digests as the bare operator; see
    # :func:`_strip_unwritten_fields`.
    _strip_unwritten_fields(payload)
    return payload


def _digest(payload: dict[str, Any]) -> str:
    """Return the scheme-tagged SHA-256 of ``payload``."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"{SEMANTIC_HASH_SCHEME}:{digest}"


def compute_semantic_hash(node: BaseModel) -> str:
    """Return the scheme-tagged SHA-256 of the IR's canonical shape.

    Accepts any IR ``BaseModel`` subtree — a full :class:`QueryIR`, a
    standalone :class:`Pipeline`, an :class:`Expr` subtree — and returns a
    scheme-tagged hash like ``kustology-sem-v2:<64 hex chars>``.

    Two subtrees with the same semantic content collide:

    * Whitespace and formatting differences, comments included (same AST →
      same IR, plus ``raw_text`` and every span cleared before the dump)
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
      (``$let0``, …), inside a function body or a ``declare pattern`` arm as
      well as at the top level. A **call site** renames too when the binding is
      a function, so ``let f = () { … }; f()`` and ``let g = () { … }; g()``
      are one digest. Any other call stays as written: a built-in, a
      server-side function, one sharing a *scalar* binding's name, which KQL
      resolves in a separate namespace, or one whose binding only *aliases* a
      function (``let g = f; g()``)
    * ``let f = (w:int) { T | where a > w }`` vs the same function written
      ``(z:int) { T | where a > z }`` — a *parameter* name is a local label
      too, replaced by its position in the signature (``$param0``, …) along
      with every reference to it in that body. A ``declare pattern``'s own
      parameters and a ``declare query_parameters`` name are never renamed —
      see :class:`~kustology.ir.query.PatternStmt` and
      :class:`~kustology.ir.query.QueryParametersStmt`
    * ``| where A | where B`` vs ``| where B | where A``, and either against
      ``| where B and A`` — the merge and the sort *compose*, so a run of
      filters becomes one ``And`` whose operands are then sorted, and its order
      stops mattering as well as its grouping. This falls out of the two rules
      above.

    Two subtrees with different literal values, operators, identifiers, or
    operator sequences do *not* collide, and neither do the near-misses of the
    rules above. Only genuinely commutative operands are sorted, so ``A < B``
    and ``B < A`` stay apart as the opposite predicates they are. Both renames
    are positional, so ``$let0`` and ``$let1`` keep two bindings apart, and
    ``$param0`` and ``$param1`` two parameters. Only a run of *consecutive*
    filters merges, so ``| where A | take 5`` and ``| take 5 | where A``, which
    return different rows, still hash apart.

    Three rules fix the bounds of all that:

    * Sorting reaches ``And.operands``, ``Or.operands`` and
      ``SetMembership.values`` and nothing else, keyed on each operand's own
      dumped JSON (:func:`_sort_commutative`). ``Expr.canonical_form`` sorts
      the same three places by *rendered string*, a different key over the
      same set, and ``normalize_expressions`` sorts nothing at all.
    * Renaming runs over two query-global sequences
      (:func:`_canonicalize_let_names`). ``$let<i>`` takes top-level
      declarations first, then a function body's or a pattern arm's own as
      that body is reached, and every reference, call sites included, takes
      whatever was visible where it was written.
    * Datetime literals are UTC. The builder Kind-normalizes every
      ``datetime`` literal before recording ``value`` and ``ticks``
      (``_builder_helpers.literal_value_and_ticks``), and numeric and timespan
      literals render under ``InvariantCulture``, so the same query digests
      identically in Tokyo and New York and under ``de-DE``.

    Everything the binder *writes* is stripped before the dump —
    ``result_type``, ``result_type_inner``, ``table``, ``result_schema`` and
    ``hints``, plus ``span`` / ``body_span`` (:data:`_VOLATILE_FIELDS`) — so
    passing a schema does not move the digest. One divergence survives that,
    being a difference of *shape* rather than of a field's value: a ``let``
    whose right-hand side aliases a table records ``rhs_expr`` unbound and
    ``rhs_pipeline`` once the binder has proved the name is a table, and no
    field-clearing can make two different nodes into one. The tail of a
    ``let``-function body is the same grammatical position read by the same
    predicate (``builder._is_tabular_rhs``), so ``let f = () { OtherTable }``
    diverges the same way, on ``body_expr`` vs ``body_pipeline``. Queries that
    alias no bare table name in either position are unaffected.

    Two more fields are excluded for an unrelated reason.
    :attr:`~kustology.ir.query.LetBinding.inner_tables` and
    :attr:`~kustology.ir.query.LetBinding.inner_time_exprs` index the
    right-hand side sitting beside them (:data:`_DERIVED_INDEX_FIELDS`), so
    hashing them adds nothing the tree does not already carry and costs the
    collisions those copied names desynchronize on. They stay on your own IR,
    populated as always; only the digest ignores them.

    Equal digests are not a proof of equivalence. Each entry below is a
    decision:

    * Typed nulls and obfuscated strings, in literals. See
      :class:`~kustology.ir.expr.LiteralExpr` for why neither is a difference
      in what a query returns.
    * A ``let``-declared function's call sites. ``let f = () { … }; f(); f()``
      records the body once, on the declaration, and leaves each ``f()`` a
      call, so it does not hash as the query that inlines the body twice. That
      is the right answer for dedup, since the two *are* the same query, but a
      caller counting a table's occurrences in the digest gets the number of
      declarations.
    * A local name that shadows a real column. Both renames above decide what
      is local from the query text, where KQL resolves an unqualified name to
      a row-scope column first, so ``let Count = 5; T | where Count > 1``
      against a ``T`` that really has ``Count`` merges with the same query
      written ``let Other = 5; … Other > 1``. A function parameter shadowing a
      column behaves the same way. Deciding by symbol needs a bound parse and
      would put the digest back under bind state — see
      :class:`~kustology.ir.expr.LetValueRef` for the trade.

    A dedup consumer that must not merge across any of these has to compare
    more than the hash. Each entry is pinned, though not all in one place.
    ``KNOWN_MERGES`` in ``examples/semantic_hash_demo.py`` covers the
    literal-level merges, hashing every row it holds when it runs and raising
    if one stops behaving as filed. Recording the body once at the declaration
    is pinned by ``test_a_call_site_is_still_not_expanded`` in
    ``tests/ir/test_let_bindings.py``, and the shadow merge by
    ``test_the_shadowing_case_collapses_two_queries_onto_one_hash`` in
    ``tests/ir/test_let_value_ref.py``. Neither is in ``KNOWN_MERGES``, so the
    demo's table is not the whole list.

    The hash operates on a deep copy of ``node``, so it does not mutate the
    input, and the result reflects the IR shape at call time. It goes stale if
    you mutate ``node`` afterwards; call again for the current value.
    """
    return _digest(_payload(_canonicalize(node)))
