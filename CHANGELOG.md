# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-08-25

First release since 0.1.0. Two themes: values the library reported wrongly (culture-corrupted literals, mis-assigned column provenance, conflated operators), and public surface that never worked (`LetBinding`'s fields, `LetRef`, `ExternalDataExpr`'s contents). Tier 2 breaks in several places, as its pre-1.0 policy permits — see **Upgrading from 0.1.0** and **Breaking** below.

### Upgrading from 0.1.0

- **Every stored digest string changes**: the scheme tag bumps to v2 and the canonical content moves with it — see `### Breaking` for the field-by-field detail.
- **Stored 0.1.0 IR JSON no longer loads**, in either direction, under `extra="forbid"`. Rebuild from source.
- **IR unions are discriminated on `kind`.** Hand-built JSON (anything not from `model_dump`) must include the `kind` field.
- **`SchemaAttacher`, `BinderEnricher`, and the `KIND` ClassVar are removed from the public surface.** Use `parse(query, schema=...)`/`to_ir(attach_schema=...)`; read `model_fields["kind"].default` for a class's kind.
- **`count` fields widen to `int | AnyExpr`** on `TakeOp`/`SampleOp`/`TopOp`/`TopHittersOp`/`SampleDistinctOp` — a `let`-bound or computed count no longer raises.
- **Renamed or reshaped:** `ForkOp.pipelines` → `.branches`; `ExternalDataExpr.uri` → `.uris` (`list[str]`); `SortOp.expressions`/`TopOp.by`/`ProjectReorderOp.columns` gain ordering-aware wrapper types; `ParseKvOp.columns`, `MakeSeriesOp.aggregations`, `MvExpandOp.columns` change shape; a `let`-bound scalar now lowers to `LetValueRef`, a typed name declaration to `TypedNameDecl` — neither is a `ColumnRef` anymore.
- **The CLI's exit-code contract is enforced**: `0` success, `1` a diagnostic-carrying parse, `2` a usage/input error.
- See `### Breaking` below for the field-by-field detail.

### Breaking (tier 2, pre-1.0)

