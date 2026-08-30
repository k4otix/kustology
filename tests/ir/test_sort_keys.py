# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Ordering keys: ``sort by`` / ``order by`` / ``top … by`` / ``project-reorder``.

``SortOp.expressions`` and ``TopOp.by`` hold :class:`SortKey` elements
because a bare expression carries neither the direction nor the ``nulls
first`` / ``nulls last`` clause. The AST's ``OrderedExpression`` wrapper
carries both; keeping only the inner expression collapses ``sort by x asc``
and ``sort by x desc``, which return rows in opposite orders, onto one
``semantic_hash``.

Two properties pinned here are easy to conflate. The recorded value on an
explicit query: ``asc``, ``desc``, ``nulls first`` and ``nulls last`` each
have a case asserting the written value. The effective default on a bare
query: ``sort by x`` sorts *descending* in KQL, so ``direction`` is
``"desc"`` there and the two queries hash alike. The second is a claim about
KQL's semantics, which is why ``SortKey.direction`` is required with no
default: ``to_llm_dict`` drops a field holding its declared default, and a
reader could not then tell ``desc`` was in force.

``project-reorder`` is the third consumer of ``OrderedExpression``, and the
one that makes those two properties come apart. Its ``asc``/``desc`` orders
*columns*, and its no-modifier case means "keep the order they are listed
in", so there is no effective default to substitute and :class:`ReorderKey`
gives ``direction`` a genuine ``None``. Reusing ``SortKey`` would stamp
``desc`` on a bare column, misreporting it and collapsing it against an
explicit ``desc``.
"""

import pytest

from kustology import parse
from kustology.ir import (
    ColumnRef,
    ProjectReorderOp,
    QueryIR,
    ReorderKey,
    SortKey,
    SortOp,
    TopOp,
    find_all,
)


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
    """``SchemaAttacher._fill_children`` descends ``model_fields``, not a
    hardcoded attribute tuple, so interposing ``SortKey`` between the operator
    and its expression must not hide the expression from the binder. Asserted
    on a bound parse for both the column's provenance and its type."""
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
#
# Every ordering hash pair lives in tests/ir/test_hash_battery.py, as
# sort-direction, sort-per-key-direction, top-by-direction,
# sort-bare-is-desc, top-bare-is-desc, sort-nulls-placement,
# sort-nulls-clause-vs-absent, order-by-is-sort-by and
# order-by-desc-is-sort-by-desc.

# -- serialization --------------------------------------------------------

def test_sort_keys_round_trip_through_json():
    """``SortOp.expressions`` holds ``SortKey`` elements and ``TopOp.by`` is
    one; both need a validator that reads them back under
    ``extra="forbid"``."""
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
    """``to_llm_dict`` drops a field holding its declared default, so
    ``direction`` has none: the effective ``desc`` on a bare ``sort by x``
    has to reach the reader, who cannot otherwise tell which way the rows
    come back."""
    from kustology.ir import to_llm_dict

    ir = parse("T | sort by x").to_ir()
    (sort_view,) = [
        op for op in to_llm_dict(ir)["main_pipeline"]["operators"]
        if op["kind"] == "sort"
    ]
    assert sort_view["expressions"][0]["direction"] == "desc"
    # ``nulls`` is genuinely optional: unwritten means "the engine decides",
    # so it keeps its ``None`` default and the view drops it.
    assert "nulls" not in sort_view["expressions"][0]


def test_sort_key_rejects_an_unknown_direction():
    """``Literal["asc", "desc"]`` under ``extra="forbid"`` -- a dump carrying
    anything else must fail loudly rather than validate into a live IR."""
    import pydantic

    ir = parse("T | sort by x desc").to_ir()
    dumped = ir.model_dump_json().replace('"direction":"desc"', '"direction":"down"')
    with pytest.raises(pydantic.ValidationError):
        QueryIR.model_validate_json(dumped)


# -- malformed input degrades, it does not raise --------------------------

MALFORMED_NULLS = [
    ("no-keyword", "T | sort by x nulls", "desc"),
    ("truncated-keyword", "T | sort by x nulls firs", "desc"),
    ("after-a-direction", "T | sort by x asc nulls", "asc"),
    ("garbage-keyword", "T | top 5 by x nulls xyz", "desc"),
]


