# Handoff: library-gaps follow-ups + stubbed-functionality sweep

**Date:** 2026-08-20
**Status:** plan input — not yet designed, not yet planned
**Predecessor work:** merged to `main` as `1fa3ab4..eb55f17` (10 commits)
**Next step:** brainstorm → spec → plan → execute, on a new branch

Read this cold. It assumes no context from the session that produced it.

---

## 1. Where this came from

A downstream consumer replacing regex-based KQL parsing reported four library
gaps and five API traps. Six were real; four were misreadings of the API. The
merged branch closed the real ones:

| Commit | Change |
|---|---|
| `7ef43f1` | Pin `InvariantCulture` at import; de-DE/fr-FR CI matrix |
| `98df348` | Export `iter_elements` (Tier 1) for unwrapping `SeparatedElement` |
| `8f38943` | `literal_kind` from the .NET node; invariant values; `LiteralExpr.ticks` |
| `1ba7a79` | `InvariantCulture` on the decimal/guid `ToString` fallback |
| `a8c241e` | Populate `LetBinding`; remove `category`; add `LetFunction` |
| `c3c12de` | `get_time_range()` → `find_time_expressions()` (+ deprecated alias) |
| `41d3c3e` | Trap docs, CHANGELOG, `IR_SCHEMA_VERSION` 0.2 / `kustology-sem-v2` |
| `b6c5af3` | Parenthesized and operator-rooted tabular `let` RHS; `SubqueryExpr` |
| `a9210ca` | Corpus coverage gates walk `let_bindings` |
| `eb55f17` | Culture, hash-divergence and binder-boundary doc corrections |

Design rationale lives in `docs/superpowers/specs/2026-08-20-library-gaps-design.md`.

**The defect that motivated everything (call it G1):** `LetBinding` declared
`rhs_expr`, `rhs_pipeline`, `inner_tables` and `inner_time_exprs`, plus a
seven-value `category` enum. The builder populated *none* of them — it hardcoded
`category="alias"` in a list comprehension. The fields read as implemented and
were permanently empty. A consumer could not tell from the API that
let-resolution was impossible.

**That pattern — declared surface with no implementation behind it — is what
this follow-up work is about.** Section 4 has verified evidence it recurs.

---

## 2. Four known items, carried forward

Each was found by review, adjudicated, and deliberately deferred rather than
rushed into an already-large branch.

### F1 — `SchemaAttacher` does not resolve columns inside pipeline-bearing expressions

**Severity: highest of the four. This is a live functional bug, not a gap.**

`SchemaAttacher._fill` (`src/kustology/ir/binder.py:360-375`) recurses over a
hand-maintained attribute list:

```python
for attr in ("left", "right", "operand", "target", "expression", "selector",
             "column", "low", "high"):
```

`pipeline` is absent, so column resolution never descends into `ToScalarExpr`,
`MaterializeExpr`, or the new `SubqueryExpr`. Verified on a schema-bound parse of
`SecurityEvent | where EventID > toscalar(SecurityEvent | summarize max(EventID)) | project Account`:

```
ColumnRef EventID   table=SecurityEvent     <- outside toscalar
ColumnRef EventID   table=None              <- inside toscalar(...)
ColumnRef Account   table=SecurityEvent
```

The same column in the same query gets inconsistent provenance, silently. Any
lineage analyzer built on `ColumnRef.table` is wrong inside those subtrees.

Related: `SchemaAttacher.enrich` (`binder.py:78-87`) walks only
`ir.main_pipeline`, so `LetBinding.rhs_pipeline.result_schema` is permanently
`None`. That boundary is currently *documented* (`src/kustology/ir/query.py:552-561`)
rather than fixed — the documentation was the deliberate stopgap.

Decide whether `enrich` should cover let pipelines, and whether `_fill` should
descend into `pipeline`. They are the same root cause; treat them together.

### F2 — Corpus gate walkers share the identical blind spot

