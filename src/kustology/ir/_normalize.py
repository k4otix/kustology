# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Post-build IR normalization and canonical-form generation.

Pure functions on already-built IR nodes — no .NET AST, no GlobalState. The
builder invokes these during its final pass over each expression to ensure
equivalent KQL queries collapse to the same IR shape and that operands of
commutative operators have a stable string form for diffing.
"""

from __future__ import annotations

from typing import Any

from .expr import (
    And,
    Between,
    BinOp,
    BracketedExpr,
    CaseExpr,
    ColumnRef,
    CompoundNamedExpr,
    ElementExpr,
    Exists,
    ExternalDataExpr,
    FuncCall,
    LetValueRef,
    LiteralExpr,
    NamedExpr,
    Not,
    Or,
    PathExpr,
    RegexMatch,
    SetMembership,
    StarExpr,
    SubqueryExpr,
    ToScalarExpr,
    TypedNameDecl,
    UnaryOp,
)


def _case_fold_side(e: Any) -> bool:
    """True iff ``e`` is a single-argument ``tolower(...)``/``toupper(...)`` call."""
    return isinstance(e, FuncCall) and e.name.lower() in ("tolower", "toupper") and len(e.args) == 1


def _literal_matches_fold(lit: Any, fn: str) -> bool:
    """True iff ``lit`` is a string literal already in the case ``fn`` folds to.

    ``tolower(X) == "Y"`` (capital Y) is always false -- ``tolower`` never
    returns anything but lowercase -- while ``X =~ "Y"`` is a case-insensitive
    match that is often true, so the rewrite is only sound when the literal is
    already lowercase (or uppercase, for ``toupper``). A non-string literal,
    or an operand that is not a literal at all, never matches.
    """
    return (isinstance(lit, LiteralExpr) and lit.literal_kind == "string" and isinstance(lit.value, str)
            and lit.value == (lit.value.lower() if fn == "tolower" else lit.value.upper()))


def normalize_in_place(expr: Any) -> Any:
    """Apply semantic-preserving rewrites so equivalent KQL produces the same shape.

    * ``tolower(X) == "y"`` → ``X =~ "y"`` when ``"y"`` is already lowercase
      (case-insensitive equality); symmetrically ``toupper(X) == "Y"`` →
      ``X =~ "Y"`` when ``"Y"`` is already uppercase. The literal may be on
      either side -- ``"y" == tolower(X)`` rewrites the same way, with the
      unwrapped operand landing on the left and the literal on the right, so
      it collapses to the same canonical form/hash as ``tolower(X) == "y"``.
      A literal that does not already match the fold (e.g.
      ``tolower(X) == "Y"``) is left alone -- that predicate is always false,
      while ``X =~ "Y"`` is not -- and so is a comparison against anything
      that is not a literal at all (``tolower(X) == Col``), since there is no
      fixed case to know the rewrite is sound for.
    * ``tolower(X) != "y"`` → ``X !~ "y"`` (and the ``toupper``/``!~`` mirror),
      under the same case-matching condition.
    * Flatten nested ``And`` / ``Or`` operands into a single list.
    * ``!!X`` → ``X``.
    """
    if isinstance(expr, BinOp) and expr.op in ("==", "!="):
        for fold_side, other_side in (("left", "right"), ("right", "left")):
            fold, other = getattr(expr, fold_side), getattr(expr, other_side)
            if _case_fold_side(fold) and _literal_matches_fold(other, fold.name.lower()):
                expr.op = "=~" if expr.op == "==" else "!~"
                expr.case_sensitive = False
                # Always land the unwrapped operand on the left and the
                # literal on the right, regardless of which side the source
                # wrote them on -- ``"y" == tolower(X)`` and
                # ``tolower(X) == "y"`` are the same predicate (equality is
                # symmetric) and must produce the same canonical form/hash.
                expr.left, expr.right = fold.args[0], other
                break
    elif isinstance(expr, And):
        flat: list = []
        for o in expr.operands:
            if isinstance(o, And):
                flat.extend(o.operands)
            else:
                flat.append(o)
        expr.operands = flat
    elif isinstance(expr, Or):
        flat = []
        for o in expr.operands:
            if isinstance(o, Or):
                flat.extend(o.operands)
            else:
                flat.append(o)
        expr.operands = flat
    elif isinstance(expr, Not) and isinstance(expr.operand, Not):
        return expr.operand.operand
    return expr


# KQL operator precedence, loosest to tightest. The number is only ever
# compared against another number from this table, so the absolute values
# mean nothing -- only the ordering does.
_PREC_OR = 1
_PREC_AND = 2
# Every comparison and every string operator (``==``, ``has``, ``!in~``,
# ``:``) sits at one level: KQL does not let two of them chain without
# parentheses, so their relative order is unobservable.
_PREC_COMPARISON = 3
_PREC_UNARY = 6
_PREC_ARITHMETIC = {"+": 4, "-": 4, "*": 5, "/": 5, "%": 5}


def _kql_string(value: str) -> str:
    """Render ``value`` as a KQL double-quoted string literal.

    Escaping is the whole point. ``f("a\\", \\"b")`` is a call with **one**
    argument whose value contains quotes and a comma; rendered raw it came
    out as ``f("a", "b")``, a call with two arguments -- a description of a
    tree that does not exist. Backslash goes first, or it would re-escape
    the backslashes the other rules just introduced.

    ``\\r`` is escaped alongside ``\\n`` for the same reason: a raw control
    character inside the quotes makes the rendering unreadable and, for a
    consumer feeding it back to a parser, unparseable.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _kql_literal(value: Any, literal_kind: str) -> str:
    """Render a literal's value the way KQL spells it.

    ``bool`` is checked before anything else because ``True`` is a Python
    ``int`` subclass, and before ``literal_kind`` because the kind tells us
    what the parser called it, not how to write it down. KQL's spellings are
    ``true`` / ``false`` / ``null``; ``str()`` produced Python's, so a
    canonical form of ``x == True`` named no value KQL has.

    Non-string kinds that happen to hold a ``str`` (``datetime``,
    ``timespan``, ``guid``, ``dynamic``) stay unquoted: their KQL spelling is
    a bare token or a constructor call, not a quoted string.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if literal_kind == "string" and isinstance(value, str):
        return _kql_string(value)
    return str(value)


def canonical(expr: Any) -> str:
    """Stable, commutative-aware string representation for diffing.

    Parenthesized **by precedence**, not by what the source wrote. The parser
    discards redundant parentheses before the builder ever sees them -- ``(a
    and b) or c`` and ``a and b or c`` build byte-identical IR -- so
    reproducing the source's brackets is not an option even in principle, and
    ``BracketedExpr`` stays dropped. What the renderer owes the reader is that
    the string it emits parses back to the tree it came from: ``a and (b or
    c)`` keeps its parentheses because ``or`` binds looser than ``and``, and
    ``x - (y - z)`` keeps them because arithmetic is left-associative, so a
    right operand of equal precedence can only have got there by being
    bracketed.

    This is a *display and diffing* form, not the hash's key.
    ``semantic_hash`` digests the model dump, and ``canonical_form`` is a
    property rather than a field, so nothing here can move a digest.
    """

    def _wrap(text: str, prec: int, parent_prec: int, parens_on_equal: bool) -> str:
        """Parenthesize ``text`` when its operator binds looser than the one
        it sits inside -- or exactly as tight, in the right operand's
        position, where a left-associative grammar could not have produced it
        without brackets."""
        if prec < parent_prec or (prec == parent_prec and parens_on_equal):
            return f"({text})"
        return text

    def _render(e: Any, parent_prec: int = 0, parens_on_equal: bool = False) -> str:
        # Precedence-bearing shapes -- ``BinOp``, ``And``, ``Or``,
        # ``UnaryOp`` -- compute their text and hand it to ``_wrap``. Every
        # other shape is an atom: a leaf, a call, or something that renders
        # with delimiters of its own (``not(...)``, ``case(...)``,
        # ``X between (a .. b)``). An atom returns directly, is never
        # parenthesized, never forces its parent to parenthesize it, and
        # renders its children at ``parent_prec`` 0 -- its own delimiters
        # already do the grouping.
        if isinstance(e, LiteralExpr):
            return _kql_literal(e.value, e.literal_kind)
        if isinstance(e, ColumnRef):
            return f"{e.table}.{e.name}" if e.table else e.name
        if isinstance(e, LetValueRef):
            # The name as the query wrote it. A ``let``-bound scalar reads
            # like a column at the use site and that is the faithful
            # rendering of the source; the two are told apart by node type,
            # and by the ``kind`` discriminator in the dump the hash actually
            # digests.
            return e.name
        if isinstance(e, TypedNameDecl):
            # ``name:type`` — the KQL spelling. Rendering the bare name would
            # make a typed capture indistinguishable from an untyped one,
            # which is the collision the node exists to close.
            return f"{e.name}:{e.declared_type}"
        if isinstance(e, BinOp):
            prec = _PREC_ARITHMETIC.get(e.op, _PREC_COMPARISON)
            # The right operand always re-brackets at equal precedence; the
            # left never does. That asymmetry is left-associativity, and it
            # belongs to the *child's* position rather than to the parent
            # operator: a right operand of equal precedence cannot have come
            # from an unbracketed parse, because an unbracketed chain nests
            # left. This rule replaced one that asked whether the parent was
            # ``-``, ``/`` or ``%`` -- the right observation attached to the
            # wrong operator, which left ``x * (y / z)`` rendering as
            # ``x * y / z``. Under integer division those are different
            # numbers: ``2 * (7 / 2)`` is 6 and ``2 * 7 / 2`` is 7.
            text = (
                f"{_render(e.left, prec)} {e.op} {_render(e.right, prec, True)}"
            )
            return _wrap(text, prec, parent_prec, parens_on_equal)
        if isinstance(e, And):
            # Sorted by each operand's *unparenthesized* rendering, so the
            # order does not depend on whether a nested operand happened to
            # need brackets -- a leading "(" would otherwise sort ahead of
            # every letter and reorder the chain for that reason alone.
            #
            # That costs a second render per operand, and the cost compounds
            # with the *alternating* and/or depth, since a chain of the same
            # connective is already flattened into one node by
            # ``normalize_in_place``. Real queries nest two or three deep;
            # 64k renders over the corpus measure at 0.4s.
            ordered = sorted(e.operands, key=_render)
            return _wrap(
                " and ".join(_render(o, _PREC_AND) for o in ordered),
                _PREC_AND, parent_prec, parens_on_equal,
            )
        if isinstance(e, Or):
            ordered = sorted(e.operands, key=_render)
            return _wrap(
                " or ".join(_render(o, _PREC_OR) for o in ordered),
                _PREC_OR, parent_prec, parens_on_equal,
            )
        if isinstance(e, UnaryOp):
            return _wrap(
                f"{e.op}{_render(e.operand, _PREC_UNARY)}",
                _PREC_UNARY, parent_prec, parens_on_equal,
            )
        if isinstance(e, Not):
            # Renders as a call, so its own parentheses do the grouping.
            return f"not({_render(e.operand)})"
        if isinstance(e, FuncCall):
            return f"{e.name}({', '.join(_render(a) for a in e.args)})"
        if isinstance(e, SetMembership):
            # Render the recorded operator. Rebuilding it from polarity plus
            # case_sensitive could only ever emit one of four strings, so
            # has_any and has_all both came out as `in~` -- a different
            # predicate. Same reason BinOp above renders `e.op` verbatim.
            vals = ", ".join(sorted(_render(v) for v in e.values))
            return f"{_render(e.column, _PREC_COMPARISON)} {e.op} ({vals})"
        if isinstance(e, Between):
            op = "between" if e.polarity == "inclusion" else "!between"
            return (
                f"{_render(e.target, _PREC_COMPARISON)} {op} "
                f"({_render(e.low)} .. {_render(e.high)})"
            )
        if isinstance(e, CaseExpr):
            branches = ", ".join(
                f"{_render(p)} => {_render(v)}" for p, v in e.branches
            )
            default = _render(e.default) if e.default is not None else "_"
            return f"case({branches} | else {default})"
        if isinstance(e, Exists):
            # `exists(...)` is not KQL -- name the function that produced it.
            # ``op`` already spells the negation, so ``polarity`` is not
            # rendered on top of it.
            return f"{e.op}({_render(e.target)})"
        if isinstance(e, RegexMatch):
            # The pattern through ``_kql_string`` like any other string
            # literal: a regex is where backslashes actually live, and
            # ``\\d+`` rendered raw inside quotes is the ambiguity this
            # function exists to remove.
            return f"{_render(e.target, _PREC_COMPARISON)} matches regex {_kql_string(e.pattern)}"
        if isinstance(e, PathExpr):
            return f"{_render(e.expression)}.{_render(e.selector)}"
        if isinstance(e, ElementExpr):
            return f"{_render(e.expression)}[{_render(e.selector)}]"
        if isinstance(e, BracketedExpr):
            # Parentheses carry no semantics once the tree is built, so they
            # are dropped rather than rendered -- `(X) > 1` and `X > 1` are
            # the same predicate and must produce the same string. The
            # precedence table above puts back the ones that *do* carry
            # grouping, which is a different question from what the source
            # typed.
            return _render(e.expression, parent_prec, parens_on_equal)
        if isinstance(e, NamedExpr):
            return f"{e.name} = {_render(e.expression)}"
        if isinstance(e, CompoundNamedExpr):
            return f"({', '.join(e.names)}) = {_render(e.expression)}"
        if isinstance(e, StarExpr):
            return "*"
        if isinstance(e, ExternalDataExpr):
            cols = ", ".join(f"{n}:{ty}" for n, ty in e.columns)
            # Every URI, in source order. Rendering only the first collapsed
            # a two-URI feed onto a one-URI feed -- the same loss the
            # singular ``uri`` field used to bake into the model.
            return f"externaldata({cols})[{', '.join(e.uris)}]"
        # Pipeline-bearing expressions. The inner pipeline is elided rather
        # than rendered: canonical() is a pure Expr function and Pipeline is
        # modeled in ir.query, so recursing would invert the dependency. The
        # wrapper is still named, which is what distinguishes these from each
        # other and from every other shape -- before, all three rendered as a
        # bare "?".
        if isinstance(e, ToScalarExpr):
            return f"toscalar({_pipeline_head(e.pipeline)} | ...)"
        if isinstance(e, SubqueryExpr):
            return f"({_pipeline_head(e.pipeline)} | ...)"
        # UnknownExpr and anything a future release adds. UnknownExpr carries
        # the source text, which is the most faithful thing available for a
        # shape the builder could not model.
        return getattr(e, "raw_text", "?").strip() if hasattr(e, "raw_text") else "?"

    return _render(expr)


def _pipeline_head(pipeline: Any) -> str:
    """The name a sub-pipeline reads from, for canonical rendering."""
    source = getattr(pipeline, "source", None)
    return getattr(source, "name", None) or "..."
