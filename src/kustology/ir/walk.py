# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Generic IR traversal.

``walk`` is depth-first, pre-order, and yields only pydantic
``BaseModel`` descendants — primitive values (strings, ints, enums,
``None``) are skipped, since they're read via attribute access on the
node that owns them. Container fields are unwrapped to any depth, so a
model reached only through ``list[tuple[...]]`` is still visited, and an
object reachable by more than one path is yielded once.

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
    nested containers such as ``list[tuple[Expr, Expr]]``.

    **Each object is yielded exactly once.** The IR is a DAG, not a tree:
    several nodes index into a subtree that another field already owns,
    holding the *same* objects rather than copies. ``LetBinding`` is the
    clear case — ``inner_time_exprs`` and ``inner_tables`` point at nodes
    that also live inside ``rhs_pipeline`` — so an un-deduplicated walk
    reported ``ago`` and ``now`` twice for a single ``let``, and any caller
    counting occurrences double-counted them. Identity is the key
    (``id(node)``), not equality: two structurally identical nodes written
    at different offsets are different nodes and both must surface.

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
    yield from _walk(node, predicate, set())


def _walk(
    node: BaseModel,
    predicate: Predicate | None,
    seen: set[int],
) -> Iterator[BaseModel]:
    """Recursive half of :func:`walk`, threading the visited set.

    Kept private so the public signature stays two arguments: the set is an
    implementation detail of one traversal, and a caller who supplied their
    own could silently suppress nodes.

    Re-entering an already-visited object prunes its whole subtree, not just
    the yield — descending a second time can only re-yield what the first
    descent already produced. Holding ``id()`` values is safe because the
    root keeps every descendant alive for the traversal's lifetime, so no
    address is recycled underneath us.
    """
    if id(node) in seen:
        return
    seen.add(id(node))
    if predicate is None or predicate(node):
        yield node
    for name in type(node).model_fields:
        for item in _models_in(getattr(node, name)):
            yield from _walk(item, predicate, seen)


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
