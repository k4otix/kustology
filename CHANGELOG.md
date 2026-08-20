# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-08-20

### Fixed

- **Distinct KQL operators no longer collide in the IR (tier 2).** `in~`,
  `has_any` and `has_all` all produced a byte-identical `SetMembership` and an
  identical `semantic_hash`, and `isnotnull` / `isnotempty` an identical
  `Exists` — so `semantic_hash`'s documented contract that different operators
  do not collide was false, and a consumer deduplicating rules by hash silently
  merged rules meaning different things. `has_any` and `has_all` are opposites
  (OR vs AND of term matches); `in~` compares whole values rather than terms;
  `isnotempty` also rejects `""`. Both nodes now carry `op`, following the
  existing `BinOp` pattern, and `canonical_form` and the LLM view render it
  instead of re-deriving the operator from flags. The parser had always
  supplied the distinction — `in` / `!in` / `in~` / `!in~` share the class
  `InExpression` and differ only in `.Kind`, which the builder discarded by
  dispatching on the class name.
- **`BinOp.case_sensitive` is right for the whole operator family (tier 2).**
  It came from a hand-maintained allow-list of six operators, with everything
  absent falling through to `True` — so `hasprefix` and `hassuffix` were wrong
  before anyone negated anything, and `!has`, `!contains`, `!startswith`,
  `!endswith`, `!hasprefix` and `!hassuffix` were all reported backwards. KQL
  string operators fold case unless suffixed `_cs`, and negating a predicate
  does not change how it compares. Now derived from the operator's suffix, so
  an operator added by a DLL refresh no longer lands wrong by default.

- **Column provenance is resolved everywhere, not in 17 of 53 operators
  (tier 2).** `SchemaAttacher` recursed a hardcoded tuple of attribute names
  with no `pipeline`, `branches` or `default` entry, and dispatched on an
  `isinstance` chain that simply fell off the end for unhandled operators. So
  in `SecurityEvent | where EventID > toscalar(SecurityEvent | summarize
  max(EventID))`, `EventID` resolved to `SecurityEvent` outside the `toscalar`
  and to `None` inside — the same column, one query, inconsistent provenance,
  silent. `| sort by EventID` left it unresolved while a `| project` in the
  same query worked, and every `ColumnRef` inside a `case()` or `iif()` was
  unresolved. The traversal now derives from `model_fields`; operators without
  a bespoke scope rule fill their expressions and walk their sub-pipelines
  instead of being skipped. `SchemaAttacher`'s docstring names the operators
  whose downstream scope is still stale, so that boundary is stated rather
  than silent.
- **`walk()` / `find_all()` descend tuple-valued fields (tier 2).** They
  unwrapped lists and dicts but not tuples, so `CaseExpr.branches` — typed
  `list[tuple[Expr, Expr]]` — was invisible. A `case()` holding five
  `ColumnRef`s surfaced one. This is the traversal the docs point every
  analyzer at, and the one the bespoke walkers were converted to.
- **`let` bindings are enriched, and their names resolve (tier 2).**
  `SchemaAttacher.enrich` walked `main_pipeline` only, so a tabular binding's
  `rhs_pipeline.result_schema` stayed `None` on a fully bound parse and the
  `ColumnRef`s inside it kept `table=None`. Bindings are now walked in
  declaration order and each one's output columns registered under its name,
  so in `let Base = SecurityEvent | where EventID > 4624; Base | project
  Account`, `Account` gains the type `string` and the provenance `"Base"` —
  previously `unknown` and `None`.
- **`ExternalDataExpr` carries real data (tier 2).** `uri` was the hardcoded
  placeholder `"url"` because the guard read `node.Uris` and the member is
  `URIs`; `columns` was bound to `[]` and never appended to; `format` was
  hardcoded `"unknown"`. All three are read from the node. `format` is now
  `None` when the query states none, rather than a string that read like a
  real value.
- **`ParseKvOp.columns` and `MacroExpandOp.pipeline` are populated (tier 2).**
  Both probed .NET members that do not exist — `Keys.Count` where `Keys` is a
  `RowSchema` exposing `Columns`, and `Subquery`/`Body` where the member is
  `StatementList` — so both were empty for every query ever parsed.
