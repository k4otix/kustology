# The Tier 2 IR

Tier 2 turns a parsed query into a typed Pydantic model (`FilterOp`,
`BinOp`, `ColumnRef`), so an analyzer can ask questions Tier 1's syntax
tree does not answer directly: which source table a column came from
after joins, renames, and `let` aliases; what schema the pipeline
produces at the end; whether two queries are the same modulo
formatting; how to serialize the graph for a UI, a service, or a
language model. This page covers what you need to know before you walk
the IR: how `let` names lower, what the operator vocabularies map to
across the two tiers and the wire format, what the binder resolves
without a schema, where the IR's modeling stops, and how to traverse
the tree with `walk` and `find_all`.

## How `let` names lower

A `let` binds either a table-shaped thing or a scalar. The IR keeps
both apart from real columns and real tables, using three node types:

| in the query | in the IR | where it appears |
| --- | --- | --- |
| `let Base = SecurityEvent \| …;` … `Base \| project X` | `LetRef` | pipeline source, `find in (…)`, `search in (…)` |
| `let threshold = 5;` … `where Count > threshold` | `LetValueRef` | expression position |
| `let f = (x:int) { … };` | `LetBinding.rhs_function` (a `LetFunction`) | signature on `.parameters`, body on `.body_lets` / `.body_pipeline` / `.body_expr` |

Because of this split, `find_all(ir, TableRef)` answers "which tables
does this query read" and `find_all(ir, ColumnRef)` answers "which
columns does it touch." Neither list contains `let` names. Use
`find_all(ir, LetRef)` for the tabular aliases and
`find_all(ir, LetValueRef)` for the scalars.

There is one exception: a tabular function parameter (`(T:(*))`)
shadows the same way any other parameter does, so a reference to it
inside the body lowers as a `TableRef`, indistinguishable there from a
real table name.

### Resolving columns through an alias

A bound parse resolves columns through a tabular alias. In

```
let Base = SecurityEvent | …; Base | project Account
```

`Account` carries the type from `SecurityEvent` and the provenance
`Base`. An alias can shadow a real table name (`let SecurityEvent =
SecurityEvent | …` is a common Sentinel idiom), so `ColumnRef.table` is
a scope name, not a guaranteed table name. Read `result_type` rather
than re-deriving types from `ColumnRef.table`. When you need to tell a
scope name from a real table name, see the `enrich` docstring in
[`kustology/ir/binder.py`](../src/kustology/ir/binder.py).

### Known limitation: a `let` name can shadow a real column

Which of `LetRef`, `LetValueRef`, or `ColumnRef` a name becomes is
decided from the `let` statements alone, without the binder. This
keeps the classification, and therefore `semantic_hash`, independent
of whether you passed a schema. See [`semantic-hash.md`](semantic-hash.md)
for how the hash is built.

KQL resolves names the other way round: an unqualified name is a
row-scope column first and a `let`-bound variable second. So where a
`let` name shadows a real column (`let Count = 5; T | where Count > 1`
over a `T` that has a `Count` column), the IR records the reference as
a `LetValueRef`. `find_all(ir, ColumnRef)` does not report it.

## Three names for the same operator

Each tier has its own vocabulary, and a third appears on the wire. One
`where` is a `FilterOperator` node in Microsoft's syntax tree, a
`FilterOp` model in the IR, and `"kind": "filter"` in the IR's JSON:

| KQL | Tier 1 — `str(node.Kind)`, `parse --ast` | Tier 2 — class / `kind` |
| --- | --- | --- |
| `where` | `FilterOperator` | `FilterOp` / `"filter"` |
| `project` | `ProjectOperator` | `ProjectOp` / `"project"` |
| `extend` | `ExtendOperator` | `ExtendOp` / `"extend"` |
| `summarize` | `SummarizeOperator` | `SummarizeOp` / `"summarize"` |
| `join` | `JoinOperator` | `JoinOp` / `"join"` |
| `sort by`, `order by` | `SortOperator` | `SortOp` / `"sort"` |
| `take`, `limit` | `TakeOperator` | `TakeOp` / `"take"` |
| `mv-expand` | `MvExpandOperator` | `MvExpandOp` / `"mv_expand"` |

Two spellings that share a parser node share an IR node too. `order by`
is `sort`, and `limit` is `take`, so an analyzer written against one
spelling sees both.

The mapping from Tier 1 to Tier 2 is mostly mechanical: Microsoft's
`<Name>Operator` becomes our `<Name>Op`, and hyphens become
underscores in `kind`. It is not fully mechanical, though: `where` is
a `FilterOperator`, and its `kind` is `filter`, not `where`. Read
`SomeOp.model_fields["kind"].default` rather than deriving the string
yourself. These are the discriminators `to_llm_dict` and
`model_dump_json` emit, so they are part of the IR's versioned shape.

## What the binder knows without a schema

`to_ir()` binds an unbound parse against `GlobalState.Default`, which knows
every built-in function but no table. So `result_type` is populated for
anything the built-ins decide: `ago(2h)` is `datetime`, `8h` is `timespan`,
and `end - 8h` is `datetime` when `end` was bound to `ago(2h)` — even though
every column stays `unresolved`. A resolver that keeps one `dict[str,
timedelta]` for `let` bindings conflates the two and reads `end - 8h` as
`2h - 8h`; dispatching on `binding.rhs_expr.result_type` does not.
[`examples/lookback_window.py`](../examples/lookback_window.py) computes a
detection's outer lookback that way.

## Where Tier 2 stops

