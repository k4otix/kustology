# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Primitive AST traversal helpers shared by the analysis surface.

:class:`KustoWalker` is a pre/post visitor base class; :func:`iter_elements`
unwraps the ``SeparatedElement`` wrappers that .NET list properties yield;
:func:`node_to_dict` serializes a .NET syntax node into a recursive
``{kind, text, children}`` mapping suitable for JSON or further programmatic
walking.
"""

from __future__ import annotations


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