@pytest.mark.parametrize(
    "case_id, query, direction", MALFORMED_NULLS, ids=[c[0] for c in MALFORMED_NULLS],
)
def test_a_malformed_nulls_clause_degrades_instead_of_raising(case_id, query, direction):
    """``to_ir()`` must not be the thing that fails on bad KQL.

    Kusto's error recovery has two ways of saying "the keyword isn't there",
    and only one is ``None``: ``sort by x nulls`` builds an
    ``OrderingNullsClause`` that *exists*, holding a ``FirstOrLastKeyword``
    that also exists but is a missing token whose ``Text`` is ``""``. A
    presence check alone lets that empty string reach
    ``Literal["first", "last"]`` and turns a typo into a ``ValidationError``
    out of ``to_ir()``, where ``T | take``, ``T | where``,
    ``T | summarize by`` and ``T | sort by`` all build a degraded operator
    and leave the complaint to the diagnostics.
    """
    parsed = parse(query)
    assert parsed.diagnostics, f"{case_id}: expected the parser to complain about {query!r}"
    ir = parsed.to_ir()  # must not raise
    keys = [
        k for op in ir.main_pipeline.operators if isinstance(op, (SortOp, TopOp))
        for k in (op.expressions if isinstance(op, SortOp) else [op.by])
    ]
    key = keys[0]
    assert key.nulls is None
    assert key.direction == direction, "an unreadable nulls clause must not eat the direction"
    assert key.expression.name == "x", "the ordering expression survives the bad clause"


def test_a_truncated_nulls_keyword_recovers_as_a_second_key():
    """Recovery is the parser's call: ``nulls firs`` drops the unreadable
    clause and reads ``firs`` as a second ordering key. The builder reports
    that shape and does not fail on it."""
    keys = _sort_keys("T | sort by x nulls firs")
    assert [(k.expression.name, k.direction, k.nulls) for k in keys] == [
        ("x", "desc", None), ("firs", "desc", None),
    ]


# -- project-reorder: the third consumer of OrderedExpression -------------

def _reorder_keys(query: str) -> list[ReorderKey]:
    ir = parse(query).to_ir()
    (op,) = find_all(ir, ProjectReorderOp)
    return op.columns


def test_project_reorder_keeps_the_column_and_records_the_direction():
    """The ``OrderedExpression`` branch of ``_visit_expr`` for ``sort`` and
    ``top`` does not cover this third site. Unhandled,
    ``project-reorder x asc`` falls through to ``UnknownExpr``, losing the
    column identity: unbindable and invisible to ``find_all``."""
    (key,) = _reorder_keys("T | project-reorder x asc")
    assert isinstance(key, ReorderKey), type(key).__name__
    assert isinstance(key.expression, ColumnRef), type(key.expression).__name__
    assert (key.expression.name, key.direction) == ("x", "asc")


def test_project_reorder_column_is_reachable_by_find_all():
    ir = parse("T | project-reorder x asc").to_ir()
    assert [c.name for c in find_all(ir, ColumnRef)] == ["x"]


def test_project_reorder_without_a_modifier_has_no_direction():
    """``project-reorder x`` keeps the listed order. There is no KQL default
    to record, which is why ``ReorderKey`` is a separate model from
    ``SortKey``."""
    (key,) = _reorder_keys("T | project-reorder x")
    assert key.direction is None


def test_project_reorder_records_each_term_independently():
    keys = _reorder_keys("T | project-reorder x asc, y desc, z")
    assert [(k.expression.name, k.direction) for k in keys] == [
        ("x", "asc"), ("y", "desc"), ("z", None),
    ]


def test_project_reorder_wildcard_terms_survive_with_their_direction():
    """``*`` and prefix wildcards are where ``asc``/``desc`` earn their keep:
    the direction orders the columns the wildcard matched.

    A bare ``*`` is *every remaining column*. Kusto parses it as a
    ``NameReference`` holding a ``WildcardedName``, the same class an
    ordinary column gets, so lowering it the same way produces
    ``ColumnRef(name="*")`` and ``find_all(ir, ColumnRef)``, the documented
    way to ask which columns a query names, reports a column that does not
    exist. It is a :class:`~kustology.ir.StarExpr`, the node ``distinct *``
    produces.

    A *prefix* wildcard stays a ``ColumnRef``: ``a*`` names a set of real
    columns by pattern, the pattern text is the only record of which ones,
    and ``StarExpr`` has no field to hold it.
    """
    from kustology.ir import StarExpr

    (star,) = _reorder_keys("T | project-reorder * asc")
    assert isinstance(star.expression, StarExpr), type(star.expression).__name__
    assert star.direction == "asc"

    (prefix,) = _reorder_keys("T | project-reorder a* desc")
    assert isinstance(prefix.expression, ColumnRef), type(prefix.expression).__name__
    assert (prefix.expression.name, prefix.direction) == ("a*", "desc")

    keys = _reorder_keys("T | project-reorder *, a")
    assert isinstance(keys[0].expression, StarExpr), type(keys[0].expression).__name__
    assert (type(keys[1].expression).__name__, keys[1].expression.name) == ("ColumnRef", "a")
    assert [k.direction for k in keys] == [None, None]


