# Kustology

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue.svg)](https://www.python.org/downloads/)
[![.NET 8.0+](https://img.shields.io/badge/.NET-8.0+-purple.svg)](https://dotnet.microsoft.com/download/dotnet/8.0)

> **Not affiliated with Microsoft.** This is an independent open-source project
> that wraps Microsoft's publicly distributed Apache 2.0–licensed library.

Kustology is a Python library that exposes Microsoft's KQL parser — the
same one Azure Data Explorer, Azure Monitor, and Microsoft Sentinel use
internally — through `pythonnet`. It has two tiers you can adopt
independently: a thin wrapper around Microsoft's syntax tree, and an
opt-in intermediate representation (IR) built from Pydantic.

## Tier 1 — thin wrapper

The thin tier exposes Microsoft's parser, formatter, and validator, and
adds AST analyzers for the questions a KQL author asks most often:
which tables a query touches, which columns it reads, which operators
chain through it, where time filters live, and how to rename a table
everywhere it appears. You work with Microsoft's syntax tree directly.

## Tier 2 — semantic IR

The IR tier gives you a typed Pydantic model of the parsed query —
`FilterOp`, `BinOp`, `ColumnRef` — for the questions an *analyzer*
asks: which source table a column came from after joins, renames and
`let` aliases, what schema the pipeline produces at the end, whether two
queries are the same modulo formatting, and how to serialize the whole
graph for a UI, a service, or a language model.

### `let` names are their own nodes

A `let` binds either a table-shaped thing or a scalar, and the IR keeps the
two apart from real columns and real tables:

| in the query | in the IR | where it appears |
| --- | --- | --- |
| `let Base = SecurityEvent \| …;` … `Base \| project X` | `LetRef` | pipeline source, `find in (…)`, `search in (…)` |
| `let threshold = 5;` … `where Count > threshold` | `LetValueRef` | expression position |
| `let f = (x:int) { … };` | `LetBinding.rhs_function` (a `LetFunction`) | — |

So `find_all(ir, TableRef)` answers "which tables does this query read" and
`find_all(ir, ColumnRef)` answers "which columns does it touch" — neither
list is polluted by `let` names. Use `find_all(ir, LetRef)` for the tabular
aliases and `find_all(ir, LetValueRef)` for the scalars.

A bound parse resolves columns *through* a tabular alias: in
`let Base = SecurityEvent | …; Base | project Account`, `Account` carries the
type from `SecurityEvent` and the provenance `Base`. An alias can shadow a
real table name (`let SecurityEvent = SecurityEvent | …` is a common Sentinel
idiom), so `ColumnRef.table` is a scope name rather than a guaranteed table
name — read `result_type` rather than re-deriving types from it, and see the
`enrich` docstring in `kustology/ir/binder.py` for telling the two apart when
you must.

**Known limitation.** Which of `LetRef` / `LetValueRef` / `ColumnRef` a name
becomes is decided from the `let` statements alone, without the binder, so
that the classification — and therefore `semantic_hash` — cannot depend on
whether you passed a schema. KQL resolves the other way round: an unqualified
name is a row-scope column first and a `let`-bound variable second. Where a
`let` name shadows a real column (`let Count = 5; T | where Count > 1` over a
`T` that has a `Count`), the reference is recorded as a `LetValueRef` and
`find_all(ir, ColumnRef)` does not report it.

### Where Tier 2 stops

Eight operators are recorded as their own source text rather than structured
fields, on `raw_text`: `scan`, `top-nested`, `make-graph`, `graph-match`,
`graph-mark-components`, `graph-shortest-paths`, `graph-to-table`, and
`macro-expand` (which also keeps its inner pipeline). They round-trip and
they hash, but there is nothing typed inside them to walk. `graph-where-edges`
and `graph-where-nodes` are modeled, with a real predicate.

A `let`-declared **function body** is the other boundary: `let f = (x:int)
{ … }` records a `LetFunction` with the parameter names and a `body_span`
locating the body in the source. The body itself is not built, call sites are
not expanded, and parameter types and defaults are not recorded.

**This is the one place the two tiers disagree about the same query**, and it
is worth knowing before you use a Tier 2 walk for lineage. Tier 1 walks
Microsoft's tree, which contains the body; Tier 2 walks the IR, which does
not. On a query with zero diagnostics:

```python
q = 'let f = () { SecurityEvent | where Account=="root" | project Computer }; f()'
parse(q).get_referenced_tables()          # {'SecurityEvent'}
parse(q).get_referenced_columns()         # {'Account', 'Computer'}

ir = parse(q).to_ir()
list(find_all(ir, TableRef))              # []
list(find_all(ir, ColumnRef))             # []
```

So `find_all(ir, TableRef)` is exhaustive over the query's *pipelines*, not
over the query. If you need every table a query can touch and the query
declares functions, use Tier 1's `get_referenced_tables()`, or slice the body
out with `body_span` and parse it separately. The same gap is why two `let`
functions with different bodies share a `semantic_hash` — see [What
`semantic_hash` deliberately ignores](#what-semantic_hash-deliberately-ignores).

## Choosing a tier

Both tiers share the same parser; pick based on what shape of data your
code wants to work with.

| | Tier 1 — thin wrapper | Tier 2 — semantic IR |
|---|---|---|
| **Install** | `pip install kustology` | `pip install 'kustology[ir]'` |
| **Dependencies** | `pythonnet` + .NET 8 runtime | adds `pydantic` |
| **Returns** | `KustoQuery` wrapping Microsoft's syntax tree | `QueryIR` — Pydantic models |
| **Traversal** | Microsoft AST (`node.Kind` dispatch via `pythonnet`) | Typed pipeline (`isinstance` dispatch) |
| **Serialization** | `KustoQuery.to_dict()` / `to_json()` | `model_dump_json` (round-trips through `QueryIR.model_validate_json`) + `to_llm_dict` (LLM-tailored, lossy) |
| **Schema binding** | `parse(query, schema=...)` runs Microsoft's binder — semantic diagnostics plus symbol resolution accessible via AST methods | `to_ir()` on a bound parse already carries Microsoft's per-operator `result_schema` and column `result_type` — the builder stamps both at construction, independent of `attach_schema`. The attach pass adds table provenance (`ColumnRef.table`) and sets `schema_attached`. Without a schema, `to_ir()` still types literals and built-in calls — see below |
| **Best for** | Formatting / linting, IDE integrations, extracting referenced tables/columns/functions/operators, surgical table renames | Lineage and anti-pattern analyzers, JSON-serializable query representations for APIs and UIs, schema-aware column flow, LLM-fed query graphs |

### Three names for the same operator

Each tier has its own vocabulary, and a third appears on the wire. One `where`
is a `FilterOperator` node in Microsoft's tree, a `FilterOp` model in the IR,
and `"kind": "filter"` in the IR's JSON:

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

Two spellings that share a parser node share an IR node too — `order by`
really is `sort`, `limit` really is `take` — so an analyzer written against
one spelling sees both.

The mapping is mostly mechanical (Microsoft's `<Name>Operator`, our
`<Name>Op`, hyphens becoming underscores in `kind`) but not always: `where`
is a `FilterOperator`, so its `kind` is `filter`, not `where`. Read
`SomeOp.model_fields["kind"].default` rather than deriving the string.
These are the discriminators `to_llm_dict` and `model_dump_json` emit, so
they are part of the IR's versioned shape.

## Working with Microsoft's syntax tree

Tier 1 is a thin projection: you get Microsoft's nodes, with Microsoft's
shapes. These are the places that shape surprises people.

**Member lookup is exact, case-sensitive, and silent.** pythonnet resolves
.NET members by exact name and returns nothing when one is absent, so a
typo'd or mis-cased probe fails quietly into your fallback:

```python
getattr(node, "Uris", None)   # None -- the member is URIs
getattr(node, "URIs", None)   # the SyntaxList you wanted
```

Nothing raises and nothing logs. Four fields in this library sat at their
default for a full release because of exactly this. Before relying on a
probe, confirm the member exists:

```python
[m for m in dir(node) if m[:1].isupper()]
```

**An empty .NET collection is truthy in Python.** `IReadOnlyList` does not
implement `__bool__`, so the natural check never fires — use `.Count`:

```python
if not code.GetSyntaxDiagnostics():      # never true, even with 0 diagnostics
if code.GetSyntaxDiagnostics().Count == 0:   # correct
```

**`node.Kind` is a .NET enum, not a string.** It has no `__format__`, so any
f-string format spec raises `TypeError`. Call `str()` on it — always:

```python
f"{node.Kind:<30}"       # TypeError: unsupported format string
f"{str(node.Kind):<30}"  # fine
```

**List-valued properties yield `SeparatedElement` wrappers.**
`ProjectOperator.Expressions`, `QueryBlock.Statements` and
`FunctionParameters.Parameters` return `SyntaxList[SeparatedElement[T]]`; the
wrapper carries the trailing comma alongside the expression. Its `str()` reads
almost like the expression's, so a missing unwrap looks correct in printed
output while every `.Kind` check silently fails to match — the wrapper's
`Kind` is `SeparatedElement`, never the expression's. Use `iter_elements`,
which also passes through plain `SyntaxList[T]` such as
`SummarizeOperator.Parameters`:

```python
from kustology import iter_elements, parse

for expr in iter_elements(project_operator.Expressions):
    print(str(expr.Kind))   # NameReference, not SeparatedElement
```

**`BinaryExpression` is one generic class; the operator lives in `Kind`.**
All six comparisons share the class, so branching on type will not separate
them. Branch on `str(node.Kind)` (`GreaterThanExpression`, `EqualExpression`,
`NotEqualExpression`, ...) or read `node.Operator.ToString().strip()`.

**`!between` shares `BetweenExpression` with `between`.** The negation exists
only in `Kind` (`NotBetweenExpression`), so branching on class silently
inverts the predicate. Both put the column in `.Left` and the bounds in
`.Right` as an `ExpressionCouple` with `.First` / `.Second`.

**Tier 1 symbols require a schema — there is no partial binding.** `parse(q)`
calls `KustoCode.Parse`, which does no semantic analysis: `has_semantics` is
`False` and every `ReferencedSymbol` is `None`, built-in functions included.
`parse(q, schema=...)` binds, and they all resolve. It is all-or-nothing.

**Tier 2 is not all-or-nothing.** `to_ir()` on an unbound parse runs
`KustoCode.Analyze(GlobalState.Default)` over the tree already in hand — no
second parse — purely to acquire types, so a schemaless IR carries real ones
for everything that does not need a table:

```python
ir = parse("StormEvents | where StartTime > ago(7d) and CpuPct > 1.5").to_ir()
# 1.5      -> result_type real,     literal_kind "real"
# 7d       -> result_type timespan, literal_kind "timespan"
# ago(...) -> result_type datetime, is_time_func True
# StartTime, CpuPct -> result_type unresolved, table None
```

`GlobalState.Default` describes Kusto's built-in functions, aggregates and
plug-ins — that is why `ago(1h)` types — but its **database is empty**: no
tables, user-defined functions, external tables, materialized views, entity
groups or stored query results. So columns and tables stay `unresolved`
until you supply a schema, and the "unknown name" diagnostics that binding
raises are an artifact of how the types were obtained rather than anything
you wrote. `to_ir()` filters that family out; a parse *you* bound keeps every
one of them, because there an undescribed name is a real error.

The Tier 1 object is untouched by any of this: `has_semantics` stays `False`
and every Tier 1 accessor keeps taking its syntactic path.

**Read `TimeSpan.Ticks`, not `TotalSeconds`.** `TotalSeconds` is a float and
loses sub-second exactness. A tick is 100ns, so `ticks // 10` converts to exact
microseconds — enough to round-trip `1microsecond` (10 ticks → 1µs) through a
`datetime.timedelta`. It does **not** round-trip anything finer: `2tick` is 2
ticks, `2 // 10 == 0`, and `timedelta`'s resolution is one microsecond, so 200ns
cannot be represented at all. Read `ticks` itself to preserve sub-microsecond
literals. On Tier 2, `LiteralExpr.ticks` carries the raw tick count directly.

**Unary minus wraps a positive literal.** `-1h` parses as a
`UnaryMinusExpression` over a `TimespanLiteralExpression` whose value is
`+01:00:00` — correct KQL grammar, the same way every language parses `-1`.
Read the sign from the parent. Tier 2 models this as
`UnaryOp(op="-", operand=LiteralExpr(...))`.

**Importing kustology pins .NET's culture to invariant, process-wide.** This
is deliberate and has no opt-out. Microsoft's parser evaluates `LiteralValue`
lazily on property access, using the culture live at that moment, so under a
comma-decimal locale the decimal point is read as a group separator and the
fractional part is swallowed. This is **not** limited to durations — every
fractional numeric literal kind is affected the same way:

| literal | written | read back under `de-DE` without the pin |
| --- | --- | --- |
| `timespan` | `1.5h` | `15:00:00` (fifteen hours) |
| `real` | `1.5` | `15.0` — a `where CpuPct > 1.5` filter becomes 10x too strict |
| `decimal` | `decimal(1.5)` | `15` |

Under `fr-FR` a duration parses to zero instead. Because the corruption
happens in caller code, arbitrarily far from any kustology call, only a
process-global pin closes it. `CurrentUICulture` is left untouched.

**Residual risk:** the pin runs once, at import. A host that assigns
`CultureInfo.DefaultThreadCurrentCulture` or
`Thread.CurrentThread.CurrentCulture` *afterwards* — directly, or through
another .NET-interop library sharing the process — re-opens the corruption for
every `LiteralValue` not yet read, including literals in a tree parsed while
the pin was still in force (the value is computed on first access and cached,
so only already-read literals keep their correct value). Nothing at this layer
can detect or prevent that; restore invariant culture before reading parsed
values.

## Prerequisites

- **Python 3.10 – 3.13** (`requires-python = ">=3.10,<3.14"`)
- **[.NET 8.0+ runtime](https://dotnet.microsoft.com/download/dotnet/8.0)**

### macOS / Homebrew

If you installed `dotnet` via Homebrew, the runtime layout differs from
Microsoft's installer (`libhostfxr.dylib` lives under `libexec/`, not `bin/`).
The bridge auto-detects this. If detection fails, set `DOTNET_ROOT` explicitly:

```bash
export DOTNET_ROOT=/opt/homebrew/opt/dotnet/libexec   # Apple Silicon
export DOTNET_ROOT=/usr/local/opt/dotnet/libexec      # Intel
```

## Installation

```bash
pip install kustology           # tier 1: thin .NET wrapper
pip install 'kustology[ir]'     # tier 1 + tier 2: semantic IR (adds pydantic)
```

## Quick start

```python
from kustology import parse, format_query

query = (
    "StormEvents | where StartTime > ago(7d) and DeathsDirect > 0 "
    "| project StartTime, State, EventType"
)

print(format_query(query))

result = parse(query)
print(result.get_referenced_tables())          # {'StormEvents'}
print(result.get_referenced_columns())         # {'StartTime', 'DeathsDirect', 'State', 'EventType'}
print(result.get_referenced_functions())       # {'ago'}
print(result.get_structural_hash()[:16])

# Semantic binding via a schema enables column-aware analysis:
schema = {"StormEvents": {"StartTime": "datetime", "DeathsDirect": "int", "State": "string", "EventType": "string"}}
bound = parse(query, schema=schema)
assert bound.has_semantics
print(bound.diagnostics)                       # [] — validate()'s dicts, read
                                               # off the parse you already have
```

With the `[ir]` extra installed, the same `KustoQuery` builds a Pydantic IR:

```python
from kustology import parse
from kustology.ir import FilterOp

schema = {"StormEvents": {"DeathsDirect": "int", "State": "string", "EventType": "string"}}
ir = parse("StormEvents | where DeathsDirect > 0", schema=schema).to_ir()
# A bound parse already carries Microsoft's result_schema and column
# result_type -- the builder stamps both at construction, independent of
# attach_schema. attach_schema instead controls table provenance
# (ColumnRef.table, schema_attached): False skips it, {...} rebinds
# against a schema after the fact -- output schemas, types and IR shape
# then match having parsed with schema= exactly. Diagnostics do not:
# they still follow this call's own receiver, so an unbound receiver
# stays lenient about unknown names where a bound one would not.

for op in ir.main_pipeline.operators:
    if isinstance(op, FilterOp):
        print(op.predicate.canonical_form)     # StormEvents.DeathsDirect > 0
        print(op.predicate.left.table)         # StormEvents
        print(op.predicate.left.result_type)   # int  (KustoType.INT)
```

## Runnable examples

Everything above, as scripts you can run rather than snippets you have to
assemble. Each one runs standalone (`python examples/walk_tree.py`) and every
one is executed by `tests/test_examples.py`, so none of them can drift away
from the library without CI noticing.

| | |
| --- | --- |
| [`examples/walk_tree.py`](examples/walk_tree.py) | Direct AST traversal via `KustoQuery.syntax` |
| [`examples/query_analysis.py`](examples/query_analysis.py) | End-to-end analysis of a non-trivial query |
| [`examples/binding_comparison.py`](examples/binding_comparison.py) | What a schema adds: `parse(query, schema=…)` side by side with an unbound parse |
| [`examples/walk_ir.py`](examples/walk_ir.py) | The same walk over the typed IR, on a bound parse |
| [`examples/find_all_demo.py`](examples/find_all_demo.py) | Generic IR traversal with `find_all` |
| [`examples/analyzer_demo.py`](examples/analyzer_demo.py) | Composing analyzers and consuming `Finding`s |
| [`examples/linter.py`](examples/linter.py) | A working KQL linter in ~100 lines, built on the IR |
| [`examples/llm_view.py`](examples/llm_view.py) | LLM-friendly IR serialization via `to_llm_dict` |
| [`examples/semantic_hash_demo.py`](examples/semantic_hash_demo.py) | What `semantic_hash` merges, what it splits, and where it lies — every verdict computed at run time, including the known collisions listed under [Versioning and stability](#versioning-and-stability) |

The IR ones (`walk_ir`, `find_all_demo`, `analyzer_demo`, `linter`,
`llm_view`, `semantic_hash_demo`) need the `[ir]` extra; the rest run on the
base install.

## CLI

The `kustology` console script ships with the base install:

```bash
kustology version                              # print package version
kustology format query.kql                     # reformat to canonical form

kustology validate query.kql                   # print parser diagnostics
kustology validate --json query.kql            # diagnostics as JSON
kustology validate --schema s.json query.kql   # bind first: semantic diagnostics too
kustology validate --schema s.json \
                   --ignore-unknown-tables query.kql   # waive KS204 only

kustology parse query.kql                      # print the .NET AST
kustology parse --json query.kql               # the AST as JSON
kustology parse --ir query.kql                 # print the Pydantic IR (needs [ir])
kustology parse --ir --json query.kql          # the IR as JSON, in an envelope
kustology parse --ir --schema s.json query.kql # enriched IR: types + provenance
```

A `--schema` file is JSON in the same `{"Table": {"column": "type"}}` shape
`parse(query, schema=...)` takes. On `parse` it binds the parse, and `to_ir()`
auto-attaches on a bound parse, so `parse --ir --schema` emits column types,
table provenance and `"schema_attached": true` rather than a skeleton.
`--ast` accepts it too, though binding does not change the syntax tree.

`parse --ir --json` emits a versioned envelope, not a bare dump — both tags
are your compatibility contract, and a stored payload naming neither cannot
be checked against the IR shape that produced it:

```json
{
  "ir_schema_version": "0.2",
  "semantic_hash_scheme": "kustology-sem-v2",
  "ir": { "kind": "query", "...": "..." }
}
```

`format`, `validate` and `parse` read from stdin when `file` is `-` or
omitted (`version` takes no file), and input is capped at 10 MB
(`KUSTOLOGY_MAX_INPUT_BYTES` overrides, counted in bytes).

**Exit codes.** `0` success; `1` the input had Error-severity diagnostics, or
the command failed at runtime; `2` the *invocation* was wrong — bad flags, a
file that cannot be read, a `--schema` that is not JSON, input over the byte
cap, or `parse --ir` without the `[ir]` extra. The line between 1 and 2 is
"your query is wrong" against "your command is wrong", which is what a CI job
branches on: an unreadable path says nothing about the KQL. `format` and
`parse` both run the validator before they emit anything, so neither writes
output derived from a query the parser rejected — the diagnostics go to
stderr and stdout stays empty.

A broken pipe is neither. `kustology validate q.kql | head` still exits `1`
on a query that fails validation: each subcommand decides its code before it
writes, so a reader hanging up stops the output and nothing else. Only a pipe
that breaks before any code was decided exits `0`, and either way the
interpreter's shutdown flush is silenced, so no `Exception ignored … Broken
pipe` follows the command that already returned.

## Versioning and stability

This is a `0.y` release — per SemVer §4, the public API is not yet stable.
Tier 1 is on a stabilization track and the package reaches 1.0 once it
survives external use without correctness breaks; Tier 2 is expected to keep
evolving at minor cadence.

Three numbers describe compatibility, and they are deliberately independent:

| | What it tags | When it moves |
| --- | --- | --- |
| `kustology.__version__` | the library | SemVer; pre-1.0, either tier may break at a minor |
| `kustology.ir.IR_SCHEMA_VERSION` | the IR's *field shape* | any breaking field-shape change |
| `kustology.ir.SEMANTIC_HASH_SCHEME` | the `semantic_hash` *canonicalization rules* | in lockstep with the above |

Both IR tags move **once per release**, not once per change — so they mark
what a consumer can observe, not the project's internal history.

Tag stored IR JSON with `IR_SCHEMA_VERSION` and refuse a payload whose tag you
do not recognise: every IR model sets `extra="forbid"`, so a dump from an
older release fails to load rather than silently deserializing into a shape
that no longer matches. IR JSON written before 0.2.0 does not load into 0.2.0.

`semantic_hash` carries its scheme as a prefix (`kustology-sem-v2:…`) for the
same reason — a stored hash from a different scheme will not collide with a
freshly computed one, instead of comparing unequal with no signal that the
rules moved. **Note for anyone deduplicating queries by stored hash:** as of
`kustology-sem-v2` the hash distinguishes `in` / `in~` / `has_any` /
`has_all` and `isnotnull` / `isnotempty`, which it did not before. Rehash from
source rather than comparing across schemes.

### What `semantic_hash` deliberately ignores

The digest is meant to survive differences that do not change what a query
returns. Within `kustology-sem-v2` these are ignored:

- **Operand order in commutative positions.** `where A and B` and
  `where B and A` are one digest, as are `in ("x", "y")` and `in ("y", "x")`.
  Consecutive `| where`s merge into one `and` first, so
  `| where A | where B` joins them — but only *consecutive* ones, so
  `| where A | take 5` and `| take 5 | where A` still differ.
- **`let` names.** Each is replaced by its declaration index, so
  `let n = 5; T | where a > n` and `let m = 5; T | where a > m` collide.
  Which binding a reference points at is still hashed, and a `let`-bound `n`
  never collides with a real column `n`.
- **The host's timezone and locale.** A `datetime` literal is normalized to
  UTC before it is hashed, and numeric literals render invariant, so the same
  query digests identically in Tokyo and in New York, under `de-DE` and
  under `C`.
- **Everything the binder supplied.** Column types, table provenance and
  result schemas are stripped, so passing a schema does not move the digest.
  Source offsets and `hint.*` go the same way — a hint changes how the engine
  executes a query, not the rows it returns.

Your own IR keeps every one of these as written; the canonicalization runs on
a private copy. `normalize_expressions` is a separate, opt-in transform and
still leaves your operand order alone.

Two caveats, both deliberate rather than accidental:

- The digest is **not** invariant across bind state for a query whose `let`
  aliases a table. The binder proves it is a table, which changes the IR's
  *shape* rather than a field's value, and no amount of stripping hides
  that. The alternative is treating every bare name as a table without
  proof.
- Equal digests are **not** a proof of equivalence. The known gaps below all
  remain in 0.2.0, and if you deduplicate a rule library by hash every one of
  them merges queries you may not want merged.

  **Four operators still discard a modifier** that changes what a query
  returns: `mv-apply`'s `to typeof(…)`, `limit` and `with_itemindex=`;
  `parse-kv`'s `with (…)` properties; `getschema kind=csl`; and `consume
  decodeblocks=`. That list is what a modifier-pair sweep turned up, not a
  proof that nothing else remains.

  **A `let` function's body is invisible to the digest.** `LetFunction`
  records the parameter names and a volatile `body_span`, so two functions
  with the same name and parameters and completely different bodies collide —
  and so do two that differ only in a parameter's declared type or default,
  neither of which is recorded. Parameter names and their count do split. This
  is the same boundary described under [Where Tier 2
  stops](#where-tier-2-stops); it is the largest of these gaps, because what
  collides is an arbitrary amount of query rather than one modifier.

  **Statement-level constructs other than `let` are dropped entirely** and
  hash as if they were absent — `set`, `declare query_parameters`,
  `declare pattern`, `alias database` and `restrict access to`. Two of those
  change results: `set query_now=datetime(2020-01-01); T | take 1` shares a
  digest with a bare `T | take 1` *and* with the same query pinned to a
  different `query_now`, and two `declare query_parameters` defaults differing
  only in their value collide. Nothing in the IR records that a statement was
  there, so this is invisible rather than merely lossy. All five kinds are
  tracked as unhandled in `tests/fixtures/syntax_kinds_baseline.json`.

  See the CHANGELOG's `[0.2.0]` **Fixed** section for the collisions that
  *were* closed.

## Development

```bash
git clone https://github.com/k4otix/kustology.git
cd kustology
pip install -e ".[dev]"

pytest
ruff check src tests scripts examples
mypy src
```

See [CONTRIBUTING.md](https://github.com/k4otix/kustology/blob/main/CONTRIBUTING.md) for the full workflow.

## License

Apache License 2.0. See [LICENSE](https://github.com/k4otix/kustology/blob/main/LICENSE), [NOTICE.md](https://github.com/k4otix/kustology/blob/main/NOTICE.md), and
[THIRD-PARTY-NOTICES.md](https://github.com/k4otix/kustology/blob/main/THIRD-PARTY-NOTICES.md). The bundled
`Kusto.Language.dll` is owned by Microsoft Corporation and redistributed
unmodified under Apache 2.0; it is pinned by SHA-256 and verified in CI —
see [SECURITY.md](https://github.com/k4otix/kustology/blob/main/SECURITY.md).

## Trademark notice

"Kusto", "KQL", "Microsoft", "Azure Data Explorer", "Azure Monitor", and
"Microsoft Sentinel" are trademarks of Microsoft Corporation. References to
those trademarks are nominative and used only to identify the upstream library
this package wraps. Apache License 2.0 §6 does not grant trademark rights;
nothing in this distribution should be construed as a trademark license.