- **`in` and `in~` are distinguishable (tier 2).** `SetMembership.case_sensitive`
  was hardcoded `False`, so KQL's case-sensitive `in` was indistinguishable
  from `in~`, `canonical_form` rendered `C in ("a")` as `C in~ ("a")`, and the
  two queries produced the **same** `semantic_hash` despite being different.
- **`canonical_form` renders every expression shape (tier 2).** It handled 12
  of 23 `Expr` types; the rest fell through to a bare `"?"`, so `-X > 1`,
  `D.a == 1` and `toscalar(...) > 1` were all the identical string `"? > 1"`.
- **`find in (...)` and `search in (...)` tables are found (tier 1).**
  `get_referenced_tables()` returned an empty set for `find in (S1, S2) where
  X == 1`, and `replace_table()` returned the query **unchanged**, with no
  error — so a consumer migrating a table shipped one still pointing at the
  old name.
- **The corpus gates see what they were built to see (tier 2).** Both gate
  walkers shared the blind spot above and additionally probed operator fields
  by `hasattr` from a fixed list, missing `SortOp.expressions`, `TopOp.by`,
  `RangeOp.start`/`end`/`step`, `FacetOp.with_pipeline` and others. Both now
  use `find_all`: over the bundled 33-query corpus that reaches 3661 nodes
  against the old walk's 3179, with nothing lost. A new assertion catches a
  regression of the parenthesized-`let` fix, which the `SubqueryExpr` work had
  made invisible to the gate.

- **Culture no longer corrupts fractional numeric literals (tier 1).**
  Importing `kustology` now pins .NET's culture to invariant, process-wide.
  Microsoft's parser evaluates `LiteralValue` lazily on property access, so
  the culture live in *caller* code decided the parsed value: under `de-DE`
  the decimal point was read as a group separator and the fractional part was
  swallowed. This was never limited to durations — `timespan` (`1.5h` → 15
  hours, `2.25s` → 3m45s), `real` (`1.5` → `15.0`, making
  `| where CpuPct > 1.5` ten times too strict) and `decimal`
  (`decimal(1.5)` → `15`) all corrupt identically; under `fr-FR` a duration
  parsed to zero. Integer literals were unaffected, which is why the previous
  suite passed green under `de-DE`. A `de-DE`/`fr-FR` CI matrix now guards it.
  No opt-out. **Residual risk:** the pin runs once, at import — a host that
  changes .NET's culture afterwards re-opens the corruption for every
  `LiteralValue` not yet read, and nothing at this layer can prevent that.
  See `bridge._pin_invariant_culture`.
- **`literal_kind` is read from the .NET node (tier 2).** It was re-inferred
  from the Python type of `LiteralValue`, so `real` reported as `int` and
  `datetime`, `timespan` and `guid` all reported as `string`.
- **`LiteralExpr.value` is culture-independent (tier 2).** Datetimes render
  as ISO 8601 round-trip and timespans in invariant constant form. The
  previous bare `ToString()` rendered through the ambient culture and reached
  `semantic_hash`, making the hash differ across machines for the same query.
- **`LetBinding` is populated (tier 2).** The builder set only `name`, `span`
  and a hardcoded `category="alias"`, leaving `rhs_expr`, `rhs_pipeline`,
  `inner_tables` and `inner_time_exprs` permanently empty. Every tabular
  right-hand-side shape is covered, including the two that a first pass
  missed: the parenthesized form `let X = ( T | where … );` — the dominant
  Microsoft Sentinel idiom, which arrives wrapped in a
  `ParenthesizedExpression` — and operator-rooted right-hand sides
  (`union`, `range`, `search`, `print`, `find`, `datatable`).
- **The corpus coverage gates walk `let` bindings (tier 2).** Both
  `tests/ir/test_complex_harness.py` and `scripts/mine_corpus.py` inspected
  only `QueryIR.main_pipeline`, so a gap reachable only through a `let`
  right-hand side reported green. That is why the unpopulated tabular `let`
  above survived review. Walking the bindings surfaced one further real gap,
  fixed below.
