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
- **`ExternalDataExpr` carries real data (tier 2).** `uris`, `columns` and
  `format` were placeholders — the URI was literally the string `"url"`.
- **Comments no longer reach `semantic_hash` through a column type or a
  non-literal URI (tier 2).** `AssertSchemaOp.columns`, `ParseKvOp.columns`
  and `ExternalDataExpr.columns` read each declared type with `ToString()`,
  which is `IncludeTrivia.All` and prepends the node's leading trivia: `T |
  assert-schema (a: // note`↵`long)` recorded the type as
  `"// note\nlong"` and hashed differently from the identical query without
  the comment. All three, and the new `DataTableSource.columns`, now go
  through one shared `read_row_schema` reader on `IncludeTrivia.Minimal` —
  one reader rather than four copies, since three of the four copies were
  wrong. The same leak sat on the URI fallback in `externaldata`: an
  element that does not fold to a literal (a `let`-bound feed URL,
  `strcat(...)`) is recorded as its source text, and that text carried a
  preceding comment. Note the fallback itself is by design — a `uris` entry
  is **not guaranteed to be a URI**, and both `ExternalDataSource` and
  `ExternalDataExpr` document that. A comment *interior* to an unmodelled
  node still reaches `UnknownSource.raw_text` and the URI fallback; no
  `IncludeTrivia` mode strips interior trivia, and this splits a digest
  rather than merging two, so it is documented on `UnknownSource.raw_text`
  rather than papered over.
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
- **Leading comments no longer corrupt table, column and function names
  (tier 1).** Every syntactic analyzer in `utils/analysis.py`
  (`get_referenced_tables`, `get_referenced_columns`, `get_referenced_functions`,
  `find_time_expressions`, `replace_table`) read a node's name with
  `node.ToString().strip()`, but Microsoft's `ToString()` with no argument
  includes the node's *leading trivia* — whitespace and comments both. On
  `// lead\nSecurityEvent | …` the extracted table name was the literal
  string `'// lead\nSecurityEvent'`, comment and newline included. Real
  Sentinel detection rules are full of leading comments, so this landed on
  exactly the queries the library exists to analyse: three files in this
  repo's own fixture corpus (`ADFSRemoteHTTPNetworkConnection.kql`,
  `Cross_tenantAccessSettingsOrganizationOutboundCollaborationSettingsChanged.kql`,
  `IPEntity_AzureFirewall.kql`) reported a table name containing a comment.
  `utils/walker.py` gains `node_text` (`ToString(IncludeTrivia.Minimal)`, for
  reading an expression's own text) and `node_name` (`.SimpleName` for
  `NameReference`/`BracketedName`/`TokenName`/`WildcardedName` nodes, so a
  bracketed identifier like `['my-table']` reads as the unquoted
  `my-table`); every trivia-carrying read in `analysis.py` now goes through
  one of the two. `replace_table` still replaces by `TextStart`/`Width`
  offset, so a leading comment is preserved verbatim in the output.
