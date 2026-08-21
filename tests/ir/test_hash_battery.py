# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Minimal-pair collision battery for ``semantic_hash``.

``QueryIR.semantic_hash`` is kustology's contract for "these two queries mean
the same thing" -- consumers deduplicate detection rules on it. That contract
has exactly two ways to break, and they are opposite failure modes with the
same root cause (a canonicalization rule that is either too aggressive or not
aggressive enough):

* A **collision**: two queries with different meaning hash the same. A rule
  library silently drops one of them as a "duplicate". This is the dangerous
  direction -- it is quiet, it does not show up as a test failure or a crash,
  and the missing rule is only noticed (if ever) when the threat it would
  have caught gets through.
* A **split**: two queries with the same meaning hash differently. A rule
  library keeps both as distinct entries. Merely wasteful, not dangerous, but
  it is the failure this whole feature exists to prevent, so it still counts.

A minimal pair -- two queries differing in exactly one respect -- is the
right shape to catch either one, because a failure localizes to the one
thing that changed. ``T | where a == 1`` vs ``T | where a == 2`` failing
tells you the literal-value path is broken; a large realistic query
failing tells you nothing about which of its twenty features is at fault.

``MUST_DIFFER`` covers pairs that mean different things: every one is a
potential collision. ``MUST_EQUAL`` covers pairs that mean the same thing
under a different spelling: every one is a potential split. Both lists
parse each query and compare ``QueryIR.semantic_hash`` directly -- no
schema binding, since none of these pairs need one, and binding roughly
doubles the cost of each case for no discriminating power (see
``test_semantic_hash_bind_invariance.py`` for the bind-state axis).

