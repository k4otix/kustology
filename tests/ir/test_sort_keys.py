# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Ordering keys: ``sort by`` / ``order by`` / ``top … by``.

``SortOp.expressions`` and ``TopOp.by`` used to hold bare expressions, so
``sort by x asc`` and ``sort by x desc`` — which return rows in opposite
orders — built byte-identical IR and collided under ``semantic_hash``. The
direction and the ``nulls first`` / ``nulls last`` clause were read off the
AST's ``OrderedExpression`` wrapper and thrown away when the builder unwrapped
it.

Two properties are pinned here that are easy to conflate:

* **The recorded value on an explicit query.** ``asc``, ``desc``, ``nulls
  first`` and ``nulls last`` each have a case that asserts the written value,
  so a builder that hardcoded one answer fails.
* **The effective default on a bare query.** ``sort by x`` sorts *descending*
  in KQL, so ``direction`` is ``"desc"`` there and the two queries must hash
  alike. That is an assertion about KQL's semantics rather than about a
  pydantic default — which is why ``SortKey.direction`` is declared required
  with no default at all: a defaulted field would be dropped from
  ``to_llm_dict``'s output and a reader could not tell ``desc`` was in force.
"""

import pytest

from kustology import parse
from kustology.ir import QueryIR, SortKey, SortOp, TopOp


def _hash(query: str) -> str:
    return parse(query).to_ir().semantic_hash


def _sort_keys(query: str) -> list[SortKey]:
    ir = parse(query).to_ir()
    (op,) = (o for o in ir.main_pipeline.operators if isinstance(o, SortOp))
    return op.expressions


def _top_key(query: str) -> SortKey:
    ir = parse(query).to_ir()
    (op,) = (o for o in ir.main_pipeline.operators if isinstance(o, TopOp))
    return op.by


# -- recorded values on explicit queries ---------------------------------

def test_sort_records_direction_and_nulls_per_key():
    """Each key carries its own modifiers; the second key's ``asc`` must not
    be contaminated by the first key's ``desc nulls first``."""
    keys = _sort_keys("T | sort by x desc nulls first, y asc")
    assert all(isinstance(k, SortKey) for k in keys), [type(k).__name__ for k in keys]
    assert [(k.expression.name, k.direction, k.nulls) for k in keys] == [
        ("x", "desc", "first"),
        ("y", "asc", None),
    ]


def test_explicit_asc_is_recorded():
    """The non-default direction, asserted on a real parse."""
    (key,) = _sort_keys("T | sort by x asc")
    assert key.direction == "asc"
    assert key.nulls is None


def test_nulls_last_is_recorded():
    (key,) = _sort_keys("T | sort by x asc nulls last")
    assert (key.direction, key.nulls) == ("asc", "last")


def test_nulls_clause_without_a_direction_keeps_the_effective_default():
    """``nulls first`` is grammatically independent of ``asc``/``desc``:
    Kusto.Language builds an ``OrderingClause`` whose ``AscOrDescKeyword`` is
    ``None``. The key still sorts descending."""
    (key,) = _sort_keys("T | sort by x nulls first")
    assert (key.direction, key.nulls) == ("desc", "first")


def test_sort_key_expression_can_be_a_whole_call():
    """The ordering expression is not restricted to a column."""
    from kustology.ir import FuncCall

    (key,) = _sort_keys("T | sort by strlen(x) desc")
    assert isinstance(key.expression, FuncCall)
    assert key.expression.name == "strlen"
    assert key.direction == "desc"


# -- effective defaults ---------------------------------------------------

def test_bare_sort_key_carries_kqls_effective_default_direction():
    """``sort by x`` is descending in KQL. The AST does not wrap a bare key
    in an ``OrderedExpression`` at all, so the default is supplied by the
    builder rather than read from an ordering clause."""
    (key,) = _sort_keys("T | sort by x")
    assert key.direction == "desc"
    assert key.nulls is None


def test_bare_top_by_carries_the_effective_default_direction():
    key = _top_key("T | top 5 by x")
    assert key.direction == "desc"


# -- top ------------------------------------------------------------------

def test_top_by_is_a_sort_key_with_its_direction():
    key = _top_key("T | top 5 by x desc")
    assert isinstance(key, SortKey), type(key).__name__
    assert key.expression.name == "x"
    assert key.direction == "desc"


def test_top_by_records_explicit_asc():
    key = _top_key("T | top 5 by x asc")
    assert key.direction == "asc"


def test_top_by_records_nulls_clause():
    key = _top_key("T | top 5 by x asc nulls last")
    assert (key.direction, key.nulls) == ("asc", "last")


# -- binding --------------------------------------------------------------

