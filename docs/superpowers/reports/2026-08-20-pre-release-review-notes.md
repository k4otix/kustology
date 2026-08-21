# Lead's own verified findings (before agent reports)

All reproduced with .venv/bin/python on main @ 26a5da3.

## Crashes
- L1 CRITICAL: `let n = 10; T | take n`, `T | take toscalar(U | count)`, `let n=5; T | top n by x` -> ValueError from `safe_int` (builder.py TakeOp/SampleOp/TopOp/TopHittersOp/SampleDistinctOp). Whole IR build raises on valid KQL. Fix: `count: int | AnyExpr` (or `count_expr: AnyExpr` + `count: int|None`), never raise.

## Wrong values (bound)
- L2 HIGH: `$left.b` after two joins resolves to the wrong table. `T | join U on k | join V on $left.b == $right.b` with b in T,V (not U) -> ColumnRef(b, join_side=left).table == "U". binder.py `_fill`: `$left` -> `scope[-2].table` blindly; must resolve by column name over scope[:-1]. Also after `extend` before join, `$left.a` stays "$left" (missing, not wrong).
- L3 HIGH: JoinOp.join_kind defaults to "inner" when unspecified; KQL default is innerunique. `T | join U on k` hashes equal to `kind=inner` and unequal to `kind=innerunique` (the opposite of the truth). LookupOp.lookup_kind defaults to "inner"; KQL lookup default is leftouter. Field is `str | None` -> emit None when absent (or the true default).

## Hash soundness
- L4 HIGH: `normalize_in_place` rewrites `tolower(X) == R` -> `X =~ R` for ANY R. `tolower(X) == "Y"` (always false) hashes equal to `X =~ "Y"`; `tolower(X) == Col` hashes equal to `X =~ Col`. Unsound; baked into compute_semantic_hash. Fix: only fire when R is a string literal that is already lowercase (R == R.lower()); optionally handle toupper symmetric.
- L5 MEDIUM: `LetFunction.body_span` is not stripped (`_VOLATILE_FIELDS` strips "span" only) -> a comment before `let f = (x:int){x+1}; T | extend y=f(a)` changes semantic_hash. Formatting invariance broken for any query with a let-function. Fix: strip body_span (or rename to `span`-suffixed key handled generically; or hash the body text instead).
- L6 HIGH (lossy lowering / collisions): identical IR + hash for:
  - `sort by x asc` vs `desc`; `nulls first` vs default; `top 5 by x asc` vs `desc` (OrderedExpression unwrapped, direction dropped). SortOp.expressions: list[AnyExpr]; TopOp.by: AnyExpr.
  - `mv-expand x` vs `to typeof(string)` vs `limit 10`; with_itemindex / bagexpansion dropped.
  - `parse kind=regex` vs `kind=simple`; `parse x with "a" b:long` vs `b` (type dropped; NameAndTypeDeclaration -> ColumnRef).
  - `join hint.strategy=shuffle` vs none (acceptable — hint), but `union withsource=Src T1,T2` drops withsource (a real output column!), `kind=outer/inner`, `isfuzzy`.
  - `render timechart with (title=...)` vs without (acceptable, display only — document).

## Misrepresentation / classification
- L7 HIGH: `externaldata(...)[...] | where ...` as a pipeline ROOT -> Pipeline.source = ImplicitSource (it is an explicit external source). `_visit_pipeline.walk` has no ExternalDataExpression branch. Fix: add a source variant (ExternalDataSource or allow ExternalDataExpr-as-source) or wrap.
- L8 HIGH: a `let`-bound SCALAR referenced in an expression is a ColumnRef: `let threshold = 5; T | where Count > threshold` -> ColumnRef(threshold) (bound: table=None, result_type=long). Lineage consumers will think it is a column. Builder already tracks `_let_names`; emit a distinct node (e.g. `LetRef`-like scalar ref / `VarRef`) or a flag on ColumnRef. Same for function parameters inside let-function bodies (body not modeled, so n/a).
- L9 MEDIUM: `union Sec*` -> UnionOp pipelines [TableRef(name="Sec*")] — wildcard modeled as a table name. Document or add `is_wildcard`/pattern field.
- L10 LOW: `datatable(...)` -> FuncCallSource(name="datatable", args=[]) — schema and rows dropped.

