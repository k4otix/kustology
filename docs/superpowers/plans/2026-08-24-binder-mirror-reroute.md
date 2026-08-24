# Binder-Mirror Reroute Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every dict-schema `to_ir` path through Microsoft's binder (`build_global_state` + `KustoCode.Analyze`), retire `binder.py`'s hand-rolled per-operator schema mirror in favor of the existing overlay path, and derive aggregate auto-names from Microsoft's own `ResultNameKind`/`ResultNamePrefix` symbol properties — all before tagging v0.2.0.

**Architecture:** Four mechanism facts make this small: (1) `SchemaAttacher._walk_operator` already prefers Microsoft — when `op.result_schema` is not None it overlays and skips the hand rules (binder.py:799-822); (2) the builder already stamps `result_schema` on every operator and pipeline from `ResultType` (builder.py:907, :753), gated only by `table_symbol_columns`' refusal of OPEN symbols (`_builder_helpers.py:370`); (3) `KustoCode.Analyze(globals)` re-binds the in-hand tree with no re-parse and no mutation of the receiver (Microsoft KustoCode.cs:296); (4) `build_global_state(dict)` already converts the documented schema shapes to a real `GlobalState` (schema_state.py:254). So the reroute is ~10 lines in `core.to_ir`; everything after it is deleting what the reroute obsoletes and re-pointing tests at Microsoft's answers. Order: reroute first (additive, green), then retarget the oracle to the new path, then the binder surgery together with its tests, then the naming rework, then docs, then CI + tag.

**Tech Stack:** Python 3.10–3.13, pythonnet + Kusto.Language 12.3.2 (bundled DLL), pydantic v2, pytest, ruff, mypy, uv, GitHub Actions.

**Spec:** The 2026-08-23 PE audit (facts in `~/.claude/projects/-Users-eddie-Documents-repos-kustology/memory/audit-2026-08-23-release-branch.md`) plus this session's three verified research maps (core/schema-state, binder/builder internals, test inventory). Maintainer decisions (2026-08-24): the reroute lands **pre-0.2**; the aggregate-naming rework is **included** (symbol-property read, not new hand tables); the dict path stays **lenient** on unknown tables (partial schemas are the documented Sentinel norm; `parse(q, schema=...)` remains the strict path).

## Global Constraints

- All work on a new branch `release/0.2.0-binder-reroute` off `main` (cb25ec2). `IR_SCHEMA_VERSION` stays `"0.2"`; `SEMANTIC_HASH_SCHEME` stays `"kustology-sem-v2"` (nothing is released; one bump per release, already done).
- Python is always `.venv/bin/python`. Confirm every .NET member with `[m for m in dir(node) if m[:1].isupper()]` before using it (AGENTS.md).
- Gates after every task: `.venv/bin/python -m pytest` (pyproject addopts already pass `-q`; do NOT add another), `.venv/bin/ruff check src tests scripts examples`, `.venv/bin/mypy src`, `.venv/bin/python scripts/audit_syntax_kinds.py --check`.
- The suite is green at every commit. Tasks that delete rules delete/rework the tests of those rules in the same commit.
- Every behavior change ships a test asserting a non-default value on a real parse; CHANGELOG entries are 1–3 lines in the existing `## [0.2.0]` section (unreleased — rewrite stale lines rather than append contradictions).
- Never write a count you did not derive (AGENTS.md) — this includes xfail-list sizes and deleted-line totals.
- When deleting tests: reconcile the collected-count delta (`pytest --collect-only` before/after each task) so every removed test is accounted for; pin no totals anywhere.
- Conventional commit subjects; trailers: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_016EdpkDN9Um9Hxo6mW5FBDu`.
- Hash-movement budget: dict-path hashes may move ONLY for (a) the documented let-aliases-a-table / qualified-column bind-shape divergence class (the dict path joins the bound side) and (b) aggregate names the hand tables got wrong (the bug being fixed). Any other digest movement is a defect — the volatile-field strip (`result_schema`, `result_type`, `table`, …) makes enrichment-value improvements hash-silent.

## Context

The 0.2.0 remediation left one architectural duplication standing: `SchemaAttacher` re-derives per-operator output schemas by hand (~450 lines of rules in `_walk_operator_rules` plus ~200 lines of helpers) even though Microsoft's binder computes the same answer exactly, and the builder already captures it. The hand mirror only ever answers where Microsoft was not asked — the dict path never reached Microsoft — and where Microsoft declines (open symbols), it guesses, sometimes wrongly (the oracle's strict-xfail lists document the divergences). Rerouting the dict path through `build_global_state` + `Analyze` makes Microsoft answer everywhere a schema exists, makes `to_ir(attach_schema=dict)` byte-identical to `parse(q, schema=dict).to_ir()`, turns hand-rule divergences into fixed bugs, and lets the library honestly report `result_schema=None` where Microsoft declines instead of guessing. Doing it before the tag is deliberate: the hash movement it causes is free only while nothing is released. `SchemaAttacher` survives as the provenance pass (`ColumnRef.table`, `ScopeEntry.origins`, `$left`/`$right`) — Microsoft has no named-table provenance to replace it.

---

## Task 0: Branch and archive

**Files:** Create `docs/superpowers/plans/2026-08-24-binder-mirror-reroute.md`

- [ ] **Step 1:** `git checkout -b release/0.2.0-binder-reroute main` (main must be at cb25ec2, clean).
- [ ] **Step 2:** Copy this plan: `cp ~/.claude/plans/atomic-foraging-quail.md docs/superpowers/plans/2026-08-24-binder-mirror-reroute.md`
- [ ] **Step 3:** Commit: `docs: archive the binder-mirror reroute plan`

---

## Task 1: Reroute `to_ir(attach_schema=dict)` through Microsoft's binder

**Files:**
- Modify: `src/kustology/core.py` (`to_ir` :207-291; the top-level import block around :19)
- Test: `tests/test_to_ir_seam.py` (replace `test_to_ir_explicit_attach_schema_dict_overrides_parse_schema` at :127-143; add the tests below)

**Interfaces — Produces:** `to_ir(attach_schema=dict)` semantics: a non-empty dict means *bind (or re-bind) the in-hand tree against `build_global_state(dict)` and build the IR bound*; the provenance pass then runs with the same dict. `attach_schema={}` keeps today's meaning (falsy → no attach pass, no reroute). `None`/`True`/`False` semantics unchanged. The receiver stays syntactic (`has_semantics` unchanged). `ignore_unknown_tables` stays `not bound_by_caller` — the dict path is lenient by design.

- [ ] **Step 1: Write the failing tests** in `tests/test_to_ir_seam.py`:

```python
def test_a_dict_attach_reaches_microsofts_binder():
    """`attach_schema=dict` must bind through build_global_state + Analyze.

    `scan declare` adds columns (`v`, `match_id`) that only Microsoft's
    binder computes -- ScanOp is modeled as raw text and the hand rules
    never answered for it -- so their presence proves the dict reached
    Microsoft rather than the mirror."""
    q = "T | scan declare (v: long = 0) with (step s1: true => v = 1;)"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    cols = list(ir.main_pipeline.result_schema.columns)
    assert "v" in cols
    assert "match_id" in cols

