# 0.2 Collision Closures Implementation Plan (former 0.3 candidates)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull the four parked 0.3 candidates into 0.2 before the tag: model `evaluate`'s output-schema clause and `let`-function bodies (closing all five documented `KNOWN_COLLISIONS`), eliminate the `$left`/`$right` sentinel from `ColumnRef.table` and give `find` its seeding branch (both provenance-only), and reclassify the `ColumnN` grouping-name divergence as deliberate.

**Architecture:** Two hash-moving modeling tasks first (evaluate clause — small; let-function body — the big one), each flipping its `KNOWN_COLLISIONS` rows to `MUST_DIFFER` and updating every disclosure surface the battery's failure message enumerates. Then the two hash-silent provenance changes (`table` is volatile; `join_side` is the hashed side-carrier and is untouched). Then the docs-only reclassification. Same branch discipline as the reroute plan: suite green at every commit, one task per review gate, CI via PR, finish menu, maintainer tag gate.

**Tech Stack:** Python 3.10–3.13, pythonnet + Kusto.Language 12.3.2 (bundled DLL), pydantic v2, pytest, ruff, mypy, GitHub Actions.

**Spec:** This session's three research maps (let-function/.NET surface + disclosure inventory; dollar-side/find provenance mechanics; Microsoft's default-name counter semantics) plus the SDD ledger of the reroute plan. Maintainer decisions (2026-08-24): all four candidates land pre-0.2; the `ColumnN` divergence is a **deliberate divergence** (bind-stable, edit-stable names beat Microsoft's query-global positional counter for hashing) — documented, not ported.

## Global Constraints

- All work on a new branch `release/0.2.0-collision-closures` off `main` (1185158). `IR_SCHEMA_VERSION` stays `"0.2"`; `SEMANTIC_HASH_SCHEME` stays `"kustology-sem-v2"` (nothing released; one bump per release, already done).
- Python is always `.venv/bin/python`; pytest with NO extra `-q`; .NET members confirmed via `[m for m in dir(x) if m[:1].isupper()]` before use.
- Gates after every task: full pytest, `.venv/bin/ruff check src tests scripts examples`, `.venv/bin/mypy src`, `.venv/bin/python scripts/audit_syntax_kinds.py --check`; `scripts/mine_corpus.py` + examples on modeling tasks.
- Suite green at every commit; tests of changed behavior are reworked in the same commit; collected-count deltas reconciled per task.
- **Hash-movement budget:** digests may move ONLY for (a) queries carrying an `evaluate` schema clause, (b) queries with `let`-function declarations (bodies, parameter types, defaults now hashed — including the two corpus fixtures with that shape), and (c) any function-body-nested `let` whose top-level hoisting changes per Task 2's design. Tasks 3 and 4 are hash-silent (`table`, `result_type` are volatile; `join_side` untouched). The bind-invariance oracle (`test_auto_names_do_not_depend_on_the_bind_state`) and `test_semantic_hash_bind_invariance.py` must stay green untouched throughout.
- A closed collision updates EVERY disclosure surface — the battery's `test_known_collision` failure message enumerates them: `compute_semantic_hash`'s docstring, the dropping IR node's docstring, README (`semantic_hash` section; Tier 2 boundary section for the let rows), CHANGELOG [0.2.0]'s survivor list, and the matching `KNOWN_MERGES` row in `examples/semantic_hash_demo.py` (which `tests/test_examples.py` runs — a closed gap fails there too until the row moves to the demo's must-split list).
- Never write a count you did not derive; CHANGELOG entries 1–3 lines; [0.1.0] immutable; conventional commits with the two Claude trailers:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_016EdpkDN9Um9Hxo6mW5FBDu`.

## Context

The binder-mirror reroute shipped with four disclosed residuals parked for 0.3. The maintainer pulled them forward: pre-release is the only time the two hash-moving ones (the largest disclosed collisions in the digest — an entire `let`-function body is invisible today) are free to fix, and the two provenance items make `ColumnRef.table`'s contract honest (`a real table, a scope name, or None — never a sentinel`) and complete (`find` was the fourth source-bringing operator with no branch). Research killed the fifth candidate's premise: Microsoft's `ColumnN` default names come from a query-global, scope-sensitive counter that cannot be reproduced bind-stably and would make digests more brittle — so that divergence graduates from "known limitation" to documented design choice, widened to the `extend`/`project` instance that was never disclosed.

---

## Task 0: Branch and archive

- [ ] **Step 1:** `git checkout -b release/0.2.0-collision-closures main` (main at 1185158, clean).
- [ ] **Step 2:** `cp ~/.claude/plans/atomic-foraging-quail.md docs/superpowers/plans/2026-08-24-collision-closures.md`
- [ ] **Step 3:** Commit: `docs: archive the collision-closures plan`

---

## Task 1: Model `evaluate`'s output-schema clause

**Files:**
- Modify: `src/kustology/ir/query.py` (`EvaluateOp` :714-730), `src/kustology/ir/builder.py` (EvaluateOperator branch :1292-1297), `src/kustology/ir/transforms.py` (compute_semantic_hash docstring bullet :517-527), `README.md` (:513-518), `CHANGELOG.md` (survivor list ~:123), `examples/semantic_hash_demo.py` (the two evaluate `KNOWN_MERGES` rows), `tests/ir/test_hash_battery.py`, `tests/ir/test_binder_oracle.py` (one MATRIX row)
- Test: `tests/ir/test_operator_params.py` (field-value tests)

**Interfaces — Produces:** `EvaluateOp.declared_schema: list[tuple[str, str]] | None = None` (ordered `(name, type)` pairs; `None` = no clause) and `EvaluateOp.declared_schema_star: bool = False` (`: (*, x:string)` — append semantics). Neither is volatile, so both reach the digest.

- [ ] **Step 1: Write the failing tests** in `tests/ir/test_operator_params.py`:

```python
def test_evaluate_declared_schema_is_modeled():
    """`: (x:string)` is the operator's declared result shape; it was dropped
    (documented KNOWN_COLLISIONS) and now rides the IR in clause order."""
    (op,) = _ir("T | evaluate bag_unpack(d) : (y:long, z:datetime)").main_pipeline.operators
    assert op.declared_schema == [("y", "long"), ("z", "datetime")]
    assert op.declared_schema_star is False

def test_evaluate_schema_star_means_append():
    (op,) = _ir("T | evaluate bag_unpack(d) : (*, x:string)").main_pipeline.operators
    assert op.declared_schema == [("x", "string")]
    assert op.declared_schema_star is True

def test_evaluate_without_a_clause_stays_none():
    (op,) = _ir("T | evaluate bag_unpack(d)").main_pipeline.operators
    assert op.declared_schema is None
    assert op.declared_schema_star is False

def test_evaluate_bare_star_is_empty_with_the_flag():
    (op,) = _ir("T | evaluate bag_unpack(d) : (*)").main_pipeline.operators
    assert op.declared_schema == []
    assert op.declared_schema_star is True
```

(All four shapes were probed live against the DLL: `read_row_schema(clause)` returns the pairs via its fallthrough; `clause.Schema.AsteriskToken.Width > 0` discriminates the star; `: (*)` yields `([], True)`.)

- [ ] **Step 2:** Run → FAIL (no such fields). Probe the .NET members first (`n.Schema`, `n.Schema.Schema.AsteriskToken`) per the member-probe convention.
- [ ] **Step 3: Implement.** `query.py`: add both fields; rewrite `EvaluateOp`'s docstring — the clause IS modeled now; keep the sentence explaining the binder derives `result_schema` from it; document `None` vs `[]` and the star. `builder.py` EvaluateOperator branch: `schema_clause = getattr(n, "Schema", None)`; when present, `declared = read_row_schema(schema_clause)` (`_builder_helpers.py:97-142` — its fallthrough already reaches the `EvaluateRowSchema` through the clause's `.Schema`); star from `getattr(getattr(schema_clause, "Schema", None), "AsteriskToken", None)` with `.Width > 0`. Defensive: a clause whose inner `Schema` is missing (error recovery) stays `None`.
- [ ] **Step 4: Battery flips.** Move `evaluate-schema-clause-columns` and `evaluate-schema-clause-vs-absent` from `KNOWN_COLLISIONS` to `MUST_DIFFER` verbatim (same 3-tuple shape). Add to `MUST_DIFFER`: `("evaluate-schema-star", "T | evaluate bag_unpack(d) : (*, x:string)", "T | evaluate bag_unpack(d) : (x:string)")`. Add to `MUST_EQUAL`: `("evaluate-schema-whitespace", "T | evaluate bag_unpack(d) : (x:string)", "T | evaluate bag_unpack(d) :  ( x : string )")`. Update the `KNOWN_COLLISIONS` preamble if it now mis-describes membership. Run the battery + blob guard.
- [ ] **Step 5: Disclosure surfaces** (the failure-message list): `transforms.py` :517-527 — delete the evaluate bullet from `compute_semantic_hash`'s collision list; `README.md` :513-518 — the "**`evaluate` discards its output-schema clause**" passage becomes past-tense/deleted (fold the fact that the clause is now hashed into the surrounding section's flow); `CHANGELOG.md` survivor line ~:123 — remove the evaluate clause from the survivor list and add `### Fixed`: "`evaluate`'s output-schema clause (`: (x:string)`, and the `*` append form) is now modeled on `EvaluateOp.declared_schema` and reaches `semantic_hash`; the two documented clause collisions are closed." `examples/semantic_hash_demo.py` — move both evaluate rows from `KNOWN_MERGES` to `SPLITS` with reworded labels ("evaluate's declared output schema splits" / "declared schema vs none splits"). Run `tests/test_examples.py`.
- [ ] **Step 6: Oracle row.** Add MATRIX id `evaluate-declared-schema` (`"T | evaluate bag_unpack(d) : (x:string)"`) — the dict leg verifies Microsoft's `result_schema` for the clause shape end-to-end (the binder builds it from the clause; `Binder_NodeBinder.cs:3605-3650`).
- [ ] **Step 7:** Full gates; reconcile counts. Commit: `feat(ir)!: model evaluate's output-schema clause (declared_schema)`

---

## Task 2: Model `let`-function bodies

**Files:**
- Modify: `src/kustology/ir/query.py` (new `LetFunctionParameter`; `LetFunction` replaced :1138-1167; `LetBinding` inner-fields comment; `model_rebuild` block :1265), `src/kustology/ir/__init__.py` (export `LetFunctionParameter`), `src/kustology/ir/builder.py` (imports :168-172; `__init__` :434-439; `build_from_code` :519-545; `_visit_let_statement` :595-605; `_visit_function_declaration` :617-632; `_visit_expr` NameReference :1665; `_visit_table_ref` :796; `_collect_inner_*` docstrings :312-338), `src/kustology/ir/transforms.py` (`_canonicalize_let_names` :314-358 + `_LET_NAME_MODELS` comment; `compute_semantic_hash` docstring), `README.md`, `CHANGELOG.md`, `examples/semantic_hash_demo.py`
- Test: `tests/ir/test_let_bindings.py`, `tests/ir/test_hash_battery.py`, `tests/ir/test_llm_view.py` :421-434, `tests/ir/test_ir_roundtrip.py`

**Interfaces — Produces** (design verified by live DLL probes; `.NET FunctionBody = {Statements: only Let|QueryParameters; Expression?: PipeExpression|scalar}`):

```python
class LetFunctionParameter(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["let_function_parameter"] = "let_function_parameter"
    decl: TypedNameDecl          # reuses the existing name:type node — no new Expr subclass
    default: AnyExpr | None = None   # grammar-restricted to literals; presence AND value hash

class LetFunction(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["let_function"] = "let_function"
    is_view: bool = False        # `view` changes wildcard-union membership — semantic
    parameters: list[LetFunctionParameter] = []
    body_lets: list["LetBinding"] = []          # scoped HERE, no longer hoisted
    body_pipeline: Pipeline | None = None       # tabular tail (same dispatch rule as let RHS)
    body_expr: AnyExpr | None = None            # scalar tail
    body_span: Span                             # still volatile
```

Full docstrings per the design (residual disclosures live on the model: call sites are not expanded; `declare query_parameters` in a body is dropped; parameter references are textual — a parameter shadows an outer `let`, so a shadowed name lowers as `ColumnRef`/`TableRef`, never `LetValueRef`/`LetRef`).

- [ ] **Step 1: Write the failing tests.** In `tests/ir/test_let_bindings.py`, the headline RED pair plus the edge list: scalar-vs-tabular body exclusivity; nested-let-in-body (`len(ir.let_bindings) == 1` — no double-hoist — AND `body_lets[0].name == "z"` AND the body's `take z` is a `LetValueRef`); parameter shadowing in expr position AND source position, with shadow-restore after the declaration; body-let non-leak; default `LiteralExpr(value=3)`; `is_view` True/False; `S(5)` call site stays `FuncCallSource` (no expansion); `invoke f(1)` unaffected; empty body `let f = (x:long);` builds with both body fields `None`. Run → FAIL on the new fields.
- [ ] **Step 2: Models** (above) + exports + `LetFunctionParameter.model_rebuild()` beside the existing rebuild block. The canonical-coverage guards pick the new model up once exported (plain BaseModel → no `canonical()` branch, no `AnyExpr` membership).
- [ ] **Step 3: Builder.** (a) Import `FunctionDeclaration` from `Kusto.Language.Syntax`; add `self._param_names: set[str] = set()` reset alongside `_let_names`. (b) **Hoisting filter** in `build_from_code`: skip any `LetStatement` (and defensively any expression statement) whose `GetFirstAncestor[FunctionDeclaration]()` is not None — `declare pattern` bodies are `FunctionBody`s owned by `PatternMatch`, NOT `FunctionDeclaration`, so their disclosed hoisting stays true. (c) Extract the tabular/scalar RHS classification into one `_is_tabular_rhs(expr)` predicate shared by the let RHS and the body tail. (d) Replace `_visit_function_declaration` per the design: parameters via `_visit_expr(NameAndType)` (rides the existing `TypedNameDecl` branch), defaults via `DefaultValue.Value`, `is_view` from `ViewKeyword.Width > 0`, then visit the body under a **parameter shadow** — `self._param_names |= {p.decl.name}` and a `set(self._let_names)` snapshot, both restored in `finally`; body lets visit via `_visit_let_statement` and register into `_let_names` for the rest of the body; tail dispatches through `_is_tabular_rhs` (paren-unwrapped). `body_span=to_span(body)` keeps the pinned 15/20 offsets. (e) Shadow consultation: `_visit_expr`'s NameReference branch and `_visit_table_ref` add `and name not in self._param_names` to their `_let_names` checks. (f) `_visit_let_statement`'s FunctionDeclaration branch now also populates `inner_tables`/`inner_time_exprs` from the `LetFunction` (the `_collect_*` helpers are `find_all`-based and accept any model; update their docstrings + `LetBinding`'s comment).
- [ ] **Step 4: Scope-aware `_canonicalize_let_names`** (implement now, not a disclosed deferral): one query-global `$let<i>` counter, scope-ordered traversal; a function body opens a child scope seeded from its declaration site (outer refs rename through it, inner declarations extend it body-locally, parameters never appear in it); declarations renamed only by the scope walker, never through a reference map; a shared `seen` id-set handles the `inner_time_exprs` aliasing (the `_sort_commutative` ordering caveat, extended). Update the `_LET_NAME_MODELS` comment, the docstring, and the rename-before-sort ordering comment.
- [ ] **Step 5: Battery.** Move `let-function-body`, `let-function-parameter-type`, `let-function-parameter-default` from `KNOWN_COLLISIONS` to `MUST_DIFFER` verbatim — **`KNOWN_COLLISIONS` is now empty: keep the list, the test, and a rewritten comment block** (what qualifies; currently empty; pytest's empty-parametrize skip is the standing signal). New `MUST_DIFFER`: `let-function-view-vs-plain`, `let-function-default-value` (=3 vs =4), `let-function-scalar-body`, `let-function-body-nested-let-value`. New `MUST_EQUAL`: `let-function-body-whitespace`, `let-function-body-comment`, `let-function-param-whitespace`, `let-function-body-commutative` (commutative sort composes into bodies), `let-function-outer-let-rename-through-body`, `let-function-inner-let-rename` (requires Step 4). Rework the MUST_DIFFER comment at :225-228 and the KNOWN_COLLISIONS header. Blob guard green (bodies use only modeled operators). Round-trip: add a tabular-body-with-nested-let-and-default query, a scalar-body one, a view one to `QUERIES`.
- [ ] **Step 6: Tripwires.** `test_function_binding_populates_rhs_function` → assert `[p.decl.name ...] == ["x","y"]`, types, `body_expr` is a `BinOp`. `test_a_function_body_is_reachable_from_tier_1_and_not_from_tier_2` → inverts by design; rename to `..._from_both_tiers`, Tier 2 now finds `SecurityEvent`/`Account`/`Computer`, field-set pin becomes the seven new fields, docstring rewritten. `test_llm_view` :421-434 → parameters render as dicts (`fn["parameters"][0]["decl"]["name"] == "x"`); keep `"body_span" not in fn`; pin `body_expr`'s presence in the view. The comment-before-function hash-invariance test (test_ir_builder.py:1315) stays green UNCHANGED — the canary.
- [ ] **Step 7: Disclosure sweep** (battery failure-message list): `compute_semantic_hash` docstring — delete the let-function bullet + the tier-disagreement paragraph; fold the residuals (call-site non-expansion; `declare query_parameters` in bodies) into the statements paragraph whose `declare pattern` example stays true; update the let-renaming bullet to the scope-ordered wording. README — "Where Tier 2 stops" (:76-101) inverts (the code sample now finds the body's tables/columns; residual = call-site non-expansion); `let` table row :42; "What `semantic_hash` deliberately ignores" :520-527 — delete the let-function bullet. CHANGELOG — remove the Known-limitations let-function line; `### Added`/`### Fixed` entries (1–3 lines each): body/params/defaults/`view` modeled and hashed; digests move for every function-declaring query and for function-nested lets (hoist change); scheme tag unchanged under the unreleased-window rule. Demo — move the three rows to `SPLITS`, reworded. Adjacent touch-ups: `_sort_commutative` caveat, `build_from_code`'s function-body comment, battery module docstring.
- [ ] **Step 8:** Full gates incl. `mine_corpus.py` + examples + `test_semantic_hash_bind_invariance.py` (the shadow sets are text-only — bind invariance must hold). Record which corpus fixtures' digests moved (expect exactly the let-function-declaring ones). Reconcile counts. Commit: `feat(ir)!: model let-function bodies, parameters, defaults, and view`

### Reference implementation (verified by live DLL probes; adapt names to the file, never the reverse)

**`_visit_function_declaration` replacement:**

```python
    def _visit_function_declaration(self, node: Any) -> LetFunction:
        params: list[LetFunctionParameter] = []
        outer = getattr(node, "Parameters", None)
        inner = getattr(outer, "Parameters", None) if outer is not None else None
        if inner is not None:
            for param in _iter_elements(inner):
                name_and_type = getattr(param, "NameAndType", None)
                if name_and_type is None:  # pragma: no cover — defensive
                    continue
                decl = self._visit_expr(name_and_type)
                if not isinstance(decl, TypedNameDecl):  # pragma: no cover — defensive
                    decl = TypedNameDecl(
                        name=visit_name(name_and_type), declared_type="unknown",
                        span=to_span(name_and_type),
                    )
                default_clause = getattr(param, "DefaultValue", None)
                default_value = (
                    getattr(default_clause, "Value", None)
                    if default_clause is not None else None
                )
                params.append(LetFunctionParameter(
                    decl=decl,
                    default=self._visit_expr(default_value)
                    if default_value is not None else None,
                ))

        view_kw = getattr(node, "ViewKeyword", None)
        is_view = view_kw is not None and getattr(view_kw, "Width", 0) > 0

        body = getattr(node, "Body", None)
        if body is None:  # pragma: no cover — defensive
            return LetFunction(
                is_view=is_view, parameters=params, body_span=to_span(node),
            )

        body_lets: list[LetBinding] = []
        body_pipeline: Pipeline | None = None
        body_expr: AnyExpr | None = None

        saved_params = self._param_names
        saved_lets = set(self._let_names)
        self._param_names = saved_params | {p.decl.name for p in params}
        try:
            stmts = getattr(body, "Statements", None)
            if stmts is not None:
                for st in _iter_elements(stmts):
                    if str(type(st).__name__) == "LetStatement":
                        b = self._visit_let_statement(st)
                        body_lets.append(b)
                        self._let_names.add(b.name)
                    # QueryParametersStatement / recovery placeholders: dropped.
            tail = getattr(body, "Expression", None)
            while tail is not None and str(type(tail).__name__) == "ParenthesizedExpression":
                tail = getattr(tail, "Expression", None)
            if tail is not None:
                if self._is_tabular_rhs(tail):
                    body_pipeline = self._visit_pipeline(tail)
                else:
                    body_expr = self._visit_expr(tail)
        finally:
            self._param_names = saved_params
            self._let_names = saved_lets

        return LetFunction(
            is_view=is_view, parameters=params, body_lets=body_lets,
            body_pipeline=body_pipeline, body_expr=body_expr,
            body_span=to_span(body),
        )
```

(Write real docstrings in the file's register — the plan omits them here for space; the model docstrings' content requirements are in the Interfaces block. `body_span=to_span(body)` keeps the pinned 15/20 offsets in test_ir_builder.py:1315-1329.)

**Scope-aware `_canonicalize_let_names` replacement** (needs `import itertools`, `LetFunction` import, `_models_in` from `.walk`):

```python
def _canonicalize_let_names(ir: QueryIR) -> None:
    if not ir.let_bindings:
        return
    counter = itertools.count()
    seen: set[int] = set()

    def rename(node: BaseModel, visible: dict[str, str]) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, LetFunction):
            for p in node.parameters:
                rename(p, visible)
            body_visible = dict(visible)
            canon_scope(node.body_lets, body_visible)
            for sub in (node.body_pipeline, node.body_expr):
                if sub is not None:
                    rename(sub, body_visible)
            return
        if isinstance(node, _LET_NAME_MODELS) and not isinstance(node, LetBinding):
            object.__setattr__(node, "name", visible.get(node.name, node.name))
        for field_name in type(node).model_fields:
            for item in _models_in(getattr(node, field_name)):
                rename(item, visible)

    def canon_scope(bindings: list[LetBinding], visible: dict[str, str]) -> None:
        for binding in bindings:
            written = binding.name
            rename(binding, visible)  # RHS resolves pre-declaration
            canonical = f"$let{next(counter)}"
            visible[written] = canonical
            object.__setattr__(binding, "name", canonical)

    visible: dict[str, str] = {}
    canon_scope(ir.let_bindings, visible)
    for pipeline in (ir.main_pipeline, *ir.additional_pipelines):
        rename(pipeline, visible)
```

(One query-global counter is load-bearing — per-scope numbering would let a nested body's `$let0` collide with its encloser's. Declarations are renamed only by `canon_scope`; the shared `seen` set handles `inner_time_exprs` aliasing through the owning field, which is declared first. Write the full docstring covering scope semantics per the Interfaces block.)

---

## Task 3: `ColumnRef.table` never carries a sentinel; `find` gets its seeding branch

**Files:**
- Modify: `src/kustology/ir/builder.py` (:1710-1713 — the `$left`/`$right` PathExpression branch), `src/kustology/ir/binder.py` (`_side_marker` :49-60 DELETE; `_fill` ColumnRef leg :726-763; `_flatten_side` :86-116; `_walk_operator_provenance` — new `FindOp` branch beside `SearchOp`'s :657-666; class docstring :193-235), `src/kustology/ir/expr.py` (`ColumnRef.table`/`join_side` comments :102-113), `src/kustology/ir/transforms.py` (:195-202 comment), `examples/find_all_demo.py` (:20-25, :74-79), `ARCHITECTURE.md` (:100-106), `CHANGELOG.md`
- Test: `tests/ir/test_binder.py`

**Interfaces — Produces:** `ColumnRef.table` is a real table, a scope name (`let` alias), or `None` — never `"$left"`/`"$right"`; `join_side` (hashed, builder-set on every `$side` reference even unbound) is the sole side carrier. `FindOp` seeds one `ScopeEntry` per table (appended, like `search`), fills `predicate` AND `project`, and both `find`'s and `search`'s branches resolve `LetRef` tables through `_let_schemas`.

Both changes are hash-silent: `table` and `result_type` are volatile (`transforms.py:215-218`); `join_side` is untouched.

- [ ] **Step 1: Write the failing tests** in `tests/ir/test_binder.py`:

```python
def test_an_unresolvable_dollar_side_answers_none_and_keeps_its_side():
    """The sentinel never reaches `.table` anymore: an unresolvable side is
    honestly None, and `join_side` -- set by the builder even unbound -- is
    the side's carrier."""
    ir = _dict_path("L | join (datatable(z:long)[1]) on $left.k == $right.z")
    _left, right = _on_refs(ir)
    assert right.table is None
    assert right.join_side == "right"

def test_an_unenriched_dollar_ref_has_no_sentinel_either():
    (ref,) = [c for c in find_all(parse("L | join (R) on $left.k == $right.b").to_ir(), ColumnRef) if c.name == "k"]
    assert ref.table is None
    assert ref.join_side == "left"

def test_find_seeds_its_tables_like_search():
    """`find in (T) where a > 1`'s predicate resolves against T -- the fourth
    source-bringing operator finally has its branch."""
    ir = parse("find in (T) where a > 1").to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"T"}

def test_find_project_columns_resolve_too():
    ir = parse("find in (T) where a > 1 project a").to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.project[0], ColumnRef)} == {"T"}

def test_a_find_or_search_over_a_let_alias_resolves_through_it():
    q = "let A = T | where a > 1; find in (A) where a > 5"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"A"}
```

(Adapt the last test's expected alias-vs-table answer to what `_let_schemas` threading actually yields — the contract is "resolves through the alias", matching `_source_entry`'s `LetRef` behavior; probe first, pin the real answer, and add the `search in (A)` twin.)

- [ ] **Step 2:** Run → the first two FAIL (sentinel present), the find tests FAIL (`table is None`).
- [ ] **Step 3: Implement the sentinel elimination.** `builder.py` :1710-1713: write `table=None if lhs_name in ("$left", "$right") else lhs_name` (the `T.X` bound-symbol case keeps the real name), `join_side` unchanged. `binder.py`: delete `_side_marker`; `_fill`'s side detection becomes `side = expr.join_side`; the unresolved case needs NO write (`.table` is already `None`). `_flatten_side`: drop the `and entry.columns` gate — a right-hand entry with a known table and unknown columns still names the side (the map's identified resolve-harder win). Update the class docstring: the narrowings framing (":211-213 — answers None") is now true without exception; `$left`/`$right` bullets reworded.
- [ ] **Step 4: Implement the find branch** beside `SearchOp`'s, with the two conscious upgrades: fill `op.predicate` AND every `op.project` element after seeding; resolve names through `self._let_schemas` for `LetRef` entries (and make `search`'s branch do the same — the sibling gap the map identified). Shape:

```python
        if isinstance(op, (SearchOp, FindOp)):
            refs = op.tables
            names = [t.name for t in refs if isinstance(t, TableRef)]
            aliases = [t.name for t in refs if isinstance(t, LetRef)]
            if not names and not aliases:
                names = list(self.schemas)
            scope.extend(
                ScopeEntry(table=n, columns=dict(self._table_schema(n)))
                for n in names
            )
            scope.extend(
                ScopeEntry(table=a, columns=dict(self._let_schemas.get(a, {})))
                for a in aliases
            )
            self._fill(op.predicate, scope)
            for expr in getattr(op, "project", []):
                self._fill(expr, scope)
            return
```

(Verify field availability — `SearchOp` has no `project`; the `getattr` default covers it. Keep the append-not-replace semantics and the post-seed fill ordering. Update the docstring's three-family framing to four.)

- [ ] **Step 5: Rework the pinned tests.** `test_an_unresolvable_dollar_side_keeps_its_marker` is replaced by Step 1's successor. Every other on-clause test (:756-827) must stay green unchanged — they assert resolved tables, not sentinels. `test_join_side_is_recorded_separately_from_resolved_table` stays green (its docstring's "binder overwrites the sentinel" clause needs a one-line update — the sentinel no longer exists to overwrite).
- [ ] **Step 6: Prose sweep** (the sentinel is now unrepresentable in `.table`): `expr.py` :102-113 both comments; `transforms.py` :195-202 (the `join_side` rationale — reword to past tense/current mechanism); `examples/find_all_demo.py` :20-25 and :74-79; `binder.py` :676-678 (`_resolve_side` docstring) and :193-194; `ARCHITECTURE.md` :100-106 (`find` joins the structural-branch list; delete the "don't treat it as a model to copy" parenthetical); class-docstring narrowings: delete the `find` known-gap bullet, keep the search-append narrowing bullet now shared by find.
- [ ] **Step 7:** Hash-silence check: `compute_semantic_hash` byte-identical before/after enrich for a join query and a find query (spot assertion in the test file or verified in the report). Full gates; reconcile counts. CHANGELOG `### Changed` (1–2 lines): "`ColumnRef.table` never carries the `$left`/`$right` sentinel — an unresolvable join side is `None` and `join_side` carries the side; `find` now seeds its tables for provenance like `search`." Commit: `fix(ir): honest join-side provenance; find seeds its tables`

---

## Task 4: The `ColumnN` divergence is deliberate — reclassify and widen

**Files:** `src/kustology/ir/builder.py` (`_auto_name` grouping bullet :2022-2032), `CHANGELOG.md` (Known-limitations line :127), `tests/ir/test_ir_builder.py` (grouping parity test docstring :2074-2079)

- [ ] **Step 1:** `_auto_name`'s grouping bullet: reframe from "Pre-existing, disclosed rather than fixed here" to the design decision — Microsoft's `ColumnN` comes from a query-global, scope-sensitive counter (skips names bindable in scope, tables included; resets in function bodies; carries across statements), which cannot be reproduced bind-stably and would make `Assignment.name` — a hashed field — shift under unrelated edits; deterministic first-bare-column naming is the deliberate choice. Note the same class applies wherever `_visit_assignment` names an unnamed call (`extend tolower(s)` → `tolower_s`).
- [ ] **Step 2:** `CHANGELOG.md` :127: rewrite the Known-limitations line as a deliberate divergence, widened to the extend/project instance (1–3 lines, no function list recount — point at `_auto_name`'s docstring for the current list).
- [ ] **Step 3:** The grouping parity test's exclusion paragraph (:2074-2079): the `ResultNameKind == None` skip is no longer "a known divergence this probe should not paper over" — it is out of scope because those names are deliberately ours; reword.
- [ ] **Step 4:** Full suite (`test_docs_claims.py` guards). Commit: `docs(ir): ColumnN default names are a deliberate divergence, disclosed as such`

---

## Task 5: Reconcile and close

- [ ] **Step 1:** Full gate run (pytest, ruff, mypy, audit, mine_corpus, examples). Run the battery and `test_semantic_hash_bind_invariance.py` explicitly; confirm digest movement matches the budget: derive the list of corpus fixtures whose hashes moved (expect exactly the let-function-declaration fixtures, plus any Task 2 hoisting-affected shape; zero from Tasks 3–4) and record it in the ledger.
- [ ] **Step 2:** `KNOWN_COLLISIONS` should now be EMPTY or hold only what remains disclosed — verify the preamble, the demo, README's survivor framing, and `compute_semantic_hash`'s docstring tell one consistent story; CHANGELOG [0.2.0] reads as one release (the survivor list may now be empty — say so rather than deleting the section's frame).
- [ ] **Step 3:** Verify the `[0.2.0]` date equals the intended ship day; update if the calendar moved. Commit anything outstanding: `docs: close the collision closures`

---

## Task 6: CI, merge, tag (maintainer gate)

- [ ] **Step 1:** Push; open a PR into `main`; the full matrix must be green.
- [ ] **Step 2:** Fix real matrix failures as new tasks.
- [ ] **Step 3:** Present the finish menu (maintainer preference: local merge; still present options). After merge + green main CI:
- [ ] **Step 4 (maintainer-only):** PyPI trusted publisher + `pypi` environment reviewer; CHANGELOG date = actual ship day; `git tag -a v0.2.0 -m "kustology 0.2.0" && git push origin v0.2.0`.

---

## Explicitly out of scope

- `BinderEnricher` tombstone test (one-release guard — it guards THIS release; delete in 0.3).
- `AnyExpr` discriminated conversion (works as smart union; revisit in 0.3).
- A faithful `ColumnN` port (rejected by maintainer ruling — see Task 4).
- `mv-apply`/`parse-kv`/`getschema`/`consume` modifier gaps in the demo's KNOWN_MERGES (disclosed, unchanged this release).

## Verification (end-to-end)

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests scripts examples && .venv/bin/mypy src
.venv/bin/python scripts/audit_syntax_kinds.py --check
.venv/bin/python scripts/mine_corpus.py
for f in examples/*.py; do .venv/bin/python "$f" >/dev/null || echo "FAILED $f"; done
# Spot checks that must hold at the end:
#  - the five former KNOWN_COLLISIONS ids are MUST_DIFFER rows and pass
#  - "let S = (w:int) { A | where x > w }; S(5)" vs the w:long spelling: digests differ
#  - evaluate ": (x:string)" vs ": (y:long, z:datetime)" vs absent: three digests
#  - no ColumnRef.table anywhere equals "$left" or "$right" (corpus sweep)
#  - find in (T) where-predicate columns carry table "T" via the dict path
#  - bind-invariance suites green; no corpus digest moved outside the recorded budget list
#  - the CI matrix on the PR is fully green before any merge or tag
```
