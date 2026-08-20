# Design: culture pinning, literal fidelity, and let-binding population

Date: 2026-08-20
Status: awaiting review

Origin: a downstream consumer replacing regex-based KQL parsing reported four
library gaps and five API traps. Investigation confirmed some, refuted others,
and surfaced one bug neither side had identified. This document covers the
verified defects only.

## Decisions taken before design

- All items ship in **one release**.
- The culture pin has **no opt-out**. An escape hatch would let a host
  reintroduce silent value corruption, which is worse than the co-tenancy
  cost it would avoid.

## Verified findings

### C1 — Ambient .NET culture corrupts fractional timespan literals (Tier 1)

`KustoCode.Parse` yields a `TimeSpan` whose value depends on the process
culture. The decimal point is read as a group separator under `de-DE` and
fails to parse under `fr-FR`:

| source | en-US / ja-JP | de-DE | fr-FR |
|---|---|---|---|
| `1.5h` | `01:30:00` | `15:00:00` (10x) | `00:00:00` |
| `0.5h` | `00:30:00` | `05:00:00` (10x) | `00:00:00` |
| `2.25s` | `00:00:02.25` | `00:03:45` (100x) | `00:00:00` |
| `15m`, `1h` | correct | correct | correct |

Integer-valued literals are unaffected, which is why the defect is invisible
today: the suite contains no fractional-duration literal anywhere in `tests/`,
`tests/fixtures/`, or `examples/`, and all 289 tests pass under `LANG=de-DE`
while `1.5h` parses as fifteen hours.

**`LiteralValue` is evaluated lazily on property access, not bound at parse.**
Parsing under invariant culture and reading under `de-DE` still returns the
corrupted value; parsing under `de-DE` and reading under invariant returns the
correct one. The culture that matters is the one live when the *consumer*
touches `.LiteralValue` — inside caller code, arbitrarily far from `parse()`,
possibly on another thread. A pin scoped around kustology's own entry points
would therefore fix nothing. A process-wide pin at import is the only
construction that closes the defect.

### C2 — Tier 2 renders datetime literals through ambient culture

Distinct from C1 and not fixed by it. `builder.py:845` calls `.ToString()` on
the .NET value with no format specifier. Datetime `Ticks` are correct under
every culture; only the rendered string varies:

    en-US  '1/1/2024 12:00:00 AM'      (U+202F narrow no-break space)
    de-DE  '01.01.2024 00:00:00'
    ja-JP  '2024/01/01 0:00:00'

`_normalize.canonical()` folds that string into `semantic_hash`, so the same
query hashes differently on different machines — breaking the documented
contract that semantically identical queries collide. Pinning the culture
makes the hash stable but still emits `01/01/2024 00:00:00`, which is neither
ISO 8601 nor round-trippable. C2 needs an explicit format specifier
independent of the pin.

### G4 — `literal_kind` is guessed from the Python type

`builder.py:842-851` discards the `.Kind` the .NET node already carries and
re-infers from the Python type of `LiteralValue`:

| KQL | .NET `node.Kind` | current `literal_kind` | correct |
|---|---|---|---|
| `"abc"` | StringLiteralExpression | `string` | `string` |
| `true` | BooleanLiteralExpression | `bool` | `bool` |
| `42` | LongLiteralExpression | `int` | `long` |
| `int(5)` | IntLiteralExpression | `int` | `int` |
| `1.5` | RealLiteralExpression | `int` | `real` |
| `decimal(1.5)` | DecimalLiteralExpression | `int` | `decimal` (new) |
| `datetime(...)` | DateTimeLiteralExpression | `string` | `datetime` |
| `15m` | TimespanLiteralExpression | `string` | `timespan` |
| `guid(...)` | GuidLiteralExpression | `string` | `guid` |
| `int(null)` | IntLiteralExpression | `string` | `null` |

Six of the ten declared `literal_kind` values are unreachable. `decimal` is
absent from the model entirely despite `KustoType.DECIMAL` existing.

### G1 — `LetBinding` is unpopulated

`builder.py:231-238` builds every binding in one list comprehension with
`category="alias"` hardcoded. No dispatch branch exists. Four optional fields
(`rhs_expr`, `rhs_pipeline`, `inner_tables`, `inner_time_exprs`) and six of
seven `category` values are unreachable. `LetStatement.Expression` carries
what is needed:

    let lookback = 15m                      -> TimespanLiteralExpression
    let Base = SecurityEvent | where X > 1  -> PipeExpression
    let m = toscalar(T | summarize max(X))  -> ToScalarExpression
    let f = (x:int) { x + 1 }               -> FunctionDeclaration

