# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-14

First public release.

### Added — Tier 1 (SemVer-stable)

- `parse(query, schema=None)` and `KustoQuery` for syntactic + optional semantic analysis.
- `format_query(query)` for canonical reformatting.
- `validate(query, schema=None, ignore_unknown_tables=False)` for structured parser diagnostics.
- `kustology` CLI with subcommands `version`, `format`, `validate`, `parse`.
- Reflection helpers: `time_functions()`, `aggregate_functions()`, `scalar_functions()`,
  `string_functions()`, `all_function_names()`, `syntax_kinds()`.
- Bundled `Kusto.Language.dll` (12.3.2) pinned by SHA-256; refresh + verify scripts.
- `__version__` exposed at runtime via `importlib.metadata`.

### Added — Tier 2 (pre-1.0 IR, opt-in via `[ir]` extras)

- `kustology.ir.IRBuilder.build(query)` walks the .NET syntax tree and emits a
  pydantic `QueryIR` with typed operator and expression nodes.
- `SchemaAttacher` for schema-aware type enrichment.
- 52 operator node types and 23 expression node types covering the Kusto query
  surface seen in real-world Sentinel detections (200/200 sample validates).
- `ImplicitSource` source variant for sub-pipelines whose row context comes from
  parent operators (union-at-root, mv-apply / partition subqueries, join/lookup RHS).

### Infrastructure

- CI matrix: macOS × Linux × Windows × Python 3.10+ (tested against 3.10, 3.11, 3.12).
- Coverage audit (`scripts/audit_syntax_kinds.py`) fails CI on new uncovered `SyntaxKind`.
- Corpus regression (`scripts/verify_corpus.py`) against a 200-query Sentinel sample.
- DLL provenance verified on every push (`scripts/verify_dll.py`).
- SBOM generation (CycloneDX) as a CI artifact.
- `.pre-commit-config.yaml` mirroring CI for local fail-fast.

### Documentation

- `README.md` — quickstart, install, examples, stability policy.
- `ARCHITECTURE.md` — tier layout and contribution pointers.
- `CONTRIBUTING.md` — workflow and coding conventions.
- 5 runnable examples in `examples/` (walk_tree, query_analysis, linter, query_auditor,
  binding_comparison).
