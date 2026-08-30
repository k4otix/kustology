# Semantic hash

`kustology` is a `0.y` release: per SemVer §4, the public API is not yet
stable. Tier 1 is on a stabilization track and reaches 1.0 once it survives
external use without correctness breaks. Tier 2, the IR and
`semantic_hash`, is expected to keep evolving at minor cadence. This page
is for anyone storing `semantic_hash` values, deduplicating queries with
them, or persisting IR JSON across upgrades.

## Version numbers

Three numbers describe compatibility, and they move independently:

| | What it tags | When it moves |
| --- | --- | --- |
| `kustology.__version__` | the library | SemVer; pre-1.0, either tier may break at a minor |
| `kustology.ir.IR_SCHEMA_VERSION` | the IR's field shape | any breaking field-shape change |
| `kustology.ir.SEMANTIC_HASH_SCHEME` | the `semantic_hash` canonicalization rules | in lockstep with `IR_SCHEMA_VERSION` |

Both IR tags move once per release. Several branches can land between
releases and share one increment. The tags mark what a consumer can
observe.

`build_info()` returns all three plus the bundled `Kusto.Language.dll`
version and SHA-256, and `__version__` now comes from the source tree, not
from install metadata.

## Storing IR JSON

Tag stored IR JSON with `IR_SCHEMA_VERSION` and refuse a payload whose tag
you do not recognize. Every IR model sets `extra="forbid"`, so a dump with
fields from a different schema fails to load. IR JSON written before
0.2.0 does not load into 0.2.0.

## Storing hashes

`semantic_hash` carries its scheme as a prefix (`kustology-sem-v2:…`). A
hash computed under a different scheme carries a different prefix, so a
stored hash never collides by accident with a freshly computed one from a
different scheme.

The digest is computed when first read and included in `model_dump()`;
exclude it (`exclude={"semantic_hash"}`) for a cheap dump.

Check the prefix before comparing hashes you deduplicate by. Schemes
differ in which queries they merge: `kustology-sem-v2` distinguishes
`in` / `in~` / `has_any` / `has_all` and `isnotnull` / `isnotempty`, where
`kustology-sem-v1` does not. When the schemes differ, rehash both queries
from source and compare the new hashes.

For anything short of exact equality — how much two queries overlap, or
where two versions of a rule diverge — see
[Graded similarity](similarity.md).

## What the digest ignores

The digest is built to survive differences that do not change what a query
returns. Within `kustology-sem-v2` these are ignored:

- **Operand order in commutative positions.** `where A and B` and
  `where B and A` are one digest, as are `in ("x", "y")` and
  `in ("y", "x")`. Consecutive `| where` operators merge into one `and`
  first, so `| where A | where B` joins them. Only consecutive `where`
  operators merge: `| where A | take 5` and `| take 5 | where A` still
  differ.
- **`let` names.** Each is replaced by its position in a scope-ordered
  walk, so `let n = 5; T | where a > n` and `let m = 5; T | where a > m`
  collide. The same holds for a `let` written inside a function body or a
  `declare pattern` arm. Which binding a reference points at is still
  hashed, and a `let`-bound `n` never collides with a real column `n`. A
  function's own name at its call site is part of the same rename: `let f
  = () {…}; f()` and `let g = () {…}; g()` collide, but only when the
  visible `let` bound a function. KQL resolves values and functions in
  separate namespaces (`let abs = 5; T | extend y = abs(x)` binds a value
  and calls the built-in, both cleanly). Any other call (a built-in, a
  server-side function, or one sharing a scalar binding's name) is left as
  written, so two queries calling two different functions hash apart. A
  `let` bound to a function by alias (`let g = f; g()`) is not renamed
  either. The Limits section below explains what that costs.
- **Function parameter names.** A parameter is a local label like a `let`:
  each is replaced by its position in the signature (`$param0`, …), and
  every reference to it inside that body follows the same rename, so
  `let f = (w:int) { T | where a > w }` and `let f = (z:int) { T | where a
  > z }` are one digest. Which parameter a body reads is still hashed, and
  the rename reaches inside that one body only. `declare pattern`'s own
  parameters are not renamed: they name an arm's match slots. `declare
  query_parameters` names are not renamed either, because that list is the
  caller-facing API of a saved query. `declare query_parameters(p:long)`
  and `declare query_parameters(q:long)` accept different requests and must
  not merge.
- **The host's timezone and locale.** A `datetime` literal is normalized to
  UTC before it is hashed, and numeric literals render invariant. The same
  query digests identically in Tokyo and in New York, under `de-DE` and
  under `C`.
- **Everything the binder supplied.** Column types, table provenance, and
  result schemas are stripped, so passing a schema does not move the
  digest. Source offsets and `hint.*` are stripped too: a hint changes how
  the engine executes a query without changing the rows it returns.

Your own IR keeps all of this as written; canonicalization runs on a
private copy for hashing only. `normalize_expressions` is a separate,
opt-in transform, and it leaves operand order alone.

## Limits of the digest

- The digest is not invariant across bind state for a query whose `let`
  aliases a table. Binding proves the alias names a table, which changes
  the IR's shape, and stripping volatile fields cannot hide a shape
  change. The alternative is treating every bare name as a table without
  proof.
- A local name is matched by text, where KQL resolves a column first. A
  `let` name or a function parameter that shadows a same-named real column
  is classified as the local one, so the rename rules above reach it and
  two queries reading different things can share a digest. This includes a
  shadowing column created by an earlier `extend`. Deciding by symbol
  instead needs a bound parse, which would make the digest depend on bind
  state. See [`tier2-ir.md`](tier2-ir.md) for the full shadowing behavior.
- The call-site rename only fires for a `let` whose right-hand side is
  itself a function. A binding that merely aliases one (`let g = f; g()`)
  keeps the name as written, so `let g = f; g()` and `let f = …; f()`
  hash apart. A split costs a deduplicating consumer a duplicate entry; a
  wrong merge would cost it a rule.
- Equal digests do not prove equivalence. Two cases can still tie: a
  handful of literal-level merges (typed nulls, obfuscated strings) that
  the library treats as equivalent, and a `let` function's call sites,
  which record the body once, at the declaration, and reuse it for every
  call.

Every statement kind is modeled. `set`, `declare query_parameters`,
`declare pattern`, `alias database`, and `restrict access to` live on
`QueryIR.statements` in source order, and that order is part of the digest
because `set` scopes the query that follows it. So
`set query_now=datetime(2020-01-01); T | take 1` hashes apart from a bare
`T | take 1`, and apart from the same query pinned to a different
`query_now`.

[`../examples/semantic_hash_demo.py`](../examples/semantic_hash_demo.py)
hashes every merge it files when it runs and raises if one stops behaving
as filed: the literal-level pair. The call-site merge is pinned by
[`../tests/ir/test_let_bindings.py`](../tests/ir/test_let_bindings.py)
instead. See also [`../CHANGELOG.md`](../CHANGELOG.md)'s Fixed section for
collisions that were closed, and its Known limitations section for the
boundaries that remain.
