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


def test_mv_expand_records_its_kind():
    """``kind=`` and the legacy ``bagexpansion=`` are one field.

    ``_MV_ALL`` writes both (which parses clean), and the modern spelling
    wins -- see :class:`MvExpandOp`.
    """
    (op,) = _ops(_MV_ALL)
    assert op.expand_kind == "array"


def test_mv_expand_reads_the_legacy_bagexpansion_spelling():
    (op,) = _ops("T | mv-expand bagexpansion=array a")
    assert op.expand_kind == "array"


def test_bare_mv_expand_records_kqls_effective_kind():
    (op,) = _ops("T | mv-expand a")
    assert op.expand_kind == "bag"


def test_mv_expand_kind_value_set_is_the_dlls():
    """Pins the probe both the fold and the effective default rest on."""
    for spelling in ("kind=bogus", "bagexpansion=bogus"):
        messages = [d.message for d in parse(
            f"T | mv-expand {spelling} a", schema={"T": {"a": "dynamic"}},
        ).to_ir().diagnostics]
        assert any("Expected one of: bag, array" in m for m in messages), spelling


def test_mv_expand_modifiers_all_reach_the_hash():
    """Each modifier alone must move the digest, and off a common base.

    Comparing the spellings pairwise is what catches a field that is
    modeled but not hashed: several could still collapse onto the bare form
    if only one pair were checked. ``bagexpansion`` is absent because it is
    not a fifth modifier -- it is ``kind`` spelled the old way, pinned as an
    equality above.
    """
    variants = {
        "bare": _hash("T | mv-expand a"),
        "typed": _hash("T | mv-expand a to typeof(string)"),
        "limited": _hash("T | mv-expand a limit 10"),
        "indexed": _hash("T | mv-expand with_itemindex=i a"),
        "kinded": _hash("T | mv-expand kind=array a"),
    }
    assert len(set(variants.values())) == len(variants), variants


def test_mv_expand_still_binds_the_expanded_column():
    ir = parse(
        "T | mv-expand a | where a == 'x'", schema={"T": {"a": "dynamic"}},
    ).to_ir()
    assert "a" in ir.main_pipeline.result_schema.columns


# -- mv-apply ---------------------------------------------------------------

_MV_APPLY_ALL = (
    "T | mv-apply with_itemindex=i x=d to typeof(long) limit 3 "
    "on (summarize count())"
)


def test_mv_apply_records_the_declared_element_type():
    (op,) = _ops(_MV_APPLY_ALL)
    assert op.to_typeof == "long"


def test_mv_apply_multi_column_to_typeof_takes_the_first_written():
    """Disclosed in :class:`~kustology.ir.query.MvApplyOp`'s docstring: with
    two comma-separated columns each carrying its own ``to typeof(...)``, the
    reader takes the first written occurrence across ``assignments``, not the
    last."""
    (op,) = _ops(
        "T | mv-apply a to typeof(long), b to typeof(string) on (take 1)"
    )
    assert op.to_typeof == "long"


def test_mv_apply_records_the_row_limit():
    (op,) = _ops(_MV_APPLY_ALL)
    assert op.row_limit == 3


def test_mv_apply_records_the_item_index():
    """``with_itemindex=`` precedes the expansion column here -- the postfix
    spelling is a parse error, see :class:`~kustology.ir.query.MvApplyOp`."""
    (op,) = _ops(_MV_APPLY_ALL)
    assert op.item_index == "i"


def test_bare_mv_apply_has_no_modifiers():
    (op,) = _ops("T | mv-apply x=d on (summarize count())")
    assert op.to_typeof is None
    assert op.row_limit is None
    assert op.item_index is None


def test_mv_apply_modifiers_all_reach_the_hash():
    """Each modifier alone must move the digest, and off a common base --
    mirrors mv-expand's pairwise check just above."""
    variants = {
        "bare": _hash("T | mv-apply x=d on (summarize count())"),
        "typed": _hash("T | mv-apply x=d to typeof(long) on (summarize count())"),
        "limited": _hash("T | mv-apply x=d limit 3 on (summarize count())"),
        "indexed": _hash("T | mv-apply with_itemindex=i x=d on (summarize count())"),
    }
    assert len(set(variants.values())) == len(variants), variants


def test_mv_apply_still_builds_the_subquery_pipeline():
    (op,) = _ops(_MV_APPLY_ALL)
    assert op.right.operators[0].kind == "summarize"


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


