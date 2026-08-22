# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Typed tree traversal via the IR.

Mirror of ``walk_tree.py`` on the same query, but operating on the
typed pydantic IR instead of Microsoft's syntax tree. Output is the
same depth-indented tree, but nodes are typed Python classes
(``QueryIR``, ``Pipeline``, ``FilterOp``, ``ProjectOp``, …) rather
than kind-strings. Dispatch is ``isinstance`` rather than
``str(node.Kind)``.

The IR is flatter than the AST: no ``QueryBlock`` /
``ExpressionStatement`` wrappers, no nested ``PipeExpression`` chain,
no per-token recursion. The flatter shape is what makes ``isinstance``
dispatch on it ergonomic.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

from kustology import parse
from kustology.ir import (
    Assignment,
    ColumnRef,
    FilterOp,
    LetBinding,
    LetRef,
    Pipeline,
    ProjectOp,
    QueryIR,
    TableRef,
)


def _describe_column(c) -> str:
    if isinstance(c, ColumnRef):
        return c.name
    if isinstance(c, Assignment):
        return f"{c.name} = <expr>"
    return type(c).__name__


def walk(node, depth: int = 0) -> None:
    indent = "  " * depth
    if isinstance(node, QueryIR):
        print(f"{indent}QueryIR")
        # let bindings hang off the query, not off the main pipeline —
        # walking only main_pipeline silently skips them.
        for binding in node.let_bindings:
            walk(binding, depth + 1)
        # Same trap one field over: a multi-statement query (`T | count;
        # U | count`) keeps its second and later statements in
        # `additional_pipelines`, so `main_pipeline` alone is only ever the
        # first one.
        for pipeline in [node.main_pipeline, *node.additional_pipelines]:
            walk(pipeline, depth + 1)
    elif isinstance(node, LetBinding):
        # A binding's right-hand side is a pipeline, a scalar expression or
        # a user function; only the first is walkable as a pipeline.
        if node.rhs_expr is not None:
            print(f"{indent}Let: {node.name} = {node.rhs_expr.canonical_form}")
        else:
            print(f"{indent}Let: {node.name}")
            if node.rhs_pipeline is not None:
                walk(node.rhs_pipeline, depth + 1)
    elif isinstance(node, Pipeline):
        print(f"{indent}Pipeline")
        walk(node.source, depth + 1)
        for op in node.operators:
            walk(op, depth + 1)
    elif isinstance(node, TableRef):
        print(f"{indent}Source: {node.name}")
    elif isinstance(node, LetRef):
        # A source naming a let binding rather than a real table. Without
        # this branch it falls through to the bare-class-name fallback.
        print(f"{indent}Source: {node.name} (let)")
    elif isinstance(node, FilterOp):
        print(f"{indent}Filter: {node.predicate.canonical_form}")
    elif isinstance(node, ProjectOp):
        cols = ", ".join(_describe_column(c) for c in node.columns)
        print(f"{indent}Project: {cols}")
    else:
        print(f"{indent}{type(node).__name__}")


QUERY = (
    'let tornadoes = StormEvents | where EventType == "Tornado";\n'
    "tornadoes "
    '| where State == "TEXAS" '
    "| project StartTime, State, EventType, DeathsDirect"
)


def main() -> None:
    print("Input query:")
    for line in QUERY.splitlines():
        print(f"  {line}")
    print()
    print("IR walk (typed pipeline):")
    walk(parse(QUERY).to_ir())


if __name__ == "__main__":
    main()