- **A bare tabular subquery in expression position is modeled (tier 2).**
  `| where User in ((Suspicious | project User))` collapsed the entire inner
  query into a single `UnknownExpr` blob of raw text. It now builds a
  `SubqueryExpr`, so the subtree is reachable by `walk` / `find_all`.

### Added

- **`LetRef` is emitted (tier 2).** It was exported, declared in the
  `Pipeline.source` union, and constructed nowhere — every source-position
  name became a `TableRef`, so `let X = T | take 1; X | count` reported `X` as
  a table. The classification is decidable from the `let` statements alone, so
  it holds identically for a bound and an unbound parse.
- **`Expr.result_type_inner` is populated (tier 2).** It read
  `res_type.Underlying`, a property on no type in the assembly. The real
  member is `ElementType` on `DynamicArraySymbol`, so
  `parse("print x = dynamic([1,2,3])", schema={}).to_ir()` now reports
  `result_type_inner=long`. Like every binder-derived annotation this needs
  an analyzed parse — bare `parse(q)` calls `KustoCode.Parse`, which never
  analyses, so `result_type` and `result_type_inner` are both unset there.
- `tests/test_reflection_audit.py` (tier 1) — asserts every PascalCase .NET
  member name passed to `getattr`/`hasattr` in `src/` exists in the loaded
  `Kusto.Language` assembly. Four defects in this release came from a probe
  naming a member that does not exist; pythonnet is case-sensitive and silent
  about the miss, so the guard declines and the field keeps its default.

- `iter_elements()` (tier 1) — unwraps the `SeparatedElement` wrappers that
  .NET list properties yield, and passes plain `SyntaxList` through unchanged.
- `LiteralExpr.ticks` (tier 2) — exact .NET ticks for `datetime` and
  `timespan` literals. `ticks // 10` converts to exact microseconds — down to
  `1microsecond`, but not below it: `2tick` is 2 ticks and `timedelta` cannot
  represent 200ns. This field is the only lossless form.
- `LetFunction` (tier 2) — parameter names and body span for `let`-declared
  functions. The body is not modeled.
- `SubqueryExpr` (tier 2) — a bare pipeline in expression position, i.e. the
  value set of a membership test. Mirrors `MaterializeExpr` / `ToScalarExpr`;
  carries the inner `Pipeline`.
- `literal_kind` gains `"decimal"`.
- README section documenting the syntax-tree traps.

### Changed

- `.NET` runtime discovery and the .NET-boundary member probes now log at
  `DEBUG` instead of failing silently. Enable `logging` for `kustology.bridge`
  to see which candidate `dotnet` roots were tried before the
  "Failed to initialize the .NET runtime" error is raised.
- `get_time_range()` is renamed `find_time_expressions()` on both the module
  and `KustoQuery`. The old names remain as `DeprecationWarning` aliases with
  identical behavior. The function returns a source-ordered discovery list —
  including bare `now()`, bare operands and `!between` operands — not a
  resolved range.

### Breaking (tier 2, pre-1.0)

- **`SetMembership` and `Exists` gain a required `op` field**, and
  `MaterializeExpr` is removed. Stored IR JSON written before this change fails
  to load under `extra="forbid"`.
- **`semantic_hash` changes for any query** using a membership operator,
  `isnotnull` / `isnotempty`, or one of the eight string operators whose
  case sensitivity was wrong. Across the bundled 33-query corpus, 22 hashes
  change and 11 do not — a clean partition matching exactly which files use an
  affected operator.
- **`MaterializeExpr` is removed** — proven unreachable by three independent
  methods (grammar sweep, 1,808 parse mutations, an instrumented builder over
  1,091 queries). `materialize` is a keyword the parser admits only as a `let`
  right-hand side, where it becomes a nested `Pipeline`. Unusually safe for a
  breaking change: since none was ever produced, no stored IR can contain one.
  See `docs/superpowers/reports/2026-08-20-materialize-reachability.md`.

- **`LetRef` replaces `TableRef` for `let`-bound names.**
  `find_all(ir, TableRef)` no longer returns aliases, and
  `LetBinding.inner_tables` narrows to real tables. Both now answer "which
  tables does this query read"; aliases are reachable via
  `find_all(..., LetRef)`.
