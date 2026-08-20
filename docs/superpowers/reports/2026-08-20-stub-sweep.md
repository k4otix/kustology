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
| A1 | `QueryIR.parse_warnings` | `ir/query.py:593` | Zero assignments anywhere in `src/`. The only other reference is a negative assertion at `tests/ir/test_llm_view.py:126`. `QueryIR.diagnostics` already carries real parser diagnostics (populated at `builder.py:260-300`), and `Unknown*` fallback nodes are reachable via `find_all` — so this is a redundant third channel. | **remove** |
| A2 | `Span.source_text` | `ir/spans.py:16` | Every span is built by `to_span()` (`_builder_helpers.py:92-94`), which passes only `text_start`/`width`. Set manually only at `tests/ir/test_ir_builder.py:98,117`. `Span.text(raw)` already slices from the source and ignores this field entirely. | **remove** |
| A3 | `Expr.nullable` | `ir/expr.py:36` | Dead probe (§1). Always `True`. Not populatable at all — Microsoft's parser carries no nullability information, so the declared comment ("binder flips to False when it can prove non-null") describes behavior that cannot occur. | **remove** |
| A4 | `Expr.result_type_inner` | `ir/expr.py:34` | Dead probe (§1). Always `None`, including for `dynamic([1,2])`, `pack_array`, `mv-expand`. **Is** populatable: `ElementType` on `DynamicArraySymbol`. `binder._walk_operator`'s `MvExpandOp` branch already reads this field. | **populate** |
| A5 | `ExternalDataExpr.uri` | `ir/expr.py:234` | Dead probe (§1) — emits the fake value `"url"`. | **populate** |
| A6 | `ExternalDataExpr.columns` | `ir/expr.py:233` | `cols` is initialised `[]` at `builder.py:1109` and never appended to. The data is at `node.Schema`. `llm_view.py:83` already reads the field. | **populate** |
| A7 | `ExternalDataExpr.format` | `ir/expr.py:235` | Hardcoded `format="unknown"` at `builder.py:1113`. The data is at `node.WithClause`. | **populate** |
| A8 | `ParseKvOp.columns` | `ir/query.py:396` | Dead probe (§1). `T \| parse-kv a as (b:string, c:long)` yields `columns=[]`. | **populate** |
| A9 | `MacroExpandOp.pipeline` | `ir/query.py:422` | Dead probe (§1). Always `None`. | **populate** |
| A10 | **`LetRef` — the whole class** | `ir/query.py:120-125` | Exported at `ir/__init__.py:40,93`, a declared member of the `Pipeline.source` union (`query.py:481`), referenced in an annotation at `builder.py:383` — and **`LetRef(` appears nowhere in `src/`, `tests/`, `examples/` or `scripts/`**. `_visit_pipeline` maps every source-position `NameReference` to `TableRef` (`builder.py:450-461`). Verified: `let X = T \| take 1; X \| count` → `TableRef(name='X')`. A consumer branching on `isinstance(src, LetRef)` gets a branch that never fires; one distinguishing tables from let-aliases gets wrong answers. This is G1 at class granularity. | **populate** |
| A11 | `reflection._safe_first_param_type_name` | `reflection.py:60-75` | Fully implemented, zero call sites anywhere. Not a model field but the same shape. | **remove** |
| A12 | `FuncCallSource.args` on the datatable path | `ir/query.py:155` | Populated for real UDF sources (`builder.py:433`) but hardcoded `args=[]` for `DataTableExpression` (`builder.py:439-441`), dropping the inline values. | **document** — modelling inline datatable rows is a separate feature |

### Pattern A2 — enum members nothing emits

| # | Item | Evidence | Verdict |
|---|---|---|---|
| B1 | `SetMembership.case_sensitive` | Hardcoded `False` at `builder.py:1032`. Verified: `in`, `in~`, `!in`, `!in~` **all** yield `False`. KQL `in` is case-*sensitive*; only `in~` is not. So the field is both constant and **semantically wrong for half its inputs**, and `canonical()` consequently renders `X in ("a")` as `X in~ ("a")`. | **fix — correctness bug** |
| B2 | `KustoType.TABULAR` | `ir/types.py:27`. The only producer is `map_net_type` (`_builder_helpers.py:61`), which needs a .NET symbol whose `Name` is literally `"tabular"`. `TableSymbol.Name` is the table's own name; `ScalarTypes` has no `tabular`. Its only test (`tests/ir/test_ir_builder.py:250-252`) asserts `"TABULAR" in {m.name for m in KustoType}` — a tautology that passes whether or not the member is reachable. | **document** — a legitimate type-system member; the tautological test is the real defect |
| B3 | `RegexMatch.case_sensitive` | Hardcoded `True` at `builder.py:1009`. Domain-defensible (`matches regex` is always case-sensitive) but the field carries no information. | **document** |

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
  genuinely dispatched.
