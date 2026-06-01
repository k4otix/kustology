# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Primitive AST traversal helpers shared by the analysis surface.

:class:`KustoWalker` is a pre/post visitor base class; :func:`node_to_dict`
serializes a .NET syntax node into a recursive ``{kind, text, children}``
mapping suitable for JSON or further programmatic walking.
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
