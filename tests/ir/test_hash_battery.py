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

A third list, ``KNOWN_COLLISIONS``, holds pairs that mean different things
and hash alike *today* -- open gaps in the model rather than regressions.
They cannot go in ``MUST_DIFFER``, which they would keep red, and they must
not go in ``MUST_EQUAL``, whose contract is "same meaning": a real collision
filed as a desired merge would make the paragraph above false, and a
documented survivor list that has quietly stopped being true is the exact
failure this file exists to prevent. Its assertion is equality, so closing a
gap turns the list red on purpose -- a consumer works around the collisions
kustology discloses, so one silently disappearing from the disclosure is a
defect even though the behavior improved. **The list is empty**: nothing
currently needs disclosing as a known gap. It stays for the next one; see
the comment above it.

One category needs a caveat rather than a claim of coverage. WS2 fix #1
(datetime literals UTC-normalized in ``literal_value_and_ticks`` so
``semantic_hash`` stops depending on the host timezone, commit ``1ae3488``)
is guarded in-process (see ``naive-vs-zulu-datetime`` below and the
paragraph that covers it), but one of the two pairs that looks like it
guards the fix does not. ``tz-offset-vs-zulu-same-instant`` asserts that
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

``naive-vs-zulu-datetime`` does discriminate fix #1's ``Local`` branch, and
it does so on every host, not merely on this repository's usual dev/CI
timezone. The obvious channel -- ``ticks`` -- *is* host-offset-dependent: a
pre-fix ``Local`` literal's raw ticks only diverge from the ``Unspecified``
literal's ticks when the host's current UTC offset is nonzero, and the two
coincide by accident at offset zero. But ``ticks`` is not the only field in
the hash payload; ``LiteralExpr.value`` (the ``.ToString("o")`` rendering)
is not volatile and is hashed too, and it diverges unconditionally, on
every host including a UTC one: .NET's round-trip format renders an
``Unspecified`` value with no offset marker at all (``...0000000``) and a
``Local`` one with an explicit offset (``...0000000+00:00`` even when that
offset is zero). Simulating the pre-fix code (reading ``raw.Ticks`` and
``raw.ToString("o")`` straight off, no ``Kind`` branch) and diffing the
resulting ``semantic_hash`` under both ``TZ=UTC`` and ``TZ=Asia/Tokyo``
confirms the pair fails in both regimes, for that reason -- this is the
same mechanism commit ``1ae3488``'s own message flags when it notes two
pinned literal values changing from ``...0000000`` to ``...0000000Z``.
``naive-vs-zulu-datetime`` is therefore this file's in-process,
host-independent guard for fix #1.

What does *not* generalize is a pair built from two explicit-offset
(``Local``-vs-``Local``) spellings of one instant -- the shape
``tz-offset-vs-zulu-same-instant`` uses. Both sides of such a pair carry an
offset marker in ``value`` pre-fix as well as post-fix, so the asymmetry
that gives ``naive-vs-zulu-datetime`` its teeth (marker present on one side,
absent on the other) is not available; per the previous paragraph, both
``ticks`` and ``value`` already coincide before this library's
normalization ever runs. That is the narrower claim this file's structure
actually supports, not the broader one that no in-process pair can reach
fix #1 at all.

``tests/ir/test_literals.py::test_datetime_literal_is_utc_and_tz_independent``
is still worth naming here: it is the guard for the cross-process case
this file cannot reproduce (a real ``TZ=Asia/Tokyo`` subprocess re-parse,
proving the *actual* fixed code, not a simulation of the unfixed code, is
stable across hosts) and it is fix #1's only guard for that shape of proof
-- but it is not fix #1's only guard, full stop. ``datetime-value`` is
also worth keeping though it never touches the ``Kind`` bug (both its
literals are bare/``Unspecified``), and both ``MUST_EQUAL`` datetime pairs
still pin that the two ``Kind`` branches produce a matching,
self-consistent representation.

The WS4 block at the end of each list is the regression net for a whole
workstream. Nine tasks guard against a family of *lossy lowering* bugs: the
builder reaches a fully populated node from several different KQL
constructs, so nothing looks stubbed even though the distinction between
the constructs is gone from the digest. Each such collapse is, by
construction, a ``semantic_hash`` collision, and a collision is the failure
mode this file exists to catch and the one no other test in the suite is
shaped to see: a per-task test asserts that a field holds the right value,
which stays true if a *later* change stops that field from reaching the
digest. The pairs below assert the consequence instead.

They are written against the model as it stands rather than as the
workstream planned it, which is not the same list: ``bag_expansion`` was
dropped and folded into a required ``expand_kind`` (so ``bagexpansion=array``
and ``kind=array`` are a ``MUST_EQUAL`` pair, not two discriminators),
``search_kind`` gained the effective default ``default``, and
``project-reorder`` grew a ``ReorderKey`` that the original plan did not
have. Where a field carries KQL's *effective* default (D8) the pairing is
a ``MUST_EQUAL`` -- a bare ``join`` really does mean ``kind=innerunique`` --
and the ``MUST_DIFFER`` runs against the spelling that names a different
operator.

One thing is deliberately absent:

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
  case: it pins *current* behavior, not a claim that the behavior is right.
