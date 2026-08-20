# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Direct AST traversal via ``KustoQuery.syntax``.

The raw Microsoft AST is the full grammar — every token, every wrapper,
every operator regardless of whether the IR dispatches it. Walking it
takes more code (kind-string switch, wrapper filtering, token skipping)
but gives you token positions, comments, and constructs the IR may not
yet model.

For most analysis tasks, prefer ``examples/walk_ir.py``: ``isinstance``
dispatch on typed pydantic operators, no wrapper noise. Walk the AST
when you need token-level access (refactoring, syntax highlighting) or
coverage of operators the IR hasn't dispatched. For JSON serialization
of the IR, see ``examples/llm_view.py``.

The pattern shown — match a closed set of node ``Kind`` strings, recurse
through wrappers without indenting, short-circuit on operator nodes that
already summarize their condition/columns — is the same one
``utils/analysis.py`` uses internally and is the right way to build
custom AST-level analyzers.
"""

from kustology import parse

# Wrappers that have no logical weight — descend through them silently.
_TRANSPARENT = {"List", "SeparatedElement", "TokenName"}


def walk_node(node, depth: int = 0) -> None:
    if node is None:
        return
    try:
        kind = str(node.Kind)
    except AttributeError:
        return

    # Tokens are punctuation/keywords; skip both display and recursion.
    if "Token" in kind:
        return

    # Structural wrappers: recurse without indenting.
    if kind in _TRANSPARENT:
        for i in range(node.ChildCount):
            walk_node(node.GetChild(i), depth)
        return

    indent = "  " * depth

    if kind == "QueryBlock":
        print(f"{indent}QueryBlock")
    elif kind == "LetStatement":
        # The name lives on the statement; the bound expression is a child,
        # so recurse (no early return) to show the right-hand pipeline.
        print(f"{indent}Let: {node.Name.ToString().strip()}")
    elif kind == "ExpressionStatement":
        print(f"{indent}Statement")
    elif kind == "PipeExpression":
        print(f"{indent}Pipe (|)")
    elif kind == "NameReference":
        print(f"{indent}Source: {node.ToString().strip()}")
        return  # leaf: don't recurse into TokenName/IdentifierToken
    elif kind == "FilterOperator":
        condition = node.Condition if hasattr(node, "Condition") else node.GetChild(2)
        text = condition.ToString().strip() if condition is not None else ""
        print(f"{indent}Filter: {text}")
        return  # condition is summarized in the line above
    elif kind == "ProjectOperator":
        cols = node.Columns if hasattr(node, "Columns") else node.GetChild(1)
        text = cols.ToString().strip() if cols is not None else ""
        print(f"{indent}Project: {text}")
        return  # column list is summarized in the line above

    for i in range(node.ChildCount):
        walk_node(node.GetChild(i), depth + 1)


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
    print("AST walk (logical nodes only):")
    walk_node(parse(QUERY).syntax)


if __name__ == "__main__":
    main()
