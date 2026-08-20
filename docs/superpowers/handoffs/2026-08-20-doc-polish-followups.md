# Handoff: documentation polish deferred from the 0.2.0 release

**Date:** 2026-08-20
**Status:** ready to execute — scoped, verified, no design work needed
**Predecessor:** the 0.2.0 release prep, `main` at `9d911cf`
**Estimated size:** one short branch; items are independent

Read this cold. It assumes no context from the session that produced it.

---

## 1. Where this came from

Preparing the 0.2.0 release, a documentation audit ran against the release's
consumer-visible changes. It found four things genuinely broken, all fixed in
`9d911cf` ("docs: fix stale references and document the compatibility
contract"):

- `CONTRIBUTING.md` / `README.md` told contributors to run
  `ruff check src tests scripts`, but CI had started linting `examples/` too.
- `examples/llm_view.py` described `SetMembership` as *synthesizing* its `op`
  in the LLM view — false since `SetMembership.op` became a real field.
- `ARCHITECTURE.md` named `IRBuilder._HANDLED_OPERATOR_KINDS` /
  `_HANDLED_EXPR_KINDS`; the real attributes are public, without the
  underscore, so the checklist raised `AttributeError` if followed verbatim.
- Two `ARCHITECTURE.md` cross-references pointed at a README "Stability
  policy" section that had never existed. Fixed by adding a real
  **Versioning and stability** section to the README, which also closed the
  gap that neither IR version tag was documented for consumers anywhere.

**The five items below were found by the same audit and deliberately left
undone**, because they are improvements rather than corrections and the
release did not depend on them. Nothing here is broken; everything here is
a place where the docs teach less than they could, in ways this release
proved matter.

Everything below was verified against `main` at `9d911cf`. Line numbers are
from that commit and may drift — grep for the quoted text.

---

## 2. The items

### D1 — `ARCHITECTURE.md`: the "A new IR expression" checklist omits three steps

Location: `ARCHITECTURE.md`, section **"Where to add things"**, the
**"A new IR expression"** list (~line 68). It currently reads:

```
1. Add the model in `src/kustology/ir/expr.py` (or reuse `LiteralExpr` if
   it's just a new `literal_kind`).
2. Add its kind to `IRBuilder.HANDLED_EXPR_KINDS`.
3. Add a dispatch branch in `IRBuilder._visit_expr()`.
4. Regenerate the baseline.
```

Three steps are missing, each of which the 0.2.0 work proved load-bearing:

**a. Add the class to the `AnyExpr` union AND to `ir/__init__.py`'s
`__all__`.** Omitting either produces surface that looks implemented and is
not. Both failure directions actually shipped in v0.1.0: `LetRef` was
exported and declared in the `Pipeline.source` union but constructed
nowhere, and `MaterializeExpr` was constructed at a site nothing could
reach. See `docs/superpowers/reports/2026-08-20-stub-sweep.md`.

**b. Add a render branch to `canonical()` in `src/kustology/ir/_normalize.py`,
which backs `Expr.canonical_form`.** There is a silent fallthrough to a bare
`"?"` at the end of that function. Commit `c1eaab7` fixed **11** `Expr` types
that had fallen through it, with the result that `-X > 1`, `D.a == 1` and
`toscalar(...) > 1` all rendered as the identical string `"? > 1"`.

**c. Apply the lossy-lowering check.** If the new node is reachable from more
than one KQL construct, it needs a field naming which one. This is a
`semantic_hash` correctness requirement, not style — it is why
`SetMembership.op` and `Exists.op` exist. The rule is written up in
`AGENTS.md` under "Lossy lowering: a populated node can still lose
information"; the checklist should point at it.

### D2 — `ARCHITECTURE.md`: the "A new tabular operator" checklist never mentions `SchemaAttacher`

Location: same section, the **"A new tabular operator"** list (~line 48).

It walks through the model, `HANDLED_OPERATOR_KINDS`, the `_visit_operator`
dispatch branch and the baseline — and stops. It never mentions that the new
operator also needs consideration in `src/kustology/ir/binder.py`.

This is precisely the omission that produced the bug 0.2.0 fixed as "column
provenance is resolved everywhere, not in 17 of 53 operators": operators were
added over time with no prompt to think about scope, so `SchemaAttacher`
silently covered a third of them.

The fix is a step reading roughly: *if the operator reshapes its output
schema, add a scope rule in `SchemaAttacher._walk_operator`; if it passes its
input schema through, the generic fallback already handles it — either way,
update the coverage list in `SchemaAttacher`'s class docstring.* That
docstring (`src/kustology/ir/binder.py`, class `SchemaAttacher`) now
enumerates which operators reshape scope versus pass it through, and names
the ones whose downstream scope is knowingly stale.

### D3 — `examples/`: two demos predate `LetRef` and teach the old contract

**`examples/walk_ir.py`** — this is the file whose whole purpose is to teach
`isinstance` dispatch over the IR, and its `walk()` has two gaps, both of
which are shapes 0.2.0 now produces:

- The `QueryIR` branch walks only `node.main_pipeline`, never
  `node.let_bindings`. On a `let`-bearing query the bindings print nothing at
  all — even though `rhs_pipeline` is now populated *and* schema-enriched.
- There is no `LetRef` branch, so a `let`-aliased source falls through to the
  generic `type(node).__name__` fallback and prints the bare string `LetRef`
  with no name, next to a `TableRef` branch that prints `Source: {name}`.

Its current query contains no `let`, so nothing is wrong today.

**`examples/find_all_demo.py`** — its query has no `let` either, so
`find_all(ir, TableRef)` never exercises the new `TableRef` / `LetRef` split.
The comment above that call reads "Every table referenced anywhere in the
query — including inside join right-side sub-pipelines", which is still
literally true for this query but is exactly the sentence a reader would
generalize wrongly now that aliases are excluded.

Adding a `let` to each query, a `LetRef` branch to `walk_ir.py`, a
`let_bindings` loop to its `QueryIR` branch, and a `find_all(ir, LetRef)`
line to `find_all_demo.py` would demo the headline capability and correct the
generalization in one edit each.

Note `tests/test_examples.py` smoke-tests every `examples/*.py` by importing
it and calling `main()`, so both must keep a zero-arg `main()` and their SPDX
header.

### D4 — `ARCHITECTURE.md`: the layout map omits 6 of 14 `ir/` modules

Location: `ARCHITECTURE.md`, the repository-layout block (~line 14).

It lists `builder.py`, `query.py`, `expr.py`, `binder.py`, `walk.py`,
`types.py`, `spans.py`. Missing: **`transforms.py`** (semantic hash,
canonicalization, `SEMANTIC_HASH_SCHEME`), **`llm_view.py`**
(`to_llm_dict`), **`analyzers.py`** (the `Finding` vocabulary),
`_normalize.py`, `_builder_helpers.py`, `_guard.py`. `utils/` is collapsed to
one line despite holding `analysis.py`, `walker.py` and `schema_state.py`.

Pre-existing — all six existed at the v0.1.0 commit `8ad2b28`, so this is not
merge damage. It matters because `transforms.py` and `llm_view.py` back
public API the README's tier table advertises (`model_dump_json` /
`to_llm_dict`), so the map currently points readers away from two modules
they will need.

