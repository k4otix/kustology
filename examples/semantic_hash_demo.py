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
``Exists.op`` does the same for ``isnotnull`` / ``isnotempty``.

**Merges you may not want.** Two literal collapses are deliberate, and
four operator modifiers plus every non-``let`` statement kind are known
gaps. They are listed here rather than hidden, because a dedup consumer
needs to know which merges it is buying.

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
]

KNOWN_MERGES = [
    ("typed nulls: real(null) == datetime(null) — deliberate",
     "T | where a > real(null)", "T | where a > datetime(null)"),
    ('obfuscated strings: h"x" == "x" — deliberate',
     'T | where a == h"x"', 'T | where a == "x"'),
    ("mv-apply drops `to typeof(...)` — known gap",
     "T | mv-apply d to typeof(long) on (take 1)",
     "T | mv-apply d on (take 1)"),
    ("getschema drops `kind=csl` — known gap",
     "T | getschema kind=csl", "T | getschema"),
    ("a `set` statement is discarded entirely — known gap",
     "set query_now=datetime(2020-01-01); T | take 1", "T | take 1"),
]


def report(title: str, pairs, expect_equal: bool) -> None:
    print(f"\n=== {title}")
    for label, left, right in pairs:
        same = digest(left) == digest(right)
        verdict = "merge " if same else "split "
        flag = "" if same == expect_equal else "   <-- UNEXPECTED"
        print(f"  {verdict} {label}{flag}")


def main() -> None:
    print(f"Scheme: {SEMANTIC_HASH_SCHEME}")
    print(f"Example digest: {digest('T | take 1')}")

    report("Merges the contract promises", MERGES, expect_equal=True)
    report("Splits the contract promises", SPLITS, expect_equal=False)
    report("Merges to know about before deduplicating",
           KNOWN_MERGES, expect_equal=True)

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