- **`QueryIR.parse_warnings`, `Span.source_text` and `Expr.nullable` are
  removed.** None was populated by any code path. `parse_warnings` had zero
  assignments anywhere and duplicated `QueryIR.diagnostics`; `source_text`
  duplicated `Span.text(raw)`, which already slices from the source;
  `nullable` was filled from `res_type.IsNullable`, a property on no type in
  the assembly, and is not populatable at all — Microsoft's parser carries no
  nullability information. Stored IR JSON containing any of them now fails to
  load under `extra="forbid"`.
- **`ParseKvOp.columns` changes from `list[Assignment]` to `dict[str, str]`**,
  matching `AssertSchemaOp`. A declared key has a name and a type; there is no
  expression for an `Assignment` to hold.
- **`ColumnRef.table` is populated in many more places**, including inside
  `toscalar(...)`, `case()`/`iif()` arms, and under operators that previously
  filled nothing. `Pipeline.result_schema` is populated on `let` pipelines.
  Both are in the hash payload.
- **`semantic_hash` changes for any query** containing a membership operator,
  an `externaldata`, a `let`-bound name, or — on a bound parse — a column the
  binder can now resolve. Covered by the single schema bump described below.

- **Generic traversal now descends into `let` right-hand sides.** Populating
  `LetBinding.rhs_pipeline` changes what `walk()` and `find_all()` return for
  any query with a tabular `let` — the binding's whole subtree is now part of
  the IR. On `let Base = SecurityEvent | where EventID == 1; Base | count`,
  `find_all(ir, TableRef)` yields `['SecurityEvent', 'Base']` where it
  previously yielded `['Base']`. A lineage or table-inventory analyzer built
  on `find_all` will change its answers — usually to the correct ones, but
  silently. `to_llm_dict()` payloads grow correspondingly. Deduplicate, or
  scope the walk to `ir.main_pipeline`, if you need the old shape.
- **`semantic_hash` can differ between a bound and an unbound parse of a
  query whose `let` aliases a table.** `let A = OtherTable` builds
  `rhs_expr: ColumnRef` unbound and `rhs_pipeline: Pipeline(TableRef)` once
  the binder proves `OtherTable` is a table, so the IR *shape* — not just
  bind-time annotations — depends on whether a schema was supplied, and the
  volatile-field stripping that keeps `result_type` out of the hash cannot
  undo it. Accepted rather than fixed: the only way to make the shapes match
  without a schema is to assume any bare name on a `let` right-hand side is a
  table, which trades a documented difference for a silently wrong answer.
  Queries with no table-aliasing `let` hash identically bound or unbound.
  Compare hashes only across parses made the same way.
- `literal_kind` returns different values for `long`, `real`, `decimal`,
  `datetime`, `timespan`, `guid` and `null` literals.
- `LiteralExpr.value` changes format for `datetime` and `timespan`.
- `semantic_hash` changes for any query containing a datetime, timespan or
  real literal, or a `let` statement. This is the point — it was previously
  machine-dependent.
- `LetBinding.category` is removed. Nothing read it, every binding carried
  the same value, and it entered `semantic_hash` without being stripped as
  volatile. Which `rhs_*` field is populated already carries the distinction.
  Stored IR JSON containing `category` now fails `extra="forbid"` on load.
- `IR_SCHEMA_VERSION` bumps `0.1` → `0.2`, once, covering every field-shape
  change in this release: `LetBinding.category` and `MaterializeExpr`
  removed; `QueryIR.parse_warnings`, `Span.source_text` and `Expr.nullable`
  removed; `LiteralExpr.ticks`, `LetFunction`, `SubqueryExpr`, `LetRef`,
  `SetMembership.op` and `Exists.op` added; `ParseKvOp.columns` retyped;
  `literal_kind` and `LiteralExpr.value` changed. That is what the tag exists
  to flag — a consumer comparing it on stored IR JSON can refuse a payload
  from before this release rather than deserializing it into a shape that no
  longer matches.
