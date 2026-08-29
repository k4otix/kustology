# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The walk's field cache must never skip a field that can hold a model.

``walk`` reads only the fields ``model_bearing_fields`` reports, derived from
each class's annotations. Misclassifying a field as model-bearing costs one
wasted ``getattr``. Misclassifying it the other way drops nodes from every
traversal in the library, silently, which is the failure mode AGENTS.md
records for a hand-maintained field list.

So the tests below check the classification two ways: against the annotations
directly, for shapes the corpus may not contain, and against real IR built
from every corpus fixture, where every skipped field on every node must in
fact hold no model.
"""

import pathlib

import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel

from kustology import parse
from kustology.ir import QueryIR, walk
from kustology.ir.walk import (
    _cannot_hold_a_model,
    _models_in,
    model_bearing_fields,
)

CORPUS = sorted((pathlib.Path("tests/fixtures/complex_queries")).glob("*.kql"))


@pytest.fixture(scope="module")
def corpus_irs():
    assert CORPUS, "corpus fixtures are missing"
    return [parse(f.read_text()).to_ir(semantic_hash=False) for f in CORPUS]


def _all_ir_classes() -> set[type[BaseModel]]:
    """Every ``BaseModel`` subclass reachable from the IR package."""
    import kustology.ir as ir_pkg

    found = set()
    for name in dir(ir_pkg):
        obj = getattr(ir_pkg, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            found.add(obj)
    return found


@pytest.mark.parametrize(
    "annotation, skippable",
    [
        (str, True),
        (int, True),
        (bool, True),
        (type(None), True),
        (str | None, True),
        (list[str], True),
        (dict[str, str], True),
        (tuple[int, ...], True),
        (list[list[str]], True),
        (QueryIR, False),
        (QueryIR | None, False),
        (list[QueryIR], False),
        (dict[str, QueryIR], False),
        (list[tuple[QueryIR, QueryIR]], False),
        (list, False),
        (dict, False),
        (None, False),
        ("QueryIR", False),
    ],
)
def test_the_classifier_answers_conservatively(annotation, skippable):
    assert _cannot_hold_a_model(annotation) is skippable


def test_any_is_never_skipped():
    from typing import Any

    assert _cannot_hold_a_model(Any) is False
    assert _cannot_hold_a_model(list[Any]) is False


def test_a_literal_of_scalars_is_skippable():
    from typing import Literal

    assert _cannot_hold_a_model(Literal["filter"]) is True
    assert _cannot_hold_a_model(Literal["a", "b"] | None) is True


def test_an_enum_field_is_skippable():
    from kustology.ir import KustoType

    assert _cannot_hold_a_model(KustoType) is True
    assert _cannot_hold_a_model(KustoType | None) is True


def test_every_ir_class_keeps_its_model_valued_fields():
    """A field annotated with a model must never be classified as skippable."""
    for model in _all_ir_classes():
        kept = set(model_bearing_fields(model))
        for name, field in model.model_fields.items():
            annotation = field.annotation
            if annotation is not None and _mentions_a_model(annotation):
                assert name in kept, f"{model.__name__}.{name} would be skipped"


def _mentions_a_model(annotation) -> bool:
    """Test whether a ``BaseModel`` subclass appears anywhere in an annotation."""
    from typing import get_args

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return any(_mentions_a_model(arg) for arg in get_args(annotation))


def test_no_skipped_field_ever_holds_a_model_across_the_corpus(corpus_irs):
    """The empirical half: check real values, not only annotations."""
    checked = 0
    for ir in corpus_irs:
        for node in walk(ir):
            kept = set(model_bearing_fields(type(node)))
            for name in type(node).model_fields:
                if name in kept:
                    continue
                checked += 1
                held = list(_models_in(getattr(node, name)))
                assert not held, (
                    f"{type(node).__name__}.{name} is skipped but holds "
                    f"{[type(h).__name__ for h in held]}"
                )
    assert checked > 0, "no field was skipped, so this test proves nothing"


def test_the_cache_does_not_change_which_nodes_are_walked(corpus_irs):
    """Compare against an uncached walk over every declared field."""

    def _reference(node, seen=None):
        if seen is None:
            seen = set()
        if id(node) in seen:
            return
        seen.add(id(node))
        yield node
        for name in type(node).model_fields:
            for item in _models_in(getattr(node, name)):
                yield from _reference(item, seen)

    for ir in corpus_irs:
        assert [id(n) for n in walk(ir)] == [id(n) for n in _reference(ir)]


def test_the_cache_actually_skips_something(corpus_irs):
    """Guard against the cache degenerating into "every field"."""
    skipped = sum(
        len(type(node).model_fields) - len(model_bearing_fields(type(node)))
        for ir in corpus_irs
        for node in walk(ir)
    )
    assert skipped > 0