The last shape fits none of the seven declared categories — one of
several signals that the taxonomy was speculative. See the design
section for its removal.

## Refuted — no change

- **G2** (`ReferencedSymbol` always None): binding is all-or-nothing on
  `parse()`'s `schema` argument. `parse(q)` calls `KustoCode.Parse`, which
  never analyses, so every symbol is None including built-ins. With a schema,
  all resolve. Correct behavior; document the rule.
- **G5** (`-1h` wraps a positive literal): `UnaryMinusExpression` over
  `TimespanLiteralExpression(+1h)` is correct KQL grammar. Tier 1 must not
  alter it; Tier 2 already models it as `UnaryOp(op='-', operand=...)`.
- **No generic `BinaryExpression`**: the .NET class is generic for all six
  comparisons; only `.Kind` varies. Documentation gap.
- **`node.Kind` as a raw .NET enum**: correct for a thin projection. Document
  that `str()` is mandatory because format specs raise `TypeError`.

## Design

### 1. Culture pin (Tier 1)

In `bridge.py`, immediately after CLR initialization and assembly load:

    CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture
    Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture

`DefaultThreadCurrentCulture` covers threads spawned after import; the
explicit current-thread assignment covers the importing thread, which
`DefaultThreadCurrentCulture` does not retroactively affect. Both verified.

`CurrentUICulture` is deliberately left alone — it selects exception and
diagnostic message language, not value parsing, and Kusto's diagnostics are
English regardless.

Documented in `bridge.py` and the README as a deliberate process-global
effect of importing kustology.

### 2. Literal fidelity (Tier 2)

Replace the type-guessing branch with dispatch on `str(node.Kind)`. Add
`"decimal"` to the `literal_kind` Literal in `expr.py`.

`value` becomes invariant and round-trippable:

- datetime — ISO 8601 round-trip (`"o"`), e.g. `2024-01-01T00:00:00.0000000`
- timespan — invariant `TimeSpan` form, tick-precise
  (`01:30:00`, `00:00:00.0000002`)
- numeric, bool, string, guid — unchanged apart from correct `literal_kind`

**Decided.** `LiteralExpr` gains an optional `ticks: int | None`,
populated for `datetime` and `timespan` only, leaving `value`
human-readable for the LLM view. Consumers needing exact sub-second
reconstruction use `ticks / 10` -> microseconds -> `timedelta` without
string-parsing `value`. Rejected alternative: making `value` itself the
tick count — exact, but it destroys readability in `to_llm_dict`.

### 3. `LetBinding` population (Tier 2)

Replace the list comprehension with a dispatch branch reading
`ls.Expression`. Tabular right-hand sides (`PipeExpression`, bare table
`NameReference` resolving to a table, `MaterializeExpression`) populate
`rhs_pipeline` via `_visit_pipeline`; scalar ones populate `rhs_expr` via
`_visit_expr`. `inner_tables` and `inner_time_exprs` are collected from the
populated pipeline.

`category` is **removed**. Rationale:

- Nothing reads it. Across `src/`, `tests/`, `examples/`, and `scripts/`
  the only occurrence is `builder.py:235` writing the hardcoded `"alias"`.
  No analyzer, test, `llm_view` branch, or `_normalize` use consumes it.
- It carries zero information today — every binding in every query gets
  the same value.
- It would degrade `semantic_hash`. `transforms.py:181` dumps
  `let_bindings` into the hash payload and `_VOLATILE_FIELDS` does not
  strip `category`, so a derived label would make the hash sensitive to
  our classification choices rather than to query semantics.
- Everything it would encode is recoverable from the fields this design
  populates: tabular vs scalar is which of `rhs_pipeline` / `rhs_expr` is
  set; time-scalar is `literal_kind == "timespan"` or `is_time_func`;
  `scalar_subquery` is a `ToScalarExpr`; `alias` is a pipeline whose
  source is a bare `TableRef` with no operators.

If a compact label is later wanted, it belongs as a derived `@property`
following the `Expr.canonical_form` precedent — recomputed from the RHS so
it cannot drift, and excluded from `model_dump()` so it stays out of the
hash. Not built now: no consumer has asked for it.

**Function-valued bindings.** `let f = (x:int) { x + 1 }` yields a
`FunctionDeclaration`, which is neither an expression nor a pipeline and so
cannot ride on `rhs_expr` or `rhs_pipeline`. Leaving all three fields
`None` would reproduce the "looks implemented, isn't" trap this section
exists to remove. `LetBinding` therefore gains
`rhs_function: LetFunction | None`, where `LetFunction` is deliberately
minimal:

    class LetFunction(BaseModel):
        parameters: list[str]   # parameter names, in declaration order
        body_span: Span         # body is not modeled in this release

