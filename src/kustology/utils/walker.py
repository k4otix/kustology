# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Primitive AST traversal helpers shared by the analysis surface.

:class:`KustoWalker` is a pre/post visitor base class; :func:`iter_elements`
unwraps the ``SeparatedElement`` wrappers that .NET list properties yield;
:func:`node_to_dict` serializes a .NET syntax node into a recursive
``{kind, text, children}`` mapping suitable for JSON or further programmatic
walking; :func:`node_text` reads a node's own source without its leading
trivia and :func:`node_name` reads the plain identifier a name node denotes.

Both recursive helpers stop at :data:`MAX_AST_DEPTH`.
"""

from __future__ import annotations

# ``kustology.bridge`` has already run ``clr.AddReference("Kusto.Language")``
# by the time this module is reachable via ``kustology.utils`` — see
# AGENTS.md.
#
# ``IncludeTrivia`` selects how much of the whitespace and comments around a
# node ``ToString()`` renders. The no-argument overload is ``All``, which
# prepends the node's *leading* trivia — so ``node.ToString()`` on
# ``// lead\nSecurityEvent`` returns the comment and newline as part of the
# text. ``Minimal`` renders the node's own source with no leading trivia.
from Kusto.Language.Syntax import IncludeTrivia

# Hard ceiling on how far the recursive helpers here descend. Both
# :func:`node_to_dict` and :meth:`KustoWalker.visit` are recursive, so the
# AST's depth is the Python stack's depth, and the .NET parser will happily
# build a tree thousands of levels deep from a few kilobytes of parentheses.
# CPython's own limit is 1000 frames, so an uncapped walk raises
# ``RecursionError`` out of the middle of the library — from
# ``KustoQuery.to_dict()``, from ``collect_nodes``, from anything built on
# them — where a caller cannot do anything useful with it.
#
# 300 is comfortably inside the frame budget (the emitters and JSON
# serialization stack on top of the walk) and comfortably above real KQL —
# though real KQL is deeper than it looks: measured against the deepest
# fixture, the margin is about 7x, not the 17x the median suggests.
# Counting the root as level 0 (the convention ``visit`` uses), the
# 49-fixture Sentinel corpus has a median depth of 18, a deepest of 42, and
# 22 fixtures past 20; a 100-operator pipe chain, which nests
# left-associatively one level per operator, reaches 106.
MAX_AST_DEPTH = 300

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
    """Base class for manual AST traversal; override ``pre_visit`` / ``post_visit``.

    The walk stops descending at :data:`MAX_AST_DEPTH`. A node at the cap is
    still visited — ``pre_visit`` and ``post_visit`` both run for it — but
    its children are not, so adversarially nested input degrades to a
    partial answer instead of a ``RecursionError`` thrown from inside
    whichever analyzer happened to be running.
    """

    def visit(self, node, depth: int = 0):
        """Walk ``node`` and its children pre-order, up to :data:`MAX_AST_DEPTH`."""
        if node is None:
            return
        self.pre_visit(node)
        if depth < MAX_AST_DEPTH:
            for i in range(node.ChildCount):
                child = node.GetChild(i)
                if child is not None:
                    self.visit(child, depth + 1)
        self.post_visit(node)

    def pre_visit(self, node):
        """Run before ``node``'s children are visited. Override to act on the way down."""

    def post_visit(self, node):
        """Run after ``node``'s children are visited. Override to act on the way up."""


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


def node_to_dict(node, depth: int = 0, max_depth: int = MAX_AST_DEPTH):
    """Recursively convert a .NET syntax node into ``{kind, text, children}``.

    At ``max_depth`` the node is emitted with an empty ``children`` list and
    an extra ``"truncated": True`` key, and the walk stops. The key is absent
    on every node that was serialized in full, so a consumer can tell a leaf
    from a cut-off subtree — ``"truncated" in node`` is the test, and a tree
    that fit entirely never carries it.
    """
    if node is None:
        return None
    result = {"kind": str(node.Kind), "text": node.ToString().strip(), "children": []}
    if depth >= max_depth:
        result["truncated"] = True
        return result
    for i in range(node.ChildCount):
        child = node.GetChild(i)
        if child is not None:
            result["children"].append(node_to_dict(child, depth + 1, max_depth))
    return result


def node_text(node) -> str:
    r"""Return ``node``'s own source text, without leading trivia.

    ``node.ToString()`` (no argument) is ``ToString(IncludeTrivia.All)``,
    which prepends whitespace *and comments* that precede the node — so
    ``// lead\nSecurityEvent`` reads back as the table name. ``Minimal``
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
    sides must spell a bracketed identifier identically. Reading the
    declaring side's source text instead splits them, and
    ``let ['weird-name'] = T; ['weird-name'] | take 1`` reports the alias
    as a second table.
    """
    if str(node.Kind) in _NAME_NODE_KINDS:
        return node.SimpleName
    return node_text(node)
