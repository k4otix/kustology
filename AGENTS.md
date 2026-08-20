# AI Agent Notes: Kustology

Non-obvious technical context for agents modifying this repository. Read
before changing CLR interop, the AST analysis layer, or the bundled DLL.

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

`--pin` updates `pyproject.toml` and `bin/VERSION.txt` together. After
refreshing, run `python scripts/verify_dll.py` and the full test suite —
upstream parser changes can shift diagnostic codes (`KS204` etc.) or rename
AST kinds.

## Tier 2 IR (`kustology.ir`)

### Walkers: prefer `walk` / `find_all` / `collect_nodes` over bespoke recursion
- IR side: `kustology.ir.walk(node[, predicate])` yields every Pydantic
  `BaseModel` descendant; `find_all(node, type_)` filters by type. Use
  these for "every X across the whole IR" patterns. Bespoke recursion is
  appropriate when the analyzer's structure mirrors the IR's nesting
  (e.g. rendering a tree, short-circuiting on operator shape).
- AST side: `kustology.utils.analysis.collect_nodes(syntax, predicate)`
  wraps `KustoWalker` for predicate-based single-pass collection. Prefer
  it over writing a fresh `KustoWalker` subclass; the bespoke walkers
  that remain are multi-pass, stateful, or ordered.

**Hand-maintained attribute lists drift.** Several bespoke walkers recurse over
a hardcoded tuple of field names (`"left"`, `"right"`, `"operand"`, …) — see
`SchemaAttacher._fill` (`ir/binder.py`), `_walk_expr`
(`tests/ir/test_complex_harness.py`), and `walk_expr` (`scripts/mine_corpus.py`).
Each omits `pipeline`, so none descends into `ToScalarExpr` / `MaterializeExpr` /
`SubqueryExpr`; the binder consequence is that `ColumnRef.table` stays `None`
inside a `toscalar(...)` while the same column resolves fine outside it. Every
new IR field silently widens these holes. If you add a field or a
pipeline-bearing node, either extend all three or convert the walker to iterate
`model_fields` — which is what generic `walk()` / `find_all()` already do.

### `KustoQuery.to_ir(attach_schema=...)` auto-attach default
Default is `attach_schema=None`, which auto-attaches iff the parse was
bound. So `parse(q, schema=...).to_ir()` returns a fully enriched IR —
column types, table provenance, `Pipeline.result_schema` — without
restating the schema. Explicit `True` forces attach using the parse-time
schema, `False` skips even on a bound parse, and a `dict` overrides the
schema for the attach step only. Tests that assert the no-reparse
invariant or that exercise enrichment-free IR should pass
`attach_schema=False`.

### `KustoType` is a `StrEnum`
`str(KustoType.LONG)` returns `'long'` (the wire value), not
`'KustoType.LONG'`. The unplaced-type variant is `UNRESOLVED`, **not**
`UNKNOWN` — distinct from `UnknownExpr` (which means "IR builder
couldn't model this shape").

### `Expr.canonical_form` normalizes operand order
For `And(left, right)` (and other commutative ops), `canonical_form`
sorts operands alphabetically. So the source-order predicate
`State == "TEXAS" and EventType == "Tornado"` renders as
`EventType == "Tornado" and State == "TEXAS"`. Worth knowing when
diffing IR output against AST text.

### `UnknownExpr` / `UnknownSource` / `UnknownOp` are deliberate fallbacks
The builder emits one of these when it can't model a shape, rather than
crashing. The coverage audit (`scripts/audit_syntax_kinds.py`) tracks
baseline counts and fails CI when new shapes surface (typically after
a DLL refresh). To add coverage: dispatch the shape explicitly in
`IRBuilder` and append the `SyntaxKind` to
`IRBuilder.HANDLED_OPERATOR_KINDS` or `IRBuilder.HANDLED_EXPR_KINDS` —
these are **public** attributes that the audit script reads as contract.

### Declare a field only in the change that populates it
A declared-but-never-populated field reads as implemented and is invisible to
tests. `LetBinding` shipped a public release with four such fields and a
seven-value `category` enum of which one value was ever emitted, which blocked a
downstream consumer entirely. If you cannot populate it now, do not declare it —
and when a field turns out to be unreadable *and* unread, deleting it is usually
better than implementing it (`category` was removed, not filled in). Two known
survivors of this pattern are `QueryIR.parse_warnings` and `Span.source_text`.

### Version tags bump together, and the hash is bind-state-dependent
`IR_SCHEMA_VERSION` (`ir/__init__.py`) must be bumped on any breaking
field-shape change, and `SEMANTIC_HASH_SCHEME` (`ir/transforms.py`) in lockstep
— the scheme prefix exists so a canonicalization change invalidates stored
hashes *visibly* instead of silently returning "these queries differ".

Note `semantic_hash` is not bind-invariant: a `let` aliasing a table resolves to
`rhs_pipeline` when bound and `rhs_expr` when not, and that is a shape
difference no volatile-field stripping can hide. `_VOLATILE_FIELDS` strips
`span` / `result_type` / `result_type_inner` / `nullable` only.

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