One category needs a caveat rather than a claim of coverage. WS2 fix #1
(datetime literals UTC-normalized in ``literal_value_and_ticks`` so
``semantic_hash`` stops depending on the host timezone, commit ``1ae3488``)
has no dedicated pair below that discriminates the buggy code from the
fixed code on every host. ``tz-offset-vs-zulu-same-instant`` asserts that
``datetime(2024-01-01T05:00:00Z)`` and ``datetime(2024-01-01T00:00:00-05:00)``
hash alike, which is true and worth keeping as a same-instant sanity pin,
but it cannot fail either way, on any host: probing the raw .NET node
directly (bypassing this library's normalization entirely) shows both
spellings parse through ``DateTime.Parse`` to ``Kind=Local`` with
identical raw ``.Ticks`` -- ``638396640000000000`` for both -- before
``literal_value_and_ticks`` ever runs. That is not an artifact of this
host's offset; it follows from how .NET parses *any* explicit-offset
literal: the text is first resolved to its UTC instant using the offset
written in it, then re-projected onto the host's current offset to
produce the ``Local`` value. Two spellings of the same instant resolve to
the same UTC instant by definition, and re-projecting one UTC instant
through one host offset has exactly one answer -- so any two
explicit-offset spellings of one instant collapse to identical raw ticks
on every host, buggy code or fixed. No pair built from two explicit-offset
spellings can discriminate this fix, and that holds structurally, not just
on the checkout where it was checked.

``naive-vs-zulu-datetime`` does discriminate, but only conditionally: it
pairs an ``Unspecified``-kind literal (never touched by the ``Local``
half of the bug) against a ``Local``-kind one, and the pre-fix code reads
the ``Local`` literal's raw, host-offset-shifted ticks, which differ from
the ``Unspecified`` literal's ticks exactly when the host's current UTC
offset is nonzero. It has real teeth on this repository's usual dev/CI
timezone (EDT/EST, UTC-5 in January) but would pass by accident -- not
because the fix is doing anything -- on a host whose current offset is
zero, since a ``Local`` value parsed there already has the correct ticks
without ``ToUniversalTime()``. Pairing the naive literal against a
non-``Z`` explicit offset instead does not fix this: ``Z`` already parses
to ``Kind=Local`` exactly like any other offset suffix (same probe), so
it would exercise the identical branch and inherit the identical
host-dependency rather than a new one.

Fix #1's only guard that is sound on every host is
``tests/ir/test_literals.py::test_datetime_literal_is_utc_and_tz_independent``,
which re-parses the same query in a real ``TZ=Asia/Tokyo`` subprocess and
asserts the hash does not move -- an actual cross-timezone comparison,
which a single-process pair here structurally cannot perform. The
datetime pairs in this file are still worth keeping: ``datetime-value``
catches a broken literal-value comparison outright (though it never
touches the ``Kind`` bug, since both its literals are bare/``Unspecified``),
and both ``MUST_EQUAL`` datetime pairs still pin that the two ``Kind``
branches produce a matching, self-consistent representation. None of them,
individually or together, substitutes for that subprocess test.

Two things are deliberately absent:

* WS4 will append its own discriminators here later (sort direction,
  ``fork``, ``datatable``, ``union``/``mv-expand``/``parse``/``search``/
  ``make-series`` params, join default, database qualifier) once the model
  fields those depend on exist. Writing them now would either fail against
  today's builder or -- worse -- pass by accident and assert a gap as if it
  were a guarantee: e.g. ``T | sort by a asc`` and ``T | sort by a desc``
  currently hash the *same*, because ``OrderedExpression`` drops direction
  on the floor (``IRBuilder._visit_expr``), and a ``datatable(...)`` literal
  collapses to a bare ``FuncCallSource`` with no schema or rows recorded.
  Appending discriminators for those belongs to the task that adds the
  fields, not this one.
* ``T | where "y" =~ X`` vs ``T | where X =~ "y"`` is not a ``MUST_EQUAL``
  pair. ``canonical()`` sorts commutative operand lists (``and``/``or``/set
  membership) but does not sort a ``BinOp``'s ``left``/``right`` for any
  operator, so a directly-written operand swap on ``=~`` hashes differently
  from the equivalent query with the literal on the other side. That is a
  known, pre-existing limitation logged for the whole-branch review, not a
  bug this task fixes -- see the module docstring's history for context.
  ``lt-vs-gt-swapped`` below exercises the same limitation deliberately
  (``a < b`` vs ``b > a``, mathematically the same predicate, hashed apart)
  because the task brief calls it out by name as a required ``MUST_DIFFER``
  case: it pins *current* behaviour, not a claim that the behaviour is right.
"""

import pytest

from kustology import parse
from kustology.ir import FilterOp, compute_semantic_hash


def _hash(query: str) -> str:
    return parse(query).to_ir().semantic_hash


# ---------------------------------------------------------------------------
# MUST_DIFFER: (case_id, query_a, query_b) -- different meaning, must hash
# apart. A failure here is a silent collision: two distinct detection rules
# would be deduplicated into one.
# ---------------------------------------------------------------------------

MUST_DIFFER = [
    ("literal-value", "T | where a == 1", "T | where a == 2"),
    ("column-name", "T | where a == 1", "T | where b == 1"),
    ("table-name", "T | where a == 1", "U | where a == 1"),
    ("gt-vs-ge", "T | where a > 1", "T | where a >= 1"),
    ("lt-vs-le", "T | where a < 1", "T | where a <= 1"),
    ("lt-vs-gt-swapped", "T | where a < b", "T | where b > a"),
    ("subtraction-order", "T | where a - b > 0", "T | where b - a > 0"),
    ("literal-subtraction-order", "T | where a - 2 > 0", "T | where 2 - a > 0"),
    ("has-vs-contains", 'T | where x has "a"', 'T | where x contains "a"'),
    ("has-vs-has-cs", 'T | where x has "a"', 'T | where x has_cs "a"'),
    ("contains-vs-contains-cs", 'T | where x contains "a"', 'T | where x contains_cs "a"'),
    ("startswith-vs-endswith", 'T | where x startswith "a"', 'T | where x endswith "a"'),
    ("startswith-vs-startswith-cs", 'T | where x startswith "a"', 'T | where x startswith_cs "a"'),
    ("endswith-vs-endswith-cs", 'T | where x endswith "a"', 'T | where x endswith_cs "a"'),
    ("hasprefix-vs-hassuffix", 'T | where x hasprefix "a"', 'T | where x hassuffix "a"'),
    ("has-negated", 'T | where x has "a"', 'T | where x !has "a"'),
    ("in-vs-not-in", 'T | where x in ("a")', 'T | where x !in ("a")'),
    ("in-vs-in-tilde", 'T | where x in ("a","b")', 'T | where x in~ ("a","b")'),
    ("has-any-vs-has-all", 'T | where x has_any ("a","b")', 'T | where x has_all ("a","b")'),
    ("between-polarity", "T | where a between (1 .. 5)", "T | where a !between (1 .. 5)"),
    ("between-bounds-order", "T | where a between (1 .. 5)", "T | where a between (5 .. 1)"),
    ("isnotnull-vs-isnotempty", "T | where isnotnull(x)", "T | where isnotempty(x)"),
    ("and-or-grouping", "T | where a and (b or c)", "T | where (a and b) or c"),
    ("not-scope", "T | where not(a and b)", "T | where not(a) and b"),
    ("arith-precedence", "T | project x = (a + b) * c", "T | project x = a + b * c"),
    ("project-column-order", "T | project a, b", "T | project b, a"),
    ("summarize-by-order", "T | summarize count() by a, b", "T | summarize count() by b, a"),
    ("operator-order", "T | where a == 1 | take 5", "T | take 5 | where a == 1"),
    ("take-count", "T | take 5", "T | take 6"),
    ("group-key", "T | summarize count() by a", "T | summarize count() by b"),
    ("agg-func", "T | summarize max(a)", "T | summarize min(a)"),
    ("join-side", "T | join U on $left.a == $right.b", "T | join U on $left.a == $left.b"),
    ("join-vs-lookup", "T | join U on a", "T | lookup U on a"),
    ("case-insensitive-eq-vs-eq", 'T | where x =~ "y"', 'T | where x == "y"'),
    ("neq-vs-not-match", 'T | where x != "y"', 'T | where x !~ "y"'),
    ("int-vs-real-literal", "T | where a == 1", "T | where a == 1.0"),
    ("int-vs-string-literal", "T | where a == 1", 'T | where a == "1"'),
    ("timespan-unit", "T | where d > ago(1h)", "T | where d > ago(1d)"),
    ("bool-literal", "T | where a == true", "T | where a == false"),
    ("eq-vs-neq", "T | where a == 1", "T | where a != 1"),
    ("extend-name", "T | extend z = 1", "T | extend y = 1"),
    ("project-column", "T | project a", "T | project b"),
    ("project-away-vs-keep", "T | project-away a", "T | project-keep a"),
    ("distinct-column", "T | distinct a", "T | distinct b"),
    ("case-branch-order", 'T | where case(a > 1, "x", "y") == "x"', 'T | where case(a > 1, "y", "x") == "x"'),
    ("print-value", "print x = 1", "print x = 2"),
    ("and-vs-or", "T | where a == 1 and b == 2", "T | where a == 1 or b == 2"),
    ("func-name", 'T | where hash_sha256(x) == "a"', 'T | where hash_md5(x) == "a"'),
    (
        "assert-schema-extra-column",
        "T | assert-schema (a:long)",
        "T | assert-schema (a:long, table:long)",
    ),
    ("assert-schema-column-type", "T | assert-schema (a:long)", "T | assert-schema (a:string)"),
    ("datetime-value", "T | where D == datetime(2024-01-01)", "T | where D == datetime(2024-01-02)"),
    (
        "tolower-mismatched-case-vs-case-insensitive-eq",
        'T | where tolower(x) == "Y"',
        'T | where x =~ "Y"',
    ),
]


@pytest.mark.parametrize("case_id,query_a,query_b", MUST_DIFFER, ids=[c[0] for c in MUST_DIFFER])
def test_must_differ(case_id, query_a, query_b):
    hash_a, hash_b = _hash(query_a), _hash(query_b)
    assert hash_a != hash_b, (
        f"{case_id}: {query_a!r} and {query_b!r} mean different things but "
        f"both hashed to {hash_a!r} -- a rule dedup on semantic_hash would "
        f"silently drop one of them"
    )


# ---------------------------------------------------------------------------
# MUST_EQUAL: (case_id, query_a, query_b) -- same meaning under a different
# spelling, must hash alike. A failure here is a silent split: two rules
# that mean the same thing would both survive dedup as if they didn't.
# ---------------------------------------------------------------------------

MUST_EQUAL = [
    ("whitespace", "T | where a == 1", "T\n| where a==1"),
    ("trailing-comment", "T | where a == 1", "T | where a == 1 // note"),
    ("redundant-parens", "T | where a == 1", "T | where (a == 1)"),
    ("consecutive-filters", "T | where a == 1 | where b == 2", "T | where a == 1 and b == 2"),
    ("and-assoc-flatten", "T | where a and (b and c)", "T | where (a and b) and c"),
    ("or-assoc-flatten", "T | where a or (b or c)", "T | where (a or b) or c"),
    ("and-operand-order", "T | where a == 1 and b == 2", "T | where b == 2 and a == 1"),
    ("or-operand-order", "T | where (a == 1) or (b == 2)", "T | where (b == 2) or (a == 1)"),
    ("double-negation", "T | where not(not(a == 1))", "T | where a == 1"),
    ("tolower-matching-case-eq", 'T | where tolower(x) == "y"', 'T | where x =~ "y"'),
    ("tolower-matching-case-neq", 'T | where tolower(x) != "y"', 'T | where x !~ "y"'),
    ("quote-style", "T | where x == 'y'", 'T | where x == "y"'),
    ("timespan-1h-vs-60m", "T | where d > ago(1h)", "T | where d > ago(60m)"),
    ("naive-vs-zulu-datetime", "T | where d > datetime(2024-01-01)", "T | where d > datetime(2024-01-01T00:00:00Z)"),
    # Same-instant sanity pin, NOT a guard for fix #1's host-timezone
    # independence: both spellings parse to Kind=Local with identical raw
    # .Ticks before this library's normalization ever runs, on any host, so
    # this pair cannot fail either way. See the module docstring for why
    # that is provable rather than merely observed, and for the real guard
    # (test_literals.py::test_datetime_literal_is_utc_and_tz_independent).
    (
        "tz-offset-vs-zulu-same-instant",
        "T | where d > datetime(2024-01-01T05:00:00Z)",
        "T | where d > datetime(2024-01-01T00:00:00-05:00)",
    ),
    ("raw-string-vs-escaped", 'T | where x == @"a\\b"', 'T | where x == "a\\\\b"'),
    ("let-alias-rename", "let X = T | where a==1;\nX | take 1", "let Y = T | where a==1;\nY | take 1"),
]


@pytest.mark.parametrize("case_id,query_a,query_b", MUST_EQUAL, ids=[c[0] for c in MUST_EQUAL])
def test_must_equal(case_id, query_a, query_b):
    hash_a, hash_b = _hash(query_a), _hash(query_b)
    assert hash_a == hash_b, (
        f"{case_id}: {query_a!r} and {query_b!r} mean the same thing but "
        f"hashed apart ({hash_a!r} vs {hash_b!r}) -- a rule dedup on "
        f"semantic_hash would keep both as if they didn't"
    )


def test_double_negation_collapses_at_a_bare_expr_root():
    """``double-negation`` above exercises ``not(not(X))`` nested inside a
    ``FilterOp`` -- the replacement ``normalize_in_place`` returns gets
    installed via the parent's ``setattr`` in ``_normalize_node``. There is
    a second path with no parent to install into: ``compute_semantic_hash``
    accepts a bare ``Expr`` subtree directly (its own docstring says so),
    and at the root of *that* call there is no field to rebind through --
    ``normalize_expressions`` has to hand back the replacement itself, which
    is exactly the "Rebind" case its docstring calls out. This is the "incl.
    root" half of the ``double-negation`` category: a bug in the rebind-at-
    root path would not show up nested inside a query, only when a caller
    hashes an extracted predicate on its own.
    """

    def predicate(query: str):
        ir = parse(query).to_ir()
        (op,) = (o for o in ir.main_pipeline.operators if isinstance(o, FilterOp))
        return op.predicate

    nested = predicate("T | where not(not(a == 1))")
    plain = predicate("T | where a == 1")
    assert compute_semantic_hash(nested) == compute_semantic_hash(plain), (
        "not(not(a == 1)) and a == 1 must hash alike even when compute_semantic_hash "
        "is called on the bare predicate Expr rather than the whole query"
    )


# WS4 appends its own MUST_DIFFER / MUST_EQUAL cases below this line once the
# model fields they depend on exist: sort direction, fork, datatable, union/
# mv-expand/parse/search/make-series params, join default, database
# qualifier. See the module docstring for why they are not here yet.