def test_the_dict_path_equals_the_parse_time_binding():
    """`to_ir(attach_schema=d)` and `parse(q, schema=d).to_ir()` are now the
    same computation, shape included: `let A = T` lowers to rhs_pipeline in
    both, where the old dict path (unbound build) produced rhs_expr."""
    schema = {"T": {"a": "long"}}
    q = "let A = T; A | project a"
    via_dict = parse(q).to_ir(attach_schema=schema)
    via_parse = parse(q, schema=schema).to_ir()
    assert via_dict.model_dump(mode="json") == via_parse.model_dump(mode="json")

def test_a_dict_override_on_a_bound_parse_rebinds():
    """A dict on an already-bound parse re-binds against the dict, rather
    than overlaying the parse-time answer (which kept the old types)."""
    ir = parse("T | project a", schema={"T": {"a": "long"}}).to_ir(
        attach_schema={"T": {"a": "real"}}
    )
    assert ir.main_pipeline.result_schema.columns == {"a": "real"}

def test_the_dict_path_leaves_the_receiver_syntactic():
    kq = parse("T | take 1")
    kq.to_ir(attach_schema={"T": {"a": "long"}})
    assert kq.has_semantics is False

def test_a_partial_dict_stays_lenient():
    """Unknown tables under a partial dict are the Sentinel norm: the IR
    builds, and operators Microsoft leaves open report result_schema=None
    rather than raising or guessing. (The honest-None half tightens further
    in the binder-surgery task; here it just must not error.)"""
    q = "Unknown | where x > 1"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    assert ir.main_pipeline is not None
```

- [ ] **Step 2:** Run: `.venv/bin/python -m pytest tests/test_to_ir_seam.py -k "dict or partial" -x` → the first three FAIL (scan columns missing; shape mismatch on `let A = T`; override reports `long`).
- [ ] **Step 3: Implement.** In `core.py`: add `build_global_state` to the existing `from .utils.schema_state import ...` import (:19). Rewrite the body of `to_ir` (keep the method signature):

```python
        from .ir.builder import IRBuilder  # local import: triggers the [ir] extra guard lazily

        bound_by_caller = self._code.HasSemantics
        schemas = (
            attach_schema
            if isinstance(attach_schema, dict) and attach_schema
            else None
        )
        if schemas is not None:
            # A dict is a real binding request: re-bind the tree in hand
            # against it. ``Analyze`` does not re-lex the text and does not
            # mutate ``self._code``, so the receiver stays syntactic (or
            # keeps its parse-time binding) regardless.
            code = self._code.Analyze(build_global_state(schemas))
        elif bound_by_caller:
            code = self._code
        else:
            # D5/K27. ``Analyze`` binds this tree; it does not re-lex the
            # text and it does not mutate ``self._code``.
            code = self._code.Analyze(GlobalState.Default)
        ir = IRBuilder(global_state=code.Globals).build_from_code(
            code, ignore_unknown_tables=not bound_by_caller,
        )

        # Default: attach iff we have a bound parse to extract schemas from.
        # Explicit True/False/dict always wins.
        if attach_schema is None:
            attach_schema = bound_by_caller

        if attach_schema:
            from .ir.binder import SchemaAttacher

            if schemas is None and bound_by_caller:
                schemas = _extract_schemas_from_global_state(self._code.Globals)
            SchemaAttacher(schemas or {}).enrich(ir)

        return ir
```

- [ ] **Step 4: Rewrite the `to_ir` docstring.** Keep the schemaless-Analyze explanation (:215-243) intact; replace the two-pass description (:244-263) to say: Microsoft's binder answers types and per-operator output schemas whenever a schema is in play — parse-time or via a dict here, which re-binds the same tree; `SchemaAttacher` is the provenance pass (`ColumnRef.table`). Document: dict → full re-bind (partial dicts leave unknown symbols open → honest `None` schemas); the dict path now matches `parse(q, schema=...)` exactly, including the let-aliases-a-table shape; `{}` is falsy → treated as `False`.
- [ ] **Step 5:** Delete/replace `test_to_ir_explicit_attach_schema_dict_overrides_parse_schema` (:127-143) — `test_a_dict_override_on_a_bound_parse_rebinds` is its successor. Run the new tests → PASS. Full suite → green (nothing else uses the dict path yet). Reconcile any collected-count delta.
- [ ] **Step 6:** CHANGELOG `### Changed` (rewrite, don't contradict, the existing `to_ir(attach_schema=...)` line at ~:177): "`to_ir(attach_schema=dict)` now binds the query through Microsoft's binder (`build_global_state` + `Analyze`); output schemas, types, and IR shape match `parse(query, schema=...).to_ir()` exactly." Commit: `feat(ir)!: route attach_schema dicts through Microsoft's binder`