- **`SetMembership.op`/`Exists.op` are required**, `Exists` gains a required `polarity` (`isnull`/`isempty` now build `Exists`, not `FuncCall`), `ParseKvOp.columns` becomes `dict[str, str]` (was `list[Assignment]`), and `BinOp.polarity`/`.case_sensitive` become `... | None` (arithmetic operators carry `None`).
- **Removed** (never populated by any code path): `MaterializeExpr`, `LetBinding.category`, `QueryIR.parse_warnings`, `Span.source_text`, `Expr.nullable`. `kustology.PackageNotFoundError` is gone from the package namespace too (tier 1) — it was never in `__all__`, and `__version__` is unaffected.
- **Removed from the public surface:** the undocumented `BinderEnricher` alias and `SchemaAttacher` itself — still implemented in `kustology.ir.binder`, now as the provenance pass described under `### Changed`. Use `parse(query, schema=...)` or `to_ir(attach_schema=...)`.
- **Stored IR JSON from 0.1.0 no longer loads**, in either direction, under `extra="forbid"`. Rebuild from source rather than migrating.
- **`semantic_hash` changes for every query** — the point of this release. `IR_SCHEMA_VERSION` bumps `0.1` → `0.2` and `SEMANTIC_HASH_SCHEME` `kustology-sem-v1` → `kustology-sem-v2`.
- **`literal_kind` returns different (now-correct) values** for `long`, `real`, `decimal`, `datetime`, `timespan`, `guid` and `null`; `LiteralExpr.value` changes format for `datetime`/`timespan`.
- **`find_all(ir, TableRef)` no longer returns `let` aliases** (`LetBinding.inner_tables` narrows to real tables — use `find_all(..., LetRef)`), and generic traversal now descends into `let` right-hand sides.
- **`ColumnRef.table`/`Pipeline.result_schema` are populated in many more places** (inside `toscalar(...)`, `case()` arms, `let` pipelines) and are stripped from the `semantic_hash` payload. `LetBinding.inner_tables`/`inner_time_exprs` are stripped too, for an unrelated reason: they are a derived index over the right-hand side sitting beside them, so hashing them added nothing the tree does not already carry — while, being a *copy* of names the renames below rewrite, they cost the collisions that copy desynchronized on. All four stay populated on your own IR.
- **`TakeOp`/`SampleOp`/`TopOp`/`TopHittersOp`/`SampleDistinctOp.count` is `int | AnyExpr`** (was `int`); `let n = 10; T | take n` and `take toscalar(...)` no longer raise.
- **`semantic_hash` now sorts `and`/`or` operands and `in (...)` values**, merges consecutive `| where` filters before hashing, and is invariant under renaming a **`let` binding** (by declaration index, in a function body or a `declare pattern` arm as well as at the top level), a **function parameter** (`$param<i>` by signature position, every reference inside that one body following), and a **`let`-bound function's call sites** — so `let f = (w:int) {…}; f()` and `let g = (z:int) {…}; g()` are one digest. Only a `let` that bound a *function* renames its call sites: KQL resolves values and functions in separate namespaces, so `let abs = 5; … abs(x)` still calls the built-in. `declare pattern` parameters and `declare query_parameters` names stay verbatim — neither is a local label, and the second is the caller-facing API of a saved query.
- **`SortOp.expressions` is `list[SortKey]`** (was `list[AnyExpr]`), **`TopOp.by` is a `SortKey`**, and **`ProjectReorderOp.columns` is `list[ReorderKey]`** — all three now carry direction/`nulls` ordering instead of discarding it. `SortKey.direction` is required (bare `sort by x` records `"desc"`); `ReorderKey.direction` is optional.
- **`ForkOp.pipelines` is replaced by `ForkOp.branches`**, a `list[ForkBranch]` with an optional `name` and a `pipeline` each — branches previously built with no operators at all.
- **`ToScalarExpr.pipeline`/`SubqueryExpr.pipeline` are typed `Pipeline | None`** (were `Any`) — an untyped nested pipeline silently degraded to a plain dict on JSON round-trip, breaking `find_all` and re-hashing inside it.
- **`LetFunction.parameters` is `list[LetFunctionParameter]`** (was `list[str]` of bare names) and the class gains `is_view`/`body_lets`/`body_pipeline`/`body_expr` — read `p.decl.name` where a name was read before. A `let` written inside a function body no longer appears in `QueryIR.let_bindings`; it is on that function's `body_lets`.
- **A `let`-bound scalar in an expression is a `LetValueRef`, not a `ColumnRef`** — no longer counts toward `find_all(ir, ColumnRef)` lineage. Which name is local is decided from the query text rather than column-first the way KQL resolves it; see `### Known limitations`.
- **`QueryIR` gains `additional_pipelines`**, a `list[Pipeline]` for statements after the first `;` — previously discarded outright. `main_pipeline` still holds only the first statement; iterate `[ir.main_pipeline, *ir.additional_pipelines]` for all of them.
- **The pipeline source position gains `DataTableSource`/`ExternalDataSource`**, `TableRef.database`/`.cluster`/`.is_wildcard`, and `properties: dict[str, str]` on both externaldata classes; `ExternalDataExpr.uri` becomes `uris: list[str]`. `to_llm_dict` caps `DataTableSource.rows` at 20 and adds a `rows_omitted` count; `model_dump_json` stays complete.
- **Every operator gains `hints`**, a `dict[str, str]` of `hint.*` named parameters — **excluded from `semantic_hash`** by design, since a hint changes execution, not the rows returned.
- **`JoinOp.join_kind`/`LookupOp.lookup_kind` are required and carry KQL's effective default** (`"innerunique"`/`"leftouter"`, not `"inner"`) — both were previously mislabelled and collapsed onto the wrong explicit spelling.
- **`RenderOp` gains `properties`** for the `render ... with (...)` clause (`title`, `ymin`, `series`, …), previously discarded entirely.
- **`MakeSeriesOp.aggregations` is `list[MakeSeriesAggregate]`** (was `list[Assignment]`, gains `.default`), and `in range(from, to, step)` now populates the range fields.
- **Several operators gain required/typed fields for modifiers this release stopped discarding:** `FindOp.tables` (`list[TableRef | LetRef]`, plus `withsource`/`project`), `SearchOp` (`tables`, `search_kind`), `UnionOp` (`union_kind`, `is_fuzzy`, `withsource`), `ParseOp`/`ParseWhereOp` (`parse_kind`, `flags`), `MvExpandOp.columns` (`list[MvExpandColumn]`, plus `row_limit`/`with_item_index`/`expand_kind`). Each required field carries KQL's effective default.
- **A typed name declaration is a `TypedNameDecl`, not a `ColumnRef`** — `parse a with 'x' b:long` and a typed `find ... project a:string` previously lost the declared type.
- **`KustoWalker.visit` takes a `depth` argument** (`visit(self, node, depth=0)`) so the base class can enforce `MAX_AST_DEPTH`; a subclass overriding `visit` itself now gets `TypeError` on first recursion into it (tier 1).
- **IR unions are discriminated on `kind`** (`Pipeline.source`/`.operators`, `SearchOp.tables`, `FindOp.tables`, and `AnyExpr` — every expression position) — hand-built JSON must carry it, and a kind-less payload is now rejected by name (`union_tag_not_found`) rather than validating as whichever member happens to match first. IR model classes no longer carry a `KIND` ClassVar; read `model_fields["kind"].default` instead.

### Added

