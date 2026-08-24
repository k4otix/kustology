# 0.2.0 Pre-Tag Shore-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land every correct/simplify/improve item from the 2026-08-23 PE audit on `release/0.2.0-remediation`, then run the branch's first real CI and tag v0.2.0.

**Architecture:** Correctness fixes first (the DataTableExpr twin is the only model change), then the two structural collapses (discriminated unions, KIND ClassVar removal) *before* the test-suite reduction so the cuts land on final structure, then the ~20% test cut, then docs/regression odds-and-ends, then the release gate (push → PR → CI matrix → merge → tag).

**Tech Stack:** Python 3.10–3.13, pythonnet + Kusto.Language 12.3.2 (bundled DLL), pydantic v2, pytest, ruff, mypy, uv, GitHub Actions.

**Spec:** The audit's punch list (conversation of 2026-08-23; key verified facts in `~/.claude/projects/-Users-eddie-Documents-repos-kustology/memory/audit-2026-08-23-release-branch.md`). Maintainer decisions: model `DataTableExpr` now (not disclose-and-defer); land the discriminated-union conversion in 0.2.0.

## Context

A 44-task remediation branch (170 commits, +27k lines) closed every finding from the 0.2.0 pre-release review. A cost/value audit of that branch found: one live defect (`datatable` in expression position lowers to `UnknownExpr` while `HANDLED_EXPR_KINDS` claims the kind is handled), one CI-policy contradiction (`release.yml` runs the online DLL check as a hard gate), duplicated builder logic already drifting (`FuncCall` vs `FuncCallSource` name reads), two hand-maintained invariants pydantic can enforce itself (union ordering rule, `KIND` ClassVar), and ~360–390 tests (~20% of 1,828) that re-assert behavior already pinned elsewhere. Also verified: `main` is **green** (the "10 red tests" were an editable-install artifact), the branch merge is a pure fast-forward, and **no CI has ever run** — `test.yml` fires only on push/PR to `main`, so a PR is required before the tag.

## Global Constraints

- All work lands on `release/0.2.0-remediation`. `IR_SCHEMA_VERSION` stays `"0.2"`; `SEMANTIC_HASH_SCHEME` stays `"kustology-sem-v2"` (nothing is released yet; one bump per release).
- Python is always `.venv/bin/python`. pythonnet member lookup: confirm every .NET member with `[m for m in dir(node) if m[:1].isupper()]` before using it (AGENTS.md).
- Gates after every task: `.venv/bin/python -m pytest` (note: pyproject addopts already pass `-q`; do NOT add another `-q` — `-qq` suppresses the totals line), `.venv/bin/ruff check src tests scripts examples`, `.venv/bin/mypy src`, `.venv/bin/python scripts/audit_syntax_kinds.py --check`.
- Every behavior change ships a test asserting a **non-default value on a real parse** (AGENTS.md) and a `CHANGELOG.md` entry appended to the existing `## [0.2.0]` section.
- **Never write a derivable count in prose or docstrings** — derive it or describe what qualifies (AGENTS.md:70 rule; Task 11 extends it beyond Microsoft structures).
- Conventional commit subjects; every commit ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- When deleting tests: after each deletion batch, run the full suite AND reconcile the collected-count delta (`pytest --collect-only` before/after) so every removed test is accounted for. Do not pin expected totals anywhere.

---

## Task 0: Archive this plan in the repo

**Files:** Create `docs/superpowers/plans/2026-08-23-pre-tag-shore-up.md`

- [ ] **Step 1:** Copy this plan file into place: `cp ~/.claude/plans/atomic-foraging-quail.md docs/superpowers/plans/2026-08-23-pre-tag-shore-up.md`
- [ ] **Step 2:** Commit: `docs: archive the pre-tag shore-up plan`

---

## Task 1: Model `DataTableExpr` (the expression-position twin)

**Files:**
- Modify: `src/kustology/ir/expr.py` (new class after `ExternalDataExpr`, add to `AnyExpr`), `src/kustology/ir/builder.py` (extract shared read from `_visit_datatable` at :806-823; new `_visit_expr` branch; fix the false comment at :415-420), `src/kustology/ir/_normalize.py` (`canonical()` branch), `src/kustology/ir/__init__.py` (import + `__all__`), `tests/ir/test_hash_battery.py` (pairs)
- Test: `tests/ir/test_sources.py` (twin behavior beside the existing source tests)

