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


def test_typed_capture_is_reachable_through_find_all():
    from kustology.ir import find_all
    ir = _ir("T | parse a with 'x' b:long")
    assert [d.declared_type for d in find_all(ir, TypedNameDecl)] == ["long"]


def test_typed_capture_gives_the_binder_the_declared_type():
    """The declared type is the column's type -- nothing to infer.

    Untyped captures still default to ``string`` (KQL's rule), so this
    asserts the typed branch specifically.
    """
    ir = parse(
        "T | parse a with 'x' b:long", schema={"T": {"a": "string"}},
    ).to_ir()
    assert ir.main_pipeline.result_schema.columns["b"] == "long"


# -- mv-expand ------------------------------------------------------------

_MV_ALL = (
    "T | mv-expand kind=array with_itemindex=i bagexpansion=bag "
    "a to typeof(string) limit 10"
)


def test_mv_expand_records_the_expanded_column():
    (op,) = _ops(_MV_ALL)
    (col,) = op.columns
    assert col.expression.name == "a"


def test_mv_expand_records_the_declared_element_type():
    (op,) = _ops(_MV_ALL)
    assert op.columns[0].to_typeof == "string"


def test_mv_expand_records_the_row_limit():
    (op,) = _ops(_MV_ALL)
    assert op.row_limit == 10


def test_mv_expand_records_with_itemindex():
    (op,) = _ops(_MV_ALL)
    assert op.with_item_index == "i"


def test_mv_expand_records_bagexpansion():
    (op,) = _ops(_MV_ALL)
    assert op.bag_expansion == "bag"


def test_mv_expand_records_its_kind():
    (op,) = _ops(_MV_ALL)
    assert op.expand_kind == "array"


def test_mv_expand_modifiers_all_reach_the_hash():
    """Each modifier alone must move the digest, and off a common base.

    Comparing the four spellings pairwise is what catches a field that is
    modelled but not hashed: three of them could still collapse onto the
    bare form if only one pair were checked.
    """
    variants = {
        "bare": _hash("T | mv-expand a"),
        "typed": _hash("T | mv-expand a to typeof(string)"),
        "limited": _hash("T | mv-expand a limit 10"),
        "indexed": _hash("T | mv-expand with_itemindex=i a"),
        "bagged": _hash("T | mv-expand bagexpansion=bag a"),
        "kinded": _hash("T | mv-expand kind=array a"),
    }
    assert len(set(variants.values())) == len(variants), variants


def test_mv_expand_element_type_is_a_value_not_a_flag():
    assert _hash("T | mv-expand a to typeof(string)") != _hash(
        "T | mv-expand a to typeof(long)"
    )


def test_mv_expand_still_binds_the_expanded_column():
    ir = parse(
        "T | mv-expand a | where a == 'x'", schema={"T": {"a": "dynamic"}},
    ).to_ir()
    assert "a" in ir.main_pipeline.result_schema.columns
