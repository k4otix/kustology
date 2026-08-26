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

**Match on the end of the kind, not the substring.** That is the half that
is load-bearing: `kind.endswith("Token")` drops none of the four
"Token"-containing kinds that are not tokens, where `"Token" in kind` drops
three or four of them depending on where you put the check. The second demo
below shows one of them costing real information.
"""

from _display import banner, kql, note, section, takeaway

from kustology import parse

# Wrappers that have no logical weight — descend through them silently.
_TRANSPARENT = {"List", "SeparatedElement", "TokenName"}


def walk_node(node, depth: int = 0) -> None:
    """Print the logical nodes under ``node`` as a depth-indented tree."""
    if node is None:
        return
    try:
        kind = str(node.Kind)
    except AttributeError:
        return

    # Structural wrappers: recurse without indenting. Deciding "is this a
    # wrapper" before "is this a token" is just the clearer order — with
    # the endswith filter below the two are independent, since no member of
    # _TRANSPARENT ends in "Token".
    if kind in _TRANSPARENT:
        for i in range(node.ChildCount):
            walk_node(node.GetChild(i), depth)
        return

    # Tokens are punctuation/keywords; skip both display and recursion.
    #
    # `kind.endswith("Token")`, not `"Token" in kind`. Of the parser's 605
    # kinds, 58 contain the substring and four of those are not tokens:
    # TokenName, SkippedTokens, CommandAndSkippedTokens, and
    # TokenLiteralExpression — the `inner` in `join kind=inner`. None of the
    # four ends in "Token", so endswith keeps all four and the substring
    # version drops them: all four if it runs before the wrapper check
    # (TokenName included, along with everything under it), three if it runs
    # here, where _TRANSPARENT has already claimed TokenName. Either way the
    # information is gone, which is what the second demo below shows.
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
        # `let f = (x:int) { ... }`, printed token-exact: ToString() is the
        # declaration's source text, which the typed IR does not keep. The
        # walk_ir.py mirror descends into the typed body instead.
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
    banner(
        "Walking Microsoft's syntax tree",
        "One walk over a two-statement query prints the logical nodes and "
        "drops the tokens and wrappers. A second walk over a join shows what "
        "a substring test for \"Token\" throws away.",
        "how much of this tree is grammar rather than meaning, and how many "
        "different things arrive as a bare Name line.",
    )

    section("The query")
    kql(QUERY)

    section(
        "AST walk, logical nodes only",
        "Wrappers recurse without indenting and tokens never print, so the "
        "depth below tracks the query's structure rather than the grammar's.",
    )
    walk_node(parse(QUERY).syntax)
    note(
        "The source table, the `let` alias that reads it, the function "
        "callee, and the `by` key all print as Name. The AST does not "
        "separate them. Sorting them into TableRef, LetRef, LetValueRef, "
        "and ColumnRef is what tier 2 adds, and it is the clearest single "
        "reason to walk the IR instead."
    )

    section(
        "What a substring test hides",
        "`kind=inner` parses as a TokenLiteralExpression: a kind name that "
        "contains \"Token\" on a node that is not one.",
    )
    kql(JOIN_QUERY)
    print()
    walk_node(parse(JOIN_QUERY).syntax)
    note(
        "The `Literal: inner` line is the join kind. A walker that filters "
        "on `\"Token\" in kind` reports the join and never reports which "
        "kind of join it is."
    )

    takeaway(
        "Walk the AST when you need token positions, comments, or an "
        "operator the IR does not model yet. The switch above is the "
        "pattern to copy: match a closed set of kind strings, recurse "
        "through wrappers, and stop at nodes that already summarize "
        "themselves.",
        more="docs/tier1-syntax-tree.md, and examples/walk_ir.py for this "
             "same query through the typed IR",
    )


if __name__ == "__main__":
    main()