**Interfaces — Produces:** `DataTableExpr(Expr)` with `KIND/kind = "datatable"`, `columns: list[tuple[str, str]]`, `rows: list[list[AnyExpr]]` — same payload fields as `DataTableSource` (`query.py:272`). Builder helper `_read_datatable(node) -> tuple[list[tuple[str, str]], list[list[AnyExpr]]]` shared by `_visit_datatable` and the new expr branch (the `read_external_data` sharing pattern, `_builder_helpers.py:243`).

- [ ] **Step 1: Write the failing tests** in `tests/ir/test_sources.py`:

```python
def test_datatable_in_expression_position_is_modeled():
    """`in ((datatable(...)))` parses clean and must not fall to UnknownExpr.

    Verified live during the 2026-08-23 audit: this shape lowered to
    UnknownExpr(ast_kind="DataTableExpression") while HANDLED_EXPR_KINDS
    claimed the kind "only ever occupies source position" — so the coverage
    audit was blind to the miss and the digest hashed the raw text.
    """
    from kustology.ir import DataTableExpr, UnknownExpr, find_all
    q = 'T | where a in ((datatable(x:string)["v", "w"]))'
    kq = parse(q)
    assert kq.diagnostics == []
    ir = kq.to_ir()
    assert not find_all(ir, UnknownExpr)
    (dt,) = find_all(ir, DataTableExpr)
    assert dt.columns == [("x", "string")]
    assert [cell.value for row in dt.rows for cell in row] == ["v", "w"]

def test_expression_datatable_values_reach_the_hash():
    a = parse('T | where a in ((datatable(x:string)["v"]))').to_ir().semantic_hash
    b = parse('T | where a in ((datatable(x:string)["w"]))').to_ir().semantic_hash
    assert a != b

def test_expression_datatable_whitespace_does_not_split():
    a = parse('T | where a in ((datatable(x:string)["v"]))').to_ir().semantic_hash
    b = parse('T | where a in ((datatable(x:string) [ "v" ]))').to_ir().semantic_hash
    assert a == b
```

- [ ] **Step 2:** Run: `.venv/bin/python -m pytest tests/ir/test_sources.py -k datatable_in_expression -x` → FAIL (ImportError on `DataTableExpr`, then UnknownExpr found).
- [ ] **Step 3: Implement.** (a) In `expr.py`, add after `ExternalDataExpr`, following its docstring pattern (name the source twin, name the shared reader):

```python
class DataTableExpr(Expr):
    """``datatable(...)[...]`` in expression position — a membership set.

    The source-position form is
    :class:`~kustology.ir.query.DataTableSource`; both are filled by the
    builder's ``_read_datatable`` so they cannot drift apart. The values
    are the query — see ``DataTableSource`` for why dropping them was a
    collision; in expression position the un-modeled shape was worse, an
    ``UnknownExpr`` hashing its own source text.
    """

    KIND: ClassVar[str] = "datatable"
    kind: Literal["datatable"] = "datatable"
    columns: list[tuple[str, str]]
    rows: list[list[AnyExpr]]
```

Add `"DataTableExpr"` to the `AnyExpr` union (before the permissive `Expr` tail) and to any `model_rebuild()` list the module keeps. (b) In `builder.py`, refactor `_visit_datatable` (:806) so the columns/rows computation is a `_read_datatable(self, node)` method returning `(columns, rows)`; `_visit_datatable` becomes `columns, rows = self._read_datatable(node); return DataTableSource(...)`. Add to `_visit_expr`'s dispatch: `elif kind == "DataTableExpression": columns, rows = self._read_datatable(node); return DataTableExpr(columns=columns, rows=rows, span=span)`. (c) Replace the false comment block under `"DataTableExpression"` in `HANDLED_EXPR_KINDS` (:415-420) — the kind is now handled in *both* positions. (d) `_normalize.py`: give `canonical()` a `DataTableExpr` branch rendering `datatable(x:string)[…]` from columns + child renders (mirror the `ExternalDataExpr` branch's shape). (e) `ir/__init__.py`: import + `__all__` (the guard tests in `test_canonical_coverage.py` enforce b–e — run them to find anything missed).