Eight operators are recorded as their own source text rather than
structured fields, on `raw_text`: `scan`, `top-nested`, `make-graph`,
`graph-match`, `graph-mark-components`, `graph-shortest-paths`,
`graph-to-table`, and `macro-expand` (which also keeps its inner
pipeline). They round-trip and they hash, but there is nothing typed
inside them to walk. `graph-where-edges` and `graph-where-nodes` are
modeled, with a real predicate.

### Function call sites are not inlined

A `let`-declared function call site is the other boundary, and it is a
narrow one. `let f = (x:int) { … }` records a `LetFunction` carrying
the parameters (declared types and defaults included), the `view`
keyword, and the body: `body_lets` for a `let` written inside the
braces, then `body_pipeline` or `body_expr` for the tail. The IR does
not inline the body: `f(1)` stays a call, and the body is reachable
through the declaration.

Both tiers see through the body, so a lineage walk over a
function-declaring query reports the tables and columns the body
reads. On a query with zero diagnostics:

```python
q = 'let f = () { SecurityEvent | where Account=="root" | project Computer }; f()'
parse(q).get_referenced_tables()          # {'SecurityEvent'}
parse(q).get_referenced_columns()         # {'Account', 'Computer'}

ir = parse(q).to_ir()
[t.name for t in find_all(ir, TableRef)]  # ['SecurityEvent']
{c.name for c in find_all(ir, ColumnRef)} # {'Account', 'Computer'}
```

Because the body is not copied per call site, a query that calls one
function three times still reports its tables once: the declaration is
the one place they are written. If you need the count of call sites
instead, walk the call expressions yourself. `body_span` locates the
body in the source if you want the text. See
[`examples/walk_ir.py`](../examples/walk_ir.py) for a worked
traversal.

### Parameter shadowing

A parameter shadows a same-named `let` from the enclosing query for
the length of the body. This is decided from the declaration text
alone, so it does not depend on whether you passed a schema. Inside

```
let n = 5; let f = (n:int) { T | where a > n }
```

the body's `n` is a `ColumnRef`, not a `LetValueRef`.

## Traversal

`walk(node)` yields `node` and every pydantic `BaseModel` under it,
depth-first and pre-order. Writing a custom analyzer over the IR usually
means answering two separate questions, and `walk` keeps them as two
separate parameters instead of folding them into one:

* **What should come back?** `predicate` filters what `walk` *yields*.
  A node `predicate` rejects is not returned, but `walk` still descends
  into its children — a `FilterOp` your predicate skips does not hide
  the `ColumnRef`s inside its own predicate.
* **Where should the walk go?** `prune` filters what `walk` *descends
  into*. A node `prune` accepts is still yielded, but none of its
  descendants are visited. Use it to read an outer pipeline without the
  subqueries a `join` or `lookup` carries:

  ```python
  from kustology.ir import JoinOp, LookupOp, walk

  outer = walk(ir.main_pipeline, prune=lambda n: isinstance(n, (JoinOp, LookupOp)))
  ```

  The `JoinOp` itself is still in `outer`; its `right` pipeline — the
  subquery's own source, filters, and time literals — is not.
  [`examples/lookback_window.py`](../examples/lookback_window.py) uses
  exactly this `prune` to compute a detection's *outer* lookback window
  without the wider time range a joined lookup table reads.

`find_all(node, SomeType, prune=...)` is the wrapper most analyzers
actually reach for: it calls `walk` and keeps only the instances of
`SomeType`, which covers the majority case of "give me every `BinOp`" /
"every `TableRef`" plus attribute access. `prune` passes straight
through to the underlying `walk`.

### `span_of`

Every `Operator` and `Expr` node carries its own `span` field, but
`Pipeline`, `QueryIR`, and the statement models do not — they are pure
containers built out of other nodes, with no token range of their own.
`span_of(node)` answers "where in the source is this?" for those
classes anyway: it folds over `find_all(node, Span)`, skips zero-width
spans, and returns the smallest span covering everything it found (or
`None` if the subtree carries none). It is how
`examples/lookback_window.py` prints the outer pipeline's own source
text — `span_of(ir.main_pipeline).text(ir.raw_text)` — with no
`Pipeline.span` to read directly.

### `walk` yields `Span` objects too

`Span` is itself a pydantic `BaseModel`, so an unfiltered `walk(node)`
yields every node's `span` field alongside the operators and
expressions that own them, interleaved with everything else in
depth-first order. Filter them out with `isinstance(n, Span)` when a
predicate needs to see only structural nodes, or reach for
`find_all(node, SomeType)` in the first place — a type filter for
anything other than `Span` already excludes them.

### The IR is a DAG in one place

`walk` deduplicates by identity (`id(node)`), not equality, because one
corner of the IR is a DAG rather than a tree:
`LetBinding.inner_time_exprs` holds the *same* `AnyExpr` objects that
also sit inside that binding's `rhs_pipeline` or `rhs_function` — not
copies of them. So `ago(1h)` inside a `let`'s pipeline is one object
reachable through two fields, and an un-deduplicated walk would report
it twice. `walk` and `find_all` yield it once, keyed on the object's
identity rather than its structure, so two separately-written `ago(1h)`
literals at different offsets still both surface.

`LetBinding.inner_tables` does not raise the same question: it is a
plain `list[str]`, a copy of table names rather than a hop to a shared
node, so there is nothing for identity-based deduplication to do
there.

`prune` stops a path, not an object — a node reachable through another
field (for example a `let`'s time expression through
`LetBinding.inner_time_exprs`) is still yielded when the walk reaches it
that way.
