# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Coverage for table-source positions beyond the leftmost pipe source.

Pins the contract that ``get_referenced_tables`` and ``find_table_references``
detect tables in every position where one can appear: the leftmost
``Source | ...`` pipe source, ``join`` and ``lookup`` targets, ``union``
operands, and ``facet`` targets. Both syntactic and semantic modes are
covered.
"""

import pytest

from kustology import parse

JOIN_QUERY = "A | join (B) on x"
UNION_QUERY = "union A, B | count"
LOOKUP_QUERY = "A | lookup B on x"
FACET_QUERY = "A | facet by x with (B)"

ALL_TWO_TABLE_QUERIES = [
    pytest.param(JOIN_QUERY, id="join"),
    pytest.param(UNION_QUERY, id="union"),
    pytest.param(LOOKUP_QUERY, id="lookup"),
    pytest.param(FACET_QUERY, id="facet"),
]


@pytest.mark.parametrize("query", ALL_TWO_TABLE_QUERIES)
def test_two_table_extraction_syntactic(query):
    tables = parse(query).get_referenced_tables()
    assert tables == {"A", "B"}, f"syntactic mode missed a table in: {query!r}"


@pytest.mark.parametrize("query", ALL_TWO_TABLE_QUERIES)
def test_two_table_extraction_semantic(query):
    schema = {"A": {"x": "string"}, "B": {"x": "string"}}
    tables = parse(query, schema=schema).get_referenced_tables()
    assert tables == {"A", "B"}, f"semantic mode missed a table in: {query!r}"


def test_let_shadow_excludes_local_variable():
    """A `let X = Source` shadow must not leak `X` as a table reference."""
    tables = parse("let X = A; X | count").get_referenced_tables()
    assert tables == {"A"}


def test_force_syntactic_overrides_semantic():
    """When the query is bound, callers can opt back into the syntactic walk."""
    schema = {"A": {"x": "string"}, "B": {"x": "string"}}
    q = parse(JOIN_QUERY, schema=schema)
    assert q.has_semantics is True
    assert q.get_referenced_tables(force_syntactic=True) == {"A", "B"}


def test_three_way_union_extraction():
    tables = parse("union A, B, C | count").get_referenced_tables()
    assert tables == {"A", "B", "C"}


def test_subpipeline_in_join():
    """A `(B | filter)` sub-pipeline still resolves B as a table."""
    q = "A | join (B | where x == 1) on x"
    assert parse(q).get_referenced_tables() == {"A", "B"}


def test_database_qualified_table_extraction():
    """`database('d').T` is a PathExpression — the trailing T is the table."""
    assert parse('database("d").T | count').get_referenced_tables() == {"T"}


def test_cluster_qualified_table_extraction():
    """`cluster('c').database('d').T` — same shape, deeper path."""
    assert parse('cluster("c").database("d").T | count').get_referenced_tables() == {"T"}


def test_database_qualified_table_replaces_only_table_name():
    """replace_table renames the trailing identifier, not database()/cluster()."""
    out = parse('database("d").T | count').replace_table("T", "U")
    assert out == 'database("d").U | count'


# --- operators whose table positions were never collected ------------------
#
# `_collect_table_refs` enumerated table-source positions by node kind and
# omitted the `find in (...)` / `search in (...)` clauses. The consequence
# for get_referenced_tables is a missing name; the consequence for
# replace_table is worse -- it returns the query *unchanged*, with no error,
# so a consumer migrating a table ships one still pointing at the old name.
#
# A partition subquery has no table position at all: `partition by K (B | …)`
# is a parse error ("Query operator expected"), since the subquery runs on
# the partitioned rows rather than a new source.


def test_partition_subquery_has_no_table_position():
    q = "A | partition by K (where Z > 1)"
    assert parse(q).get_referenced_tables() == {"A"}


def test_find_in_clause_tables_are_collected():
    q = "find in (S1, S2) where X == 1"
    assert parse(q).get_referenced_tables() == {"S1", "S2"}


def test_search_in_clause_tables_are_collected():
    q = 'search in (S1, S2) "err"'
    assert parse(q).get_referenced_tables() == {"S1", "S2"}


def test_replace_table_rewrites_those_positions():
    """The silent-no-op case: replace_table must actually rewrite."""
    assert (
        parse("find in (S1, S2) where X == 1").replace_table("S1", "NewS1")
        == "find in (NewS1, S2) where X == 1"
    )
    assert (
        parse('search in (S1, S2) "err"').replace_table("S2", "NewS2")
        == 'search in (S1, NewS2) "err"'
    )


def test_let_bound_names_are_still_excluded_in_those_positions():
    """A let alias in an `in (...)` clause is not a table reference."""
    q = "let Local = A | take 1; find in (Local, B) where X == 1"
    assert parse(q).get_referenced_tables() == {"A", "B"}


# --- names that occupy a table position but are not tables (K16, K29) ------
#
# The syntactic walk reported four kinds of non-table as tables: the name a
# `let` binds, the name an `as` operator binds, a user-defined function's
# table-typed parameter, and a `union T*` wildcard pattern. It also got
# shadowing backwards -- with `let T = T | ...`, the *right-hand side* T is
# the real table (a binding's RHS is evaluated outside its own name, so KQL
# has no recursion here) while every later use is the alias, and the flat
# name-based filter dropped both.

SHADOW_QUERY = "let SecurityEvent = SecurityEvent | where a; SecurityEvent | take 1"


def test_let_shadowing_keeps_the_bindings_own_rhs():
    """`let T = T | ...` reads the real table on the right-hand side."""
    assert parse(SHADOW_QUERY).get_referenced_tables() == {"SecurityEvent"}


def test_let_shadowing_replaces_only_the_rhs():
    """The alias use sites are not table references, so they must not move."""
    out = parse(SHADOW_QUERY).replace_table("SecurityEvent", "Z")
    assert out == "let SecurityEvent = Z | where a; SecurityEvent | take 1"


def test_let_alias_in_a_later_binding_rhs_is_not_a_table():
    """Only the binding's *own* name is unshadowed on its RHS.

    `Local` is in scope inside the second binding's RHS, so it is still an
    alias there -- the unshadowing rule must not be a blanket bypass.
    """
    q = "let Local = A | take 1; let Other = Local | count; Other | take 1"
    assert parse(q).get_referenced_tables() == {"A"}


def test_function_parameter_is_not_a_table():
    q = "let f = (T1:(a:long)){ T1 | count }; T | invoke f()"
    assert parse(q).get_referenced_tables() == {"T"}


def test_function_parameter_exclusion_is_scoped_to_the_body():
    """A real table sharing a parameter's name is still reported.

    The parameter is lexically scoped to the function body; the same name
    used as a source outside it is the table. Asserting on the *set* would
    not show that -- `{"T"}` comes back whether the body's `T` was excluded
    as a parameter, excluded wrongly along with the outer one, or never
    excluded at all. The occurrence list distinguishes them: exactly one
    reference, at the outer `T`.
    """
    q = "let f = (T:(a:long)){ T | count }; T | invoke f()"
    refs = parse(q).find_table_references()
    assert [(name, n.TextStart, n.Width) for name, n in refs] == [("T", 35, 1)]
    assert parse(q).get_referenced_tables() == {"T"}


def test_function_parameter_exclusion_does_not_cross_a_nested_declaration():
    """An inner declaration's parameter is not in scope in the outer body.

    Collecting parameters from the whole `FunctionDeclaration` subtree
    registered a nested declaration's parameter against the *outer* body,
    so the `U` of `union T, U` -- which is outside `g` entirely -- was
    dropped as if it were `g`'s parameter.
    """
    q = (
        "let f = (T:(a:long)){ let g = (U:(b:long)){ U | count }; union T, U };"
        " T | invoke f()"
    )
    assert parse(q).get_referenced_tables() == {"T", "U"}
    # Control: renaming the inner parameter cannot change the answer.
    control = q.replace("(U:(b:long)){ U | count }", "(V:(b:long)){ V | count }")
    assert parse(control).get_referenced_tables() == {"T", "U"}


def test_as_alias_is_not_a_table():
    assert parse("T | as X | join (X) on a").get_referenced_tables() == {"T"}


def test_let_alias_does_not_hide_a_real_table_of_the_same_name_before_it():
    """A `let` binds its name from that statement onward, exactly like `as`.

    Found while fixing the `as` case, and confirmed against the binder
    rather than assumed: parsed with a schema, the leading `X` resolves to a
    `TableSymbol`, so a bound parse always reported it and only the
    syntactic walk lost it.
    """
    q = "X | count; let X = T | take 1"
    assert parse(q).get_referenced_tables() == {"T", "X"}
    schema = {"X": {"a": "string"}, "T": {"a": "string"}}
    assert parse(q, schema=schema).get_referenced_tables() == {"T", "X"}
    assert parse(q).replace_table("X", "Z") == "Z | count; let X = T | take 1"


def test_as_alias_does_not_hide_a_real_table_of_the_same_name_before_it():
    """`| as X` binds X from the `as` onward, not for the whole query.

    The leading `X` here is read before anything rebinds the name, so it is
    a genuine table; a name-keyed exclusion dropped it.
    """
    q = "union X, (T | as X) | count"
    assert parse(q).get_referenced_tables() == {"T", "X"}
    assert parse(q).replace_table("X", "Z") == "union Z, (T | as X) | count"


def test_wildcard_pattern_is_not_a_table():
    """`union T*` names a pattern, not a table -- deliberately excluded."""
    assert parse("union withsource=S T*").get_referenced_tables() == set()


def test_bracketed_table_name_is_reported_unquoted():
    assert parse("['my-table'] | take 1").get_referenced_tables() == {"my-table"}


def test_replace_bracketed_table_name():
    out = parse("['my-table'] | take 1").replace_table("my-table", "Z")
    assert out == "Z | take 1"


# A bracketed *alias* is the other half of that pair. The declaration side of
# a `let` is a NameDeclaration and the use side is a NameReference wrapping a
# BracketedName; until NameDeclaration also read back unquoted, the two
# spellings did not match and the alias escaped the let filter as a table.

BRACKETED_ALIAS_QUERY = "let ['weird-name'] = SecurityEvent;\n['weird-name'] | take 1"


def test_bracketed_let_alias_is_not_a_table():
    assert parse(BRACKETED_ALIAS_QUERY).get_referenced_tables() == {"SecurityEvent"}


def test_replace_table_leaves_a_bracketed_let_alias_alone():
    out = parse(BRACKETED_ALIAS_QUERY).replace_table("weird-name", "Z")
    assert out == BRACKETED_ALIAS_QUERY


def test_table_references_are_one_per_occurrence_in_source_order():
    """Several walker branches see the same node; callers see it once.

    The unbound walk reports a pipe source from both the `ExpressionStatement`
    and the `PipeExpression` branch, and a shadowed `let` RHS from the
    `LetStatement` branch as well -- so the same span came back two or three
    times, while the bound path (whose semantic half visits each node once)
    returned it once. `find_table_references` documents one entry per
    occurrence for both, so both now dedupe by span and sort by it.
    """
    unbound = parse(SHADOW_QUERY).find_table_references()
    bound = parse(
        SHADOW_QUERY, schema={"SecurityEvent": {"a": "string"}}
    ).find_table_references()
    spans = [(name, n.TextStart, n.Width) for name, n in unbound]
    assert spans == [("SecurityEvent", 20, 13)]
    assert spans == [(name, n.TextStart, n.Width) for name, n in bound]

    multi = parse("union C, A | join (B) on x").find_table_references()
    assert [name for name, _ in multi] == ["C", "A", "B"]
    starts = [n.TextStart for _, n in multi]
    assert starts == sorted(starts)


# --- a bound parse keeps tables the schema does not know (K17) -------------
#
# Semantic mode reported only what Microsoft's binder resolved, so any table
# absent from the supplied schema vanished silently -- the opposite failure
# to the false positives above, and the more dangerous one: a partial schema
# is the normal case for a SOC engineer, and the analyzer answered as if the
# missing tables were not in the query at all.

PARTIAL_SCHEMA = {"SecurityEvent": {"Account": "string", "EventID": "long"}}
PARTIAL_UNION = "union SecurityEvent, SigninLogs"


def test_bound_parse_keeps_a_table_absent_from_the_schema():
    q = parse(PARTIAL_UNION, schema=PARTIAL_SCHEMA)
    assert q.has_semantics is True
    assert q.get_referenced_tables() == {"SecurityEvent", "SigninLogs"}


def test_bound_find_table_references_yields_both_nodes_in_source_order():
    """The unresolved node comes back with the span replace_table rewrites."""
    refs = parse(PARTIAL_UNION, schema=PARTIAL_SCHEMA).find_table_references()
    assert [name for name, _ in refs] == ["SecurityEvent", "SigninLogs"]
    assert [(n.TextStart, n.Width) for _, n in refs] == [(6, 13), (21, 10)]


def test_bound_parse_prefers_the_binders_own_reference():
    """A resolved table is not double-counted by the syntactic pass."""
    schema = {"A": {"x": "string"}, "B": {"x": "string"}}
    refs = parse(JOIN_QUERY, schema=schema).find_table_references()
    assert [name for name, _ in refs] == ["A", "B"]


def test_bound_parse_does_not_reintroduce_an_as_alias():
    """An alias is not a table on a bound parse either.

    Two things keep it out: the binder gives it a ``VariableSymbol`` rather
    than no symbol at all, and the syntactic side of the union excludes it.
    """
    q = parse("T | as X | join (X) on a", schema={"T": {"a": "string"}})
    assert q.get_referenced_tables() == {"T"}


def test_bound_parse_does_not_reintroduce_an_unmatched_wildcard():
    """Here the syntactic filter is the only thing standing in the way.

    A wildcard matching nothing in the schema gets *no* symbol from the
    binder, so ``ReferencedSymbol is None`` admits it and only the
    pattern exclusion keeps it out — the reason table extraction had to be
    cleaned up before the union was built on it.
    """
    q = parse("union withsource=S Unknown*", schema={"Other": {"a": "string"}})
    assert q.get_referenced_tables() == set()
    assert q.replace_table("Unknown*", "Z") == "union withsource=S Unknown*"


def test_bound_parse_does_not_reintroduce_a_wildcard():
    """The pattern text never leaks into a bound result.

    The binder expands ``T*`` itself: against a schema with exactly one
    match it resolves the reference straight to that ``TableSymbol``, so the
    bound answer is the expansion, not the pattern. (Two or more matches
    resolve to a ``GroupSymbol``, which this analyzer does not unpack — a
    bound ``union T*`` over several tables reports none of them. Separate
    gap, not this one.)
    """
    q = parse("union withsource=S T*", schema={"T1": {"a": "string"}})
    assert q.get_referenced_tables() == {"T1"}


def test_bound_parse_keeps_the_shadowing_lets_rhs():
    q = parse(SHADOW_QUERY, schema={"SecurityEvent": {"a": "string"}})
    assert q.get_referenced_tables() == {"SecurityEvent"}
    assert (
        q.replace_table("SecurityEvent", "Z")
        == "let SecurityEvent = Z | where a; SecurityEvent | take 1"
    )
