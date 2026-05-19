# Kustology

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![.NET 8.0+](https://img.shields.io/badge/.NET-8.0+-purple.svg)](https://dotnet.microsoft.com/download/dotnet/8.0)

Kustology is a two-tier Python library for Microsoft's KQL parser
(the `Microsoft.Azure.Kusto.Language` NuGet package), bridged via `pythonnet`.
It exposes the same parser used by Azure Data Explorer, Azure Monitor, and
Microsoft Sentinel.

> **Not affiliated with Microsoft.** This is an independent open-source project
> that wraps Microsoft's publicly distributed Apache 2.0–licensed library.

## Two tiers

| Tier | Install | Adds |
|---|---|---|
| 1 — thin .NET wrapper | `pip install kustology` | `KustoCode`, `KustoQuery`, `parse`, `format_query`, `validate`. Full Microsoft API surface via `kustology.bridge.*`. |
| 2 — semantic IR | `pip install 'kustology[ir]'` | An **intermediate representation** (IR) of the parsed query as pydantic models — typed operators, expressions, and source spans — plus `SchemaAttacher` for propagating Microsoft's binding results through the pipeline into serializable fields. |

The two tiers compose: `KustoQuery.to_ir()` builds the IR from the same parsed
AST tier 1 produced — there is no double parse.

```python
from kustology import parse

q = parse("StormEvents | where EventType == 'Tornado' | summarize count() by State")
ir = q.to_ir()                # tier 2: pydantic IR
ops = q.get_operator_chain()  # tier 1: Microsoft AST walk
```

## What's the IR?

The **IR** (intermediate representation) is a higher-level model of a
parsed KQL query. Tier 1 exposes Microsoft's native syntax tree — the
same tree used internally by the parser. Tier 2 reshapes that tree
into pydantic models: a `Pipeline` of typed operators (`FilterOp`,
`JoinOp`, `SummarizeOp`, …), typed expressions (`BinOp`, `FuncCall`,
`SetMembership`, …), and source spans.

What the IR adds on top of the syntax tree:

- **Typed.** Every node is a pydantic model, so analyzers dispatch by
  `isinstance(op, FilterOp)` rather than by string-matching node kinds.
- **Serializable.** `model_dump_json` round-trips losslessly through
  `QueryIR.model_validate_json`; nothing crosses process boundaries by
  reference. `to_llm_dict` produces a trimmed projection (~50% smaller)
  tailored for handing to a language model.
- **Materialized binding.** `SchemaAttacher(schemas)` walks the pipeline
  and writes resolved column types and owning-table provenance into
  pydantic fields, plus a `Pipeline.result_schema` capturing the final
  column shape — so binding survives serialization and is available
  without re-querying the AST.
- **Explicit coverage tracking.** Shapes the IR doesn't model yet
  surface as `UnknownExpr` / `UnknownSource`. A coverage audit fails CI
  when those grow beyond a baseline, so the IR's coverage of Microsoft's
  grammar is measurable over time.

## Choosing a tier

Both tiers share the same parser; pick based on what shape of data your
code wants to work with.

| | Tier 1 — thin wrapper | Tier 2 — semantic IR |
|---|---|---|
| **Install** | `pip install kustology` | `pip install 'kustology[ir]'` |
| **Dependencies** | `pythonnet` + .NET 8 runtime | adds `pydantic` |
| **Returns** | `KustoQuery` wrapping Microsoft's syntax tree | `QueryIR` — pydantic models |
| **Traversal** | Microsoft AST (`node.Kind` dispatch via `pythonnet`) | Typed pipeline (`isinstance` dispatch) |
| **Serialization** | `KustoQuery.to_dict()` / `to_json()` | `model_dump_json` (lossless) + `to_llm_dict` (LLM-tailored) |
| **Schema binding** | `parse(query, schema=...)` runs Microsoft's binder — semantic diagnostics plus symbol resolution accessible via AST methods | `SchemaAttacher` materializes those binding results into pydantic fields and computes `Pipeline.result_schema` |
| **Stability** | SemVer-stable | Pre-1.0; minor-version breaking changes documented in CHANGELOG |
| **Best for** | Formatting / linting, IDE integrations, extracting referenced tables/columns/operators, surgical table renames | Lineage and anti-pattern analyzers, JSON-serializable query representations for APIs and UIs, schema-aware column flow, LLM-fed query graphs |

## Stability

**Tier 1** — the `kustology` top-level surface (`parse`,
`format_query`, `validate`, `KustoQuery`, the reflection helpers, the
`kustology` CLI) follows [Semantic Versioning](https://semver.org/).
Public signatures, default values, CLI flags and exit codes, and the
JSON output shape of `kustology parse --json` / `kustology validate --json`
are all part of the stable contract.

**Tier 2** — `kustology.ir.*` is pre-1.0. The IR shape (node
field names and types, the `Pipeline.source` union, binder enrichment
fields, recognized `SyntaxKind` dispatch entries) may change at minor
versions until it survives one Kusto.Language.dll upgrade cycle without
breakage. Each break is named in `CHANGELOG.md` with a migration path.

## Capabilities

### Tier 1 — thin wrapper (always available)

- **Parse and format** KQL via Microsoft's public `KustoCodeService` API.
- **Validate** queries with structured diagnostics (severity, code, offset).
- **Semantic binding** when called with a schema — resolves columns and tables
  through symbols. Falls back to a fast syntactic walk otherwise.
- **AST analyzers**: referenced tables (including `database()`/`cluster()`
  cross-cluster refs, joins, unions, lookups, facets), referenced columns,
  operator chain, operator counts, structural hash, time-range expressions.
- **Surgical rewrites**: rename tables across every reference position.
- **JSON interop**: export the AST to a Python dict or JSON.
- **Runtime reflection** (`kustology.reflection`): self-updating lists
  of KQL functions — `time_functions()`, `aggregate_functions()`,
  `string_functions()`, `scalar_functions()` for categorized listings,
  `all_function_names()` for the full set, plus `syntax_kinds()` for the
  SyntaxKind enum.

### Tier 2 — semantic IR

- **`QueryIR`** — pydantic root model holding a `Pipeline` of typed operators
  (`FilterOp`, `SummarizeOp`, `JoinOp`, `LookupOp`, …) and typed expressions
  (`BinOp`, `FuncCall`, `SetMembership`, …).
- **`IRBuilder.build(query)`** — parse, bind, and build the IR in one call.
- **`IRBuilder.build_from_code(code)`** — build from a pre-parsed `KustoCode`.
  `KustoQuery.to_ir()` uses this so callers don't pay for two parses.
- **`SchemaAttacher(schemas)`** — propagates Microsoft's binding results
  into pydantic fields (`Expr.result_type`, `ColumnRef.table`) and computes
  `Pipeline.result_schema` for the post-pipeline column shape. Takes a flat
  `{table: {col: type}}` dict.
- **`QueryIR.to_llm_dict()`** — lossy projection optimized for handing the
  IR to a language model: every node carries a `kind` discriminator, spans
  and defaulted fields are stripped, and `polarity` is collapsed into
  natural KQL operators (`!=`, `!in`, `!between`). Roughly 50% smaller than
  `model_dump_json()` on typical queries. See `examples/llm_view.py`.
- **`UnknownExpr` / `UnknownSource`** — explicit fallback nodes for shapes
  the builder doesn't model. The coverage audit (`scripts/audit_syntax_kinds.py`)
  fails CI when new shapes appear after a Kusto.Language DLL upgrade.

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
from kustology import parse, format_query, validate

query = "SecurityEvent | where EventID == 4624 | project TimeGenerated, Account"

print(format_query(query))

result = parse(query)
print(result.get_referenced_tables())          # {'SecurityEvent'}
print(result.get_referenced_columns())         # {'EventID', 'TimeGenerated', 'Account'}
print(result.get_structural_hash()[:16])

# Semantic binding via a schema enables column-aware analysis:
schema = {"SecurityEvent": {"EventID": "long", "TimeGenerated": "datetime", "Account": "string"}}
bound = parse(query, schema=schema)
assert bound.has_semantics
```

With the `[ir]` extra installed, the same `KustoQuery` builds a pydantic IR:

```python
from kustology import parse
from kustology.ir import FilterOp, SchemaAttacher

schema = {"SecurityEvent": {"EventID": "long", "TimeGenerated": "datetime", "Account": "string"}}
ir = parse("SecurityEvent | where EventID == 4624", schema=schema).to_ir()

SchemaAttacher(schema).enrich(ir)              # fills column types + table provenance

for op in ir.main_pipeline.operators:
    if isinstance(op, FilterOp):
        print(op.predicate.canonical_form)     # EventID == 4624
        print(op.predicate.left.table)         # SecurityEvent
        print(op.predicate.left.result_type)   # KustoType.LONG ('long')
```

## CLI

The `kustology` console script ships with the base install:

```bash
kustology version                          # print package version
kustology format query.kql                 # reformat to canonical form
kustology validate query.kql               # print parser diagnostics
kustology validate --json query.kql        # diagnostics as JSON
kustology parse query.kql                  # print the .NET AST
kustology parse --ir query.kql             # print the pydantic IR (requires [ir])
kustology parse --ir --json query.kql      # serializable IR
```

All subcommands also read from stdin when `file` is `-` or omitted. Exit codes:
`0` success, `1` input had Error-severity diagnostics or a runtime failure,
`2` usage error (bad flags, missing file, or missing `[ir]` extras for
`parse --ir`).

## Architecture

1. **Bridge** (`bridge.py`): initializes the .NET CLR via `pythonnet` with
   cross-platform `DOTNET_ROOT` auto-detection, exposing only Microsoft's
   public API surface (`KustoCode`, `KustoCodeService`, `GlobalState`,
   `TableSymbol`, …).
2. **Engine**: the unmodified Microsoft parser DLL — see
   [Verifying the bundled DLL](#verifying-the-bundled-dll).
3. **Tier-1 Python layer**: `services.py`, `core.py`, `utils/analysis.py` —
   `parse`/`format_query`/`validate` entry points and the `KustoQuery`
   analyzer. AST walks; no pydantic dependency.
4. **Tier-2 IR layer** (`kustology.ir`, optional): pydantic `QueryIR`,
   `IRBuilder`, and `SchemaAttacher`. Activated by
   `pip install 'kustology[ir]'`.

## Verifying the bundled DLL

Every release pins the bundled DLL to a specific NuGet package version with a
SHA-256 hash. The pin lives in:

- `src/kustology/bin/VERSION.txt` — package, version, sha256, refresh
  timestamp.
- `pyproject.toml` under `[tool.kustology] kusto_language_version`.

### Offline hash check

```bash
shasum -a 256 src/kustology/bin/Kusto.Language.dll
cat src/kustology/bin/VERSION.txt
# The two sha256 values must match.
```

### Re-fetch from nuget.org and compare

`scripts/verify_dll.py` is pure Python (no `dotnet` required). It downloads
the pinned `Microsoft.Azure.Kusto.Language` package from nuget.org, hashes
every `Kusto.Language.dll` inside, and confirms one is byte-identical to the
bundled file.

```bash
python scripts/verify_dll.py
```

This is a **trust-on-first-use** model: it confirms the bundled DLL matches
what nuget.org currently serves over TLS. It does not validate NuGet's
signed-package signature. If your policy requires signature validation, run
`nuget verify -All <path-to-nupkg>` against a freshly fetched package.

### Refresh the bundled DLL

If you'd rather use a DLL you fetched yourself:

```bash
python scripts/refresh_dll.py            # uses the pinned version
python scripts/refresh_dll.py --version 12.3.2 --pin
```

This requires `dotnet` 8.0+, runs `dotnet publish` against a temporary
`.csproj`, copies the resulting `Kusto.Language.dll` into `bin/`, and updates
`VERSION.txt`.

## Development

```bash
git clone https://github.com/k4otix/kustology.git
cd kustology
pip install -e ".[dev]"

pytest
ruff check src tests scripts
mypy src
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## License

Apache License 2.0. See [LICENSE](LICENSE), [NOTICE.md](NOTICE.md), and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The bundled
`Kusto.Language.dll` is owned by Microsoft Corporation and redistributed
unmodified under Apache 2.0.

## Trademark notice

"Kusto", "KQL", "Microsoft", "Azure Data Explorer", "Azure Monitor", and
"Microsoft Sentinel" are trademarks of Microsoft Corporation. References to
those trademarks are nominative and used only to identify the upstream library
this package wraps. Apache License 2.0 §6 does not grant trademark rights;
nothing in this distribution should be construed as a trademark license.