- New IR surface (tier 2): `ir_schema_version` on `to_llm_dict(QueryIR)`, `Exists.polarity`, `LetRef`, `SetMembership.op`/`Exists.op`, `ColumnRef.join_side`, `SubqueryExpr`, `LetFunction`, `LiteralExpr.ticks`, and `literal_kind` gains `"decimal"`.
- **Operator fields for the last modifiers this release stopped discarding** (tier 2): `MvApplyOp.to_typeof`/`.row_limit`/`.item_index`, `ParseKvOp.properties` (its `with (...)` clause — a list of pairs, since `quote` legally repeats), `GetSchemaOp.output_kind`, `ConsumeOp.decodeblocks`, and `EvaluateOp.declared_schema`/`.declared_schema_star` for the output-schema clause. Each is optional with a default, which is why they are here rather than under `### Breaking`: hand-built JSON that omits them still validates, and an unwritten one dumps exactly as it did before the field existed, so a query that writes no modifier does not move digest.
- **`LetFunction` carries the whole declaration** (tier 2): `is_view`, `body_lets`, `body_pipeline`/`body_expr`, and `parameters` as `LetFunctionParameter`s (a `TypedNameDecl` plus an optional `default`). `LetBinding.inner_tables`/`inner_time_exprs` are populated for a function binding too.
- `iter_elements()` and `plugin_functions()` (tier 1) — the latter reflects the 47 `evaluate` plug-ins, previously absent from `all_function_names()`.
- `SEMANTIC_HASH_SCHEME` is now exported from `kustology.ir`. README gains "Versioning and stability" and syntax-tree-traps sections; ARCHITECTURE.md's checklists gain the steps this release proved load-bearing.
- `examples/walk_ir.py`/`walk_tree.py` now demonstrate `let`; two new examples (`semantic_hash_demo.py`, `analyzer_demo.py`) and a rewritten `linter.py` (now an actual linter) bring the set to nine, all exercised by `tests/test_examples.py`.
- `kustology parse --schema PATH` (tier 1), a versioned `{ir_schema_version, semantic_hash_scheme, ir}` envelope for `parse --ir --json`, and `KustoQuery.diagnostics` reading off an already-parsed `KustoCode` — unfiltered (no `ignore_unknown_tables` on a property; filter the list yourself).

### Changed

- `get_operator_chain()` returns operators only, not `[source, *operators]` (tier 1) — use `find_table_references()` for the source. `get_time_range()` is renamed `find_time_expressions()` (old name kept as a deprecated alias).
- `SchemaLike` narrows from `dict | str | None` to `dict | None` (tier 1, typing only) — the `str` arm never worked at runtime.
- .NET runtime discovery and boundary member probes now log at `DEBUG`.
- `format`/`parse` now refuse input the parser rejected — no stdout output, diagnostics on stderr, exit `1` — instead of printing a fragment of invalid KQL and exiting `0`.
- `parse --ast --json` node text no longer carries the node's leading whitespace (a leading comment still does); both CLI JSON emitters now render `KustoQuery.to_dict()`.
- CLI input is read as bytes, not newline-translated text (tier 1) — through `sys.stdin.buffer`, with file inputs opened binary; `KUSTOLOGY_MAX_INPUT_BYTES` therefore counts bytes, and reported byte offsets reflect the input as written, including CRLF. `format` output is unaffected.
- A schemaless `to_ir()` now binds against `GlobalState.Default`, so literal and built-in-function types resolve the same as `IRBuilder().build()` (tier 2); the twelve unknown-name diagnostic families are dropped from both schemaless paths instead of reported spuriously.
- `to_ir(attach_schema=dict)` now binds the query through Microsoft's binder (`build_global_state` + `Analyze`); output schemas, types, and IR shape match `parse(query, schema=...).to_ir()` exactly.
- Operator/pipeline `result_schema` now comes from Microsoft's binder's `ResultType` instead of hand-written per-operator rules (tier 2), correcting join-collision suffixes, wildcard `project-keep`, `mv-expand` element types, `arg_max(t, *)`, and union conflicts. This correction moves nothing in `semantic_hash` by itself — `result_schema`/`table` are volatile, stripped from the payload before hashing — **the movement comes from that stripping** (see `### Breaking`).
- Partial schemas no longer get hand-guessed operator schemas: where Microsoft's binder leaves a symbol open, `result_schema` is now honestly `None`. `ColumnRef.table` provenance is unchanged except where the walk can no longer tell which source a name belongs to, and there it narrows to `None` rather than guessing: the two halves of a post-join collision (`shared`/`shared1`), a union type-conflict's split variants (`a_long`/`a_string`), and a column both a `search`'s tables and its enclosing scope carry. Qualified `$left.`/`$right.` references and `project-rename` targets are unaffected.
- `to_llm_dict` drops `Operator.result_schema` and keeps `Pipeline.result_schema` (tier 2): per-operator copies were 35% of the whole LLM view across the 49-query fixture corpus (295,156 of 851,224 bytes); without them the view is a median 45% smaller than `model_dump_json`, up from 28%.
- `ColumnRef.table` never carries the `$left`/`$right` sentinel — an unresolvable join side is `None` and `join_side` carries the side; `find` now seeds its tables for provenance like `search`. Where a `let` alias is not a table the walk could close (a scalar or function binding), both operators seed an empty scope instead of writing the alias name into `table` regardless.
- **Provenance walks a `let`-function's and a `declare pattern` arm's body as its own scope** (tier 2), masking every parameter name so a same-named real table cannot leak its columns into a reference that is really the parameter. A call site still acquires nothing from the body. Digest-silent, like the rest of this pass: every field it writes is stripped before hashing.

### Fixed