- Tier 1 (`src/kustology/*.py`, `utils/`) is **clean** on Pattern A: no
  declared-but-unassigned attributes, no unreachable `Literal` members. Its
  defects are all Pattern B (§3).

---

## 3. Pattern B — hand-maintained traversal that skips branches

| # | Item | Evidence | Verdict |
|---|---|---|---|
| C1 | `SchemaAttacher._fill` (**F1**) | `binder.py:363-376` recurses a hardcoded tuple omitting `pipeline`, `branches` and `default`. Verified: `SecurityEvent \| where EventID > toscalar(SecurityEvent \| summarize max(EventID)) \| project Account` gives `EventID` `table=SecurityEvent` outside the `toscalar` and `table=None` inside — inconsistent provenance for the same column in one query. Note that adding `"pipeline"` to the tuple would have been a **no-op**: the loop guards `isinstance(child, Expr)` and `Pipeline` is not an `Expr`. | **fix** |
| C2 | `SchemaAttacher._walk_operator` | An `isinstance` chain covering **17 of 53** `Operator` subclasses, with no fallback — it simply falls off the end. Verified: `SecurityEvent \| sort by EventID \| project Account` leaves `EventID` unresolved while `Account` resolves. 36 operator types affected, including `sort`, `top`, `search`, `find`, `mv-apply`, `partition`, `fork`, `range`, `print`, `serialize`, `parse-kv` and the graph operators. | **fix** |
| C3 | `SchemaAttacher._source_entry` | `Pipeline.source` may itself be a `Pipeline` (`query.py:481`, produced for `materialize(P) \| …`); `_source_entry` returns an empty anonymous scope and the inner pipeline is never walked. | **fix** |
| C4 | **Generic `walk()`** | `walk.py:48-58` descends `list` and `dict` values but **not `tuple`**, so `CaseExpr.branches: list[tuple[AnyExpr, AnyExpr]]` is invisible. Verified: a `case()` holding 5 `ColumnRef`s surfaces 1 via `find_all`. This matters disproportionately — AGENTS.md holds `walk`/`find_all` up as the drift-free traversal that bespoke walkers should be converted to. `transforms._normalize_field` and `tests/ir/test_ast_isolation.py:71-90` both handle tuples; `walk` is the odd one out. | **fix — land first** |
| C5 | Corpus gate walkers (**F2**) | `tests/ir/test_complex_harness.py:65-115` and `scripts/mine_corpus.py:65-108` are near-duplicates. Beyond the reported `pipeline` omission they also hand-enumerate *operator* fields via `hasattr`, missing `SortOp.expressions`, `TopOp.by`, `RangeOp.start/end/step`, `FacetOp.with_pipeline`, `MacroExpandOp.pipeline`, `MakeSeriesOp.on_column`, `ParseOp.patterns`, `SampleDistinctOp.of`, `JoinOp.on`. `test_complex_harness._walk_expr:81-82` additionally re-walks `NamedExpr.expression`, already covered by `"expression"` in its own tuple, double-counting unknowns beneath it. | **fix** |
| C6 | `scripts/verify_corpus.py:174-189` | A third, weaker copy of `walk` — no dict branch, no tuple branch, and applied to `main_pipeline` only. | **fix** |
| C7 | `_normalize.canonical()` | `_normalize.py:69-112` handles 12 of 23 `Expr` types; the remaining 11 hit the `"?"` fallthrough at line 112. Verified: `-X > 1`, `D.a == 1` and `toscalar(…) > 1` **all** render as `"? > 1"`. Not hash-affecting (`canonical_form` is a property excluded from `model_dump`) but user-visible. | **fix** |
| C8 | Tier 1 `_collect_table_refs` | `utils/analysis.py:125-167` omits the `find in (...)` and `search in (...)` clauses. Verified: `find in (S1, S2) where X == 1` → `get_referenced_tables()` returns `set()`, and `replace_table("S1","New")` **silently returns the query unchanged** — the worst failure mode for a rewriting API, since a consumer migrating tables ships one still pointing at the old name. **Correction:** an earlier draft of this row also named `PartitionOperator`. That was wrong — `partition by K (B \| …)` is a parse error ("Query operator expected"), because the subquery runs on the partitioned rows rather than a new source. There is no table position there to miss. | **fix** |
| C9 | `llm_view` dispatch | `llm_view.py:100-156` keys on class *name strings* (`"ColumnRef"`, `"BinOp"`, …), so a rename silently disables the rule. Not a traversal gap, same silence. | **fix** |
| C10 | `_VOLATILE_FIELDS` | `transforms.py:152-154` is hand-maintained: any new bind-populated annotation field must be added or the hash diverges between bound and unbound parses. | **document** — versioned by `SEMANTIC_HASH_SCHEME`, drift is at least visible |

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

The handoff supplied one heuristic. It found two of the twelve Pattern A items.
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
