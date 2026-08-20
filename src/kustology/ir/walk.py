# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Generic IR traversal.

The IR is acyclic. ``walk`` is depth-first, pre-order, and yields only
pydantic ``BaseModel`` descendants — primitive values (strings, ints,
enums, ``None``) are skipped, since they're read via attribute access on
the node that owns them. Container fields are unwrapped to any depth, so
a model reached only through ``list[tuple[...]]`` is still visited.

``find_all`` is the type-filtered convenience wrapper most analyzers use.
"""

from collections.abc import Callable, Iterator
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Predicate = Callable[[BaseModel], bool]


def walk(
    node: BaseModel,
    predicate: Predicate | None = None,
) -> Iterator[BaseModel]:
    """Yield every ``BaseModel`` descendant of ``node`` (including the
    root) in depth-first, pre-order.

    Descends into list-, tuple- and dict-valued fields, and through
    nested containers such as ``list[tuple[Expr, Expr]]``. Assumes the
    tree is acyclic — the IR builder never produces cycles.

    With ``predicate``, only nodes for which ``predicate(node)`` returns
    truthy are yielded; traversal still descends into every subtree, so a
    skipped parent doesn't hide its children. Use this for "every BinOp
    that's case-insensitive" / "every operator with a span past
    position X" / cross-cutting filters that don't reduce to a type.

    Example:
        >>> for n in walk(ir):
        ...     ...
        >>> for n in walk(ir, lambda n: isinstance(n, BinOp) and not n.case_sensitive):
        ...     ...
    """
    if predicate is None or predicate(node):
        yield node
    for name in type(node).model_fields:
        for item in _models_in(getattr(node, name)):
            yield from walk(item, predicate)


def _models_in(value: object) -> Iterator[BaseModel]:
    """Yield every ``BaseModel`` directly held by ``value``.

    Descends list, tuple and dict containers recursively, so a field typed
    ``list[tuple[Expr, Expr]]`` (``CaseExpr.branches``) is reached as
    readily as a plain ``list[Expr]``. Nesting the containers is what
    matters: a tuple sitting inside a list is not a ``BaseModel``, so a
    walker that only unwraps one level treats the whole arm as a scalar
    and silently skips it.
    """
    if isinstance(value, BaseModel):
        yield value
        return
    if isinstance(value, dict):
        value = value.values()
    elif not isinstance(value, (list, tuple)):
        return
    for item in value:
        yield from _models_in(item)


def find_all(node: BaseModel, type_: type[T]) -> Iterator[T]:
    """Yield every descendant of ``node`` that is an instance of ``type_``.

    The 90%-case wrapper around :func:`walk`. Custom analyzers typically
    reduce to a single ``find_all`` call plus attribute access.

    Example:
        >>> from kustology.ir import find_all, FilterOp
        >>> filters = list(find_all(ir, FilterOp))
    """
    for n in walk(node):
        if isinstance(n, type_):
            yield n