- **Culture no longer corrupts fractional numeric literals (tier 1).** Importing `kustology` pins .NET's culture to invariant, process-wide, with no opt-out; a `de-DE`/`fr-FR` CI matrix guards it. **Residual risk:** the pin runs once, at import — a host that changes culture afterwards re-opens the corruption for any literal not yet read; see `bridge._pin_invariant_culture`.
- **Column provenance resolves everywhere (tier 2).** `ColumnRef.table` was filled for 17 of 53 operator types and never inside `toscalar(...)`, `case()`/`iif()` arms, or nested pipelines.
- **`let` bindings are enriched and their names resolve (tier 2).** `SchemaAttacher.enrich` now walks bindings in order and threads their names.
- **Operator collisions closed across every operator this release re-modelled (tier 2).** Distinct operators produced identical IR and one `semantic_hash` — `in~`, `has_any` and `has_all`, and `isnotnull`/`isnotempty`; both node kinds now carry `op`. Each modifier pair below collided the same way and now does not: sort/`order by`/`top` direction and `nulls` ordering, `project-reorder` direction, `fork` branches, `datatable`/`externaldata` content, database/cluster qualifiers and wildcard tables, `mv-expand`'s four modifiers, `mv-apply`'s `to typeof`/`limit`/`with_itemindex`, `parse` kind/flags, `parse-kv`'s `with (...)` properties, `union` kind/withsource/isfuzzy, `search` kind and in-list, `make-series` default/range, `render` configuration, `getschema`'s `kind=csl`, `consume`'s `decodeblocks=`, `evaluate`'s output-schema clause (`: (x:string)`, and the `*` append form), `find` in-list/withsource, typed captures, `let`-bound scalars vs. columns, `externaldata`'s `ignoreFirstRecord=`, and multi-statement queries past the first `;`. KQL's effective defaults (e.g. a bare `join` = `innerunique`) still hash with their explicit spelling. For the field-by-field detail see `### Breaking` above where a field was reshaped or made required, and `### Added` where the operator simply gained one; `### Known limitations` below has what is still open.
- Comments and leading trivia no longer leak into table/column/function names (tier 1), nor into `semantic_hash` via column-type strings or the `externaldata` URI fallback (tier 2) — both read via `ToString()` with default trivia; reads now go through trivia-stripped helpers (`node_text`/`node_name`, `read_row_schema`). A comment *interior* to an unmodelled node still reaches `UnknownSource.raw_text` and the URI fallback (no `IncludeTrivia` mode strips interior trivia) — documented there rather than papered over.
- **`BinOp.case_sensitive` and `.polarity` are correct across the operator family (tier 2).** Eight operators reported case sensitivity backwards, including every negated string operator and `hasprefix`/`hassuffix`; `search Col:'x'` is now case-insensitive, matching its documented meaning. Arithmetic operators (`+ - * / %`) claim neither any more — both record `None`, and `to_llm_dict` omits the fields rather than emitting `null`.
- **`literal_kind`/`LiteralExpr.value` are faithful (tier 2).** `real`/`datetime`/`timespan`/`guid` no longer misreport, and values render culture-independently.
- **`semantic_hash` no longer depends on whether a schema was passed (tier 2).** Re-hashing an IR after mutation previously leaked binder-populated `ColumnRef.table`/`Pipeline.result_schema`.
- **`LetBinding` is populated (tier 2).** `rhs_expr`, `rhs_pipeline`, `inner_tables` and `inner_time_exprs` cover all tabular right-hand-side shapes, including the parenthesized `let X = (T | where …)` form.
- **Fields that shipped as placeholders or empty now carry real data (tier 2).** `ExternalDataExpr` (uris/columns/format) and expression-position `datatable(...)` (`DataTableExpr`) held placeholders — the URI was literally the string `"url"`; `ParseKvOp.columns`, `MacroExpandOp.pipeline` and `Expr.result_type_inner` were empty for every query ever parsed.
- **`walk()`/`find_all()` now descend tuple-valued fields and visit each shared IR node once (tier 2)** — a `case()` holding five `ColumnRef`s surfaced one, and a `let`-bound `FuncCall` reachable through two fields was reported twice.
- **All four null/empty tests lower to `Exists` (tier 2).** `Exists` gains a required `polarity`; previously only the positive pair (`isnotnull`/`isnotempty`) did.
- **A bare `*` is a `StarExpr`, not a column named `*` (tier 2).** Covers every position a bare wildcard can occupy; a *prefix* wildcard (`a*`) stays a `ColumnRef`.
- **Adjacent string literals are one literal (tier 2).** `'a' 'b'` now lowers to one `LiteralExpr` instead of an `UnknownExpr` holding raw text.
- **`canonical_form` renders every expression shape, parenthesizes by precedence, escapes strings, and spells bools/null the KQL way (tier 2).** Eleven `Expr` types previously rendered as a bare `"?"`; `x - (y - z)` no longer reads as `x - y - z`.
- **A bare tabular subquery is modeled (tier 2).** `in ((Suspicious | project User))` now builds a `SubqueryExpr` instead of one opaque blob.
- **`find in (...)`/`search in (...)` tables are found (tier 1).** `get_referenced_tables()` returned nothing for them, and `replace_table()` silently returned the query unchanged.
- **`top-hitters`/`__partitionby` no longer raise; `TopHittersOp` gains a required `of` (tier 2).** Both builder branches read a .NET member their node type does not have.
- **`datetime` literal `value`/`ticks`/`semantic_hash` no longer depend on the host timezone (tier 2).** A `Z`-suffixed and a bare literal now both normalize to UTC.
- **The `tolower`/`toupper` equality rewrite only fires against a matching-case literal (tier 2).** It previously could turn an always-false predicate into a usually-true one.
- **Volatile fields are stripped from `semantic_hash` by model field, not by key name (tier 2).** A column literally named `table` is no longer dropped, and `LetFunction.body_span`/`raw_text` whitespace are now excluded correctly. A bare `Expr` root of the form `not(not(X))` now collapses too — the replacement used to be computed and discarded, since only a parent field assignment installed it.
- **`find_table_references` reports what a query really names (tier 1).** Aliases, function parameters and wildcards are no longer reported as tables (a bracketed `let` name leaked the same way for a different reason), a shadowing `let` keeps its right-hand side, and a bound parse no longer drops tables the schema does not describe — the binder's answer is merged with every syntactic reference the binder left unresolved.
- **`get_structural_hash()` no longer collapses `kind=inner` into `kind=leftanti` (tier 1).** Named-parameter values and `evaluate` plug-in names are now part of the hash. **Stored hashes from 0.2-dev are invalidated.**
- **`get_referenced_columns()` excludes tables by position, and stops walking into dynamic paths (tier 1).** A column sharing a table's name elsewhere no longer vanishes everywhere; dynamic-bag selector keys are no longer reported as columns. Syntactic mode now also reports the columns an `extend`/`summarize` creates; semantic mode reports those only where the query reads the alias back.
- **Reflection reads every overload, sees shadowed statics, and lists `evaluate` plug-ins (tier 1).** `bin`/`bin_at` are now correctly time functions and `FuncCall.is_time_func` reflects it; `scalar_functions()`/`aggregate_functions()` no longer overlap.
- **The CLI honours its documented exit codes (tier 1).** File/schema-load failures are `2`; a diagnostic-carrying `parse` exits `1` instead of `0`; and a broken pipe no longer reports a usage error or erases the command's own exit code.
- **Deeply nested input no longer raises `RecursionError` out of walker-routed traversal (tier 1)** — capped at `MAX_AST_DEPTH = 300`. **Not covered:** `to_ir()`/`IRBuilder` and two `utils/analysis.py` helpers recurse outside the walker and can still raise on adversarial input.
- **`replace_table` validates its arguments and quotes a name that needs it (tier 1).** Rejects empty/non-`str` names instead of emitting an unparseable query, and quotes a replacement name with hyphens, spaces, or a KQL keyword instead of emitting one the parser reads differently.
- **`find_time_expressions` reports a nested temporal call once (tier 1).** `startofday(now())` previously came back as two overlapping entries.
- **A malformed schema names its own problem (tier 1).** An empty schema string is a `ValueError`; a wrong-typed type/column/table name is a `TypeError` naming its position; type names match case-insensitively; and the schema-*string* form warns about a column it could not type (`{"T": "(n:bogus)"}`) the way the dict form already did — all in place of an opaque CLR exception a caller could catch only with a bare `except Exception`. The unknown-scalar-type `RuntimeWarning` now names the caller's file, its frame depth computed per Python version rather than hardcoded.
- **`SchemaAttacher`'s per-operator schema-guessing rules are gone (tier 2).** They were corrected one construct at a time through this release's development — `project`/`project-*` provenance, join/union column collisions, `mv-expand`'s item-index column, multi-output aggregate naming, and more — and every one of those corrections is now retired; see `### Changed` for what replaced them and what that moved in `semantic_hash`. What the pass still supplies: table provenance, `$left`/`$right` inside a join's `on` clause, `let` threading, and the body scopes described under `### Changed`.
- **The CLI now emits UTF-8 on every platform (tier 1).** Non-ASCII query text no longer raises `UnicodeEncodeError` on Windows.
- **Aggregate and `summarize ... by` grouping auto-names now come from Microsoft's own `ResultNameKind`/`ResultNamePrefix` symbol properties (tier 2)**, not a hand-written prefix table — irregulars (`buildschema(d)` → `schema_d`, `argmax`/`argmin` (`argmax_t` → `max_t`), `binary_all_and(a)` → `a`, and family) are named as the engine names them, in both bind states.
- **Everything a query *declares* now reaches the IR and the digest (tier 2).** A `let`-declared function carries its body, parameter types, defaults and `view` keyword — closing the let-function collisions the hash battery had filed (body, parameter type, parameter default), and `find_all(ir, TableRef)` sees inside a body, so the two tiers no longer disagree about a function-declaring query. So do the statement kinds that are neither `let` nor tabular: `QueryIR.statements` holds `set`, `declare query_parameters`, `declare pattern`, `alias database` and `restrict access to` in source order (order is hashed too, since `set` scopes what follows it) as `SetOptionStmt`/`QueryParametersStmt`/`PatternStmt`/`AliasStmt`/`RestrictStmt`, plus a defensive `UnknownStmt`, with `LetFunction.body_query_parameters` for the one kind a function body admits. Before this, two *different values of the same statement* collided with each other. A `let` written inside a function body or a `declare pattern` arm is scoped to that body (`LetFunction.body_lets`, `PatternMatch.body_lets`) instead of being hoisted into `QueryIR.let_bindings`.
- **Microsoft's binder crashing no longer reaches the caller as a CLR traceback (tiers 1 and 2).** A `declare pattern` whose match arm supplies more values than the declaration has parameters parses clean and then throws `IndexOutOfRangeException` out of `VisitPatternDeclaration` (Kusto.Language 12.3.2 through 12.4.1). Every analyze seam — `parse(schema=…)`, `validate(schema=…)`, `to_ir()`, `IRBuilder.build()` — now falls back to the unanalyzed parse and reports one `Error` diagnostic with the kustology-owned code `KUSTOLOGY001`. `semantic_hash` is bind-invariant, so the fallback digest is the right digest.

