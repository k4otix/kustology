# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``semantic_hash`` must not depend on whether a schema was supplied.

``QueryIR.semantic_hash`` memoizes on first read, which can land before or
after ``SchemaAttacher`` runs, so bind-invariance cannot rest on ordering. It
rests on ``compute_semantic_hash`` calling ``_clear_volatile`` first: every
field the binder populates has to be volatile, or one query text hashes two
ways.

The accepted exception is the ``let``-aliases-a-table divergence pinned in
``test_let_bindings.py``. That changes which node the builder emits, so
field-stripping cannot reach it.
"""

import pytest

from kustology import parse
from kustology.ir import ColumnRef, compute_semantic_hash, find_all

SCHEMA = {
    "T": {"a": "string", "b": "string", "k": "string"},
    "U": {"b": "string", "k": "string"},
    "SecurityEvent": {"Account": "string", "EventID": "int"},
}


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("T | where a == 'x' | project a, k", id="plain-table"),
        pytest.param(
            "let Base = SecurityEvent | where EventID > 4624;\n"
            "Base | where Account != ''",
            id="let-alias",
        ),
        pytest.param("T | join U on $left.a == $right.b", id="join"),
        pytest.param(
            "let f = (X:(*)) { X | where a > 1 | project a }; T | count",
            id="let-function-tabular-param",
        ),
        pytest.param(
            "let f = (n:long) { "
            "let Filtered = T | where a > n; Filtered | project a "
            "}; T | count",
            id="let-function-body-lets",
        ),
        pytest.param(
            "let f = (T:long) { U | where b > T }; T | where a > 1",
            id="let-function-scalar-param-collides-with-table",
        ),
    ],
)
def test_recomputed_hash_is_the_same_bound_and_unbound(query):
    """Refreshing the hash, as ``semantic_hash`` advises, must not make it
    depend on whether a schema was passed."""
    bound = parse(query, schema=SCHEMA).to_ir()
    unbound = parse(query).to_ir()

    assert compute_semantic_hash(bound) == compute_semantic_hash(unbound)


def test_join_side_is_recorded_separately_from_resolved_table():
    """``table`` cannot carry the join side, so the side gets its own field.

    ``table`` never holds the ``$left`` / ``$right`` syntax: unresolved it is
    ``None``, and resolving overwrites it with the concrete table.
    """
    query = "T | join U on $left.a == $right.b"

    unbound = {c.name: c for c in find_all(parse(query).to_ir(), ColumnRef)}
    bound = {
        c.name: c
        for c in find_all(parse(query, schema=SCHEMA).to_ir(), ColumnRef)
    }

    assert unbound["a"].join_side == "left"
    assert unbound["b"].join_side == "right"
    # Survives binding, even though `table` itself is rewritten to T / U.
    assert bound["a"].join_side == "left"
    assert bound["b"].join_side == "right"
    assert (bound["a"].table, bound["b"].table) == ("T", "U")


def test_join_side_keeps_semantically_different_join_keys_apart():
    """``table`` is volatile, so the side is what keeps these two apart.

    ``$left.a == $left.b`` compares two columns of the left table; ``$left.a
    == $right.b`` is a join key. Nothing else in the IR distinguishes them.
    """
    same_side = parse("T | join U on $left.a == $left.b").to_ir()
    across = parse("T | join U on $left.a == $right.b").to_ir()

    assert compute_semantic_hash(same_side) != compute_semantic_hash(across)


def test_a_column_named_table_is_not_erased_from_the_hash():
    """Volatile fields are stripped by model field, so a key name is safe.

    ``AssertSchemaOp`` carries its declaration as ``dict[str, str]``, so a
    column a query names ``table`` is a plain key in the dumped JSON. A strip
    that deleted every ``table`` / ``span`` / ``result_schema`` key by name
    would hash ``(a:long, table:long)`` the same as ``(a:long)``, two
    different assertions about the data.
    """
    with_extra = parse("T | assert-schema (a:long, table:long)").to_ir()
    without = parse("T | assert-schema (a:long)").to_ir()

    # The column really is in the IR; only the hash payload lost it.
    assert with_extra.main_pipeline.operators[0].columns == {
        "a": "long", "table": "long",
    }
    assert without.main_pipeline.operators[0].columns == {"a": "long"}

    assert compute_semantic_hash(with_extra) != compute_semantic_hash(without)
