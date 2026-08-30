# Graded similarity

`similarity`, `containment`, `differing_subtrees`, and the rest of
`kustology.ir`'s similarity functions compare two or more queries by
degree instead of by a single yes/no digest.

## What this answers

`semantic_hash` (see [semantic-hash.md](semantic-hash.md)) answers one
question exactly: are these two queries the same, modulo formatting, `let`
names, and operand order? Tier 1's `get_structural_hash` sits at the other
extreme: it hashes the AST shape alone, blind to every literal, table name,
and column name, so two queries with the same skeleton but no relationship
to each other — different tables, different columns, different intent —
still collide. Most real questions live between those two poles: how much
of two queries overlaps, which part of a rule someone changed, and whether
one query is a near-duplicate of another. Graded similarity fills that gap
by comparing at the subtree level, with a number between 0 and 1 instead of
an equal/not-equal verdict.

## Subtree digests

`subtree_hashes(node, *, min_size=3, spans=True)` returns one `SubtreeHash`
per subtree of `node` with at least `min_size` model nodes, in post-order —
children before parents, root last. Each entry carries the span locating that
subtree in the source; `spans=False` leaves those `None` and skips building
the map, which costs a subtree walk per node. `similarity`, `containment` and
`similarity_sketch` pass it, since they read only the digests. The digests come from the same canonical copy
`compute_semantic_hash` builds: `let` names replaced by their position in a
scope-ordered walk, consecutive `where` operators merged into one `and`,
commutative operand order sorted, and every binder-supplied field, span,
and `hint.*` stripped. The root entry's digest equals
`compute_semantic_hash(node)` — pass the `QueryIR` root, not a bare
`Pipeline`, to get that `let`-name invariance; a bare `Pipeline` keeps
`let` names as written, the same rule `compute_semantic_hash` follows.

Because canonicalization runs on the whole tree, a subtree's entry here can
differ from calling `compute_semantic_hash` on that subtree pulled out on
its own: a `let` name canonicalizes relative to the whole query's binding
order, and two consecutive `where` operators merge into one node before
either gets a digest. Isolating a subtree loses that context.

Each entry carries:

- `digest` — the same scheme and prefix as `semantic_hash`.
- `kind` — the node's `kind` field, or its class name when it has none.
- `size` — model nodes in the subtree; a `Span` doesn't count.
- `span` — the subtree's envelope in your IR, or `None` if nothing below
  it carries one.

`min_size` floors what comes back, so a bag of digests isn't dominated by
single-token matches (a bare column reference, a lone literal) that say
nothing about structure. For a multi-resolution view, call once at a low
`min_size` and filter the result by `.size` afterward — that reads at
several granularities from one call instead of one call per threshold.

## Comparing two queries

`similarity(a, b)` is the Jaccard overlap of two subtree-digest bags: `a`
and `b` can each be a `QueryIR` (or any node), an iterable of
`SubtreeHash`, or an iterable of digest strings. It returns `0.0` when both
sides are empty, not `1.0` — two empty bags carry no comparable structure,
so there's nothing to call identical.

`containment(a, b)` is directional: the share of `a`'s subtrees found
inside `b`. Use it to ask "is `a`'s logic a subset of `b`'s," the way
`similarity`, normalized by the union, can't. A larger `b` has more
operators wrapped around the same logic, so its root and top-level
pipeline entries structurally differ from `a`'s and never match — even a
clean subsumption caps out below 1.0 on the raw bags. For a strict
subsumption test, call `subtree_hashes` yourself and pass `containment`
the bags filtered to exclude those top-level `kind`s, or filtered by
`size` to the operators you actually care about.

`differing_subtrees(a, b, *, min_size=3)` finds where two queries diverge.
A single changed node changes the digest of every one of its ancestors, so
"the largest differing subtree" is always the whole query — not useful.
This function reports the other end instead: the smallest subtrees of `a`
whose digest doesn't appear anywhere in `b`, but whose own children are all
present in `b`. Ancestors of a reported node are suppressed, so the result
localizes each change rather than repeating it at every level above it. A
change smaller than `min_size` surfaces at its smallest qualifying
ancestor. Swap the arguments — `differing_subtrees(b, a)` — for what
changed on `b`'s side.

## Comparing many

`similarity_sketch(a, *, k=128)` returns a fixed-size MinHash sketch: an
8-byte header (magic, `k`, and a tag for the digest scheme) followed by `k`
4-byte slots — 520 bytes at the default `k`. `sketch_similarity(s1, s2)`
estimates `similarity` from the share of slots that agree, without ever
materializing the full digest bags; it raises when the two sketches' magic,
scheme or `k` don't match. Sketching an empty bag raises, since there's
nothing to hash into slots.