def test_make_series_default_differs_from_no_default():
    assert _hash("T | make-series n=count() default=0 on t step 1h") != _hash(
        "T | make-series n=count() on t step 1h"
    )


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


def test_render_properties_reach_the_hash():
    assert _hash('T | render timechart with (title="a")') != _hash(
        "T | render timechart"
    )


# -- join / lookup effective defaults (D8) --------------------------------

def test_bare_join_records_kqls_effective_kind():
    (op,) = _ops("T | join U on k")
    assert op.join_kind == "innerunique"


def test_written_join_kind_is_kept():
    (op,) = _ops("T | join kind=leftanti U on k")
    assert op.join_kind == "leftanti"


def test_bare_lookup_records_kqls_effective_kind():
    (op,) = _ops("T | lookup U on k")
    assert op.lookup_kind == "leftouter"


# -- hints (volatile: recorded, never hashed) -----------------------------

def test_join_hint_is_recorded():
    (op,) = _ops("T | join hint.strategy=shuffle (U) on k")
    assert op.hints == {"hint.strategy": "shuffle"}


def test_summarize_hint_is_recorded_and_not_hashed():
    (op,) = _ops("T | summarize hint.shufflekey=a count() by a")
    assert op.hints == {"hint.shufflekey": "a"}
    assert _hash("T | summarize hint.shufflekey=a count() by a") == _hash(
        "T | summarize count() by a"
    )


def test_mv_expand_hint_is_recorded_and_not_hashed():
    (op,) = _ops("T | mv-expand hint.spread=2 a")
    assert op.hints == {"hint.spread": "2"}
    assert _hash("T | mv-expand hint.spread=2 a") == _hash("T | mv-expand a")


def test_two_different_hints_are_kept_apart():
    (op,) = _ops("T | join hint.strategy=shuffle hint.remote=left (U) on k")
    assert op.hints == {"hint.strategy": "shuffle", "hint.remote": "left"}


def test_a_non_hint_parameter_is_not_a_hint():
    """``kind=`` changes the operator and is modeled as a field; only
    ``hint.*`` is advisory."""
    (op,) = _ops("T | join kind=inner (U) on k")
    assert op.hints == {}


def test_a_function_call_render_property_is_recorded_as_written():
    """A property value is not always a bare name or a literal.

    ``strcat("a","b")`` is a ``FunctionCallExpression``, which *has* a
    ``Name`` member -- so the bare-name branch fired and recorded the
    function's name, ``"strcat"``, for every call whatever its arguments.
    """
    (op,) = _ops('T | render timechart with (title=strcat("a","b"))')
    assert op.properties == {"title": 'strcat("a","b")'}


def test_two_function_call_render_properties_hash_apart():
    assert _hash('T | render timechart with (title=strcat("a","b"))') != _hash(
        'T | render timechart with (title=strcat("c","d"))'
    )


def test_bare_search_records_kqls_effective_kind():
    """D8 does apply here: the DLL pins the value set.

    ``search kind=bogus 'x'`` on a bound parse is diagnosed *Expected one
    of: default, case_insensitive, case_sensitive*, so ``default`` is a
    value the grammar knows and the unwritten case selects it.
    """
    (op,) = _ops("search 'x'")
    assert op.search_kind == "default"


def test_search_default_and_case_sensitive_hash_apart():
    assert _hash("search kind=default 'x'") != _hash("search kind=case_sensitive 'x'")


def test_search_kind_value_set_is_the_dlls():
    """Pins the probe the effective default is derived from."""
    messages = [d.message for d in parse(
        "search kind=bogus 'x'", schema={"T": {"a": "string"}},
    ).to_ir().diagnostics]
    assert any("default, case_insensitive, case_sensitive" in m for m in messages)


def test_only_the_grammars_hint_spelling_is_a_hint():
    """The prefix match is case-sensitive because the grammar is.

    ``HINT.strategy=shuffle`` is not a named parameter at all -- the parser
    reads ``HINT`` as a name and complains -- so a lenient match could not
    admit anything a strict one misses, and would risk two dict entries for
    one hint if a later DLL did accept a second casing.
    """
    ir = parse(
        "T | join HINT.strategy=shuffle (U) on k",
        schema={"T": {"k": "string"}, "U": {"k": "string"}},
    ).to_ir()
    assert any("HINT" in d.message for d in ir.diagnostics)
    (op,) = ir.main_pipeline.operators
    assert op.hints == {}