---

## Task 2: Retarget the oracle to the rerouted path

**Files:**
- Modify: `tests/ir/test_binder_oracle.py` (docstring :4-44; `fallback_columns` :423-435; `assert_fallback_agrees` :438-467; `_fallback_case` :309-313; `XFAIL_FALLBACK` :236-299; the two unbound-leg tests :470-501)
- Modify: `scripts/verify_corpus.py` (:49, :184, :291 — SchemaAttacher-direct usage)
- Modify: `CONTRIBUTING.md` :70-93 (the oracle-harness section)

**Interfaces — Produces:** the oracle's second leg becomes the **dict-path leg**: `parse(q).to_ir(attach_schema=schema)` compared against `microsoft_columns(q, schema)`, types included. It no longer gates hand rules; it gates the reroute plumbing (per-operator `ResultType` capture, ordering, the `Analyze` seam) on the full 74-case MATRIX and the corpus.

- [ ] **Step 1:** Rework `fallback_columns` → rename `dict_path_columns`; body: `ir = parse(query).to_ir(attach_schema=schema)` then the same `main_pipeline.result_schema` read. Rework `assert_fallback_agrees` → `assert_dict_path_agrees`: compare ordered `(name, type)` lists exactly (both sides are Microsoft now; delete the names-only/`unknown`-leniency machinery). Keep the `theirs is None → pytest.skip` guard.
- [ ] **Step 2:** Run the retargeted MATRIX leg. Expected: the five MATRIX `XFAIL_FALLBACK` entries (`project-reorder-desc`, `arg-max-star-beside-another-aggregate`, `mv-apply`, `find`, `scan`) now XPASS — `strict=True` turns them into failures — so **delete those five entries** (the reroute fixed them; that is the deliverable). The six corpus entries: re-run and re-derive empirically. Any that still fails does so because Microsoft's own symbol is open (the enrich pass still runs the hand rules until Task 3); update each surviving reason to say so, and note in the reason that Task 3 flips it to an honest-None assertion. Do not carry forward any reason text describing hand-rule behavior that no longer executes.
- [ ] **Step 3:** Rewrite the module docstring's two-leg story (:20-44): both legs now compare Microsoft's capture against Microsoft's direct answer; the bound leg samples (`BOUND_LEG_IDS`) because it shares the parse-time plumbing already exercised everywhere, while the dict leg keeps the full MATRIX because it is the public `attach_schema=dict` entry point being proven end-to-end. Update `CONTRIBUTING.md`'s oracle section to match (no counts).
- [ ] **Step 4:** `scripts/verify_corpus.py`: replace the `IRBuilder` + `SchemaAttacher(schemas=schemas)` pair with the public path (`parse(q).to_ir(attach_schema=schemas)`), preserving the script's outputs. Run `.venv/bin/python scripts/mine_corpus.py` and the script itself to confirm.
- [ ] **Step 5:** Full suite; reconcile the collected delta (xfail-entry deletions change xfail counts, not collection). Commit: `test(ir): the oracle's second leg rides the rerouted dict path`

---

## Task 3: Retire the hand mirror; keep provenance

**Files:**
- Modify: `src/kustology/ir/binder.py` (surgery per the design below)
- Modify: `tests/ir/test_binder.py` (disposition table below), `tests/ir/test_sources.py` (:70, :233, :313, :326), `tests/ir/test_binder_oracle.py` (open-symbol honesty flips)
- Modify: `docs`-facing prose only where it names deleted rules (ARCHITECTURE.md step 4 — done properly in Task 5; here only if a test greps it)

**Interfaces — Produces:** `SchemaAttacher` = provenance pass. Public behavior: with `op.result_schema` present (every closed-symbol path) — unchanged overlay; with it absent (open symbols, syntactic IRs) — scope passes through un-reshaped, `Pipeline.result_schema` stays/becomes `None` (honest), `ColumnRef.table` still fills from whatever scope exists. `ScopeEntry` and `_walk_pipeline(pipeline, inherited=None)` keep their signatures (tests call them directly).

### Surgery design (verified against binder.py at cb25ec2; every caller checked)

**(1) Replace `_walk_operator` (:799-822) and `_walk_operator_rules` (:824-1273) with:**