## Schemaless population gap / inconsistency
- L11 MEDIUM (Enhancement): `parse(q).to_ir()` leaves literal and built-in-function result_type UNRESOLVED (`1h`, `1.5`, `ago()`), while `IRBuilder().build(q)` (ParseAndAnalyze with GlobalState.Default) resolves them (timespan/real/datetime) but adds KS204 "unknown table" Error diagnostics. Two "schemaless" entry points produce different annotations and different `diagnostics`. Hash is equal (result_type stripped). Cheap fix: have the builder set literal result_type from literal_kind (no binder needed) and/or run SchemaAttacher({}) fallback in to_ir; document the diagnostics difference.

## Provenance design limit
- L12 MEDIUM: after any `project`/`summarize`/`distinct`/`project-*`, scope becomes anonymous (table=None) so `T | project a, b | where a > 1` gives the second `a` table=None (result_type long comes from the .NET binder). README sells "which source table a column came from after joins, renames and let aliases" — provenance is lost by the most common operator. Enhancement: ProjectOp/Distinct/ProjectKeep/Away/Reorder/Rename should carry per-column provenance forward for bare ColumnRef projections (keep a {col: table} map in ScopeEntry or multiple entries).

## Canonical-form ambiguity (low)
- L13 LOW: canonical() drops BracketedExpr and renders And/Or without parens -> `a and (b or c)` and `(a and b) or c` have the same canonical_form string (hash differs because it is over the model dump). AGENTS.md says "And … (and other commutative ops) canonical_form sorts operands" — only And/Or/SetMembership values are sorted; BinOp `==` operands are not. Docs overstate.

## Agent A (builder) — verified by lead
- A1 fork branches -> UnknownSource, 0 operators (Critical) ✔
- A2 top-hitters AttributeError 'ValueExpression' (Critical) ✔ ; TopHittersOp lacks `of`
- A3 = L1 take/sample/top non-literal count crash ✔
- A4 only first tabular statement modeled; `T | count; U | count` drops U, 0 diags (High/Medium) ✔
- A5 = L6 sort/top direction ✔
- A6 = L8 let-scalar as ColumnRef ✔
- A7 named params/clauses dropped (union kind/withsource/isfuzzy, mv-expand *, parse kind/typed, search kind/in(...), find in/project, make-series default/in range, render with) ✔ ; search/find in(...) yield zero TableRef
- A8 hints dropped (Enhancement)
- A9 `search Col:'x'` BinOp(op=":", case_sensitive=True) wrong; KQL := has (insensitive) ✔
- A10 raw_text-only ops undocumented (scan, top-nested, make-graph, macro-expand, graph-*) 
- A11 externaldata uri keeps h' prefix; only first URI ✔
- A12 CompoundStringLiteralExpression -> UnknownExpr ✔
- A13 dead AndExpression/OrExpression branch; HANDLED_EXPR_KINDS mixes SyntaxKind/class names
- A14 wildcard union -> TableRef("T*"), database('d').* -> TableRef("*")
- A15 isnull/isempty asymmetry (Enhancement)
- A16 case_sensitive True on arithmetic (Enhancement)
- NOTE: A's "verified OK" says join default inner is correct — it is NOT (KQL default innerunique); keep L3.