- `SEMANTIC_HASH_SCHEME` bumps `kustology-sem-v1` → `kustology-sem-v2` in
  lockstep. The dump format feeding the hash changed, so `semantic_hash`
  differs for any query containing a datetime, timespan, real or decimal
  literal, a `let` statement, a membership or null-test operator, or — on a
  bound parse — a column the binder can now resolve. Bumping the tag makes
  that visible: a hash stored under `kustology-sem-v1:` no longer collides
  with one computed under `kustology-sem-v2:`, instead of silently comparing
  unequal with no signal that the canonicalization rules moved.

### Internal

- Both schema tags move exactly once for this release, not once per branch
  that touched them. Three branches landed breaking IR changes since `v0.1.0`;
  numbering each would have burned tags no consumer ever saw and left gaps in
  the released sequence for a later reader to explain. Reasoning recorded next
  to `SEMANTIC_HASH_SCHEME`, since the lockstep rule otherwise reads as
  "bump on every change".

- The `ruff` job lints `examples/` as well as `src tests scripts`; the six
  pre-existing `I001` findings there are fixed. `examples/` was already
  smoke-tested by `tests/test_examples.py` but never linted.
- `docs/superpowers/reports/2026-08-20-stub-sweep.md` records the full
  declared-but-unpopulated sweep behind this release, including the two
  detection methods that found ten of the twelve instances and two findings
  left open as follow-ups.
- The LLM view dispatches on class identity rather than `cls.__name__`
  string comparison, which would silently stop firing on a rename.

- Lint tooling is installed from `uv.lock` in CI rather than resolved fresh, so
  an upstream ruff release can no longer redefine the rule set mid-flight.
  Adopted ruff 0.16's default rule set; deviations are documented in
  `[tool.ruff.lint.per-file-ignores]`.

## [0.1.0] — 2026-06-01

First public release.

### Stability

This is a `0.y` release — per SemVer §4, the public API is not yet
considered stable.

Two version numbers describe the surface:

- **Package version** (`__version__`, displayed by `kustology version`):
  the overall library version. Pre-1.0, both Tier 1 and Tier 2 may
  break at minor versions.
- **`kustology.ir.IR_SCHEMA_VERSION`**: the IR shape's own version,
  used to tag serialized IR JSON so consumers can refuse to load an
  incompatible payload. Currently bumps in lockstep with the package
  version.

Tier 1 (`kustology` top-level surface) is on a stabilization track:
the package goes to 1.0 once Tier 1 survives external use without
correctness breaks. Tier 2 (`kustology.ir.*`) is expected to keep
evolving at package-minor cadence after 1.0 — its breaks are tracked
by `IR_SCHEMA_VERSION` and called out in this CHANGELOG.

### Tier 1 — thin .NET wrapper

- `parse(query, schema=None)` and `KustoQuery` for syntactic plus optional
  semantic analysis.
- `format_query(query)` for canonical reformatting via Microsoft's public
  `KustoCodeService.GetFormattedText`.
- `validate(query, schema=None, ignore_unknown_tables=False)` for structured
  parser diagnostics (severity, code, source offset).
- AST analyzers on `KustoQuery`:
  - `get_referenced_tables(force_syntactic=False)` — every table source,
    including joined, union'd, lookup'd, and `database()` / `cluster()`
    cross-cluster references.
  - `get_referenced_columns(force_syntactic=False)` — every column
    reference, with function callees and `$`-prefixed join sides filtered
    out.
  - `get_referenced_functions(force_syntactic=False)` — every function
    callee. Semantic mode resolves through `FunctionSymbol`; syntactic
    mode falls back to `NameReference` nodes in callee position.
  - `get_operator_chain()` / `get_operator_stats()` — ordered pipeline
    of operators and a `{OperatorKind: count}` map.
  - `get_structural_hash()` — SHA-256 over the AST node-kind sequence;
    stable across literal and whitespace changes.
  - `get_time_range()` — temporal expressions with source offsets.
  - `replace_table(old, new)` — AST-aware rename across every reference
    position (sub-pipelines, lookups, let-bound bodies).
