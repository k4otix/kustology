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


# -- parse / parse-where --------------------------------------------------

def test_parse_records_a_written_kind():
    (op,) = _ops("T | parse kind=regex flags='i' a with 'x' b")
    assert op.parse_kind == "regex"


def test_parse_records_regex_flags():
    (op,) = _ops("T | parse kind=regex flags='i' a with 'x' b")
    assert op.flags == "i"


def test_bare_parse_records_kqls_effective_kind():
    """D8: an unwritten modifier records the value KQL applies, not ``None``."""
    (op,) = _ops("T | parse a with 'x' b")
    assert op.parse_kind == "simple"


def test_parse_where_records_a_written_kind():
    (op,) = _ops("T | parse-where kind=relaxed a with 'x' b")
    assert op.parse_kind == "relaxed"


def test_bare_parse_where_records_kqls_effective_kind():
    (op,) = _ops("T | parse-where a with 'x' b")
    assert op.parse_kind == "simple"


def test_parse_kind_reaches_the_hash():
    assert _hash("T | parse kind=regex a with 'x' b") != _hash(
        "T | parse kind=simple a with 'x' b"
    )


def test_bare_parse_hashes_as_its_effective_kind():
    assert _hash("T | parse a with 'x' b") == _hash(
        "T | parse kind=simple a with 'x' b"
    )


def test_parse_flags_reach_the_hash():
    assert _hash("T | parse kind=regex flags='i' a with 'x' b") != _hash(
        "T | parse kind=regex a with 'x' b"
    )


# -- union ----------------------------------------------------------------

_UNION_ALL = "union kind=inner withsource=S isfuzzy=true T, U"


def test_union_records_a_written_kind():
    (op,) = _ops(_UNION_ALL)
    assert op.union_kind == "inner"


def test_union_records_withsource():
    (op,) = _ops(_UNION_ALL)
    assert op.withsource == "S"


def test_union_records_isfuzzy_as_a_bool():
    (op,) = _ops(_UNION_ALL)
    assert op.is_fuzzy is True


def test_bare_union_records_kqls_effective_kind():
    (op,) = _ops("union T, U")
    assert op.union_kind == "outer"


def test_union_kind_reaches_the_hash():
    assert _hash("union kind=inner T, U") != _hash("union kind=outer T, U")


def test_bare_union_hashes_as_its_effective_kind():
    assert _hash("union T, U") == _hash("union kind=outer T, U")


def test_union_withsource_reaches_the_hash():
    assert _hash("union withsource=S T, U") != _hash("union T, U")


def test_union_isfuzzy_reaches_the_hash():
    assert _hash("union isfuzzy=true T, U") != _hash("union T, U")


# -- search ---------------------------------------------------------------

def test_search_records_a_written_kind():
    (op,) = _ops("search kind=case_sensitive in (A, B) 'x'")
    assert op.search_kind == "case_sensitive"


def test_search_records_the_tables_it_searches():
    (op,) = _ops("search kind=case_sensitive in (A, B) 'x'")
    assert [t.name for t in op.tables] == ["A", "B"]


def test_searched_tables_are_reachable_as_table_refs():
    from kustology.ir import TableRef, find_all
    ir = _ir("search in (A, B) 'x'")
    assert [t.name for t in find_all(ir, TableRef)] == ["A", "B"]


def test_a_let_bound_name_in_search_in_is_a_let_ref():
    from kustology.ir import LetRef
    ir = _ir("let A = T | take 1; search in (A) 'x'")
    (op,) = ir.main_pipeline.operators
    assert isinstance(op.tables[0], LetRef)


def test_search_qualified_table_keeps_its_database():
    (op,) = _ops("search in (database('d').T) 'x'")
    assert (op.tables[0].name, op.tables[0].database) == ("T", "d")


def test_search_kind_reaches_the_hash():
    assert _hash("search kind=case_sensitive 'x'") != _hash("search 'x'")


def test_searched_table_reaches_the_hash():
    assert _hash("search in (A) 'x'") != _hash("search in (B) 'x'")


def test_search_scope_reaches_the_hash():
    assert _hash("search in (A) 'x'") != _hash("search 'x'")


# -- find -----------------------------------------------------------------

_FIND_ALL = "find withsource=S in (T, U) where a == 1 project a, b"


def test_find_records_its_tables_as_refs():
    (op,) = _ops(_FIND_ALL)
    assert [t.name for t in op.tables] == ["T", "U"]


def test_find_records_withsource():
    (op,) = _ops(_FIND_ALL)
    assert op.withsource == "S"