```python
    def _walk_operator(self, op: Operator, scope: list[ScopeEntry]) -> None:
        """Provenance first, then Microsoft's schema where there is one.

        The provenance step fills every expression the operator carries and
        adds the scope *structure* the multi-source operators need (a join's
        right side, a union's arms, a search's tables). It never derives the
        operator's output columns. Where the builder stamped
        ``op.result_schema`` -- Microsoft's ``ResultType``, present exactly
        where the symbol is closed -- it is overlaid onto that structure, so
        names and types are the binder's and each column keeps the table the
        walk knows it came from. Where it is ``None`` the scope passes
        through untouched: downstream references still resolve against the
        last known shape (stale where the operator really reshaped, and
        visibly so), and the pipeline's own ``result_schema`` will say
        ``None`` rather than guess.
        """
        self._walk_operator_provenance(op, scope)
        if op.result_schema is not None:
            self._overlay_result_schema(dict(op.result_schema.columns), scope)

    def _walk_operator_provenance(
        self, op: Operator, scope: list[ScopeEntry],
    ) -> None:
        """The scope structure provenance needs; never the output columns.

        Three operator families get a branch, because they bring *sources*
        into scope and the overlay cannot recover where a column came from
        once the sources are forgotten:

        * ``join`` / ``lookup``: the right-hand pipeline is walked and
          appended as one flattened entry, and ``_join_sides`` is set around
          the ``on`` clause so ``$left`` / ``$right`` and the bare-key
          left-first rule resolve. Join-kind column selection, the ``Foo1``
          collision suffixes and ``lookup``'s right-key drop are *not*
          reproduced -- ``result_schema`` states the surviving columns and
          the overlay applies them.
        * ``union``: each arm's entries are appended so a column only one
          arm carries keeps that arm's table. Type-conflict splitting
          (``a_long`` / ``a_string``) and ``withsource`` are Microsoft's to
          state.
        * ``search``: an implicit source, so without seeding here the
          predicate and every emitted column would resolve against nothing.
          One entry per named table is appended -- or per table in
          ``self.schemas`` for an unqualified search, the dict standing in
          for "every table in the database" -- and the predicate is filled
          *after*, against the seeded scope. ``$table`` is Microsoft's to
          state.

        Everything else fills its expressions and walks its nested
        pipelines, scope untouched. Implicit-source sub-pipelines (the
        ``mv-apply`` / ``partition`` / ``fork`` / ``facet`` bodies) inherit
        the current scope; ones with their own source ignore it.
        """
        if isinstance(op, (JoinOp, LookupOp)):
            rhs_scope = self._walk_pipeline(op.right)
            # Snapshot the left before the right entry is appended: ``$left``
            # is the accumulated left row set, which after a previous join is
            # several entries, and resolving it positionally named whichever
            # table that join happened to add.
            left_side = list(scope)
            right_side = [_flatten_side(rhs_scope)]
            on_scope = left_side + right_side
            previous_sides = self._join_sides
            self._join_sides = (left_side, right_side)
            try:
                for e in op.on:
                    self._fill(e, on_scope)
            finally:
                self._join_sides = previous_sides
            scope.append(right_side[0])
            return
        if isinstance(op, UnionOp):
            for sub in op.pipelines:
                for entry in self._walk_pipeline(sub):
                    if entry not in scope:
                        scope.append(entry)
            return
        if isinstance(op, SearchOp):
            names = [t.name for t in op.tables if isinstance(t, TableRef)]
            if not names:
                names = list(self.schemas)
            scope.extend(
                ScopeEntry(table=n, columns=dict(self._table_schema(n)))
                for n in names
            )
            self._fill(op.predicate, scope)
            return
        self._fill_children(op, scope, inherited=scope)
```

(Verify the exact field names — `op.right`, `op.on`, `op.pipelines`, `op.tables`, `op.predicate` — against the current join/union/search branches before transcribing; preserve the old post-seeding predicate-fill ordering for search.)

**(2) `enrich` snapshot (:337-354) becomes unconditional** — this fixes a latent double-enrich bug the design uncovered: with the merge leg gone, the old `schema_attached` guard would make a *second* `enrich` wipe every operator-less pipeline's schema to `None` (snapshot empty, nothing re-derives). The guard existed only because the walk used to write derived guesses into the field; the new walk writes back only copies of authoritative values or `None`, so reading the live field is always safe:

```python
        self._builder_schemas = {
            id(p): p.result_schema for p in find_all(ir, Pipeline)
        }
```

