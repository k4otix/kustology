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
    CaseExpr,
    ColumnRef,
    Exists,
    FuncCall,
    LiteralExpr,
    Not,
    Or,
    RegexMatch,
    SetMembership,
)


def normalize_in_place(expr: Any) -> Any:
    """Apply semantic-preserving rewrites so equivalent KQL produces the same shape.

    * ``tolower(X) == "y"`` → ``X =~ "y"`` (case-insensitive equality)
    * ``tolower(X) != "y"`` → ``X !~ "y"``
    * Flatten nested ``And`` / ``Or`` operands into a single list.
    * ``!!X`` → ``X``.
    """
    if isinstance(expr, BinOp):
        if (isinstance(expr.left, FuncCall)
                and expr.left.name.lower() == "tolower"
                and expr.op in ("==", "!=")
                and len(expr.left.args) == 1):
            expr.op = "=~" if expr.op == "==" else "!~"
            expr.case_sensitive = False
            expr.left = expr.left.args[0]
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
    elif isinstance(expr, Not):
        if isinstance(expr.operand, Not):
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
        vals = ", ".join(sorted(canonical(v) for v in expr.values))
        op = "in" if expr.polarity == "inclusion" else "!in"
        if not expr.case_sensitive:
            op += "~"
        return f"{canonical(expr.column)} {op} ({vals})"
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
        return f"exists({canonical(expr.target)})"
    if isinstance(expr, RegexMatch):
        return f"{canonical(expr.target)} matches regex \"{expr.pattern}\""
    return getattr(expr, "raw_text", "?").strip() if hasattr(expr, "raw_text") else "?"
