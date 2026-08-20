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