- **Aliases, function parameters and wildcards are no longer reported as
  tables, and a shadowing `let` keeps its right-hand side (tier 1).** The
  syntactic walk behind `get_referenced_tables()` and `replace_table()`
  reported four kinds of name that are not tables: the name an `| as X`
  operator binds, a user-defined function's table-typed parameter
  (`let f = (T1:(a:long)){ T1 | count }` reported `T1`), and a `union T*`
  wildcard, which names a pattern rather than a table and which
  `replace_table` must never rewrite. It also got `let` shadowing
  backwards: in `let SecurityEvent = SecurityEvent | where a; SecurityEvent
  | take 1` the right-hand `SecurityEvent` *is* the real table — KQL
  evaluates a binding's right-hand side outside its own name, so a `let`
  cannot be recursive — while every later use is the alias. The old
  name-keyed filter dropped both and returned **no tables at all** for that
  query, with `replace_table` a silent no-op. The right-hand occurrences of
  the name a statement is itself binding are now exempted by source span
  (names bound by *earlier* `let`s stay excluded). Every exclusion is
  positional for the same reason — a name is only an alias where it is in
  scope. An `| as X` binds `X` from the `as` onward, so the real table in
  `union X, (T | as X)` survives; a `let` binds from its own statement
  onward, so the real table in `X | count; let X = T | take 1` survives too
  (Microsoft's binder resolves that occurrence to a `TableSymbol`, so a
  bound parse always reported it and only the syntactic walk lost it); and a
  parameter is bound only inside the body of the function declaring it, so
  neither a real table sharing its name nor an outer `union T, U` beside a
  nested `(U:(b:long)){…}` is lost. `replace_table` additionally refuses to
  rewrite a wildcard span in either mode — the binder resolves `union T*`
  against a one-table schema straight to that `TableSymbol`, so a
  `replace_table("T1", "Z")` would have overwritten the pattern the caller
  never named and narrowed which tables the query reads.
  A bracketed `let` name leaked the same way for a different reason: the
  declaring side is a `NameDeclaration`, which `node_name` did not unwrap,
  so `let ['weird-name'] = SecurityEvent; ['weird-name'] | take 1` compared
  `['weird-name']` against `weird-name` and reported the alias as a second
  table — and, in `get_referenced_columns`, a bracketed scalar `let` as a
  column. `NameDeclaration` now reads back unquoted like every other name
  node. Column extraction drops wildcard patterns too, so `project-away
  Foo*` no longer reports `Foo*` and `project-keep *` no longer reports `*`.
- **A bound parse no longer drops tables the schema does not describe
  (tier 1).** `get_referenced_tables()` and `replace_table()` returned the
  binder's answer alone whenever the query was parsed with a schema, so any
  table the schema did not mention silently vanished: with
  `schema={"SecurityEvent": …}`, `union SecurityEvent, SigninLogs` reported
  only `SecurityEvent` and `replace_table("SigninLogs", "X")` returned the
  query unchanged with no error. A partial schema is the normal case — a
  detection rule joins tables from workspaces the caller never described —
  and the more of the schema a caller supplied, the more they got back, so
  the failure looked like the schema was working. `find_table_references`
  now returns the binder's references plus every syntactic reference the
  binder left unresolved (`ReferencedSymbol is None`) whose source span no
  semantic reference already covers, and both public methods read from it,
  so they can no longer disagree about what a table is. Both modes return
  one entry per occurrence in source order: the syntactic walk used to
  report a pipe source two or three times, once per branch that saw the
  node, while the bound path reported it once. Binding all 33
  fixture rules against a deliberately half-complete schema lost a table in
  13 of them before this change and none after. `get_tables_semantic()` is
  unchanged and still strictly the binder's answer.
- **`get_structural_hash()` no longer collapses `kind=inner` into
  `kind=leftanti` (tier 1).** The walker skipped every syntax kind whose
  name *contains* "Token", which is true of `TokenLiteralExpression` — the
  node holding the value half of a named parameter. So the value was
  discarded along with the punctuation, and an inner join hashed identically
  to an anti-join, `union kind=inner` to `kind=outer`, and
  `mv-expand bagexpansion=array` to `bagexpansion=bag`. The plug-in an
  `evaluate` names was lost the same way for a different reason — it is an
  ordinary identifier in the tree — so `evaluate bag_unpack(d)` and
  `evaluate pivot(d)` shared a hash. Named-parameter keyword values and
  `evaluate` plug-in names are now part of the hash; literals, identifiers,
  whitespace and comments still are not, and the docstring now states both
  halves of that boundary. **Stored hashes from 0.2-dev are invalidated** —
  the token-kind exclusion is now matched by suffix, so `TokenName` nodes
  changed the digest of every query as well.
