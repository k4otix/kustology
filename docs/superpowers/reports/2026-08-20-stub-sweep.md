# Sweep: declared-but-unpopulated surface and hand-maintained traversal

**Date:** 2026-08-20
**Charter:** `docs/superpowers/handoffs/2026-08-20-library-gaps-followups.md` §3
**Status:** complete — every entry verified by running the library against the
bundled DLL, not read off source

The predecessor branch fixed **G1**: `LetBinding` declared four fields and a
seven-value `category` enum that the builder never populated. The surface read as
implemented, the tests passed, and a downstream consumer's whole design was
blocked on it. This sweep looks for every other instance of that shape.

Baseline at time of sweep: 354 tests green, corpus gate clean (0 unknown
expressions / sources / unspecialized operators over 33 queries).

**Dispositions, added 2026-08-22.** Every row below now carries its outcome
alongside the original verdict — **REMOVED** where the surface was deleted,
**FIXED** where it was populated or corrected, **DOCUMENTED** where that was
the ask. Each was re-checked by running the library on the 0.2.0 release
branch rather than read off a commit message, and the check is quoted in the
cell. **One row is not closed:** B3 (`RegexMatch.case_sensitive`) is still a
hardcoded `True` with no comment saying why. Suite at the time of checking:
1785 passed, 4 skipped, 15 xfailed.

---

## 1. The root cause behind four of the findings

Four defects share one mechanism, which the handoff did not identify: a
`getattr`/`hasattr` probe names a .NET member that **does not exist**. pythonnet
is case-sensitive and silent about this, so the guard never fires and the field
keeps its declared default. There is no exception, no log line, no test failure.

| Probe | Site | Reality | Result |
|---|---|---|---|
| `node.Uris` | `builder.py:1111` | real member is `URIs` | `ExternalDataExpr.uri` is always the placeholder `"url"` |
| `res_type.IsNullable` | `_builder_helpers.py:85` | property on **zero types** in the assembly | `Expr.nullable` is always `True` |
| `res_type.Underlying` | `_builder_helpers.py:76` | property on **zero types**; the real one is `ElementType` on `DynamicArraySymbol` | `Expr.result_type_inner` is always `None` |
| `n.Keys` then `.Count` | `builder.py:824` | `Keys` is a `RowSchema`; it exposes `Columns`, not `Count` | `ParseKvOp.columns` is always `[]` |

`MacroExpandOp.pipeline` (`builder.py:855`) is the same shape: it probes
`Subquery` and `Body`; the real member is `StatementList`.

Verification (reflecting over the loaded assembly):