- `kustology.utils.analysis.collect_nodes(syntax, predicate)` — reusable
  single-pass walker that returns every node satisfying a predicate.
  Hides `KustoWalker` boilerplate so a new analyzer is one lambda
  instead of a five-line subclass.
- `kustology` CLI with subcommands `version`, `format`, `validate`,
  `parse`. `parse` supports `--ast` (default) and `--ir` (requires the
  `[ir]` extra). Exit codes: `0` success, `1` Error-severity diagnostics
  or runtime failure, `2` usage error (bad flags, missing file, missing
  extra).
- CLI input capped at 10 MB by default; override with the
  `KUSTOLOGY_MAX_INPUT_BYTES` environment variable. Inputs exceeding the
  cap exit with code 2.
- CLI `parse --ast` and `parse --ast --json` cap recursion depth at 1000
  levels — past that, the node emits a `truncated` marker rather than
  blowing the Python stack. AST emitters use `str(node.Kind)` (the
  `SyntaxKind` enum value) for kind labels, matching the audit script's
  convention.
- `kustology.reflection` — runtime reflection on `Kusto.Language.Functions`
  drives categorized listings: `time_functions()`,
  `aggregate_functions()`, `scalar_functions()`, `string_functions()`,
  `all_function_names()`, plus `syntax_kinds()` for the `SyntaxKind` enum.
  No hard-coded fallback — a reflection failure is loud, not silent.
- `__version__` exposed at runtime via `importlib.metadata`.
- Bundled `Kusto.Language.dll` (12.3.2) pinned by SHA-256; refresh and
  verify scripts in `scripts/`.

### Tier 2 — semantic IR (opt-in via `[ir]` extras)

- `kustology.ir.QueryIR` — Pydantic root model holding a `Pipeline` of
  typed operators (`FilterOp`, `SummarizeOp`, `JoinOp`, `LookupOp`,
  `ProjectOp`, `ExtendOp`, …) and typed expressions (`BinOp`,
  `FuncCall`, `SetMembership`, `Between`, `And`, `Or`, …). 53 operator
  types and 26 expression types covering the Kusto query surface seen in
  real-world Sentinel detections (200/200 sample validates clean).
- `kustology.ir.IRBuilder.build(query)` parses, binds, and builds the IR
  in one call.
- `kustology.ir.IRBuilder.build_from_code(code)` builds from a
  pre-parsed `KustoCode`. `KustoQuery.to_ir()` uses this so callers
  don't pay for two parses.
- `KustoQuery.to_ir(attach_schema=...)` — controls the `SchemaAttacher`
  pass that materializes column provenance and `Pipeline.result_schema`.
  Default `None` auto-attaches when the parse was bound with a schema,
  so `parse(query, schema=...).to_ir()` returns a fully enriched IR
  without restating the schema. `True` forces attach with the parse-time
  schema; `False` skips even on a bound parse; a `dict` overrides for
  the attach step only.
- `kustology.ir.SchemaAttacher(schemas)` — propagates Microsoft's
  binding results into Pydantic fields (`Expr.result_type`,
  `ColumnRef.table`) and computes `Pipeline.result_schema`. Takes a
  flat `{table: {col: type}}` dict.
- `kustology.ir.walk(node)` — depth-first, pre-order traversal yielding
  every Pydantic `BaseModel` descendant.
- `kustology.ir.walk(node, predicate=...)` — predicate-filtered
  traversal for cross-cutting filters that don't reduce to a type
  (e.g. "every case-insensitive `BinOp`").
- `kustology.ir.find_all(node, type_)` — type-filtered traversal. The
  one-liner most custom analyzers reduce to:
  `find_all(ir, FilterOp)` returns every filter regardless of where it
  lives (main pipeline, join RHS, let-bound sub-pipeline).
- `kustology.ir.compute_semantic_hash(node)` — SHA-256 over the
  canonical IR shape (post-transforms, with spans and bind-time
  annotations stripped), prefixed with the scheme tag `kustology-sem-v1:`
  so the canonicalization rules are versionable. Accepts any IR
  subtree, not just `QueryIR`. Computed once at build and stored on
  `QueryIR.semantic_hash`; call directly to refresh after mutating the IR.
