# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import types
from pathlib import Path
from typing import Annotated, ForwardRef, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

import kustology.ir as ir_pkg
from kustology import parse
from kustology.ir import JoinOp, LetFunction, Pipeline, Span, find_all, span_of
from kustology.ir import expr as E
from kustology.ir import query as Q

CORPUS = sorted((Path(__file__).resolve().parent.parent / "fixtures" / "complex_queries").glob("*.kql"))


def _ir(q):
    return parse(q).to_ir(semantic_hash=False)


def test_pipeline_envelope_excludes_the_let_statement():
    q = "let n = 5;\nT | where a > n | take 1"
    assert span_of(_ir(q).main_pipeline).text(q) == "T | where a > n | take 1"


def test_query_envelope_covers_the_whole_text():
    q = "let n = 5;\nT | where a > n | take 1"
    assert span_of(_ir(q)).text(q) == q


def test_join_subquery_pipeline_text():
    q = "T | join (S | where b == 1) on x"
    inner = next(find_all(next(find_all(_ir(q), JoinOp)), Pipeline))
    assert span_of(inner).text(q) == "S | where b == 1"


def test_node_with_its_own_span_returns_that_span():
    ir = _ir("T | where a > 1")
    op = ir.main_pipeline.operators[0]
    assert span_of(op) == op.span


def test_let_function_envelope_includes_the_body():
    q = "let f = (x: long) { T | where a > x };\nf()"
    fn = next(find_all(_ir(q), LetFunction))
    assert "T | where a > x" in span_of(fn).text(q)


def test_no_span_gives_none():
    assert span_of(Span(text_start=0, width=0)) is None


def _resolve(annotation):
    """Return the class a forward reference names.

    Python 3.10 leaves the string inside ``list["Pipeline"]`` unevaluated in
    ``model_fields[...].annotation``; 3.11 and later hand back the class. A
    string read as an opaque object would answer "no ``Span`` here" for a
    field that holds one, so close the reference or fail.
    """
    if isinstance(annotation, ForwardRef):
        annotation = annotation.__forward_arg__
    if isinstance(annotation, str):
        for module in (Q, E):
            resolved = getattr(module, annotation, None)
            if resolved is not None:
                return resolved
        raise AssertionError(f"forward reference names nothing in the IR: {annotation!r}")
    return annotation


def _mentions_span(annotation) -> bool:
    """Test whether ``Span`` appears anywhere in ``annotation``."""
    if get_origin(annotation) is Literal:
        return False  # its arguments are values, not types
    annotation = _resolve(annotation)
    if annotation is Span:
        return True
    return any(_mentions_span(arg) for arg in get_args(annotation) if arg is not Ellipsis)


def _holds_span_directly(annotation) -> bool:
    """Test whether the field's own value can be a ``Span``.

    True for ``Span``, ``Span | None`` and ``Annotated[Span, …]``. False when
    a ``Span`` is only reachable through a container, such as ``list[Span]``,
    where the field's value is the list.
    """
    annotation = _resolve(annotation)
    origin = get_origin(annotation)
    if origin is Annotated:
        return _holds_span_directly(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return any(_holds_span_directly(arg) for arg in get_args(annotation))
    return annotation is Span


def _ir_model_classes() -> set[type[BaseModel]]:
    """Return every ``BaseModel`` subclass the IR package exposes."""
    return {
        obj
        for name in dir(ir_pkg)
        if isinstance(obj := getattr(ir_pkg, name), type) and issubclass(obj, BaseModel)
    }


def test_no_ir_model_holds_a_span_inside_a_container():
    """A ``Span`` is always the field's own value, never an element of one.

    ``retarget_spans_to_codepoints`` rebinds each ``Span`` on the field that
    holds it, so it reads model fields and installs a replacement there. A
    ``list[Span]`` field would convert nothing: the field's value is the list,
    and the spans inside it would keep Microsoft's UTF-16 offsets with no
    error, on non-BMP input only.

    The invariant is a property of the annotations, so assert it there rather
    than inferring it from the shapes the corpus happens to produce.
    """
    direct, container = [], []
    for cls in _ir_model_classes():
        for name, field in cls.model_fields.items():
            if not _mentions_span(field.annotation):
                continue
            target = direct if _holds_span_directly(field.annotation) else container
            target.append(f"{cls.__name__}.{name}: {field.annotation}")

    assert not container, "a Span reachable only through a container: " + ", ".join(container)
    assert direct, "found no Span-typed field at all, so this proves nothing"


@pytest.mark.parametrize("path", CORPUS, ids=lambda p: p.stem)
def test_every_span_converts_when_an_astral_character_shifts_the_offsets(path):
    """Every span in the IR carries code-point offsets, wherever the tree holds it.

    The retarget pass reads model fields, so a span reachable any other way
    keeps Microsoft's UTF-16 offsets and slices the wrong characters. An
    astral character ahead of the query makes every UTF-16 offset one larger
    than its code-point twin, which is what exposes a skipped span.

    A comment is trivia, so the two IRs have the same nodes in the same walk
    order and their spans pair up positionally.
    """
    plain = path.read_text(encoding="utf-8")
    shifted = f"// \U0001F600\n{plain}"
    plain_spans = list(find_all(parse(plain).to_ir(attach_schema=False), Span))
    shifted_spans = list(find_all(parse(shifted).to_ir(attach_schema=False), Span))

    assert plain_spans, "expected the IR to carry spans"
    assert len(shifted_spans) == len(plain_spans)
    assert [s.text(shifted) for s in shifted_spans] == [s.text(plain) for s in plain_spans]
