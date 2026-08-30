# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Coverage for `replace_table` across every table position.

The leftmost pipe source, join, union, lookup, and database-qualified
targets, in both the syntactic and semantic paths.
"""

import pytest

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


def test_replace_a_table_the_schema_does_not_know():
    """A bound parse must still rewrite a table the binder could not resolve:
    otherwise a retarget against a partial schema silently ships the old name."""
    schema = {"SecurityEvent": {"Account": "string"}}
    q = parse("union SecurityEvent, SigninLogs", schema=schema)
    assert q.has_semantics is True
    assert q.replace_table("SigninLogs", "X") == "union SecurityEvent, X"
    assert q.replace_table("SecurityEvent", "X") == "union X, SigninLogs"


def test_replace_never_rewrites_a_wildcard_pattern():
    """The binder expands `T*` to a single match; the rewrite must not follow.

    `get_referenced_tables()` reports `T1` because that is what the query
    reads today. Rewriting that span would put a fixed name where the pattern
    is, so the query stops picking up a second `T…` table once one exists.
    """
    q = parse("union withsource=S T*", schema={"T1": {"a": "string"}})
    assert q.get_referenced_tables() == {"T1"}
    assert q.replace_table("T1", "Z") == "union withsource=S T*"
    assert q.replace_table("T*", "Z") == "union withsource=S T*"


def test_replace_repeated_references():
    """A table referenced multiple times should be renamed in every position."""
    out = parse("A | join (A) on x").replace_table("A", "Z")
    assert out == "Z | join (Z) on x"


def test_replace_rejects_an_empty_new_name():
    """An empty new name is rejected up front. Spliced in, it returns
    ` | count`, a query the parser rejects, and the caller sees no error."""
    q = parse("A | count")
    with pytest.raises(ValueError) as exc_info:
        q.replace_table("A", "")
    assert "new_name" in str(exc_info.value)


def test_replace_rejects_an_empty_old_name():
    q = parse("A | count")
    with pytest.raises(ValueError) as exc_info:
        q.replace_table("", "Z")
    assert "old_name" in str(exc_info.value)


def test_replace_rejects_a_non_string_name():
    """Left to the concatenation, a non-string raises ``can only concatenate
    str (not "NoneType") to str``, a message about this function's internals.
    The TypeError names ``new_name``."""
    q = parse("A | count")
    with pytest.raises(TypeError) as exc_info:
        q.replace_table("A", None)
    assert "new_name" in str(exc_info.value)
    assert "NoneType" in str(exc_info.value)


def test_replace_brackets_a_new_name_that_is_not_an_identifier():
    """`my-new-table` is a legal Kusto table name and an illegal bare
    identifier: raw, it parses as the subtraction `my - new - table`, so the
    rewritten query reads no table. Bracketed-name quoting is KQL's answer."""
    out = parse("A | count").replace_table("A", "my-new-table")
    assert out == "['my-new-table'] | count"
    # The output still parses and still names the table the caller asked for.
    assert parse(out).get_referenced_tables() == {"my-new-table"}


def test_replace_brackets_every_occurrence_and_switches_quote_style():
    """Back-to-front rewriting keeps the later spans valid, though the
    bracketed form is longer than the name it replaces.

    A `'` inside the name would close a single-quoted literal early, so
    `KustoFacts.BracketNameIfNecessary` switches the whole literal to double
    quotes. That is the one case where the emitted text is not `['...']`.
    """
    out = parse("A | join (A) on x").replace_table("A", "space name")
    assert out == "['space name'] | join (['space name']) on x"
    assert parse(out).get_referenced_tables() == {"space name"}

    quoted = parse("A | count").replace_table("A", "o'brien")
    assert quoted == '["o\'brien"] | count'
    assert parse(quoted).get_referenced_tables() == {"o'brien"}


def test_replace_brackets_only_the_positions_that_are_really_tables():
    """Quoting must not widen what gets rewritten.

    The alias filters are positional and the bracketed form is four characters
    longer than the name it replaces, so this pins the shadowing rule (a
    `let`'s own right-hand side is the real table, every later use is the
    alias) and the `| as` exclusion against a length change.
    """
    out = parse("let T = T | where x == 1; T | take 1").replace_table("T", "my-new-table")
    assert out == "let T = ['my-new-table'] | where x == 1; T | take 1"

    aliased = parse("union A, (A | as A2), A2").replace_table("A", "a b")
    assert aliased == "union ['a b'], (['a b'] | as A2), A2"


def test_replace_leaves_an_identifier_new_name_unquoted():
    """The control: a plain identifier must not grow brackets, or every
    ordinary rename would start emitting noise."""
    assert parse("A | count").replace_table("A", "Z_9") == "Z_9 | count"


@pytest.mark.parametrize(
    "new_name",
    [
        # A KQL keyword matches `[A-Za-z_][A-Za-z0-9_]*` and is still not
        # usable bare. `project | count` validates with zero diagnostics and
        # reads no table, the silent failure the quoting exists to prevent.
        "project",
        "where",
        "union",
        "datatable",
        # Not keywords: the shapes the regex catches, in the same matrix so
        # one assertion covers both classes.
        "my-new-table",
        "space name",
        "o'brien",
        'quote"inside',
        "line\nbreak",
        "back\\slash",
        "9leading",
        # And the control that must stay bare.
        "Z_9",
    ],
)
def test_replace_emits_a_name_that_parses_and_resolves(new_name):
    """Two assertions per name, because "it parses" is not sufficient.

    `project | count` has no diagnostics and no tables, so the second
    assertion -- the rewritten query still reads the table the caller named --
    is what pins the keyword case.
    """
    out = parse("A | count").replace_table("A", new_name)
    reparsed = parse(out)
    assert reparsed.diagnostics == [], out
    assert reparsed.get_referenced_tables() == {new_name}, out


def test_replace_leaves_a_bare_identifier_bare_across_the_matrix():
    """The other half of the matrix: quoting applies only where it is needed.
    A rename to an ordinary identifier is the common call and stays bare."""
    for name in ("Z_9", "T", "_private", "SecurityEvent"):
        assert parse("A | count").replace_table("A", name) == f"{name} | count"