(the function's name is already `LetBinding.name`)

Modeling function bodies (parameter types, defaults, tabular vs scalar
bodies, call-site expansion) is a separate feature and out of scope. The
explicit `rhs_function` makes the boundary legible rather than silent.

### 4. Export the `SeparatedElement` unwrap helper (Tier 1)

`_iter_elements` (`ir/builder.py:147`, ~25 internal uses) moves to
`utils/walker.py` and is exported from the package root. It must live at
Tier 1, not Tier 2, because Tier 1 consumers walking the .NET tree are
exactly who needs it, and it must not require the `[ir]` extra.

It iterates Microsoft's own shape without reinterpreting it, so it does not
weaken the minimal-projection contract. It handles both `SyntaxList` and
`SeparatedSyntaxList` — the former has no `.Element` — so callers need not
know which a given property returns.

Name: `iter_elements`. `ir.builder` imports it from its new home; the
private alias is retained internally for one release to keep the IR diff
small.

### 5. Rename `get_time_range()` (Tier 1)

The function returns every time-related expression with spans, in source
order. That is a discovery list, not a range, and the name has caused a
downstream consumer to use it as a lookback extractor and get wrong answers.

Rename to `find_time_expressions()`. `get_time_range()` remains as a
deprecated alias emitting `DeprecationWarning`, on both the module function
and `KustoQuery`. Behavior is unchanged in both.

A semantic lookback extractor is explicitly **out of scope**: resolving an
effective time window requires let-resolution, `TimeGenerated` awareness, and
negation handling, which is analysis rather than projection and belongs in a
consumer or a separate analyzer package.

### 6. Documentation

A trap list in the README covering, in the corrected form established here:
`str(node.Kind)` is mandatory; list-valued properties yield
`SeparatedElement` wrappers whose `str()` looks right while `.Kind` checks
silently fail; `BinaryExpression` is one generic class, so branch on `.Kind`
or `.Operator` rather than on type; `between`/`!between` put the bounds in `.Right` as an
`ExpressionCouple` with `.First`/`.Second`, and both share the class
`BetweenExpression` — the negation exists only in `.Kind`
(`NotBetweenExpression`), so branching on class silently inverts the
predicate; use
`TimeSpan.Ticks` rather than the lossy `TotalSeconds`; symbols require a
schema and there is no partial binding; the culture pin is process-global.

## Testing

Every fix lands test-first, with the test demonstrated failing before the fix.

- **C1** — fractional-duration fixtures (`1.5h`, `0.5h`, `2.25s`, `1.5d`)
  asserting exact `Ticks`. A CI matrix adds `LANG=de-DE` and `LANG=fr-FR`
  jobs across the whole suite. Verification that the guard works: with the
  pin reverted, the de-DE job must fail. A suite that stays green without the
  pin is only testing integer literals.
- **C2 / literals** — a table-driven test over one query per literal kind,
  asserting `literal_kind` and `value`. A locale-invariance test asserting
  `semantic_hash` is identical under all three locales.
- **G1** — per-shape assertions that the right RHS field is populated
  for scalar, tabular, `toscalar`, and function bindings, plus
  `inner_tables` on a tabular binding. A test asserts `category` is
  gone from `model_dump()`.
- **unwrap** — assertions over both `SyntaxList` and `SeparatedSyntaxList`
  properties.
- **rename** — the alias warns and returns identical results.

`scripts/audit_syntax_kinds.py --update-baseline` is re-run once the literal
dispatch changes, since it reads `_HANDLED_EXPR_KINDS` statically.

## Compatibility

Tier 1 gains the culture pin (corrects previously wrong values), one new
export, and one deprecation. No Tier 1 signature changes.

Tier 2 breaks, as its pre-1.0 policy permits, and each needs a CHANGELOG
entry:

- `literal_kind` returns different values for real, long, decimal, datetime,
  timespan, guid, and null literals.
- `LiteralExpr.value` changes format for datetime and timespan.
- `semantic_hash` changes for any query containing a datetime, timespan, or
  real literal. This is the point — it was previously machine-dependent.
- `LetBinding.category` is removed. Stored IR JSON containing it
  will fail to deserialize under `extra="forbid"`.
- `LiteralExpr` gains `ticks` (additive).
- `LetBinding` gains `rhs_function`; new `LetFunction` model (additive).
