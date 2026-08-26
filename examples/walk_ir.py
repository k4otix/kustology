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

from _display import banner, kql, note, section, takeaway

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
    """Print ``node`` and everything under it as a depth-indented tree."""
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
            # `let f = (x:int) { ... }`. The body *is* built, so this walk
            # descends into it — one more scope, with its own `let`s, rather
            # than a leaf. What is still not expanded is the call site: the
            # body is reachable here, through the declaration, and not again
            # at each `f(...)`.
            fn = node.rhs_function
            params = ", ".join(
                p.decl.name + ":" + p.decl.declared_type
                + (f"={p.default.canonical_form}" if p.default is not None else "")
                for p in fn.parameters
            )
            view = "view " if fn.is_view else ""
            print(f"{indent}LetFunction: {view}{node.name}({params})")
            for body_let in fn.body_lets:
                walk(body_let, depth + 1)
            if fn.body_pipeline is not None:
                walk(fn.body_pipeline, depth + 1)
            elif fn.body_expr is not None:
                print(f"{indent}  Body: {fn.body_expr.canonical_form}")
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
    banner(
        "Walking the typed IR",
        "The same query as examples/walk_tree.py, walked over the pydantic "
        "IR instead of Microsoft's syntax tree. Dispatch is isinstance on "
        "typed classes.",
        "the depth of this tree against the AST walk of the same query. No "
        "QueryBlock, no ExpressionStatement, no nested Pipe chain.",
    )

    section("The query")
    kql(QUERY)

    section(
        "IR walk, bound against SCHEMA",
        "Two statements and two let bindings, one of them a function. Every "
        "column prints as name:type <- scope, and the type and scope are "
        "what binding adds.",
    )
    walk(parse(QUERY, schema=SCHEMA).to_ir())
    note(
        "Three fields hold parts of the query that main_pipeline alone does "
        "not reach: let_bindings, additional_pipelines for the second and "
        "later statements, and a let function's own body. Walk only "
        "main_pipeline and all three go missing without an error."
    )
    note(
        "A column read through a `let` alias resolves to the alias, since "
        "`table` names the immediate scope. Drop schema=SCHEMA and the walk "
        "still runs; every column then prints `unresolved <- None`."
    )

    takeaway(
        "The IR is flatter than the AST and its nodes are classes, so a "
        "walk over it is isinstance dispatch and little else. Reach for "
        "examples/find_all_demo.py when you want every node of one type "
        "wherever it sits, rather than the pipeline's exact shape.",
        more="docs/tier2-ir.md",
    )


if __name__ == "__main__":
    main()