- **`get_referenced_columns()` excludes tables by position, and stops walking
  into dynamic paths (tier 1).** Syntactic mode filtered extracted names
  against the *set of table names*, so a genuine column spelled like some
  table elsewhere in the same query vanished from every occurrence at once —
  `T | where T2 > 1 | join (T2) on a` reported no `T2` at all. The exclusion
  is now by `(TextStart, Width)`, the same spans `find_table_references`
  reports. Separately, it descended into `PathExpression` selectors, so
  `tostring(InitiatedBy.user.userPrincipalName)` reported three columns when
  the table has one: `user` and `userPrincipalName` are keys inside a dynamic
  value, and a caller resolving that list against a schema is looking up
  names that cannot exist. Selectors are skipped unless the left side is
  `$left` / `$right`, where the selector is the real column. A `| as X` alias
  is excluded too — it is deliberately not a table reference, so it has no
  span to match and needs its own name-keyed exclusion, which is the scoping
  the language itself uses since `as` binds query-wide. Syntactic mode also
  now reports the columns an `extend` / `summarize` creates; semantic mode
  reports those only where the query reads the alias back, since the binder
  attaches a `ColumnSymbol` to references and a never-read alias has none.
  Across the 33 fixture rules this drops 100 dynamic-bag keys and adds 105
  projected columns.
- **Reflection reads every overload, sees shadowed statics, and lists
  `evaluate` plug-ins (tier 1).** Three separate defects in
  `kustology.reflection`. It read only *signature zero*'s
  `DeclaredReturnType`, and `bin` declares `[None, timespan, datetime,
  datetime]` — so the two functions almost every Sentinel query uses to
  bucket time, `bin` and `bin_at`, were classified as ordinary scalars and
  `find_time_expressions()` skipped them, reporting the bare `1h` instead of
  the `bin(TimeGenerated, 1h)` around it and missing `bin(TimeGenerated,
  BinTime)` entirely. It enumerated containers with `dir()`, which loses any
  static whose name collides with a member of `System.Object` — so
  `tostring`, `gettype` and `all` were in no category at all; `container.All`
  is now read as well. And `scalar_functions()` overlapped
  `aggregate_functions()` on `any`, `hll_merge`, `merge_tdigest` and
  `tdigest_merge`, which are aggregates; the two sets are now disjoint.
  `time_functions()` 24 → 28, `string_functions()` 66 → 68,
  `scalar_functions()` 335 → 328, `all_function_names()` 482 → 532,
  `aggregate_functions()` unchanged at 61. **Tier 2 is affected too:**
  `FuncCall.is_time_func` now reads `true` for `bin`, `bin_at` and `floor`,
  where it read `false` before — the field's whole point is finding the
  temporal calls, and it was blind to the most common one. `abs` is
  explicitly excluded from that widening despite being in
  `time_functions()`: its only temporal claim is an `abs(timespan)` overload,
  and `abs(x)` over a numeric column is not a time expression. `semantic_hash`
  does not read the flag, so stored tier-2 hashes are unaffected.
- **The CLI honours its documented exit codes (tier 1).** `cli.py`'s
  docstring has always promised `0` success, `1` the input had errors, `2` a
  usage error, and the code decided between them by whichever exception
  happened to escape. A missing file and a malformed `--schema` JSON both
  reached the bare `except Exception` and reported `1` — the code that means
  "we read your query and it had errors", for a query that was never read;
  and `parse` printed the AST of input carrying Error-severity diagnostics
  and exited `0`, so a script checking only the status code treated
  `T | where` as a good parse. Both file and schema failures are now `2` and
  an Error diagnostic is `1`, in every subcommand. The `2` is raised where
  the invocation is *read* — `_read_input` and `_load_schema` — rather than
  by a blanket `except OSError` in `main`, which would also cover every
  `sys.stdout.write`: under one, `kustology parse --ast --json big.kql |
  head` reported a usage error for a correct invocation whose reader simply
  stopped reading.
