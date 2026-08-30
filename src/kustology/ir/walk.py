# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Generic IR traversal.

``walk`` is depth-first and pre-order. It yields each pydantic ``BaseModel``
descendant once and skips primitive values (strings, ints, enums, ``None``),
which you read by attribute access on the node that owns them. Container
fields are unwrapped to any depth, so a model reached only through
``list[tuple[...]]`` is still visited.

``find_all`` is the type-filtered convenience wrapper most analyzers use.
"""

from collections.abc import Callable, Iterator
from enum import Enum
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel

from .spans import Span

T = TypeVar("T", bound=BaseModel)

Predicate = Callable[[BaseModel], bool]

# Leaf types that cannot hold a ``BaseModel``. :func:`_walk` skips a field
# annotated entirely from these and the containers below.
_SCALAR_LEAVES = (type(None), bool, int, float, complex, str, bytes)

# Containers :func:`_models_in` descends. Nesting only these around scalar
# leaves still cannot reach a model.
_CONTAINER_ORIGINS = (list, tuple, set, frozenset, dict)


def _cannot_hold_a_model(annotation: Any) -> bool:
    """Test whether an annotation rules out reaching a ``BaseModel``.

    The answer is ``True`` only for an annotation built entirely from
    :data:`_SCALAR_LEAVES`, ``Enum`` subclasses, ``Literal`` values of those
    types, and :data:`_CONTAINER_ORIGINS` wrapping the same. ``Any``, a bare
    container, an unresolved forward reference, a ``BaseModel`` subclass and
    every unrecognized class return ``False``.

    Answer ``False`` for any annotation this cannot read. A wrong ``False``
    costs one wasted ``getattr`` per node. A wrong ``True`` silently drops
    those nodes from every traversal in the library, the failure mode
    AGENTS.md records for a hand-maintained field list.
    """
    if annotation is None or annotation is Any:
        return False

    origin = get_origin(annotation)

    if origin is Literal:
        return all(isinstance(arg, _SCALAR_LEAVES) for arg in get_args(annotation))

    if origin is Annotated:
        return _cannot_hold_a_model(get_args(annotation)[0])

    if origin in (Union, UnionType):
        return all(_cannot_hold_a_model(arg) for arg in get_args(annotation))

    if origin in _CONTAINER_ORIGINS:
        args = get_args(annotation)
        if not args:
            return False  # a bare ``list``/``dict`` says nothing about its items
        return all(arg is Ellipsis or _cannot_hold_a_model(arg) for arg in args)

    if origin is not None:
        return False  # some other generic; not worth reasoning about

    if not isinstance(annotation, type):
        return False  # a string annotation or unresolved forward reference

    return annotation in _SCALAR_LEAVES or issubclass(annotation, Enum)


_MODEL_BEARING_FIELDS: dict[type[BaseModel], tuple[str, ...]] = {}


def model_bearing_fields(model: type[BaseModel]) -> tuple[str, ...]:
    """Return the field names of ``model`` whose values can hold a ``BaseModel``.

    Derived from ``model_fields`` annotations once per class and cached, so
    :func:`_walk` skips ``getattr`` and ``_models_in`` on the ``str``,
    ``bool``, ``Literal`` and ``dict[str, str]`` fields that make up much of
    the IR. A new field is classified by its own annotation the first time its
    class is walked, with no hand-maintained list.
    """
    cached = _MODEL_BEARING_FIELDS.get(model)
    if cached is None:
        cached = tuple(
            name
            for name, field in model.model_fields.items()
            if not _cannot_hold_a_model(field.annotation)
        )
        _MODEL_BEARING_FIELDS[model] = cached
    return cached


def walk(
    node: BaseModel,
    predicate: Predicate | None = None,
    *,
    prune: Predicate | None = None,
) -> Iterator[BaseModel]:
    """Yield every ``BaseModel`` descendant of ``node`` (including the root) in depth-first, pre-order.

    Descends list-, tuple- and dict-valued fields to any nesting depth.

    Each object is yielded exactly once, keyed by ``id(node)``. The IR is a
    DAG: ``LetBinding.inner_time_exprs`` and ``inner_tables`` hold the same
    objects that live inside ``rhs_pipeline``, so without deduplication a
    single ``let`` reports ``ago`` and ``now`` twice and occurrence counts
    double. Identity is the key, so two structurally identical nodes written
    at different offsets both surface.

    With ``predicate``, only nodes it returns truthy for are yielded.
    Traversal still descends into a rejected node's subtree, so a skipped
    parent doesn't hide its children. Use it for filters that don't reduce to
    a type, such as "every case-insensitive BinOp".

    ``prune`` limits where the walk goes. A node for which ``prune`` returns
    ``True`` is still yielded when ``predicate`` accepts it, but none of its
    descendants are visited. To analyse an outer pipeline without its
    subqueries::

        walk(ir.main_pipeline, prune=lambda n: isinstance(n, (JoinOp, LookupOp)))

    Example:
        >>> for n in walk(ir):
        ...     ...
        >>> for n in walk(ir, lambda n: isinstance(n, BinOp) and n.case_sensitive is False):
        ...     ...

    Use ``is False`` there. ``BinOp.case_sensitive`` is ``None`` on the
    arithmetic operators, where the question does not apply, and ``not None``
    is true.

    """
    yield from _walk(node, predicate, prune, set())


def _walk(
    node: BaseModel,
    predicate: Predicate | None,
    prune: Predicate | None,
    seen: set[int],
) -> Iterator[BaseModel]:
    """Recursive half of :func:`walk`, threading the visited set.

    Private because a caller who supplied their own visited set could silently
    suppress nodes.

    Re-entering an already-visited object prunes its whole subtree, since a
    second descent can only re-yield what the first produced. Holding ``id()``
    values is safe because the root keeps every descendant alive for the
    traversal's lifetime, so no address is recycled underneath us.

    The set also makes a cycle terminate. The builder produces none, and a
    cycle would break ``model_dump`` and the hash, but the walk ends instead
    of recursing forever if one ever appears.

    Only the fields :func:`model_bearing_fields` reports are read.
    """
    if id(node) in seen:
        return
    seen.add(id(node))
    if predicate is None or predicate(node):
        yield node
    if prune is not None and prune(node):
        return
    for name in model_bearing_fields(type(node)):
        for item in _models_in(getattr(node, name)):
            yield from _walk(item, predicate, prune, seen)


def _models_in(value: object) -> Iterator[BaseModel]:
    """Yield every ``BaseModel`` directly held by ``value``.

    Descends list, tuple and dict containers recursively, so a field typed
    ``list[tuple[Expr, Expr]]`` (``CaseExpr.branches``) is reached as readily
    as a plain ``list[Expr]``. A tuple sitting inside a list is not a
    ``BaseModel``, so unwrapping only one level treats the whole arm as a
    scalar and silently skips it.
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


def find_all(node: BaseModel, type_: type[T], *, prune: Predicate | None = None) -> Iterator[T]:
    """Yield every descendant of ``node`` that is an instance of ``type_``.

    The 90%-case wrapper around :func:`walk`, with ``prune`` passed straight
    through. Custom analyzers typically reduce to one ``find_all`` call plus
    attribute access.

    Example:
        >>> from kustology.ir import find_all, FilterOp
        >>> filters = list(find_all(ir, FilterOp))

    """
    for n in walk(node, prune=prune):
        if isinstance(n, type_):
            yield n


def span_of(node: BaseModel) -> Span | None:
    """Return the smallest span covering ``node`` and every descendant that carries one.

    Works for classes without a ``span`` field (``Pipeline``, ``QueryIR``,
    the statement models) by folding over the spans below them. Zero-width
    spans are ignored. Offsets are code points, like every other ``Span``.
    Leading trivia is not included: the envelope starts at the first token.
    """
    start: int | None = None
    end: int | None = None
    for span in find_all(node, Span):
        if span.width == 0:
            continue
        start = span.text_start if start is None else min(start, span.text_start)
        end = span.text_end if end is None else max(end, span.text_end)
    if start is None or end is None:
        return None
    return Span(text_start=start, width=end - start)