## Agent B (binder) — verified by lead
- B-C1 join kind ignored for semi/anti: leftanti/leftsemi keep RHS cols; rightanti/rightsemi wrong cols AND wrong type for `shared` (Critical) ✔
- B-C2 union type conflicts collapsed (EventID:int + EventID:string -> EventID:string; MS splits EventID_int/EventID_string); `union withsource` column missing (Critical) — not re-run but consistent with L6/A7
- B-C3 = L2 multi-join $left ✔
- B-C4 `on k` bare key attributed to RIGHT table (matches[-1]); MS binds left; with L.k:string R2.k:long -> table=R2, result_type=string (contradictory) ✔
- B-C5 project-keep Foo* -> {} (all dropped); project-away Foo* -> nothing removed (Critical) ✔
- B-C6 mv-expand type inference inverted: pack_array col -> long (MS dynamic); `to typeof(string)` ignored (MS string) (Critical) ✔
- B-H1 arg_max(t,*) -> single `arg_max_t: unknown`; MS: t + all cols. 62 of 62 corpus type divergences (High) ✔
- B-H2 auto-names: make_set_s (MS set_s), take_any_n (MS n), percentile_n (MS percentile_n_95); make_list/make_bag same (High) ✔
- B-H3 typed parse capture `av:long` -> string (High)
- B-H4 project/distinct/keep/reorder ignore ColumnRef.result_type -> "unknown" when binder knew (High, cheap fix)
- B-M1 = L6/A7 collisions
- B-M2 SchemaAttacher docstring taxonomy wrong: search adds $table; getschema/scan/find/datatable/externaldata/as unlisted
- B-M3 parse-kv declared columns not applied to scope (3 lines)
- B-M4 fabricated result_schema on fork branches (UnknownSource, 0 ops, yet result_schema inherited)
- B-M5 ambiguous provenance last-wins silent
- B-M6 schema_attached=True on unbound parse with attach_schema=True and no dict
- B-E1 schemaless: 0/3177 typed via parse().to_ir(), 2081 (65.5%) via IRBuilder().build (GlobalState.Default, zero diagnostics for built-ins per agent; NOTE my run showed KS204 for unknown table T — consistent: KS204 is for the table) ; README "no partial binding" misstates MS API (= L11)
- B-E2 use operator node ResultType (binder) for result_schema on bound parses — architectural recommendation (strong)
- B-L1 BinderEnricher undocumented alias in __all__
- B-L2 schema input edge cases: "LONG" -> string w/ RuntimeWarning; None -> CLR ArgumentNullException; "" -> InvalidOperationException; "['my col']" literal
- B-L3 "unknown" vs "unresolved" sentinels
- test_binder.py asserts names/order only, never types; no oracle vs code.ResultType

## Tier 1 trivia bug (found via B's side note, verified by lead)
- T1 HIGH: syntactic analyzers use `node.ToString().strip()` on NameReference; .NET ToString() includes leading trivia, so a `//` comment line immediately before a name is glued on:
  - `get_referenced_tables()`: "// a comment\nSecurityEvent | take 1" -> {'// a comment\nSecurityEvent'}; 3/33 fixtures affected (ADFSRemoteHTTPNetworkConnection, Cross_tenant..., IPEntity_AzureFirewall); join RHS "(\n// c\nSigninLogs)" -> '// c\nSigninLogs'
  - `get_referenced_columns()`: "project\n// c\nb" -> '// c\nb'
  - `get_referenced_functions()`: "a > // c\nago(1d)" -> '// c\nago'
  - `replace_table("SecurityEvent","NewTable")` on "// a comment\nSecurityEvent | take 1" returns the text UNCHANGED (silent no-op — same class the CHANGELOG says was fixed for find/search)
  - semantic mode unaffected (uses symbols). Fix: use `node.Name.SimpleName` / `node.SimpleName` (NameReference) or `ToString(IncludeTrivia.Minimal)`; audit every `.ToString().strip()` on name nodes in analysis.py (lines 140, 173, 272, 284, 319, 370, 375, 390). `find_time_expressions` returned clean text in my probe (offsets TextStart-based) but uses ToString too — check.
  - `find_time_expressions()`: "TimeGenerated > // c\nago(1d) and x == // c2\n1h" -> [('1d',35,2), ('// c2\n1h',54,2)] — the ago() call is DROPPED (callee text '// c\nago' fails the _TIME_FUNCS check) and the literal's text carries the comment while width stays 2.
  - `get_operator_chain()` includes the root NameReference (source) as its first "operator" — check with agent C's report.