- [ ] **Step 4:** Run the three new tests → PASS. Run `tests/ir/test_canonical_coverage.py` → PASS (proves `AnyExpr`/`__all__`/`canonical()` coverage). Full suite → green. `audit_syntax_kinds.py --check` → unchanged (the kind was already listed).
- [ ] **Step 5:** Add to `tests/ir/test_hash_battery.py` `MUST_DIFFER`: `("expr-datatable-values", 'T | where a in ((datatable(x:string)["v"]))', 'T | where a in ((datatable(x:string)["w"]))')` and to `MUST_EQUAL`: the whitespace pair from Step 1. Run the battery (its blob-guard now passes for these queries **because** the node is modeled — that is the regression net).
- [ ] **Step 6:** CHANGELOG `### Fixed`: "`datatable(...)` in expression position (`in ((datatable(...)))`) is now modeled as `DataTableExpr` instead of falling through to `UnknownExpr`; its values reach the digest as structure rather than as raw source text." Commit: `fix(ir): model datatable in expression position (DataTableExpr)`

---

## Task 2: `release.yml` — offline DLL gate, online as warning

**Files:** Modify `.github/workflows/release.yml` (the bare `python scripts/verify_dll.py` at ~:40)

- [ ] **Step 1:** Mirror the policy `test.yml:292-308` already spells out. Replace the single step with:

```yaml
      - name: Verify bundled DLL against the pin (offline, hard gate)
        run: python scripts/verify_dll.py --offline
      - name: Verify bundled DLL against NuGet (online, warning only)
        run: |
          python scripts/verify_dll.py || {
            rc=$?
            if [ "$rc" -eq 2 ]; then echo "::warning::verify_dll online leg unavailable (network/config, exit 2) — offline pin already verified"; exit 0; fi
            exit "$rc"
          }
```

- [ ] **Step 2:** Validate: `python -c "import yaml; yaml.safe_load(open('.github/workflows/release.yml'))"`. Simulate locally: `.venv/bin/python scripts/verify_dll.py --offline` → rc 0.
- [ ] **Step 3:** CHANGELOG `### Internal`: one line. Commit: `ci(release): hard-gate the offline DLL pin; online check degrades to a warning`

---

## Task 3: Shared `read_func_call` for the FuncCall/FuncCallSource twins

**Files:** Modify `src/kustology/ir/builder.py` (:690-707 source path, :1817-1844 expr path), Test: `tests/ir/test_sources.py`

The two copies already differ: the expression path prefers `ReferencedSymbol.Name`, the source path is syntactic-only. **Preserve each path's current behavior** (unifying would move hashes days before the tag) — centralize the reading so the drift is a documented parameter, not two divergent copies.

- [ ] **Step 1: Write the failing test** (pins both current behaviors so the refactor is provably inert):

```python
def test_func_call_name_reads_agree_across_positions():
    """One reader, two positions. The expression path resolves through the
    binder (safe: both bind states start from GlobalState.Default); the
    source path stays syntactic. Pinned so the shared reader cannot
    silently change either."""
    from kustology.ir import FuncCall, FuncCallSource, find_all
    ir = parse("materialized_view('MV') | where tostring(a) == 'x'").to_ir()
    (src,) = find_all(ir, FuncCallSource)
    assert src.name == "materialized_view"
    names = {f.name for f in find_all(ir, FuncCall)}
    assert "tostring" in names
```

- [ ] **Step 2:** Run → currently PASSES (it pins existing behavior). That is intentional: this is a refactor task; the test is the safety net, written first and seen green before and after. Record both hash digests of the two queries above pre-refactor (`compute_semantic_hash`) in the task notes.
- [ ] **Step 3:** Extract `_read_func_call(self, node, *, prefer_symbol: bool) -> tuple[str, list[AnyExpr]]` in `builder.py`; both call sites use it (`prefer_symbol=True` for the expr path, `False` for the source path), each site keeping a one-line comment saying why its flag is what it is (the bind-invariance argument lives at the expr site).
- [ ] **Step 4:** Full suite green; re-derive the two digests from Step 2 — byte-identical. Commit: `refactor(ir): one reader for function-call names in both positions`

---

## Task 4: Discriminated unions (spike, then convert)