### Known limitations

- `semantic_hash` can differ between a bound and an unbound parse of a query whose `let` aliases a table — accepted rather than papered over, since proving it needs the binder.
- **`ColumnN` default names are a deliberate divergence** (grouping keys and unnamed `extend`/`project` columns alike): Microsoft's ``ColumnN`` counter is query-global and scope-sensitive, unreproducible bind-stably; first-bare-column naming is deterministic and stable. See ``_auto_name``'s grouping mode for the full list and design rationale.
- **A local name is matched by text, where KQL resolves a column first.** A `let` name or a function parameter that shadows a same-named real column is still classified as the local one — a `LetValueRef`, or a reference the parameter rename rewrites — so the shadowed column stays invisible to `find_all` and two queries reading different things share a digest; the same holds when an earlier `extend` is what creates the column. Provenance has the mirror of it: a *scalar* parameter sharing a column's name can still resolve against that column and report a table it never came from (masking closes the table case, not this one). Classifying by symbol instead would need a bound parse, which would make the digest depend on bind state — see `LetValueRef`'s docstring for the trade and `binder.enrich`'s for the provenance half.
- **The call-site rename is conservative where a binding aliases another.** `let g = f; g()` leaves `g` as written, because only a `let` whose right-hand side *is* a function renames its call sites, so that spelling splits from `let f = …; f()` rather than merging with it. A split is the safe direction — a dedup consumer keeps two entries instead of dropping one — but it is a boundary of the rename invariant `### Breaking` states.
- **`restrict access to`'s targets are not table references.** A bare entity name in the list arrives as a `ColumnRef` (a `LetValueRef` when a `let` bound it), because that is the position the grammar puts it in, so `find_all(ir, TableRef)` does not report a restrict target and a lineage consumer reading only that will not see one. See `RestrictStmt`.