## Agent C (Tier 1) — verified by lead
- C-C1 = T1 trivia bug (tables/columns/functions/time/replace_table) ✔ — zero tests contain a comment
- C-C2 `let SecurityEvent = SecurityEvent | …` -> syntactic tables = set(); replace_table silent no-op (High) ✔
- C-H1 bound parse: tables absent from schema vanish from get_referenced_tables(); replace_table silent no-op for them (High) ✔
- C-H2 get_structural_hash: `if "Token" in kind` also skips TokenLiteralExpression -> join kind=inner == leftanti; union kind; mv-expand kind; evaluate bag_unpack == pivot (High) ✔ ; docstring understates identifier-blindness
- C-M1 CLI depth guard (1000) unreachable (RecursionError at ~988 first); library analyzers/to_dict have no guard
- C-M2 time_functions() misses bin/bin_at (first-overload return type) -> find_time_expressions drops bin(...) despite docstring ✔
- C-M3 all_function_names() misses tostring/gettype/all (dir() shadowing) ✔
- C-M4 syntactic tables include fn params, `as` aliases, `T*` wildcard, bracket-quoted ['my-table'] unstripped (mode-dependent replace_table)
- C-M5 syntactic columns drop a column whose name matches any table in query; JSON path segments reported as columns
- C-M6 CLI missing file / bad --schema -> exit 1 not 2 (README says 2) ✔
- C-M7 `kustology parse`/`format` exit 0 on Error diagnostics (README says 1) ✔
- C-M8 parse(schema="(a:string)") documented+typed but raises TypeError ✔
- C-M9 format_query repairs invalid KQL silently, non-idempotent on broken input
- C-L1 scalar_functions ∩ aggregate_functions = any, hll_merge, merge_tdigest, tdigest_merge
- C-L2 CLI --ir --json untagged (no IR_SCHEMA_VERSION envelope)
- C-L3 CLI parse --ir has no --schema, bypasses to_ir (second parse, never enriched)
- C-L4 get_operator_chain (main only, includes root NameReference) vs get_operator_stats (whole AST); three kind vocabularies
- C-L5 KUSTOLOGY_MAX_INPUT_BYTES counts chars not bytes
- C-L6 KustoQuery has no diagnostics property; 7 methods lack docstrings
- C-L7 plugins (46) not in any reflection category; get_referenced_functions returns them
- C-L8 replace_table doesn't validate/quote new_name
- C-L9 find_time_expressions double-reports nested calls
- C-L10 PackageNotFoundError leaks into kustology namespace
- C-L11 schema_state RuntimeWarning stacklevel wrong
- get_referenced_columns conflates read vs created columns (both modes) — undocumented