- **A broken pipe no longer reports a usage error, and no longer erases the
  command's own exit code (tier 1).** A reader hanging up says nothing about
  whether the input was valid, and for `validate` the validity verdict *is*
  the exit code — so neither `2` nor a blanket `0` is right.
  `kustology validate q.kql | head` still exits `1` on a query that fails
  validation: each subcommand decides its code before it writes and wraps
  only the writing, so the pipe stops the output and nothing else. Only a
  pipe that breaks before any code was decided reaches `main`'s own arm,
  which exits `0`. Either way stdout is redirected to `devnull` so the
  interpreter's shutdown flush cannot print `Exception ignored … Broken
  pipe` after the command has already returned.
- **`KUSTOLOGY_MAX_INPUT_BYTES` counts bytes (tier 1).** The ceiling read
  through a decoded text stream, so `len(data)` counted *characters*: a
  20-character query occupying 28 bytes passed a 22-byte cap. The read now
  goes through `sys.stdin.buffer`, and file inputs open binary.
- **Deeply nested input no longer raises `RecursionError` out of the AST
  traversal layer (tier 1).** `KustoQuery.to_dict()`, `KustoWalker.visit` and
  the `utils/analysis.py` analyzers reached through `collect_nodes` recursed
  once per AST level with no cap, and 1200 nested parentheses nest the tree
  past 2400 levels — deeper than CPython's own 1000-frame limit. The CLI
  carried a local cap of 1000 that could never be reached for the same
  reason. The cap is now `walker.MAX_AST_DEPTH = 300`, enforced in the walker
  where all three paths share it: `node_to_dict` emits `{"kind", "text",
  "children": [], "truncated": true}` at the cap and `visit` stops
  descending, so adversarial input degrades to a marked partial answer.
  **Not covered:** `to_ir()` / `IRBuilder` walk the AST with their own
  recursion, which is still uncapped — `parse --ir` on such input exits 1
  with a one-line `RecursionError` message rather than a truncated IR.
- **`replace_table` validates its arguments and quotes a name that needs it
  (tier 1).** `replace_table("A", "")` deleted the table name and returned
  ` | count` — a query the parser rejects — with no error, and a non-string
  died in the middle of a string concatenation with a message about this
  function's internals. Both names must now be non-empty `str`. Separately,
  a hyphenated or spaced table name is legal in Kusto and illegal as a bare
  identifier, so `replace_table("A", "my-new-table")` emitted
  `my-new-table | count`, which parses as arithmetic and reads no table at
  all. `new_name` now goes through Microsoft's own
  `KustoFacts.BracketNameIfNecessary`, so it is emitted bare when it is
  usable bare and as `['my-new-table']` — or `["o'brien"]`, the form that
  needs no escape — when it is not. **KQL keywords are the case a regex gets
  wrong:** `project` matches `[A-Za-z_][A-Za-z0-9_]*` and is still not usable
  bare, and `project | count` validates with *zero* diagnostics while reading
  no table at all, which is the same silent failure reached through an input
  that looks like an identifier. `where`, `union` and `datatable` behave the
  same way; all four are now quoted, and a name containing a newline is
  emitted correctly rather than as a broken literal. An ordinary identifier
  rename is byte-for-byte what it always was.
- **`find_time_expressions` reports a nested temporal call once (tier 1).**
  `startofday(now())` came back as two overlapping entries — the outer call
  and its own argument — so a caller counting time expressions or slicing
  the source by their spans saw one construct twice. A matched call inside
  another matched call is now suppressed, which is the same containment rule
  the datetime/timespan literal pass already applied to `ago(1h)`. A
  temporal call inside a *non*-temporal one is untouched: nothing matched
  around it, so `tostring(now())` still reports `now()`.
- **The unknown-scalar-type `RuntimeWarning` names the caller's file (tier
  1).** `stacklevel=3` landed inside `utils/schema_state.py`, so
  `parse(q, schema={"T": {"x": "typo"}})` blamed a library module the caller
  does not own, `-W error::RuntimeWarning` pointed at the wrong file, and the
  default once-per-location filter folded every caller's typo into a single
  report. The depth is now *computed* — the resolver walks out to the first
  frame whose file is outside the package and warns there — rather than
  written down. A constant cannot be right: `parse` and `validate` sit one
  frame deeper than a direct `build_global_state` call, and PEP 709 inlined
  comprehensions in **3.12**, so on the 3.10 and 3.11 legs of the support
  matrix the two comprehensions in `schema_state.py` each push a frame and
  any hardcoded number lands back inside that same file. All three entry
  points are now attributed to their caller on every supported version,
  including the direct `build_global_state` call that previously overshot.
- **`kustology.PackageNotFoundError` is gone from the package namespace
  (tier 1).** `from importlib.metadata import PackageNotFoundError` bound the
  name into `kustology`, where it appeared in `dir()` and in generated
  documentation as if it were part of this library's API. `__all__` never
  listed it, which is why nothing caught it; it is imported under an
  underscored alias now. `__version__` is unchanged.
- **Six public `KustoQuery` members gained docstrings (tier 1).**
  `get_operator_chain`, `get_referenced_columns`, `get_referenced_functions`,
  `get_structural_hash`, `syntax` and `text` delegated in silence, so
  `help(KustoQuery)` showed a bare signature for methods whose module-level
  functions document at length what they are blind to and where their two
  modes disagree. A test now fails if a public member ships undocumented.

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
- `plugin_functions()` (tier 1) — the 47 `evaluate` plug-ins reflected from
  `Kusto.Language.PlugIns`, a container nothing enumerated before, so
  `bag_unpack`, `pivot`, `narrow` and the rest were in no category and
  absent from `all_function_names()`. Disjoint from the scalar and aggregate
  sets, since a plug-in is invoked by `evaluate` and never as a scalar call.
  Exported from `kustology` alongside the other reflection helpers.
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
- `kustology parse --schema PATH` (tier 1) — the same JSON schema file
  `validate --schema` takes, now on `parse`. It binds the parse, and
  `to_ir()` auto-attaches on a bound parse, so `parse --ir --schema` emits an
  IR with column types, table provenance and `schema_attached: true` instead
  of an unenriched skeleton. `--ast` accepts it too; binding does not change
  the syntax tree.
- `parse --ir --json` emits a versioned envelope (tier 1):
  `{"ir_schema_version", "semantic_hash_scheme", "ir"}`. Both tags are the
  consumer's compatibility contract and neither was reachable from the CLI,
  so a stored payload could not be checked against the IR shape that
  produced it. The IR itself moved under `"ir"`.
- `KustoQuery.diagnostics` (tier 1) — the query's diagnostics in
  `validate()`'s dict shape, read off the `KustoCode` the object already
  holds. A caller who had parsed could only get diagnostics by handing the
  text back to `validate()`, which parses it a second time and, for a bound
  query, re-runs the binder against a schema it has to be given again.
  Unfiltered — there is no `ignore_unknown_tables` on a property; filter the
  list. `validate()` and the property share
  `services._diagnostic_dicts`, so the two shapes cannot drift.

### Changed

- **`get_operator_chain()` returns operators only (tier 1).** Element 0 used
  to be the `NameReference` naming the source table, so `len()` was an
  operator count one too high — `KustoQuery.__repr__` reported `T | where a |
  take 1` as `3 ops` — and every consumer had to know the first element was
  different in kind from the rest. `T | where a | take 1` now yields two
  nodes and a bare `T` yields none; the source is available from
  `find_table_references()`. The docstring also states the other half of the
  scope, which was never written down: this is the *main* pipeline only, and
  `get_operator_stats()` is the whole-AST count.
- `get_time_range()` is renamed `find_time_expressions()` on both the module
  and `KustoQuery`; the old name remains as a deprecated alias. It returns
  every time-related expression, not a resolved range, and the old name led a
  consumer to use it as a lookback extractor.
- .NET runtime discovery and boundary member probes log at `DEBUG`.
- `format` refuses input the parser rejected. It used to print whatever
  Microsoft's formatter returned for a broken query — `'T | where '` for the
  truncated `T | where` — and exit `0`, so a shell redirect wrote a query the
  parser had already rejected to a file. It now writes nothing to stdout,
  reports the diagnostics on stderr and exits `1`; `parse` does the same. The
  gate is unbound, so an unknown table is still fine.
- `parse --ast --json` node text no longer carries the node's leading
  *whitespace*. The CLI had its own copy of the tree serializer that differed
  from `walker.node_to_dict` in exactly this respect — the `where` token of
  `| where x == 1` serialized as `" where"` and the pipe token as `"\n|"`.
  Both emitters now render the library's dict, so the CLI's JSON is
  `KustoQuery.to_dict()` output. Kinds and tree shape are unchanged, and the
  text form's output is byte-identical. Note this is `ToString().strip()`,
  not `IncludeTrivia.Minimal`: a leading **comment** is still part of the
  text, so the second pipe of `StormEvents\n| where … // c\n| take 5` still
  serializes as `"// c\n|"`. Reading a node's own source without comments is
  what `node_text` is for — see the trivia entry under **Fixed**.
