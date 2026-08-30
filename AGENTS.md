# AI Agent Notes: Kustology

Non-obvious technical context for agents modifying this repository. Read
before changing CLR interop, the AST analysis layer, or the bundled DLL.

## Documentation style

These rules govern all prose and comments in the repository — README and
docs, docstrings, and inline comments. Version-history records
(`CHANGELOG.md`) and this file's postmortem notes are the exceptions: they
exist to carry history.

- **Narrative prose and docstrings follow the Microsoft Writing Style
  Guide.** Active voice, present tense, second person for instructions.
  Sentence-case headings. Contractions are fine. No filler ("please",
  "simply", "just", "easy"). Spell out Latin abbreviations — "for example"
  and "that is", not "e.g." and "i.e.". Oxford comma.
- **Python docstrings follow PEP 257, written in Microsoft style.** Triple
  double quotes. The summary line is one imperative sentence ending in a
  period ("Return the bound tree.", not "Returns the bound tree."). A
  one-liner keeps its closing quotes on the same line; a multi-line
  docstring puts a blank line after the summary and the closing quotes on
  their own line. Document parameters, returns, and raises only when the
  signature doesn't already say it.
- **Inline comments are terse and action-oriented.** Comment only
  non-obvious or complex load-bearing code and critical decision points.
  State the constraint or the decision, not what the next line does. A
  comment that restates the code is noise — delete it.