def test_the_render_with_clause_wins_a_property_collision():
    """Both spellings in one query: the modern ``with`` clause is the value.

    Documented in the merge and otherwise unpinned, so a reordering of the
    two ``read_named_params`` calls would flip it silently.
    """
    (op,) = _ops("T | render columnchart kind=stacked with (kind=unstacked)")
    assert op.properties == {"kind": "unstacked"}


def test_find_qualified_table_keeps_its_database():
    """``find`` shares ``search``'s table reader, so it gains the same
    qualifier fidelity -- asserted rather than assumed from the sharing."""
    (op,) = _ops("find in (database('d').T) where a == 1")
    assert (op.tables[0].name, op.tables[0].database) == ("T", "d")


def test_find_project_smart_is_the_default_projection():
    """``project-smart`` is what a bare ``find`` does, so the two must hash
    alike.

    True today because the clause holds no columns; pinned so a future read
    of ``ProjectKeyword`` cannot split them by accident.
    """
    assert _hash("find in (T) where a == 1 project-smart") == _hash(
        "find in (T) where a == 1"
    )


# -- evaluate ---------------------------------------------------------------

def test_evaluate_declared_schema_is_modeled():
    """`: (x:string)` is the operator's declared result shape, carried on
    the IR in clause order."""
    (op,) = _ir("T | evaluate bag_unpack(d) : (y:long, z:datetime)").main_pipeline.operators
    assert op.declared_schema == [("y", "long"), ("z", "datetime")]
    assert op.declared_schema_star is False


def test_evaluate_schema_star_means_append():
    (op,) = _ir("T | evaluate bag_unpack(d) : (*, x:string)").main_pipeline.operators
    assert op.declared_schema == [("x", "string")]
    assert op.declared_schema_star is True


def test_evaluate_without_a_clause_stays_none():
    (op,) = _ir("T | evaluate bag_unpack(d)").main_pipeline.operators
    assert op.declared_schema is None
    assert op.declared_schema_star is False


def test_evaluate_bare_star_is_empty_with_the_flag():
    (op,) = _ir("T | evaluate bag_unpack(d) : (*)").main_pipeline.operators
    assert op.declared_schema == []
    assert op.declared_schema_star is True


# -- parse-kv properties ----------------------------------------------------

def test_parse_kv_records_with_clause_properties():
    (op,) = _ops(
        "T | parse-kv x as (b:string) with (pair_delimiter=';', kv_delimiter='=')"
    )
    assert op.properties == [("pair_delimiter", ";"), ("kv_delimiter", "=")]


def test_parse_kv_keeps_a_repeated_property_name():
    """``quote`` legally repeats -- a dict would silently drop one spelling,
    which is why ``properties`` is a list, not a dict."""
    (op,) = _ops("T | parse-kv x as (b:string) with (quote='|', quote='~')")
    assert op.properties == [("quote", "|"), ("quote", "~")]


def test_bare_parse_kv_has_no_properties():
    (op,) = _ops("T | parse-kv x as (b:string)")
    assert op.properties == []


def test_parse_kv_properties_reach_the_hash():
    assert _hash("T | parse-kv x as (b:string) with (pair_delimiter=',')") != _hash(
        "T | parse-kv x as (b:string)"
    )


# -- getschema kind -----------------------------------------------------------

def test_getschema_records_a_written_kind():
    (op,) = _ops("T | getschema kind=full")
    assert op.output_kind == "full"


def test_bare_getschema_has_no_kind():
    (op,) = _ops("T | getschema")
    assert op.output_kind is None


def test_getschema_kind_reaches_the_hash():
    assert _hash("T | getschema kind=csl") != _hash("T | getschema")


def test_getschema_two_written_kinds_hash_apart():
    assert _hash("T | getschema kind=csl") != _hash("T | getschema kind=full")


# -- consume decodeblocks -----------------------------------------------------

def test_consume_records_decodeblocks_as_the_dlls_own_text():
    """The DLL's own literal rendering -- ``true`` arrives as the text
    ``'True'``, not a Python bool. Recorded as written, not normalized."""
    (op,) = _ops("T | consume decodeblocks=true")
    assert op.decodeblocks == "True"


def test_bare_consume_has_no_decodeblocks():
    (op,) = _ops("T | consume")
    assert op.decodeblocks is None


def test_consume_decodeblocks_reaches_the_hash():
    assert _hash("T | consume decodeblocks=true") != _hash("T | consume")


def test_consume_two_written_decodeblocks_hash_apart():
    assert _hash("T | consume decodeblocks=true") != _hash(
        "T | consume decodeblocks=false"
    )