The digest boundaries above — bind state, the textual shadow, and the conservative call-site rename — are worked through in README's "What `semantic_hash` deliberately ignores" section; `ColumnN` and `restrict access to` carry their own pointers.

### Internal

- `tests/test_reflection_audit.py` asserts every reflected .NET member name actually exists.
- Corpus coverage gates now walk the whole `QueryIR` via `find_all` (including `let` right-hand sides) instead of hand-maintained attribute lists; `tests/ir/test_binder_oracle.py` is a new gate comparing our `result_schema` against Microsoft's own `ResultType`, column-by-column and in order, across an operator matrix and the fixture corpus, on both the bound and dict-path entry points.
- CI lints `examples/` and every job installs from `uv.lock`. The IR test matrix now runs the full suite on every cell (previously one) and adds Python 3.13, with `fail-fast: false`; a locale/timezone leg (`de_DE`, `fr_FR`, `en_US` + `TZ=Asia/Tokyo`) guards the culture pin and the datetime-`Kind` fix.
- A weekly upstream canary (`.github/workflows/canary.yml`) resolves `pyproject.toml`'s dependency ranges fresh, catching what the pinned `uv.lock` hides. `.github/dependabot.yml` declares the update cadence and grouping that previously lived only in repository settings. `release.yml` now hard-gates on the offline DLL pin.
- The test suite was deduplicated against the hash battery as the single pair registry, and mechanically-parametrized guards were collapsed to looped equivalents; behavioral coverage is unchanged.
- Docstrings are gated rather than optional: six public `KustoQuery` members gained one (`get_operator_chain`, `get_referenced_columns`, `get_referenced_functions`, `get_structural_hash`, `syntax`, `text`) and a test now fails if a public member ships undocumented; `build_global_state` and `TabularSchema.columns` gained ones clarifying raw schema-key names and the `"unknown"` type sentinel, behavior unchanged in both cases.
- Dependency refresh ahead of the tag: the bundled `Kusto.Language.dll` 12.3.2 → 12.4.1 (latest stable on nuget.org, with `[tool.kustology] kusto_language_version` and `bin/VERSION.txt`'s SHA-256 re-pinned and the provenance chain re-verified via `scripts/verify_dll.py`; no digest movement, and the pattern-arity binder crash above is unchanged by it), plus pythonnet 3.1.0, pytest 9.1.1, ruff 0.16.4, mypy 2.3.1, cyclonedx-bom 7.3.1, twine 7.0.0 and the pinned GitHub-action group — verified by the full CI matrix and a local gate run (suite, corpus, examples, locale/TZ leg, build + `twine check`).

## [0.1.0] — 2026-06-01

First public release.

### Stability

This is a `0.y` release — per SemVer §4, the public API is not yet considered stable.

Two version numbers describe the surface:

- **Package version** (`__version__`, displayed by `kustology version`): the overall library version. Pre-1.0, both Tier 1 and Tier 2 may break at minor versions.
- **`kustology.ir.IR_SCHEMA_VERSION`**: the IR shape's own version, used to tag serialized IR JSON so consumers can refuse to load an incompatible payload. Currently bumps in lockstep with the package version.

Tier 1 (`kustology` top-level surface) is on a stabilization track: the package goes to 1.0 once Tier 1 survives external use without correctness breaks. Tier 2 (`kustology.ir.*`) is expected to keep evolving at package-minor cadence after 1.0 — its breaks are tracked by `IR_SCHEMA_VERSION` and called out in this CHANGELOG.

### Tier 1 — thin .NET wrapper

- `parse(query, schema=None)` and `KustoQuery` for syntactic plus optional semantic analysis.
- `format_query(query)` for canonical reformatting via Microsoft's public `KustoCodeService.GetFormattedText`.
- `validate(query, schema=None, ignore_unknown_tables=False)` for structured parser diagnostics (severity, code, source offset).
- AST analyzers on `KustoQuery`:
  - `get_referenced_tables(force_syntactic=False)` — every table source, including joined, union'd, lookup'd, and `database()` / `cluster()` cross-cluster references.
  - `get_referenced_columns(force_syntactic=False)` — every column reference, with function callees and `$`-prefixed join sides filtered out.
  - `get_referenced_functions(force_syntactic=False)` — every function callee. Semantic mode resolves through `FunctionSymbol`; syntactic mode falls back to `NameReference` nodes in callee position.
  - `get_operator_chain()` / `get_operator_stats()` — ordered pipeline of operators and a `{OperatorKind: count}` map.
  - `get_structural_hash()` — SHA-256 over the AST node-kind sequence; stable across literal and whitespace changes.
  - `get_time_range()` — temporal expressions with source offsets.
  - `replace_table(old, new)` — AST-aware rename across every reference position (sub-pipelines, lookups, let-bound bodies).
- `kustology.utils.analysis.collect_nodes(syntax, predicate)` — reusable single-pass walker that returns every node satisfying a predicate. Hides `KustoWalker` boilerplate so a new analyzer is one lambda instead of a five-line subclass.
- `kustology` CLI with subcommands `version`, `format`, `validate`, `parse`. `parse` supports `--ast` (default) and `--ir` (requires the `[ir]` extra). Exit codes: `0` success, `1` Error-severity diagnostics or runtime failure, `2` usage error (bad flags, missing file, missing extra).
- CLI input capped at 10 MB by default; override with the `KUSTOLOGY_MAX_INPUT_BYTES` environment variable. Inputs exceeding the cap exit with code 2.
- CLI `parse --ast` and `parse --ast --json` cap recursion depth at 1000 levels — past that, the node emits a `truncated` marker rather than blowing the Python stack. AST emitters use `str(node.Kind)` (the `SyntaxKind` enum value) for kind labels, matching the audit script's convention.
- `kustology.reflection` — runtime reflection on `Kusto.Language.Functions` drives categorized listings: `time_functions()`, `aggregate_functions()`, `scalar_functions()`, `string_functions()`, `all_function_names()`, plus `syntax_kinds()` for the `SyntaxKind` enum. No hard-coded fallback — a reflection failure is loud, not silent.
- `__version__` exposed at runtime via `importlib.metadata`.
- Bundled `Kusto.Language.dll` (12.3.2) pinned by SHA-256; refresh and verify scripts in `scripts/`.

### Tier 2 — semantic IR (opt-in via `[ir]` extras)

- `kustology.ir.QueryIR` — Pydantic root model holding a `Pipeline` of typed operators (`FilterOp`, `SummarizeOp`, `JoinOp`, `LookupOp`, `ProjectOp`, `ExtendOp`, …) and typed expressions (`BinOp`, `FuncCall`, `SetMembership`, `Between`, `And`, `Or`, …). 53 operator types and 23 expression types covering the Kusto query surface seen in real-world Sentinel detections. (0.2.0 keeps the 53 and takes the expressions to 25 — `MaterializeExpr` out, `LetValueRef`, `TypedNameDecl` and `SubqueryExpr` in.)
- `kustology.ir.IRBuilder.build(query)` parses, binds, and builds the IR in one call.
- `kustology.ir.IRBuilder.build_from_code(code)` builds from a pre-parsed `KustoCode`. `KustoQuery.to_ir()` uses this so callers don't pay for two parses.
- `KustoQuery.to_ir(attach_schema=...)` — controls the `SchemaAttacher` pass that materializes column provenance and `Pipeline.result_schema`. Default `None` auto-attaches when the parse was bound with a schema, so `parse(query, schema=...).to_ir()` returns a fully enriched IR without restating the schema. `True` forces attach with the parse-time schema; `False` skips even on a bound parse; a `dict` overrides for the attach step only.
- `kustology.ir.SchemaAttacher(schemas)` — propagates Microsoft's binding results into Pydantic fields (`Expr.result_type`, `ColumnRef.table`) and computes `Pipeline.result_schema`. Takes a flat `{table: {col: type}}` dict.
- `kustology.ir.walk(node)` — depth-first, pre-order traversal yielding every Pydantic `BaseModel` descendant.
- `kustology.ir.walk(node, predicate=...)` — predicate-filtered traversal for cross-cutting filters that don't reduce to a type (e.g. "every case-insensitive `BinOp`").
- `kustology.ir.find_all(node, type_)` — type-filtered traversal. The one-liner most custom analyzers reduce to: `find_all(ir, FilterOp)` returns every filter regardless of where it lives (main pipeline, join RHS, let-bound sub-pipeline).
- `kustology.ir.compute_semantic_hash(node)` — SHA-256 over the canonical IR shape (post-transforms, with spans and bind-time annotations stripped), prefixed with the scheme tag `kustology-sem-v1:` so the canonicalization rules are versionable. Accepts any IR subtree, not just `QueryIR`. Computed once at build and stored on `QueryIR.semantic_hash`; call directly to refresh after mutating the IR.
- Opt-in canonicalization transforms (`kustology.ir.transforms`):
  - `merge_consecutive_filters(root)` — fold consecutive `FilterOp`s into one whose predicate is an `And` of the originals. Recurses into sub-pipelines.
  - `normalize_expressions(root)` — apply semantic-preserving rewrites (`tolower(X) == "y"` → `X =~ "y"`, nested `And` / `Or` flattening, `not(not(X))` collapse). Post-order so deep nesting collapses cleanly.
- `QueryIR.to_llm_dict()` — lossy projection optimized for handing the IR to a language model: every node carries a `kind` discriminator, spans and defaulted fields are stripped, and `polarity` is collapsed into natural KQL operators (`!=`, `!in`, `!between`). Running today's fixture corpus back through the 0.1.0 code (48 of the 49 build under it), the view was a median 58% smaller than `model_dump_json()` on a schemaless parse and 42% on a bound one. (For the 0.2.0 figure see `to_llm_dict` under **Changed** above.)
- `kustology.ir.UnknownExpr` / `UnknownSource` / `UnknownOp` — explicit fallback nodes for shapes the builder doesn't model. The coverage audit (`scripts/audit_syntax_kinds.py`) fails CI when new shapes appear after a `Kusto.Language` DLL upgrade.
- `kustology.ir.Finding` / `AnalyzerFn` / `Severity` — minimal analyzer-output protocol so independently-developed analyzers share a `Finding` vocabulary.
- `kustology.ir.IR_SCHEMA_VERSION` — IR shape version, decoupled from the package version.
- `kustology.ir.KustoType` — `StrEnum` for column and expression types (`STRING`, `LONG`, `INT`, `DATETIME`, `TIMESPAN`, …). `UNRESOLVED` for types the binder hasn't placed.
- `ImplicitSource` source variant for sub-pipelines whose row context comes from parent operators (union-at-root, mv-apply / partition subqueries, join / lookup RHS).
- Every IR `BaseModel` declares `extra="forbid"` for strict validation.
- `kustology.utils.schema_state.extract_schemas_from_global_state` — inverse of `build_global_state`; recovers `{table: {col: type}}` from a bound Microsoft `GlobalState`.
- `IRBuilder.HANDLED_OPERATOR_KINDS` / `HANDLED_EXPR_KINDS` — public attributes that the coverage audit reads as contract.

### Infrastructure

- CI matrix: Linux on Python 3.10, 3.11 and 3.12, plus one Windows and one macOS cell on 3.12. Not a full cross-product — the two non-Linux cells are sanity checks.
- Coverage audit (`scripts/audit_syntax_kinds.py`) fails CI on new uncovered `SyntaxKind` after a DLL refresh.
- Corpus regression (`scripts/mine_corpus.py`) over the bundled fixture corpus (33 queries at 0.1.0), failing CI when the builder falls through to an `UnknownExpr` / `UnknownSource` / unspecialized `Operator`. A second, soft job mines Microsoft's own KQL corpus for the same signal without gating the build. `scripts/verify_corpus.py` is a **maintainer diagnostic**, not a gate: it runs against a local, gitignored Sentinel sample that is not in the repository and nothing in CI invokes it.
- DLL provenance verified on every push (`scripts/verify_dll.py`).
- PyPI publish workflow (`.github/workflows/release.yml`) — triggered by `v*` tags, builds via `python -m build`, generates a CycloneDX SBOM, publishes via PyPI trusted publishing, attests the artifacts, and creates a GitHub release with the dist and SBOM attached.
- `.pre-commit-config.yaml` mirroring CI for local fail-fast.

### Documentation

- `README.md` — install, two-tier overview, Quick Start, CLI reference.
- `ARCHITECTURE.md` — tier layout and contribution pointers.
- `CONTRIBUTING.md` — workflow and coding conventions.
- `SECURITY.md` — private vulnerability reporting and bundled-DLL verification (offline hash, NuGet re-fetch, DLL refresh).
- `AGENTS.md` — non-obvious technical context for agents modifying the code (CLR interop, AST navigation, IR gotchas, DLL provenance).
- 7 runnable examples in `examples/`:
  - `linter.py`, `binding_comparison.py`, `query_analysis.py` — Tier 1 analysis demos.
  - `walk_tree.py` — AST traversal via `KustoQuery.syntax`.
  - `walk_ir.py` — typed IR traversal (mirror of `walk_tree`).
  - `find_all_demo.py` — generic IR traversal via `find_all`.
  - `llm_view.py` — LLM-tailored IR serialization via `to_llm_dict`.

[Unreleased]: https://github.com/k4otix/kustology/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/k4otix/kustology/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/k4otix/kustology/releases/tag/v0.1.0
