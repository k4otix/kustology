# Tier 1 syntax tree

Tier 1 hands you Microsoft's parsed syntax tree through pythonnet, with .NET's
shapes and .NET's interop behavior intact. The sections below cover what that
surface does and where it differs from what a plain Python object would do.

## Member lookup is exact, case-sensitive, and silent

pythonnet resolves .NET members by exact name. A typo or the wrong case makes
`getattr(node, name, default)` return `default` instead of raising or
logging anything:

```python
getattr(node, "Uris", None)   # None -- the member is URIs
getattr(node, "URIs", None)   # the SyntaxList you wanted
```

Code built on a `getattr` fallback keeps running on the default value, and
nothing indicates the lookup missed. Before relying on a probe, confirm the
member exists:

```python
[m for m in dir(node) if m[:1].isupper()]
```

## Empty .NET collections are truthy

`IReadOnlyList` and the other .NET collection types don't implement
`__bool__`. An empty one is still truthy in Python, so a plain `if not
collection:` check never fires, even when the collection has zero elements.
Check `.Count` instead:

```python
if not code.GetSyntaxDiagnostics():      # never true, even with 0 diagnostics
if code.GetSyntaxDiagnostics().Count == 0:   # correct
```

## `node.Kind` is a .NET enum

`node.Kind` is a .NET enum, not a Python string. It has no `__format__`, so
an f-string format spec on it raises `TypeError`. Call `str()` on it first:

```python
f"{node.Kind:<30}"       # TypeError: unsupported format string
f"{str(node.Kind):<30}"  # fine
```

## List-valued properties yield `SeparatedElement` wrappers

`ProjectOperator.Expressions`, `QueryBlock.Statements`, and
`FunctionParameters.Parameters` return `SyntaxList[SeparatedElement[T]]`, not
a list of the elements themselves. Each wrapper carries the trailing comma
alongside the expression, and its `Kind` is `SeparatedElement`, never the
wrapped expression's.

The wrapper's `str()` reads almost like the expression's own, so printed
output looks correct without unwrapping it. A `.Kind` check against the
wrapper still fails to match, with nothing in the output to explain why. Use
`iter_elements`, which unwraps `SeparatedElement` and also passes through
plain `SyntaxList[T]` properties such as `SummarizeOperator.Parameters`:

```python
from kustology import iter_elements, parse

for expr in iter_elements(project_operator.Expressions):
    print(str(expr.Kind))   # NameReference, not SeparatedElement
```

See [`../examples/walk_tree.py`](../examples/walk_tree.py) for a full
traversal that handles this wrapper alongside the tree's other transparent
node kinds.

## `BinaryExpression` is one class for every comparison operator

All six comparison operators share the `BinaryExpression` class, so
`isinstance` or other type-based branching can't tell them apart. Branch on
`str(node.Kind)` (`GreaterThanExpression`, `EqualExpression`,
`NotEqualExpression`, and so on), or read `node.Operator.ToString().strip()`.

## `!between` shares `BetweenExpression` with `between`

`!between` and `between` both produce a `BetweenExpression`. The negation
shows up only in `Kind`, as `NotBetweenExpression`. Branching on class alone
treats a `!between` predicate the same as a `between` one. Both forms put
the column in `.Left` and the bounds in `.Right`, as an `ExpressionCouple`
with `.First` and `.Second`.

## Binding is all-or-nothing in Tier 1

`parse(q)` calls `KustoCode.Parse`, which does no semantic analysis.
`has_semantics` is `False`, and every `ReferencedSymbol` is `None`,
including references to built-in functions. `parse(q, schema=...)` binds
the whole tree, and every symbol resolves.

[Tier 2](tier2-ir.md) doesn't follow that pattern. Calling `to_ir()` on an
unbound parse runs `KustoCode.Analyze(GlobalState.Default)` over the tree
you already have, with no second parse, purely to get types for the IR. A
schemaless IR ends up with real types for everything that doesn't depend on
a table:

```python
ir = parse("StormEvents | where StartTime > ago(7d) and CpuPct > 1.5").to_ir()
# 1.5      -> result_type real,     literal_kind "real"
# 7d       -> result_type timespan, literal_kind "timespan"
# ago(...) -> result_type datetime, is_time_func True
# StartTime, CpuPct -> result_type unresolved, table None
```

`GlobalState.Default` resolves everything built into the language, which is
why `ago(1h)` gets a type. Its database is empty: no tables, user-defined
functions, external tables, materialized views, entity groups, or stored
query results. Columns and tables stay `unresolved` until you supply a
schema. The "unknown name" diagnostics that binding produces against
`GlobalState.Default` come from that empty database. `to_ir()` filters that
family of diagnostic out. A parse you bind yourself keeps all of them,
because there an unresolved name is a real error.

None of this touches the Tier 1 object. `has_semantics` stays `False`, and
every Tier 1 accessor keeps using its syntactic path.

## `TotalSeconds` loses sub-second precision

`TimeSpan.TotalSeconds` is a float, which loses exactness below one second.
A tick is 100 nanoseconds, so `ticks // 10` converts to exact microseconds.
That's precise enough to round-trip `1microsecond` (10 ticks, 1 µs) through
a `datetime.timedelta`.

It doesn't round-trip anything finer than a microsecond. `2tick` is 2
ticks, and `2 // 10 == 0`. `timedelta`'s resolution is one microsecond, so
200 nanoseconds has no representation in it at all. Read `ticks` directly
to preserve sub-microsecond literals. On Tier 2, `LiteralExpr.ticks` carries
the raw tick count directly.

## Unary minus wraps a positive literal

