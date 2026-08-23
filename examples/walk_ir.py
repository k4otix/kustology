# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Typed tree traversal via the IR, over a bound parse.

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

The parse is **bound** — ``parse(QUERY, schema=SCHEMA)`` — which is what
puts a type and a table on every column reference. ``to_ir()`` auto-attaches
on a bound parse, so no schema is restated. Drop ``schema=SCHEMA`` and the
same walk still runs; every column just prints ``unresolved <- None``.

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
    SummarizeOp,
    TableRef,
)

SCHEMA = {
    "StormEvents": {
        "StartTime": "datetime",
        "State": "string",
        "EventType": "string",
        "DeathsDirect": "int",
    }
}


def _describe_column(c) -> str:
    if isinstance(c, ColumnRef):
        # ``name`` is syntactic and always there; ``result_type`` and
        # ``table`` are what binding adds. ``result_type`` is a KustoType
        # StrEnum, so it formats as the wire value ("string"), and ``table``
        # is the *scope* the column resolved against — which for a column
        # read through a `let` alias is the alias, not the table behind it.
        return f"{c.name}:{c.result_type} <- {c.table}"
    if isinstance(c, Assignment):
        return f"{c.name} = {c.expr.canonical_form}"
    return type(c).__name__


def walk(node, depth: int = 0) -> None:
    indent = "  " * depth
    if isinstance(node, QueryIR):
        print(f"{indent}QueryIR (schema_attached={node.schema_attached})")
        # let bindings hang off the query, not off the main pipeline —
        # walking only main_pipeline silently skips them.
        for binding in node.let_bindings:
            walk(binding, depth + 1)
        # Same trap one field over: a multi-statement query (`T | count;
        # U | count`) keeps its second and later statements in
        # `additional_pipelines`, so `main_pipeline` alone is only ever the
        # first one. This query has two statements, so the list is not empty.
        for pipeline in [node.main_pipeline, *node.additional_pipelines]:
            walk(pipeline, depth + 1)
    elif isinstance(node, LetBinding):
        # A binding's right-hand side is exactly one of three things, and
        # they are separate fields rather than one polymorphic one.
        if node.rhs_function is not None:
            # `let f = (x:int) { ... }`. The body is NOT built — the IR
            # records the parameter names and a span locating the body in
            # the source, and stops there. Call sites are not expanded, so
            # nothing inside those braces reaches this walk.
            params = ", ".join(node.rhs_function.parameters)
            body = node.rhs_function.body_span
            print(f"{indent}LetFunction: {node.name}({params})"
                  f"  body at [{body.text_start}, {body.text_start + body.width})")
        elif node.rhs_expr is not None:
            print(f"{indent}Let: {node.name} = {node.rhs_expr.canonical_form}")
        else:
            print(f"{indent}Let: {node.name}")
            if node.rhs_pipeline is not None:
                walk(node.rhs_pipeline, depth + 1)
    elif isinstance(node, Pipeline):
        shape = node.result_schema
        cols = ", ".join(f"{n}:{t}" for n, t in shape.columns.items()) if shape else "?"
        print(f"{indent}Pipeline -> ({cols})")
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
    elif isinstance(node, SummarizeOp):
        aggs = ", ".join(_describe_column(a) for a in node.aggregations)
        by = ", ".join(_describe_column(b) for b in node.by)
        print(f"{indent}Summarize: {aggs} by {by}")
    else:
        print(f"{indent}{type(node).__name__}")


QUERY = (
    'let tornadoes = StormEvents | where EventType == "Tornado";\n'
    "let deadly = (n:int) { tornadoes | where DeathsDirect > n };\n"
    "tornadoes\n"
    '| where State == "TEXAS"\n'
    "| project StartTime, State, EventType, DeathsDirect;\n"
    "tornadoes | summarize Events = count() by State"
)


def main() -> None:
    print("Input query:")
    for line in QUERY.splitlines():
        print(f"  {line}")
    print()
    print("IR walk (typed pipeline, bound against SCHEMA):")
    walk(parse(QUERY, schema=SCHEMA).to_ir())


if __name__ == "__main__":
    main()