- **Prose is greenfield: explain what is, not what was.** Outside the
  exceptions above, never write "previously", "now", "no longer", "used
  to", or any reference to removed code, past defects, or the change that
  produced the current behavior. Rationale is welcome when phrased as
  present-tense fact ("newlines are safe to fold because a KQL string
  literal cannot contain a raw one"), not as narration of a fix. History
  belongs in git and the changelog.

Two standing rules elsewhere in this file are part of the same discipline:
never write a count you did not derive, and keep changelog entries to one
to three lines.

### Write plainly; the style guide alone will not do it

The Microsoft guide governs voice, tense, and terminology. It says nothing
about rhetoric, so prose can satisfy every rule above and still read as
machine-written. These are the patterns that produce that effect. Remove
them on sight:

- **Antithesis as a closer.** "X, not Y", "X rather than Y" used to land a
  point: "emits a versioned envelope, not a bare dump". State the fact and
  stop. If a contrast genuinely helps, make it two plain sentences.
- **Aphorisms.** A sentence-final judgment that sounds quotable — "that is
  the direction to err in", "the invariant is worth more than the shadow
  case". Give the concrete consequence or cut the sentence.
- **Triadic rhythm.** Three clauses in a row for cadence: "the guard
  declines, the fallback value stands, and the surface reads as
  implemented". Say what happens once.
- **Em-dash density.** At most one per paragraph, preferably none. A
  period, a comma, or parentheses nearly always works.
- **Self-justification.** "deliberate", "deliberately", "on purpose",
  "intentionally". Give the reason or say nothing; the adjective adds no
  information.
- **Personification.** Code does not decline, admit, lie, or want. Describe
  what it does.
- **Portentous framing.** "These are the places where that shape surprises
  people." Delete and start with the content.
- **Bold lead-in chains.** Consecutive paragraphs each opening with a bold
  sentence. Use real headings, which are also linkable, or a table.

Aim for short sentences carrying one idea each, second person for
instructions, and the fact first with the reason after it only when a
reader needs the reason to act.

## .NET runtime and pythonnet interop

### Use the public `KustoCodeService` for formatting
`Kusto.Language.Editor.KustoFormatter` is `internal` — not part of the public
API. The supported public path is
`Kusto.Language.Editor.KustoCodeService.GetFormattedText()`, which returns a
`FormattedText` with a `.Text` property. Use this; do not reflect into
`KustoFormatter`.

```python
from Kusto.Language.Editor import KustoCodeService
text = KustoCodeService(query).GetFormattedText().Text
```

### CoreCLR initialization on macOS / Linux
- pythonnet defaults to Mono off-Windows; we always call
  `pythonnet.load("coreclr")` first.
- On Homebrew macOS the runtime layout differs from Microsoft's installer:
  `libhostfxr.dylib` lives under `<dotnet>/libexec/host/fxr/`, not
  `<dotnet>/bin/host/fxr/`. `clr_loader.find_dotnet_root()` falls back to the
  parent of `which dotnet`, which is wrong for Homebrew.
- `bridge.py` runs a cascade: honor `DOTNET_ROOT`, try the default load, then
  probe `/opt/homebrew/opt/dotnet/libexec`,
  `/usr/local/opt/dotnet/libexec`, `/usr/share/dotnet`,
  `/usr/local/share/dotnet`, `~/.dotnet`. Probing the `opt` symlink (not the
  `Cellar/X.Y.Z/` path) keeps detection stable across `brew upgrade`.

### pythonnet member lookup is exact, case-sensitive, and silent
`getattr(node, "Uris", None)` on a node whose member is `URIs` returns `None`.
No exception, no warning. So the guard around it declines, the field it would
have populated keeps its declared default, and the surface reads as implemented
forever. Four separate defects shipped this way — `Uris` for `URIs`,
`IsNullable` and `Underlying` (properties on *no* type in the assembly), and
`Keys.Count` where `Keys` is a `RowSchema` exposing `Columns`.

An adjacent trap: an **empty .NET `IReadOnlyList` is truthy** in Python, so
`if not code.GetSyntaxDiagnostics():` never fires and every query looks like a
parse error. Use `.Count`.

**Direct attribute access is the opposite failure and is also scanned.**
`n.ValueExpression` on a node without that member *raises* `AttributeError`,
out of whatever public API the caller invoked — loud rather than silent, but
just as shippable: two `IRBuilder` branches crashed `to_ir()` on valid KQL
(`T | top-hitters 5 of a by b`, `T | __partitionby a (take 1)`) while both
kinds sat in `HANDLED_OPERATOR_KINDS` claiming to be modelled.

`tests/test_reflection_audit.py` covers **both** styles: it asserts every
PascalCase member name in `src/` that is passed to `getattr`/`hasattr` **or
read as a direct attribute** resolves somewhere in `Kusto.Language`. Two
limits. The check is per name, not per type, so the `Keys.Count` shape still
needs a value assertion on a real parse. And the direct-access scan drops any
pure attribute chain rooted at a name the file imports — it cannot tell a
namespace segment from a member read — so neither `Default` nor
`WithDatabase` is checked in `GlobalState.Default.WithDatabase(db)`. A call
breaks the chain, but only downstream of itself: in
`TableSymbol.From(cols).WithName(name)`, `From` is **dropped** and only
`WithName` is checked. So a *partly* covered expression is the normal case,
not the exception — do not read a green audit as "every member on that line
exists". Before adding a probe of either style, confirm the member exists:

```python
[m for m in dir(node) if m[:1].isupper()]
```

### Never write a count you did not derive
Never write an exhaustive count or enumeration you do not derive — Microsoft's structures, this repo's own files, prose, docstrings, and comments alike. Derive it, or describe what qualifies and let the reader count.

`IRBuilder.build`'s docstring said `GlobalState.Default` describes "built-in
functions, aggregates and plug-ins, **and nothing else**". Four rewrites later
it was still wrong, most recently by omitting `Operators`, of which the
default state has 55. Every attempt failed the same way: the sentence
enumerated a set nobody had counted, and each fix added one more member
instead of dropping the claim.

The default state's *populated* side is not enumerable by reading our source —
it is whatever Microsoft shipped. Its *empty* side is, and is short, and is
what a caller needs: no tables, user functions, external tables, materialized
views, entity groups or stored query results, and no clusters. Write the
empty side and say "everything built in resolves" for the other. Measure
before you write a count of anything on the far side of the bridge:

```python
[(m, getattr(getattr(GlobalState.Default, m), "Count", None))
 for m in ("Operators", "Functions", "Aggregates", "PlugIns")]
```

### Changelog entries are one to three lines
A `CHANGELOG.md` entry states what changed, who is affected, and what to do
about it — nothing else. Mechanism, rationale, and measurement evidence
belong in the commit message and the docs, not the changelog; a bullet that
needs a worked example belongs in README instead, with the changelog line
pointing at it. A contract disclosure (a known collision, an unmodelled
modifier) gets one line plus a pointer to the README section that owns it,
not the derivation.

The 0.2.0 release cycle needed several correction rounds specifically
because entries had grown past changelog altitude — essays standing in for
what a consumer needed to decide whether to upgrade. Keep new entries short
in the first draft rather than writing long and trimming later.

### `LiteralValue` is lazy, cached, and culture-sensitive
Microsoft's parser computes `node.LiteralValue` on **property access**, not at
parse time, then caches it. The .NET culture live at that first read decides the
value — so under a comma-decimal locale the decimal point is read as a group
separator and *every fractional numeric literal* is silently wrong: `1.5h` → 15
hours, `2.25s` → 3m45s, `1.5` → `15.0`, `decimal(1.5)` → `15`; under `fr-FR` the
parse fails to `0`. Integer literals are unaffected, which is what makes this
invisible in a test suite that only uses them.

Because the corruption happens wherever the *caller* touches the property,
scoping a culture fix around our own entry points cannot work.
`bridge._pin_invariant_culture()` therefore pins `InvariantCulture`
**process-wide at import**, with no opt-out. It sets both
`DefaultThreadCurrentCulture` (covers later threads) and the importing thread's
`CurrentCulture`; `CurrentUICulture` is left alone deliberately. A host that
changes culture *after* import re-opens the corruption for any literal not yet
read — documented in the docstring; nothing at this layer can prevent it.

CI runs the suite under `LANG=de-DE` and `LANG=fr-FR` to guard this. If you touch
the pin, verify the guard still bites: revert the pin and confirm the de-DE job
goes red. A green suite without the pin means you are only testing integers.

### Datetime literals are UTC-normalized at build — never read a raw `.Ticks`
The same trap one type over, and this one is the *host's timezone* rather than
its locale. `DateTime.Parse` (what `LiteralValue` uses) returns two different
`DateTimeKind`s from KQL's two datetime spellings, and they need **opposite**
treatment:

- `datetime(2024-01-01)` — no offset in the source — parses as
  `Unspecified`. KQL datetimes are UTC by definition, so the value is already
  right; it only needs `DateTime.SpecifyKind(raw, Utc)` to be *labelled*.
- `datetime(2024-01-01T00:00:00Z)`, and any explicit offset, parses as
  **`Local`**: .NET has already converted it to the host's wall clock. Its
  `.Ticks` carries the host's offset baked in. It needs
  `raw.ToUniversalTime()` — a conversion, not a relabel.

Measured on a UTC-5 host, the raw `.Ticks` for those two literals differ by
five hours (`…640000000000` against `…460000000000`) though the literals name
the same instant. `_builder_helpers.literal_value_and_ticks` does both
branches, so `LiteralExpr.value` and `LiteralExpr.ticks` are UTC and the `Z`
suffix on `value` is unconditional. **Never read `.Ticks` (or `LiteralValue`)
off a `DateTimeKind.Local` node yourself** — go through that helper, or the
`semantic_hash` of a query becomes a function of where the machine is. Swap
the two branches and every offset-suffixed timestamp shifts silently while
bare ones stay correct, which is the shape that survives a test suite written
in one timezone.

CI has a cell for exactly this: `test-locale`'s third entry runs the whole
suite under `en_US.UTF-8` with `TZ=Asia/Tokyo` (`.github/workflows/test.yml`).
It is separate from the two culture cells because it guards a separate bug —
a UTC runner cannot tell "converted" from "not converted", so every other
cell is blind to it. **A local `pytest` is one of those blind runs unless
your machine is off UTC.** To check a datetime change before pushing:

```bash
TZ=Asia/Tokyo .venv/bin/python -m pytest -rs
```

No `-q`: `pyproject.toml`'s `addopts` already passes it, and a second one is
`-qq`, which suppresses the totals line. `-rs` prints the reason for any
skip — including this leg skipping, which is what you would see on a
platform that resolves the local zone from the OS instead of from `TZ`.

## AST structure and navigation

### Node *class* is generic; the type lives in `Kind`
Branching on `type(node).__name__` and branching on `str(node.Kind)` are not
interchangeable, and the difference is silent:
- All six comparisons share the class `BinaryExpression`; only `Kind` separates
  `GreaterThanExpression` / `EqualExpression` / `NotEqualExpression` / …
  (`node.Operator.ToString().strip()` also works).
- `between` and `!between` **both** have the class `BetweenExpression` — the
  negation exists only in `Kind` (`NotBetweenExpression`), so branching on class
  silently inverts the predicate. Bounds are in `.Right` as an `ExpressionCouple`
  with `.First` / `.Second`.
- Every literal shares the class `LiteralExpression`; `Kind` is what says
  `TimespanLiteralExpression` vs `RealLiteralExpression`. Read `Kind` — do not
  re-infer the type from the Python type of `LiteralValue`.

`node.Kind` is a raw .NET enum with no `__format__`, so any f-string format spec
(`f"{node.Kind:<30}"`) raises `TypeError`. Always `str()` it.

### Unary minus wraps a *positive* literal
`-1h` parses as `UnaryMinusExpression` over a `TimespanLiteralExpression` whose
value is `+01:00:00` — correct KQL grammar, same as any language parsing `-1`.
Read the sign from the parent. Tier 2 models it as `UnaryOp(op="-", operand=…)`.

### Left-associative pipe expressions
A pipe chain `A | B | C` is parsed as `PipeExpression(PipeExpression(A, B), C)`.
The leftmost source is the deepest `GetChild(0)`. For `PipeExpression`:
- `GetChild(0)` — left-hand expression (previous part of the pipeline).
- `GetChild(1)` — `|` token.
- `GetChild(2)` — right-hand operator (e.g. `FilterOperator`).

### Source positions
Use `node.TextStart` and `node.Width` for offset-based replacements. Process
replacements back-to-front so earlier offsets remain valid. Do not use
`node.Start` / `node.Length`.

### Semantic vs. syntactic
- `KustoCode.Parse(text)` returns a syntactic-only tree.
- `KustoCode.ParseAndAnalyze(text, globals)` runs the binder, populating
  `node.ReferencedSymbol`.
- The library exposes both via `parse(query)` and
  `parse(query, schema={...})`. Analyzers in `utils/analysis.py` dispatch on
  `KustoCode.HasSemantics`; semantic results are preferred when available.

### Schemas
- Dict form: `{"TableName": {"col": "string", "n": "long"}}` — types resolved
  via `ScalarTypes.GetSymbol`.
- String form: `"(col:string, n:long)"` — passed to `TableSymbol.From`
  (Microsoft's parser).
- Both flow through `utils/analysis.build_global_state`.

### Path expressions: `database("d").T` and `cluster("c").database("d").T`
Modeled as `PathExpression(left, dot, right)` where `right` is the trailing
table identifier. `_unwrap_table_expr` descends into the right child so
syntactic table extraction still resolves `T`. Replacement targets only `T`,
not the `database(...)`/`cluster(...)` calls.

### Structural wrappers
The AST contains `List`, `SeparatedElement`, and similar wrappers with no
logical weight. The `KustoWalker` base in `utils/analysis.py` traverses them
transparently. When matching node kinds, **use exact equality on a closed
set** rather than substring matches like `"List" in kind` (which falsely
matches `NameReferenceList`, `RenameList`, `JsonArrayExpression`, etc.).

### pythonnet identity gotcha
`parent.GetChild(0) is node` is unreliable: pythonnet returns fresh wrapper
objects on each .NET property access. Compare positions instead:

```python
callee = parent.GetChild(0)
return callee.TextStart == node.TextStart and callee.Width == node.Width
```

## Bundled DLL

`src/kustology/bin/Kusto.Language.dll` comes from the
`Microsoft.Azure.Kusto.Language` NuGet package. The version is pinned in
`bin/VERSION.txt` (package, version, sha256, refresh date) and in
`pyproject.toml` under `[tool.kustology]`. Refresh with:

```bash
python scripts/refresh_dll.py             # uses the pinned version
python scripts/refresh_dll.py --version X.Y.Z --pin
```

`bin/VERSION.txt` is rewritten on **every** run (`refresh_dll.py:229-235`),
the bare form included — it re-resolves the pinned version and restamps
`refreshed=`. `--pin` adds the `pyproject.toml` write (`:239-241`), nothing
else. So `refresh_dll.py --version X.Y.Z` *without* `--pin` leaves the two
files disagreeing. Only the **online** `verify_dll.py` catches that: it
reads the version from `pyproject.toml`, prints
`WARN: bin/VERSION.txt records version …` and then fails on the hash it
fetches. `--offline` compares the DLL against `VERSION.txt` alone and never
opens `pyproject.toml`, so it passes happily. After refreshing, run
`python scripts/verify_dll.py` and the full test suite — upstream parser
changes can shift diagnostic codes (`KS204` etc.) or rename AST kinds.

## Tier 2 IR (`kustology.ir`)

### Walkers: prefer `walk` / `find_all` / `collect_nodes` over bespoke recursion
- IR side: `kustology.ir.walk(node[, predicate], prune=...)` yields every Pydantic
  `BaseModel` descendant; `find_all(node, type_)` filters by type. Use
  these for "every X across the whole IR" patterns. Bespoke recursion is
  appropriate when the analyzer's structure mirrors the IR's nesting
  (e.g. rendering a tree, short-circuiting on operator shape).
- AST side: `kustology.utils.analysis.collect_nodes(syntax, predicate)`
  wraps `KustoWalker` for predicate-based single-pass collection. Prefer
  it over writing a fresh `KustoWalker` subclass; the bespoke walkers
  that remain are multi-pass, stateful, or ordered.

**Hand-maintained attribute lists drift — all of them are now gone.** Four
bespoke walkers recursed over a hardcoded tuple of field names (`"left"`,
`"right"`, `"operand"`, …): `SchemaAttacher._fill`, `_walk_expr`
(`tests/ir/test_complex_harness.py`), `walk_expr` (`scripts/mine_corpus.py`) and
a fourth copy in `scripts/verify_corpus.py`. All omitted `pipeline`, `branches`
and `default`, so none descended into `ToScalarExpr` / `MaterializeExpr`
(since-removed) / `SubqueryExpr` or either arm of a `case()`. Each now derives
from `model_fields` or calls `find_all` outright. **Do not reintroduce one.**
If you need a traversal, use `walk` / `find_all`.

Note the generic walker was not immune either: it descended lists and dicts but
not tuples, so `CaseExpr.branches` (`list[tuple[Expr, Expr]]`) was invisible to
`find_all` — a `case()` with five `ColumnRef`s surfaced one. Container descent
is now recursive and container-kind agnostic. A field whose *type* nests
containers is the shape to watch for.

### `KustoQuery.to_ir(attach_schema=...)` auto-attach default
Default is `attach_schema=None`, which auto-attaches iff the parse was
bound. So `parse(q, schema=...).to_ir()` returns a fully enriched IR —
column types, table provenance, `Pipeline.result_schema` — without
restating the schema. Explicit `True` forces attach using the parse-time
schema, `False` skips even on a bound parse. A non-empty `dict` **re-binds
the same tree** through Microsoft's binder
(`self._code.Analyze(build_global_state(dict))`) before building the IR,
then runs the attach pass against that schema — a real re-bind, not an
overlay, so the output schemas, types, and IR shape that come back match
`parse(q, schema=dict).to_ir()` exactly. Diagnostics are the exception:
`ignore_unknown_tables` tracks the *receiver's* own bind state, not the
dict's, so `parse(q).to_ir(attach_schema=d)` stays lenient about unknown
names while `parse(q, schema=d).to_ir()` keeps them. `{}` is falsy and
treated the same as `False`: no re-bind, no attach. Tests that assert the
no-reparse invariant or that exercise enrichment-free IR should pass
`attach_schema=False`.

### `KustoType` is a `StrEnum`
`str(KustoType.LONG)` returns `'long'` (the wire value), not
`'KustoType.LONG'`. The unplaced-type variant is `UNRESOLVED`, **not**
`UNKNOWN` — distinct from `UnknownExpr` (which means "IR builder
couldn't model this shape").

### Three orderings coexist; only one of them is the IR's
The IR keeps a commutative operand list in **source order**, always. The
builder writes it that way and `normalize_expressions` — a faithful public
transform — leaves it that way, because callers apply it to their own IR
alongside spans that still have to line up with the source.

Two consumers reorder for their own purposes, and they do not use the same
key:

- `Expr.canonical_form` sorts **exactly three places**: `And.operands`,
  `Or.operands` and a `SetMembership`'s value list — nothing else — and it
  sorts them **alphabetically by rendered string**, so `State == "TEXAS" and
  EventType == "Tornado"` renders as `EventType == "Tornado" and State ==
  "TEXAS"`. Worth knowing when diffing IR output against AST text.
- `compute_semantic_hash` sorts the same three places on its own deep copy,
  but by each operand's **dumped JSON** (`_sort_commutative`), not by the
  rendered string. Same set of nodes, different key. The dump is the
  stronger of the two — two operands tie only when every field matches — and
  it is why the sort has to run *after* `_clear_volatile`, so span offsets
  cannot order the list, and bottom-up, so a parent is keyed on children that
  are already canonical.

Do not "unify" these by sorting in the builder or in `normalize_expressions`.
The IR's job is to be faithful; canonicalization is the hash's job.

### `UnknownExpr` / `UnknownSource` / `UnknownOp` / `UnknownStmt` are deliberate fallbacks
The builder emits one of these when it can't model a shape, rather than
crashing. The coverage audit (`scripts/audit_syntax_kinds.py`) tracks
baseline counts and fails CI when new shapes surface (typically after
a DLL refresh). To add coverage: dispatch the shape explicitly in
`IRBuilder` and append the `SyntaxKind` to `IRBuilder.HANDLED_OPERATOR_KINDS`,
`IRBuilder.HANDLED_EXPR_KINDS`, or `IRBuilder.HANDLED_STATEMENT_KINDS` —
these are **public** attributes that the audit script reads as contract.

### Declare a field only in the change that populates it
A declared-but-never-populated field reads as implemented and is invisible to
tests. `LetBinding` shipped a public release with four such fields and a
seven-value `category` enum of which one value was ever emitted, which blocked a
downstream consumer entirely. If you cannot populate it now, do not declare it —
and when a field turns out to be unpopulatable *and* unread, deleting it is
usually better than implementing it (`category` was removed, not filled in).

Two more cases surfaced later: `MaterializeExpr` was proven unreachable and
removed, and `SetMembership` gained an `op` field — see lossy lowering below.

**Two detection questions, not one.** "Is this field ever assigned?" is the
obvious check and it is not sufficient — it would not have caught `category`,
which *was* assigned, always to the same literal. Also ask "is every assignment
site the same constant?" That is what found `ExternalDataExpr.format="unknown"`
and `SetMembership.case_sensitive=False`.

### Lossy lowering: a populated node can still lose information
Distinct from declared-but-unpopulated surface, and invisible to the same
checks. When the builder lowers several KQL constructs onto one IR node, the
node is fully populated — nothing looks stubbed — but the distinction *between*
the constructs is gone. `SetMembership` collapsed `in~`, `has_any` and `has_all`
into one node (`has_any` and `has_all` are opposites), and `Exists` collapsed
`isnotnull` and `isnotempty`. Both produced identical `semantic_hash` values for
queries that mean different things, breaking that function's documented
contract.

The check is not "is this field populated" but **"can two different queries
produce identical IR?"** If a node can be reached from more than one source
construct, it needs a field naming which one — `BinOp.op` is the pattern to
copy, and `canonical()` / `llm_view` should render that field rather than
re-deriving the operator from flags.

Often this is information the parser already handed over and the builder threw
away: `in` / `!in` / `in~` / `!in~` share the class `InExpression` and differ
only in `.Kind`, so dispatching on the class name discards it. See the
Kind-vs-class trap above — it shows up as data loss, not just a wrong branch.

### A test that asserts a default proves nothing
Three defects in the sweep survived review behind tests asserting only a field's
default value or its declaration's existence — `assert e.result_type_inner is
None` on a hand-built node passes identically whether the populating code works
or has never worked once. Assert a **non-default value on a real parse**.

### Version tags bump together, once per release; the hash is bind-state-dependent
`IR_SCHEMA_VERSION` (`ir/__init__.py`) must be bumped on any breaking
field-shape change, and `SEMANTIC_HASH_SCHEME` (`ir/transforms.py`) in lockstep
— the scheme prefix exists so a canonicalization change invalidates stored
hashes *visibly* instead of silently returning "these queries differ".

**Do not bump either one yourself.** They move **once per release**, not once
per change: several branches can land between releases and share one
increment, and the tags exist to mark what a consumer can observe rather than
the project's internal history. Record the shape change in the CHANGELOG and
leave the constants alone; the release commit moves them.
`tests/ir/test_schema_tags.py` pins both, so an accidental bump fails there.

Note `semantic_hash` is not bind-invariant: a `let` aliasing a table resolves to
`rhs_pipeline` when bound and `rhs_expr` when not, and that is a shape
difference no volatile-field stripping can hide. This applies to a binding's
own right-hand side; the *use* site is bind-independent, since a name bound by
an earlier `let` is a `LetRef` decided from the statement text alone.

That shape divergence is the *only* one. `_VOLATILE_FIELDS` names every field
the binder writes — `result_type` / `result_type_inner` / `table` /
`result_schema` — plus the source offsets, `span` and `body_span`, so field
*values* never make a query hash two ways. `hints` is in the set for a
different reason: nothing binder-ish about it, but a `hint.strategy=shuffle`
asks the engine to *execute* a query differently without changing the rows it
returns, so two rules that differ only there are one rule to a deduplicating
consumer. When you add a binder-populated field, add it there too, and check
first whether it is carrying source-derived information that must keep
hashing: `ColumnRef.table` was, and splitting `join_side` out of it is what let
the rest be stripped.

The set is keyed by **model field name**, cleared by `_clear_volatile` walking
the hash's deep copy — not by key name in the dumped JSON, which is what it
used to be. Filtering the dump is both too broad and too narrow: it deleted
`AssertSchemaOp.columns` entries for a column the query named `table`, and it
never saw `LetFunction.body_span`, whose field is not called `span`. A new
volatile field is one name in the frozenset; nothing else changes.

`raw_text` is not in the set but is normalized on the same copy: the builder
records `ToString(IncludeTrivia.Minimal)` (no leading trivia, comments gone),
and `_normalize_raw_text` folds line breaks. The rule is **newlines collapse,
interior spacing does not** — and both halves of that are load-bearing,
because `raw_text` is source text and some of what looks like formatting in
it is data:

- A run of spaces can be inside a **string literal**, where it is part of the
  value: `Msg == "error  occurred"` and `Msg == "error occurred"` are
  different predicates. `" ".join(text.split())` merged them. Outside a
  literal there is nothing left to collapse anyway — the DLL already
  normalized it, recording `top-nested 3  of  a` as `top-nested 3 of a`.
  Newlines are the safe thing to fold precisely because a KQL string literal
  cannot contain a raw one.
- Do not add a `//`-comment strip either — `Minimal` already removed them,
  and `//` is also the middle of every URL a rule matches on.

Both boundaries have tests. Widening the function fails the first; adding a
comment strip fails the second.

### `extra="forbid"` on every IR `BaseModel`
Strict validation: JSON dumps with extra top-level fields fail to
validate. Useful for catching drift in third-party serialization;
surprising when round-tripping older dumps that carried since-removed
fields.

## Examples

`tests/test_examples.py` smoke-tests every `*.py` under `examples/` by
importing it and calling `main()` with stdout captured. New examples
must expose a zero-arg `main()` and an SPDX/copyright header to match
the existing convention. Tier 2 examples are listed in the harness's
`IR_EXAMPLES` set so they `importorskip("pydantic")` cleanly on a
base install.

### `examples/_display.py` is presentation, not API

Examples narrate their own output through `_display.py`: `banner`,
`section`, `note`, `kql`, `data`, `table`, `takeaway`, plus `paint` and
`severity` for inline colour. The module picks `rich` when the
`[examples]` extra is installed and plain text when it is not, so no
example carries a rendering branch and none of them imports `rich`.

Two constraints on that file. Its name starts with `_`, which is what
keeps the harness's `*.py` glob from importing it as an example. And
the harness has to put `examples/` on `sys.path` before loading a
module by file path, because a direct `python examples/linter.py` gets
that for free and `spec_from_file_location` does not.

Both rendering paths run in CI: the harness parameterizes each example
over the default path and `KUSTOLOGY_EXAMPLES_PLAIN=1`, and the
ubuntu / py3.10 matrix cell installs neither `[ir]` nor `[examples]`,
so the plain path is exercised there for real.