A sketch is exactly as durable across schemes as `semantic_hash` itself or
stored IR JSON (see semantic-hash.md's "Storing hashes"), because the digests
underneath it change with the canonicalization. The header carries the scheme
that built it, so a sketch stored across a `SEMANTIC_HASH_SCHEME` bump is
rejected rather than compared: recompute it from the IR. The slots themselves
come from a keyed hash of each permutation's index, so two sketches agree
across processes and across interpreter versions.

Neither `similarity` nor a sketch weights subtrees by how much they say
about a query. Over a corpus, a filter such as `where TimeGenerated >
ago(x)` recurs across a large share of any detection corpus and so
carries little signal, but plain Jaccard counts it the same as a subtree
two queries share by coincidence nowhere else. Down-weighting a digest by
how common it is across the corpus fixes that, and it's the consumer's
job: kustology has no notion of a corpus.

```python
import math
from collections import Counter

def idf_weighted_jaccard(bags: list[set[str]], a: set[str], b: set[str]) -> float:
    df = Counter(digest for bag in bags for digest in bag)
    idf = {digest: math.log(len(bags) / count) for digest, count in df.items()}
    inter = sum(idf[d] for d in a & b)
    union = sum(idf[d] for d in a | b)
    return inter / union if union else 0.0
```

Indexing a large corpus for sub-linear retrieval — banding a sketch's
slots and bucketing by hash of each band (LSH) — is the same kind of
consumer-side structure, built on `similarity_sketch`'s output rather than
provided by it.

## Choosing the defaults

`scripts/eval_similarity.py --sentinel-repo <checkout>` is the mechanism
behind `min_size=3` and `k=128`: it re-derives both against a public
detection corpus, so a maintainer can rerun it rather than take these
numbers on faith.

`min_size=3` is the default because recall — finding a drifted copy's
counterpart at all — held within a few points of itself from `min_size=1`
through `3` and then dropped sharply at `4`, while precision on the
family grouping only declined gently across that same range, with no
comparable break. 3 sits at the last point before the recall cliff. The
script prints the mean digest-bag size alongside each table, so rerunning
it shows directly how much bag weight a higher `min_size` sheds for that
trade.

`k=128` is the default because the script's fidelity table showed the
sketch's mean absolute error against the exact Jaccard value flattening
there: `k=64` still tracks the exact value closely enough to be useful when
520 bytes per query is too much to store, and `k=256` narrows the error
further for the same trade running the other way, but neither moves the
mean far enough to change the default. Rerun the script to see the table for
your own corpus.

Read that error on the pairs a caller reads the estimator at — the ones that
share at least one digest. A pair with nothing in common has an exact
similarity of 0.0 and a sketch estimate of 0.0, so averaging every pair in a
corpus measures mostly trivially-correct zeros: on this repo's 49 fixtures,
93.5% of pairs are disjoint and the whole-corpus mean is more than an order
of magnitude below the mean over overlapping pairs. Both the script and
`tests/ir/test_similarity.py` sample only overlapping pairs for this reason;
the test is a regression guard on the permutation family, not the source of
the default.

## What it is not

**Not adversarially robust.** The digest recognizes exactly the
canonicalizations `semantic_hash` documents — `let` renaming, filter
merging, operand order, and the rest — and nothing past that. Whoever
writes the query text can defeat it: restructure logic into a form outside
those known merges, or add a no-op branch, and the digests split even
though nothing about what the query returns changed. Treat this as a
structural fingerprint, not a semantic-equivalence proof.

**Not a replacement for diffing drifted textual copies.** Plain token
shingling — n-grams over the raw query text — finds a copy-pasted, lightly
edited rule about as well as subtree digests do. What subtree digests add
is the semantic invariances a token diff can't see (`let` renames, filter
splitting and merging, operand order, `hint.*`), results that map to a
concrete node `kind` and source `span` instead of opaque line ranges,
directional `containment`, and a diff that localizes to the smallest
changed subtree instead of only flagging that two queries differ.

**Equal digests still don't prove equivalence.** The same handful of cases
that let two different queries share one `semantic_hash` apply here,
subtree by subtree — see semantic-hash.md's
[Limits of the digest](semantic-hash.md#limits-of-the-digest).

See [`examples/query_similarity.py`](../examples/query_similarity.py) for
all of this computed against a small set of related queries.