"""

import pytest

from kustology import parse
from kustology.ir import walk


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
    # `LetFunction`, whole. Every part of a `let`-declared function reaches
    # the digest -- the signature (parameter count, declared types, defaults,
    # `view`) and the body, statements and tail alike. Three of the rows below
    # (`-parameter-type`, `-parameter-default`, `-body`) were KNOWN_COLLISIONS
    # until the body was built; the body one was the largest gap in the
    # release, because what collided was an arbitrary amount of query rather
    # than one modifier.
    #
    # A parameter's *name* is deliberately not among them: it is a local label
    # like a `let` name, and is alpha-canonicalized to `$param<i>` by
    # declaration position (see `let-function-parameter-name` in MUST_EQUAL,
    # which was a row here until 0.2.0). What that buys has to be paid for in
    # this list: once the name stops mattering, *which* parameter a reference
    # reads has to start mattering, and a rename that fired on names it was
    # never given has to be caught. The next two rows are those two guards.
    (
        "let-function-param-positional",
        "let S = (a:int, b:int) { A | where x > a }; S(1, 2)",
        "let S = (a:int, b:int) { A | where x > b }; S(1, 2)",
    ),
    # The call-site rename is map-gated rather than blanket: a call is
    # rewritten only when a *visible* `let` declared that name. Neither `f`
    # nor `g` is one -- both name a server-side function the query never
    # declared -- so they must still split, while the `n` written beside them
    # renames identically on both sides.
    (
        "let-function-body-server-call",
        "let n = 5; let S = () { A | where x > f(n) }; S()",
        "let n = 5; let S = () { A | where x > g(n) }; S()",
    ),
    # The other half of the gate, and the sharper one: a visible `let` of the
    # call's name is *not* enough, because KQL keeps values and functions in
    # separate namespaces. `let abs = 5; T | extend y = abs(x)` binds a value
    # called `abs` and still calls the built-in `abs`, and Microsoft's binder
    # accepts it with no diagnostic -- `let abs = 5; T | extend y = abs(x) +
    # abs` resolves both readings at once. So a name-only gate renamed the
    # call to the binding's `$let<i>` and merged two queries computing
    # different things. Only a `let` that bound a *function* (`rhs_function`)
    # renames its call sites; the same holds for a stored server-side function
    # of that name, which no schema in this suite can show but which the
    # cluster resolves identically. KS211 ("does not refer to any known
    # function") fires only when no function of the name exists at all, so it
    # is not a signal that could have been used here.
    (
        "let-scalar-shadowing-a-builtin-call",
        "let abs = 5; T | extend y = abs(x)",
        "let sqrt = 5; T | extend y = sqrt(x)",
    ),
    (
        "let-function-parameter-count",
        "let S = (w:int) { A | where x > 1 }; S(5)",
        "let S = (w:int, y:int) { A | where x > 1 }; S(5, 1)",
    ),
    (
        "let-function-parameter-type",
        "let S = (w:int) { A | where x > w }; S(5)",
        "let S = (w:long) { A | where x > w }; S(5)",
    ),
    # Presence of a default, then its value: a parameter that may be omitted
    # is a different signature from one that may not, and two defaults are two
    # different functions for every call that omits the argument.
    (
        "let-function-parameter-default",
        "let S = (w:int) { A | where x > w }; S(5)",
        "let S = (w:int=3) { A | where x > w }; S(5)",
    ),
    (
        "let-function-default-value",
        "let S = (w:int=3) { A | where x > w }; S(5)",
        "let S = (w:int=4) { A | where x > w }; S(5)",
    ),
    # `view` decides whether a wildcard `union *` picks the function up, so
    # the two spellings return different rows from the same data.
    (
        "let-function-view-vs-plain",
        "let S = (w:int) { A | where x > w }; S(5)",
        "let S = view (w:int) { A | where x > w }; S(5)",
    ),
    (
        "let-function-body",
        "let S = (w:int) { A | where EventID == 4625 | summarize c=count() by Account | where c > w }; S(5)",
        "let S = (w:int) { A | where EventID == 4624 | summarize c=count() by Computer | where c > w }; S(5)",
    ),
    # The body's other half of the tail dispatch. A scalar body lands on
    # `body_expr` rather than `body_pipeline`, and a pair over a tabular body
    # alone would stay green if that field stopped being hashed.
    (
        "let-function-scalar-body",
        "let S = (w:int) { w + 1 }; T | extend y = S(1)",
        "let S = (w:int) { w + 2 }; T | extend y = S(1)",
    ),
    # Guards a `let` written inside the body staying scoped to `body_lets`
    # rather than reaching the digest through top-level `let_bindings`: the
    # route must not change the answer.
    (
        "let-function-body-nested-let-value",
        "let S = (w:int) { let z = 5; A | take z }; S(1)",
        "let S = (w:int) { let z = 9; A | take z }; S(1)",
    ),
    # -----------------------------------------------------------------------
    # WS4 -- one pair per collision the IR-model workstream closed. Each was
    # a real collision on this branch's first commit: the two queries mean
    # different things and hashed the same, because the builder lowered them
    # onto one node. Grouped by the task that closed it.
    # -----------------------------------------------------------------------
    # 4.1 -- SortKey. The builder unwrapped the parser's OrderedExpression
    # and discarded its ordering clause, so direction and null placement --
    # which decide the order rows come back in -- reached neither the IR nor
    # the digest. Three operators share that wrapper.
    ("sort-direction", "T | sort by a asc", "T | sort by a desc"),
    # `sort by a` defaults to `desc` (pinned below in MUST_EQUAL), so it must
    # still hash apart from the explicit `asc` spelling -- the default and
    # the opposite direction are not the same query.
    ("sort-bare-vs-asc", "T | sort by a", "T | sort by a asc"),
    ("sort-nulls-placement", "T | sort by a nulls first", "T | sort by a nulls last"),
    # The nulls clause is a real field with a `None` default of its own,
    # distinct from the direction default above -- writing `nulls first` must
    # reach the digest relative to not writing a nulls clause at all, not
    # merely relative to `nulls last`.
    ("sort-nulls-clause-vs-absent", "T | sort by a desc nulls first", "T | sort by a desc"),
    ("order-by-direction", "T | order by a asc", "T | order by a desc"),
    ("top-by-direction", "T | top 5 by a asc", "T | top 5 by a desc"),
    # The `top` analogue of `sort-bare-vs-asc` above: `top 5 by a` defaults to
    # `desc` too (`top-bare-is-desc` in MUST_EQUAL), so it must hash apart
    # from the explicit `asc` spelling for the same reason.
    ("top-bare-vs-asc", "T | top 5 by a", "T | top 5 by a asc"),
    ("sort-per-key-direction", "T | sort by a asc, b desc", "T | sort by a desc, b asc"),
    # 4.1 fix round -- ReorderKey. `project-reorder` is the third consumer of
    # the same wrapper; with the unwrap gone it fell through to an
    # UnknownExpr and lost the column identity with it. Its direction is
    # optional, so unwritten is a third distinct state rather than a default.
    ("project-reorder-direction", "T | project-reorder a asc", "T | project-reorder a desc"),
    ("project-reorder-direction-vs-unwritten", "T | project-reorder a", "T | project-reorder a asc"),
    # 4.2 -- ForkBranch. `ForkOp.pipelines` was declared and never populated:
    # every branch came back empty, so any two forks were one node and
    # nothing inside a branch was reachable at all.
    ("fork-branch-bodies", "T | fork (take 1) (count)", "T | fork (count) (where x == 1)"),
    ("fork-branch-name", "T | fork a=(count) (take 1)", "T | fork b=(count) (take 1)"),
    ("fork-branch-order", "T | fork (take 1) (count)", "T | fork (count) (take 1)"),
    # 4.3 -- source position. Four queries built indistinguishable sources: a
    # datatable collapsed to a bare FuncCallSource with no schema and no
    # rows, externaldata had no source class at all, and a qualifier or a
    # wildcard on a table name was not read.
    ("datatable-rows", "datatable(a:long)[1,2] | count", "datatable(a:long)[3,4] | count"),
    ("datatable-schema", "datatable(a:long)[1] | count", "datatable(b:long)[1] | count"),
    # The schema is a (name, type) list and the pair above varies only the
    # name, so it would stay green if the type half stopped being read.
    # `assert-schema` already treats the type as worth its own case.
    (
        "datatable-column-type",
        "datatable(a:long)[1] | count",
        "datatable(a:int)[1] | count",
    ),
    (
        "externaldata-uris",
        'externaldata(a:string)["https://x/1.csv"] | count',
        'externaldata(a:string)["https://x/2.csv"] | count',
    ),
    # `uris` was the only externaldata field with a pair, so the other three
    # reached the digest unguarded. `columns` is the same name-vs-type point
    # as the datatable pair above; one case covers both halves here because
    # the reader is shared (`read_row_schema`) and pinned per-half there.
    (
        "externaldata-format",
        'externaldata(a:string)["https://x/1.csv"] with (format="csv") | count',
        'externaldata(a:string)["https://x/1.csv"] with (format="json") | count',
    ),
    (
        "externaldata-columns",
        'externaldata(a:string)["https://x/1.csv"] | count',
        'externaldata(b:string)["https://x/1.csv"] | count',
    ),
    # A real collision until this review round, and the corpus fixture
    # `ExternalData_CsvFeed.kql` writes the very modifier that was dropped:
    # `ignoreFirstRecord` skips the CSV header, so the feed yields one fewer
    # row. Only `format` was read out of the with-clause, and a source node
    # has no `raw_text` for the rest to survive in.
    (
        "externaldata-ignore-first-record",
        (
            'externaldata(a:string)["https://x/1.csv"] '
            'with (format="csv", ignoreFirstRecord=true) | count'
        ),
        'externaldata(a:string)["https://x/1.csv"] with (format="csv") | count',
    ),
    ("database-qualifier", 'database("d1").T | count', 'database("d2").T | count'),
    (
        "cluster-qualifier",
        'cluster("c1").database("d").T | count',
        'cluster("c2").database("d").T | count',
    ),
    # `union T*` names every table matching the pattern; `union ['T*']` names
    # one table actually called `T*`.
    ("wildcard-vs-quoted-table", "union T* | count", "union ['T*'] | count"),
    # 4.4 -- operator parameters. Every modifier below changes the rows the
    # operator returns and none of them was read.
    ("mv-expand-to-typeof", "T | mv-expand a to typeof(string)", "T | mv-expand a to typeof(long)"),
    ("mv-expand-limit", "T | mv-expand a limit 10", "T | mv-expand a limit 20"),
    # `with_itemindex=i` vs bare is retired here: `test_operator_params.py`'s
    # `test_mv_expand_modifiers_all_reach_the_hash` pairwise-checks it (as
    # "indexed" vs "bare") among five mutually-distinct variants, which is a
    # stronger guarantee than this single pair.
    ("mv-expand-kind", "T | mv-expand kind=array a", "T | mv-expand kind=bag a"),
    (
        "mv-apply-to-typeof",
        "T | mv-apply x=d to typeof(long) on (summarize count())",
        "T | mv-apply x=d on (summarize count())",
    ),
    # The value-level companion: two different written types, not merely
    # written-vs-absent. `getschema-kind`/`consume-decodeblocks` below have
    # had this shape since 0.2.0; these four modifiers only had the
    # present-vs-absent pair above until this pair was added.
    (
        "mv-apply-to-typeof-values",
        "T | mv-apply x=d to typeof(long) on (summarize count())",
        "T | mv-apply x=d to typeof(string) on (summarize count())",
    ),
    (
        "mv-apply-limit",
        "T | mv-apply x=d limit 3 on (summarize count())",
        "T | mv-apply x=d on (summarize count())",
    ),
    (
        "mv-apply-limit-values",
        "T | mv-apply x=d limit 3 on (summarize count())",
        "T | mv-apply x=d limit 5 on (summarize count())",
    ),
    # The modifier precedes the column name; the postfix spelling is a parse
    # error (see MvApplyOp), so the bare-vs-modifier pair is the only shape.
    (
        "mv-apply-with-itemindex",
        "T | mv-apply with_itemindex=i x=d on (summarize count())",
        "T | mv-apply x=d on (summarize count())",
    ),
    (
        "mv-apply-with-itemindex-values",
        "T | mv-apply with_itemindex=i x=d on (summarize count())",
        "T | mv-apply with_itemindex=j x=d on (summarize count())",
    ),
    (
        "parse-kv-with-properties",
        "T | parse-kv a as (b:string) with (pair_delimiter=',')",
        "T | parse-kv a as (b:string)",
    ),
    (
        "parse-kv-with-properties-values",
        "T | parse-kv a as (b:string) with (pair_delimiter=',')",
        "T | parse-kv a as (b:string) with (pair_delimiter=';')",
    ),
    ("getschema-kind", "T | getschema kind=csl", "T | getschema"),
    ("consume-decodeblocks", "T | consume decodeblocks=true", "T | consume"),
    ("parse-kind", "T | parse x with 'a' y", "T | parse kind=regex x with 'a' y"),
    ("parse-flags", "T | parse kind=regex flags='i' x with 'a' y", "T | parse kind=regex x with 'a' y"),
    ("parse-where-kind", "T | parse-where x with 'a' y", "T | parse-where kind=regex x with 'a' y"),
    # `parse-where` duplicates `ParseOp`'s fields on its own class and reads
    # them at its own call site, so each half of the duplication needs its
    # own pair. The `kind` half was pinned a commit before this one and the
    # `flags` half one line below it in the builder was not.
    (
        "parse-where-flags",
        "T | parse-where kind=regex flags='i' x with 'a' y",
        "T | parse-where kind=regex x with 'a' y",
    ),
    ("union-kind", "union kind=inner A, B", "union kind=outer A, B"),
    ("union-withsource", "union withsource=S A, B", "union A, B"),
    ("union-isfuzzy", "union isfuzzy=true A, B", "union A, B"),
    ("search-scope-tables", "search in (A) 'x'", "search in (B) 'x'"),
    ("search-kind", "search kind=case_sensitive 'x'", "search 'x'"),
    (
        "make-series-default",
        (
            "T | make-series C=count() default=0 on d "
            "from datetime(2024-01-01) to datetime(2024-01-02) step 1h"
        ),
        (
            "T | make-series C=count() default=1 on d "
            "from datetime(2024-01-01) to datetime(2024-01-02) step 1h"
        ),
    ),
    (
        "make-series-in-range-window",
        "T | make-series C=count() on d in range(datetime(2024-01-01), datetime(2024-01-02), 1h)",
        "T | make-series C=count() on d in range(datetime(2024-01-01), datetime(2024-01-03), 1h)",
    ),
    ("render-with-properties", "T | render timechart with (title='a')", "T | render timechart with (title='b')"),
    # Guards `join`/`lookup`'s effective default: a bare join must record
    # "innerunique", not collapse onto "inner", a different operator. See
    # the MUST_EQUAL half for the effective default each records.
    ("join-default-vs-inner", "T | join U on a", "T | join kind=inner U on a"),
    ("lookup-default-vs-inner", "T | lookup U on a", "T | lookup kind=inner U on a"),
    ("find-scope-tables", "find in (A) where x == 1", "find in (B) where x == 1"),
    ("find-withsource", "find withsource=S in (A) where x == 1", "find in (A) where x == 1"),
    # `project` is the third field the FindOp bullet announces and was the
    # one with no pair; it selects which columns come back.
    (
        "find-project",
        "find in (A) where x == 1 project a",
        "find in (A) where x == 1 project b",
    ),
    # Presence/absence of the project clause (the sibling row covers value-vs-value).
    ("find-project-presence", "find in (T) where a == 1 project a", "find in (T) where a == 1"),
    # TypedNameDecl: the declaration's Type child was never read, so a typed
    # capture and an untyped one were one node.
    ("typed-capture-vs-untyped", "T | parse x with 'a' b:long", "T | parse x with 'a' b"),
    ("typed-capture-declared-type", "T | parse x with 'a' b:long", "T | parse x with 'a' b:string"),
    # 4.5 -- multi-statement queries. The builder read the first tabular
    # statement and discarded the rest, so `T | count; U | count` built and
    # hashed exactly as `T | count`.
    ("second-statement-table", "T | count; U | count", "T | count; V | count"),
    ("second-statement-dropped", "T | count; U | count", "T | count"),
    # `second-statement-table` only proves the later pipeline's *source name*
    # reaches the digest; this pins that a change inside its operators does
    # too, so `additional_pipelines` is not hashed by source name alone.
    ("second-statement-operator-param", "T | count; U | take 1", "T | count; U | take 2"),
    # 4.6 -- LetValueRef. A let-bound scalar lowered to a ColumnRef, so a
    # query-local constant and a real column of that name were one node.
    #
    # Both sides declare a binding, and only the *resolution* of `n` in the
    # predicate varies: on the left `n` is the name just bound, on the right
    # the binding is called `z` so `n` can only be a column of `T`. Holding
    # the binding count fixed is the whole point. The pair this replaces was
    # `let n = 5; T | where a > n` against a bare `T | where a > n`, which
    # differs for a reason that has nothing to do with the field it claimed
    # to guard -- `let n = 5; T | where a > 1` and `T | where a > 1` already
    # hash apart on the presence of the binding alone, so it stayed green
    # under a full revert of the use site to `ColumnRef`.
    (
        "let-scalar-vs-column",
        "let n = 5; T | where a > n",
        "let z = 5; T | where a > n",
    ),
    # 4.7 -- typed nested pipelines. `pipeline` was declared `Any`, so the
    # subtree survived in memory but reloaded as a plain dict, and the
    # span-stripping walk never reached inside it -- a stored IR did not
    # reproduce its own hash. These pairs pin that the nested query is in
    # the digest at all; the MUST_EQUAL half pins that its spans are not.
    ("toscalar-nested-table", "T | where a > toscalar(U | count)", "T | where a > toscalar(V | count)"),
    ("subquery-nested-table", "T | where x in ((U | project x))", "T | where x in ((V | project x))"),
    # 4.8 -- small fidelity gaps.
    ("compound-string-literal", "T | where x == 'a' 'b'", "T | where x == 'a'"),
    ("project-reorder-star", "T | project-reorder *, a", "T | project-reorder b, a"),
    # `isnull`/`isempty`/`isnotnull`/`isnotempty` pairwise-distinctness lives
    # in `test_ir_builder.py::test_all_four_null_tests_hash_distinctly`,
    # which is pairwise-complete over all four and therefore a strict
    # superset of a `isnull-vs-isempty`/`isnull-vs-isnotnull` pair here.
    # Guards `datatable(...)` in expression position building a real
    # `DataTableExpr` rather than falling through to `UnknownExpr` and
    # hashing raw source text -- which would happen to discriminate on value
    # too, passing this pair for the wrong reason. See
    # test_no_battery_pair_discriminates_on_an_unmodelled_blob.
    (
        "expr-datatable-values",
        'T | where a in ((datatable(x:string)["v"]))',
        'T | where a in ((datatable(x:string)["w"]))',
    ),
    # Guards `evaluate`'s output-schema clause: `declared_schema` and
    # `declared_schema_star` carry the .NET `EvaluateSchemaClause`
    # (`EvaluateOperator.Schema`), so a written schema, a different written
    # schema, and no clause at all are three distinct digests.
    (
        "evaluate-schema-clause-columns",
        "T | evaluate bag_unpack(d) : (x:string)",
        "T | evaluate bag_unpack(d) : (y:long, z:datetime)",
    ),
    (
        "evaluate-schema-clause-vs-absent",
        "T | evaluate bag_unpack(d) : (x:string)",
        "T | evaluate bag_unpack(d)",
    ),
    # `: (*, x:string)` appends `x` to the plug-in's own columns instead of
    # replacing them -- `declared_schema_star` is the only thing that
    # distinguishes it from the non-star spelling with the same columns.
    (
        "evaluate-schema-star",
        "T | evaluate bag_unpack(d) : (*, x:string)",
        "T | evaluate bag_unpack(d) : (x:string)",
    ),
    # The five statement kinds. Guards the worst collision shape: nothing
    # else would record that the statement was there, so two *different*
    # values of one statement would hash alike, not merely a query with one
    # against a query without. `QueryIR.statements` carries them.
    (
        "set-two-values",
        "set query_now=datetime(2020-01-01); T | take 1",
        "set query_now=datetime(2021-01-01); T | take 1",
    ),
    ("set-vs-absent", "set query_now=datetime(2020-01-01); T | take 1", "T | take 1"),
    (
        "query-parameters-two-defaults",
        "declare query_parameters(n:long = 5); T | take 1",
        "declare query_parameters(n:long = 9); T | take 1",
    ),
    # The discriminating pair for "a `declare query_parameters` name is never
    # alpha-canonicalized". Both sides also declare a `let` of that same name,
    # and that one *does* rename -- to `$let0`, identically on both sides --
    # so the parameter name is the only thing left that can split them. It
    # does, and it would stop doing so the moment `TypedNameDecl` joined
    # `_LET_NAME_MODELS`: the two queries accept different requests, so the
    # merge would be a real collision rather than a spelling fold. A pair
    # without the `let` could not see that, since nothing would rename in it
    # at all.
    (
        "query-parameters-shadowed-by-let",
        "let p = 5; declare query_parameters(p:long = 1); T | take 1",
        "let q = 5; declare query_parameters(q:long = 1); T | take 1",
    ),
    (
        "alias-two-databases",
        "alias database D = cluster('c').database('d'); T | take 1",
        "alias database D = cluster('c').database('e'); T | take 1",
    ),
    (
        "pattern-two-bodies",
        'declare pattern P = (a:string) { ("x") = { T | take 1 }; }; T | take 1',
        'declare pattern P = (a:string) { ("x") = { U | take 9 }; }; T | take 1',
    ),
    # A forward declaration names the pattern and nothing else; the full
    # declaration also says what each arm does. `declared_only` is what keeps
    # them apart from an empty-bodied pattern.
    (
        "pattern-forward-vs-full",
        "declare pattern P; T | take 1",
        'declare pattern P = (a:string) { ("x") = { T | take 1 }; }; T | take 1',
    ),
    (
        "restrict-two-targets",
        'restrict access to (database("d")); T | take 1',
        'restrict access to (database("e")); T | take 1',
    ),
    # Guards a `let` inside a pattern body staying scoped to
    # `PatternMatch.body_lets` rather than reaching the digest through
    # top-level `let_bindings`: the route must not change the answer. Same
    # shape as `let-function-body-nested-let-value` above.
    (
        "pattern-nested-let-scoped",
        'declare pattern P = (a:string) { ("x") = { let z = 5; T | take z }; }; T | take 1',
        'declare pattern P = (a:string) { ("x") = { let z = 9; T | take z }; }; T | take 1',
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
    # This is fix #1's in-process, host-independent guard: pre-fix, the two
    # sides' LiteralExpr.value strings diverge in the offset-marker channel
    # on every host (Unspecified renders with no marker, Local renders one
    # even at offset zero), not merely in ticks, which only diverges on a
    # non-UTC-offset host. See the module docstring for the simulation that
    # confirms this under both TZ=UTC and TZ=Asia/Tokyo.
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
    # -----------------------------------------------------------------------
    # WS4 -- the other half of the workstream. Recording a modifier the query
    # did not write is its own way to be wrong: it splits one query's two
    # spellings into two digests. Each pair below is a decision the model
    # makes about an unwritten or duplicate spelling, pinned so the decision
    # cannot drift into a split.
    # -----------------------------------------------------------------------
    # D8 -- a required field carrying KQL's *effective* default, so the bare
    # spelling and the explicit one are one query and one digest. The value
    # is the one KQL actually applies, which for join/lookup is not the
    # "inner" a naive read of the bare spelling would assume.
    ("sort-bare-is-desc", "T | sort by a", "T | sort by a desc"),
    ("top-bare-is-desc", "T | top 5 by a", "T | top 5 by a desc"),
    # The same default reached through the *other* branch of the same
    # expression: `sort by a nulls first` has an Ordering node with no
    # direction keyword, where a bare `sort by a` has no Ordering node at
    # all. One `or "desc"` serves both, so the two pairs above -- which only
    # exercise the missing-node path -- would both stay green if the
    # keyword-absent path stopped defaulting.
    ("sort-nulls-only-is-desc", "T | sort by a nulls first", "T | sort by a desc nulls first"),
    # `order by` is a bare alias of `sort by` -- not merely a query that
    # defaults the same way, but the identical operator under a different
    # keyword -- so the two spellings must be one digest, direction bare or
    # written.
    ("order-by-is-sort-by", "T | order by a", "T | sort by a"),
    ("order-by-desc-is-sort-by-desc", "T | order by a asc", "T | sort by a asc"),
    ("mv-expand-bare-is-bag", "T | mv-expand a", "T | mv-expand kind=bag a"),
    ("parse-bare-is-simple", "T | parse x with 'a' y", "T | parse kind=simple x with 'a' y"),
    # `parse-where` is a separate operator class carrying its own copy of the
    # field, so the default is a second decision rather than the same one
    # observed twice -- it is only shared by the docstring saying so.
    (
        "parse-where-bare-is-simple",
        "T | parse-where x with 'a' y",
        "T | parse-where kind=simple x with 'a' y",
    ),
    ("union-bare-is-outer", "union A, B", "union kind=outer A, B"),
    ("search-bare-is-default", "search 'x'", "search kind=default 'x'"),
    ("join-bare-is-innerunique", "T | join U on a", "T | join kind=innerunique U on a"),
    ("lookup-bare-is-leftouter", "T | lookup U on a", "T | lookup kind=leftouter U on a"),
    # Two spellings of one modifier fold onto one field: modeling
    # `bagexpansion=` separately from `kind=` would split these, which is
    # why there is no separate `bag_expansion` field alongside `kind`.
    ("mv-expand-bagexpansion-is-kind", "T | mv-expand bagexpansion=array a", "T | mv-expand kind=array a"),
    # Same shape on `render`: the legacy bare parameter and the `with (...)`
    # clause land in the same properties dict.
    ("render-with-clause-vs-bare-param", "T | render columnchart kind=stacked", "T | render columnchart with (kind=stacked)"),
    # `hints` is the one field in this release that is source-derived and
    # still excluded from the digest: a hint asks the engine to execute the
    # query differently, not to return different rows.
    ("join-hint-excluded", "T | join hint.strategy=shuffle U on a", "T | join U on a"),
    # Guards `FindOp.tables`, read with the no-argument `ToString()`
    # overload: `IncludeTrivia.All` means a comment written before a table
    # name is part of the name in the raw text, and reaches the digest
    # through it.
    ("find-table-comment", "find in (// note\nT) where x == 1", "find in (T) where x == 1"),
    # A let-bound name is a local label: the hash renames every binding to
    # its declaration index. That rename could not reach a use site lowered
    # to a ColumnRef, so the declaration was canonicalized while the use
    # site kept the name the query wrote. LetValueRef closes the split.
    ("let-scalar-name-rename", "let n = 5; T | where a > n", "let m = 5; T | where a > m"),
    # Typing `pipeline` is what lets the span-stripping walk reach inside a
    # nested query, so formatting within one stops reaching the digest.
    ("toscalar-nested-whitespace", "T | where a > toscalar(U | count)", "T | where a >  toscalar(U   | count)"),
    # Adjacent string literals are one literal in KQL, and the parser has
    # already concatenated them by the time LiteralValue is read.
    ("compound-string-is-concatenation", "T | where x == 'a' 'b'", "T | where x == 'ab'"),
    # Same shape as `toscalar-nested-whitespace` above, for the sibling
    # expression-position construct: DataTableExpr's cells are visited
    # expressions, not raw text, so formatting within the row does not
    # reach the digest.
    (
        "expr-datatable-whitespace",
        'T | where a in ((datatable(x:string)["v"]))',
        'T | where a in ((datatable(x:string) [ "v" ]))',
    ),
    # `read_row_schema` reads the clause's columns as expressions, not raw
    # text, so formatting within `evaluate`'s schema clause does not reach
    # the digest -- the closure-side sibling of the pairs above.
    (
        "evaluate-schema-whitespace",
        "T | evaluate bag_unpack(d) : (x:string)",
        "T | evaluate bag_unpack(d) :  ( x : string )",
    ),
    # `named_param_name`/`named_param_value` already strip trivia (used by
    # every other named-parameter reader in this release), so spacing inside
    # `parse-kv`'s `with (...)` clause does not reach the digest either.
    (
        "parse-kv-with-properties-whitespace",
        "T | parse-kv a as (b:string) with (pair_delimiter=',')",
        "T | parse-kv a as (b:string) with ( pair_delimiter = ',' )",
    ),
    # A `let` function's body is built out of the same nodes as any other
    # pipeline, so every canonicalization rule that holds for a top-level
    # query holds inside the braces too -- with no special case for the body
    # anywhere in `transforms.py`. Formatting and comments first, then the
    # signature's own formatting.
    (
        "let-function-body-whitespace",
        "let S = (w:int) { A | where x > w }; S(5)",
        "let S = (w:int) {\nA\n|   where x>w\n}; S(5)",
    ),
    (
        "let-function-body-comment",
        "let S = (w:int) { A | where x > w }; S(5)",
        "let S = (w:int) { A | where x > w // note\n}; S(5)",
    ),
    (
        "let-function-param-whitespace",
        "let S = (w:int) { A | take 1 }; S(5)",
        "let S = ( w : int ) { A | take 1 }; S(5)",
    ),
    # A parameter name is a local label too. It is renamed to `$param<i>` by
    # declaration position -- the declaration and every body reference the
    # builder lowered under the parameter's shadow -- so two alpha-equivalent
    # signatures are one query. This row was a MUST_DIFFER until 0.2.0, when
    # the only place a parameter name reached the digest was the declaration
    # and nothing folded `w` onto `z`.
    (
        "let-function-parameter-name",
        "let S = (w:int) { A | where x > 1 }; S(5)",
        "let S = (z:int) { A | where x > 1 }; S(5)",
    ),
    # The same rename with the parameter actually read in the body, which is
    # the half the row above cannot see: renaming the declaration alone and
    # leaving the reference written as the query spelled it splits these two.
    (
        "let-function-param-used-in-body",
        "let S = (w:int) { A | where x > w }; S(5)",
        "let S = (z:int) { A | where x > z }; S(5)",
    ),
    # A tabular parameter is the same rename reaching a `TableRef` instead of
    # a `ColumnRef`, and it is the row that needs the derived-index strip:
    # `LetBinding.inner_tables` records the body's table names as plain
    # *strings*, which no node rename can reach, so `["T"]` and `["U"]` kept
    # these apart until the index stopped reaching the digest at all.
    (
        "let-function-tabular-param-rename",
        "let S = (T:(*)) { T | count }; S(A)",
        "let S = (U:(*)) { U | count }; S(A)",
    ),
    # The shadow edge, where the two renames meet: the body declares a `let`
    # of the parameter's own name. The declaration renames as a `let`
    # (`$let<i>`, through the body scope) and the reference renames as a
    # parameter (`$param<i>`, through the scope-local map), because the
    # builder committed that reference to the shadow reading and lowered it
    # as a `ColumnRef`. Two disjoint node classes, so neither rename can
    # reach the other's node and their order does not matter.
    (
        "let-function-shadow-edge",
        "let S = (w:int) { let w = 5; A | where x > w }; S(1)",
        "let S = (z:int) { let z = 5; A | where x > z }; S(1)",
    ),
    # A call site names the binding it calls, so it is as much a local label
    # as the declaration is -- in source position...
    (
        "let-function-call-site-rename",
        "let f = () { T | count }; f()",
        "let g = () { T | count }; g()",
    ),
    # ...in expression position, which is a different IR class (`FuncCall`,
    # not `FuncCallSource`) reached by a different builder path...
    (
        "let-function-call-expr-rename",
        "let f = () { 1 }; T | extend y = f()",
        "let g = () { 1 }; T | extend y = g()",
    ),
    # ...and through `invoke`, a third call site that builds the
    # expression-position class. One map-gated branch covers all three.
    (
        "let-function-invoke-rename",
        "let f = (X:(*)) { X | count }; U | invoke f()",
        "let g = (X:(*)) { X | count }; U | invoke g()",
    ),
    # The composition claim, stated as a pair rather than asserted in prose:
    # `_sort_commutative` walks `model_fields`, so it reaches a function body
    # the same way it reaches `main_pipeline`.
    (
        "let-function-body-commutative",
        "let S = () { A | where a == 1 and b == 2 }; S()",
        "let S = () { A | where b == 2 and a == 1 }; S()",
    ),
    # The `let` rename is scope-ordered, and both directions of that matter.
    # An outer binding renames *through* the body, which reads it...
    (
        "let-function-outer-let-rename-through-body",
        "let n = 5; let S = () { A | where x > n }; S()",
        "let m = 5; let S = () { A | where x > m }; S()",
    ),
    # ...and a binding declared *inside* the body is a local label there, so
    # renaming it is no more a change than renaming a top-level one.
    (
        "let-function-inner-let-rename",
        "let S = () { let z = 5; A | take z }; S()",
        "let S = () { let y = 5; A | take y }; S()",
    ),
    # A `let` bound inside a function body and read through a time function:
    # `d`/`e` is a duration the body declares and `ago(d)` reads, so the row
    # pins that the body's own scope reaches a reference nested inside a call
    # argument.
    #
    # `LetBinding.inner_time_exprs` aliases the same `FuncCall` objects that
    # live inside `rhs_function` rather than copies of them, so a rename walk
    # would reach each one twice if the index were not cleared first.
    # `transforms._DERIVED_INDEX_FIELDS` clears it before the rename runs, so
    # there is no second path to the node and no field order to trip over.
    (
        "let-function-body-time-alias-rename",
        "let S = () { let d = 1h; A | where t > ago(d) | take 1 }; S()",
        "let S = () { let e = 1h; A | where t > ago(e) | take 1 }; S()",
    ),
    # The statement kinds. Whitespace is the shape that proves a statement is
    # *structure* in the digest rather than recorded text: a node that kept
    # its own source would split every one of these.
    ("set-whitespace", "set querytrace; T | take 1", "set   querytrace;\nT | take 1"),
    (
        "query-parameters-whitespace",
        "declare query_parameters(n:long = 5); T | take 1",
        "declare query_parameters(\n  n : long = 5\n); T | take 1",
    ),
    (
        "alias-whitespace",
        "alias database D = cluster('c').database('d'); T | take 1",
        "alias database D =\n    cluster('c').database('d');\nT | take 1",
    ),
    (
        "pattern-whitespace",
        'declare pattern P = (a:string) { ("x") = { T | take 1 }; }; T | take 1',
        'declare pattern P = (a:string)\n{\n  ("x") = {\n    T | take 1\n  };\n};\nT | take 1',
    ),
    (
        "restrict-whitespace",
        'restrict access to (database("d")) with (a=1); T | take 1',
        'restrict access to (\n  database("d")\n) with ( a = 1 );\nT | take 1',
    ),
    # A `let` inside a pattern body is a local label there, exactly as one
    # inside a function body is, so renaming it is no more a change than
    # renaming a top-level one. `_canonicalize_let_names` opens a child scope
    # for a `PatternMatch` the same way it does for a `LetFunction`; without
    # that branch these two would split.
    (
        "pattern-body-let-rename",
        'declare pattern P = (a:string) { ("x") = { let z = 5; T | take z }; }; T | take 1',
        'declare pattern P = (a:string) { ("x") = { let w = 5; T | take w }; }; T | take 1',
    ),
    # An outer binding renames *through* a pattern body that reads it, the
    # other direction of the same scope rule.
    (
        "pattern-body-outer-let-rename",
        'let n = 5; declare pattern P = (a:string) { ("x") = { T | take n }; }; T | take 1',
        'let m = 5; declare pattern P = (a:string) { ("x") = { T | take m }; }; T | take 1',
    ),
    # ...and through a statement that is not a pattern. `restrict access to
    # (V)` over a `let`-bound view reads the binding, so the rename has to
    # reach `QueryIR.statements` or these two split on a local label. The
    # `restrict-two-targets` row above is the other half: what the statement
    # points *at* still has to matter.
    (
        "restrict-over-a-renamed-let",
        "let V = T | take 1; restrict access to (V); T | count",
        "let W = T | take 1; restrict access to (W); T | count",
    ),
]


@pytest.mark.parametrize("case_id,query_a,query_b", MUST_EQUAL, ids=[c[0] for c in MUST_EQUAL])
def test_must_equal(case_id, query_a, query_b):
    hash_a, hash_b = _hash(query_a), _hash(query_b)
    assert hash_a == hash_b, (
        f"{case_id}: {query_a!r} and {query_b!r} mean the same thing but "
        f"hashed apart ({hash_a!r} vs {hash_b!r}) -- a rule dedup on "
        f"semantic_hash would keep both as if they didn't"
    )


# ---------------------------------------------------------------------------
# KNOWN_COLLISIONS: (case_id, query_a, query_b) -- pairs that mean *different*
# things and hash alike anyway. These are the failure MUST_DIFFER exists to
# catch; they are filed here rather than there because a gap is an open
# boundary in the model, not a regression, and a permanently red MUST_DIFFER
# entry would be deleted by the first person to run the suite.
#
# They are not in MUST_EQUAL either, and that distinction is the point of the
# third list. MUST_EQUAL means "same meaning, different spelling" -- putting a
# real collision there would make this module's own docstring false, which is
# exactly the drift between what we say and what the code does that a
# disclosure exists to prevent.
#
# **The list is empty.** Every pair it held has been closed and moved to
# MUST_DIFFER: `evaluate`'s output-schema clause, then the three
# `let`-function rows (body, parameter type, parameter default). The list, the
# parametrization and the test stay, because the shape of the disclosure is
# what has value and it will be needed again -- and because pytest's own
# "skipped: got empty parameter set" line is the standing signal that there is
# nothing currently filed here. An emptied list quietly deleted would leave
# the next gap with nowhere to go and no convention to follow.
#
# What qualifies a pair for this list: the two queries mean different things,
# they hash alike *today*, and the reason is a construct the IR does not model
# rather than a canonicalization rule that is too aggressive. (A rule that
# over-merges is a bug -- it goes to MUST_DIFFER red and gets fixed.) A pair
# filed here must also be disclosed everywhere the failure message below
# names, and mirrored as a row in `examples/semantic_hash_demo.py`'s
# KNOWN_MERGES, which the suite runs -- that file prints the verdict for a
# reader, this one fails the build.
#
# The assertion is the same equality MUST_EQUAL uses, so a gap that later
# *closes* turns this list red. That is intended: the digest's documented
# survivor list is the safety mechanism a consumer works around, so a survivor
# silently ceasing to be one is a documentation defect even though the
# behavior improved.
# ---------------------------------------------------------------------------

KNOWN_COLLISIONS: list[tuple[str, str, str]] = []


@pytest.mark.parametrize(
    "case_id,query_a,query_b", KNOWN_COLLISIONS, ids=[c[0] for c in KNOWN_COLLISIONS]
)
def test_known_collision(case_id, query_a, query_b):
    hash_a, hash_b = _hash(query_a), _hash(query_b)
    assert hash_a == hash_b, (
        f"{case_id}: {query_a!r} and {query_b!r} are a documented collision "
        f"and no longer collide ({hash_a!r} vs {hash_b!r}). If the gap was "
        f"closed on purpose, move this pair to MUST_DIFFER and update "
        f"everything that still discloses it -- not a fixed list, so find "
        f"them: compute_semantic_hash's docstring, the docstring of the IR "
        f"node that drops the construct, README (its `semantic_hash` section, "
        f"plus any boundary section that describes the construct), the "
        f"CHANGELOG entry that filed it, and the matching KNOWN_MERGES row "
        f"in examples/semantic_hash_demo.py, which is failing beside this."
    )


def test_no_battery_pair_discriminates_on_an_unmodelled_blob():
    """Every query in this file must build IR that carries no source text.

    A ``MUST_DIFFER`` pair proves nothing if the builder did not model
    either side. ``raw_text`` is in the digest payload for every node below
    the root -- only ``QueryIR``'s own copy is excluded -- so two queries
    differing in *any* text hash apart the moment one of them reaches a node
    that records its own source. A discriminator written against such a
    shape passes for a reason that has nothing to do with the field it
    claims to guard, and would keep passing if that field were deleted
    tomorrow.

    This is a real risk, not a hypothetical one: an operator that lowers to
    ``UnknownExpr`` carries ``raw_text``, so its direction pair would pass
    green through the exact regression it is meant to catch. Asserting the
    whole battery is text-free is cheaper than reasoning about it pair by
    pair, and it holds for the pre-WS4 cases too.

    The check is **not** limited to the ``Unknown*`` classes, and the
    difference is the point. Eight modeled operators also record their own
    source -- ``ScanOp``, ``TopNestedOp``, ``MacroExpandOp``, ``MakeGraphOp``
    and the four ``graph-*`` operators -- because they are dispatched but
    only partly modeled. Nothing in the battery reaches one today, so naming
    them in prose would protect nobody: the first pair written against
    ``scan`` or ``graph-match`` would discriminate on ``raw_text`` and pass.
    Deriving the set from ``model_fields`` instead means a node added to the
    partly-modeled list is covered from the moment it is defined, the same
    rule the rest of this suite follows.
    """
    offenders: dict[str, list[str]] = {}
    for query in sorted({q for _, a, b in MUST_DIFFER + MUST_EQUAL + KNOWN_COLLISIONS for q in (a, b)}):
        ir = parse(query).to_ir()
        carriers = sorted(
            f"{type(n).__name__}({n.raw_text!r})" for n in walk(ir)
            if n is not ir and "raw_text" in type(n).model_fields and n.raw_text
        )
        if carriers:
            offenders[query] = carriers
    assert not offenders, (
        "these queries did not lower cleanly -- their nodes carry source "
        f"text into the digest: {offenders}"
    )
