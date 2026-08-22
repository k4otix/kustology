# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Operator parameters: the modifiers that change what an operator does.

Every assertion here runs on a real parse. The pattern is a value assertion
on a non-default parameter plus a hash pair, because the two failures this
family produces are different: a field that is never populated reads as
implemented, and a parameter the IR does not carry at all makes two
different queries share one ``semantic_hash``.

The effective-default cases (``join``, ``lookup``, ``union kind``,
``parse kind``) are always written as a *pair* -- the bare form against the
explicitly-written default, which must hash alike, and the bare form against
a different written value, which must not. Asserting only the first would
pass on a builder that recorded nothing at all.
"""

from __future__ import annotations

from kustology import parse
from kustology.ir import TypedNameDecl


def _ir(query: str):
    return parse(query).to_ir(attach_schema=False)


def _ops(query: str):
    return _ir(query).main_pipeline.operators


def _hash(query: str) -> str:
    return _ir(query).semantic_hash


# -- typed captures (D13) -------------------------------------------------

def test_parse_typed_capture_is_a_typed_name_decl():
    (op,) = _ops("T | parse a with 'x' b:long")
    decl = op.patterns[1]
    assert isinstance(decl, TypedNameDecl)
    assert decl.name == "b"
    assert decl.declared_type == "long"


def test_typed_capture_renders_name_and_type():
    (op,) = _ops("T | parse a with 'x' b:long")
    assert op.patterns[1].canonical_form == "b:long"


def test_typed_capture_differs_from_untyped_capture():
    assert _hash("T | parse a with 'x' b:long") != _hash("T | parse a with 'x' b")


def test_capture_type_reaches_the_hash():
    assert _hash("T | parse a with 'x' b:long") != _hash("T | parse a with 'x' b:string")


def test_typed_capture_gives_the_binder_the_declared_type():
    """The declared type is the column's type -- nothing to infer.

    Untyped captures still default to ``string`` (KQL's rule), so this
    asserts the typed branch specifically.
    """
    ir = parse(
        "T | parse a with 'x' b:long", schema={"T": {"a": "string"}},
    ).to_ir()
    assert ir.main_pipeline.result_schema.columns["b"] == "long"