## Agent F (tests/CI/packaging) — verified by lead where marked
- F-F1 = A2 top-hitters crash (one-word fix n.ValueExpression -> n.OfExpression... but TopHittersOp also needs `of` field)
- F-F2 corpus gates `type(op) is Operator` blind to UnknownOp (test_complex_harness.py:86, mine_corpus.py:85, verify_corpus.py) ✔
- F-F3 [build-system] setuptools>=61.0 but PEP 639 `license="Apache-2.0"`+license-files needs setuptools>=77 (agent built with 76.1.0 -> error; 77.0.3 OK). Release blocker for sdist consumers.
- F-F4 release.yml publishes without running pytest (test.yml has no tag trigger) ✔ ; F-F5 no tag↔version↔CHANGELOG guard; notes extracted AFTER publish (F12)
- F-F6 reflection audit ignores direct attribute access (ast.Attribute) — would have caught ValueExpression
- F-F7 ~20 builder operator branches zero coverage (PartitionBy, ProjectByNames, Sample, SampleDistinct, TopHitters, As, ExecuteAndCache, Invoke, Facet, Fork, AssertSchema, Graph*, Scan, TopNested, MakeGraph, Evaluate fallback)
- F-F8 cli.py 0% measured coverage (subprocess tests)
- F-F9 sdist ships 12 top-level tests only (no conftest/ir/fixtures/examples/CHANGELOG) -> vacuous 102-pass suite ✔ (13 entries)
- F-F10 verify_dll.py network failure exits 1 (= tamper code) not 2; no --offline
- F-F11 verify_corpus.py unconditional return 0; CHANGELOG 0.1.0 advertises it as a gate; not wired into CI
- F-F13 py3.13 supported by pythonnet 3.0.5 (<3.14) but absent from CI/classifiers; IR tests only on ubuntu/3.12; no upper bound on requires-python
- F-F14 IR_SCHEMA_VERSION unguarded by any test (mutation to "9.9-BOGUS" -> 442 pass)
- F-F15 canonical_form "every shape" test covers 6/23 types (all 23 do render today)
- F-F16 handoff F3 gate shipped narrower than promised
- F-F17 scripts robustness (extract_sentinel_schemas writes {} before failing; sample_sentinel_corpus exit 0 on bad dir; verify_corpus UnicodeDecodeError; refresh_dll not atomic; no TFM pin in DLL scripts)
- F-F18 README relative links 404 on PyPI (CONTRIBUTING/LICENSE/NOTICE/SECURITY/THIRD-PARTY-NOTICES)
- F-F19 .claude/settings.local.json not in repo .gitignore
- F-F20 CI: dependency-review needs pull-requests:write for comment; release SBOM scans build machine (no path); release pip install unpinned; fail-fast true on platform matrix; harden-runner audit on publish; pre-commit pydantic floating
- Test-gap: IR_SCHEMA_VERSION none; DEBUG logging none; extra=forbid "lacks new required fields" direction none; canonical partial. Culture pin mutation-proven (11 fails under de-DE without pin).
- Coverage 84%; cli 0%, bridge 57%, builder 81%.
- Packaging: wheel clean; twine check pass; clean-venv install OK; metadata OK.
- Go/no-go: BLOCK on F1 (=A2), F3, F4, F5; maintainer must verify PyPI trusted publisher (workflow filename release.yml, env `pypi`).

## Agent D (hash/canonical/llm/walk) — verified by lead where marked
- D-F1 CRITICAL semantic_hash + LiteralExpr.value + ticks are host-TIMEZONE dependent for datetime literals (DateTimeKind.Local): TZ=UTC/NY/Tokyo -> 3 hashes; datetime(2024-01-01) != datetime(2024-01-01T00:00:00Z) ✔ Fix: ToUniversalTime()/SpecifyKind(Utc) before "o"/Ticks; add TZ leg to CI.
- D-F2 = L6 sort/top direction
- D-F3 = L4 tolower rewrite unsound; also asymmetric (`"y" == tolower(X)` not rewritten); no toupper
- D-F4 = A1 fork
- D-F5 datatable args=[] hardcoded -> all datatable queries collide (High)
- D-F6 = L7 externaldata root source dropped (hash collision too)
- D-F7 database()/cluster() qualification dropped from TableRef -> cross-db/cluster same-named tables collide (High)
- D-F8 raw_text (with leading trivia) hashed for ScanOp/TopNested/MakeGraph/MacroExpand/Graph*/Unknown* -> whitespace/comment sensitivity ✔
- D-F9 _strip_volatile_fields strips by KEY NAME in any dict -> AssertSchemaOp/ParseKvOp columns named table/span/result_type deleted from hash ✔
- D-F10 canonical_form no parens: `a and (b or c)` == `(a and b) or c` string (hash OK) — LLM fidelity (= L13)
- D-F11 canonical() doesn't escape string literals
- D-F12 root Not(Not(X)) not collapsed (normalize_expressions discards replacement at root) -> compute_semantic_hash(expr) two ways ✔
- D-F13 walk/find_all yields same object twice via LetBinding.inner_time_exprs aliasing ✔ (['ago','now','ago','now'])
- D-F14 = A7 modifiers
- D-F15 = L1/A2 crashes
- D-F16 canonical() renders True/None (Python repr) not true/null
- D-F17 hash is operand-order-sensitive for And/Or/SetMembership values while canonical_form sorts them — `where A and B` vs `where B and A` do NOT dedup. Decision needed.
- D-F18 let alias names hashed (alpha-renaming breaks dedup) — undocumented
- D-F19 h"x" == "x" hash (decide/document); D-F20 typed nulls collapse
- D-F21 canonical "?" fallback unguarded; test iterating Expr subclasses
- D-F22 LetFunction.body_span leaks into llm view as {"kind":"Span"} (also = L5 hashed)
- D-F23 llm_view docstring bullet about renaming kind fields is stale
- D-F24 llm view lacks ir_schema_version
- D-F25 no analyzer example
- Verified OK: bind invariance 50 queries (2 documented divergences); build-time == recomputed 33/33; merge/normalize idempotent; llm view JSON-safe; collapsed ops valid KQL; 46.2% smaller (CHANGELOG says ~50%).

