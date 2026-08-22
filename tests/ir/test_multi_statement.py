# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every tabular statement of a multi-statement query survives the lowering.

KQL lets a query hold more than one tabular statement, separated by ``;``::

    T | count; U | count

The builder read ``expr_stmts[0]`` and stopped, so everything after the first
semicolon was discarded: ``T | count; U | count`` built exactly the IR of
``T | count``, carried its ``semantic_hash``, and nothing in the second
statement was reachable through ``walk``/``find_all``. That is the lossy
lowering shape AGENTS.md describes — the node is fully populated, so nothing
looks stubbed, while two different queries produce identical IR.

``QueryIR.additional_pipelines`` holds the second and later statements in
source order. The hash payload names it explicitly (``compute_semantic_hash``
builds a ``{let_bindings, main_pipeline, additional_pipelines}`` dict rather
than dumping the whole model), so a field left out of that dict would be
invisible to the digest however well the builder populated it — which is what
the hash tests below pin.
"""

import pytest

from kustology import parse
from kustology.ir import (
    ColumnRef,
    CountOp,
    FilterOp,
    QueryIR,
    TakeOp,
    compute_semantic_hash,
    find_all,
)


def _ir(query: str, **kwargs) -> QueryIR:
    return parse(query, **kwargs).to_ir(attach_schema=False)


def _hash(query: str) -> str:
    return _ir(query).semantic_hash


# -- the statements are kept, in order ------------------------------------

def test_the_second_tabular_statement_is_kept():
    ir = _ir("T | count; U | count")
    assert ir.main_pipeline.source.name == "T"
    assert len(ir.additional_pipelines) == 1
    assert ir.additional_pipelines[0].source.name == "U"


def test_statements_are_kept_in_source_order():
    ir = _ir("T | count; U | take 1; V | where x == 1")
    assert ir.main_pipeline.source.name == "T"
    assert [p.source.name for p in ir.additional_pipelines] == ["U", "V"]
    assert [type(p.operators[0]) for p in ir.additional_pipelines] == [
        TakeOp, FilterOp,
    ]


def test_a_single_statement_query_has_no_additional_pipelines():
    """The boundary: one statement must not grow a spurious second entry.

    ``let`` statements are not tabular statements, so a query with three of
    them still has exactly one pipeline.
    """
    assert _ir("T | count").additional_pipelines == []
    assert _ir("let a = 5; let b = 6; T | where x > a").additional_pipelines == []


# -- the hash responds ----------------------------------------------------

def test_a_second_statement_changes_the_hash():
    """The collision the field exists to close."""
    assert _hash("T | count; U | count") != _hash("T | count")


def test_queries_differing_only_in_the_second_statement_hash_apart():
    """Proof that ``additional_pipelines`` reaches the payload.

    Both queries have byte-identical first statements, so a payload dict that
    names only ``let_bindings`` and ``main_pipeline`` produces one digest for
    the pair however faithfully the builder filled the new field.
    """
    assert _hash("T | count; U | count") != _hash("T | count; V | count")
    assert _hash("T | count; U | take 1") != _hash("T | count; U | take 2")


def test_statement_order_is_hashed():
    """``T | count; U | take 1`` and ``U | take 1; T | count`` are different
    queries — the last statement is the one whose result the caller gets."""
    assert _hash("T | count; U | take 1") != _hash("U | take 1; T | count")


def test_compute_semantic_hash_agrees_with_the_stored_field():
    """``QueryIR.semantic_hash`` is computed at build time and
    ``compute_semantic_hash`` is what consumers call after mutating the IR;
    the two must not disagree about the new field."""
    ir = _ir("T | count; U | count")
    assert compute_semantic_hash(ir) == ir.semantic_hash


# -- the subtree is reachable ---------------------------------------------

def test_walk_reaches_into_a_later_statement():
    """``find_all`` is the documented traversal; an analyzer built on it was
    blind to everything past the first semicolon."""
    ir = _ir("T | count; U | where FileName == 'cmd.exe'")
    assert [f.name for f in find_all(ir, ColumnRef)] == ["FileName"]
    assert len(list(find_all(ir, FilterOp))) == 1
    assert len(list(find_all(ir, CountOp))) == 1


def test_json_round_trip_keeps_every_statement():
    ir = _ir("T | count; U | take 1; V | where x == 1")
    reloaded = QueryIR.model_validate_json(ir.model_dump_json())
    assert reloaded.model_dump() == ir.model_dump()
    assert [p.source.name for p in reloaded.additional_pipelines] == ["U", "V"]


def test_the_binder_enriches_a_later_statement(sample_schema):
    """A later statement is a real pipeline, so the schema pass must reach it
    — otherwise the same column resolves in statement one and not in two."""
    ir = parse(
        "DeviceProcessEvents | count; "
        "DeviceFileEvents | where FileName == 'cmd.exe'",
        schema=sample_schema,
    ).to_ir()
    (second,) = ir.additional_pipelines
    assert second.result_schema is not None
    (col,) = find_all(second, ColumnRef)
    assert col.table == "DeviceFileEvents"
    assert col.result_type.value == "string"


# -- a later statement sees the query's let bindings ----------------------

def test_a_let_reference_in_a_later_statement_is_canonicalized():
    """``_canonicalize_let_names`` rewrites ``let`` names on the hash's copy.
    It walked the main pipeline only, so a reference from statement two kept
    its source-level name and the rename stopped being a rename."""
    a = _hash("let X = T | take 1; T | count; X | count")
    b = _hash("let Y = T | take 1; T | count; Y | count")
    assert a == b


@pytest.mark.parametrize("query", [
    "T | count; U | count",
    "let a = 5; T | where x > a; U | count",
])
def test_llm_view_renders_later_statements(query):
    """``to_llm_dict`` derives from ``model_fields``, so the field appears
    without a per-field rule — pinned so a future view change cannot drop it
    silently."""
    view = _ir(query).to_llm_dict()
    assert len(view["additional_pipelines"]) == 1