### D5 — `ARCHITECTURE.md`: `--update` relies on argparse prefix matching (minor)

`ARCHITECTURE.md` step 5 of the operator checklist says:

```
5. Regenerate the baseline: `python scripts/audit_syntax_kinds.py --update`.
```

The script defines `--check` and `--update-baseline`, and no `--update`.
**It does currently work** — argparse's `allow_abbrev` accepts `--update` as
an unambiguous prefix, and it was confirmed to rewrite the baseline with
identical content. So this is not broken today. But it breaks silently the
moment a second `--update*` flag is added, and it teaches a flag that does
not appear in `--help`. Prefer the canonical `--update-baseline`, which is
what `AGENTS.md` already uses.

---

## 3. Context for whoever picks this up

**Do not update** `docs/superpowers/handoffs/` or `docs/superpowers/specs/`
or `docs/superpowers/plans/` — those are dated historical records, correct as
of their date. `docs/superpowers/reports/` is the same, except that the
stub-sweep report carries live **FIXED** / **REMOVED** disposition markers
which should stay current.

**Verification** for anything touched here:

```bash
.venv/bin/python -m pytest                       # 437 tests
.venv/bin/python -m ruff check src tests scripts examples
.venv/bin/python -m mypy src
for f in examples/*.py; do .venv/bin/python "$f" >/dev/null || echo "FAILED $f"; done
```

`.venv` is Python 3.12 — pythonnet 3.0.5 rejects the 3.14 that `uv` picks by
default.

**Release state at the time of writing:** `pyproject.toml` is at `0.2.0`,
`CHANGELOG.md` has a `## [0.2.0] — 2026-08-20` section plus an empty
`## [Unreleased]` above it, and `IR_SCHEMA_VERSION` / `SEMANTIC_HASH_SCHEME`
are `0.2` / `kustology-sem-v2`. **The `v0.2.0` tag had not been pushed.** If
it still has not been, these items can ride along in the release; if it has,
they belong under `[Unreleased]` and must not move either schema tag — see
the cadence rule in `CONTRIBUTING.md` step 6.
