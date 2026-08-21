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
    if isinstance(expr, And):
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


def canonical(expr: Any) -> str:
    """Stable, commutative-aware string representation for diffing."""
    if isinstance(expr, LiteralExpr):
        if expr.literal_kind == "string":
            return f'"{expr.value}"'
        return str(expr.value)
    if isinstance(expr, ColumnRef):
        return f"{expr.table}.{expr.name}" if expr.table else expr.name
    if isinstance(expr, BinOp):
        return f"{canonical(expr.left)} {expr.op} {canonical(expr.right)}"
    if isinstance(expr, And):
        ops = [canonical(o) for o in expr.operands]
        return " and ".join(sorted(ops))
    if isinstance(expr, Or):
        ops = [canonical(o) for o in expr.operands]
        return " or ".join(sorted(ops))
    if isinstance(expr, Not):
        return f"not({canonical(expr.operand)})"
    if isinstance(expr, FuncCall):
        args = ", ".join(canonical(a) for a in expr.args)
        return f"{expr.name}({args})"
    if isinstance(expr, SetMembership):
        # Render the recorded operator. Rebuilding it from polarity plus
        # case_sensitive could only ever emit one of four strings, so
        # has_any and has_all both came out as `in~` -- a different
        # predicate. Same reason BinOp above renders `expr.op` verbatim.
        vals = ", ".join(sorted(canonical(v) for v in expr.values))
        return f"{canonical(expr.column)} {expr.op} ({vals})"
    if isinstance(expr, Between):
        op = "between" if expr.polarity == "inclusion" else "!between"
        return (
            f"{canonical(expr.target)} {op} "
            f"({canonical(expr.low)} .. {canonical(expr.high)})"
        )
    if isinstance(expr, CaseExpr):
        branches = ", ".join(
            f"{canonical(p)} => {canonical(v)}" for p, v in expr.branches
        )
        default = canonical(expr.default) if expr.default is not None else "_"
        return f"case({branches} | else {default})"
    if isinstance(expr, Exists):
        # `exists(...)` is not KQL -- name the function that produced it.
        return f"{expr.op}({canonical(expr.target)})"
    if isinstance(expr, RegexMatch):
        return f"{canonical(expr.target)} matches regex \"{expr.pattern}\""
    if isinstance(expr, UnaryOp):
        return f"{expr.op}{canonical(expr.operand)}"
    if isinstance(expr, PathExpr):
        return f"{canonical(expr.expression)}.{canonical(expr.selector)}"
    if isinstance(expr, ElementExpr):
        return f"{canonical(expr.expression)}[{canonical(expr.selector)}]"
    if isinstance(expr, BracketedExpr):
        # Parentheses carry no semantics once the tree is built, so they are
        # dropped rather than rendered -- `(X) > 1` and `X > 1` are the same
        # predicate and must produce the same string.
        return canonical(expr.expression)
    if isinstance(expr, NamedExpr):
        return f"{expr.name} = {canonical(expr.expression)}"
    if isinstance(expr, CompoundNamedExpr):
        return f"({', '.join(expr.names)}) = {canonical(expr.expression)}"
    if isinstance(expr, StarExpr):
        return "*"
    if isinstance(expr, ExternalDataExpr):
        cols = ", ".join(f"{n}:{ty}" for n, ty in expr.columns)
        return f"externaldata({cols})[{expr.uri}]"
    # Pipeline-bearing expressions. The inner pipeline is elided rather than
    # rendered: canonical() is a pure Expr function and Pipeline is modeled
    # in ir.query, so recursing would invert the dependency. The wrapper is
    # still named, which is what distinguishes these from each other and
    # from every other shape -- before, all three rendered as a bare "?".
    if isinstance(expr, ToScalarExpr):
        return f"toscalar({_pipeline_head(expr.pipeline)} | ...)"
    if isinstance(expr, SubqueryExpr):
        return f"({_pipeline_head(expr.pipeline)} | ...)"
    # UnknownExpr and anything a future release adds. UnknownExpr carries the
    # source text, which is the most faithful thing available for a shape the
    # builder could not model.
    return getattr(expr, "raw_text", "?").strip() if hasattr(expr, "raw_text") else "?"


def _pipeline_head(pipeline: Any) -> str:
    """The name a sub-pipeline reads from, for canonical rendering."""
    source = getattr(pipeline, "source", None)
    return getattr(source, "name", None) or "..."
