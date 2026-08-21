# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Primitive AST traversal helpers shared by the analysis surface.

:class:`KustoWalker` is a pre/post visitor base class; :func:`iter_elements`
unwraps the ``SeparatedElement`` wrappers that .NET list properties yield;
:func:`node_to_dict` serializes a .NET syntax node into a recursive
``{kind, text, children}`` mapping suitable for JSON or further programmatic
walking; :func:`node_text` reads a node's own source without its leading
trivia and :func:`node_name` reads the plain identifier a name node denotes.
"""

from __future__ import annotations

# Bridge import elsewhere in the package (``kustology.bridge``) already
# triggered ``clr.AddReference("Kusto.Language")`` by the time this module is
# reachable via ``kustology.utils`` — see AGENTS.md.
#
# ``IncludeTrivia`` selects how much of the whitespace and comments around a
# node ``ToString()`` renders. The no-argument overload is ``All``, which
# prepends the node's *leading* trivia — so ``node.ToString()`` on
# ``// lead\nSecurityEvent`` returns the comment and newline as part of the
# text. ``Minimal`` renders the node's own source with no leading trivia.
from Kusto.Language.Syntax import IncludeTrivia

_NAME_NODE_KINDS = frozenset(
    {
        "NameReference",
        "NameDeclaration",
        "BracketedName",
        "TokenName",
        "WildcardedName",
    }
)


class KustoWalker:
    """Base class for manual AST traversal. Override pre_visit / post_visit."""

    def visit(self, node):
        if node is None:
            return
        self.pre_visit(node)
        for i in range(node.ChildCount):
            child = node.GetChild(i)
            if child is not None:
                self.visit(child)
        self.post_visit(node)

    def pre_visit(self, node):
        pass

    def post_visit(self, node):
        pass


def iter_elements(syntax_list):
    """Yield the real nodes of a .NET syntax list, unwrapping ``SeparatedElement``.

    Microsoft's list-valued properties come in two shapes and the difference is
    not visible at the call site. ``SyntaxList[SeparatedElement[T]]`` — which is
    what ``ProjectOperator.Expressions``, ``QueryBlock.Statements`` and
    ``FunctionParameters.Parameters`` return — yields wrappers that carry the
    trailing comma alongside the expression. ``SyntaxList[T]``, such as
    ``SummarizeOperator.Parameters``, yields ``T`` directly.

    A wrapper's ``str()`` differs from the expression's only by that comma and
    surrounding whitespace, so a missing unwrap looks correct in printed output
    while every ``node.Kind`` check silently fails to match — the wrapper's
    ``Kind`` is ``SeparatedElement``, never the expression's kind.

    Handles both shapes so callers need not know which a property returns.

    Example:
        >>> from kustology import iter_elements, parse
        >>> from kustology.utils.analysis import collect_nodes
        >>> syntax = parse("T | project A, B").syntax
        >>> project = collect_nodes(syntax, lambda n: str(n.Kind) == "ProjectOperator")[0]
        >>> [str(e.Kind) for e in iter_elements(project.Expressions)]
        ['NameReference', 'NameReference']
    """
    for i in range(syntax_list.Count):
        item = syntax_list[i]
        yield getattr(item, "Element", item)


def node_to_dict(node):
    """Recursively convert a .NET syntax node into ``{kind, text, children}``."""
    if node is None:
        return None
    result = {"kind": str(node.Kind), "text": node.ToString().strip(), "children": []}
    for i in range(node.ChildCount):
        child = node.GetChild(i)
        if child is not None:
            result["children"].append(node_to_dict(child))
    return result


def node_text(node) -> str:
    """Return ``node``'s own source text, without leading trivia.

    ``node.ToString()`` (no argument) is ``ToString(IncludeTrivia.All)``,
    which prepends whitespace *and comments* that precede the node — so
    ``// lead\\nSecurityEvent`` reads back as the table name. ``Minimal``
    renders only the node's own text; interior comments collapse to a
    line break rather than vanishing, so this is a read, not a rewrite.
    """
    return node.ToString(IncludeTrivia.Minimal)


def node_name(node) -> str:
    """Return the plain identifier a name node denotes, unquoted.

    ``NameReference``, ``NameDeclaration``, ``BracketedName``, ``TokenName``
    and ``WildcardedName`` all expose ``SimpleName`` directly: for
    ``['my-table']`` (a ``BracketedName``) it is the unquoted ``my-table``,
    not the bracketed-and-quoted source text ``node_text`` would return. For
    every other node kind this falls back to :func:`node_text`.

    ``NameDeclaration`` is the *declaring* side of a name — the ``X`` in
    ``let X = ...`` or ``| as X`` — and belongs here for the same reason:
    analyzers match declarations against references by name, so the two
    sides must spell a bracketed identifier identically. They did not, and
    ``let ['weird-name'] = T; ['weird-name'] | take 1`` reported the alias
    as a second table.
    """
    if str(node.Kind) in _NAME_NODE_KINDS:
        return node.SimpleName
    return node_text(node)
