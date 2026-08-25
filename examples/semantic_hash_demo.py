# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""What ``semantic_hash`` merges, what it splits, and where it lies.

``compute_semantic_hash(ir)`` is kustology's answer to "are these two
queries the same query?" — the thing you deduplicate a detection-rule
library on. Every line below is *computed*, not asserted: each pair is
hashed and the verdict printed, so the table cannot go stale.

Read it as three groups.

**Merges you want.** Formatting, comment placement, commutative operand
order, ``let`` names, a ``hint.*`` that changes execution and not results,
and everything the binder supplied. Passing a schema does not move the
digest.

**Splits you want.** Different literals, a different operator sequence,
and — new in 0.2.0 — the operators that used to collapse. ``in~`` /
``has_any`` / ``has_all`` all built one ``SetMembership`` with no field
recording which, so ``has_any`` and ``has_all``, which are opposites,
shared a digest. ``SetMembership.op`` is what separates them now, and
``Exists.op`` does the same for ``isnotnull`` / ``isnotempty``. The five
statement kinds — ``set``, ``declare query_parameters``,
``declare pattern``, ``alias database``, ``restrict access`` — join them:
they were absent from the IR entirely, so two different values of one
statement collided with each other, and they are on
``QueryIR.statements`` now.

**Merges you may not want.** Every case is a row in ``KNOWN_MERGES`` below
and every row is hashed when this file runs, so the list *is* the claim.
There is no tally in this sentence on purpose — a number quoted about the
list is one more thing that can drift away from it, and in this file it
twice did.

A consumer deduplicating on the digest acts on that third group, so the
rows are load-bearing rather than illustrative: if any of them stops
matching the group it is filed under, ``main()`` raises instead of printing
a table that is quietly wrong.

The digest carries its scheme as a prefix (``kustology-sem-v2:``) so a
stored hash from an older canonicalization cannot silently compare unequal
against a fresh one. Rehash from source rather than across schemes.

