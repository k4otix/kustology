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

## AST structure and navigation

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