- Opt-in canonicalization transforms (`kustology.ir.transforms`):
  - `merge_consecutive_filters(root)` — fold consecutive `FilterOp`s
    into one whose predicate is an `And` of the originals. Recurses
    into sub-pipelines.
  - `normalize_expressions(root)` — apply semantic-preserving rewrites
    (`tolower(X) == "y"` → `X =~ "y"`, nested `And` / `Or` flattening,
    `not(not(X))` collapse). Post-order so deep nesting collapses
    cleanly.
- `QueryIR.to_llm_dict()` — lossy projection optimized for handing the
  IR to a language model: every node carries a `kind` discriminator,
  spans and defaulted fields are stripped, and `polarity` is collapsed
  into natural KQL operators (`!=`, `!in`, `!between`). Roughly 50%
  smaller than `model_dump_json()` on typical queries.
- `kustology.ir.UnknownExpr` / `UnknownSource` / `UnknownOp` — explicit
  fallback nodes for shapes the builder doesn't model. The coverage
  audit (`scripts/audit_syntax_kinds.py`) fails CI when new shapes
  appear after a `Kusto.Language` DLL upgrade.
- `kustology.ir.Finding` / `AnalyzerFn` / `Severity` — minimal
  analyzer-output protocol so independently-developed analyzers share
  a `Finding` vocabulary.
- `kustology.ir.IR_SCHEMA_VERSION` — IR shape version, decoupled from
  the package version.
- `kustology.ir.KustoType` — `StrEnum` for column and expression types
  (`STRING`, `LONG`, `INT`, `DATETIME`, `TIMESPAN`, …). `UNRESOLVED`
  for types the binder hasn't placed.
- `ImplicitSource` source variant for sub-pipelines whose row context
  comes from parent operators (union-at-root, mv-apply / partition
  subqueries, join / lookup RHS).
- Every IR `BaseModel` declares `extra="forbid"` for strict validation.
- `kustology.utils.schema_state.extract_schemas_from_global_state` —
  inverse of `build_global_state`; recovers `{table: {col: type}}`
  from a bound Microsoft `GlobalState`.
- `IRBuilder.HANDLED_OPERATOR_KINDS` / `HANDLED_EXPR_KINDS` — public
  attributes that the coverage audit reads as contract.

### Infrastructure

- CI matrix: macOS × Linux × Windows × Python 3.10+ (tested against
  3.10, 3.11, 3.12).
- Coverage audit (`scripts/audit_syntax_kinds.py`) fails CI on new
  uncovered `SyntaxKind` after a DLL refresh.
- Corpus regression (`scripts/verify_corpus.py`) against a 200-query
  Sentinel sample.
- DLL provenance verified on every push (`scripts/verify_dll.py`).
- PyPI publish workflow (`.github/workflows/release.yml`) — triggered
  by `v*` tags, builds via `python -m build`, generates a CycloneDX
  SBOM, publishes via PyPI trusted publishing, attests the artifacts,
  and creates a GitHub release with the dist and SBOM attached.
- `.pre-commit-config.yaml` mirroring CI for local fail-fast.

### Documentation

- `README.md` — install, two-tier overview, Quick Start, CLI reference.
- `ARCHITECTURE.md` — tier layout and contribution pointers.
- `CONTRIBUTING.md` — workflow and coding conventions.
- `SECURITY.md` — private vulnerability reporting and bundled-DLL
  verification (offline hash, NuGet re-fetch, DLL refresh).
- `AGENTS.md` — non-obvious technical context for agents modifying the
  code (CLR interop, AST navigation, IR gotchas, DLL provenance).
- 7 runnable examples in `examples/`:
  - `linter.py`, `binding_comparison.py`, `query_analysis.py` — Tier 1
    analysis demos.
  - `walk_tree.py` — AST traversal via `KustoQuery.syntax`.
  - `walk_ir.py` — typed IR traversal (mirror of `walk_tree`).
  - `find_all_demo.py` — generic IR traversal via `find_all`.
  - `llm_view.py` — LLM-tailored IR serialization via `to_llm_dict`.
