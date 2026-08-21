# Kustology 0.2.0 Pre-Release Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding in the 0.2.0 pre-release review (K01–K42 + K-ARCH-1..5) on `main` before the `v0.2.0` tag, leaving one coherent IR shape, a sound `semantic_hash`, a correct Tier 1, accurate docs, and a release pipeline that cannot publish an untested or mis-versioned build.

**Architecture:** Eight workstreams, ordered so safety nets land first, the IR model change lands once (before the binder depends on its fields), and docs land last against the final shape. Model/builder/binder/Tier 1 each get one short-lived branch merged to `main`; nothing is released in the window so `IR_SCHEMA_VERSION="0.2"` and `SEMANTIC_HASH_SCHEME="kustology-sem-v2"` do not move.

**Tech Stack:** Python 3.10–3.13, pythonnet + Microsoft.Azure.Kusto.Language 12.3.2 (bundled DLL), pydantic v2, pytest, ruff, mypy, uv, GitHub Actions.

**Spec:** the published review (`/private/tmp/claude-501/-Users-eddie-Documents-repos-kustology/bbb566d3-62e4-4b10-91b7-68bebf071f3a/scratchpad/lead/kustology-0.2.0-review.html`, artifact https://claude.ai/code/artifact/df894d61-19eb-434a-bf0e-84123668e713) and the lead's raw notes (`…/scratchpad/lead/lead-findings.md`). Repro scripts for every finding: `…/scratchpad/{lead,agentA..G}/`. Task 0.1 copies both into the repo so the spec travels with the plan.

## Context

A seven-reviewer audit of `main @ 26a5da3` found that the green baseline (442 tests, ruff, mypy, audit, build, twine) hides: three crash classes on valid KQL (`take n`, `top-hitters`, `__partitionby`); a host-timezone-dependent `semantic_hash`; an unsound `tolower` rewrite baked into the hash; a dozen lossy-lowering collisions (sort direction, fork, datatable, `join` default kind, …); a family of wrong-schema bugs in `SchemaAttacher` that Microsoft's own binder already answers correctly; a systemic comment-trivia bug across every syntactic Tier 1 analyzer; release plumbing that publishes without tests. The maintainer decided: (1) the hash sorts commutative operands and canonicalizes `let` names; (2) schemaless `to_ir()` analyzes against `GlobalState.Default`; (3) bound parses take result schemas from the binder, and the hand-rolled fallback is fixed too; (4) every P3 enhancement is in scope. All of it lands pre-tag.

## Global Constraints

- All work lands on `main` before `git tag v0.2.0`. `IR_SCHEMA_VERSION` stays `"0.2"`; `SEMANTIC_HASH_SCHEME` stays `"kustology-sem-v2"` (CONTRIBUTING step 6: one bump per release). Stored 0.2-dev IR JSON will stop loading under `extra="forbid"` — acceptable, nothing is released.
- Python is always `.venv/bin/python` (3.12). pythonnet member lookup is exact and silent for `getattr`, raising for direct access — confirm every .NET member with `[m for m in dir(node) if m[:1].isupper()]` before using it (AGENTS.md). All member names used below were verified on 12.3.2.
- Every behavior change ships with a test that asserts a **non-default value on a real parse** (AGENTS.md "A test that asserts a default proves nothing"), and a `CHANGELOG.md` entry appended to the existing `## [0.2.0]` section (not `[Unreleased]` — 0.2.0 is unreleased but already has its section).
- `IRBuilder.HANDLED_OPERATOR_KINDS` / `HANDLED_EXPR_KINDS` are a public contract read by `scripts/audit_syntax_kinds.py`; after any change run `python scripts/audit_syntax_kinds.py --update-baseline` and commit `tests/fixtures/syntax_kinds_baseline.json`.
- Gates that must stay green after every task: `.venv/bin/python -m pytest -q`, `ruff check src tests scripts examples`, `mypy src`, `python scripts/audit_syntax_kinds.py --check`, `python scripts/mine_corpus.py`, `for f in examples/*.py; do .venv/bin/python "$f" >/dev/null || echo FAIL $f; done`.
- Conventional commit subjects (`fix(ir): …`, `feat(ir)!: …`, `docs: …`, `ci: …`, `test: …`); every commit ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Branch per workstream, merge to `main` locally (the maintainer's stated preference) — present the finish menu (merge / PR / keep) at each workstream's end.
- `extra="forbid"` is on every IR model; `Pipeline.operators` Union ordering rule (query.py:496-503): fields-less ops first, defaulted-field ops after, `UnknownOp` last.
- Do not edit `docs/superpowers/handoffs/`, `specs/`, `plans/` history files; `docs/superpowers/reports/2026-08-20-stub-sweep.md` disposition markers *are* live and get updated (Task 6.3).

## Decisions (maintainer's calls + reconciliation with the independent Plan agent)

| # | Decision | Outcome |
|---|---|---|
| D1 | Bound result schemas | From Microsoft's per-operator `ResultType` **and** fix the hand-rolled walk (provenance + dict-only path still need it) — Task 5.2/5.3 |
| D2 | Commutative sort for the hash | Yes — but inside `compute_semantic_hash`'s deep copy (private `_sort_commutative`), **not** in the public in-place `normalize_expressions` — Task 2.4 |
| D3 | `let`-name canonicalization | Yes (maintainer's call; the agent preferred deferral) — positional `$let0…` on the hash copy over `LetBinding`/`LetRef`/`LetValueRef` — Task 2.4/4.6 |
| D4 | Multi-statement queries | `QueryIR.additional_pipelines`; **must be added to the hash payload** (`transforms.py:220-223` hardcodes `{let_bindings, main_pipeline}`) — Task 4.5 |
| D5 | Schemaless typing | `to_ir()` on an unbound parse calls `KustoCode.Analyze(GlobalState.Default)` (returns a bound `KustoCode` without re-parsing); KS204 filtered in both `build()` and `to_ir()` — Task 5.1 |
| D6 | P3 scope | All in (maintainer's call): `hints` (excluded from the hash by field), K40 flags, K41, K42 |
| D7 | K17 | Union semantic + uncovered syntactic refs for both `get_referenced_tables` and `replace_table`; depends on Task 3.2 landing first (it removes the syntactic false positives) |
| D8 | Unwritten modifiers | Emit KQL's **effective** default, never `None`: `join`→`innerunique`, `lookup`→`leftouter`, `union kind`→`outer`, `parse kind`→`simple`, sort/top direction→`desc`. Fields that carry an effective default are **required** (no pydantic default) so `to_llm_dict` renders them |
| D9 | `raw_text` | Build: `node.ToString(IncludeTrivia.Minimal)` (faithful, no leading trivia). Hash: whitespace/comment normalization on the deep copy only |
| D10 | K26 | `id()` visited-set in `walk`; keep `inner_time_exprs` |
| D11 | K01 | `count: int | AnyExpr` (int for the literal case keeps `op.count == 5` working); never `UnknownOp` |
| D12 | `let X = externaldata(...)` | Becomes `rhs_pipeline=Pipeline(source=ExternalDataSource, operators=[])`; the `_is_tabular_let_rhs` exclusion paragraph is deleted |
| D13 | Typed captures | New `TypedNameDecl(Expr)` node for `NameAndTypeDeclaration` (not a `declared_type` field on `ColumnRef`) |
| D14 | xfail policy | Tests land in the same commit as their fix; the only strict-xfail is Task 2.4's let-rename pair, removed in Task 4.6 (WS4 follows WS2 sequentially, so no cross-worktree XPASS) |

## Workstreams, dependencies and concurrency

```
WS0 safety nets ─┬─► WS1 crashes + release plumbing ──┐
                 ├─► WS2 hash hygiene ─────────────────┼─► WS4 IR model ─► WS5 binder ─► WS6 docs/examples ─► WS8 release
                 ├─► WS3 Tier 1 ───────────────────────┤
                 └─► WS7 CI / scripts ─────────────────┘
```

| WS | Findings | Files touched (for conflict planning) | Runs concurrently with |
|---|---|---|---|
| 0 | K32, K35, K02-audit | tests/ir/test_complex_harness.py, scripts/mine_corpus.py, scripts/verify_corpus.py, tests/test_reflection_audit.py, tests/ir/test_schema_tags.py (new), tests/ir/test_canonical_coverage.py (new), tests/test_cli_inprocess.py (new), docs/superpowers/reports/, docs/superpowers/plans/ | — (first) |
| 1 | K01, K02, K19, K20, K21 | query.py (count fields), builder.py (5 lines), pyproject.toml, MANIFEST.in, .github/workflows/{test,release}.yml | WS2, WS3, WS7 (no file overlap except test.yml → WS7 owns test.yml; WS1 owns release.yml) |
| 2 | K03, K04, K06, K34-hash | _builder_helpers.py, _normalize.py, transforms.py, llm_view.py (`_OMIT_FIELDS`), tests/ir/test_hash_battery.py (new) | WS1, WS3, WS7 |
| 3 | K15, K16, K17, K18, K29, K30, K31, K41 | utils/analysis.py, utils/walker.py, reflection.py, cli.py, core.py, services.py, __init__.py, utils/schema_state.py, tests/test_tier1_comments.py (new), tests/test_cli*.py | WS1, WS2, WS7 |
| 4 | K05, K22, K24, K25, K26, K39, K40 | query.py, expr.py, builder.py, _builder_helpers.py, _normalize.py, llm_view.py, walk.py, ir/__init__.py, baseline JSON | — (after WS1–WS3 merge) |
| 5 | K07–K14, K27, K28, K36, K-ARCH-1 | binder.py, builder.py (operator schema capture), core.py (`to_ir`), services.py, utils/schema_state.py, tests/ir/test_binder_oracle.py (new) | — (after WS4) |
| 6 | K33, K37, K42, all doc bullets | CHANGELOG.md, README.md, ARCHITECTURE.md, AGENTS.md, CONTRIBUTING.md, .github templates, docstrings, examples/, stub-sweep report | — (after WS5) |
| 7 | K38, K20-leftovers | .github/workflows/test.yml, canary.yml, dependabot, .pre-commit-config.yaml, .gitignore, scripts/*.py | WS1–WS3 |
| 8 | release | CHANGELOG.md final pass, tag | last |

Rule of thumb for executors: WS1/WS2/WS3/WS7 may run in separate worktrees (`superpowers:using-git-worktrees`) and merge in any order; WS4 starts only after all four are on `main`; WS5 after WS4; WS6 after WS5.

---

## WS0 — Safety nets (land first, all green today)

### Task 0.1: Archive the review and this plan in the repo

**Files:**
- Create: `docs/superpowers/reports/2026-08-20-pre-release-review.html` (copy of the review HTML)
- Create: `docs/superpowers/reports/2026-08-20-pre-release-review-notes.md` (copy of `lead-findings.md`)
- Create: `docs/superpowers/plans/2026-08-20-pre-release-remediation.md` (this plan)

- [ ] **Step 1:** `cp` the three files from the scratchpad paths in **Spec** into place.
- [ ] **Step 2:** Commit: `git commit -m "docs: archive the 0.2.0 pre-release review and remediation plan"`

### Task 0.2: Corpus gates see `UnknownOp` (K32)

**Files:** Modify `tests/ir/test_complex_harness.py:82-89,126`, `scripts/mine_corpus.py:85`, `scripts/verify_corpus.py:187-191`.

- [ ] **Step 1: Write the failing self-test** in `tests/ir/test_complex_harness.py`:
```python
def test_gate_sees_unknown_op():
    """`T | reduce by X` is not dispatched by the builder and must trip the gate."""
    from kustology.ir import UnknownOp
    ir = parse("T | reduce by X").to_ir()
    assert list(find_all(ir, UnknownOp)), "reduce should fall through to UnknownOp"
    unknown_ops, *_ = _scan(ir)          # _scan returns the Unknown buckets
    assert unknown_ops, "the gate must surface UnknownOp, not only bare Operator"
```
- [ ] **Step 2:** Run `pytest tests/ir/test_complex_harness.py::test_gate_sees_unknown_op -v` — FAIL (the gate's bucket is empty because it filters `type(op) is Operator`).
- [ ] **Step 3:** In `_scan` replace `[op for op in find_all(ir, Operator) if type(op) is Operator]` with `list(find_all(ir, UnknownOp))` (import it), keep the `UnknownExpr`/`UnknownSource`/degraded-let buckets, and update the assertion message to name `UnknownOp`. Make the identical change in `scripts/mine_corpus.py` (line 85; count them under `"<UnknownOp>"` with `op.ast_kind` as the example), `scripts/verify_corpus.py` (add an `isinstance(n, UnknownOp)` branch), **and the fourth site `tests/ir/test_ir_builder.py:334`**.
- [ ] **Step 4:** Run the harness + `python scripts/mine_corpus.py` — PASS (corpus is clean: 0 UnknownOp).
- [ ] **Step 5:** Commit: `test(ir): make the corpus gates see UnknownOp`

### Task 0.3: Pin the schema tags and HANDLED_* validity

**Files:** Create `tests/ir/test_schema_tags.py`; Modify `src/kustology/ir/builder.py:281-298` (drop dead entries), `src/kustology/ir/builder.py:1073-1079` (delete dead branch) — K35.

- [ ] **Step 1: Write tests:**
```python
import pytest
from kustology.ir import IR_SCHEMA_VERSION, SEMANTIC_HASH_SCHEME, IRBuilder

def test_schema_tags_are_pinned_and_in_lockstep():
    assert IR_SCHEMA_VERSION == "0.2"
    assert SEMANTIC_HASH_SCHEME == "kustology-sem-v2"
    # lockstep: the scheme's major equals the IR minor (v2 <-> 0.2)
    assert SEMANTIC_HASH_SCHEME.rsplit("v", 1)[1] == IR_SCHEMA_VERSION.split(".")[1]

def test_handled_kinds_are_real_syntax_classes():
    import Kusto.Language.Syntax as S
    missing = sorted(k for k in IRBuilder.HANDLED_OPERATOR_KINDS | IRBuilder.HANDLED_EXPR_KINDS if not hasattr(S, k))
    assert missing == [], f"not classes in Kusto.Language.Syntax: {missing}"
```
- [ ] **Step 2:** Run — second test FAILS on `AndExpression`, `OrExpression`.
- [ ] **Step 3:** Remove `"AndExpression", "OrExpression"` from `HANDLED_EXPR_KINDS`; delete the `elif kind in ("AndExpression", "OrExpression"):` branch in `_visit_expr` (the `BinaryExpression` branch's `op == "and"/"or"` arms already cover it). Run `python scripts/audit_syntax_kinds.py --update-baseline`.
- [ ] **Step 4:** Run tests + `--check` — PASS. Commit: `test(ir): pin the schema tags; drop the dead And/OrExpression dispatch`

### Task 0.4: `canonical()` coverage over every `Expr` subclass

**Files:** Create `tests/ir/test_canonical_coverage.py`.

- [ ] **Step 1:**
```python
from kustology.ir import expr as E
from kustology.ir._normalize import canonical

def _all_expr_subclasses(cls=E.Expr):
    out = set()
    for sub in cls.__subclasses__():
        out.add(sub); out |= _all_expr_subclasses(sub)
    return out

def test_every_expr_subclass_has_a_render_branch():
    import inspect
    src = inspect.getsource(canonical)
    missing = sorted(c.__name__ for c in _all_expr_subclasses() if c.__name__ not in src and c is not E.UnknownExpr)
    assert missing == [], f"canonical() has no branch for: {missing}"
```
(UnknownExpr is rendered by the `raw_text` fallthrough, so it is exempt; every other subclass must be named in the function body.)
- [ ] **Step 2:** Run — PASS today (22 types). Commit: `test(ir): guard canonical() against silent '?' fallthrough`

### Task 0.5: In-process CLI tests so `cli.py` is measured

**Files:** Create `tests/test_cli_inprocess.py` (keep the subprocess tests in `tests/test_cli.py` as the end-to-end layer).

- [ ] **Step 1:** Port each subprocess case to `from kustology.cli import main; rc = main(["validate", str(path)])` with `capsys`; assert exit codes and output shape. Include `version`, `format`, `validate [--json]`, `parse [--json]`, `parse --ir [--json]`, stdin via `monkeypatch.setattr(sys, "stdin", io.StringIO(...))`, `KUSTOLOGY_MAX_INPUT_BYTES`.
- [ ] **Step 2:** `pytest --cov=kustology --cov-report=term-missing tests/test_cli_inprocess.py` shows `cli.py` > 80%.
- [ ] **Step 3 (DEBUG-logging gap):** in `tests/ir/test_ir_builder.py` add
```python
def test_semantic_info_probe_fallthrough_logs_debug(caplog):
    from types import SimpleNamespace
    from kustology.ir._builder_helpers import map_semantic_info
    class _Boom:                       # ElementType exists but Name raises
        @property
        def Name(self): raise RuntimeError("probe")
    node = SimpleNamespace(ResultType=SimpleNamespace(Name="dynamic", ElementType=_Boom()))
    expr = LiteralExpr(value="[]", literal_kind="dynamic", span=Span(text_start=0, width=2))
    with caplog.at_level(logging.DEBUG, logger="kustology.ir._builder_helpers"):
        map_semantic_info(node, expr)
    assert expr.result_type == KustoType.DYNAMIC and "inner result-type probe fell through" in caplog.text
```
Commit: `test(cli): exercise the CLI in-process so coverage is measured; cover the DEBUG probe path`

---

## WS1 — Crashes and release plumbing

### Task 1.1: Non-literal row counts (K01)

**Files:** Modify `src/kustology/ir/query.py` (TakeOp, SampleOp, TopOp, TopHittersOp, SampleDistinctOp), `src/kustology/ir/builder.py` (the five branches), `src/kustology/ir/_builder_helpers.py` (delete `safe_int`), tests in `tests/ir/test_ir_builder.py`.

**Interfaces — Produces:** `TakeOp.count: int | AnyExpr`, `SampleOp.count: int | AnyExpr`, `TopOp.count: int | AnyExpr`, `TopHittersOp.count: int | AnyExpr`, `SampleDistinctOp.count: int | AnyExpr` — `int` when the source wrote an integer literal (so `op.count == 5` keeps working), otherwise the visited expression (`LetValueRef` after WS4, `ToScalarExpr`, …). Builder helper `_visit_count(node) -> int | AnyExpr`: `if str(node.Kind) in ("LongLiteralExpression","IntLiteralExpression"): return int(node.LiteralValue)` else `self._visit_expr(node)`.

- [ ] **Step 1: Failing tests:**
```python
@pytest.mark.parametrize("q", [
    "let n = 10; T | take n",
    "T | take toscalar(U | count)",
    "let n = 5; T | top n by x",
    "let n = 3; T | sample n",
])
def test_non_literal_counts_build(q):
    ir = parse(q).to_ir()                       # must not raise
    op = ir.main_pipeline.operators[-1]
    assert not isinstance(op.count, int)        # an expression, not a number

def test_literal_take_count_is_int():
    op = parse("T | take 5").to_ir().main_pipeline.operators[0]
    assert op.count == 5 and isinstance(op.count, int)
```
- [ ] **Step 2:** Run — FAIL with `ValueError`.
- [ ] **Step 3:** Change the five fields to `count: int | AnyExpr`; add `_visit_count` and use it in all five branches; delete `safe_int` and its import. Existing `op.count == 5` assertions keep passing.
- [ ] **Step 4:** Full suite green. CHANGELOG `### Breaking`: "`TakeOp/SampleOp/TopOp/TopHittersOp/SampleDistinctOp.count` is `int | AnyExpr` (was `int`); `let n = 10; T | take n` and `take toscalar(...)` no longer raise." Commit: `fix(ir)!: model non-literal take/sample/top counts instead of raising`

### Task 1.2: `top-hitters` / `__partitionby` crashes + audit extension + HANDLED smoke test (K02)

**Files:** Modify `src/kustology/ir/builder.py:600-601, 690`, `src/kustology/ir/query.py` (TopHittersOp), `tests/test_reflection_audit.py`, create `tests/ir/test_handled_kinds_smoke.py`.

**Interfaces — Produces:** `TopHittersOp.of: AnyExpr | None` (the `of X` column), `TopHittersOp.by: AnyExpr | None`.

- [ ] **Step 1: Failing tests.** In `tests/ir/test_handled_kinds_smoke.py` build one query per operator kind and assert no exception and no `UnknownOp`:
```python
SAMPLES = {
  "FilterOperator": "T | where a == 1", "ExtendOperator": "T | extend b = 1", "SummarizeOperator": "T | summarize count() by a",
  "JoinOperator": "T | join (U) on a", "LookupOperator": "T | lookup (U) on a", "PartitionByOperator": "T | __partitionby a (take 1)",
  "PartitionOperator": "T | partition by a (top 1 by b)", "ProjectOperator": "T | project a", "ProjectAwayOperator": "T | project-away a",
  "ProjectKeepOperator": "T | project-keep a", "ProjectReorderOperator": "T | project-reorder a", "ProjectRenameOperator": "T | project-rename b = a",
  "ProjectByNamesOperator": "T | project-by-names a", "DistinctOperator": "T | distinct a", "TakeOperator": "T | take 1", "SampleOperator": "T | sample 1",
  "SortOperator": "T | sort by a", "TopOperator": "T | top 1 by a", "TopHittersOperator": "T | top-hitters 5 of a by b", "SearchOperator": "search 'x'",
  "UnionOperator": "union T, U", "MakeSeriesOperator": "T | make-series n=count() on t step 1h", "MvExpandOperator": "T | mv-expand a",
  "MvApplyOperator": "T | mv-apply a on (where a > 1)", "ParseOperator": "T | parse a with 'x' b", "ParseWhereOperator": "T | parse-where a with 'x' b",
  "AsOperator": "T | as X", "RangeOperator": "range x from 1 to 3 step 1", "RenderOperator": "T | render timechart", "EvaluateOperator": "T | evaluate bag_unpack(d)",
  "CountOperator": "T | count", "PrintOperator": "print 1", "FacetOperator": "T | facet by a", "GetSchemaOperator": "T | getschema", "InvokeOperator": "T | invoke f()",
  "FindOperator": "find in (T) where a == 1", "ForkOperator": "T | fork (take 1) (count)", "ScanOperator": "T | scan declare (s:long=0) with (step x: true => s = 1;)",
  "SerializeOperator": "T | serialize", "ConsumeOperator": "T | consume", "AssertSchemaOperator": "T | assert-schema (a:long)", "ExecuteAndCacheOperator": "T | __executeAndCache",
  "ParseKvOperator": "T | parse-kv a as (k:string)", "SampleDistinctOperator": "T | sample-distinct 1 of a", "TopNestedOperator": "T | top-nested 1 of a by count()",
  "MakeGraphOperator": "T | make-graph a --> b", "MacroExpandOperator": "macro-expand X as Y (T | count)", "GraphMatchOperator": "T | make-graph a --> b | graph-match (n)-[e]->(m) project n",
  "GraphMarkComponentsOperator": "T | make-graph a --> b | graph-mark-components", "GraphShortestPathsOperator": "T | make-graph a --> b | graph-shortest-paths (n)-[e*1..2]->(m) project n",
  "GraphToTableOperator": "T | make-graph a --> b | graph-to-table nodes", "GraphWhereEdgesOperator": "T | make-graph a --> b | graph-where-edges a == 1",
  "GraphWhereNodesOperator": "T | make-graph a --> b | graph-where-nodes a == 1",
}
def test_sample_covers_every_handled_operator_kind():
    assert set(SAMPLES) == set(IRBuilder.HANDLED_OPERATOR_KINDS)
@pytest.mark.parametrize("kind,q", sorted(SAMPLES.items()))
def test_every_handled_operator_builds_without_unknown_op(kind, q):
    ir = parse(q).to_ir()
    assert not list(find_all(ir, UnknownOp)), kind
```
(If a sample's KQL proves wrong for the graph operators, adjust the sample — the contract is "every HANDLED kind has a buildable sample".) In `tests/ir/test_ir_builder.py` add:
```python
def test_top_hitters_reads_of_and_by():
    op = parse("T | top-hitters 5 of a by b").to_ir().main_pipeline.operators[0]
    assert op.of.canonical_form == "a" and op.by.canonical_form == "b" and op.count.value == 5
def test_partitionby_builds():
    op = parse("T | __partitionby a (take 1)").to_ir().main_pipeline.operators[0]
    assert op.by.canonical_form == "a" and op.right.operators
```
- [ ] **Step 2:** Run — FAIL with `AttributeError`.
- [ ] **Step 3:** builder.py: `TopHittersOperator` → `TopHittersOp(count=self._visit_expr(n.Expression), of=self._visit_expr(n.OfExpression) if n.OfExpression is not None else None, by=self._visit_expr(n.ByClause.Expression) if n.ByClause is not None else None, span=span)`; `PartitionByOperator` → `by=self._visit_expr(n.Entity)`, `right=self._visit_pipeline(n.Subquery)`. query.py: add `of: AnyExpr | None = None` to `TopHittersOp`, make `by: AnyExpr | None = None`.
- [ ] **Step 4:** Extend `tests/test_reflection_audit.py::_probed_member_names` to also collect `ast.Attribute` nodes whose `.attr` is PascalCase (regex `^[A-Z][A-Za-z0-9]+$`) from `src/`. Exclude **structurally**, not by allowlist: skip attributes whose base `ast.Name` is an imported module/alias in that file (`argparse`, `System`, `CultureInfo`, `Thread`, `DateTime`, `DateTimeKind`, `Enum`, `clr`, `GlobalState`, `KustoCode`), and skip annotation subtrees. Keep `ALLOWED_ELSEWHERE` empty (its stale-entry test stays a real gate). Run it — it must pass now that `ValueExpression`/`Expression`-on-PartitionBy are gone, and would have failed before.
- [ ] **Step 5:** Full suite, audit `--check`. CHANGELOG Fixed: "`top-hitters` and `__partitionby` no longer raise; `TopHittersOp` gains `of`." Commit: `fix(ir): build top-hitters and __partitionby; audit direct .NET member access`

### Task 1.3: setuptools floor (K19)

- [ ] **Step 1:** `pyproject.toml` line 2 → `requires = ["setuptools>=77.0.3"]` (drop `"wheel"`).
- [ ] **Step 2:** `.venv/bin/python -m build --outdir /tmp/kustology-dist` succeeds; `uv lock --check` still OK. Commit: `build: require setuptools>=77 for PEP 639 license metadata`

### Task 1.4: Release workflow (K20)

**Files:** Modify `.github/workflows/test.yml` (add `workflow_call` trigger only — WS7 owns the rest), `.github/workflows/release.yml`.

- [ ] **Step 1:** In `test.yml` `on:` add `workflow_call:` (keeps push/pull_request).
- [ ] **Step 2:** In `release.yml` add a first job:
```yaml
  tests:
    uses: ./.github/workflows/test.yml
    secrets: inherit
  build:
    needs: tests
    ...
      - name: Verify tag matches project version and CHANGELOG
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          PROJECT=$(python -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
          test "$VERSION" = "$PROJECT" || { echo "tag $GITHUB_REF_NAME != pyproject $PROJECT"; exit 1; }
          grep -q "^## \[$VERSION\]" CHANGELOG.md || { echo "CHANGELOG has no [$VERSION] section"; exit 1; }
      - name: Extract release notes        # moved here from publish
        run: <existing awk block> ; test -s release-notes.md
```
and upload `release-notes.md` with the artifact; in `publish` download it and drop the extraction step. Pin `python -m pip install build==<ver> cyclonedx-bom==<ver>` to the versions in `uv.lock`, and point the SBOM at the installed wheel: `cyclonedx-py environment "$(python -c 'import sys;print(sys.prefix)')"` after `pip install dist/*.whl` into a fresh venv (mirror the `test.yml` sbom job). Guard the version step with `if: startsWith(github.ref, 'refs/tags/v')` so `workflow_dispatch` still builds.
- [ ] **Step 3:** `act`-free validation: `python -c "import yaml;yaml.safe_load(open('.github/workflows/release.yml'))"`; simulate the guard locally with `GITHUB_REF_NAME=v0.2.0 bash -c '<the run block>'` → exit 0, and `v0.2.1` → exit 1. Commit: `ci(release): run tests, guard tag↔version, extract notes before publish`

### Task 1.5: sdist contents (K21)

- [ ] **Step 1:** Create `MANIFEST.in`:
```
graft tests
graft examples
graft scripts
include CHANGELOG.md ARCHITECTURE.md AGENTS.md CONTRIBUTING.md SECURITY.md
prune src/kustology.egg-info
global-exclude __pycache__ *.py[cod] .coverage
```
- [ ] **Step 2:** Build; `tar tzf dist/*.tar.gz | grep -c "tests/ir/"` > 0; in a scratch venv `pip install dist/*.tar.gz` then `pytest` from the unpacked sdist runs the full suite. Commit: `build: ship the whole test suite and examples in the sdist`

---

## WS2 — Hash hygiene

### Task 2.1: UTC-normalize datetime literals (K03)

**Files:** Modify `src/kustology/ir/_builder_helpers.py:212`, `tests/ir/test_literals.py`.

- [ ] **Step 1: Failing tests** (in-process + subprocess with a different TZ):
```python
def test_datetime_literal_is_utc_and_tz_independent():
    q = "T | where d > datetime(2024-01-01T00:00:00Z)"
    lit = next(l for l in find_all(parse(q).to_ir(), LiteralExpr) if l.literal_kind == "datetime")
    assert lit.value == "2024-01-01T00:00:00.0000000Z" and lit.ticks == 638396640000000000
    here = parse(q).to_ir().semantic_hash
    other = subprocess.run([sys.executable, "-c", f"from kustology import parse; print(parse({q!r}).to_ir().semantic_hash)"],
                           env={**os.environ, "TZ": "Asia/Tokyo"}, capture_output=True, text=True, check=True).stdout.strip()
    assert here == other
def test_naive_and_zulu_datetime_hash_equal():
    assert parse("T | where d > datetime(2024-01-01)").to_ir().semantic_hash == parse("T | where d > datetime(2024-01-01T00:00:00Z)").to_ir().semantic_hash
```
- [ ] **Step 2:** Run — FAIL (value carries the local offset).
- [ ] **Step 3:** In `literal_value_and_ticks`, datetime branch:
```python
from System import DateTime, DateTimeKind
if raw.Kind == DateTimeKind.Local:
    raw = raw.ToUniversalTime()
elif raw.Kind == DateTimeKind.Unspecified:
    raw = DateTime.SpecifyKind(raw, DateTimeKind.Utc)   # KQL datetimes are UTC
return raw.ToString("o", CultureInfo.InvariantCulture), raw.Ticks
```
- [ ] **Step 4:** PASS. CHANGELOG Fixed: "`datetime` literal `value`/`ticks`/`semantic_hash` no longer depend on the host timezone." Commit: `fix(ir): normalize datetime literals to UTC before rendering and hashing`

### Task 2.2: Sound `tolower` rewrite (K04)

**Files:** `src/kustology/ir/_normalize.py:50-57`, tests in `tests/ir/test_ir_builder.py`.

- [ ] **Step 1: Failing tests:** `tolower(X) == "Y"` hash ≠ `X =~ "Y"`; `tolower(X) == "y"` == `X =~ "y"`; `"y" == tolower(X)` == `X =~ "y"`; `toupper(X) == "Y"` == `X =~ "Y"`; `tolower(X) == Col` ≠ `X =~ Col`.
- [ ] **Step 2:** Run — the `!=` assertions FAIL.
- [ ] **Step 3:** Replace the BinOp branch with:
```python
def _case_fold_side(e):
    return isinstance(e, FuncCall) and e.name.lower() in ("tolower", "toupper") and len(e.args) == 1
def _literal_matches_fold(lit, fn):
    return (isinstance(lit, LiteralExpr) and lit.literal_kind == "string" and isinstance(lit.value, str)
            and lit.value == (lit.value.lower() if fn == "tolower" else lit.value.upper()))
if isinstance(expr, BinOp) and expr.op in ("==", "!="):
    for fold_side, other_side in (("left", "right"), ("right", "left")):
        fold, other = getattr(expr, fold_side), getattr(expr, other_side)
        if _case_fold_side(fold) and _literal_matches_fold(other, fold.name.lower()):
            expr.op = "=~" if expr.op == "==" else "!~"; expr.case_sensitive = False
            setattr(expr, fold_side, fold.args[0]); break
```
(Non-literal right sides are no longer rewritten — `tolower(X) == Col` is not equivalent to `X =~ Col`.)
- [ ] **Step 4:** PASS; update the `normalize_expressions` docstring bullet list. Commit: `fix(ir): only rewrite tolower/toupper equality against a matching-case literal`

### Task 2.3: Strip volatile fields by model field; body_span; raw_text; root double negation (K06)

**Files:** `src/kustology/ir/transforms.py`, `src/kustology/ir/llm_view.py` (`_OMIT_FIELDS`), `src/kustology/ir/builder.py` (raw_text cleaning), tests in `tests/ir/test_semantic_hash_bind_invariance.py` and `tests/ir/test_ir_builder.py`.

**Interfaces — Produces:** `transforms._VOLATILE_FIELDS` becomes a per-model-field set `{"span","body_span","result_type","result_type_inner","table","result_schema"}` applied as attribute clears on the deep copy — `span`/`body_span` are set to `Span(text_start=0, width=0)` (they are required fields; `None` would trip pydantic's serializer), the rest to their declared default (WS4 adds `"hints"`; WS5's `Operator.result_schema` shares the name). `normalize_expressions(root) -> root'` returns the (possibly replaced) root. `transforms._normalize_raw_text(text) -> str` (hash copy only) strips `//…` comments and collapses whitespace. Builder: every `raw_text=node.ToString()` becomes `raw_text=node.ToString(IncludeTrivia.Minimal)` — faithful source, no leading trivia (D9).

- [ ] **Step 1: Failing tests:** (a) comment before `let f = (x:int){x+1}; T | extend y = f(a)` does not change the hash; (b) `T | assert-schema (a:long, table:long)` ≠ `(a:long)`; (c) `T | top-nested 3 of a by max(b)` == `T\n|   top-nested 3 of a by max(b)` and == with a `// c` comment; (d) `compute_semantic_hash(pred_not_not) == compute_semantic_hash(pred_plain)` for bare `Expr` roots; (e) `to_llm_dict` of a let-function has no `body_span` key.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** transforms.py: replace `_strip_volatile_fields` with
```python
_ZERO_SPAN = Span(text_start=0, width=0)
def _clear_volatile(root: BaseModel) -> None:
    for node in walk(root):
        fields = type(node).model_fields
        for name in _VOLATILE_FIELDS & fields.keys():
            if name in ("span", "body_span"):
                object.__setattr__(node, name, _ZERO_SPAN)
            else:
                d = fields[name].default
                object.__setattr__(node, name, None if d is PydanticUndefined else d)
        if "raw_text" in fields:
            object.__setattr__(node, "raw_text", _normalize_raw_text(node.raw_text))
```
called on the deep copy **before** `model_dump`; `payload` is then dumped without key filtering. `_normalize_raw_text = lambda t: " ".join(re.sub(r"//[^\n]*", "", t).split())`. `_normalize_node` already returns the replacement; make `normalize_expressions` `return _normalize_node(root)` and in `compute_semantic_hash` write `canonical = normalize_expressions(canonical)`. llm_view: `_OMIT_FIELDS = {"span", "body_span", "schema_attached", "ticks"}`. builder: `from Kusto.Language.Syntax import IncludeTrivia`; every `raw_text=node.ToString()` → `raw_text=node.ToString(IncludeTrivia.Minimal)`.
- [ ] **Step 4:** PASS; bind-invariance tests still pass. Commit: `fix(ir): strip volatile fields by model field; hash-neutral raw_text and body_span`

### Task 2.4: Hash canonicalization decisions — commutative sort + let-name canonicalization (K34)

**Files:** `src/kustology/ir/_normalize.py`, `src/kustology/ir/transforms.py`, docstrings of `compute_semantic_hash`, AGENTS.md line ~212 (WS6 will finalize wording).

**Interfaces — Produces:** `transforms._sort_commutative(root) -> None` — on the **hash's deep copy only** (D2; `normalize_expressions` stays a faithful public transform) — sorts `And.operands`, `Or.operands` and `SetMembership.values` by `json.dumps(child.model_dump(mode="json"), sort_keys=True)` (stable, span-cleared first so offsets don't order). `transforms._canonicalize_let_names(ir: QueryIR) -> None` renames every `LetBinding.name`, `LetRef.name` (and `LetValueRef.name` once WS4 adds it — keep a tuple `_LET_NAME_MODELS` to extend) to `f"$let{i}"` in declaration order on the deep copy. Order inside `compute_semantic_hash`: deep copy → `merge_consecutive_filters` → `normalize_expressions` (rebind) → `_clear_volatile` → `_canonicalize_let_names` → `_sort_commutative` → dump.

- [ ] **Step 1: Failing tests:** `T | where a == 1 and b == 2` == `… b == 2 and a == 1`; `x in ("a","b")` == `x in ("b","a")`; `a < b` ≠ `b < a` (not sorted); `let X = T | where a == 1; X | take 1` == `let Y = …; Y | take 1`; `let n = 5; T | where a > n` == `let m = 5; T | where a > m` (this last one passes only after WS4 Task 4.8 — mark it `xfail(strict=True, reason="K22 LetValueRef lands in WS4")` and remove the marker there).
- [ ] **Step 2:** Run — FAIL. **Step 3:** implement (also update `canonical()` so `And`/`Or` sort by the same key — today it sorts by rendered string, which agrees in practice; keep). **Step 4:** PASS; `compute_semantic_hash` docstring lists the new equivalences. CHANGELOG Breaking: "`semantic_hash` now sorts `and`/`or` operands and `in (...)` values, and is invariant under renaming `let` bindings." Commit: `feat(ir)!: sort commutative operands and canonicalize let names in semantic_hash`

### Task 2.5: Minimal-pair collision battery

**Files:** Create `tests/ir/test_hash_battery.py` (seed from `scratchpad/agentD/battery.py`).

- [ ] **Step 1:** Two parametrized lists: `MUST_DIFFER` (≥40 pairs: every string/comparison operator variant, `in`/`!in`/`in~`, `between`/`!between`, literal values, `x-2`/`2-x`, `a<b`/`b>a`, `(a+b)*c`/`a+b*c`, `project a,b`/`b,a`, `summarize by a,b`/`b,a`, `isnotnull`/`isnotempty`, `tolower(X)=="Y"` vs `X=~"Y"`, assert-schema columns, datetime values, `case()` branch order) and `MUST_EQUAL` (≥15: whitespace, comments, quote style, parens, consecutive filters, and/or flattening/order, double negation incl. root, `tolower=="y"`/`=~"y"`, `1h`/`60m`, naive/Z datetime, `@"a\b"`/`"a\\b"`, let alias rename, TZ). WS4 appends its new discriminators (sort direction, fork, datatable, union/mv-expand/parse/search/make-series params, join default, database qualifier).
- [ ] **Step 2:** PASS after 2.1–2.4. Commit: `test(ir): add a semantic_hash minimal-pair collision battery`

---

## WS3 — Tier 1 correctness

### Task 3.1: Trivia-free names everywhere (K15)

**Files:** `src/kustology/utils/walker.py` (new helper), `src/kustology/utils/analysis.py` (8 sites), create `tests/test_tier1_comments.py`.

**Interfaces — Produces:** `kustology.utils.walker.node_text(node) -> str` = `node.ToString(IncludeTrivia.Minimal)` (import `from Kusto.Language.Syntax import IncludeTrivia`); `node_name(node) -> str` = `node.SimpleName` for `NameReference`/`BracketedName`/`TokenName`/`WildcardedName` nodes (unquoted `my-table` for `['my-table']`), else `node_text(node)`.

- [ ] **Step 1: Failing tests** — one parametrized test per analyzer over pairs `(plain, commented)`:
```python
CASES = [
 ("SecurityEvent | where EventID == 4625 | project Account", "// lead\nSecurityEvent\n| where\n  // only failed\n  EventID == 4625\n| project\n// c\nAccount"),
 ("T | join (U) on a | union V", "T | join (\n// rhs\nU) on a | union\n // first\n V"),
 ("T | where t > ago(1d) and x == 1h", "T | where t > // c\nago(1d) and x == // c2\n1h"),
]
@pytest.mark.parametrize("plain,commented", CASES)
def test_analyzers_ignore_comments(plain, commented):
    a, b = parse(plain), parse(commented)
    assert a.get_referenced_tables() == b.get_referenced_tables()
    assert a.get_referenced_columns() == b.get_referenced_columns()
    assert a.get_referenced_functions() == b.get_referenced_functions()
    assert [t for t, *_ in a.find_time_expressions()] == [t for t, *_ in b.find_time_expressions()]
def test_replace_table_after_leading_comment():
    q = "// a comment\nSecurityEvent | take 1"
    assert parse(q).replace_table("SecurityEvent", "X") == "// a comment\nX | take 1"
def test_fixture_tables_are_identifiers():
    for f in Path("tests/fixtures/complex_queries").glob("*.kql"):
        for t in parse(f.read_text()).get_referenced_tables():
            assert "\n" not in t and "//" not in t, (f.name, t)
```
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Add the helpers to walker.py (export via `utils/__init__` and `analysis.__all__`); replace `node.ToString().strip()` / `ref.ToString().strip()` / `callee.ToString().strip()` / `name_node.ToString().strip()` at analysis.py lines 140, 173, 272, 284, 319 (function callee), 370 (time callee), 375, 390 (time text → `node_text(node)`), and in `_collect_table_refs`/`get_referenced_functions` with `node_name(...)`. Keep `TextStart`/`Width` as offsets.
- [ ] **Step 4:** PASS; run `tests/test_table_extraction.py` etc. Commit: `fix: read syntactic names without leading trivia (comments no longer corrupt Tier 1 analyzers)`

### Task 3.2: Let-shadowing, parameters, aliases, wildcards, bracket names (K16, K29-tables)

**Files:** `src/kustology/utils/analysis.py::_collect_table_refs`, tests `tests/test_table_extraction.py`.

- [ ] **Step 1: Failing tests:** `let SecurityEvent = SecurityEvent | where a; SecurityEvent | take 1` → `{'SecurityEvent'}` and `replace_table` rewrites the RHS only; `let f = (T1:(a:long)){ T1 | count }; T | invoke f()` → `{'T'}`; `T | as X | join (X) on a` → `{'T'}`; `union withsource=S T*` → `set()` (wildcard excluded; documented) ; `['my-table'] | take 1` → `{'my-table'}` and `replace_table("my-table","Z")` works unbound.
- [ ] **Step 2:** FAIL. **Step 3:** In the walker: collect `LetStatement` RHS refs into a separate `rhs_refs` list that bypasses the `let_vars` filter; collect `FunctionParameter` → `NameAndType.Name` names and `AsOperator.Name` into an `exclude` set; skip refs whose `node.Name.Kind == "WildcardedName"`; emit names via `node_name`. **Step 4:** PASS; docstring on `KustoQuery.get_referenced_tables`: "let aliases, `as` aliases, function parameters and wildcard patterns are not tables and are excluded; the binding's own right-hand side is included." Commit: `fix: table extraction excludes parameters/aliases/wildcards and keeps a shadowing let's RHS`

### Task 3.3: Bound parses keep unresolved tables (K17)

**Files:** `analysis.py::find_table_references`, tests.

- [ ] **Step 1: Failing test:** with `schema={"SecurityEvent": {...}}`, `union SecurityEvent, SigninLogs` → `{'SecurityEvent','SigninLogs'}` and `replace_table("SigninLogs","X")` rewrites.
- [ ] **Step 2:** FAIL. **Step 3:** In semantic mode return semantic refs **plus** syntactic refs whose `(TextStart, Width)` is not covered by a semantic ref and whose `node.ReferencedSymbol is None`. **Step 4:** PASS; document on both methods. CHANGELOG Fixed. Commit: `fix: bound parses no longer drop tables the schema does not know`

### Task 3.4: Structural hash token handling (K18)

- [ ] **Step 1: Failing test:** `join kind=inner` ≠ `kind=leftanti`; `union kind=inner` ≠ `outer`; `evaluate bag_unpack(d)` ≠ `pivot(d)`; still equal under whitespace/literal change.
- [ ] **Step 2:** FAIL. **Step 3:** `_TOKEN_KINDS = frozenset(k for k in syntax_kinds() if k.endswith("Token"))` computed once; `if kind in _TOKEN_KINDS: return`; for `TokenLiteralExpression` append `f"{kind}:{node_text(node)}"`. Rewrite the docstring: blind to literals, identifiers, whitespace, comments; sensitive to every named-parameter value. **Step 4:** PASS. Commit: `fix: structural hash keeps named-parameter values (join kind, plugin names)`

### Task 3.5: Columns and reflection (K29-columns, K30)

**Files:** `analysis.py::get_referenced_columns`, `analysis.py::find_time_expressions` (`_TIME_FUNCS`), `src/kustology/reflection.py`, tests `tests/test_advanced_utils.py`, `tests/test_reflection.py`.

- [ ] **Step 1: Failing tests:** `T | where T2 > 1 | join (T2) on a` syntactic columns ⊇ `{'T2','a'}`; `AuditLogs | extend actor = tostring(InitiatedBy.user.userPrincipalName)` syntactic columns == `{'InitiatedBy','actor'}`; `"bin" in time_functions()`, `"bin_at" in time_functions()`; `"tostring" in all_function_names()`; `aggregate_functions() & scalar_functions() == set()`; `"bag_unpack" in plugin_functions()` and `in all_function_names()`; `find_time_expressions("T | summarize count() by bin(TimeGenerated, 1h)")` returns the `bin(...)` call.
- [ ] **Step 2:** FAIL. **Step 3:** columns: exclude only names whose `(TextStart,Width)` matches a table-source ref; skip a `NameReference` that is the `Selector` of a `PathExpression` whose expression is not `$left`/`$right`. reflection: `_safe_return_type_name` scans every signature and returns `datetime`/`timespan` if any declares it; `_enumerate_static_symbols` enumerates `container.All` (the `IReadOnlyList[FunctionSymbol]`) instead of `dir()`; add `plugin_functions()` from `Kusto.Language.PlugIns.All`; `scalar_set -= agg_set`; `all_set |= plugins`; export `plugin_functions` in `reflection.__all__` and `kustology.__all__`. `find_time_expressions`: `_TIME_FUNCS = time_functions() | {"bin","bin_at","format_datetime","datetime_part","datetime_diff","datetime_add","startofday",...}` — define `_TEMPORAL_RELEVANT` explicitly in analysis.py and union it. **Step 4:** PASS. Commit: `fix: column extraction by position; reflection scans all signatures, lists plugins, dedupes aggregates`

### Task 3.6: CLI contract and library hardening (K31)

**Files:** `src/kustology/cli.py`, `src/kustology/utils/walker.py` (`node_to_dict` depth cap, `KustoWalker.visit` depth cap), `src/kustology/services.py` (`SchemaLike = dict | None`, docstrings), `src/kustology/core.py` (`to_dict` uses capped walker), tests `tests/test_cli_inprocess.py`, `tests/test_cli.py`.

- [ ] **Step 1: Failing tests:** missing file → 2; bad `--schema` JSON → 2; `parse -` on `T | where` → 1; `format -` on `T | where` → 1 and stdout empty; `parse --ir --json` output has keys `{"ir_schema_version","semantic_hash_scheme","ir"}`; `parse --ir --schema s.json` yields `schema_attached: true`; 1200 nested parens → JSON contains `"truncated": true` (no RecursionError) for both CLI and `KustoQuery.to_dict()`; `KUSTOLOGY_MAX_INPUT_BYTES=22` rejects a 20-char/28-byte payload; `parse("T", schema="(a:string)")` raises `TypeError` with a message naming the dict form (already) and `SchemaLike` no longer includes `str`.
- [ ] **Step 2:** FAIL. **Step 3:** `main()`: `except (OSError, json.JSONDecodeError) as e: … return 2`; `_cmd_parse`/`_cmd_format`: `diags = validate(body)`; if any Error → print them to stderr and return 1 (format: do not write output); `_cmd_parse --ir`: `from .services import parse; ir = parse(body, schema=_load_schema(args.schema)).to_ir()`; JSON = `{"ir_schema_version": IR_SCHEMA_VERSION, "semantic_hash_scheme": SEMANTIC_HASH_SCHEME, "ir": json.loads(ir.model_dump_json())}`; add `--schema` to the `parse` subparser; delete `_ast_to_dict`/`_ast_to_text` duplicates in favour of `walker.node_to_dict(node, max_depth=300)` + a text renderer over that dict; `walker.py`: `MAX_AST_DEPTH = 300`, `node_to_dict(node, depth=0)` emits `{"kind","text","children":[],"truncated":True}` past the cap, `KustoWalker.visit` stops descending past the cap; `_read_capped` reads `stream.buffer` bytes and decodes; services: narrow `SchemaLike`, remove the "or a Kusto schema string" sentence from both docstrings. **Step 4:** PASS; README CLI section updated in WS6. Commit: `fix(cli): honour the documented exit codes; cap AST depth in the library; tag IR JSON; add parse --schema`

### Task 3.7: Tier 1 polish (K41)

**Files:** `core.py`, `analysis.py`, `__init__.py`, `utils/schema_state.py`, tests.

- [ ] **Step 1: Failing tests:** `KustoQuery.diagnostics` returns the same dict shape as `validate()` without re-parsing (bound and unbound); `replace_table("T", "")` raises `ValueError`; `replace_table("T", "my-new-table")` emits `['my-new-table']`; `find_time_expressions("T | where Time > startofday(now())")` returns only the outer call; `get_operator_chain()` for `T | where a | take 1` contains no `NameReference` and `__repr__` says `2 ops`; `"PackageNotFoundError" not in dir(kustology)`; the `RuntimeWarning` for an unknown scalar type is attributed to the caller's file (use `pytest.warns` + `record[0].filename`).
- [ ] **Step 2:** FAIL. **Step 3:** implement each: `diagnostics` property built from `self._code.GetDiagnostics()` via a shared `services._diagnostic_dicts(diags, ignore_unknown_tables)` helper (refactor `validate` to use it); `replace_table` validates and bracket-quotes when `not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", new_name)`; `FnPass` containment check; `get_operator_chain` drops the source node (docstring: "operator nodes only; main pipeline only — use `get_operator_stats` for the whole AST"); `from importlib.metadata import PackageNotFoundError as _PackageNotFoundError`; `stacklevel=5` in `_resolve_scalar_type`. Add one-line delegating docstrings to `get_operator_chain`, `to_dict`, `to_json`, `get_referenced_columns`, `get_referenced_functions`, `get_structural_hash`, `replace_table`, `get_referenced_tables` (let/alias exclusion). **Step 4:** PASS. CHANGELOG Changed: operator chain excludes the source node; Added: `KustoQuery.diagnostics`, `plugin_functions()`. Commit: `feat: KustoQuery.diagnostics; replace_table validation; operator chain is operators only`

---

## WS4 — IR model completeness (one branch, one CHANGELOG Breaking block)

All tasks here add fields; after the last one run `python scripts/audit_syntax_kinds.py --update-baseline`, extend `tests/ir/test_hash_battery.py` with the new discriminators, and write the CHANGELOG `### Breaking` entries together (Task 4.13).

### Task 4.1: Sort keys (K05-sort)

**Files:** `query.py`, `builder.py` (SortOperator/TopOperator branches; delete the `OrderedExpression` unwrap in `_visit_expr`), `_normalize.py` (no change — SortKey is not an Expr), `llm_view.py` (no change), tests.

**Interfaces — Produces:**
```python
class SortKey(BaseModel):
    model_config = {"extra": "forbid"}; KIND = "sort_key"; kind: Literal["sort_key"] = "sort_key"
    expression: AnyExpr
    direction: Literal["asc", "desc"]          # REQUIRED: KQL's effective default is desc (D8); required so llm_view renders it
    nulls: Literal["first", "last"] | None = None
    span: Span
SortOp.expressions: list[SortKey]          # was list[AnyExpr]
TopOp.by: SortKey                          # was AnyExpr
```
Builder helper `_visit_sort_key(node) -> SortKey`: if `type(node).__name__ == "OrderedExpression"`: `expression=self._visit_expr(node.Expression)`; `oc = node.Ordering`; `direction = oc.AscOrDescKeyword.Text.lower()` when `oc is not None and oc.AscOrDescKeyword is not None` else `"desc"`; `nulls = oc.NullsClause.FirstOrLastKeyword.Text.lower()` when present; else (a bare `NameReference` — verified: `sort by x` is not wrapped in `OrderedExpression`) `SortKey(expression=self._visit_expr(node), direction="desc")`.

- [ ] **Step 1: Failing tests:** `sort by x desc nulls first, y asc` → `[("x","desc","first"),("y","asc",None)]`; `top 5 by x desc` → `by.direction == "desc"`; `sort by x` → `direction == "desc"` and hash == `sort by x desc` and ≠ `sort by x asc`; hashes differ for nulls first/default; `order by x` == `sort by x`.
- [ ] **Step 2:** FAIL. **Step 3:** implement; remove the `OrderedExpression` entry from `HANDLED_EXPR_KINDS` if the expr-level unwrap is deleted (it is still handled — by the operator — keep it listed with a comment like `MaterializeExpression`'s). Binder: `SchemaAttacher._fill_children` already descends `SortKey` (BaseModel) → no change. **Step 4:** PASS. Commit: `feat(ir)!: model sort direction and null ordering`

### Task 4.2: Fork branches (K05-fork)

**Interfaces — Produces:** `class ForkBranch(BaseModel): name: str | None = None; pipeline: Pipeline; span: Span` ; `ForkOp.branches: list[ForkBranch]` (replaces `pipelines`).

- [ ] **Step 1: Failing tests:** `T | fork a=(where x==1 | count) (take 1)` → two branches, `branches[0].name == "a"`, `find_all(ir, FilterOp)` non-empty, `find_all(ir, TakeOp)` non-empty; two different forks hash differently.
- [ ] **Step 2:** FAIL. **Step 3:** builder `ForkOperator`: for each `el in _iter_elements(n.Expressions)`: `name = visit_name(el.NameEquals.Name) if el.NameEquals is not None else None`, `pipeline = self._visit_pipeline(el.Expression)`; `_visit_pipeline.walk` gets a `ForkExpression` case that walks `n.Expression` (defensive). Binder `_fill_children` handles it generically (ForkBranch is a BaseModel holding a Pipeline → `_walk_pipeline(child, inherited)`). **Step 4:** PASS. Commit: `fix(ir)!: build fork branches (they were empty) and record branch names`

### Task 4.3: Sources — datatable, externaldata, database/cluster, wildcards (K05-sources, K25)

**Interfaces — Produces:**
```python
class DataTableSource(BaseModel):    KIND="datatable_source";     columns: list[tuple[str, str]]; rows: list[list[AnyExpr]]; span: Span
class ExternalDataSource(BaseModel): KIND="external_data_source"; columns: list[tuple[str, str]]; uris: list[str]; format: str | None = None; span: Span
class TableRef(BaseModel): name: str; database: str | None = None; cluster: str | None = None; is_wildcard: bool = False; span: Span
Pipeline.source: Annotated[Union[TableRef, LetRef, FuncCallSource, DataTableSource, ExternalDataSource, ImplicitSource, UnknownSource, "Pipeline"], Field(union_mode="left_to_right")]
   # ORDERING RULE now applies to sources too: the new classes sit after FuncCallSource and before the fields-less ImplicitSource
ExternalDataExpr.uris: list[str]   # replaces uri (expression position, e.g. inside a where); shares _read_external_data(node) with the source
UnknownSource.raw_text: the real source text (IncludeTrivia.Minimal)
_TABULAR_LET_RHS_KINDS += {"ExternalDataExpression"}   # D12: let X = externaldata(...) -> rhs_pipeline
```
- [ ] **Step 1: Failing tests:** `datatable(a:int, b:string)[1,"x",2,"y"] | take 1` → `DataTableSource(columns=[("a","int"),("b","string")], rows=[[1,"x"],[2,"y"]])`; `externaldata(a:string)[h"https://x"] with (format="csv") | where a == "x"` → `ExternalDataSource(uris=["https://x"], format="csv")`; `let X = externaldata(a:string)["https://x","https://y"]; X | take 1` → `rhs_pipeline.source` is `ExternalDataSource` with both URIs and `inner_tables == []`; `database('d').T` → `TableRef(name="T", database="d")`; `cluster('c').database('d').T` → `cluster="c"`; `union T*` → `TableRef(name="T*", is_wildcard=True)`; `union database('d').*` → `TableRef(name="*", database="d", is_wildcard=True)`; hashes differ between `database('d1').T` and `('d2')`, and between two datatables; round-trip of every `Pipeline.source` member through `model_dump`/`model_validate` returns the same class (new `tests/ir/test_union_ordering.py`, also covering every `Operator` subclass).
- [ ] **Step 2:** FAIL. **Step 3:** builder `_visit_pipeline.walk`: `DataTableExpression` → columns from `n.Schema.Columns` (`visit_name(col)`, `col.Type.ToString().strip()`), flat `_iter_elements(n.Values)` reshaped into rows of `len(columns)`; `ExternalDataExpression` → `ExternalDataSource(*_read_external_data(n))`; `PathExpression` → `extract_qualified_table_ref(node) -> (cluster, database, name, is_wildcard)` reading the `FunctionCallExpression` string-literal arg of `database(...)`/`cluster(...)` via `LiteralValue`; `NameReference` with `n.Name.Kind == WildcardedName` → `is_wildcard=True`; `UnknownSource(raw_text=node.ToString(IncludeTrivia.Minimal))`. `_read_external_data`: URI text from `el.LiteralValue` (the DLL decodes `h"…"`), falling back to `ToString().strip().strip("@\"'")`. Binder `_source_entry`: `DataTableSource`/`ExternalDataSource` → `ScopeEntry(table=None, columns=dict(columns))`; `is_wildcard` → empty entry; lookups stay keyed on bare `name` (document). `canonical()`: `ExternalDataExpr` renders `externaldata(cols)[uri1, uri2]`. `to_llm_dict`: cap `DataTableSource.rows` at 20 and add `"rows_omitted": n` (IOC datatables run to thousands of rows; `model_dump_json` stays complete). Delete the `externaldata` exclusion paragraph from `_is_tabular_let_rhs`'s docstring. Tier 1 (`_unwrap_table_expr`) unchanged. **Step 4:** PASS. Commit: `feat(ir)!: datatable/externaldata sources, database/cluster qualifiers, wildcard flag`

### Task 4.4: mv-expand, parse, union, search, find, make-series, render, join/lookup defaults, hints (K05-params, K39)

**Interfaces — Produces:**
```python
class Operator(BaseModel): ...; hints: dict[str, str] = {}          # hint.* named params; volatile (excluded from the hash by field)
class MvExpandColumn(BaseModel): KIND="mv_expand_column"; expression: AnyExpr; to_typeof: str | None = None; span: Span
MvExpandOp.columns: list[MvExpandColumn]   # element type changes; field name stays
MvExpandOp: row_limit: int | AnyExpr | None = None; with_item_index: str | None = None; bag_expansion: str | None = None; expand_kind: str | None = None
class TypedNameDecl(Expr): KIND="typed_name"; name: str; declared_type: str     # NameAndTypeDeclaration in parse/parse-where patterns, scan declare, find project (D13)
ParseOp/ParseWhereOp: parse_kind: str           # REQUIRED, effective default "simple" (D8); flags: str | None = None
UnionOp: union_kind: str (REQUIRED, effective default "outer"); is_fuzzy: bool = False; withsource: str | None = None
SearchOp: search_kind: str | None = None; tables: list[TableRef] = []      # TableRef (or LetRef when the name is let-bound)
FindOp: tables: list[TableRef] (was list[str]); withsource: str | None = None; project: list[AnyExpr] = []; project_away: list[AnyExpr] = []
class MakeSeriesAggregate(BaseModel): KIND="make_series_aggregate"; name: str; expr: AnyExpr; default: AnyExpr | None = None; span: Span
MakeSeriesOp.aggregations: list[MakeSeriesAggregate]   # same .name/.expr attribute names as Assignment so the binder's reads are unchanged; range_from/to/step populated from MakeSeriesInRangeClause.Arguments
RenderOp: properties: dict[str, str] = {}
JoinOp.join_kind: str (REQUIRED, effective default "innerunique"); LookupOp.lookup_kind: str (REQUIRED, effective default "leftouter")   # D8
```
Builder reads (verified members): `extract_named_param(n, "kind", default=...)` (widen its `default` to `str | None`), `"withsource"`, `"isfuzzy"` (→ bool), `"flags"`, `"with_itemindex"`, `"bagexpansion"`; `MvExpandOperator.RowLimitClause.RowLimit` (int literal or expression → `_visit_count`), `MvExpandExpression.ToTypeOf` (render the type node: `ToTypeOf.TypeOf.ToString().strip()` — confirm the inner member with `dir()` at implementation time; fall back to the clause text after `typeof(`), `SearchOperator.InClause.Expressions`, `FindOperator.InClause/Project/ProjectAway` (`FindProjectClause.Columns`), `MakeSeriesExpression.DefaultExpression.Expression`, `MakeSeriesOperator.RangeClause` being `MakeSeriesInRangeClause` (`Arguments.Expressions` → from, to, step), `RenderOperator.WithClause.Properties` (`prop.Name`, `prop.Expression`). A shared `extract_hints(node) -> dict[str,str]` in `_builder_helpers.py` collects `Parameters` whose name starts with `hint.`; call it from every operator branch that has `Parameters`. `_VOLATILE_FIELDS` gains `"hints"`. `NameAndTypeDeclaration` in `_visit_expr` → `TypedNameDecl(name=visit_name(node.Name), declared_type=node.Type.ToString().strip())`; add it to `AnyExpr`, `__all__`, and a `canonical()` branch rendering `name:type`.

- [ ] **Step 1: Failing tests:** one assertion per field above on a real parse, plus hash pairs: `mv-expand x` ≠ `to typeof(string)` ≠ `limit 10` ≠ `with_itemindex=i`; `parse kind=regex` ≠ `simple` and bare `parse` == `kind=simple`; `parse … b:long` ≠ `b` (and `patterns[1]` is `TypedNameDecl(name="b", declared_type="long")`); `union kind=inner` ≠ `outer` and bare == `kind=outer`; `withsource=S` ≠ none; `search kind=case_sensitive` ≠ default; `search in (A)` ≠ `in (B)` and `find_all(ir, TableRef)` finds `A`; `find … project a` ≠ without; `make-series n=count() default=0 …` ≠ `default=1`, `in range(d1,d2,1h)` populates from/to/step; `render timechart with (title="a")` ≠ without; `join U on k` == `join kind=innerunique U on k` and ≠ `join kind=inner U on k`; `lookup U on k` == `kind=leftouter`; `join hint.strategy=shuffle` == without (hint excluded from the hash) and `op.hints == {"hint.strategy": "shuffle"}`.
- [ ] **Step 2:** FAIL. **Step 3:** implement; `llm_view` drops empty dicts/None automatically; required fields render. Binder (WS5) will consume `to_typeof`, `with_item_index`, `withsource`, `union_kind`, `TypedNameDecl.declared_type`. **Step 4:** PASS. Commit: `feat(ir)!: model operator parameters (mv-expand, parse, union, search, find, make-series, render), typed captures, effective defaults, hints`

### Task 4.5: Multi-statement queries (K05-last)

**Interfaces — Produces:** `QueryIR.additional_pipelines: list[Pipeline] = []` (2nd.. tabular statements, in order).

- [ ] **Step 1: Failing test:** `T | count; U | count` → `main_pipeline.source.name == "T"`, `additional_pipelines[0].source.name == "U"`, hash ≠ `T | count`. **Step 3:** in `build_from_code` visit `expr_stmts[1:]`; **add `"additional_pipelines"` to the `compute_semantic_hash` payload dict** (`transforms.py:220-223` hardcodes `{let_bindings, main_pipeline}` — without this the field is invisible to the hash and the test's `≠` fails). Commit: `feat(ir)!: keep every tabular statement of a multi-statement query`

### Task 4.6: Scalar let references (K22)

**Interfaces — Produces:** `class LetValueRef(Expr): KIND="let_value_ref"; name: str` in `expr.py` + `AnyExpr` + `ir/__init__.__all__`; `canonical()` renders `name`; `transforms._LET_NAME_MODELS = (LetBinding, LetRef, LetValueRef)`.

- [ ] **Step 1: Failing tests:** `let threshold = 5; T | where Count > threshold` → right operand is `LetValueRef(name="threshold")`, `find_all(ir, ColumnRef)` names == `{"Count"}` (bound and unbound); `let list = dynamic([1]); T | where a in (list)` → `SetMembership.values[0]` is `LetValueRef`; remove the `xfail` from Task 2.4's let-rename test and assert equality. **Step 3:** `_visit_expr` `NameReference` branch: `if name in self._let_names: res = LetValueRef(name=name, span=span)`; binder `_fill`: `LetValueRef` is not a ColumnRef → only `map_semantic_info` types it. Tier 1 unaffected. Commit: `feat(ir)!: distinguish let-bound scalars from columns (LetValueRef)`

### Task 4.7: Typed nested pipelines (K24)

- [ ] **Step 1: Failing test:** round-trip `QueryIR.model_validate_json(ir.model_dump_json()) == ir` and equal `walk` counts for `toscalar(...)` and `in ((subquery))` queries (both modes). **Step 3:** `ToScalarExpr.pipeline: "Pipeline | None"`, `SubqueryExpr.pipeline: "Pipeline | None"`; add the two classes to the `model_rebuild()` calls at the bottom of `query.py` (after `Pipeline` is defined) — the forward ref resolves from `query.py`'s namespace; if pydantic complains about the cycle, import `Pipeline` under `TYPE_CHECKING` in `expr.py` and call `ToScalarExpr.model_rebuild(_types_namespace={"Pipeline": Pipeline})` in `query.py`. Commit: `fix(ir): type nested pipelines so JSON round-trips keep the subtree`

### Task 4.8: Small fidelity gaps (K25 rest, K26, K40)

- [ ] **Step 1: Failing tests:** `T | where x == 'a' 'b'` → `LiteralExpr(value="ab")`; `T | project-reorder *, a` → first column is `StarExpr`; `find_all(ir, FuncCall)` names for `let A = T | where d > ago(1h) | where d < now(); A | take 1` == `['ago','now']` (no duplicates); `BinOp` for `x + 1` has `case_sensitive is None and polarity is None`; `isnull(a)`/`isempty(a)` → `Exists(op="isnull"/"isempty", polarity="exclusion")`, `isnotnull` → `polarity="inclusion"`, all four hash distinctly.
- [ ] **Step 3:** builder: dispatch `CompoundStringLiteralExpression` → `LiteralExpr(value=str(node.LiteralValue), literal_kind="string")` and add it to `HANDLED_EXPR_KINDS`; `NameReference` with `node.Name.Kind == WildcardedName and visit_name(...) == "*"` → `StarExpr`; `walk()` keeps a `seen: set[int]` of `id(node)`; `BinOp.case_sensitive: bool | None`, `polarity: Literal[...] | None` — `None` unless `op` is a string operator or a comparison (`_is_case_sensitive_op` returns `None` for arithmetic: `+ - * / %`), `polarity` `None` for arithmetic; **K23:** `_is_case_sensitive_op(":")` returns `False` (`search Col:'x'` ≡ `Col has 'x'`) — add `":"` as an explicit case before the stem check, and a test `search Col:'x'` → `BinOp(op=":", case_sensitive=False)`; `Exists.polarity: Literal["inclusion","exclusion"]` and lower `isnull`/`isempty` too; `_normalize.canonical` for `Exists` unchanged (renders `op`); `llm_view._collapse_polarity_into_op` handles `None`. Commit: `fix(ir): compound strings, star in project-reorder, walk dedupe, meaningful case/polarity, symmetric Exists, search ':' folds case`

### Task 4.8b: `canonical_form` fidelity — precedence, escaping, bool/null (K34 items 1–2)

**Files:** `src/kustology/ir/_normalize.py::canonical`, tests `tests/ir/test_ir_builder.py`.

- [ ] **Step 1: Failing tests:** `a and (b or c)` → `"a and (b or c)"`, `(a and b) or c` → `"(a and b) or c"` (distinct strings); `(x + y) * z` → `"(x + y) * z"`, `x + y * z` unchanged; `x - (y - z)` → `"x - (y - z)"`; `not(a and b)` → `"not(a and b)"`; `f("a\", \"b")` → `'f("a\\", \\"b")'` (escaped, one argument); `x == true` → `"x == true"`; `x == real(null)` → `"x == null"`; `to_llm_dict` of `T | where e == true` has **no** `canonical_form` on the bool literal (redundant-form drop now fires).
- [ ] **Step 2:** FAIL. **Step 3:** Give `canonical()` a precedence table `_PREC = {"or": 1, "and": 2, comparison ops: 3, "+": 4, "-": 4, "*": 5, "/": 5, "%": 5, unary: 6}`; an inner helper `_render(expr, parent_prec, is_right_operand)` wraps the child in parentheses when its precedence is lower than the parent's, or equal on the right side of a non-associative operator (`-`, `/`, `%`); `And`/`Or` sort their operands by rendered string then join; string literals render through `_kql_string(value)` which emits `"…"` with `\\`, `\"`, `\n`, `\t` escaped; `bool` → `true`/`false`, `None` → `null`. Keep `BracketedExpr` dropped (precedence now carries the meaning). `llm_view._canonical_literal_repr` already produces `true`/`false`/`null` — they now match. **Step 4:** PASS; battery unchanged (hash is structural). Commit: `fix(ir): canonical_form parenthesizes by precedence, escapes strings, renders KQL bool/null`

### Task 4.9: LLM view and docstrings touched by the model change

- [ ] `to_llm_dict(QueryIR)` adds `"ir_schema_version": IR_SCHEMA_VERSION` at top level; fix the module docstring bullets (`result_type=unresolved`; delete the stale "renamed to render_kind/join_kind/lookup_kind in the LLM output" bullet — the model fields already carry those names). Test: `to_llm_dict(ir)["ir_schema_version"] == "0.2"`. Commit: `feat(ir): tag the LLM view with the IR schema version`

### Task 4.10: Baseline, battery, `__all__`, Breaking notes

- [ ] `python scripts/audit_syntax_kinds.py --update-baseline`; add every new class (`SortKey`, `ForkBranch`, `DataTableSource`, `ExternalDataSource`, `MvExpandColumn`, `MakeSeriesAggregate`, `TypedNameDecl`, `LetValueRef`) to `ir/__init__.py` imports and `__all__` (and `TypedNameDecl`/`LetValueRef` to `AnyExpr`; `tests/ir/test_canonical_coverage.py` also asserts every `Expr` subclass is in both `AnyExpr` and `__all__`); extend `tests/ir/test_hash_battery.py` with the WS4 discriminators; in `tests/ir/test_ir_roundtrip.py` add the missing `extra="forbid"` direction — a `SortOp` dump whose `expressions[0]` lacks `expression` (a 0.2-dev shape) fails `QueryIR.model_validate_json` with a `ValidationError` naming the field; write the CHANGELOG `### Breaking` bullets for every field added/renamed in WS4 (one bullet per model, listing old → new), and a Fixed bullet for each collision closed. Full gate run. Commit: `chore(ir): baseline, exports and release notes for the 0.2.0 IR shape`

---

## WS5 — Binder: binder-derived schemas + fallback fixes + schemaless typing

### Task 5.1: Schemaless `to_ir()` analyzes against default globals (K27)

**Files:** `src/kustology/core.py::to_ir`, `src/kustology/ir/builder.py::build/build_from_code`, `README.md:124-127` (WS6 finalizes), tests `tests/test_to_ir_seam.py`, `tests/ir/test_ir_builder.py`.

**Interfaces — Produces:** `IRBuilder.build_from_code(code, *, ignore_unknown_tables: bool = False)`; `IRBuilder.build(query)` passes `ignore_unknown_tables=True`; `KustoQuery.to_ir()` on an unbound parse calls `self._code.Analyze(GlobalState.Default)` (D5 — verified to exist; returns a **new** bound `KustoCode` without re-parsing, and leaves `self._code.HasSemantics` False so Tier 1 keeps its syntactic path) and builds with `ignore_unknown_tables=True`; the bound path is unchanged and keeps KS204. `tests/test_to_ir_seam.py`'s no-reparse invariant stays green.

- [ ] **Step 1: Failing tests:** `parse("T | where a > ago(1h) and b == 1.5").to_ir()` → literal `1h` `result_type == TIMESPAN`, `1.5` → `REAL`, `ago` → `DATETIME`, `diagnostics == []`; `IRBuilder().build(same)` → same types and `diagnostics == []`; `parse(q, schema=partial).to_ir().diagnostics` still contains KS204 for the unknown table; hash unchanged across the three paths; the seam test "no second parse on a bound KustoCode" still holds.
- [ ] **Step 3:** implement; `build_from_code` filters `code_val == "KS204"` when the flag is set. Commit: `feat(ir): schemaless IR carries literal and built-in types (default-globals analysis)`

### Task 5.2: Capture Microsoft's per-operator result schema at build (K-ARCH-1)

**Files:** `query.py` (`Operator.result_schema`), `builder.py` (`_attach_operator_schema`), `_builder_helpers.py`, `binder.py` (`_walk_pipeline` seeds from it), `transforms.py` (`result_schema` already volatile), tests.

**Interfaces — Produces:** `Operator.result_schema: TabularSchema | None = None` (binder-populated; volatile; dropped from the LLM view when None). `_builder_helpers.table_symbol_columns(sym) -> dict[str, str] | None` (`{c.Name: c.Type.Name for c in sym.Columns}` when `sym` is a `TableSymbol`). `Pipeline.result_schema` is set at build time to the last operator's schema (or the source node's `ResultType` columns when no operators) when the parse is bound; `SchemaAttacher._walk_pipeline` uses `op.result_schema` as the authoritative post-operator scope (names+types) and only runs the hand-rolled rule when it is `None`; provenance (`ColumnRef.table`) is still computed by the walk.

- [ ] **Step 1: Failing tests (oracle):** create `tests/ir/test_binder_oracle.py` (from `scratchpad/agentB/oracle.py`): parametrize over an operator matrix (join all kinds, project-keep/away wildcards, mv-expand variants, arg_max(t,*), make_set/take_any/percentile, typed parse, parse-kv, union conflict + withsource, print/range/datatable/externaldata/getschema/search/find/scan/serialize row_number) and every fixture with a heuristic schema; assert `ir.main_pipeline.result_schema.columns == {c.Name: c.Type.Name for c in code.ResultType.Columns}` (ordered). Mark nothing xfail — all should pass once 5.2 lands because the answer comes from Microsoft.
- [ ] **Step 2:** FAIL on every current divergence (K07–K14, K28). **Step 3:** implement; in `_visit_operator` wrap the dispatch: `op = <existing>; if op is not None: op.result_schema = _operator_schema(node)`; in `_visit_pipeline` set `pipeline.result_schema` from the last op or the source node; `SchemaAttacher._walk_operator`: at the top `if op.result_schema is not None: self._fill_children(op, scope, inherited=scope); self._set_scope(scope, dict(op.result_schema.columns)); return` — **except** keep provenance: for `JoinOp`/`LookupOp`/`UnionOp` still append per-side `ScopeEntry(table=...)` so `ColumnRef.table` resolves, then overlay names/types from `op.result_schema`. **Step 4:** PASS. Commit: `feat(ir): take bound result schemas from Microsoft's binder per operator`

### Task 5.3: Fallback scope-walk correctness (K07–K14, K28) — for `attach_schema={dict}` on unbound parses and for provenance

Each sub-item: failing test in `tests/ir/test_binder.py` (asserting **types**, via `SchemaAttacher({...}).enrich(parse(q).to_ir())` — the unbound path — and `ColumnRef.table`), then the fix:
- [ ] K07 join kinds: `leftanti/leftsemi` → scope unchanged; `rightanti/rightsemi` → scope = RHS entry; `None` → innerunique; normalize case.
- [ ] K08 wildcards: `fnmatch` in project-keep/away.
- [ ] K09 mv-expand: bare → `dynamic`; `column.to_typeof` → that type; `with_item_index` → add long column; drop the `result_type_inner` branch.
- [ ] K10 `$left`: resolve by name over `scope[:n]` snapshot taken before appending the RHS entry; `$right` → the appended entry.
- [ ] K11 bare `on k`: left scope first.
- [ ] K12 union: conflicting types → `Name_type` split (drop unsuffixed), `withsource` → prepend `{name: "string"}`.
- [ ] K13 `arg_max/arg_min(x, *)`: ordering col first then scope columns (or listed columns).
- [ ] K14 auto-names in `builder._auto_name`: `make_set→set_`, `make_list→list_`, `make_bag→bag_`, `take_any/any→<col>`, `percentile(x, p)→percentile_x_p`, `percentiles`, `countif→countif_`, `dcountif→dcountif_x`; prefer the bound node's `ResultType` column name when available (`SummarizeOperator.ResultType.Columns[i]` aligned to aggregate index).
- [ ] K28: project/distinct/keep/reorder fall back to `c.result_type.value` before `"unknown"`; typed parse captures (`TypedNameDecl`) use `declared_type` and untyped (`ColumnRef`) stay `string`; `parse-kv` applies `op.columns`; `print`/`range`/`datatable`/`externaldata`/`getschema` (`ColumnName:string, ColumnOrdinal:int, DataType:string, ColumnType:string`)/`search` (adds `$table:string`)/`count` derive their fixed shapes; leave `result_schema=None` when nothing is known (never stamp `{}`); no schema on fork branches whose pipeline has `UnknownSource` and no operators; `schema_attached = True` only when a schema dict or bound globals existed; provenance carried through `project`/`project-*`/`distinct` via a `{column: table}` map on the anonymous `ScopeEntry` (add `origins: dict[str, str | None]` to `ScopeEntry`); ambiguous unqualified columns (same name in ≥2 scope entries, none `$`-qualified) → `table=None`.
- [ ] Update the `SchemaAttacher` class docstring taxonomy (search adds `$table`; getschema/scan/find/datatable/externaldata/as listed; counts 18/35) and the `enrich` "Three boundaries" wording. Commit per sub-group: `fix(ir): …`

### Task 5.4: BinderEnricher, schema input validation, sentinels (K36)

- [ ] Remove `BinderEnricher` from `binder.py` and `ir/__init__.py` (CHANGELOG Breaking: "removed undocumented alias"); `schema_state._resolve_scalar_type`: lower-case the type name, `TypeError` for non-str, consistent `RuntimeWarning` for the string form, document that keys are raw names; `TabularSchema.columns` docstring names the `"unknown"` sentinel and points at `KustoType.UNRESOLVED`. Tests for each. Commit: `fix(ir): schema input validation; remove BinderEnricher alias`

---

## WS6 — Documentation and examples (against the final shape)

### Task 6.1: CHANGELOG `[0.2.0]`
- [ ] Rewrite/append: Fixed (K01–K04, K06–K18, K23–K26, K29–K31), Added (`SortKey`, `ForkBranch`, `DataTableSource`, `MvExpandItem`, `LetValueRef`, `TableRef.database/cluster/is_wildcard`, operator params, `hints`, `additional_pipelines`, `KustoQuery.diagnostics`, `plugin_functions`, `parse --schema`, IR JSON envelope, `Operator.result_schema`), Changed (schemaless typing, operator chain, CLI exit codes, format refuses invalid input, hash sorts/let-canonicalizes), Breaking (every field rename/type change; `uri→uris`; `FindOp.tables`; `ForkOp.pipelines→branches`; `MvExpandOp.columns→items`; `count` types; `BinderEnricher` removed; stored hashes and 0.2-dev JSON invalid), Internal (canary, dependabot, gates, oracle harness, TZ leg). Fix: re-measure the LLM-view size reduction after WS4 (the review measured 46%; the models grew) and state the measured figure; 0.1.0 "23 expression types"; CI matrix wording; add the link-reference footer; demote `verify_corpus.py` to "maintainer diagnostic".

### Task 6.2: README
- [ ] Replace the "Symbols require a schema" paragraph with the accurate two-tier statement (Tier 1 `parse()` is all-or-nothing; `ParseAndAnalyze(text, GlobalState.Default)` resolves built-ins/literals; `to_ir()` now uses it); tier table "lossless" → "round-trips"; CLI block adds `validate --schema/--ignore-unknown-tables`, `parse --json`, `parse --ir --schema`; exit-code sentence matches Task 3.6; "Versioning" note: hash sorts commutative operands, is let-rename invariant, timezone-independent; list the eight raw-text-only operators and the let-function-body boundary under Tier 2; absolute GitHub URLs for the five relative links; "which source table a column came from" paragraph mentions `LetValueRef`; a three-column mapping table for the operator vocabularies (`where` → `FilterOperator` (Tier 1 / `parse --ast`) → `FilterOp` / `kind: "filter"` (Tier 2)).

### Task 6.3: ARCHITECTURE / AGENTS / CONTRIBUTING / templates / reports / docstrings
- [ ] ARCHITECTURE: Tier 1 "on a stabilization track; pre-1.0 it may break at a minor (0.2.0 renamed `get_time_range`)"; scripts list adds `mine_corpus.py`, `extract_complex_corpus.py`; "A new tabular operator" step 4 mentions `Operator.result_schema` and `hints`; "A new IR expression" mentions `LetValueRef` as the scalar-name pattern. AGENTS: tag cadence paragraph → once per release; `canonical_form` sorting sentence → "And/Or/SetMembership only; the hash sorts the same"; bind-invariance paragraph unchanged; `MaterializeExpr` → "(since-removed)"; add "Datetime literals are UTC-normalized at build; never read `.Ticks` off a `DateTimeKind.Local` value" and "Direct attribute access raises — the audit now scans it". CONTRIBUTING: unchanged commands; mention the oracle harness. `.github/pull_request_template.md`: `ruff check src tests scripts examples`; bug template: the `python -c` VERSION.txt path. `docs/superpowers/reports/2026-08-20-stub-sweep.md`: add **FIXED**/**REMOVED** markers to every closed row (A1–A11, B1, C1–C9). Docstrings: `binder.py` inline comment 18/35; `enrich` three boundaries; `compute_semantic_hash` (add the bind-divergence note, the sorting/let-rename/UTC rules); `llm_view` (done in 4.9); `Exists`, `ScanOp`/`TopNestedOp`/`MakeGraphOp`/`MacroExpandOp`/`Graph*Op` class docstrings in the `LetFunction` register; `LiteralExpr` docstring records two deliberate hash collapses — typed nulls (`real(null)` ≡ `datetime(null)`, `literal_kind="null"`) and obfuscated strings (`h"x"` ≡ `"x"`, the marker is not a predicate difference); `get_referenced_tables` (done 3.2). Commit: `docs: align every document with the 0.2.0 shape`

### Task 6.4: Examples
- [ ] `walk_ir.py`: bound parse with a small `SCHEMA`, print `f"{c.name}:{c.result_type} <- {c.table}"`, add the `rhs_function` arm; `find_all_demo.py`: join `on $left.DeviceId == $right.DeviceId`, print `join_side` and `result_type`; `llm_view.py`: `let` + join + `ago(7d)` query, `parse(q, schema).to_ir()` one-liner, fix `result_type=unresolved`; `query_analysis.py`: compute the structural-hash invariance instead of asserting it; `walk_tree.py`: `kind.endswith("Token")` after the `_TRANSPARENT` check; `binding_comparison.py`: `len(cols)`; `linter.py`: three IR rules (unbounded time range via `find_time_expressions` == [], `contains` on a string literal where `has` would index, `project *`) + a semantic diagnostic via `validate(q, schema=...)`; new `examples/semantic_hash_demo.py` (dedup pairs, `SetMembership.op`, `ticks`/`literal_kind`) and `examples/analyzer_demo.py` (`Finding`/`Severity` over `find_all`); add both to `IR_EXAMPLES`. Run every example; `tests/test_examples.py` green. Commit: `docs(examples): demonstrate the 0.2.0 capabilities`

---

## WS7 — CI, infra, scripts (K38)

### Task 7.1: Workflows and packaging metadata
- [ ] `test.yml`: python matrix `["3.10","3.11","3.12","3.13"]`; the `--extra test` base job stays pydantic-free on one cell, every other cell installs `--extra ir`; `fail-fast: false`; add `TZ: Asia/Tokyo` to the locale job matrix as a third cell (`{locale: "en_US.UTF-8", tz: "Asia/Tokyo"}`, export `TZ`); `dependency-review` job `permissions: pull-requests: write` (or drop the comment option); harden-runner stays `audit` (document why: egress allowlist unknown). `pyproject.toml`: classifiers add 3.13; `requires-python = ">=3.10,<3.14"`. `.pre-commit-config.yaml`: `pydantic==2.13.4` (match `uv.lock`). `.gitignore`: `/.claude/settings.local.json`. `canary.yml` matrix adds 3.13. Commit: `ci: py3.13, IR tests on every cell, TZ leg, fail-fast off, dependency-review perms`

### Task 7.2: Scripts
- [ ] `verify_dll.py`: typed exceptions, exit 2 for network/config, 1 only for hash mismatch, `--offline` flag, pin TFM `lib/net6.0/`; `refresh_dll.py`: write to temp + `os.replace` for both pins, same TFM pin; `verify_corpus.py`: exit 1 on empty corpus / non-empty findings, `--soft`, `errors="replace"`; `extract_sentinel_schemas.py`: write output only on success; `sample_sentinel_corpus.py`: non-zero on a non-repo dir; tests in `tests/test_scripts.py` for the exit codes (use a fake URL / missing dir). Commit: `chore(scripts): honest exit codes, offline DLL verification, atomic pin writes`

---

## WS8 — Release close-out

- [ ] Full gate run (see Global Constraints) plus `LANG=de_DE.UTF-8 LC_ALL=de_DE.UTF-8 .venv/bin/python -m pytest -q` and `TZ=Asia/Tokyo .venv/bin/python -m pytest -q tests/ir`.
- [ ] `.venv/bin/python -m build --outdir /tmp/dist && .venv/bin/twine check /tmp/dist/*`; fresh-venv install of wheel (with and without `[ir]`) and sdist from outside the repo; `kustology version`.
- [ ] Re-run the review's empirical harness (`scratchpad/agentG/harness.py`) — expect 0 exceptions, 0 round-trip failures, 0 literals UNRESOLVED under (a), oracle divergences 0.
- [ ] CHANGELOG date = release day; `pyproject` 0.2.0; `uv lock --check`; `git status` clean.
- [ ] Maintainer verifies the PyPI trusted publisher (workflow file `release.yml`, environment `pypi`) and the GitHub `pypi` environment (add a required reviewer).
- [ ] `git tag -a v0.2.0 -m "kustology 0.2.0" && git push origin main --tags` (the maintainer pushes).

## Verification (end-to-end)

```bash
.venv/bin/python -m pytest -q                                   # all green, count > 442
.venv/bin/ruff check src tests scripts examples && .venv/bin/mypy src
.venv/bin/python scripts/audit_syntax_kinds.py --check
.venv/bin/python scripts/mine_corpus.py
for f in examples/*.py; do .venv/bin/python "$f" >/dev/null || echo "FAILED $f"; done
TZ=Asia/Tokyo LANG=de_DE.UTF-8 LC_ALL=de_DE.UTF-8 .venv/bin/python -m pytest -q tests/ir tests/test_culture.py
.venv/bin/python /private/tmp/claude-501/-Users-eddie-Documents-repos-kustology/bbb566d3-62e4-4b10-91b7-68bebf071f3a/scratchpad/agentG/harness.py   # 0 exceptions / 0 round-trip failures / 0 schemaless-UNRESOLVED literals
.venv/bin/python -m build --outdir /tmp/dist && .venv/bin/twine check /tmp/dist/*
```
Spot checks from the review that must now pass: `let n = 10; T | take n`; `T | top-hitters 5 of a by b`; `TZ=Asia/Tokyo` hash equality; `sort by x asc` ≠ `desc`; `"// c\nSecurityEvent | take 1"` → `{'SecurityEvent'}`; `L | join kind=leftanti (R) on k` schema == Microsoft's; `T | project-keep Foo*` == Microsoft's; `parse(q).to_ir()` literal `1h` typed `timespan`.

## Execution approach

Recommended: **subagent-driven** (`superpowers:subagent-driven-development`) — one fresh subagent per task, two-stage review, WS1/WS2/WS3/WS7 in parallel worktrees (`superpowers:using-git-worktrees`), WS4→WS5→WS6→WS8 sequential on `main`. Alternative: inline execution with `superpowers:executing-plans`, batch per workstream. At the end of each workstream use `superpowers:finishing-a-development-branch` (the maintainer's standing preference is to merge to `main` locally; present the menu anyway).