- **Input is no longer newline-translated (tier 1).** The CLI reads bytes now
  that `KUSTOLOGY_MAX_INPUT_BYTES` counts bytes, so a CRLF file and a CRLF
  stdin pipe both reach the parser as CRLF instead of being folded to LF by
  Python's universal-newline layer. Byte offsets are therefore computed over
  the input as it actually is: `validate --json` on a query whose first two
  lines end CRLF reports `"start": 33` where the LF-authored equivalent
  reports `32`, and `parse --ast --json` node text contains literal `\r\n`.
  This is arguably more correct — the offsets index the bytes on disk, which
  is what an editor integration needs — but it does change published numbers
  for Windows-authored `.kql`, and CI runs on LF so nothing else catches it.
  `format` output is unaffected: `format_query` normalizes CRLF to LF, and
  CRLF and LF inputs produce byte-identical output.

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
- **`semantic_hash` now sorts `and`/`or` operands and `in (...)` values, and
  is invariant under renaming `let` bindings.** Queries that differ only in
  those three ways used to produce different digests and now collide, which
  is the point — they mean the same thing. `normalize_expressions` is
  unchanged and still leaves a user's operand order exactly as written; the
  sort runs on the hash's own deep copy. `let` names are replaced by their
  declaration index (`$let0`, `$let1`, …), so which binding a reference
  points at is still hashed. One further equivalence falls out of the sort
  composing with the existing consecutive-filter merge: a run of `| where`
  operators becomes a single `and` whose operands are then sorted, so
  `| where A | where B`, `| where B | where A` and `| where B and A` are now
  all one digest. Only *consecutive* filters merge — `| where A | take 5`
  and `| take 5 | where A` still hash apart. Still `kustology-sem-v2`, which
  covers the whole unreleased window since `v0.1.0`.