Requires the ``[ir]`` extras: ``pip install 'kustology[ir]'``.
"""

from kustology import parse
from kustology.ir import (
    SEMANTIC_HASH_SCHEME,
    LiteralExpr,
    SetMembership,
    compute_semantic_hash,
    find_all,
)

SCHEMA = {"T": {"a": "long", "d": "timespan", "t": "datetime"}}


def digest(query: str, schema: dict | None = None) -> str:
    return compute_semantic_hash(parse(query, schema=schema).to_ir())


# (label, left, right, expected) — `expected` is what the docs claim, and
# the runner prints a loud marker if the measurement disagrees.
MERGES = [
    ("formatting and comments",
     "T | where a==1 and d>1h",
     "T\n// a note\n| where a == 1\n    and d > 1h"),
    ("commutative `and` operands",
     'T | where a == 1 and b == 2', 'T | where b == 2 and a == 1'),
    ("`in (...)` value order",
     'T | where a in ("x", "y")', 'T | where a in ("y", "x")'),
    ("consecutive `where`s merge into one `and`",
     "T | where a == 1 | where b == 2", "T | where a == 1 and b == 2"),
    ("`let` names are positional labels",
     "let n = 5; T | where a > n", "let m = 5; T | where a > m"),
    ("tolower(x) == y folds to x =~ y",
     'T | where tolower(a) == "x"', 'T | where a =~ "x"'),
    ("a `hint.*` changes execution, not rows",
     "T | summarize hint.strategy=shuffle count() by a",
     "T | summarize count() by a"),
    ("equal durations, different spellings",
     "T | where d > 1h", "T | where d > 60m"),
    ("a datetime literal is normalized to UTC",
     "T | where t > datetime(2024-01-01)",
     "T | where t > datetime(2024-01-01T00:00:00Z)"),
]

SPLITS = [
    ("different literal", "T | where a > 1", "T | where a > 2"),
    ("different operator order",
     "T | where a == 1 | take 5", "T | take 5 | where a == 1"),
    ("has_any vs has_all (opposites)",
     'T | where a has_any ("x","y")', 'T | where a has_all ("x","y")'),
    ("in vs in~ (case folding)",
     'T | where a in ("x")', 'T | where a in~ ("x")'),
    ("isnotnull vs isnotempty",
     "T | where isnotnull(a)", "T | where isnotempty(a)"),
    # `evaluate`'s output-schema clause used to be the costliest known gap:
    # it declares the columns the plug-in returns, so what collided was the
    # operator's result *shape*. `EvaluateOp.declared_schema` now carries it.
    ("evaluate's declared output schema splits",
     "T | evaluate bag_unpack(d) : (x:string)",
     "T | evaluate bag_unpack(d) : (y:long, z:datetime)"),
    ("declared schema vs none splits",
     "T | evaluate bag_unpack(d) : (x:string)",
     "T | evaluate bag_unpack(d)"),
    # A `let` function's body was the largest known gap in 0.2.0: what
    # collided was an arbitrary amount of query rather than one modifier.
    # `LetFunction` now carries the body, the parameters' declared types and
    # defaults, and the `view` keyword.
    ("`let` function bodies split",
     "let S = (w:int) { A | where EventID == 4625 | summarize c=count() by Account | where c > w }; S(5)",
     "let S = (w:int) { A | where EventID == 4624 | summarize c=count() by Computer | where c > w }; S(5)"),
    ("a parameter's declared type splits",
     "let S = (w:int) { A | where x > w }; S(5)",
     "let S = (w:long) { A | where x > w }; S(5)"),
    ("a parameter's default splits",
     "let S = (w:int) { A | where x > w }; S(5)",
     "let S = (w:int=3) { A | where x > w }; S(5)"),
    # The remaining operator-modifier gaps, closed in 0.2.0: mv-apply's
    # `to typeof(...)`, `limit` and `with_itemindex=`, parse-kv's `with (...)`
    # properties, getschema's `kind=`, consume's `decodeblocks=`.
    ("mv-apply's `to typeof(...)` splits",
     "T | mv-apply d to typeof(long) on (take 1)",
     "T | mv-apply d on (take 1)"),
    ("mv-apply's `limit` splits",
     "T | mv-apply d limit 5 on (take 1)",
     "T | mv-apply d on (take 1)"),
    # The modifier precedes the column name; `mv-apply d with_itemindex=i`
    # is a parse error whose error recovery moves the hash for the wrong
    # reason, which reads as "no collision here" if you do not check
    # diagnostics.
    ("mv-apply's `with_itemindex=` splits",
     "T | mv-apply with_itemindex=i d on (take 1)",
     "T | mv-apply d on (take 1)"),
    ("parse-kv's `with (...)` properties split",
     "T | parse-kv a as (b:string) with (pair_delimiter=',')",
     "T | parse-kv a as (b:string)"),
    ("getschema's `kind=` splits",
     "T | getschema kind=csl", "T | getschema"),
    ("consume's `decodeblocks=` splits",
     "T | consume decodeblocks=true", "T | consume"),
    # The five statement kinds that are neither `let` nor tabular. Until
    # 0.2.0 the builder read none of them, and every row below sat in
    # KNOWN_MERGES: not "a query with a statement collides with one without"
    # (easy to shrug off) but two *different values of the same statement*
    # colliding with each other, which is the shape that costs a dedup
    # consumer a rule. `QueryIR.statements` carries them now, in source
    # order, and the order is part of the digest too.
    ("`set` splits: pinned query_now vs none",
     "set query_now=datetime(2020-01-01); T | take 1", "T | take 1"),
    ("`set` splits: two different query_now values",
     "set query_now=datetime(2020-01-01); T | take 1",
     "set query_now=datetime(2021-01-01); T | take 1"),
    ("`declare query_parameters` splits: two different defaults",
     "declare query_parameters(n:long = 5); T | take 1",
     "declare query_parameters(n:long = 9); T | take 1"),
    # A parameter name is the caller-facing API of a saved query, so unlike a
    # `let` name it is never alpha-canonicalized.
    ("`declare query_parameters` splits: two different parameter names",
     "declare query_parameters(n:long); T | take 1",
     "declare query_parameters(m:long); T | take 1"),
    ("`alias database` splits: two different databases",
     "alias database D = cluster('c').database('d'); T | take 1",
     "alias database D = cluster('c').database('e'); T | take 1"),
    ("`declare pattern` splits: two different bodies",
     'declare pattern P = (a:string) { ("x") = { T | take 1 }; }; T | take 1',
     'declare pattern P = (a:string) { ("x") = { U | take 9 }; }; T | take 1'),
    ("`restrict access` splits: two different targets",
     'restrict access to (database("d")); T | take 1',
     'restrict access to (database("e")); T | take 1'),
]

# Every row here is a *merge* the library makes on purpose. The statement
# kinds used to be the rest of this list and are gone from it: they are in
# SPLITS above, one row per kind, since 0.2.0 models all five. Nothing was
# retired quietly — a gap that closes turns
# `tests/ir/test_hash_battery.py`'s KNOWN_COLLISIONS red on purpose, and
# that is the tripwire that brought this list here.
KNOWN_MERGES = [
    ("typed nulls: real(null) == datetime(null) — deliberate",
     "T | where a > real(null)", "T | where a > datetime(null)"),
    ('obfuscated strings: h"x" == "x" — deliberate',
     'T | where a == h"x"', 'T | where a == "x"'),
]


def errors(query: str) -> list[str]:
    """Error-severity diagnostic codes for ``query``, if any."""
    return [d["code"] for d in parse(query).diagnostics if d["severity"] == "Error"]


def report(title: str, pairs, expect_equal: bool) -> int:
    """Hash every pair, print the measured verdict, return how many ran.

    Raises rather than printing a wrong table. ``tests/test_examples.py``
    calls ``main()``, so every row here is a claim the suite enforces —
    which is the only thing that stops this file drifting away from the
    library the way its own prose twice did.
    """
    print(f"\n=== {title}")
    for label, left, right in pairs:
        # A pair that does not parse is the trap here, not a result. KQL's
        # error recovery happily builds *something* for invalid text, and
        # that something has its own digest — so a typo in a modifier's
        # spelling shows up as a clean-looking "split" and reads as "no
        # collision", which is the opposite of what this file is claiming.
        # Check the input before believing the verdict.
        bad = errors(left) + errors(right)
        if bad:
            raise AssertionError(f"{label}: demo query does not parse: {bad}")
        same = digest(left) == digest(right)
        if same != expect_equal:
            want = "merge" if expect_equal else "split"
            got = "merge" if same else "split"
            raise AssertionError(
                f"{label}: filed under {want}, measured {got} — this file's "
                f"claim has drifted from the library"
            )
        print(f"  {'merge ' if same else 'split '} {label}")
    return len(pairs)


def main() -> None:
    print(f"Scheme: {SEMANTIC_HASH_SCHEME}")
    print(f"Example digest: {digest('T | take 1')}")

    measured = report("Merges the contract promises", MERGES, expect_equal=True)
    measured += report("Splits the contract promises", SPLITS, expect_equal=False)
    measured += report("Merges to know about before deduplicating",
                       KNOWN_MERGES, expect_equal=True)
    # Derived, never written down: the only count in this file's output is
    # one it just computed.
    print(f"\n  {measured} pairs measured, every verdict computed at run time.")

    print("\n=== Bind state")
    # Every binder-written field is stripped, so a schema does not move the
    # digest — except for one shape difference no stripping can hide.
    plain = "T | where a > 1"
    aliased = "let A = T; A | take 1"
    print(f"  {'merge ' if digest(plain) == digest(plain, SCHEMA) else 'split '}"
          f" schema does not move the digest: {plain!r}")
    print(f"  {'merge ' if digest(aliased) == digest(aliased, SCHEMA) else 'split '}"
          f" a `let` aliasing a table does: {aliased!r}")
    print("         Unbound the RHS is an expression; bound, the binder has")
    print("         proved it is a table and it becomes a pipeline. Different")
    print("         nodes, not different field values.")

    print("\n=== The fields that carry the 0.2.0 splits")
    for query in ('T | where a in~ ("x")', 'T | where a has_any ("x")'):
        node = next(iter(find_all(parse(query).to_ir(), SetMembership)))
        print(f"  {query:<26} op={node.op:<8} "
              f"polarity={node.polarity} case_sensitive={node.case_sensitive}")
    print("         `op` is the source of truth; the other two are derived")
    print("         from it and kept because they are what a caller filters on.")

    print("\n=== Literals: what is hashed")
    for query in ("T | where d > 1500ms", "T | where t > datetime(2024-01-01Z)"):
        node = next(iter(find_all(parse(query).to_ir(), LiteralExpr)))
        print(f"  {query:<38} literal_kind={node.literal_kind:<9} "
              f"value={node.value!r} ticks={node.ticks}")
    print("         `ticks` is the lossless form — 100ns units. A timedelta")
    print("         rebuilt as ticks // 10 is exact only to a microsecond,")
    print("         so `2tick` does not survive the round trip and `ticks` is")
    print("         what to read when sub-microsecond precision matters.")


if __name__ == "__main__":
    main()
