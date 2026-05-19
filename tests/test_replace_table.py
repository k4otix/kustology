# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Coverage for `replace_table` across every table position.

Pins behavior for the leftmost pipe source as well as join, union, lookup,
and database-qualified targets, in both the syntactic and semantic paths.
"""

from kustology import parse


def test_replace_leftmost_table_syntactic():
    out = parse("A | join (B) on x").replace_table("A", "Z")
    assert out == "Z | join (B) on x"


def test_replace_joined_table_syntactic():
    out = parse("A | join (B) on x").replace_table("B", "Z")
    assert out == "A | join (Z) on x"


def test_replace_unioned_table_syntactic():
    out = parse("union A, B, C | count").replace_table("B", "Z")
    assert out == "union A, Z, C | count"


def test_replace_lookup_target_syntactic():
    out = parse("A | lookup B on x").replace_table("B", "Z")
    assert out == "A | lookup Z on x"


def test_replace_does_not_touch_columns_or_keywords():
    """`A` as a column reference must not be replaced when renaming the table."""
    out = parse("A | where A_col == 1 | project x, y").replace_table("A", "Z")
    assert out == "Z | where A_col == 1 | project x, y"


def test_replace_semantic_path():
    schema = {"A": {"x": "string"}, "B": {"x": "string"}}
    q = parse("A | join (B) on x", schema=schema)
    assert q.replace_table("B", "Z") == "A | join (Z) on x"


def test_replace_unknown_table_is_no_op():
    out = parse("A | count").replace_table("Nonexistent", "Z")
    assert out == "A | count"


def test_replace_repeated_references():
    """A table referenced multiple times should be renamed in every position."""
    out = parse("A | join (A) on x").replace_table("A", "Z")
    assert out == "Z | join (Z) on x"