- **`SortOp.expressions` is `list[SortKey]`** (was `list[AnyExpr]`) and
  **`TopOp.by` is a `SortKey`** (was `AnyExpr`). The builder unwrapped the
  parser's `OrderedExpression` and dropped its ordering clause, so
  `sort by x asc` and `sort by x desc` — opposite orderings — built identical
  IR and one `semantic_hash`, as did `nulls first` against `nulls last`.
  `SortKey.direction` is a required `Literal["asc", "desc"]` carrying KQL's
  *effective* value: a bare `sort by x` records `"desc"`, never `None`, and
  the field has no pydantic default so `to_llm_dict` renders it.
  `SortKey.nulls` is `Literal["first", "last"] | None`. Reach the expression
  through `.expression`; `semantic_hash` changes for every query with a
  `sort`, `order by` or `top`. An unreadable modifier degrades rather than
  raising: `sort by x nulls` (and `nulls firs`, `nulls xyz`) records
  `nulls=None` and keeps the direction, leaving the complaint to the
  diagnostics, which is what every other operator does with malformed input.
- **`ProjectReorderOp.columns` is `list[ReorderKey]`** (was
  `list[ColumnRef | AnyExpr]`). `project-reorder` is the third consumer of
  the same `OrderedExpression` wrapper, so `project-reorder x asc` used to
  reach `_visit_expr`'s unwrap; with that unwrap gone it fell through to an
  `UnknownExpr` and the column identity went with it — unbindable, invisible
  to `find_all(ir, ColumnRef)`, an opaque blob in the LLM view, while a bare
  `project-reorder x` was unaffected. Each term is now a `ReorderKey`
  carrying its `expression` and an **optional** `direction`. Optional is the
  difference from `SortKey`: `project-reorder`'s `asc`/`desc` orders
  *columns* rather than rows, and omitting it means "keep the listed order"
  rather than selecting a KQL default, so `None` is the honest record and
  D8's effective-default rule does not apply. `asc`, `desc` and unwritten all
  hash distinctly. Reach the column through `.expression`.