**Files:** Modify `src/kustology/ir/query.py` (`Pipeline.source` :1157, `Pipeline.operators` :1170, `SearchOp.tables` :487, `FindOp.tables` :867 — delete the ORDERING RULE comment blocks), `tests/ir/test_union_ordering.py`, `CHANGELOG.md`

**Interfaces — Produces:** all four unions become `Field(discriminator="kind")`; the hand-maintained ordering invariant ceases to exist. Forward refs (`"JoinOp"`, `"Pipeline"`) resolve at the existing `model_rebuild()` calls.

- [ ] **Step 1: Spike (throwaway, 15 min).** In a scratch script, convert only `Pipeline.source` to `Annotated[Union[...], Field(discriminator="kind")]`, run `QueryIR.model_rebuild()`, round-trip one query per source class. Confirms pydantic accepts the forward-ref member and the `kind` literals as discriminator values. If the spike fails on a pydantic limitation, STOP and report — do not force it.
- [ ] **Step 2: Write the failing test** in `tests/ir/test_union_ordering.py`:

```python
def test_operator_payload_without_kind_is_rejected_with_a_discriminator_error():
    """Under left_to_right unions a kind-less payload was absorbed by shape
    (20 of 53 classes collapsed onto a structural twin). A discriminated
    union refuses it by name instead."""
    import pydantic, pytest
    payload = {"source": {"kind": "implicit", "span": {"text_start": 0, "width": 0}},
               "operators": [{"span": {"text_start": 0, "width": 0}}]}
    with pytest.raises(pydantic.ValidationError, match="kind"):
        Q.Pipeline.model_validate(payload)
```

- [ ] **Step 3:** Run → FAIL today (the payload validates by shape into a fields-less class).
- [ ] **Step 4:** Convert all four unions to `Field(discriminator="kind")` (keep member order for diff-friendliness; it no longer carries meaning). Delete the two ORDERING RULE comment blocks in `Pipeline` and the smaller ones on `SearchOp.tables`/`FindOp.tables`; replace each with one line: `# Discriminated on the kind literal; member order is not load-bearing.` Update `test_union_ordering.py`'s module docstring (it currently explains the ordering failure mode): the membership tests (`test_every_source_union_member_has_a_sample`, `test_every_operator_union_member_has_a_sample`) and both round-trip tests **stay** — they still catch a class missing from the union and a mis-declared Literal.
- [ ] **Step 5:** Full suite green; new test PASSES. CHANGELOG `### Breaking`: "IR unions (`Pipeline.source`/`.operators`, `SearchOp.tables`, `FindOp.tables`) are discriminated on `kind`. Hand-assembled JSON omitting `kind` no longer validates by shape — `model_dump` output always carried it, so only hand-built payloads are affected." Commit: `refactor(ir)!: discriminate the IR unions on kind instead of ordering`

---

## Task 5: Collapse the `KIND` ClassVar

**Files:** Modify `src/kustology/ir/llm_view.py:126`, every model in `src/kustology/ir/{query,expr}.py` (delete the `KIND: ClassVar[str] = "..."` line — ~100 sites, mechanical), `tests/ir/test_llm_view.py:296-336`

- [ ] **Step 1: Write the failing test** (replaces the two police tests with the one invariant that remains):

```python
def test_llm_view_kind_comes_from_the_model_field():
    """KIND ClassVars are gone; the view reads the pydantic discriminator
    default, so the two can never disagree (the drift IR-5 warned about)."""
    from kustology.ir import FilterOp
    assert not hasattr(FilterOp, "KIND")
    assert FilterOp.model_fields["kind"].default == "filter"
```

- [ ] **Step 2:** Run → FAIL (`hasattr` is True).
- [ ] **Step 3:** `llm_view.py:126`: `out = {"kind": type(node).model_fields["kind"].default if "kind" in type(node).model_fields else cls.__name__}`. Delete every `KIND: ClassVar[str]` line in `query.py`/`expr.py` (and the now-unused `ClassVar` imports if none remain). Delete `test_every_ir_model_class_has_kind_constant` and `test_kind_values_are_unique_per_class` (`test_llm_view.py:296,321`) — uniqueness is now enforced at model-build time by the discriminated unions from Task 4 (a duplicate discriminator value raises when the union is built), and presence is enforced by union membership.
- [ ] **Step 4:** Full suite green (grep first: `grep -rn "\.KIND\b" src tests scripts examples` — repoint any straggler to `model_fields["kind"].default`). Commit: `refactor(ir): derive kind from the pydantic field; drop the duplicate KIND ClassVar`