def test_a_bare_wildcard_is_not_reported_as_a_column():
    """``find_all(ir, ColumnRef)`` must not name ``*``.

    The rule lives in ``_visit_expr``'s ``NameReference`` branch, shared by
    every operator that puts an expression in that position, so its reach is
    wider than ``project-reorder``: without it, ``search *``,
    ``summarize arg_max(*, x)`` and ``evaluate bag_unpack(*)`` each write a
    phantom column into the IR. The cases are enumerated because the shared
    branch makes the blast radius easy to under-report.
    """
    from kustology.ir import StarExpr, find_all

    for query in (
        "T | project-reorder *, a",
        "T | project-away *",
        "T | project-keep *",
        "search *",
        "T | summarize arg_max(*, x)",
        "T | evaluate bag_unpack(*)",
    ):
        ir = parse(query).to_ir()
        assert "*" not in [c.name for c in find_all(ir, ColumnRef)], query
        assert len(list(find_all(ir, StarExpr))) == 1, query


def test_a_prefix_wildcard_and_a_bare_one_do_not_hash_alike():
    """The ``StarExpr`` rewrite keys on ``WildcardedName`` *and* the text
    being exactly ``*``. Keying on ``WildcardedName`` alone swallows ``a*``
    too, and ``StarExpr`` has no field to carry the pattern, so every prefix
    wildcard collapses onto every other one and onto a bare ``*``."""
    assert (
        parse("T | project-reorder *").to_ir().semantic_hash
        != parse("T | project-reorder a*").to_ir().semantic_hash
    )


REORDER_MUST_DIFFER = [
    # desc-vs-bare and per-term are here because the battery does not carry
    # them. asc-vs-desc and asc-vs-bare live in test_hash_battery.py, as
    # project-reorder-direction and project-reorder-direction-vs-unwritten.
    ("desc-vs-bare", "T | project-reorder x desc", "T | project-reorder x"),
    ("per-term", "T | project-reorder x asc, y desc", "T | project-reorder x desc, y asc"),
]


@pytest.mark.parametrize(
    "case_id, a, b", REORDER_MUST_DIFFER, ids=[c[0] for c in REORDER_MUST_DIFFER],
)
def test_project_reorder_directions_hash_apart(case_id, a, b):
    """The direction survives inside an ``UnknownExpr``'s raw text, so these
    forms hash apart even where ``project-reorder`` is undispatched. Modeling
    the column must keep them apart too."""
    assert _hash(a) != _hash(b), f"{case_id}: {a!r} and {b!r} order columns differently"


def test_project_reorder_binder_reaches_the_column_and_microsoft_orders_it():
    """The column has to be a real expression for ``_fill`` to place and type
    it. The emitted column order is Microsoft's: ``project-reorder`` closes
    the symbol, so the answer is stamped."""
    schema = {"T": {"x": "string", "n": "long"}}
    ir = parse("T | project-reorder n asc", schema=schema).to_ir()
    assert ir.schema_attached
    (key,) = _reorder_keys_from(ir)
    assert (key.expression.table, key.expression.result_type.value) == ("T", "long")
    # KQL emits listed columns first, then the rest in source order.
    assert list(ir.main_pipeline.result_schema.columns) == ["n", "x"]


def _reorder_keys_from(ir: QueryIR) -> list[ReorderKey]:
    (op,) = find_all(ir, ProjectReorderOp)
    return op.columns


def test_project_reorder_round_trips_through_json():
    ir = parse("T | project-reorder x asc, y desc, z").to_ir()
    again = QueryIR.model_validate_json(ir.model_dump_json())
    assert again == ir
    assert [(k.expression.name, k.direction) for k in _reorder_keys_from(again)] == [
        ("x", "asc"), ("y", "desc"), ("z", None),
    ]


def test_project_reorder_rejects_an_unknown_direction():
    import pydantic

    ir = parse("T | project-reorder x asc").to_ir()
    dumped = ir.model_dump_json().replace('"direction":"asc"', '"direction":"sideways"')
    with pytest.raises(pydantic.ValidationError):
        QueryIR.model_validate_json(dumped)


def test_reorder_direction_is_optional_so_the_llm_view_shows_only_written_ones():
    """The mirror image of ``SortKey.direction``: ``None`` is the right value
    for an unwritten modifier, so it keeps a pydantic default and
    ``to_llm_dict`` drops it. A written one still renders."""
    from kustology.ir import to_llm_dict

    ir = parse("T | project-reorder x asc, z").to_ir()
    (view,) = [
        op for op in to_llm_dict(ir)["main_pipeline"]["operators"]
        if op["kind"] == "project_reorder"
    ]
    assert view["columns"][0]["direction"] == "asc"
    assert "direction" not in view["columns"][1]
