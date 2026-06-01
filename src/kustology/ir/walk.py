# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Generic IR traversal.

The IR is acyclic. ``walk`` is depth-first, pre-order, and yields only
pydantic ``BaseModel`` descendants — primitive values (strings, ints,
enums, ``None``) are skipped, since they're read via attribute access on
the node that owns them.

``find_all`` is the type-filtered convenience wrapper most analyzers use.
"""

from typing import Callable, Iterator, Optional, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Predicate = Callable[[BaseModel], bool]


def walk(
    node: BaseModel,
    predicate: Optional[Predicate] = None,
) -> Iterator[BaseModel]:
    """Yield every ``BaseModel`` descendant of ``node`` (including the
    root) in depth-first, pre-order.

    Descends into list-valued and dict-valued fields. Assumes the tree is
    acyclic — the IR builder never produces cycles.

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
        value = getattr(node, name)
        if isinstance(value, list):
            items: object = value
        elif isinstance(value, dict):
            items = value.values()
        else:
            items = (value,)
        for item in items:  # type: ignore[attr-defined]
            if isinstance(item, BaseModel):
                yield from walk(item, predicate)


def find_all(node: BaseModel, type_: Type[T]) -> Iterator[T]:
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