---

## Task 6: Collapse the hash-battery blob-guard (233 → 1)

**Files:** Modify `tests/ir/test_hash_battery.py:666-711`

- [ ] **Step 1:** Replace the `@pytest.mark.parametrize` on `test_no_battery_pair_discriminates_on_an_unmodelled_blob` with a single test that loops all unique queries itself, keeping the docstring verbatim and the per-query failure message:

```python
def test_no_battery_pair_discriminates_on_an_unmodelled_blob():
    """<keep the existing docstring verbatim — it records the incident>"""
    offenders: dict[str, list[str]] = {}
    for query in sorted({q for _, a, b in MUST_DIFFER + MUST_EQUAL + KNOWN_COLLISIONS for q in (a, b)}):
        ir = parse(query).to_ir()
        carriers = sorted(
            f"{type(n).__name__}({n.raw_text!r})" for n in walk(ir)
            if n is not ir and "raw_text" in type(n).model_fields and n.raw_text
        )
        if carriers:
            offenders[query] = carriers
    assert not offenders, (
        "these queries did not lower cleanly -- their nodes carry source "
        f"text into the digest: {offenders}"
    )
```

- [ ] **Step 2: Prove the collapsed guard still bites.** Temporarily add `("scan-canary", "T | scan declare (s:long) with (step s1: true => s = 1;)", "T | count")` to `MUST_DIFFER`, run the test → must FAIL naming the scan query (`ScanOp` carries `raw_text`). Remove the canary. This is the same break-discipline the KNOWN_COLLISIONS guard was verified with.
- [ ] **Step 3:** Full suite; reconcile collection delta (−232 collected). Commit: `test(ir): collapse the battery blob-guard to one looped check`

---

## Task 7: Prune the binder-oracle bound MATRIX leg

**Files:** Modify `tests/ir/test_binder_oracle.py` (bound leg at :364-368)

- [ ] **Step 1:** The module docstring (:20-23) concedes the bound MATRIX leg mostly "compares Microsoft's answer with itself." Keep the bound leg for a fixed representative subset that still exercises the ResultType capture/ordering path — select ~10 MATRIX ids spanning: one join, one union-conflict, one mv-expand `to typeof`, one wildcard project-keep, one parse (typed), one summarize multi-output, one datatable, one search, one evaluate, one getschema. Implement by filtering the parametrization: `BOUND_LEG_IDS = {…the ten ids…}` and `@pytest.mark.parametrize` over `[c for c in MATRIX if c.id in BOUND_LEG_IDS]` for the bound test only. The unbound leg, both corpus legs, both strict-xfail dicts, and `test_auto_names_do_not_depend_on_the_bind_state` are untouched.
- [ ] **Step 2:** Full suite; reconcile delta (−64 collected). Update the module docstring's description of the bound leg. Commit: `test(ir): bound oracle leg keeps ten representative cases`

---

## Task 8: Cut the CLI subprocess duplicates

**Files:** Modify `tests/test_cli.py`

