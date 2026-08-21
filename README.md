# Kustology

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
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

`let`-bound names are modeled as `LetRef` rather than `TableRef`, and a bound
parse resolves columns through them — so in
`let Base = SecurityEvent | …; Base | project Account`, `Account` carries the
type from `SecurityEvent` and the provenance `Base`. `find_all(ir, TableRef)`
therefore answers "which tables does this query read"; use
`find_all(ir, LetRef)` for the aliases. An alias can shadow a real table name
(`let SecurityEvent = SecurityEvent | …` is a common Sentinel idiom), so
`ColumnRef.table` is a scope name rather than a guaranteed table name — read
`result_type` rather than re-deriving types from it, and see
`SchemaAttacher.enrich`'s docstring for telling the two apart when you must.

## Choosing a tier

Both tiers share the same parser; pick based on what shape of data your
code wants to work with.

| | Tier 1 — thin wrapper | Tier 2 — semantic IR |
|---|---|---|
| **Install** | `pip install kustology` | `pip install 'kustology[ir]'` |
| **Dependencies** | `pythonnet` + .NET 8 runtime | adds `pydantic` |
| **Returns** | `KustoQuery` wrapping Microsoft's syntax tree | `QueryIR` — Pydantic models |
| **Traversal** | Microsoft AST (`node.Kind` dispatch via `pythonnet`) | Typed pipeline (`isinstance` dispatch) |
| **Serialization** | `KustoQuery.to_dict()` / `to_json()` | `model_dump_json` (lossless) + `to_llm_dict` (LLM-tailored) |
| **Schema binding** | `parse(query, schema=...)` runs Microsoft's binder — semantic diagnostics plus symbol resolution accessible via AST methods | `SchemaAttacher` materializes those binding results into Pydantic fields and computes `Pipeline.result_schema` |
| **Best for** | Formatting / linting, IDE integrations, extracting referenced tables/columns/functions/operators, surgical table renames | Lineage and anti-pattern analyzers, JSON-serializable query representations for APIs and UIs, schema-aware column flow, LLM-fed query graphs |

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

**Symbols require a schema — there is no partial binding.** `parse(q)` calls
`KustoCode.Parse`, which does no semantic analysis: `has_semantics` is
`False` and every `ReferencedSymbol` is `None`, built-in functions included.
`parse(q, schema=...)` binds, and they all resolve. It is all-or-nothing.

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

- **Python 3.10+**
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
```

With the `[ir]` extra installed, the same `KustoQuery` builds a Pydantic IR:

```python
from kustology import parse
from kustology.ir import FilterOp

schema = {"StormEvents": {"DeathsDirect": "int", "State": "string", "EventType": "string"}}
ir = parse("StormEvents | where DeathsDirect > 0", schema=schema).to_ir()
# A bound parse auto-runs SchemaAttacher: column types and table provenance
# are populated. Pass attach_schema=False to skip, or attach_schema={...} to
# override the schema used for the attach pass.

for op in ir.main_pipeline.operators:
    if isinstance(op, FilterOp):
        print(op.predicate.canonical_form)     # StormEvents.DeathsDirect > 0
        print(op.predicate.left.table)         # StormEvents
        print(op.predicate.left.result_type)   # int  (KustoType.INT)
```

## CLI

The `kustology` console script ships with the base install:

```bash
kustology version                          # print package version
kustology format query.kql                 # reformat to canonical form
kustology validate query.kql               # print parser diagnostics
kustology validate --json query.kql        # diagnostics as JSON
kustology parse query.kql                  # print the .NET AST
kustology parse --ir query.kql             # print the Pydantic IR (requires [ir])
kustology parse --ir --json query.kql      # serializable IR
```

All subcommands also read from stdin when `file` is `-` or omitted. Exit codes:
`0` success, `1` input had Error-severity diagnostics or a runtime failure,
`2` usage error (bad flags, missing file, or missing `[ir]` extras for
`parse --ir`).

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

## Development

```bash
git clone https://github.com/k4otix/kustology.git
cd kustology
pip install -e ".[dev]"

pytest
ruff check src tests scripts examples
mypy src
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## License

Apache License 2.0. See [LICENSE](LICENSE), [NOTICE.md](NOTICE.md), and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The bundled
`Kusto.Language.dll` is owned by Microsoft Corporation and redistributed
unmodified under Apache 2.0; it is pinned by SHA-256 and verified in CI —
see [SECURITY.md](SECURITY.md).

## Trademark notice

"Kusto", "KQL", "Microsoft", "Azure Data Explorer", "Azure Monitor", and
"Microsoft Sentinel" are trademarks of Microsoft Corporation. References to
those trademarks are nominative and used only to identify the upstream library
this package wraps. Apache License 2.0 §6 does not grant trademark rights;
nothing in this distribution should be construed as a trademark license.