## Agent E (docs/examples) — verified by lead where marked
- E-E1 = D-F1 TZ-dependent hash; CHANGELOG "machine-dependent fixed" + "ticks lossless" claims now false
- E-E2 HIGH `SubqueryExpr.pipeline: Any` AND `ToScalarExpr.pipeline: Any` -> model_validate_json reloads a dict; walk 19->12 / 23->12; find_all loses inner ColumnRefs; README "model_dump_json (lossless)" false ✔ Fix: type as "Pipeline | None" + model_rebuild.
- E-E3 = C-M6 CLI missing file exit 1 vs documented 2 (README, CHANGELOG, cli.py docstring)
- E-E4 = L11/B-E1 README "no partial binding" false of MS API; IRBuilder default uses GlobalState.Default
- E-E5 PR template lint cmd omits examples/
- E-E6 llm_view.py + examples/llm_view.py say `result_type=unknown`; it's `unresolved`
- E-E7 AGENTS.md "bump on any breaking change" vs CONTRIBUTING/README "once per release"
- E-E8 ARCHITECTURE "Tier 1 SemVer-stable" vs README/CHANGELOG "either tier may break pre-1.0" (Tier 1 did break: get_time_range)
- E-E9 stub-sweep report disposition markers stale (4 of ~24 marked)
- E-E10 query_analysis.py prints TEXAS/OHIO claim about a literal not in its query
- E-E11 CHANGELOG 0.1.0 "26 expression types" never true (23)
- E-E12 ARCHITECTURE scripts/ list omits mine_corpus.py, extract_complex_corpus.py
- E-E13 binder.py inline comment "17 of 53 / other 36" vs docstring 18 / 35
- E-E14 enrich docstring "Two boundaries" lists three
- E-E15 CHANGELOG no link-reference footer
- E-E16 CHANGELOG Internal omits canary workflow + dependabot
- E-E17 README CLI block omits validate --schema/--ignore-unknown-tables, parse --json
- E-E18 "CI matrix: macOS × Linux × Windows × Python 3.10+" overstates
- E-E19 KustoQuery.replace_table/get_structural_hash no docstrings
- E-E20 walk_tree.py `"Token" in kind` shadows its own _TRANSPARENT "TokenName" entry; contradicts AGENTS.md
- E-E21 bug template asks for src/ path
- E-E22 AGENTS.md cites MaterializeExpr as live
- E-E23..E31 example enhancements: walk_ir unbound (hide enrichment; no rhs_function arm); find_all_demo no result_type/join_side; llm_view query misses let/join/temporal and uses 4-line manual build; no semantic_hash/ticks/SetMembership.op demo; linter.py has no rules; get_referenced_tables docstring silent on let aliases; compute_semantic_hash docstring missing bind-divergence note; binding_comparison hardcodes "22 columns"
- Snippet log: 61 snippets; FAIL: E4 (partial binding), E2 (lossless), partial: exit codes, datetime literal.
- Follow-ups F1-F4, D1-D5 all closed ✔
- externaldata root with no operators -> UnknownSource; with operators -> ImplicitSource (both wrong) (= L7)
