# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-08-20

First release since 0.1.0. Two themes: values the library reported wrongly
(culture-corrupted literals, mis-assigned column provenance, conflated
operators), and public surface that never worked (`LetBinding`'s fields,
`LetRef`, `ExternalDataExpr`'s contents).

Tier 2 breaks in several places, as its pre-1.0 policy permits — see
**Breaking** for the migration. Root-cause detail for the audits behind this
release is in `docs/superpowers/reports/`.

### Fixed

- **Culture no longer corrupts fractional numeric literals (tier 1).**
  Importing `kustology` pins .NET's culture to invariant, process-wide.
  Microsoft's parser evaluates `LiteralValue` lazily on property access, so
  the culture live in *caller* code decided the value: under `de-DE`, `1.5h`
  parsed as 15 hours and `| where CpuPct > 1.5` became ten times too strict;
  under `fr-FR` durations parsed to zero. Integer literals were unaffected,
  which is why it stayed invisible. A `de-DE`/`fr-FR` CI matrix guards it.
  No opt-out. **Residual risk:** the pin runs once, at import — a host that
  changes culture afterwards re-opens the corruption for any literal not yet
  read. See `bridge._pin_invariant_culture`.
- **Column provenance resolves everywhere (tier 2).** `ColumnRef.table` was
  filled for 17 of 53 operator types and never inside `toscalar(...)`,
  `case()`/`iif()` arms, or nested pipelines — so the same column resolved in
  one clause and not another, silently. Any lineage analysis over those
  subtrees was wrong.
- **`let` bindings are enriched and their names resolve (tier 2).**
  `SchemaAttacher.enrich` walked the main pipeline only. It now walks
  bindings in order and threads their names, so
  `let Base = SecurityEvent | …; Base | project Account` gives `Account` a
  type and the provenance `"Base"` — previously `unknown` and `None`.
- **Distinct KQL operators no longer collide (tier 2).** `in~`, `has_any` and
  `has_all` produced identical IR and an identical `semantic_hash`, as did
  `isnotnull` / `isnotempty` — so hash-based deduplication merged queries
  that mean different things (`has_any` and `has_all` are opposites). Both
  nodes now carry `op`.
- **`BinOp.case_sensitive` is correct across the operator family (tier 2).**
  Eight operators were reported backwards, including every negated string
  operator (`!has`, `!contains`, …) and `hasprefix`/`hassuffix`.
- **`literal_kind` and `LiteralExpr.value` are faithful (tier 2).**
  `literal_kind` was re-inferred from the Python type, so `real` reported as
  `int` and `datetime`/`timespan`/`guid` all as `string`. `value` rendered
  through the ambient culture and reached `semantic_hash`, making the hash
  machine-dependent; datetimes now render as ISO 8601 round-trip and
  timespans in invariant form.
- **`semantic_hash` no longer depends on whether a schema was passed
  (tier 2).** `QueryIR.semantic_hash` is computed at build time so the shipped
  value was unaffected, but the field's own docstring tells consumers to call
  `compute_semantic_hash` again after mutating the IR — and that path hashed
  `ColumnRef.table` and `Pipeline.result_schema`, both binder-populated. The
  same query text therefore hashed two ways. The one remaining divergence is
  a difference in IR *shape*, not field values, and is documented above.
- **`LetBinding` is populated (tier 2).** `rhs_expr`, `rhs_pipeline`,
  `inner_tables` and `inner_time_exprs` were permanently empty, which blocked
  let-resolution entirely. All tabular right-hand-side shapes are covered,
  including the parenthesized `let X = ( T | where … );` form that dominates
  Microsoft Sentinel rules.
- **`ExternalDataExpr` carries real data (tier 2).** `uri`, `columns` and
  `format` were placeholders — `uri` was literally the string `"url"`.
- **`ParseKvOp.columns`, `MacroExpandOp.pipeline` and `Expr.result_type_inner`
  are populated (tier 2).** Each was empty for every query ever parsed.
- **`walk()` / `find_all()` descend tuple-valued fields (tier 2).** A
  `case()` holding five `ColumnRef`s surfaced one, since `CaseExpr.branches`
  is `list[tuple[Expr, Expr]]`.
- **`canonical_form` renders every expression shape (tier 2).** Eleven `Expr`
  types rendered as a bare `"?"`, so `-X > 1`, `D.a == 1` and
  `toscalar(…) > 1` were indistinguishable.
- **A bare tabular subquery is modeled (tier 2).**
  `| where User in ((Suspicious | project User))` collapsed the inner query
  into one `UnknownExpr` blob; it now builds a `SubqueryExpr`.
- **`find in (...)` / `search in (...)` tables are found (tier 1).**
  `get_referenced_tables()` returned nothing for them, and `replace_table()`
  returned the query **unchanged with no error** — so a table migration
  silently shipped the old name.
- **`top-hitters` and `__partitionby` no longer raise; `TopHittersOp` gains
  `of` (tier 2).** Both builder branches read a .NET member their node type
  does not have — `TopHittersOperator.ValueExpression`, which exists on no
  type in the assembly, and `PartitionByOperator.Expression`, where the
  partition key is `Entity` — so `to_ir()` raised `AttributeError` on valid
  KQL while both kinds sat in `HANDLED_OPERATOR_KINDS` claiming to be
  modelled. `top-hitters N of C by V` has two column operands and
  `TopHittersOp` had a field for only one: `of` is the column being counted
  and is required, matching `SampleDistinctOp.of`; `by` is the optional
  ranking weight and widens to `AnyExpr | None`.
- **`datetime` literal `value`/`ticks`/`semantic_hash` no longer depend on
  the host timezone (tier 2).** `literal_value_and_ticks` rendered a `datetime`
  literal's `.Ticks`/`.ToString()` straight off .NET's `DateTime` without
  normalizing `Kind`. A `Z`-suffixed literal such as
  `datetime(2024-01-01T00:00:00Z)` parses through .NET's default
  `DateTime.Parse` as `DateTimeKind.Local`, so its `.Ticks` already carried
  the *host's* UTC offset baked in; a bare literal like `datetime(2024-01-01)`
  parses `Unspecified` instead. The two need opposite treatment — `Local`
  must be *converted* to UTC, `Unspecified` merely *specified* as UTC, since
  KQL datetimes are UTC by definition — so the same query hashed differently
  on a laptop in New York than a CI runner in Tokyo.
- **The `tolower`/`toupper` equality rewrite only fires against a
  matching-case literal (tier 2).** `normalize_expressions` unconditionally
  rewrote `tolower(X) == "Y"` to `X =~ "Y"`, but that is only sound when the
  literal is already in the folded case: `tolower(X) == "Y"` (capital Y) is
  always false, since `tolower` never returns anything but lowercase, while
  `X =~ "Y"` is a case-insensitive match that is often true — so the rewrite
  made two predicates with different truth values collide on `semantic_hash`.
  The fix checks the literal against the fold before rewriting, handles the
  literal on either side of the comparison (normalizing to `X =~ "y"`
  either way), covers `toupper` symmetrically, and no longer rewrites at all
  when the other side is not a literal (`tolower(X) == Col` is not
  equivalent to `X =~ Col` for arbitrary `Col`).
- **Volatile fields are stripped from `semantic_hash` by model field, not by
  key name (tier 2).** The strip ran over the dumped JSON and deleted every
  key called `span` / `table` / `result_schema` at any depth, which was both
  too broad and too narrow. Too broad: `AssertSchemaOp.columns` is a
  `dict[str, str]` of the query's own column names, so
  `assert-schema (a:long, table:long)` lost the column literally named
  `table` and hashed identically to `assert-schema (a:long)`. Too narrow:
  `LetFunction.body_span` is a span whose field is not called `span`, so a
  comment ahead of a `let`-declared function shifted its offset and changed
  the hash, and `raw_text` — recorded for `scan`, `top-nested` and the
  `graph-*` family — carried the node's leading whitespace and comments
  verbatim. The builder now records `ToString(IncludeTrivia.Minimal)`, the
  hash normalizes `raw_text` whitespace on its private copy, and a bare
  `Expr` root of the form `not(not(X))` finally collapses (the replacement
  was computed and discarded, since only a parent field assignment installed
  it). `to_llm_dict` drops `body_span` too.

### Added

- `LetRef` (tier 2) — now actually emitted, so a `let` alias is
  distinguishable from a table. Decided from the `let` statements alone, so
  it holds with or without a schema.
- `SetMembership.op` and `Exists.op` (tier 2) — the literal KQL operator,
  following `BinOp.op`.
- `ColumnRef.join_side` (tier 2) — `"left"` / `"right"` when the query wrote
  `$left.` / `$right.`, otherwise `None`. `table` could not carry this: the
  binder overwrites the sentinel with the table it resolves to, so a bound
  parse lost the side entirely. The distinction is semantic —
  `$left.a == $left.b` compares two columns of one table, `$left.a ==
  $right.b` is a join key — and it is what lets `table` be excluded from
  `semantic_hash` without those two colliding.
- `SubqueryExpr`, `LetFunction`, `LiteralExpr.ticks` (tier 2), and
  `literal_kind` gains `"decimal"`. `ticks` is the only lossless form for
  `datetime` / `timespan`; `ticks // 10` gives exact microseconds.
- `iter_elements()` (tier 1) — unwraps the `SeparatedElement` wrappers .NET
  list properties yield, passing plain `SyntaxList` through unchanged.
- `SEMANTIC_HASH_SCHEME` is exported from `kustology.ir`, beside
  `IR_SCHEMA_VERSION`. Both are the consumer's compatibility contract; only
  one of them was reachable without importing a private module.
- README gains a "Versioning and stability" section covering both tags and
  what they mean for stored IR and stored hashes.
- README section documenting the syntax-tree traps, including two pythonnet
  ones this release's audits turned up: member lookup is exact,
  case-sensitive and silent, and an empty .NET `IReadOnlyList` is truthy.
- ARCHITECTURE.md's "Where to add things" checklists gain the steps this
  release proved load-bearing: a column-scope decision in `SchemaAttacher`
  for new operators (the step whose absence left provenance covering 17 of
  53), and, for new expressions, the `AnyExpr`/`__all__` pair, a `canonical()`
  render branch, and the lossy-lowering check. Its layout map now lists all
  of `ir/` and `utils/` rather than half of it.
- `examples/walk_ir.py` and `examples/walk_tree.py` handle `let`, and their
  shared query now contains one — demonstrating `LetBinding.rhs_pipeline` and
  the `TableRef` / `LetRef` split against the AST equivalent.
  `examples/find_all_demo.py` shows the same split via `find_all`.

### Changed

- `get_time_range()` is renamed `find_time_expressions()` on both the module
  and `KustoQuery`; the old name remains as a deprecated alias. It returns
  every time-related expression, not a resolved range, and the old name led a
  consumer to use it as a lookback extractor.
- .NET runtime discovery and boundary member probes log at `DEBUG`.

### Breaking (tier 2, pre-1.0)

- **`SetMembership.op` and `Exists.op` are required**, and
  `ParseKvOp.columns` changes from `list[Assignment]` to `dict[str, str]`.
- **Removed:** `MaterializeExpr` (proven unreachable — see
  `docs/superpowers/reports/2026-08-20-materialize-reachability.md`),
  `LetBinding.category`, `QueryIR.parse_warnings`, `Span.source_text` and
  `Expr.nullable`. None was ever populated by any code path.
- **Stored IR JSON from 0.1.0 no longer loads** under `extra="forbid"`, in
  both directions: it lacks the new required fields and may carry removed
  ones. Rebuild from source rather than migrating.
- **`semantic_hash` changes for most queries**, and this is the point — it
  was machine-dependent (culture-rendered literals) and collided across
  distinct operators. It now differs for any query containing a datetime,
  timespan, real or decimal literal; a membership or null-test operator; one
  of the eight corrected string operators; a `let` statement; or, on a bound
  parse, a column the binder can now resolve. `IR_SCHEMA_VERSION` bumps
  `0.1` → `0.2` and `SEMANTIC_HASH_SCHEME` `kustology-sem-v1` →
  `kustology-sem-v2` in lockstep, so stored hashes are invalidated visibly
  rather than silently comparing unequal.
- **`literal_kind` returns different values** for `long`, `real`, `decimal`,
  `datetime`, `timespan`, `guid` and `null` literals, and `LiteralExpr.value`
  changes format for `datetime` and `timespan`. Both were wrong before; code
  branching on the old values needs updating.
- **`find_all(ir, TableRef)` no longer returns `let` aliases**, and
  `LetBinding.inner_tables` narrows to real tables. Both now answer "which
  tables does this query read"; aliases are reachable via
  `find_all(..., LetRef)`.
- **Generic traversal descends into `let` right-hand sides**, so `walk()` and
  `find_all()` return nodes from inside bindings that were previously
  unreachable.
- **`ColumnRef.table` and `Pipeline.result_schema` are populated in many more
  places**, including inside `toscalar(...)`, `case()` arms and `let`
  pipelines. Neither is in the hash payload: both are inferred from the
  caller's schema rather than stated by the query, so hashing them made the
  same query text hash two ways depending on whether one was passed. Every
  field `SchemaAttacher` writes is now stripped before hashing, which shifts
  every `semantic_hash` value again — still within `kustology-sem-v2`, which
  covers the whole unreleased window since `v0.1.0`.
- **`semantic_hash` can differ between a bound and an unbound parse** of a
  query whose `let` aliases a table: the binder proves it is a table and the
  IR shape changes accordingly. Accepted and documented rather than papered
  over — the alternative is treating every bare name as a table without
  proof.
- **`TakeOp`/`SampleOp`/`TopOp`/`TopHittersOp`/`SampleDistinctOp.count` is
  `int | AnyExpr`** (was `int`); `let n = 10; T | take n` and
  `take toscalar(...)` no longer raise.

### Internal

- `docs/superpowers/reports/` records the two audits behind this release: a
  sweep for declared-but-unpopulated surface, and the reachability proof for
  `MaterializeExpr`.
- `tests/test_reflection_audit.py` asserts every .NET member name probed via
  `getattr`/`hasattr` exists in the loaded assembly. Four defects in this
  release came from a probe naming a member that does not exist — pythonnet
  resolves members case-sensitively and says nothing when one is absent.
- The corpus coverage gates walk the whole `QueryIR` via `find_all` instead
  of hand-maintained attribute lists, and now cover `let` right-hand sides.
- CI lints `examples/` alongside `src tests scripts`, and installs lint
  tooling from `uv.lock` so an upstream release cannot redefine the rule set
  mid-flight.

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
