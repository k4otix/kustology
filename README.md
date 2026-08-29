# Kustology

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![.NET 8.0+](https://img.shields.io/badge/.NET-8.0+-purple.svg)](https://dotnet.microsoft.com/download/dotnet/8.0)

> **Not affiliated with Microsoft.** This is an independent open-source project
> that wraps Microsoft's publicly distributed Apache 2.0–licensed library.

Kustology gives Python programs Microsoft's own KQL parser, the one behind
Azure Data Explorer, Azure Monitor, and Microsoft Sentinel. It loads the
official `Kusto.Language` library through `pythonnet`, so queries parse exactly
as the service parses them.

The library comes in two tiers. Either one works on its own.

## The two tiers

### Tier 1: thin wrapper

Tier 1 exposes Microsoft's parser, formatter, and validator, and adds analyzers
for the questions a KQL author asks most: which tables a query reads, which
columns it touches, which operators it chains, where its time filters sit, and
how to rename a table everywhere it appears. You work with Microsoft's syntax
tree directly.

### Tier 2: semantic IR

Tier 2 builds a typed Pydantic model of the parsed query, with classes like
`FilterOp`, `BinOp`, and `ColumnRef`. It suits the questions an analyzer asks:
which source table a column came from after joins, renames, and `let` aliases;
what schema the pipeline produces; whether two queries mean the same thing
despite formatting; and how to serialize the whole graph for a UI, a service,
or a language model.

## Choosing a tier

Both tiers use the same parser. Pick based on the shape of data your code wants.

| | Tier 1 — thin wrapper | Tier 2 — semantic IR |
|---|---|---|
| **Install** | `pip install kustology` | `pip install 'kustology[ir]'` |
| **Dependencies** | `pythonnet` + .NET 8 runtime | adds `pydantic` |
| **Returns** | `KustoQuery` wrapping Microsoft's syntax tree | `QueryIR` — Pydantic models |
| **Traversal** | Microsoft AST, dispatching on `node.Kind` | Typed pipeline, dispatching on `isinstance` |
| **Serialization** | `to_dict()` / `to_json()` | `model_dump_json()`, which round-trips, plus `to_llm_dict()` for language models |
| **Schema binding** | `parse(query, schema=...)` runs Microsoft's binder for semantic diagnostics and symbol resolution | `to_ir()` on a bound parse carries per-operator schemas and column types; without a schema it still types literals and built-in calls |
| **Best for** | Formatting, linting, IDE integrations, extracting tables and columns, surgical table renames | Lineage and anti-pattern analyzers, JSON for APIs and UIs, schema-aware column flow, query graphs for language models |

## Prerequisites

- **Python 3.10 or later**
- **[.NET 8.0+ runtime](https://dotnet.microsoft.com/download/dotnet/8.0)**

If you installed `dotnet` through Homebrew, its runtime layout differs from
Microsoft's installer and `libhostfxr.dylib` sits under `libexec/` instead of
`bin/`. Kustology detects this. If detection fails, set `DOTNET_ROOT` yourself:

```bash
export DOTNET_ROOT=/opt/homebrew/opt/dotnet/libexec   # Apple Silicon
export DOTNET_ROOT=/usr/local/opt/dotnet/libexec      # Intel
```

## Installation

```bash
pip install kustology             # Tier 1: thin .NET wrapper
pip install 'kustology[ir]'       # Tier 1 + Tier 2: semantic IR (adds pydantic)
pip install 'kustology[examples]' # Colour output in examples/ (adds rich)
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
result.get_referenced_tables()      # {'StormEvents'}
result.get_referenced_columns()     # {'StartTime', 'DeathsDirect', 'State', 'EventType'}
result.get_referenced_functions()   # {'ago'}

# Pass a schema to bind the query and get semantic diagnostics.
schema = {"StormEvents": {"StartTime": "datetime", "DeathsDirect": "int",
                          "State": "string", "EventType": "string"}}
bound = parse(query, schema=schema)
bound.has_semantics                 # True
bound.diagnostics                   # []
```

With the `[ir]` extra installed, the same parse builds a Pydantic IR:

```python
from kustology import parse
from kustology.ir import FilterOp

schema = {"StormEvents": {"DeathsDirect": "int", "State": "string"}}
ir = parse("StormEvents | where DeathsDirect > 0", schema=schema).to_ir()

for op in ir.main_pipeline.operators:
    if isinstance(op, FilterOp):
        op.predicate.canonical_form   # StormEvents.DeathsDirect > 0
        op.predicate.left.table       # StormEvents
        op.predicate.left.result_type # int  (KustoType.INT)
```

## CLI

The `kustology` console script ships with the base install:

```bash
kustology format query.kql        # reformat to canonical form
kustology validate query.kql      # print parser diagnostics
kustology parse --ir query.kql    # print the Pydantic IR (needs [ir])
```

Every subcommand, the `--schema` and `--json` flags, and the exit codes CI
branches on are in the
[CLI reference](https://github.com/k4otix/kustology/blob/main/docs/cli.md).

## Examples

Each script runs standalone and explains its own output as it goes, and
`tests/test_examples.py` runs all of them, so none can drift away from the
library without CI noticing.

| | |
| --- | --- |
| [examples/walk_tree.py](https://github.com/k4otix/kustology/blob/main/examples/walk_tree.py) | Direct AST traversal |
| [examples/query_analysis.py](https://github.com/k4otix/kustology/blob/main/examples/query_analysis.py) | End-to-end analysis of a non-trivial query |
| [examples/binding_comparison.py](https://github.com/k4otix/kustology/blob/main/examples/binding_comparison.py) | What passing a schema adds |
| [examples/walk_ir.py](https://github.com/k4otix/kustology/blob/main/examples/walk_ir.py) | The same walk over the typed IR |
| [examples/find_all_demo.py](https://github.com/k4otix/kustology/blob/main/examples/find_all_demo.py) | Generic IR traversal with `find_all` |
| [examples/analyzer_demo.py](https://github.com/k4otix/kustology/blob/main/examples/analyzer_demo.py) | Composing analyzers and reading `Finding`s |
| [examples/linter.py](https://github.com/k4otix/kustology/blob/main/examples/linter.py) | A working KQL linter built on the IR |
| [examples/llm_view.py](https://github.com/k4otix/kustology/blob/main/examples/llm_view.py) | IR serialization for language models |
| [examples/semantic_hash_demo.py](https://github.com/k4otix/kustology/blob/main/examples/semantic_hash_demo.py) | What `semantic_hash` merges and what it splits |

The IR examples need the `[ir]` extra. The rest run on the base install. Add
the `[examples]` extra for colour and syntax highlighting; without it the same
output prints as plain text.

## Documentation

| | |
| --- | --- |
| [Working with the syntax tree](https://github.com/k4otix/kustology/blob/main/docs/tier1-syntax-tree.md) | Tier 1 in depth, and the pythonnet behavior that catches people off guard |
| [The Tier 2 IR](https://github.com/k4otix/kustology/blob/main/docs/tier2-ir.md) | How `let` names, operators, and function bodies lower into IR nodes |
| [CLI reference](https://github.com/k4otix/kustology/blob/main/docs/cli.md) | Subcommands, flags, JSON output, exit codes |
| [Versioning and `semantic_hash`](https://github.com/k4otix/kustology/blob/main/docs/semantic-hash.md) | Compatibility tags, and what the digest ignores |
| [Architecture](https://github.com/k4otix/kustology/blob/main/ARCHITECTURE.md) | Code layout, for contributors |
| [Contributing](https://github.com/k4otix/kustology/blob/main/CONTRIBUTING.md) | Setup and the development loop |

## Versioning

This is a `0.y` release, so the public API can still change at a minor version
(SemVer §4). Tier 1 is on a stabilization track and reaches 1.0 once it survives
external use without correctness breaks. Tier 2 keeps evolving at minor cadence.

Three numbers describe compatibility: `kustology.__version__` tags the library,
`kustology.ir.IR_SCHEMA_VERSION` tags the IR's field shape, and
`kustology.ir.SEMANTIC_HASH_SCHEME` tags the hash canonicalization rules. Tag
any IR JSON or hash you store with the value that produced it, and refuse a
payload whose tag you do not recognize. See
[Versioning and `semantic_hash`](https://github.com/k4otix/kustology/blob/main/docs/semantic-hash.md)
for the full contract.

## Development

```bash
git clone https://github.com/k4otix/kustology.git
cd kustology
pip install -e ".[dev]"

pytest
ruff check src tests scripts examples
mypy src
```

See [CONTRIBUTING.md](https://github.com/k4otix/kustology/blob/main/CONTRIBUTING.md)
for the full workflow.

## License

Apache License 2.0. See
[LICENSE](https://github.com/k4otix/kustology/blob/main/LICENSE),
[NOTICE.md](https://github.com/k4otix/kustology/blob/main/NOTICE.md), and
[THIRD-PARTY-NOTICES.md](https://github.com/k4otix/kustology/blob/main/THIRD-PARTY-NOTICES.md).
The bundled `Kusto.Language.dll` is owned by Microsoft Corporation and
redistributed unmodified under Apache 2.0. It is pinned by SHA-256 and verified
in CI. See [SECURITY.md](https://github.com/k4otix/kustology/blob/main/SECURITY.md).

## Trademark notice

"Kusto", "KQL", "Microsoft", "Azure Data Explorer", "Azure Monitor", and
"Microsoft Sentinel" are trademarks of Microsoft Corporation. References to
those trademarks are nominative and used only to identify the upstream library
this package wraps. Apache License 2.0 §6 does not grant trademark rights;
nothing in this distribution should be construed as a trademark license.