`-1h` parses as a `UnaryMinusExpression` wrapping a
`TimespanLiteralExpression` whose value is `+01:00:00`. This is correct KQL
grammar; most languages parse a negative literal the same way, as a unary
operator over a positive one. Read the sign from the parent node. On
[Tier 2](tier2-ir.md), this is `UnaryOp(op="-", operand=LiteralExpr(...))`.

## Node offsets count UTF-16 code units

`node.TextStart`, `node.Width`, `node.End` and every other offset on a raw
syntax node count UTF-16 code units, because .NET strings are UTF-16. The
Python `str` you passed to `parse()` is indexed by code point. The two agree
across the whole Basic Multilingual Plane and diverge by one for each astral
character (an emoji, a rare CJK ideograph, or a historic script) earlier in
the query.

So slicing your own query text at a raw offset is correct for most input and
wrong for the rest, with no error either way:

```python
from kustology import parse, utf16_to_codepoint

q = 'let e="😀"; T | where X > 1'
tok = next(t for t in parse(q).syntax.GetTokens() if t.Text == "where")

q[tok.TextStart:][:5]                        # 'here ' - off by one
q[utf16_to_codepoint(q, tok.TextStart):][:5] # 'where'
```

`utf16_to_codepoint` and `codepoint_to_utf16` index the text on each call.
To translate more than a couple of offsets from one query, build a
`kustology._text.Utf16Offsets` and reuse it.

This applies to raw nodes only. Every offset kustology itself reports is
already a code-point offset: the `start` and `length` of a diagnostic dict
from `validate()` or `KustoQuery.diagnostics`, the offsets from
`find_time_expressions()`, and [Tier 2](tier2-ir.md)'s `Span`. `replace_table()`
translates internally.

## Lexical spans

`kustology.lexical` reports positions the lexer already decided —
comments, string literals, statements, the tokens themselves — as
code-point spans, with no pydantic and no dependency on Tier 2. Every
`KustoQuery` exposes the same four helpers as methods.

`TextSpan` is the plain type they all return: a `start`/`length`
`NamedTuple` with an `end` property and a `text(query)` method that
slices the original string. `TimeExpr`, `find_time_expressions()`'s
result type, is the same shape with a `text`/`start`/`length` layout
for tuple-unpacking compatibility, plus a `.span` property that returns
the equivalent `TextSpan`. `Token`, `tokens()`'s result type, pairs a
token's own `kind`/`text`/`span` with the `trivia`/`trivia_span`
(whitespace and comments) that precede it.

`tokens(kusto_code)` returns every token, including the final
`EndOfTextToken`, which owns the query's trailing trivia. On a
malformed query, the list also includes the zero-width placeholder
tokens the parser inserts for what it expected but did not find —
empty `text`, no source of their own — for example the missing operand
`IdentifierToken` in `T | where // c\n`.

`comment_spans(kusto_code)` finds every `//` comment by scanning each
token's trivia. KQL has no block comments, so `//` to end of line is
the complete rule. A comment ends at `\n`, `\r`, U+2028 LINE SEPARATOR,
or U+2029 PARAGRAPH SEPARATOR — the line terminator itself is not part
of the span.

`string_literal_spans(kusto_code, include_prefix=True)` finds every
string literal token. Microsoft's token text includes the `@` and `h`
prefixes (verbatim and obfuscated strings); pass
`include_prefix=False` to start the span at the opening quote or
backtick instead.

`statement_spans(kusto_code)` finds the top-level statements in source
order. The `;` separator between statements is excluded from every
span.

Everything above is code points, like every other span kustology
reports — see [Node offsets count UTF-16 code
units](#node-offsets-count-utf-16-code-units) — and none of it needs
the `[ir]` extra: Tier 1 stays usable on the base install.

## Importing kustology pins .NET's culture to invariant

Importing the package sets .NET's culture to invariant for the whole
process, with no opt-out. `CurrentUICulture` is left untouched.

Microsoft's parser evaluates `LiteralValue` lazily, on property access,
using whatever culture is active at that moment. Under a comma-decimal
locale, the decimal point in a literal reads as a group separator, and the
fractional part is dropped. This affects every fractional numeric literal
kind, not only durations:

| literal | written | read back under `de-DE` without the pin |
| --- | --- | --- |
| `timespan` | `1.5h` | `15:00:00` (fifteen hours) |
| `real` | `1.5` | `15.0` (a `where CpuPct > 1.5` filter becomes 10x too strict) |
| `decimal` | `decimal(1.5)` | `15` |

Under `fr-FR`, a duration parses to zero. The corruption happens in caller
code, potentially far from any kustology call, so only a process-global pin
closes it.

The pin runs once, at import. If a host assigns
`CultureInfo.DefaultThreadCurrentCulture` or
`Thread.CurrentThread.CurrentCulture` afterward, directly or through
another .NET-interop library sharing the process, the corruption reopens
for every `LiteralValue` not yet read. This includes literals in a tree
that was parsed while the pin was still in force: a `LiteralValue` is
computed on first access and cached, so only literals already read by that
point keep their correct value.

`ensure_invariant_culture()` restores the pin on the calling thread. Every
kustology entry point (`parse`, `validate`, `format_query`, `to_ir`) calls it
first, so a query the library parses and lowers reads its literals under
invariant culture whatever the host did in between. Call it yourself before
reading `LiteralValue` off a raw syntax node:

```python
from kustology import ensure_invariant_culture, parse
from kustology.utils.analysis import collect_nodes

q = parse("T | where ts > ago(1.5h)")
lit = collect_nodes(q.syntax, lambda n: "TimespanLiteral" in str(n.Kind))[0]
...                                  # a co-tenant switches to de-DE
ensure_invariant_culture()
str(lit.LiteralValue)                # '01:30:00', not '15:00:00'
```

The check is a reference comparison against the cached singleton, so a call
on an already-invariant thread costs one interop property read.