- [ ] **Step 1:** Delete these tests — each is a subprocess re-run of the same-named (or noted) in-process test in `tests/test_cli_inprocess.py`; before deleting each, confirm the pair asserts the same contract and keep any that differs: `test_format_from_stdin`, `test_format_from_file`, `test_format_default_input_is_stdin`, `test_format_empty_input_is_not_an_error`, `test_format_refuses_input_it_could_not_parse`, `test_parse_refuses_input_it_could_not_parse` (= inproc `test_parse_refuses_input_it_could_not_parse`), `test_validate_clean_query_exits_0`, `test_validate_json_output_shape`, `test_validate_ignore_unknown_tables_flag`, `test_parse_ast_text_default`, `test_parse_ast_json_shape`, `test_parse_ir_json_requires_extras` (= inproc envelope + missing-extras tests), `test_parse_ir_text_default_format`.
- [ ] **Step 2:** KEEP as the end-to-end layer, and say so in the module docstring: `test_version_prints_runtime_version` (entry point launches), `test_validate_broken_query_exits_1` (exit 1 through the real binary), `test_missing_file_is_a_usage_error` (exit 2), `test_input_ceiling_counts_bytes_on_real_stdin` (needs the interpreter's real `sys.stdin.buffer`).
- [ ] **Step 3:** Full suite; reconcile delta. Commit: `test(cli): subprocess layer keeps entry-point smokes; in-process file owns the contract`

---

## Task 9: One home per hash pair (battery = registry)

**Files:** Modify `tests/ir/test_operator_params.py`, `tests/ir/test_sort_keys.py`, `tests/ir/test_sources.py`, `tests/ir/test_fork_branches.py`, `tests/ir/test_multi_statement.py`, `tests/ir/test_let_value_ref.py`, `tests/ir/test_ir_builder.py`, `tests/ir/test_hash_battery.py`

Rule for every deletion in this task: open the named battery id, confirm it exercises the same pair (same queries or same discriminating field), then delete the local copy. If the battery id does NOT cover it, **move** the pair into the battery instead of deleting.

- [ ] **Step 1: `test_operator_params.py`** — delete the `*_reaches_the_hash` / `*_hashes_as_*` / `*_hash_apart` tests whose pairs the battery already owns (audit-verified candidates, with battery line refs as of HEAD: typed-capture 383/384, mv-expand 328/492/509, parse 332-343/493-501, union 344-346/502, search 347-348/503, find 373/376-380/521, make-series 349-364, render 365/512, join/lookup 370-371/504-505, hint 516). KEEP every field-value test (`*_records_*`) and KEEP `test_mv_expand_modifiers_all_reach_the_hash` (pairwise-all — stronger than the battery's singles; optionally retire battery:329-331 in its favor).
- [ ] **Step 2: per-feature files** — `test_sort_keys.py`: first MOVE the one pair the battery lacks (`sort-bare-vs-asc`, :167) into `MUST_DIFFER`, then delete `test_ordering_modifiers_hash_apart` and `test_equivalent_orderings_hash_alike` (:184,192) and the REORDER duplicate rows (:407-408). `test_sources.py`: delete the datatable/ignoreFirstRecord/database-qualifier/wildcard hash assertions (battery 275/309-317/325). `test_fork_branches.py:122-127` and `test_multi_statement.py:77-90`: same treatment (battery 268-270, 388-389). `test_let_value_ref.py:131-134`: delete — the identical pair lives in battery:526 AND `test_ir_builder.py:1596-1610`; keep the `test_ir_builder.py` copy (it also pins structure) and the battery row.
- [ ] **Step 3: whole-test dups** — delete `test_hash_battery.py`'s `test_double_negation_collapses_at_a_bare_expr_root` (:639-663; `test_ir_builder.py:1421` is the stronger copy) and battery rows 191/417/418 (isnull family ⊂ `test_ir_builder.py:857` pairwise-complete).
- [ ] **Step 4:** Full suite after each file; reconcile total delta. Commit per file or one commit: `test(ir): the battery is the single registry for hash pairs`

---

## Task 10: Delete prose-pins, default-asserts, and the lockstep rule

**Files:** Modify `tests/ir/test_ir_builder.py`, `tests/ir/test_binder.py`, `tests/test_basic_parse.py`, `tests/test_docs_claims.py`, `tests/ir/test_schema_tags.py`, `tests/ir/test_operator_params.py`, `tests/ir/test_sources.py`, `tests/test_scripts.py`, `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, `AGENTS.md`

- [ ] **Step 1: prose-pinning tests.** `test_ir_builder.py`: in `test_both_schemaless_docstrings_describe_the_family_not_a_list` (:1905-1940) delete the docstring-content asserts (the `counts = {...}` spelled-out-word machinery), keep the behavioral `_SCHEMALESS_ARTIFACTS` asserts; in `test_the_schemaless_docstrings_do_not_claim_built_ins_resolve_only_when_bound` (:1980-2022) keep the GlobalState behavior asserts (:2003-2014), delete the two exact-sentence-absence asserts. `test_binder.py:1581-1583`: delete the `TabularSchema.__doc__` content asserts, keep the producer asserts. `test_basic_parse.py`: delete `test_dict_keys_are_raw_column_names_and_the_docstring_says_so`'s doc-content half and `test_schema_like_alias_no_longer_advertises_a_bare_string`'s `__doc__` grep (the `SchemaLike` annotation assert stays).
- [ ] **Step 2: docs counts.** `test_docs_claims.py`: delete the two count-regex asserts (`r"defines (\w+) jobs"`, `r"the other \*\*(\w+)\*\* have no local counterpart"`) and `test_architecture_states_the_real_corpus_split`'s exact-phrase pins; KEEP both list-sync mechanisms (job-membership, README↔examples). Edit `CONTRIBUTING.md:45-46` to drop the two numerals (describe the table instead) and `docs/ARCHITECTURE.md:40-42` to state the mechanism ("the extract script rewrites only the fixtures listed in `RELATIVE_PATHS`; the rest are hand-written") with no numbers.
- [ ] **Step 3: default-asserts.** Delete `test_operator_params.py::test_an_operator_with_no_parameters_has_no_hints` (:534), `test_ir_builder.py::test_pipeline_result_schema_field` (:364), `test_ir_builder.py::test_kustotype_has_tabular` (:328), `test_sources.py::test_unqualified_table_leaves_both_qualifiers_none` (:276), `test_scripts.py::test_verify_dll_tfm_pin_is_net6_0` (:121 — subsumed by :193).
- [ ] **Step 4: lockstep.** In `tests/ir/test_schema_tags.py` delete line 30 (`SEMANTIC_HASH_SCHEME.rsplit("v",1)[1] == IR_SCHEMA_VERSION.split(".")[1]`) — an invented coupling a legitimate IR-0.3/hash-v2 release would break. The two constant pins stay.
- [ ] **Step 5: the rule.** In `AGENTS.md` (the rule at :70 scoped to "a Microsoft data structure"), widen it: "Never write an exhaustive count or enumeration you do not derive — Microsoft's structures, this repo's own files, prose, docstrings, and comments alike. Derive it, or describe what qualifies and let the reader count."
- [ ] **Step 6:** Full suite; reconcile delta. Commit: `test: pin behavior, not prose — drop docstring greps, default asserts, the lockstep rule`

---

## Task 11: Small regression adds and stale-note fixes

**Files:** Modify `tests/ir/test_binder.py`, `src/kustology/ir/transforms.py` (comment only), `.superpowers/sdd/2026-08-20-pre-release-remediation/deferred-items.md` (one word; git-ignored, edit anyway for the next reader)

- [ ] **Step 1: partition+search scope test** (the three rows the final IR review measured; no test anywhere combines the two operators):

```python
@pytest.mark.parametrize("schema,query,expect", [
    ({"T": {"a": "long", "k": "string"}, "U": {"a": "long"}},
     "T | partition by k (search in (U) a > 1)", {"k": "T", "a": "U"}),
    ({"T": {"a": "long", "k": "string"}, "U": {"z": "long"}},
     "T | partition by k (search a > 1)", {"k": "T", "a": "T"}),
    ({"T": {"a": "long", "k": "string"}, "U": {"a": "long"}},
     "T | partition by k (search a > 1)", {"k": "T", "a": None}),  # ambiguous: correct answer is None
])
def test_search_inside_partition_resolves_scope(schema, query, expect):
    ir = parse(query).to_ir()
    SchemaAttacher(schema).enrich(ir)
    got = {c.name: c.table for c in find_all(ir, ColumnRef) if c.name in expect}
    assert got == expect
```

Run first → expected PASS (it pins current, verified-correct behavior; it exists because deferred item 9 alleged a regression here and nothing tested either way). If any row FAILS, stop and report — that would be a real finding.
- [ ] **Step 2:** `transforms.py` at the `_canonicalize_let_names` call (~:588): add the one missing ordering comment: `# Must run before _sort_commutative: renaming changes the JSON sort keys of operands containing LetValueRefs; sorted-then-renamed, two spellings of the same query order differently and split.`
- [ ] **Step 3:** `deferred-items.md` §A.1: "to typeof(...), limit" → "to typeof(...), limit AND with_itemindex=" (the file undercounts; the shipped docs are right).
- [ ] **Step 4:** Full suite. Commit: `test(ir): pin search-inside-partition scope; document the let-rename ordering`

---

## Task 12: Reconcile and close the books

**Files:** Modify `CHANGELOG.md` (if needed)

- [ ] **Step 1:** Full gate run: `pytest` (plain), `ruff check src tests scripts examples`, `mypy src`, `audit_syntax_kinds.py --check`, `python scripts/mine_corpus.py`, all `examples/*.py`.
- [ ] **Step 2:** Reconcile the total collected-count movement across Tasks 1–11 (`git log` the per-task deltas; they must sum to the observed before/after difference). Do not write the totals into any doc.
- [ ] **Step 3:** CHANGELOG `### Internal`: one paragraph, no counts: "The test suite was deduplicated against the hash battery as the single pair registry, and mechanically-parametrized guards were collapsed to looped equivalents; behavioral coverage is unchanged (the do-not-cut set from the 2026-08-23 audit was preserved verbatim)." Verify the `[0.2.0]` date is still the intended ship date.
- [ ] **Step 4:** Commit: `docs: close the 0.2.0 shore-up`

---

## Task 13: First real CI, merge, tag (maintainer gate)

**Files:** none (operations)

- [ ] **Step 1:** Push: `git push -u origin release/0.2.0-remediation`. **Pushing alone runs nothing** — `test.yml` triggers only on push/PR to `main`. Open a PR into `main` to get the full matrix (Windows, Linux, 3.10–3.13, dependency-review, verify-dll online leg, sbom, Codecov — all currently unexercised by anything, ever).
- [ ] **Step 2:** Watch the run. Known watch-item: the reusable-workflow concurrency groups (`release-${{ github.ref }}` vs `${{ github.workflow }}-${{ github.ref }}` — the surface review's D3) — if the called workflow queues indefinitely, rename `release.yml`'s group to `release-publish-${{ github.ref }}`. Fix any real matrix failures as new tasks (the local process found three CI-only defects by hand; expect the matrix to find something).
- [ ] **Step 3 (maintainer-only):** Verify the PyPI trusted publisher (repo + `release.yml` + environment `pypi` — the publish job passes no token), the GitHub `pypi` environment's required reviewer, and that `CHANGELOG.md`'s `[0.2.0]` date is the actual ship day.
- [ ] **Step 4:** Merge. Maintainer's standing preference is a local merge to `main` — the branch is a strict fast-forward of `main` (verified 2026-08-23; the duplicate-message pairs on main are empty merge bubbles, harmless) — but present the finish menu (merge locally / merge the PR / keep) before acting. After `main` is pushed and green: `git tag -a v0.2.0 -m "kustology 0.2.0" && git push origin v0.2.0` (the tag push triggers `release.yml`: tests → build → tag↔version guard → publish behind the `pypi` environment).

---

## Explicitly deferred to 0.3 (do NOT do in this plan)

- Reroute the dict-schema path through `build_global_state` + `Analyze` and retire binder.py's ~625-line per-operator mirror + `_auto_name` (~130 lines). Unblocked by the `SchemaAttacher` export removal (commit `8a20d96`); requires reworking `test_binder.py` expectations toward Microsoft's answers (the 11 strict xfails flip).
- Model the `LetFunction` body (audit item; highest-value post-tag change) and `EvaluateOp.declared_schema` — both disclosed and `KNOWN_COLLISIONS`-pinned; their fixes flip those rows to `MUST_DIFFER` and update every disclosure surface (grep the case id — the surfaces are more than three).
- Delete the `BinderEnricher` tombstone test (one-release guard).
- `AnyExpr` discriminated conversion (works as smart union today; revisit with the 0.3 model changes).

## Verification (end-to-end)

```bash
.venv/bin/python -m pytest                                   # all green (no extra -q!)
.venv/bin/ruff check src tests scripts examples && .venv/bin/mypy src
.venv/bin/python scripts/audit_syntax_kinds.py --check
.venv/bin/python scripts/mine_corpus.py
for f in examples/*.py; do .venv/bin/python "$f" >/dev/null || echo "FAILED $f"; done
# Spot checks that must hold at the end:
#  - parse('T | where a in ((datatable(x:string)["v"]))').to_ir() has a DataTableExpr, no UnknownExpr
#  - Pipeline.model_validate rejects a kind-less operator payload naming 'kind'
#  - hash digests of the Task 3 probe queries are byte-identical to their pre-refactor values
#  - the CI matrix on the PR is fully green before any merge or tag
```