- **`ForkOp.pipelines` is replaced by `ForkOp.branches`**, a
  `list[ForkBranch]` where each `ForkBranch` carries an optional `name` (the
  `a=` prefix, which names the result table the branch produces) and its
  `pipeline`. The old field was declared but never populated: the builder
  handed each `ForkExpression` to `_visit_pipeline`, whose walker has no case
  for that node kind, so every branch came back with no operators and an
  `UnknownSource`. `T | fork (take 1) (count)` and
  `T | fork (count) (where x == 1)` therefore built identical IR and one
  `semantic_hash`, and nothing inside a branch was reachable —
  `find_all(ir, FilterOp)` returned an empty list for a query whose only
  `where` sat in a fork. Branch pipelines now carry an `ImplicitSource`, the
  binder descends into them, and `semantic_hash` changes for every query
  containing a `fork`. The field is renamed rather than retyped so that a
  stored dump written against the old shape fails validation under
  `extra="forbid"` instead of quietly reproducing the empty branches it
  recorded.
- **The pipeline source position gains two classes and three fields, and
  `ExternalDataExpr.uri` becomes `uris`.** Four different queries used to
  build indistinguishable sources and share one `semantic_hash`. A
  `datatable` lowered to `FuncCallSource(name="datatable", args=[])`, so its
  schema and every row were discarded and any two datatables collided; it is
  now a `DataTableSource` carrying `columns` and `rows` (the parser hands
  over a flat value list, which the builder reshapes by the column count).
  An `externaldata` in source position had no source class at all and is now
  an `ExternalDataSource` with `columns`, `uris` and `format` — which also
  makes `let X = externaldata(...)` a tabular binding on `rhs_pipeline`
  rather than an `ExternalDataExpr` on `rhs_expr`, so `rhs_pipeline is not
  None` is a reliable "is this binding tabular" test again. `TableRef` gains
  `database`, `cluster` and `is_wildcard`: `database('d1').T` and
  `database('d2').T` read different tables, and `union T*` names a set of
  tables where `union ['T*']` names one table called `T*`. Both
  `ExternalDataExpr.uri` and the single URI it held are gone — the field is
  `uris: list[str]`, because a feed stitched from two URIs is not the feed
  from either one. `Pipeline.source` becomes an explicitly ordered
  `union_mode="left_to_right"` union (fields-less `ImplicitSource` after the
  classes that add fields, as `Pipeline.operators` already did), and
  `UnknownSource.raw_text` records the real source text instead of the
  constant `"unknown"`. `semantic_hash` changes for every query with a
  `datatable`, an `externaldata`, a database- or cluster-qualified table, a
  wildcard table, or an unmodelled source. `to_llm_dict` caps
  `DataTableSource.rows` at 20 and adds a `rows_omitted` count, since real
  IOC datatables run to thousands of rows; `model_dump_json` stays complete.
- **`KustoWalker.visit` takes a `depth` argument** (tier 1, listed here for
  want of a tier-1 breaking section): `visit(self, node)` →
  `visit(self, node, depth=0)`, so the base class can stop at
  `MAX_AST_DEPTH`. `pre_visit` / `post_visit` are the documented override
  points and are unchanged; a subclass that overrode `visit` itself gets a
  `TypeError` the first time the base recurses into it. Nothing in this
  repository does.

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
