# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Direct AST traversal via ``KustoQuery.syntax``.

The raw Microsoft AST is the full grammar — every token, every wrapper,
every operator regardless of whether the IR dispatches it. Walking it
takes more code (kind-string switch, wrapper filtering, token skipping)
but gives you token positions, comments, and constructs the IR may not
yet model.

For most analysis tasks, prefer ``examples/walk_ir.py``: ``isinstance``
dispatch on typed pydantic operators, no wrapper noise. It walks the same
query this file does, so the two outputs are directly comparable. Walk the
AST when you need token-level access (refactoring, syntax highlighting) or
coverage of operators the IR hasn't dispatched. For JSON serialization
of the IR, see ``examples/llm_view.py``.

The pattern shown — match a closed set of node ``Kind`` strings, recurse
through wrappers without indenting, short-circuit on operator nodes that
already summarize their condition/columns — is the same one
``utils/analysis.py`` uses internally and is the right way to build
custom AST-level analyzers.

**Order the two filters correctly, and match on the end of the kind.** Both
halves of that are load-bearing, and the second demo below shows what the
sloppy version costs.
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

    # Structural wrappers: recurse without indenting. This has to run
    # *before* the token filter, because `TokenName` is a wrapper whose
    # name contains "Token" — with the checks the other way round it is
    # dropped as if it were a token, along with everything under it.
    if kind in _TRANSPARENT:
        for i in range(node.ChildCount):
            walk_node(node.GetChild(i), depth)
        return

    # Tokens are punctuation/keywords; skip both display and recursion.
    # `kind.endswith("Token")`, not `"Token" in kind`: 58 of the parser's
    # kinds contain the substring and four of them are not tokens —
    # TokenName, SkippedTokens, CommandAndSkippedTokens, and
    # TokenLiteralExpression, which is the `inner` in `join kind=inner`.
    # The substring version silently deletes all four from the walk.
    if kind.endswith("Token"):
        return

    indent = "  " * depth

    if kind == "QueryBlock":
        print(f"{indent}QueryBlock")
    elif kind == "LetStatement":
        # The name lives on the statement; the bound expression is a child,
        # so recurse (no early return) to show the right-hand pipeline.
        print(f"{indent}Let: {node.Name.ToString().strip()}")
    elif kind == "FunctionDeclaration":
        # `let f = (x:int) { ... }`. The IR records only the parameter names
        # and a body span (see walk_ir.py); the AST has the whole body, which
        # is the reason to reach for tier 1 here.
        print(f"{indent}FunctionDeclaration: {node.ToString().strip()}")
        return
    elif kind == "ExpressionStatement":
        print(f"{indent}Statement")
    elif kind == "PipeExpression":
        print(f"{indent}Pipe (|)")
    elif kind == "NameReference":
        # One node kind for every bare name: the source table, a `let`
        # alias, a function callee, a `by` key. The AST does not separate
        # them — that separation is what tier 2 adds (TableRef / LetRef /
        # LetValueRef / ColumnRef), and it is the clearest single reason to
        # prefer walk_ir.py for analysis.
        print(f"{indent}Name: {node.ToString().strip()}")
        return  # leaf: don't recurse into TokenName/IdentifierToken
    elif kind.endswith("LiteralExpression"):
        print(f"{indent}Literal: {node.ToString().strip()}  [{kind}]")
        return
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
    elif kind.endswith("Operator"):
        # Any operator this switch does not name. Labelling it keeps the
        # tree readable — without this the children of a `summarize` indent
        # under a line that was never printed. Recursion continues, since
        # there is no summary line standing in for the subtree.
        print(f"{indent}{kind.removesuffix('Operator')}")

    for i in range(node.ChildCount):
        walk_node(node.GetChild(i), depth + 1)


QUERY = (
    'let tornadoes = StormEvents | where EventType == "Tornado";\n'
    "let deadly = (n:int) { tornadoes | where DeathsDirect > n };\n"
    "tornadoes\n"
    '| where State == "TEXAS"\n'
    "| project StartTime, State, EventType, DeathsDirect;\n"
    "tornadoes | summarize Events = count() by State"
)

# `kind=inner` parses as a TokenLiteralExpression — a literal whose *kind
# name* contains "Token" though it is not a token. A `"Token" in kind`
# filter drops it, so a walker written that way reports a join and never
# reports which kind of join.
JOIN_QUERY = "StormEvents | join kind=inner (StormEvents) on State"


def main() -> None:
    print("Input query:")
    for line in QUERY.splitlines():
        print(f"  {line}")
    print()
    print("AST walk (logical nodes only):")
    walk_node(parse(QUERY).syntax)

    print()
    print(f"Input query: {JOIN_QUERY}")
    print()
    print("AST walk — the `inner` line is what `\"Token\" in kind` would hide:")
    walk_node(parse(JOIN_QUERY).syntax)


if __name__ == "__main__":
    main()