`_walk_expr` in `tests/ir/test_complex_harness.py:70-78` and `walk_expr` in
`scripts/mine_corpus.py:71-78` use the *same* hardcoded attribute list as F1,
also omitting `pipeline`. So the gates that exist to catch builder coverage
regressions cannot see inside `ToScalarExpr` / `MaterializeExpr` / `SubqueryExpr`.

Currently harmless — a generic `walk()` cross-check shows all four such subtrees
in the bundled corpus are clean — but it is a hole in the branch's most valuable
structural fix. Roughly three lines each.

**Note the shared root cause across F1 and F2:** three separate hand-maintained
attribute lists that drift from the model as fields are added. `walk.py`'s
generic `walk()` / `find_all()` iterates `model_fields` and has no such problem.
Consider whether these three walkers should derive from `model_fields` or reuse
the generic traversal outright, rather than each being patched to add `pipeline`.

### F3 — The corpus gate no longer catches a regression of the parenthesized-`let` fix

With `SubqueryExpr` now modeled, removing the paren unwrap in
`_visit_let_statement` degrades a parenthesized tabular binding to
`rhs_expr=SubqueryExpr, inner_tables=[]` instead of `UnknownExpr` — which the
gate does not flag. The gate still catches operator-rooted regressions.

Coverage exists via four dedicated tests in `tests/ir/test_let_bindings.py`; this
is recorded so nobody treats the corpus gate as the sole safety net. Consider
whether the gate should assert on empty `inner_tables` for a binding whose RHS is
tabular-shaped.

### F4 — `externaldata` on a `let` RHS is classified as scalar

`let X = externaldata(...)` yields `rhs_expr=ExternalDataExpr`,
`rhs_pipeline=None`, `inner_tables=[]`, so `rhs_pipeline is not None` is a false
negative for "is this binding tabular."

The current behavior is defensible — routing it through `_visit_pipeline` would
manufacture an `UnknownSource` — but the reasoning exists nowhere in the code.
At minimum add a line to the `_is_tabular_let_rhs` docstring
(`src/kustology/ir/builder.py:170-176`) so it is not re-discovered as a bug.

---

## 3. The sweep (primary ask)

Find every remaining instance of two patterns. Both are "the API says it works,
the code never does it," which is precisely why tests and reviews miss them.

**Pattern A — declared but never populated (the G1 shape).**
Model fields, enum members, or documented behaviors that no code path ever
produces. G1 shipped in a public release and blocked a consumer's entire design.

**Pattern B — hand-maintained traversal lists that skip branches.**
Any walker enumerating attribute names or node kinds by hand. Each is a silent
coverage hole that grows every time the model gains a field. F1 and F2 are three
known instances; the sweep should establish whether there are more.

### Verified leads — already found, already confirmed

These came from a ~10 minute pass. **They are evidence the sweep is worth doing,
not the sweep itself.** Treat them as the starting set.

| Lead | Location | Finding |
|---|---|---|
| `QueryIR.parse_warnings` | `src/kustology/ir/query.py:593` | Declared `list[str] = []`. **No code anywhere populates it.** The only other reference is `tests/ir/test_llm_view.py:126` asserting it is absent from the LLM view. A consumer reading it for parse warnings gets silence, always. Textbook G1. |
| `Span.source_text` | `src/kustology/ir/spans.py:16` | Declared `str \| None = None`. Never populated by the builder; set only manually in two tests. `Span.text(raw)` already slices from raw text, so this is a dead parallel path. Decide: populate, or remove. |
| `SchemaAttacher._fill` | `src/kustology/ir/binder.py:363` | Pattern B — see F1. Live bug. |
| `_walk_expr` | `tests/ir/test_complex_harness.py:70` | Pattern B — see F2. |
| `walk_expr` | `scripts/mine_corpus.py:71` | Pattern B — see F2. |

Explicitly **not** G1 instances, checked and cleared: `Finding.rule_id` and
`Finding.extra` (`src/kustology/ir/analyzers.py`) are populated by analyzer
authors by design, as their docstring states.

### A detection method that worked

For Pattern A, this found `parse_warnings` and `source_text` in seconds — parse
field declarations out of the IR model modules, then check whether the name is
ever assigned or read across `builder.py`, `binder.py`, `_builder_helpers.py`,
`transforms.py`, `_normalize.py`:

```python
import re, pathlib
src = pathlib.Path("src/kustology/ir")
fields = {}
for f in ("expr.py", "query.py", "spans.py", "analyzers.py"):
    cur = None
    for line in (src / f).read_text().splitlines():
        m = re.match(r"class (\w+)\(", line)
        if m: cur = m.group(1)
        m2 = re.match(r"\s{4}(\w+):\s", line)
        if m2 and cur and not m2.group(1).isupper():
            fields.setdefault(m2.group(1), []).append(cur)
body = "\n".join((src / f).read_text() for f in
                 ("builder.py", "binder.py", "_builder_helpers.py",
                  "transforms.py", "_normalize.py"))
for name, owners in sorted(fields.items()):
    if name == "kind": continue
    if not re.search(rf"\b{name}\s*=", body) and not re.search(rf"\.{name}\b", body):
        print(name, sorted(set(owners)))
```

It is a starting filter, not an oracle — it cannot see fields populated through
`setattr`, `model_copy(update=...)`, or by callers rather than the builder. Verify
every hit by running the library, the way the leads above were confirmed.

Widen beyond the IR: Tier 1 (`src/kustology/*.py`, `src/kustology/utils/`) has
had no equivalent audit. `Literal[...]` enums whose members are never emitted are
the same defect class as an unpopulated field — `LetBinding.category` had six
unreachable members out of seven.

### Deliverable for the sweep

A triaged inventory, each entry marked **populate it** / **remove it** /
**document the boundary**. Removal is often right: `LetBinding.category` was
deleted rather than implemented, because nothing read it and it was polluting
`semantic_hash`. Dead surface that looks alive is worse than absent surface.

---

## 4. Context the next session needs

**Tiers.** Tier 1 (`src/kustology/*.py`, `src/kustology/utils/`) is a deliberately
minimal projection of Microsoft's parser — it must not reinterpret the tree, and
must not import from `src/kustology/ir/` at module scope. Tier 2
(`src/kustology/ir/`) is a pydantic IR behind the `[ir]` extra, pre-1.0, where
breaking changes are permitted when recorded in `CHANGELOG.md`.

**Version tags.** `IR_SCHEMA_VERSION` (`src/kustology/ir/__init__.py:19`, now
`"0.2"`) must be bumped on any breaking field-shape change, and
`SEMANTIC_HASH_SCHEME` (`src/kustology/ir/transforms.py:149`, now
`"kustology-sem-v2"`) in lockstep. The predecessor branch initially forgot both;
that omission would have silently invalidated consumers' stored hashes. If this
work removes or repopulates fields, bump them again.

**Testing.** `.venv/bin/python -m pytest` — 352 tests, currently green. The suite
must also pass under `LANG=de-DE` and `LANG=fr-FR`; a CI job enforces this. That
matrix exists because Microsoft's parser evaluates `LiteralValue` lazily on
property access, so ambient culture corrupts fractional numeric literals
(`1.5h` → 15 hours under de-DE). `.venv` runs Python 3.12 — pythonnet 3.0.5
rejects 3.14, which `uv` picks by default.

**Corpus.** `tests/fixtures/complex_queries/` holds 33 real Sentinel queries and
is the best available reality check. The predecessor branch's Critical defect
existed precisely because its spec enumerated node shapes from hand-written
examples instead of from this corpus. **Check new work against the corpus before
believing an enumeration is complete.**

**Lint.** CI runs `ruff check src tests scripts` — `examples/` is out of scope and
carries 6 pre-existing `I001` findings. Adding `examples` to the lint job is a
reasonable small item to fold in.

---

## 5. Suggested shape

F1 is a live correctness bug; the sweep is discovery whose scope is unknown until
run. Those want different treatment, and the sweep may well change what F1's fix
should look like — if several walkers share one root cause, fix the cause once.

A workable order: run the sweep first and triage its inventory, then design the
walker fix (F1 + F2) against everything it found, then fold in F3 and F4 as small
items. Confirm this ordering during brainstorming rather than assuming it.
