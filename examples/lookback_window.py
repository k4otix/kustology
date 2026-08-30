# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Compute a detection's outer lookback window from `result_type` alone.

`let end = ago(2h); let start = end - 8h;` binds two names, and both are
`datetime` -- `end` because `ago()` says so, `start` because Microsoft's
binder can add a `timespan` to a `datetime` and knows the result is a
`datetime` too. Neither binding needed a schema: this parse never supplies
one, and `T`'s columns stay `unresolved` throughout.

A resolver that reads every `let` into one `dict[str, timedelta]` cannot
tell `end` and `start` apart -- it would read `end - 8h` as `2h - 8h` and
report a six-hour lookback for a query that reads ten. Dispatching on
`binding.rhs_expr.result_type` keeps `datetime` and `timespan` apart, the
way the query itself does.

The second half of the example is what `prune=` buys once that offset
exists: the query also joins a lookup table scanning `ago(99d)`, a much
wider window that never determines what the *outer* pipeline reads. Walking
`ir.main_pipeline` with `prune=lambda n: isinstance(n, (JoinOp, LookupOp))`
keeps the `JoinOp` node itself in the walk -- so an analyzer can still see
that a join happened -- without descending into its right-hand subquery, so
the 99-day scan never reaches the offset calculation.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

from datetime import timedelta

from _display import banner, kql, note, section, table, takeaway

from kustology import parse
from kustology.ir import (
    BinOp,
    FuncCall,
    JoinOp,
    KustoType,
    LetValueRef,
    LiteralExpr,
    LookupOp,
    span_of,
    walk,
)

QUERY = """let end = ago(2h);
let start = end - 8h;
T
| where TimeGenerated between (start .. end)
| join (S | where TimeGenerated > ago(99d)) on X
"""


def _is_time(node: object) -> bool:
    """Report whether ``node`` names a point in time relative to now.

    True for an ``ago(...)`` call and for a ``let`` reference the binder
    already resolved to ``datetime`` -- ``result_type`` lives on the
    ``LetValueRef`` itself, copied from the binding it names, so no lookup
    into ``ir.let_bindings`` is needed to ask the question.
    """
    if isinstance(node, FuncCall) and node.name == "ago":
        return True
    return isinstance(node, LetValueRef) and node.result_type is KustoType.DATETIME


def _offset(expr, env: dict[str, timedelta]) -> timedelta:
    """Return the signed offset from now (datetime) or a duration (timespan), as a timedelta."""
    if isinstance(expr, LiteralExpr) and expr.result_type is KustoType.TIMESPAN:
        # ticks are 100ns units; ticks // 10 is exact to a microsecond -- see
        # docs/tier1-syntax-tree.md#totalseconds-loses-sub-second-precision.
        return timedelta(microseconds=expr.ticks / 10)
    if isinstance(expr, FuncCall) and expr.name == "ago":
        # ago(x) means x in the past, so its offset from now is -x.
        return -_offset(expr.args[0], env)
    if isinstance(expr, LetValueRef):
        return env[expr.name]
    if isinstance(expr, BinOp) and expr.op in ("+", "-"):
        left, right = _offset(expr.left, env), _offset(expr.right, env)
        return left + right if expr.op == "+" else left - right
    raise ValueError(f"unsupported {expr.kind}")


def main() -> None:
    banner(
        "Outer lookback via result_type, prune, and span_of",
        "A schemaless parse still tells `end = ago(2h)` and `start = end - "
        "8h` apart as `datetime`, because `result_type` comes from "
        "Microsoft's built-ins, not a table schema. `prune=` then keeps a "
        "joined lookup table's own 99-day scan out of the answer.",
        "the `datetime` column below -- both `let`s resolve to `datetime`, "
        "not `timespan`, which is what makes subtracting `8h` from `end` "
        "still land on a point in time.",
    )

    ir = parse(QUERY).to_ir(semantic_hash=False)

    section(
        "Binding each `let` without a schema",
        "Every offset below is computed from `rhs_expr`, not asserted.",
    )
    env: dict[str, timedelta] = {}
    rows = []
    for binding in ir.let_bindings:
        env[binding.name] = _offset(binding.rhs_expr, env)
        rows.append([binding.name, str(binding.rhs_expr.result_type), str(env[binding.name])])
    table(["let", "result_type", "offset from now"], rows)

    # The fact the whole example rests on: an unbound parse still resolves
    # `end - 8h` to `datetime`, because `end` is `datetime` and Microsoft's
    # binder knows `datetime - timespan -> datetime`. If the binder ever
    # stopped resolving this, every row above would still print -- only this
    # assertion would catch the regression.
    assert ir.let_bindings[1].rhs_expr.result_type is KustoType.DATETIME
    note(
        "`start`'s result_type is `datetime`, not `timespan` -- a resolver "
        "keeping one `dict[str, timedelta]` for every `let` would have no "
        "way to tell it apart from `8h` and would read `end - 8h` as "
        "`2h - 8h` instead of `ago(2h) - 8h`."
    )

    section(
        "The outer pipeline, without the join's own subquery",
        "`span_of` locates this text even though `Pipeline` has no `span` "
        "field of its own.",
    )
    kql(span_of(ir.main_pipeline).text(ir.raw_text))

    outer = walk(ir.main_pipeline, prune=lambda n: isinstance(n, (JoinOp, LookupOp)))
    offsets = [_offset(n, env) for n in outer if isinstance(n, (FuncCall, LetValueRef)) and _is_time(n)]
    lookback = -min(offsets)
    note(
        "The join's `ago(99d)` never reaches `offsets`: `prune` still "
        "yields the `JoinOp` node itself, but stops the walk before it "
        "descends into `right`, so the 99-day lookup scan the join reads "
        "cannot inflate the outer pipeline's own window."
    )

    section("Outer lookback")
    print(f"  {lookback} (the `where` clause's `start .. end` window)")

    takeaway(
        "`result_type` on a schemaless parse is real for anything the "
        "built-ins decide, `datetime` and `timespan` included -- enough to "
        "resolve a detection's own time window without a table schema. "
        "`prune=` then keeps a join's or lookup's wider scan from being "
        "read as part of that window.",
        more="docs/tier2-ir.md",
    )

    assert lookback == timedelta(hours=10), lookback


if __name__ == "__main__":
    main()