def test_find_records_its_project_columns():
    (op,) = _ops(_FIND_ALL)
    assert [c.name for c in op.project] == ["a", "b"]


def test_find_typed_project_column_keeps_its_type():
    (op,) = _ops("find in (T) where a == 1 project a:string")
    assert isinstance(op.project[0], TypedNameDecl)
    assert op.project[0].declared_type == "string"


def test_found_tables_are_reachable_as_table_refs():
    from kustology.ir import TableRef, find_all
    ir = _ir("find in (T, U) where a == 1")
    assert [t.name for t in find_all(ir, TableRef)] == ["T", "U"]


def test_a_let_bound_name_in_find_in_is_a_let_ref():
    from kustology.ir import LetRef
    ir = _ir("let A = T | take 1; find in (A) where a == 1")
    (op,) = ir.main_pipeline.operators
    assert isinstance(op.tables[0], LetRef)


def test_find_project_reaches_the_hash():
    assert _hash("find in (T) where a == 1 project a") != _hash(
        "find in (T) where a == 1"
    )


def test_find_withsource_reaches_the_hash():
    assert _hash("find withsource=S in (T) where a == 1") != _hash(
        "find in (T) where a == 1"
    )


def test_a_comment_before_a_found_table_does_not_reach_the_hash():
    """The routed finding: ``FindOp.tables`` was read with a bare
    ``ToString()``, which is ``IncludeTrivia.All`` and prepends the node's
    leading trivia -- so the table name was recorded as ``"// note\\n T"``
    and a comment split the digest."""
    assert _hash("find in (// note\n T) where x == 1") == _hash(
        "find in (T) where x == 1"
    )


# -- make-series ----------------------------------------------------------

_MS_RANGE = (
    "T | make-series n=count() default=0 on t in "
    "range(datetime(2024-01-01), datetime(2024-01-02), 1h) by g"
)


def test_make_series_keeps_the_aggregate_name_and_expression():
    (op,) = _ops(_MS_RANGE)
    (agg,) = op.aggregations
    assert (agg.name, agg.expr.name) == ("n", "count")


def test_make_series_records_the_gap_filling_default():
    (op,) = _ops(_MS_RANGE)
    assert op.aggregations[0].default.value == 0


def test_in_range_populates_from_to_and_step():
    (op,) = _ops(_MS_RANGE)
    assert op.range_from.value.startswith("2024-01-01")
    assert op.range_to.value.startswith("2024-01-02")
    assert op.step.value == "01:00:00"


def test_from_to_step_clause_still_populates_the_same_fields():
    (op,) = _ops(
        "T | make-series n=count() on t from datetime(2024-01-01) "
        "to datetime(2024-01-02) step 1h by g"
    )
    assert op.range_from is not None and op.range_to is not None
    assert op.step.value == "01:00:00"


def test_make_series_default_reaches_the_hash():
    assert _hash("T | make-series n=count() default=0 on t step 1h") != _hash(
        "T | make-series n=count() default=1 on t step 1h"
    )


def test_make_series_default_differs_from_no_default():
    assert _hash("T | make-series n=count() default=0 on t step 1h") != _hash(
        "T | make-series n=count() on t step 1h"
    )


def test_make_series_range_bounds_reach_the_hash():
    assert _hash(_MS_RANGE) != _hash(_MS_RANGE.replace("2024-01-02", "2024-01-03"))


# -- render ---------------------------------------------------------------

def test_render_records_with_clause_properties():
    (op,) = _ops('T | render timechart with (title="a", xtitle="b")')
    assert op.properties == {"title": "a", "xtitle": "b"}


def test_render_keeps_its_chart_type():
    (op,) = _ops('T | render timechart with (title="a")')
    assert op.render_kind == "timechart"


def test_render_records_the_legacy_parameter_spelling():
    (op,) = _ops("T | render columnchart kind=stacked")
    assert op.properties == {"kind": "stacked"}


def test_the_two_render_property_spellings_agree():
    """``render c kind=stacked`` and ``render c with (kind=stacked)`` are the
    same query written two ways, so they must hash alike."""
    assert _hash("T | render columnchart kind=stacked") == _hash(
        "T | render columnchart with (kind=stacked)"
    )


def test_render_properties_reach_the_hash():
    assert _hash('T | render timechart with (title="a")') != _hash(
        "T | render timechart"
    )


def test_render_property_values_reach_the_hash():
    assert _hash('T | render timechart with (title="a")') != _hash(
        'T | render timechart with (title="b")'
    )