def test_binder_reaches_expressions_through_the_new_wrapper():
    """``SchemaAttacher._fill_children`` descends ``model_fields`` rather than
    a hardcoded attribute tuple, so interposing ``SortKey`` between the
    operator and its expression must not hide the expression from the binder.
    Asserted on a bound parse with non-default values on both sides: the
    column's provenance *and* its type."""
    schema = {"T": {"x": "string", "n": "long"}}
    ir = parse("T | sort by x desc, n asc | top 3 by n asc", schema=schema).to_ir()
    assert ir.schema_attached
    (sort_op,) = (o for o in ir.main_pipeline.operators if isinstance(o, SortOp))
    (top_op,) = (o for o in ir.main_pipeline.operators if isinstance(o, TopOp))
    assert [
        (k.expression.table, k.expression.result_type.value) for k in sort_op.expressions
    ] == [("T", "string"), ("T", "long")]
    assert (top_op.by.expression.table, top_op.by.expression.result_type.value) == ("T", "long")


# -- hashing --------------------------------------------------------------

MUST_DIFFER = [
    ("sort-asc-vs-desc", "T | sort by x asc", "T | sort by x desc"),
    ("sort-bare-vs-asc", "T | sort by x", "T | sort by x asc"),
    ("sort-nulls-first-vs-default", "T | sort by x desc nulls first", "T | sort by x desc"),
    ("sort-nulls-first-vs-last", "T | sort by x desc nulls first", "T | sort by x desc nulls last"),
    ("sort-per-key-direction", "T | sort by x asc, y desc", "T | sort by x desc, y asc"),
    ("top-asc-vs-desc", "T | top 5 by x asc", "T | top 5 by x desc"),
    ("top-bare-vs-asc", "T | top 5 by x", "T | top 5 by x asc"),
]

MUST_EQUAL = [
    ("bare-sort-is-desc", "T | sort by x", "T | sort by x desc"),
    ("bare-top-is-desc", "T | top 5 by x", "T | top 5 by x desc"),
    ("order-by-is-sort-by", "T | order by x", "T | sort by x"),
    ("order-by-desc-is-sort-by-desc", "T | order by x asc", "T | sort by x asc"),
]


@pytest.mark.parametrize("case_id, a, b", MUST_DIFFER, ids=[c[0] for c in MUST_DIFFER])
def test_ordering_modifiers_hash_apart(case_id, a, b):
    assert _hash(a) != _hash(b), (
        f"{case_id}: {a!r} and {b!r} return rows in different orders but "
        f"produced the same semantic_hash"
    )


@pytest.mark.parametrize("case_id, a, b", MUST_EQUAL, ids=[c[0] for c in MUST_EQUAL])
def test_equivalent_orderings_hash_alike(case_id, a, b):
    assert _hash(a) == _hash(b), f"{case_id}: {a!r} and {b!r} mean the same thing"


# -- serialization --------------------------------------------------------

def test_sort_keys_round_trip_through_json():
    """``SortKey`` is a new element type inside ``SortOp.expressions`` and a
    new field type on ``TopOp.by``; both need a validator that reads them
    back under ``extra="forbid"``."""
    ir = parse("T | sort by x desc nulls first, y asc | top 5 by z asc").to_ir()
    again = QueryIR.model_validate_json(ir.model_dump_json())
    assert again == ir
    (sort_op,) = (o for o in again.main_pipeline.operators if isinstance(o, SortOp))
    (top_op,) = (o for o in again.main_pipeline.operators if isinstance(o, TopOp))
    assert [(k.direction, k.nulls) for k in sort_op.expressions] == [
        ("desc", "first"), ("asc", None),
    ]
    assert (top_op.by.direction, top_op.by.nulls) == ("asc", None)


def test_sort_key_direction_is_required_so_the_llm_view_renders_it():
    """A field holding its declared default is dropped by ``to_llm_dict``.
    ``direction`` therefore has no default: the effective ``desc`` on a bare
    ``sort by x`` has to reach the reader, who cannot otherwise tell which
    way the rows come back."""
    from kustology.ir import to_llm_dict

    ir = parse("T | sort by x").to_ir()
    (sort_view,) = [
        op for op in to_llm_dict(ir)["main_pipeline"]["operators"]
        if op["kind"] == "sort"
    ]
    assert sort_view["expressions"][0]["direction"] == "desc"
    # ``nulls`` is genuinely optional -- unwritten means "the engine decides"
    # rather than a value KQL substitutes -- so it keeps its ``None`` default
    # and is dropped from the view.
    assert "nulls" not in sort_view["expressions"][0]


def test_sort_key_rejects_an_unknown_direction():
    """``Literal["asc", "desc"]`` under ``extra="forbid"`` -- a dump carrying
    anything else must fail loudly rather than validate into a live IR."""
    import pydantic

    ir = parse("T | sort by x desc").to_ir()
    dumped = ir.model_dump_json().replace('"direction":"desc"', '"direction":"down"')
    with pytest.raises(pydantic.ValidationError):
        QueryIR.model_validate_json(dumped)