(Carry the reasoning above into the comment that replaces :337-354's.)

**(3) `_walk_pipeline` tail (:475-525):** `_set_scope`, `_scope_determined`, and the merge leg all go. The `$left`/`$right` save/restore and the operator loop stay; the tail becomes:

```python
        authoritative = (
            pipeline.operators[-1].result_schema if pipeline.operators
            else self._builder_schemas.get(id(pipeline))
        )
        pipeline.result_schema = (
            TabularSchema(columns=dict(authoritative.columns))
            if authoritative is not None
            else None
        )
        return scope
```

with a comment stating: the pipeline's schema is Microsoft's answer or nobody's; never a merge of the walked scope (post-surgery that scope is provenance structure, not a column inventory); `None` = unknown vs `TabularSchema(columns={})` = really emits nothing, and only Microsoft says the latter (bound `T | project-away *` closes to an empty symbol; `table_symbol_columns` returns `{}`, the stamp carries it).

**(4) Delete table** (all callers verified in-file and repo-wide; nothing external calls any of these):

| Symbol | Lines | Verdict |
|---|---|---|
| `fnmatchcase` import :15; `MULTI_OUTPUT_AGGREGATES`/`aggregate_function_name`/`percentile_token` imports :19-21; `StarExpr` :33; `TypedNameDecl` :34 | DELETE (`ARITHMETIC_OPS` :18 stays — `_fill:1386`) |
| 22 query-model imports used only by deleted branches (`Assignment, CountOp, DistinctOp, EvaluateOp, ExtendOp, FilterOp, GetSchemaOp, MakeSeriesOp, MvExpandOp, ParseKvOp, ParseOp, ParseWhereOp, PrintOp, ProjectAwayOp, ProjectByNamesOp, ProjectKeepOp, ProjectOp, ProjectRenameOp, ProjectReorderOp, RangeOp, SerializeOp, SummarizeOp`) | DELETE (keep `DataTableSource, ExternalDataSource, JoinOp, LetRef, LookupOp, Operator, Pipeline, QueryIR, SearchOp, TableRef, TabularSchema, UnionOp, UnknownSource`) |
| `_LEFT_ONLY_JOIN_KINDS`/`_RIGHT_ONLY_JOIN_KINDS` :76-85, `_matches_any` :102-121, `_join_kind` :157-170 | DELETE |
| `_scope_columns` :529-534, `_set_scope` :559-577, `_project_patterns` :579-598, `_expr_type` :600-608, `_extract_target_name` :610-617, `_aggregate_columns` :671-744, `_split_union_type_conflicts` :746-797 | DELETE |
| `_scope_determined` (:287-290 and every touch: 476, 478, 482, 485, 510, 654) | DELETE |
| `_side_marker` :88-99, `_flatten_side` :124-154, `ScopeEntry` :173-203, `_table_schema` :384-387, `_source_entry` :389-427 (whole — every branch feeds provenance seeding), `_column_origins` :536-557 (docstring edit: join collisions now reach it and answer `None`), `_overlay_result_schema` :619-669 (drop :654 and the `_set_scope` reference), `_resolve_side`/`_resolve_column_table` :1275-1298, `_fill_children` :1300-1330, `_fill` :1332-1389 **including the type-fallback tail** (it fills `Expr.result_type`, not schemas — the mandate is to stop mirroring operator outputs, not to stop typing expressions) | KEEP |

**(5) Class docstring (:207-271):** replace the 25-of-53 census with the provenance contract — schemas are Microsoft's to answer via the builder stamps (closed symbols; `None` = not determined, `{}` = really empty, symbols can close mid-pipeline: `count`, `datatable` roots); this class supplies `ColumnRef.table`, `ScopeEntry.origins`, `$left`/`$right`, `let` threading, and `result_type` backfill for the exactly-knowable cases only; provenance-then-overlay per operator; three structural families (join/lookup, union, search); accepted narrowings (post-join collision columns and union split variants file as ambiguous/anonymous — `$left.x`/`$right.x` keep their sides). Net accounting: binder.py lands around 700 lines (do not write the number anywhere in the repo).

**(6) Behavior edges to pin with tests** (add to `tests/ir/test_binder.py` unless noted; probe before asserting):
- E1: `datatable(a:long)[1] | project a` — schemaless `to_ir()` still yields `{"a": "long"}` (closed without any schema; overlay path).
- E2: schemaless `T | count` keeps `{"Count": "long"}` (closes mid-pipeline); probe `getschema` the same way before asserting either way.
- E3: bound `T | project-away *` → `TabularSchema(columns={})`; schemaless → `None` (move :1321 to a bound/dict parse).
- E4: join-collision provenance narrows — `shared`/`shared1` origins become `None` (was `L`/`R`); `$left.shared`/`$right.shared` in the `on` clause still resolve to their sides. Pin both.
- E5: union type-conflict variants (`a_long`/`a_string`) file as anonymous, origin `None` (was per-arm). Pin the new answer (supersedes the REWIRE of :1106 — expectations change to `None`).
- E6: double-enrich — bound parse of `"T"`, `enrich` twice, `result_schema` unchanged. The re-enrich tests at :1022-1071 INVERT: a second `enrich` with a different dict no longer changes `result_schema` (re-binding via `to_ir(attach_schema=...)` is how you change a schema now) — rework them to assert the new invariant.
- E7: let threading is Microsoft-gated — on the dict path bindings close and thread (`_let_schemas` fills); on a raw unbound IR with a dict attacher the alias stays unregistered and downstream is honestly unresolved. Pin one of each.
- E8: mid-pipeline unqualified `search` still seeds every dict table (pre-existing quirk, now provenance-only — at worst ambiguous → `None` origins). Comment, no fix.

- [ ] **Step 1: Write the failing honesty tests** in `tests/ir/test_binder.py`:

```python
def test_an_open_symbol_gets_no_invented_schema():
    """Partial schemas are the norm; where Microsoft declines to type an
    operator (open symbol -- the table is not in the dict), the IR now says
    result_schema=None instead of a hand-computed guess."""
    ir = parse("Unknown | project a, b").to_ir(attach_schema={"T": {"a": "long"}})
    (op,) = ir.main_pipeline.operators
    assert op.result_schema is None
    assert ir.main_pipeline.result_schema is None

def test_provenance_still_fills_under_an_open_symbol():
    """Deleting the schema rules must not delete provenance: a column read
    from a table the dict does describe keeps its table even when a later
    operator is open."""
    q = "T | where a > 1 | lookup Unknown on a | project a"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FilterOp, find_all
    (where_op,) = [op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)]
    assert {c.table for c in find_all(where_op, ColumnRef)} == {"T"}
```

Run → the first FAILS today (the hand rules compute `{a: ..., b: ...}` for the project). The second passes today and after — it is the regression net for the cut.

- [ ] **Step 2: Perform the surgery** exactly per the design block above: replace `_walk_operator`/`_walk_operator_rules` with the provenance-only walk, simplify the `_walk_pipeline` tail, delete the dead defs/constants listed, rewrite the class docstring. `ruff` will flag the dead imports (binder.py:17-22 shrink).
- [ ] **Step 3: Rework `tests/ir/test_binder.py`** per this disposition table. Rules: DELETE = the shape is owned by the oracle MATRIX (name the owning MATRIX id in the deletion commit if ambiguity exists); REWIRE = same assertion, entry point becomes `parse(q).to_ir(attach_schema=...)` (expectations may only change to Microsoft's answers — record each such change); KEEP = unchanged. Helpers: `_fallback` (:713-726) is deleted with its premise; `_syntactic` (:1218-1234) stays for syntactic-path tests; `_final_columns` (:47-49) survives only if a kept test still uses it.

| Section / lines | Verdict |
|---|---|
| Scope-narrowing :54-116, away/keep/reorder :120-151, parse :155-173, mv-expand :177-187, make-series :191-201 | DELETE (MATRIX: project-keep-plain, project-away-plain, project-reorder, parse-typed/untyped, mv-expand-*, make-series) |
| result_schema population :206-215 | REWIRE to dict path (TabularSchema plumbing) |
| result_schema ordering :217-285 | DELETE (MATRIX: summarize-by-bin, project-keep-plain, project-reorder, project-rename, make-series) |
| Auto-naming parity :289-317 | DELETE (owned by Task 4's library-parity test + MATRIX arg-max/make-set/percentile ids) |
| Join suffixing :321-359 | DELETE (MATRIX join-* + bound leg join-inner) |
| Make-series range fields :363-384 | KEEP (builder fields, no enrich) |
| Traversal completeness :404-476 | REWIRE (provenance; :453's nested-pipeline schema assert moves to a bound/dict parse — unbound it becomes `None`) |
| count reshape :478-487 | DELETE (MATRIX: count, count-as) |
| let threading :492-566 | REWIRE to dict path (`_let_schemas` now fills from Microsoft-stamped binding pipelines); :531 forward-ref and :548 scalar-binding KEEP; :555 no-leak KEEP |
| K-ARCH-1 :570-690 | :570/:600 KEEP (bound provenance); :632 REWRITE as honesty (Microsoft declines → stays None; the hand rule no longer answers); :653/:667 KEEP |
| K28 origins :747-797 | REWIRE to dict path (pure provenance; expectations unchanged) |
| K07 :802-869 | DELETE :802,:817,:839,:847,:862 (MATRIX join-* family); REWIRE :825 (right-side provenance) |
| K08 wildcards :874-909 | DELETE (MATRIX project-keep-wildcard, project-away-wildcard; case-sensitivity is Microsoft's own now) |
| K09 mv-expand :914-945 | DELETE (MATRIX mv-expand-* ids) |
| K10/K11 on-clause :958-1016 | REWIRE ($left/$right provenance stays) |
| Re-enrich :1022-1071 | REWORK per edge E6 — the invariant inverts: a second `enrich` no longer changes `result_schema`; re-binding is how you change a schema |
| K12 union :1076-1104 | DELETE :1076,:1092,:1101 (MATRIX union-conflict/-withsource/-kind-*); REWIRE :1106 (split-column provenance — expectations become Microsoft's split names) |
| K13/K14 naming :1120-1213 | DELETE (MATRIX arg-max*/take-any*/percentile*/make-set/make-list + Task 4 parity); EXCEPT :1193 (auto-name lands on Assignment) — KEEP, it pins the hashed field |
| K28 search/fixed shapes :1237-1304 | DELETE :1237,:1250,:1258,:1274,:1280,:1288,:1293,:1298 (MATRIX search, parse-kv, getschema, print, range); REWIRE :1269 (search provenance) |
| "nothing known is None" :1310-1377 | KEEP :1310; REWIRE :1321 (`project-away *` → `{}` — now Microsoft's closed empty answer, via dict path); KEEP :1329 (ScopeEntry + `_walk_pipeline` direct); KEEP :1351 (the `_fill` type-fallback tail survives) |
| schema_attached :1382-1404 | KEEP |
| Arithmetic/serialize :1409-1430 | KEEP :1409 (tests the surviving `_fill` tail on the syntactic path); DELETE :1422 (MATRIX serialize-with-column) |
| Corpus-corrected :1435-1465 | DELETE (premises name the deleted rules) |
| evaluate :1470-1487 | DELETE (MATRIX evaluate-bag-unpack; :1483's no-op plug-in shape moves to a MATRIX row if absent — check for id `facet-by`-style coverage first, else add `evaluate-autocluster` to MATRIX) |
| Tail :1489-1595 | KEEP (:1489, :1510, :1528, :1544 provenance/plumbing); :1559 KEEP, adjust the SchemaAttacher-producer half to whatever surviving code produces the sentinel |

- [ ] **Step 4:** `tests/ir/test_sources.py`: :70/:233 (datatable/externaldata seed the scope) — verify they still pass: those sources are closed even schemaless, so the overlay path covers them; rewire only if red. :313 (`{"T*": ...}` wildcard schema key) — DELETE and replace with the honest statement: wildcard *sources* now resolve through Microsoft when the dict lists real tables (`parse("union T1, T2 | count").to_ir(attach_schema={"T1": ..., "T2": ...})` closes); a literal `"T*"` key is just a strangely-named table. :326 KEEP.
- [ ] **Step 5:** Oracle honesty flips: for each corpus xfail that survived Task 2 because of open symbols, replace the xfail with the honest assertion — when Microsoft's whole-query `ResultType` is open (probe `getattr(result_type, "IsOpen", False)` in `microsoft_columns`, returning a sentinel), assert `pipeline.result_schema is None`. Entries whose queries now pass outright: delete. `XFAIL_5_3` (:201-228, bound leg): same treatment — these four are open-symbol cases; expected result is deletion of the dict, with the honesty branch answering for both legs.
- [ ] **Step 6:** Full suite + gates; reconcile the collected delta precisely (this is the big deletion task). Commit: `refactor(ir)!: retire the hand-rolled schema mirror; SchemaAttacher is the provenance pass`
- [ ] **Step 7:** CHANGELOG `### Changed`: "Partial schemas no longer get hand-guessed operator schemas: where Microsoft's binder leaves a symbol open, `result_schema` is now honestly `None`. `ColumnRef.table` provenance is unaffected." Include in the same commit.

---

## Task 4: Aggregate auto-names from Microsoft's symbol properties

**Files:**
- Modify: `src/kustology/ir/builder.py` (`_auto_name` :1992-2065; `_take_binder_aggregate_names` :2067-2123 and its call site :936; `_visit_expr_as_assignment` :1972-1990)
- Modify: `src/kustology/ir/_builder_helpers.py` (:640-698: delete `AGGREGATE_NAME_PREFIXES`; keep `percentile_token`; `COLUMN_NAMED_AGGREGATES`/`MULTI_OUTPUT_AGGREGATES` per Step 4)
- Test: `tests/ir/test_ir_builder.py` (new parity test), `tests/ir/test_binder_oracle.py` (auto-name invariance test must stay green)

**Interfaces — Produces:** `_auto_name(node, mode)` derives names by porting Microsoft's `GetFunctionResultName` (Binder_Projection.cs:612-700): read `ResultNameKind`/`ResultNamePrefix` off the call's `ReferencedSymbol` (public properties; resolve via `str()` on the enum), apply the 8-kind switch, with argument names from the existing paren-unwrap/`NameReference` walk. Fallbacks preserved: no symbol or kind `None` → today's `f"{fname}_{first_col}"` / `f"{fname}_"` behavior (covers raw `KustoCode.Parse` trees and unresolved functions — names there must not change). `COLUMN_NAMED_AGGREGATES` handling stays (multi-output aggregates: `Assignment.name` = first argument's name — Microsoft names their *columns*, not the call). Percentile value-suffix spelling stays via `percentile_token`.

- [ ] **Step 1: Write the failing test** (the drift case the strict xfail documents):

```python
def test_buildschema_takes_microsofts_name_even_beside_a_multi_output_aggregate():
    """`arg_max(t, *)` reports six columns bound and one unbound, so the old
    operator-level alignment read had to give up on the whole summarize --
    and `buildschema(d)` fell to the hand rule's `buildschema_d` where the
    engine says `schema_d` (Aggregates.cs declares PrefixAndFirstArgument +
    prefix "schema"). Reading ResultNameKind off the resolved symbol is
    per-expression, so the alignment problem does not exist."""
    ir = parse("T | summarize arg_max(t, *), buildschema(d)").to_ir()
    names = [a.name for a in ir.main_pipeline.operators[0].aggregations]
    assert names == ["t", "schema_d"]

def test_auto_names_are_bind_invariant_under_the_symbol_read():
    q = "T | summarize buildschema(d), make_set(s), binary_all_and(a)"
    bound = parse(q, schema={"T": {"d": "dynamic", "s": "string", "a": "long"}}).to_ir()
    unbound = parse(q).to_ir()
    pick = lambda ir: [a.name for a in ir.main_pipeline.operators[0].aggregations]
    assert pick(bound) == pick(unbound) == ["schema_d", "set_s", "binary_all_and_a"]
```

(Verify the `binary_all_and` expectation first — `FirstArgument` kind names it `a`, in which case the expected list is `["schema_d", "set_s", "a"]`; probe Microsoft once and pin the real answer. Whichever it is, it is what the operator-level `Columns` says for the single-aggregate query.)

- [ ] **Step 2:** Run → FAIL (`buildschema_d`).
- [ ] **Step 3: Implement** `_function_result_name(self, node) -> str | None` in builder.py: read `node.ReferencedSymbol`; if absent return None; read `str(sym.ResultNameKind)` and `sym.ResultNamePrefix`; port the switch verbatim in Python (fold `NameAndFirstArgument`→`PrefixAndFirstArgument` with `prefix = sym.Name`, same for `OnlyArgument` variants; `FirstArgumentValueIfColumn` may return None — fall through to the fallback). Argument names via the existing first-`NameReference` walk (which is `GetExpressionResultName` for our purposes). In `_auto_name`: aggregation mode tries `_function_result_name` first; `COLUMN_NAMED_AGGREGATES` and the percentile literal-token special case keep their current precedence (they answer the multi-output and percentile-value cases the symbol read cannot); the final fallback is unchanged. Grouping mode: also try `_function_result_name` (e.g. `bin` is `FirstArgument`), falling back to `first_col`.
- [ ] **Step 4: Delete** `_take_binder_aggregate_names` (:2067-2123) and its call site (:936) — the symbol read is per-expression and bind-stable, so the operator-level alignment read has no remaining job. Delete `AGGREGATE_NAME_PREFIXES`. `MULTI_OUTPUT_AGGREGATES`: its binder.py consumer died in Task 3 and its builder.py consumer dies here — delete it too if no caller remains; `COLUMN_NAMED_AGGREGATES` stays (still consumed by `_auto_name`).
- [ ] **Step 5: Write the library-parity test** in `tests/ir/test_ir_builder.py`:

```python
def test_aggregate_auto_names_match_microsofts_for_the_whole_library():
    """Exhaustive: for every aggregate in Microsoft's own library that our
    probe can call with one column argument, the Assignment.name we derive
    must equal the column name Microsoft reports for `T | summarize fn(col)`.
    This pins the ResultNameKind port against the DLL, upgrade after upgrade."""
    from Kusto.Language import Aggregates
    from kustology.utils.analysis import build_global_state
    schema = {"T": {
        "s": "string", "n": "long", "r": "real", "b": "bool",
        "t": "datetime", "ts": "timespan", "d": "dynamic", "g": "guid",
    }}
    by_type = {"string": "s", "long": "n", "int": "n", "real": "r",
               "decimal": "r", "bool": "b", "datetime": "t",
               "timespan": "ts", "dynamic": "d", "guid": "g"}
    state = build_global_state(schema)
    mismatches, probed = [], 0
    for sym in Aggregates.All:
        name = str(sym.Name)
        probe = None
        for arg in by_type.values():
            q = f"T | summarize {name}({arg})"
            code = KustoCode.ParseAndAnalyze(q, state)
            result_type = getattr(code, "ResultType", None)
            columns = getattr(result_type, "Columns", None)
            if columns is None or columns.Count != 1 or any(
                str(d.Severity) == "Error" for d in code.GetDiagnostics()
            ):
                continue
            probe = (q, str(columns[0].Name))
            break
        if probe is None:
            continue  # needs literals/multiple args; the MATRIX covers the famous ones
        probed += 1
        q, expected = probe
        ir = parse(q, schema=schema).to_ir()
        (agg,) = ir.main_pipeline.operators[0].aggregations
        if agg.name != expected:
            mismatches.append((name, agg.name, expected))
    assert mismatches == []
    # The loop must not silently degrade into probing nothing.
    assert probed >= 40, f"only {probed} aggregates were probe-able"
```

(Confirm `Aggregates.All` exists via the member-probe convention first; if the property spells differently, adapt. The `>= 40` floor is a probe-coverage guard, not a library count — the library is larger.)

- [ ] **Step 6:** Run everything: the new tests PASS; `test_auto_names_do_not_depend_on_the_bind_state` (oracle :504) stays green — symbol properties resolve identically in both bind states because aggregates live in `GlobalState.Default`. Full suite + gates; reconcile deltas.
- [ ] **Step 7:** CHANGELOG `### Fixed`: "Aggregate auto-names now come from Microsoft's own `ResultNameKind`/`ResultNamePrefix` symbol properties; irregulars (`buildschema` → `schema_d`, and family) are named as the engine names them, in both bind states." Commit: `fix(ir): derive aggregate auto-names from Microsoft's symbol properties`

---

## Task 5: Documentation altitude pass

**Files:** `README.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md` (only if Task 2's rewrite left residue), `CHANGELOG.md`, `AGENTS.md` (:281 field-name example if it names a deleted method)

- [ ] **Step 1:** `ARCHITECTURE.md` :91-117 (step 4 of "a new tabular operator"): rewrite — a new operator gets its output schema from Microsoft via the wrapped dispatch (`builder.py`'s `_visit_operator`) with no hand rule; what a contributor owes is *provenance* (does the operator reshape which table a column comes from? if not, the generic fill covers it), and the oracle MATRIX row. Delete the 17-of-53 cautionary tale sentence or recast it as history. Also :18 (file-map line for binder.py → "SchemaAttacher: provenance (ColumnRef.table, origins)").
- [ ] **Step 2:** `README.md` :338-341: the comment block still describes the attach pass; update to "pass `attach_schema={...}` to bind against a schema after the fact — identical to having parsed with `schema=`". Check :48-56 (provenance paragraph — still accurate) and :115 (tier table — update the Tier 2 cell's "attach pass materializes" wording to name Microsoft as the schema source).
- [ ] **Step 3:** `CHANGELOG.md` [0.2.0]: reconcile the section with the three new entries already added — the long per-operator fallback-rule entry (~:114) describes rules that no longer exist at release: rewrite it to past-tense scope ("interim hand rules, since retired in favor of Microsoft's binder — see Changed") or fold it into the reroute entry; update ~:130 (oracle description) and ~:178 (`SchemaAttacher` description → provenance pass). Verify the `[0.2.0]` date is still the intended ship day; update if the calendar moved.
- [ ] **Step 4:** Full suite (`test_docs_claims.py` guards some prose). Commit: `docs: the binder answers schemas; SchemaAttacher is provenance`

---

## Task 6: Reconcile and close

- [ ] **Step 1:** Full gate run: `pytest` (plain), `ruff check src tests scripts examples`, `mypy src`, `audit_syntax_kinds.py --check`, `python scripts/mine_corpus.py`, every `examples/*.py`.
- [ ] **Step 2:** Reconcile the total collected-count movement across Tasks 1–5 against the per-task notes; account for every removed/added test. Run the hash battery and `test_semantic_hash_bind_invariance.py` specifically and confirm the only digest movements on record are the two budgeted classes (bind-shape divergence on the dict path; corrected aggregate names).
- [ ] **Step 3:** Commit anything outstanding: `docs: close the binder-mirror reroute`

---

## Task 7: CI, merge, tag (maintainer gate)

- [ ] **Step 1:** Push: `git push -u origin release/0.2.0-binder-reroute`; open a PR into `main` — `test.yml` fires only on push/PR to main, and the 19-check matrix (Windows/Linux × 3.10–3.13 + dependency-review + sbom + verify-dll) must be green before merge.
- [ ] **Step 2:** Fix any real matrix failures as new tasks. Known watch-item: if the called test workflow queues indefinitely, rename `release.yml`'s concurrency group to `release-publish-${{ github.ref }}`.
- [ ] **Step 3:** Present the finish menu (maintainer's standing preference is a local merge to `main`, but present the options). After merge and green main-push CI:
- [ ] **Step 4 (maintainer-only):** Verify the PyPI trusted publisher (repo + `release.yml` + environment `pypi`) and the `pypi` environment's required reviewer; confirm `CHANGELOG.md`'s `[0.2.0]` date equals the actual ship day; then `git tag -a v0.2.0 -m "kustology 0.2.0" && git push origin v0.2.0`.

---

## Explicitly out of scope (unchanged 0.3 items)

- `LetFunction` body modeling and `EvaluateOp.declared_schema` (both disclosed, `KNOWN_COLLISIONS`-pinned; note Task 3 does NOT fix these — Microsoft *expanding* a let function on the dict path affects `result_schema` (volatile), not the digest).
- `BinderEnricher` tombstone test deletion (one-release guard).
- `AnyExpr` discriminated conversion.

## Verification (end-to-end)

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests scripts examples && .venv/bin/mypy src
.venv/bin/python scripts/audit_syntax_kinds.py --check
.venv/bin/python scripts/mine_corpus.py
for f in examples/*.py; do .venv/bin/python "$f" >/dev/null || echo "FAILED $f"; done
# Spot checks that must hold at the end:
#  - parse(q).to_ir(attach_schema=d).model_dump() == parse(q, schema=d).to_ir().model_dump() for the let-alias query
#  - the scan query's result_schema contains v and match_id via the dict path
#  - Unknown-table operators under a partial dict carry result_schema=None (no guesses)
#  - summarize arg_max(t, *), buildschema(d) names its aggregates ["t", "schema_d"] in both bind states
#  - XFAIL_FALLBACK and XFAIL_5_3 are gone or reduced to Microsoft-open honesty assertions with current reasons
#  - the CI matrix on the PR is fully green before any merge or tag
```
