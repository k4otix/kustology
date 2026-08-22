# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""A ``let``-bound scalar used in an expression is not a column.

``let threshold = 5; T | where Count > threshold`` lowered ``threshold`` to a
:class:`~kustology.ir.ColumnRef`, so the IR claimed the query reads two
columns from ``T`` when it reads one and compares it against a query-local
constant. Two things followed:

* ``find_all(ir, ColumnRef)`` — the documented way to ask which columns a
  query touches — reported ``threshold``. Column-level lineage, "does this
  detection read the column we just renamed", and every schema-drift check
  built on it answered wrongly, and the binder had to fail to resolve a
  column that never existed.
* ``_canonicalize_let_names`` renames a ``let`` name to its declaration index
  on the hash's copy, so the local label a query chose cannot change its
  digest. It renames :class:`LetBinding` and :class:`LetRef` and deliberately
  not ``ColumnRef``, since a real column called ``threshold`` *is* a
  different query — so the declaration was canonicalized, the use site was
  not, and ``let n = 5; T | where a > n`` hashed apart from
  ``let m = 5; T | where a > m``. That gap is pinned by
  ``test_ir_builder.test_renaming_a_scalar_let_binding_does_not_change_the_hash``,
  which carried a strict ``xfail`` naming this task.

:class:`LetValueRef` is the node the use site builds instead. It is
deliberately *not* a ``ColumnRef`` subclass: the binder resolves column
provenance by isinstance, and a subclass would inherit that behaviour and
send the binder looking for a column of that name in the scope.
"""

import pytest

from kustology import parse
from kustology.ir import (
    ColumnRef,
    KustoType,
    LetValueRef,
    QueryIR,
    SetMembership,
    find_all,
)

# ``T`` is not in the shared ``sample_schema`` fixture; these tests need a
# bound parse of the brief's own query text, so they carry their own.
_T_SCHEMA = {"T": {"Count": "long", "a": "long"}}


def _ir(query: str, schema: dict | None = None) -> QueryIR:
    return (
        parse(query, schema=schema).to_ir()
        if schema
        else parse(query).to_ir(attach_schema=False)
    )


def _hash(query: str) -> str:
    return _ir(query).semantic_hash


# -- the use site builds its own node -------------------------------------

@pytest.mark.parametrize("schema", [None, _T_SCHEMA], ids=["unbound", "bound"])
def test_a_scalar_let_reference_is_a_let_value_ref(schema):
    ir = _ir("let threshold = 5; T | where threshold < Count", schema)
    predicate = ir.main_pipeline.operators[0].predicate
    assert isinstance(predicate.left, LetValueRef)
    assert predicate.left.name == "threshold"
    assert isinstance(predicate.right, ColumnRef)


@pytest.mark.parametrize("schema", [None, _T_SCHEMA], ids=["unbound", "bound"])
def test_find_all_column_ref_no_longer_reports_the_let_name(schema):
    """The lineage question, asked the documented way, in both bind states."""
    ir = _ir("let threshold = 5; T | where Count > threshold", schema)
    assert {c.name for c in find_all(ir, ColumnRef)} == {"Count"}
    assert [r.name for r in find_all(ir, LetValueRef)] == ["threshold"]


def test_a_let_value_ref_is_not_a_column_ref():
    """Pinned as a type relationship, not just an observed name set: making
    ``LetValueRef`` a ``ColumnRef`` subclass would satisfy every ``isinstance``
    the binder runs and quietly reinstate the bug."""
    assert not issubclass(LetValueRef, ColumnRef)


def test_a_let_bound_list_in_a_membership_test_is_a_let_value_ref():
    ir = _ir("let list = dynamic([1]); T | where a in (list)")
    (membership,) = find_all(ir, SetMembership)
    assert isinstance(membership.values[0], LetValueRef)
    assert membership.values[0].name == "list"
    assert isinstance(membership.column, ColumnRef)


def test_a_real_column_of_the_same_name_is_still_a_column_ref():
    """The boundary. Without a ``let`` declaring it, ``threshold`` is a
    column and must stay one."""
    ir = _ir("T | where Count > threshold")
    assert {c.name for c in find_all(ir, ColumnRef)} == {"Count", "threshold"}
    assert list(find_all(ir, LetValueRef)) == []


def test_only_bindings_declared_earlier_produce_a_let_value_ref():
    """``self._let_names`` is populated in declaration order, so a name bound
    *later* is not a reference to it -- the same rule ``LetRef`` follows at
    source position (``test_let_bindings.py``)."""
    ir = _ir("let early = later + 1; let later = 5; T | where a > early")
    (rhs,) = [b.rhs_expr for b in ir.let_bindings if b.name == "early"]
    assert isinstance(rhs.left, ColumnRef)
    assert rhs.left.name == "later"
    assert [r.name for r in find_all(ir, LetValueRef)] == ["early"]


# -- the hash --------------------------------------------------------------

def test_renaming_a_scalar_let_binding_no_longer_changes_the_hash():
    """The collision the node exists to restore. ``n`` and ``m`` are local
    labels; the queries are the same query."""
    assert _hash("let n = 5; T | where a > n") == _hash("let m = 5; T | where a > m")


def test_a_let_scalar_and_a_real_column_still_hash_apart():
    """The near-miss the ``ColumnRef`` lowering was protecting: reading a
    column named ``n`` is not comparing against a constant called ``n``."""
    assert _hash("let n = 5; T | where a > n") != _hash("T | where a > n")


def test_which_binding_is_referenced_is_still_hashed():
    """The rename is positional, so pointing at the first binding and
    pointing at the second stay different queries."""
    assert (
        _hash("let p = 5; let q = 6; T | where a > p")
        != _hash("let p = 5; let q = 6; T | where a > q")
    )


def test_a_let_reference_in_a_later_statement_is_renamed_too():
    a = _hash("let n = 5; T | count; U | where a > n")
    b = _hash("let m = 5; T | count; U | where a > m")
    assert a == b


def test_a_let_reference_inside_another_bindings_rhs_is_renamed():
    a = _hash("let n = 5; let big = n * 2; T | where a > big")
    b = _hash("let x = 5; let y = x * 2; T | where a > y")
    assert a == b


# -- surface ----------------------------------------------------------------

def test_canonical_form_renders_the_name():
    ir = _ir("let threshold = 5; T | where Count > threshold")
    predicate = ir.main_pipeline.operators[0].predicate
    assert predicate.canonical_form == "Count > threshold"


def test_json_round_trip_keeps_the_node_type():
    ir = _ir("let threshold = 5; T | where Count > threshold")
    reloaded = QueryIR.model_validate_json(ir.model_dump_json())
    assert reloaded.model_dump() == ir.model_dump()
    (ref,) = find_all(reloaded, LetValueRef)
    assert isinstance(ref, LetValueRef)
    assert ref.name == "threshold"


def test_the_binder_types_it_from_the_parser_not_from_the_column_scope():
    """``map_semantic_info`` copies the .NET ``ResultType``, which on a bound
    parse knows ``threshold`` is a ``long``. ``SchemaAttacher`` must not try
    to place it as a column: a non-default ``result_type`` with no ``table``
    field at all is what says the typing came from the right place."""
    ir = _ir("let threshold = 5; T | where Count > threshold", _T_SCHEMA)
    (ref,) = find_all(ir, LetValueRef)
    assert ref.result_type == KustoType.LONG
    assert "table" not in type(ref).model_fields


def test_llm_view_drops_the_redundant_canonical_form():
    """Same rule as ``ColumnRef``: on a leaf whose canonical form is its own
    name, the extra key is noise in the model's context window."""
    from kustology.ir import to_llm_dict

    ir = _ir("let threshold = 5; T | where Count > threshold")
    (ref,) = find_all(ir, LetValueRef)
    view = to_llm_dict(ref)
    assert view["kind"] == "let_value_ref"
    assert view["name"] == "threshold"
    assert "canonical_form" not in view
