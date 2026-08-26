# The Tier 2 IR

Tier 2 turns a parsed query into a typed Pydantic model (`FilterOp`,
`BinOp`, `ColumnRef`), so an analyzer can ask questions Tier 1's syntax
tree does not answer directly: which source table a column came from
after joins, renames, and `let` aliases; what schema the pipeline
produces at the end; whether two queries are the same modulo
formatting; how to serialize the graph for a UI, a service, or a
language model. This page covers three things you need to know before
you walk the IR: how `let` names lower, what the operator vocabularies
map to across the two tiers and the wire format, and where the IR's
modeling stops.

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