```
IsNullable   -> []                                              (count=0)
Underlying   -> []                                              (count=0)
ElementType  -> ['SyntaxList', 'SyntaxList`1', 'DynamicArraySymbol']
```

and on a real `ExternalDataExpression` node:

```
has 'Uris': False   has 'URIs': True
has 'Schema': True   -> (n:string, i:long)
has 'WithClause': True -> with (format="csv")
```

---

## 2. Pattern A — declared surface no code path produces

| # | Item | Site | Evidence | Verdict |
|---|---|---|---|---|
| A1 | `QueryIR.parse_warnings` | `ir/query.py:593` | Zero assignments anywhere in `src/`. The only other reference is a negative assertion at `tests/ir/test_llm_view.py:126`. `QueryIR.diagnostics` already carries real parser diagnostics (populated at `builder.py:260-300`), and `Unknown*` fallback nodes are reachable via `find_all` — so this is a redundant third channel. | **remove** → **REMOVED** — `"parse_warnings" not in QueryIR.model_fields`. |
| A2 | `Span.source_text` | `ir/spans.py:16` | Every span is built by `to_span()` (`_builder_helpers.py:92-94`), which passes only `text_start`/`width`. Set manually only at `tests/ir/test_ir_builder.py:98,117`. `Span.text(raw)` already slices from the source and ignores this field entirely. | **remove** → **REMOVED** — `"source_text" not in Span.model_fields`. |
| A3 | `Expr.nullable` | `ir/expr.py:36` | Dead probe (§1). Always `True`. Not populatable at all — Microsoft's parser carries no nullability information, so the declared comment ("binder flips to False when it can prove non-null") describes behavior that cannot occur. | **remove** → **REMOVED** — `"nullable" not in Expr.model_fields`. |
| A4 | `Expr.result_type_inner` | `ir/expr.py:34` | Dead probe (§1). Always `None`, including for `dynamic([1,2])`, `pack_array`, `mv-expand`. **Is** populatable: `ElementType` on `DynamicArraySymbol`. `binder._walk_operator`'s `MvExpandOp` branch already reads this field. | **populate** → **FIXED** — `T \| extend x = dynamic([1,2,3])` gives the literal `result_type_inner="long"`. |
| A5 | `ExternalDataExpr.uri` | `ir/expr.py:234` | Dead probe (§1) — emits the fake value `"url"`. | **populate** → **FIXED**, and renamed `uris: list[str]` since a feed may name several — `externaldata(n:string, i:long) ["https://x/y.csv"] with (format="csv")` gives `uris=['https://x/y.csv']`. |
| A6 | `ExternalDataExpr.columns` | `ir/expr.py:233` | `cols` is initialised `[]` at `builder.py:1109` and never appended to. The data is at `node.Schema`. `llm_view.py:83` already reads the field. | **populate** → **FIXED** — the same parse gives `columns=[('n', 'string'), ('i', 'long')]`. |
| A7 | `ExternalDataExpr.format` | `ir/expr.py:235` | Hardcoded `format="unknown"` at `builder.py:1113`. The data is at `node.WithClause`. | **populate** → **FIXED** — the same parse gives `format='csv'`. |
| A8 | `ParseKvOp.columns` | `ir/query.py:396` | Dead probe (§1). `T \| parse-kv a as (b:string, c:long)` yields `columns=[]`. | **populate** → **FIXED** — now `{'b': 'string', 'c': 'long'}`, and the field is a `dict[str, str]`: a declared key has a type, not a value. |
| A9 | `MacroExpandOp.pipeline` | `ir/query.py:422` | Dead probe (§1). Always `None`. | **populate** → **FIXED** — `macro-expand EG as x (x.T \| count)` builds a real `Pipeline` on `.pipeline`. |
| A10 | **`LetRef` — the whole class** | `ir/query.py:120-125` | Exported at `ir/__init__.py:40,93`, a declared member of the `Pipeline.source` union (`query.py:481`), referenced in an annotation at `builder.py:383` — and **`LetRef(` appears nowhere in `src/`, `tests/`, `examples/` or `scripts/`**. `_visit_pipeline` maps every source-position `NameReference` to `TableRef` (`builder.py:450-461`). Verified: `let X = T \| take 1; X \| count` → `TableRef(name='X')`. A consumer branching on `isinstance(src, LetRef)` gets a branch that never fires; one distinguishing tables from let-aliases gets wrong answers. This is G1 at class granularity. | **populate** → **FIXED** — `let X = T \| take 1; X \| count` now yields `Pipeline.source` of type `LetRef`, and the class is constructed in `src/`. The expression-position twin `LetValueRef` was added after this sweep for the same reason. |
| A11 | `reflection._safe_first_param_type_name` | `reflection.py:60-75` | Fully implemented, zero call sites anywhere. Not a model field but the same shape. | **remove** → **REMOVED** — `hasattr(kustology.reflection, "_safe_first_param_type_name")` is `False`. |
| A12 | **`MaterializeExpr`** | `ir/expr.py:202-205` | Found while fixing C7, after the tables above were written. Zero occurrences across the entire 33-query corpus and every shape tried. ``materialize`` is a keyword Microsoft's parser refuses in expression position — `T \| where X > toscalar(materialize(...))` fails with *"If the keyword 'materialize' is intended to be part of an expression it needs to be bracketted"* — and at source position it becomes a nested `Pipeline`, not a `MaterializeExpr`. The `_visit_expr` branch that would build one looks unreachable, and `_is_tabular_let_rhs` routes a `let` RHS to `_visit_pipeline` before it could fire. Same shape as the `LetRef` finding, but unproven: absence over one corpus is not unreachability. | **REMOVED** — proven unreachable; see `2026-08-20-materialize-reachability.md` |
| A13 | `FuncCallSource.args` on the datatable path | `ir/query.py:155` | Populated for real UDF sources (`builder.py:433`) but hardcoded `args=[]` for `DataTableExpression` (`builder.py:439-441`), dropping the inline values. | **document** → **FIXED** — modelled instead. A `DataTableExpression` no longer becomes a `FuncCallSource` at all: it builds a `DataTableSource` carrying `columns` and a `rows: list[list[AnyExpr]]`, so `datatable(a:string, b:long) ['x', 1, 'y', 2]` keeps both rows as `LiteralExpr` nodes. |
| A14 | `SetMembership` has no operator field | `ir/expr.py:99-105` | Also found late. `has_any` / `has_all` and `in~` all build a `SetMembership` with `polarity="inclusion"` and `case_sensitive=False`, and nothing else distinguishes them — a term match and a set membership are the same node. `canonical_form` renders all three as `in~`. Pre-existing and out of that branch's scope. | **FIXED** — both nodes gained an `op` field |

| A15 | `Exists` has no source-function field | `ir/expr.py:141-144` | Found when the A14 follow-up was planned. `isnotnull` and `isnotempty` both lower to `Exists(target=…)` with an identical `semantic_hash`, though `isnotempty` also rejects `""`. `isnotempty` is the most common operator of this class in the corpus — 22 occurrences across 11 of 33 files. Note the asymmetry: `isnull` / `isempty` are not lowered at all, stay `FuncCall`, and already hashed distinctly. | **FIXED** — gained an `op` field |
| A16 | `BinOp.case_sensitive` from an allow-list | `builder.py:1063-1067` | Found the same round. Derived from a six-member allow-list with everything absent defaulting to `True`, so `hasprefix`, `hassuffix` and all six negated string operators were reported backwards. Same "hand-maintained list drifts" pattern as the walkers, expressed as a wrong value rather than a missed node. | **FIXED** — derived from the operator suffix |

**A14–A16 are one class, named after this report was written: *lossy lowering*.**
A node can be fully populated and still lose information, if the builder maps
several source constructs onto it with nothing recording which. No stub
detector finds these, because nothing is unpopulated. The check is whether two
different queries can produce identical IR. Recorded in AGENTS.md.

### Pattern A2 — enum members nothing emits

| # | Item | Evidence | Verdict |
|---|---|---|---|
| B1 | `SetMembership.case_sensitive` | Hardcoded `False` at `builder.py:1032`. Verified: `in`, `in~`, `!in`, `!in~` **all** yield `False`. KQL `in` is case-*sensitive*; only `in~` is not. So the field is both constant and **semantically wrong for half its inputs**, and `canonical()` consequently renders `X in ("a")` as `X in~ ("a")`. | **fix — correctness bug** → **FIXED** — derived from `op` rather than hardcoded: `in` / `!in` give `True`, `in~` / `has_any` give `False`, and `canonical()` renders `a in ("x")`. |
| B2 | `KustoType.TABULAR` | `ir/types.py:27`. The only producer is `map_net_type` (`_builder_helpers.py:61`), which needs a .NET symbol whose `Name` is literally `"tabular"`. `TableSymbol.Name` is the table's own name; `ScalarTypes` has no `tabular`. Its only test (`tests/ir/test_ir_builder.py:250-252`) asserts `"TABULAR" in {m.name for m in KustoType}` — a tautology that passes whether or not the member is reachable. | **document** → **FIXED** — the tautology is gone. `tests/ir/test_ir_builder.py:329` now asserts `map_net_type("tabular") is KustoType.TABULAR` and carries a docstring saying the member is declared but unreachable from a real parse. |
| B3 | `RegexMatch.case_sensitive` | Hardcoded `True` at `builder.py:1009`. Domain-defensible (`matches regex` is always case-sensitive) but the field carries no information. | **document** → **STILL OPEN** — verified 2026-08-22: still `case_sensitive=True` hardcoded at `builder.py:1755`, and neither the field nor the class carries a comment saying why. Harmless (the value is right for every input) but the row's own ask is unmet. |

### Cleared — checked and not defects

- `Finding.rule_id` / `severity` / `message` / `span` / `extra` and the `Severity`
  literal (`ir/analyzers.py`) — caller-populated by design, as the class docstring
  states. Confirms the handoff's prior clearance.
- All 11 `LiteralExpr.literal_kind` members are empirically produced.
- `polarity` on `BinOp` / `SetMembership` / `Between` — both members produced.
- `Pipeline.result_schema`, `QueryIR.schema_attached`, `ColumnRef.table`,
  `Expr.result_type` — populated by attribute assignment, which a naive `name=`
  grep misses. Verified populated under `parse(q, schema=...)`.
- Every entry in `HANDLED_OPERATOR_KINDS` (53) and `HANDLED_EXPR_KINDS` (27) is
  genuinely dispatched. **Correction, 2026-08-22: this clearance was too
  strong.** "Dispatched" was checked, and *reachable without raising* was not:
  the `TopHittersOperator` and `PartitionByOperator` branches read .NET members
  that do not exist on their node types, so `T | top-hitters 5 of a by b` and
  `T | __partitionby a (take 1)` crashed `to_ir()` with `AttributeError` while
  both kinds sat in the list claiming to be modelled. That is §1's mechanism in
  its loud form — direct attribute access rather than a silent `getattr` — and
  it is now covered by `tests/ir/test_handled_kinds_smoke.py`, which builds
  every listed kind from real KQL. The counts have also moved with the release:
  53 and **29**.
- Tier 1 (`src/kustology/*.py`, `utils/`) is **clean** on Pattern A: no
  declared-but-unassigned attributes, no unreachable `Literal` members. Its
  defects are all Pattern B (§3).

---

## 3. Pattern B — hand-maintained traversal that skips branches

| # | Item | Evidence | Verdict |
|---|---|---|---|
| C1 | `SchemaAttacher._fill` (**F1**) | `binder.py:363-376` recurses a hardcoded tuple omitting `pipeline`, `branches` and `default`. Verified: `SecurityEvent \| where EventID > toscalar(SecurityEvent \| summarize max(EventID)) \| project Account` gives `EventID` `table=SecurityEvent` outside the `toscalar` and `table=None` inside — inconsistent provenance for the same column in one query. Note that adding `"pipeline"` to the tuple would have been a **no-op**: the loop guards `isinstance(child, Expr)` and `Pipeline` is not an `Expr`. | **fix** → **FIXED** — the fill derives its children from `model_fields` rather than a tuple, so the same query now reports `table=SecurityEvent` for `EventID` both inside and outside the `toscalar`. |
| C2 | `SchemaAttacher._walk_operator` | An `isinstance` chain covering **17 of 53** `Operator` subclasses, with no fallback — it simply falls off the end. Verified: `SecurityEvent \| sort by EventID \| project Account` leaves `EventID` unresolved while `Account` resolves. 36 operator types affected, including `sort`, `top`, `search`, `find`, `mv-apply`, `partition`, `fork`, `range`, `print`, `serialize`, `parse-kv` and the graph operators. | **fix** → **FIXED** — a generic fallback fills expressions and walks sub-pipelines for every operator without a bespoke rule, so `SecurityEvent \| sort by EventID \| project Account` resolves both columns. 25 of the 53 subclasses now have a scope rule; the other 28 pass the scope through, exactly for some and knowingly stale for others (both lists are in `SchemaAttacher`'s docstring). |
| C3 | `SchemaAttacher._source_entry` | `Pipeline.source` may itself be a `Pipeline` (`query.py:481`, produced for `materialize(P) \| …`); `_source_entry` returns an empty anonymous scope and the inner pipeline is never walked. | **fix** → **FIXED** — an `isinstance(source, Pipeline)` branch walks the inner pipeline and adopts its `result_schema`. `let M = materialize(T \| where …)` is the reachable shape and is tested at `tests/ir/test_binder.py:454`; `materialize(…) \| …` at the head of a bare statement is not accepted by the parser and yields an `UnknownSource`. |
| C4 | **Generic `walk()`** | `walk.py:48-58` descends `list` and `dict` values but **not `tuple`**, so `CaseExpr.branches: list[tuple[AnyExpr, AnyExpr]]` is invisible. Verified: a `case()` holding 5 `ColumnRef`s surfaces 1 via `find_all`. This matters disproportionately — AGENTS.md holds `walk`/`find_all` up as the drift-free traversal that bespoke walkers should be converted to. `transforms._normalize_field` and `tests/ir/test_ast_isolation.py:71-90` both handle tuples; `walk` is the odd one out. | **fix — land first** → **FIXED** — container descent is recursive and container-kind agnostic; `T \| extend y = case(a==1, b, c==2, d, e)` now surfaces all five `ColumnRef`s. |
| C5 | Corpus gate walkers (**F2**) | `tests/ir/test_complex_harness.py:65-115` and `scripts/mine_corpus.py:65-108` are near-duplicates. Beyond the reported `pipeline` omission they also hand-enumerate *operator* fields via `hasattr`, missing `SortOp.expressions`, `TopOp.by`, `RangeOp.start/end/step`, `FacetOp.with_pipeline`, `MacroExpandOp.pipeline`, `MakeSeriesOp.on_column`, `ParseOp.patterns`, `SampleDistinctOp.of`, `JoinOp.on`. `test_complex_harness._walk_expr:81-82` additionally re-walks `NamedExpr.expression`, already covered by `"expression"` in its own tuple, double-counting unknowns beneath it. | **fix** → **FIXED** — both now call `find_all` over the whole `QueryIR`; neither hand-enumerates a field name. |
| C6 | `scripts/verify_corpus.py:174-189` | A third, weaker copy of `walk` — no dict branch, no tuple branch, and applied to `main_pipeline` only. | **fix** → **FIXED** — it imports `walk` from `kustology.ir` and walks the whole `ir`. |
| C7 | `_normalize.canonical()` | `_normalize.py:69-112` handles 12 of 23 `Expr` types; the remaining 11 hit the `"?"` fallthrough at line 112. Verified: `-X > 1`, `D.a == 1` and `toscalar(…) > 1` **all** render as `"? > 1"`. Not hash-affecting (`canonical_form` is a property excluded from `model_dump`) but user-visible. | **fix** → **FIXED** — all three render distinctly (`-X > 1`, `D.a == 1`, `toscalar(T \| ...) > 1`), and `tests/ir/test_canonical_coverage.py` now asserts every live `Expr` subclass name appears in the function, so a new one cannot fall through silently. |
| C8 | Tier 1 `_collect_table_refs` | `utils/analysis.py:125-167` omits the `find in (...)` and `search in (...)` clauses. Verified: `find in (S1, S2) where X == 1` → `get_referenced_tables()` returns `set()`, and `replace_table("S1","New")` **silently returns the query unchanged** — the worst failure mode for a rewriting API, since a consumer migrating tables ships one still pointing at the old name. **Correction:** an earlier draft of this row also named `PartitionOperator`. That was wrong — `partition by K (B \| …)` is a parse error ("Query operator expected"), because the subquery runs on the partitioned rows rather than a new source. There is no table position there to miss. | **fix** → **FIXED** — `find in (S1, S2) where X == 1` reports `{'S1', 'S2'}` and `replace_table("S1", "New")` rewrites it; `search in (…)` likewise. |
| C9 | `llm_view` dispatch | `llm_view.py:100-156` keys on class *name strings* (`"ColumnRef"`, `"BinOp"`, …), so a rename silently disables the rule. Not a traversal gap, same silence. | **fix** → **FIXED** — dispatch is `isinstance`-based (12 sites); no class-name string keys remain in the module. |
| C10 | `_VOLATILE_FIELDS` | `transforms.py:152-154` is hand-maintained: any new bind-populated annotation field must be added or the hash diverges between bound and unbound parses. | **document** → **DOCUMENTED** — AGENTS.md's "Version tags bump together" section states the rule, names every member of the set including `hints`, and says to check whether a new field carries source-derived information before adding it. `compute_semantic_hash`'s docstring repeats the consumer-facing half. |

### Collateral

- `builder.py:1016` — `elif op in ("==", "in", "!in") or "_cs" in op:` is partly
  dead; `in`/`!in` dispatch to the `InExpression` arm at `builder.py:1027` and
  never reach this `BinaryExpression` branch.
- `builder.py:833` — the `getattr(n, "Of", None)` fallback is dead (`Of` is not a
  member of any `Kusto.Language` type), but harmless: `OfExpression` resolves
  first and `SampleDistinctOp.of` is correctly populated.
- `scripts/mine_corpus.py` — its docstring names `tests/test_corpus_unknowns.py`
  as the CI signal; that file does not exist. The real consumer is the
  `corpus-regression` job.
- `scripts/mine_corpus.py:211` — `--output` with an absolute path outside the
  repo raises `ValueError` from `Path.relative_to` in the success message.

---

## 4. Detection methods

The handoff supplied one heuristic. It found two of the fourteen Pattern A items.
Two further methods found the rest, and are worth keeping.

### 4a. Assembly reflection (found A4, A5, A8, A9)

Extract every capitalised string used as a `getattr`/`hasattr` member name across
`src/`, then check each resolves to a member of some type in the loaded
`Kusto.Language` assembly. 48 of 52 already resolved; the four that did not were
each a live defect. This is now enforced by `tests/test_reflection_audit.py`.

### 4b. Constant-assignment detection (found A6, A7, B1)

The handoff's heuristic asks *"is this field ever assigned?"* — which **would not
have found G1 itself**. `category="alias"` *was* assigned; it was just always
assigned the same literal. The stronger question is *"is every assignment site
the same constant?"*:

```python
LIT = re.compile(r"""^(\[\]|\{\}|None|True|False|".*?"|'.*?'|-?\d+)$""")
# collect every `name=value` kwarg site in src/, group by name,
# report names whose value set has exactly one constant member
```

It has a high false-positive rate — `Field(union_mode=...)` is pydantic config,
`semantic_hash=""` is overwritten on the next line — but every false positive is
dismissed in seconds, and it catches the exact shape that shipped as G1.

### 4c. The handoff's field-assignment grep

Kept for completeness. Over the IR it yields exactly `parse_warnings`,
`source_text`, and one false positive (`result_schema`, populated by attribute
assignment). It cannot see `setattr` / `model_copy(update=…)` population, nor
constants, nor an unconstructed *class* like `LetRef`.

---

## 5. Why these survived review

Three of the findings (A3, A4, B2) each have a test that asserts **only the
default value or the declaration's existence** — `tests/ir/test_ir_builder.py:250-252`
and `:256-261`, and `tests/ir/test_llm_view.py:110-116`. Those tests pass
identically whether the populating code works or has never worked once.

`tests/ir/test_ir_builder.py:263-273` (`test_pipeline_result_schema_field`) has
the same shape but happens to cover a field that does work — which is the point:
the shape of the test tells you nothing either way.

A test asserting a field is **non-default on a real parse** would have caught all
three at authoring time. That is the standard this branch holds new tests to.
