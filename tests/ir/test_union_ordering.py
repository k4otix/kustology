# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every ``Pipeline`` union member must round-trip back to its own class.

``Pipeline.source``, ``Pipeline.operators``, ``SearchOp.tables``,
``FindOp.tables``, and ``expr.AnyExpr`` are each
``Field(discriminator="kind")``, and every member carries a unique
``kind: Literal[...]``, so member order is cosmetic and a kind-less or
mismatched payload is rejected by name (see
``test_operator_payload_without_kind_is_rejected_with_a_discriminator_error``
below). Pydantic's default "smart" mode ties by shape instead: it prefers a
defaulted-fields class over a true fields-less one for a ``span`` + ``kind``
payload, so the position of every member would matter, and a kind-less
``And``/``Or`` payload (both just ``span`` + ``operands``) would silently
validate as whichever came first in the ``Union``.

A discriminator changes how a payload is matched to a class, so the membership
assertions carry the rest: without them a class added to the union goes
unexercised, and a mis-declared ``Literal`` or a ``FilterOp`` payload
validating as a fields-less ``GetSchemaOp`` with the predicate dropped both
pass the suite while the IR is wrong.
"""

from __future__ import annotations

import types
from enum import Enum
from typing import Annotated, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from kustology.ir import expr as E
from kustology.ir import query as Q
from kustology.ir.spans import Span

_SPAN = Span(text_start=0, width=1)


def _unwrap(annotation):
    """Strip ``Annotated[...]`` down to the type it decorates."""
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _union_members(annotation) -> tuple[type, ...]:
    annotation = _unwrap(annotation)
    origin = get_origin(annotation)
    assert origin in (Union, types.UnionType), f"not a union: {annotation!r}"
    return tuple(a for a in get_args(annotation) if a is not type(None))


def _sample(annotation):
    """Build one valid value for ``annotation``.

    A hand-written table drifts the moment a field is added, so this stays
    generic: a model change cannot slip past it.

    Every field is filled, including the defaulted ones. A payload made
    entirely of defaults (``{"kind": …, "span": …, "predicate": null,
    "tables": []}``) is the shape a smart-mode union cannot tell apart from a
    fields-less class's. Non-default optional values also go through the
    round-trip, where a mis-declared ``Literal`` or a container type the
    validator coerces shows up.
    """
    annotation = _unwrap(annotation)
    if annotation is Span:
        return _SPAN
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return next(iter(annotation))
    # Break the two cycles by hand. ``AnyExpr``'s first member is ``BinOp``,
    # whose operands are ``AnyExpr`` again; ``Pipeline`` nests through
    # ``JoinOp.right`` and friends.
    if annotation is Q.Pipeline:
        return Q.Pipeline(source=Q.ImplicitSource(span=_SPAN), operators=[])
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (Union, types.UnionType):
        members = _union_members(annotation)
        if E.StarExpr in members:
            return E.StarExpr(span=_SPAN)
        return _sample(members[0])
    if origin in (list, set, frozenset):
        return [_sample(get_args(annotation)[0])]
    if origin is dict:
        return {"k": _sample(get_args(annotation)[1])}
    if origin is tuple:
        return tuple(_sample(a) for a in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation(**{
            name: _sample(f.annotation)
            for name, f in annotation.model_fields.items()
        })
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    return "x"


# One instance of every ``Pipeline.source`` member with a non-default payload
# where it has one; a defaults-only sample cannot tell two classes apart.
SOURCE_SAMPLES: tuple[BaseModel, ...] = (
    Q.TableRef(name="T", database="d", cluster="c", is_wildcard=True, span=_SPAN),
    Q.LetRef(name="X", span=_SPAN),
    Q.FuncCallSource(name="f", args=[E.StarExpr(span=_SPAN)], span=_SPAN),
    Q.DataTableSource(
        columns=[("a", "long")],
        rows=[[E.LiteralExpr(value=1, literal_kind="long", span=_SPAN)]],
        span=_SPAN,
    ),
    Q.ExternalDataSource(
        columns=[("a", "string")], uris=["https://x"], format="csv", span=_SPAN,
    ),
    Q.ImplicitSource(span=_SPAN),
    Q.UnknownSource(raw_text="T | weird", span=_SPAN),
    Q.Pipeline(source=Q.TableRef(name="Inner", span=_SPAN), operators=[]),
)


def _operator_subclasses(cls: type = Q.Operator) -> set[type]:
    out: set[type] = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _operator_subclasses(sub)
    return out


OPERATOR_SAMPLES: tuple[BaseModel, ...] = tuple(
    _sample(cls) for cls in sorted(_operator_subclasses(), key=lambda c: c.__name__)
)


def test_every_source_union_member_has_a_sample():
    declared = set(_union_members(Q.Pipeline.model_fields["source"].annotation))
    assert {type(s) for s in SOURCE_SAMPLES} == declared


def test_every_operator_union_member_has_a_sample():
    element = get_args(_unwrap(Q.Pipeline.model_fields["operators"].annotation))[0]
    declared = set(_union_members(element))
    assert {type(s) for s in OPERATOR_SAMPLES} == declared


@pytest.mark.parametrize("sample", SOURCE_SAMPLES, ids=lambda s: type(s).__name__)
def test_source_round_trips_to_its_own_class(sample):
    pipeline = Q.Pipeline(source=sample, operators=[])
    back = Q.Pipeline.model_validate(pipeline.model_dump())
    assert type(back.source) is type(sample)
    assert back.source == sample
    from_json = Q.Pipeline.model_validate_json(pipeline.model_dump_json())
    assert type(from_json.source) is type(sample)


@pytest.mark.parametrize("sample", OPERATOR_SAMPLES, ids=lambda s: type(s).__name__)
def test_operator_round_trips_to_its_own_class(sample):
    pipeline = Q.Pipeline(source=Q.ImplicitSource(span=_SPAN), operators=[sample])
    back = Q.Pipeline.model_validate(pipeline.model_dump())
    assert type(back.operators[0]) is type(sample)
    assert back.operators[0] == sample
    from_json = Q.Pipeline.model_validate_json(pipeline.model_dump_json())
    assert type(from_json.operators[0]) is type(sample)


def test_operator_payload_without_kind_is_rejected_with_a_discriminator_error():
    """A shape-based ("smart") union absorbs a kind-less payload into the first
    structurally compatible class; a discriminated union refuses it by name."""
    import pydantic
    import pytest
    payload = {"source": {"kind": "implicit_source", "span": {"text_start": 0, "width": 0}},
               "operators": [{"span": {"text_start": 0, "width": 0}}]}
    with pytest.raises(pydantic.ValidationError, match="kind"):
        Q.Pipeline.model_validate(payload)
