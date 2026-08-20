# Library Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three verified defects (culture-dependent literal parsing, guessed `literal_kind`, unpopulated `LetBinding`), export the `SeparatedElement` unwrap helper, rename `get_time_range()`, and document the corrected API traps.

**Architecture:** Tier 1 (`bridge`, `utils`, `core`, `services`) stays a minimal projection of Microsoft's parser — it gains a process-global culture pin, one new export, and one deprecation, with no behavioral reinterpretation of the syntax tree. Tier 2 (`kustology.ir`) gains correctness: literal kinds read from the .NET node instead of being re-guessed from Python types, values rendered culture-independently, and let bindings actually populated. All six items ship in one release.

**Tech Stack:** Python 3.10+, pythonnet (CLR bridge to `Kusto.Language.dll`), pydantic v2 (Tier 2 only, behind the `[ir]` extra), pytest, uv, ruff.

**Spec:** `docs/superpowers/specs/2026-08-20-library-gaps-design.md`

## Global Constraints

- Every source file starts with the two-line header: `# SPDX-License-Identifier: Apache-2.0` then `# Copyright 2026 Eddie Allan`.
- Tier 1 (`src/kustology/*.py`, `src/kustology/utils/`) must not import from `src/kustology/ir/` at module scope — the `[ir]` extra is optional. Tier 2 imports Tier 1 freely.
- Tier 2 models set `model_config = {"extra": "forbid"}`.
- Tests under `tests/ir/` require pydantic and are skipped without it (`tests/ir/conftest.py`). Tier 1 tests go in `tests/` and must not import pydantic.
- Run the suite with `.venv/bin/python -m pytest`. Run a locale-specific check with `LANG=de-DE .venv/bin/python -m pytest`.
- The culture pin has **no opt-out** — no environment variable, no keyword argument.
- `_HANDLED_EXPR_KINDS` / `HANDLED_EXPR_KINDS` in `builder.py` is read statically by `scripts/audit_syntax_kinds.py`; re-run `python scripts/audit_syntax_kinds.py --update-baseline` after changing it.
- Commit after each task. Do not squash tasks together.

---

### Task 1: Pin InvariantCulture at bridge import

Microsoft's parser evaluates `LiteralValue` **lazily on property access**, using the culture live at that moment — not the one active during `parse()`. Under `de-DE` the decimal point is read as a group separator (`1.5h` → 15 hours, `2.25s` → 3m45s); under `fr-FR` the parse fails to zero. Because the corruption happens inside caller code, a pin scoped around kustology's entry points fixes nothing. Only a process-wide pin closes it.

**Files:**
- Modify: `src/kustology/bridge.py` (add `_pin_invariant_culture`, call from `_initialize_bridge`)
- Modify: `.github/workflows/test.yml` (add `test-locale` job)
- Test: `tests/test_culture.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `kustology.bridge._pin_invariant_culture() -> None`, called during module import. No public API change.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_culture.py`:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Fractional duration literals must parse identically under every locale.

Microsoft's parser reads ``TimespanLiteralExpression.LiteralValue`` lazily,
using the culture live at property-access time. Under ``de-DE`` the decimal
point is a group separator, so ``1.5h`` yields fifteen hours and ``2.25s``
yields three minutes forty-five; under ``fr-FR`` the parse fails to zero.
The bridge pins InvariantCulture at import to close this.

These tests pass on an en-US machine even without the fix. Run them under
``LANG=de-DE`` to see them fail.
"""

import pytest

from kustology import parse
from kustology.utils.analysis import collect_nodes

TICKS_PER_SECOND = 10_000_000

FRACTIONAL_CASES = [
    ("1.5h", 54_000_000_000),
    ("0.5h", 18_000_000_000),
    ("2.25s", 22_500_000),
    ("1.5d", 1_296_000_000_000),
]


def _single_timespan_ticks(query: str) -> int:
    nodes = collect_nodes(
        parse(query).syntax,
        lambda n: str(n.Kind) == "TimespanLiteralExpression",
    )
    assert len(nodes) == 1, f"expected one timespan literal in {query!r}"
    return nodes[0].LiteralValue.Ticks


def test_bridge_pins_invariant_culture():
    """The pin is in effect after importing kustology, on any host locale."""
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    assert CultureInfo.InvariantCulture.Name == ""
    assert Thread.CurrentThread.CurrentCulture.Name == ""
    assert CultureInfo.DefaultThreadCurrentCulture is not None
    assert CultureInfo.DefaultThreadCurrentCulture.Name == ""


@pytest.mark.parametrize("literal,expected_ticks", FRACTIONAL_CASES)
def test_fractional_timespan_literal_is_culture_independent(literal, expected_ticks):
    assert _single_timespan_ticks(f"T | where X > {literal}") == expected_ticks


@pytest.mark.parametrize("literal,expected_ticks", [("15m", 9_000_000_000), ("1h", 36_000_000_000)])
def test_integer_timespan_literal_unaffected(literal, expected_ticks):
    """Integer literals were always correct — guard against regressing them."""
    assert _single_timespan_ticks(f"T | where X > {literal}") == expected_ticks


def test_pin_survives_a_thread_created_after_import():
    """DefaultThreadCurrentCulture must cover threads spawned later."""
    import threading

    result = {}

    def worker():
        result["ticks"] = _single_timespan_ticks("T | where X > 1.5h")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result["ticks"] == 54_000_000_000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run both. The locale-independent one must fail everywhere; the fractional ones only fail under a comma-decimal locale — that is the point of the second command.

```bash
.venv/bin/python -m pytest tests/test_culture.py::test_bridge_pins_invariant_culture -v
LANG=de-DE .venv/bin/python -m pytest tests/test_culture.py -v
```

Expected: `test_bridge_pins_invariant_culture` FAILS with `assert 'en-US' == ''` (or the host's locale name). Under `LANG=de-DE`, all four `test_fractional_timespan_literal_is_culture_independent` cases FAIL — e.g. `assert 540000000000 == 54000000000` for `1.5h` — and `test_pin_survives_a_thread_created_after_import` FAILS. The two `test_integer_timespan_literal_unaffected` cases PASS even before the fix; that is expected and is why the suite could not see this bug.

**If the de-DE run passes before the fix, stop.** It means the host has no `de-DE` ICU data and the guard is not actually running; install ICU or run the check in the CI container instead.

- [ ] **Step 3: Implement the pin**

In `src/kustology/bridge.py`, add this function immediately after `_load_runtime()`:

```python
def _pin_invariant_culture() -> None:
    """Pin .NET's culture to invariant, process-wide, before any parsing.

    Kusto's ``LiteralValue`` is evaluated lazily on property access, using the
    culture live at that moment — not the one active during ``parse()``. Under
    ``de-DE`` the decimal point is read as a group separator, so ``1.5h``
    yields fifteen hours and ``2.25s`` yields three minutes forty-five; under
    ``fr-FR`` the parse fails to zero. Because the corruption happens inside
    caller code, arbitrarily far from any kustology call, a pin scoped around
    our own entry points would not close it — only a process-wide pin does.

    ``DefaultThreadCurrentCulture`` covers threads created after import;
    ``CurrentThread.CurrentCulture`` covers the importing thread, which the
    default does not retroactively affect. ``CurrentUICulture`` is deliberately
    left alone: it selects exception and diagnostic message language, not value
    parsing.

    This is a deliberate process-global effect of importing kustology, with no
    opt-out. An escape hatch would let a host silently reintroduce 10x and 100x
    duration errors, which is worse than the co-tenancy cost it would avoid.
    """
    from System.Globalization import CultureInfo
    from System.Threading import Thread

    CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture
    Thread.CurrentThread.CurrentCulture = CultureInfo.InvariantCulture
```

Then in `_initialize_bridge()`, add the call as the final statement of the function, after the `clr.AddReference("Kusto.Language")` try/except block:

```python
    _pin_invariant_culture()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_culture.py -v
LANG=de-DE .venv/bin/python -m pytest tests/test_culture.py -v
LANG=fr-FR .venv/bin/python -m pytest tests/test_culture.py -v
```

Expected: PASS in all three.

- [ ] **Step 5: Run the whole suite under all three locales**

```bash
.venv/bin/python -m pytest -q
LANG=de-DE .venv/bin/python -m pytest -q
LANG=fr-FR .venv/bin/python -m pytest -q
```

Expected: all pass. If a pre-existing test now fails under `de-DE`, it was asserting a culture-formatted string; fix the assertion to the invariant form rather than reverting the pin.

- [ ] **Step 6: Add the CI locale matrix**

In `.github/workflows/test.yml`, add this job after the existing `test-ir` job and before `coverage-audit`, matching the surrounding indentation and pinned action SHAs:

```yaml
  test-locale:
    name: Locale ${{ matrix.locale }}
    runs-on: ubuntu-latest
    timeout-minutes: 15
    strategy:
      # Both locales report: de-DE and fr-FR fail differently (10x vs zero).
      fail-fast: false
      matrix:
        locale: ["de_DE.UTF-8", "fr_FR.UTF-8"]
    steps:
      - name: Harden the runner (Audit all outbound calls)
        uses: step-security/harden-runner@ab7a9404c0f3da075243ca237b5fac12c98deaa5 # v2.19.3
        with:
          egress-policy: audit
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
        with:
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5
        with:
          python-version: "3.12"
      - name: Set up .NET 8
        uses: actions/setup-dotnet@67a3573c9a986a3f9c594539f4ab511d57bb3ce9 # v4
        with:
          dotnet-version: "8.0.x"
      - name: Install locale data
        run: |
          sudo apt-get update
          sudo apt-get install -y locales
          sudo locale-gen de_DE.UTF-8 fr_FR.UTF-8
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
      - name: Install package with [ir] extras
        run: uv sync --locked --extra test --extra ir
      # Guards the culture pin. Without it, fractional duration literals parse
      # 10x too large under de-DE and to zero under fr-FR. See
      # bridge._pin_invariant_culture.
      - name: Run full pytest under ${{ matrix.locale }}
        env:
          LANG: ${{ matrix.locale }}
          LC_ALL: ${{ matrix.locale }}
        run: uv run pytest -v
```

- [ ] **Step 7: Verify the guard actually guards**

Temporarily comment out the `_pin_invariant_culture()` call in `_initialize_bridge()`, then:

```bash
LANG=de-DE .venv/bin/python -m pytest tests/test_culture.py -q
```

Expected: FAILURES. A green run here means the guard is inert and the whole task is worthless — investigate before continuing. Restore the call afterwards and re-run to confirm green.

- [ ] **Step 8: Commit**

```bash
git add src/kustology/bridge.py tests/test_culture.py .github/workflows/test.yml
git commit -m "fix: pin InvariantCulture at import to stop culture corrupting duration literals

LiteralValue is evaluated lazily on property access, so the culture live in
caller code decides the parsed value. Under de-DE the decimal point is read
as a group separator (1.5h -> 15h, 2.25s -> 3m45s); under fr-FR the parse
fails to zero. A pin scoped around kustology's own entry points cannot close
this, so the pin is process-global and has no opt-out.

Adds a de-DE/fr-FR CI matrix; the previous suite passed green under de-DE
because it contained no fractional duration literal anywhere."
```

---

### Task 2: Export the `SeparatedElement` unwrap helper at Tier 1

Microsoft's list-valued properties come in two shapes. `SyntaxList[SeparatedElement[T]]` (`ProjectOperator.Expressions`, `QueryBlock.Statements`, `FunctionParameters.Parameters`) yields wrappers carrying the trailing comma; `SyntaxList[T]` (`SummarizeOperator.Parameters`) yields `T` directly. The wrapper's `str()` looks almost identical to the expression's, so a missing unwrap is invisible while every `.Kind` check silently fails to match.

`_iter_elements` already exists at `src/kustology/ir/builder.py:147` with ~25 internal uses. It moves to Tier 1 so consumers walking the .NET tree can reach it without the `[ir]` extra.

**Files:**
- Modify: `src/kustology/utils/walker.py` (add `iter_elements`)
- Modify: `src/kustology/utils/analysis.py` (re-export, add to `__all__`)
- Modify: `src/kustology/__init__.py` (export, add to `__all__`)
- Modify: `src/kustology/ir/builder.py:147-150` (replace the private definition with an import)
- Test: `tests/test_iter_elements.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `kustology.iter_elements(syntax_list) -> Iterator[Any]`, also importable from `kustology.utils.walker` and `kustology.utils.analysis`. Task 4 uses it to read `FunctionParameters.Parameters`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_iter_elements.py`:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``iter_elements`` unwraps SeparatedElement across both .NET list shapes."""

from kustology import iter_elements, parse
from kustology.utils.analysis import collect_nodes


def _op(query: str, kind: str):
    nodes = collect_nodes(parse(query).syntax, lambda n: str(n.Kind) == kind)
    assert nodes, f"no {kind} in {query!r}"
    return nodes[0]


def test_unwraps_separated_element_wrappers():
    """ProjectOperator.Expressions is SyntaxList[SeparatedElement[Expression]]."""
    project = _op("T | project A, B, C", "ProjectOperator")

    # The trap: the raw list yields wrappers, not expressions.
    assert str(project.Expressions[0].Kind) == "SeparatedElement"

    kinds = [str(e.Kind) for e in iter_elements(project.Expressions)]
    assert kinds == ["NameReference", "NameReference", "NameReference"]


def test_passes_through_unwrapped_lists():
    """SummarizeOperator.Parameters is a plain SyntaxList[NamedParameter]."""
    summarize = _op("T | summarize hint.shufflekey=A count() by A", "SummarizeOperator")

    assert not hasattr(summarize.Parameters[0], "Element")

    kinds = [str(p.Kind) for p in iter_elements(summarize.Parameters)]
    assert kinds == ["NamedParameter"]


def test_yields_nothing_for_an_empty_list():
    summarize = _op("T | summarize count() by A", "SummarizeOperator")
    assert list(iter_elements(summarize.Parameters)) == []


def test_unwrapped_elements_carry_the_expression_text():
    project = _op("T | project Alpha, Beta", "ProjectOperator")
    texts = [e.ToString().strip() for e in iter_elements(project.Expressions)]
    assert texts == ["Alpha", "Beta"]
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_iter_elements.py -v
```

Expected: FAIL at collection with `ImportError: cannot import name 'iter_elements' from 'kustology'`.

- [ ] **Step 3: Add `iter_elements` to the Tier 1 walker**

In `src/kustology/utils/walker.py`, add after the `KustoWalker` class and before `node_to_dict`:

```python
def iter_elements(syntax_list):
    """Yield the real nodes of a .NET syntax list, unwrapping ``SeparatedElement``.

    Microsoft's list-valued properties come in two shapes and the difference is
    not visible at the call site. ``SyntaxList[SeparatedElement[T]]`` — which is
    what ``ProjectOperator.Expressions``, ``QueryBlock.Statements`` and
    ``FunctionParameters.Parameters`` return — yields wrappers that carry the
    trailing comma alongside the expression. ``SyntaxList[T]``, such as
    ``SummarizeOperator.Parameters``, yields ``T`` directly.

    A wrapper's ``str()`` differs from the expression's only by that comma and
    surrounding whitespace, so a missing unwrap looks correct in printed output
    while every ``node.Kind`` check silently fails to match — the wrapper's
    ``Kind`` is ``SeparatedElement``, never the expression's kind.

    Handles both shapes so callers need not know which a property returns.

    Example:
        >>> from kustology import iter_elements, parse
        >>> from kustology.utils.analysis import collect_nodes
        >>> syntax = parse("T | project A, B").syntax
        >>> project = collect_nodes(syntax, lambda n: str(n.Kind) == "ProjectOperator")[0]
        >>> [str(e.Kind) for e in iter_elements(project.Expressions)]
        ['NameReference', 'NameReference']
    """
    for i in range(syntax_list.Count):
        item = syntax_list[i]
        yield getattr(item, "Element", item)
```

Update the module docstring's second paragraph to mention it — replace the existing docstring body with:

```python
"""Primitive AST traversal helpers shared by the analysis surface.

:class:`KustoWalker` is a pre/post visitor base class; :func:`iter_elements`
unwraps the ``SeparatedElement`` wrappers that .NET list properties yield;
:func:`node_to_dict` serializes a .NET syntax node into a recursive
``{kind, text, children}`` mapping suitable for JSON or further programmatic
walking.
"""
```

- [ ] **Step 4: Re-export from `analysis` and the package root**

In `src/kustology/utils/analysis.py`, change the walker import line to:

```python
from .walker import KustoWalker, iter_elements, node_to_dict  # re-exported
```

and add `"iter_elements",` to `__all__`, keeping alphabetical order (between `"get_time_range",` and `"node_to_dict",`).

In `src/kustology/__init__.py`, add after the `from .services import ...` line:

```python
from .utils.walker import iter_elements
```

and add `"iter_elements",` to `__all__` under the `# Tier 1 — thin wrapper` group, after `"validate",`.

- [ ] **Step 5: Point the IR builder at the new home**

In `src/kustology/ir/builder.py`, delete the `_iter_elements` definition at lines 147-150:

```python
def _iter_elements(expr_list):
    """Yield the ``.Element`` of each entry in a .NET ``SeparatedSyntaxList``."""
    for i in range(expr_list.Count):
        yield expr_list[i].Element
```

and replace it with an aliasing import so the ~25 existing call sites keep working unchanged:

```python
# Moved to Tier 1 so consumers walking the .NET tree can reach it without the
# [ir] extra. The private alias keeps this module's call sites untouched.
from ..utils.walker import iter_elements as _iter_elements
```

Place that import at the top of the file with the other imports (after the `from ..bridge import ...` line at line 19), not at line 147. Leave line 147 empty where the function was.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_iter_elements.py -v
.venv/bin/python -m pytest -q
```

Expected: PASS. The full suite must stay green — the builder's 25 call sites are exercised by `tests/ir/`.

- [ ] **Step 7: Commit**

```bash
git add src/kustology/utils/walker.py src/kustology/utils/analysis.py \
        src/kustology/__init__.py src/kustology/ir/builder.py \
        tests/test_iter_elements.py
git commit -m "feat: export iter_elements for unwrapping SeparatedElement lists

The helper already existed privately in ir/builder.py with ~25 internal
uses. Consumers walking the .NET tree hit the same trap and had no way to
reach it without the [ir] extra: a wrapper's str() reads like the
expression's, so a missing unwrap is invisible while every .Kind check
silently fails to match. Handles both SyntaxList[T] and
SyntaxList[SeparatedElement[T]]."
```

---

### Task 3: Read `literal_kind` from the .NET node and render values culture-independently

`builder.py:842-851` discards the `.Kind` the .NET node already carries and re-infers from the Python type of `LiteralValue`, so `1.5` reports as `int` and every non-numeric literal as `string`. Six of the ten declared `literal_kind` values are unreachable and `decimal` is absent entirely. Separately, `.ToString()` with no format specifier renders datetimes through the ambient culture, which is how `semantic_hash` became machine-dependent.

**Files:**
- Modify: `src/kustology/ir/expr.py:53-60` (`LiteralExpr`: add `decimal`, add `ticks`)
- Modify: `src/kustology/ir/_builder_helpers.py` (add `literal_kind_for` and `literal_value_and_ticks`)
- Modify: `src/kustology/ir/builder.py` (import the helpers; replace the `LiteralExpression` branch)
- Modify: `src/kustology/ir/llm_view.py:43` (omit `ticks`)
- Test: `tests/ir/test_literals.py` (create)

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces:
  - `LiteralExpr.literal_kind` gains `"decimal"`; the full set is `string, int, long, real, decimal, bool, datetime, timespan, dynamic, guid, null`.
  - `LiteralExpr.ticks: int | None` — exact .NET ticks (100ns units) for `datetime` and `timespan`, `None` otherwise.
  - `_builder_helpers.literal_kind_for(node) -> str`
  - `_builder_helpers.literal_value_and_ticks(node, net_kind) -> tuple[str | int | float | bool | None, int | None]`

- [ ] **Step 1: Write the failing test**

Create `tests/ir/test_literals.py`:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Literal kinds come from the .NET node; values are culture-independent."""

import pytest

from kustology import parse
from kustology.ir import LiteralExpr, find_all

# (query, expected literal_kind, expected value, expected ticks)
LITERAL_CASES = [
    ('T | where S == "abc"', "string", "abc", None),
    ("T | where B == true", "bool", True, None),
    ("T | where N == 42", "long", 42, None),
    ("T | where N == int(5)", "int", 5, None),
    ("T | where R == 1.5", "real", 1.5, None),
    ("T | where R == decimal(1.5)", "decimal", "1.5", None),
    (
        "T | where D == datetime(2024-01-01)",
        "datetime",
        "2024-01-01T00:00:00.0000000",
        638396640000000000,
    ),
    ("T | where W == 15m", "timespan", "00:15:00", 9_000_000_000),
    ("T | where W == 1.5h", "timespan", "01:30:00", 54_000_000_000),
    ("T | where W == 2tick", "timespan", "00:00:00.0000002", 2),
    (
        "T | where G == guid(74be27de-1e4e-49d9-b579-fe0b331d3642)",
        "guid",
        "74be27de-1e4e-49d9-b579-fe0b331d3642",
        None,
    ),
    ("T | where X == int(null)", "null", None, None),
]


def _first_literal(query: str) -> LiteralExpr:
    literals = list(find_all(parse(query).to_ir(), LiteralExpr))
    assert literals, f"no literal in {query!r}"
    return literals[0]


@pytest.mark.parametrize("query,kind,value,ticks", LITERAL_CASES)
def test_literal_kind_value_and_ticks(query, kind, value, ticks):
    lit = _first_literal(query)
    assert lit.literal_kind == kind
    assert lit.value == value
    assert lit.ticks == ticks


def test_dynamic_literal_still_carries_its_json_body():
    lit = _first_literal('T | where D == dynamic({"a":1})')
    assert lit.literal_kind == "dynamic"
    assert lit.value == '{"a":1}'
    assert lit.ticks is None


def test_ticks_reconstruct_an_exact_timedelta():
    """Ticks / 10 -> microseconds is exact; TotalSeconds would not be."""
    from datetime import timedelta

    lit = _first_literal("T | where W == 1microsecond")
    assert lit.ticks == 10
    assert timedelta(microseconds=lit.ticks // 10) == timedelta(microseconds=1)


def test_datetime_value_is_iso_8601_not_culture_formatted():
    lit = _first_literal("T | where D == datetime(2024-03-05 13:45:00)")
    assert lit.value == "2024-03-05T13:45:00.0000000"
    # The old culture-formatted output contained a U+202F narrow no-break space
    # under en-US. Nothing ISO-formatted ever should.
    assert " " not in lit.value


def test_ticks_is_absent_from_the_llm_view():
    """The LLM reads `value`; tick counts are noise for it."""
    ir = parse("T | where W == 15m").to_ir()
    assert "ticks" not in repr(ir.to_llm_dict())


def test_semantic_hash_is_stable_for_temporal_literals():
    """Two builds of the same query agree — the hash no longer depends on
    how .NET happens to render a DateTime for the host locale."""
    q = "T | where D > datetime(2024-01-01) and W > 1.5h"
    assert parse(q).to_ir().semantic_hash == parse(q).to_ir().semantic_hash
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/ir/test_literals.py -v
```

Expected: FAIL. `test_literal_kind_value_and_ticks` fails on most rows — `assert 'int' == 'long'` for `42`, `assert 'int' == 'real'` for `1.5`, `assert 'string' == 'datetime'`, `assert 'string' == 'timespan'`. The `decimal` row fails validation because `"decimal"` is not yet a permitted `literal_kind`. `test_ticks_reconstruct_an_exact_timedelta` fails with `AttributeError: 'LiteralExpr' object has no attribute 'ticks'`.

- [ ] **Step 3: Extend the `LiteralExpr` model**

In `src/kustology/ir/expr.py`, replace the `LiteralExpr` class:

```python
class LiteralExpr(Expr):
    KIND: ClassVar[str] = "literal"
    kind: Literal["literal"] = "literal"
    value: str | int | float | bool | None
    literal_kind: Literal[
        "string", "int", "long", "real", "decimal", "bool", "datetime",
        "timespan", "dynamic", "guid", "null",
    ]
    # Exact .NET tick count (100ns units) for datetime and timespan literals;
    # None for every other kind. TimeSpan.TotalSeconds is a float and loses
    # sub-second exactness, so consumers rebuilding a timedelta use
    # ``ticks // 10`` for microseconds — that is what makes 1microsecond and
    # 2tick round-trip.
    ticks: int | None = None
```

- [ ] **Step 4: Add the literal helpers**

In `src/kustology/ir/_builder_helpers.py`, add at the end of the file:

```python
# Microsoft gives every literal its own SyntaxKind. Reading it is exact;
# re-inferring from the Python type of LiteralValue is not — it cannot tell
# long from real, and collapses datetime, timespan and guid into "string".
_LITERAL_KIND_BY_NET_KIND = {
    "StringLiteralExpression": "string",
    "BooleanLiteralExpression": "bool",
    "LongLiteralExpression": "long",
    "IntLiteralExpression": "int",
    "RealLiteralExpression": "real",
    "DecimalLiteralExpression": "decimal",
    "DateTimeLiteralExpression": "datetime",
    "TimespanLiteralExpression": "timespan",
    "GuidLiteralExpression": "guid",
    "NullLiteralExpression": "null",
}


def literal_kind_for(node: Any) -> str:
    """Map a .NET literal node's ``SyntaxKind`` to an IR ``literal_kind``.

    Typed nulls (``int(null)``, ``datetime(null)``) keep the SyntaxKind of
    their declared type but carry a null ``LiteralValue``; they report as
    ``"null"`` so consumers need only one check.
    """
    if node.LiteralValue is None:
        return "null"
    return _LITERAL_KIND_BY_NET_KIND.get(str(node.Kind), "string")


def literal_value_and_ticks(node: Any) -> tuple[Any, int | None]:
    """Return ``(value, ticks)`` for a .NET literal node, culture-independently.

    ``.ToString()`` with no format specifier renders through the ambient
    culture: the same datetime becomes ``1/1/2024 12:00:00 AM`` under en-US
    (with a U+202F narrow no-break space), ``01.01.2024 00:00:00`` under de-DE
    and ``2024/01/01 0:00:00`` under ja-JP. Those strings reach
    ``semantic_hash`` through ``_normalize.canonical``, which made the hash
    depend on the host locale. Explicit format specifiers remove the
    dependency and, for datetimes, make the value round-trippable:

    * ``"o"`` — ISO 8601 round-trip, e.g. ``2024-01-01T00:00:00.0000000``
    * ``"c"`` — invariant TimeSpan constant form, tick-precise, e.g.
      ``1.12:00:00`` and ``00:00:00.0000002``

    ``ticks`` is populated for datetime and timespan only.
    """
    from System.Globalization import CultureInfo

    raw = node.LiteralValue
    if raw is None:
        return None, None

    net_kind = str(node.Kind)
    if net_kind == "DateTimeLiteralExpression":
        return raw.ToString("o", CultureInfo.InvariantCulture), raw.Ticks
    if net_kind == "TimespanLiteralExpression":
        return raw.ToString("c", CultureInfo.InvariantCulture), raw.Ticks
    if isinstance(raw, (str, int, float, bool)):
        return raw, None
    return raw.ToString(), None
```

- [ ] **Step 5: Rewrite the builder's literal branch**

In `src/kustology/ir/builder.py`, add `literal_kind_for` and `literal_value_and_ticks` to the existing `from ._builder_helpers import (...)` block at line 20, keeping the names alphabetical within that import.

Then replace the `LiteralExpression` branch (currently lines 842-851):

```python
        elif kind == "LiteralExpression":
            val = node.LiteralValue
            if hasattr(val, "ToString") and not isinstance(val, (str, int, float, bool, type(None))):
                val = val.ToString()
            lit_kind = "string"
            if isinstance(val, bool):
                lit_kind = "bool"
            elif isinstance(val, (int, float)):
                lit_kind = "int"
            res = LiteralExpr(value=val, literal_kind=lit_kind, span=span)
```

with:

```python
        elif kind == "LiteralExpression":
            # The .NET node already carries the exact kind; read it rather than
            # re-inferring from the Python type of LiteralValue, which cannot
            # distinguish long from real and collapses datetime/timespan/guid
            # into "string".
            value, ticks = literal_value_and_ticks(node)
            res = LiteralExpr(
                value=value,
                literal_kind=literal_kind_for(node),
                ticks=ticks,
                span=span,
            )
```

- [ ] **Step 6: Keep `ticks` out of the LLM view**

In `src/kustology/ir/llm_view.py`, change line 43:

```python
_OMIT_FIELDS = {"span", "schema_attached"}
```

to:

```python
# ``ticks`` is the machine-exact companion to ``value``; an LLM reads the
# rendered value, so emitting both is noise.
_OMIT_FIELDS = {"span", "schema_attached", "ticks"}
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/ir/test_literals.py -v
```

Expected: PASS.

- [ ] **Step 8: Run the full suite and fix hash-dependent assertions**

```bash
.venv/bin/python -m pytest -q
```

Expected: some `tests/ir/` assertions fail — any that hard-code a `semantic_hash` value, a `literal_kind` of `"string"`/`"int"` for a temporal or real literal, or a culture-formatted `value`. These are correct failures. Update each to the new expected value; do not weaken an assertion to `is not None` to make it pass.

Also confirm the locale runs stay green:

```bash
LANG=de-DE .venv/bin/python -m pytest -q
LANG=fr-FR .venv/bin/python -m pytest -q
```

- [ ] **Step 9: Refresh the SyntaxKind baseline**

```bash
.venv/bin/python scripts/audit_syntax_kinds.py --update-baseline
git diff tests/fixtures/syntax_kinds_baseline.json
```

Expected: either no diff or a small one. Review it before staging — an unexpectedly large diff means the dispatch set changed more than intended.

- [ ] **Step 10: Commit**

```bash
git add src/kustology/ir/expr.py src/kustology/ir/_builder_helpers.py \
        src/kustology/ir/builder.py src/kustology/ir/llm_view.py \
        tests/ir/ tests/fixtures/syntax_kinds_baseline.json
git commit -m "fix(ir): read literal_kind from the .NET node; render values culture-independently

literal_kind was re-inferred from the Python type of LiteralValue, so real
reported as int and datetime, timespan and guid all reported as string. Six
of ten declared values were unreachable and decimal was absent entirely.

Values now use explicit format specifiers: ISO 8601 round-trip for datetime,
invariant constant form for timespan. The previous bare ToString() rendered
through the ambient culture and reached semantic_hash via canonical(), making
the hash depend on the host locale.

Adds LiteralExpr.ticks for exact sub-second reconstruction (ticks // 10 ->
microseconds); TotalSeconds is a float and loses exactness.

BREAKING (tier 2, pre-1.0): literal_kind values change for long, real,
decimal, datetime, timespan, guid and null literals; LiteralExpr.value
changes format for datetime and timespan; semantic_hash changes for any
query containing one of those literals."
```

---

### Task 4: Populate `LetBinding`, drop `category`, add `rhs_function`

`builder.py:231-238` builds every binding in one list comprehension with `category="alias"` hardcoded — no dispatch branch exists, so four optional fields and six of seven `category` values are unreachable. `category` is removed rather than defined: nothing reads it, every binding gets the same value, and it feeds `semantic_hash` via `transforms.py:181` without being stripped as volatile.

**Files:**
- Modify: `src/kustology/ir/query.py:518-531` (`LetBinding`; add `LetFunction`)
- Modify: `src/kustology/ir/__init__.py` (export `LetFunction`)
- Modify: `src/kustology/ir/builder.py:231-238` (replace the comprehension with `_visit_let_statement`)
- Test: `tests/ir/test_let_bindings.py` (create)

**Interfaces:**
- Consumes: `kustology.utils.walker.iter_elements` (Task 2); `LiteralExpr.literal_kind` values from Task 3.
- Produces:
  - `kustology.ir.LetFunction` — `parameters: list[str]`, `body_span: Span`.
  - `LetBinding` fields: `name`, `span`, `rhs_expr`, `rhs_pipeline`, `rhs_function`, `inner_tables`, `inner_time_exprs`. **`category` is gone.**
  - `IRBuilder._visit_let_statement(ls) -> LetBinding`

- [ ] **Step 1: Write the failing test**

Create `tests/ir/test_let_bindings.py`:

```python
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Let bindings carry their right-hand side, not just a name and a span."""

import pytest

from kustology import parse
from kustology.ir import LetBinding, LiteralExpr, Pipeline


def _binding(query: str, name: str) -> LetBinding:
    ir = parse(query).to_ir()
    matches = [lb for lb in ir.let_bindings if lb.name == name]
    assert matches, f"no let binding named {name!r} in {query!r}"
    return matches[0]


def test_scalar_binding_populates_rhs_expr():
    lb = _binding("let lookback = 15m; T | where X > lookback", "lookback")
    assert isinstance(lb.rhs_expr, LiteralExpr)
    assert lb.rhs_expr.literal_kind == "timespan"
    assert lb.rhs_expr.ticks == 9_000_000_000
    assert lb.rhs_pipeline is None
    assert lb.rhs_function is None


def test_tabular_binding_populates_rhs_pipeline_and_inner_tables():
    lb = _binding(
        "let Base = SecurityEvent | where EventID == 1; Base | count", "Base"
    )
    assert isinstance(lb.rhs_pipeline, Pipeline)
    assert lb.inner_tables == ["SecurityEvent"]
    assert lb.rhs_expr is None
    assert lb.rhs_function is None


def test_tabular_binding_collects_inner_time_expressions():
    lb = _binding(
        "let Recent = SecurityEvent | where TimeGenerated > ago(7d); Recent | count",
        "Recent",
    )
    assert [e.name for e in lb.inner_time_exprs] == ["ago"]


def test_toscalar_binding_populates_rhs_expr():
    lb = _binding(
        "let m = toscalar(SecurityEvent | summarize max(EventID)); T | where X == m",
        "m",
    )
    assert lb.rhs_expr is not None
    assert type(lb.rhs_expr).__name__ == "ToScalarExpr"
    assert lb.rhs_pipeline is None


def test_function_binding_populates_rhs_function():
    lb = _binding("let f = (x:int, y:string) { x + 1 }; T | extend Z = f(1, 'a')", "f")
    assert lb.rhs_function is not None
    assert lb.rhs_function.parameters == ["x", "y"]
    assert lb.rhs_function.body_span.width > 0
    assert lb.rhs_expr is None
    assert lb.rhs_pipeline is None


def test_bare_name_alias_is_not_silently_empty():
    """`let A = OtherTable` must populate exactly one right-hand side field."""
    lb = _binding("let A = OtherTable; A | count", "A")
    populated = [lb.rhs_expr, lb.rhs_pipeline, lb.rhs_function]
    assert sum(x is not None for x in populated) == 1


def test_multiple_bindings_keep_source_order():
    ir = parse("let a = 1m; let b = 2m; let c = 3m; T | count").to_ir()
    assert [lb.name for lb in ir.let_bindings] == ["a", "b", "c"]


def test_category_field_is_gone():
    """Removed rather than defined — nothing read it and it polluted the hash."""
    lb = _binding("let lookback = 15m; T | count", "lookback")
    assert not hasattr(lb, "category")
    assert "category" not in lb.model_dump()


def test_rejects_stored_json_carrying_the_removed_field():
    """extra='forbid' must surface the removal loudly, not drop the key."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LetBinding.model_validate(
            {
                "name": "x",
                "span": {"text_start": 0, "width": 1},
                "category": "alias",
            }
        )
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/ir/test_let_bindings.py -v
```

Expected: FAIL. Every RHS assertion fails with `assert None is not None` because the builder never populates them; `test_category_field_is_gone` fails because `category` still exists; `test_rejects_stored_json_carrying_the_removed_field` fails because the field is still accepted.

- [ ] **Step 3: Update the models**

In `src/kustology/ir/query.py`, replace the `LetBinding` class (lines 518-531) with:

```python
class LetFunction(BaseModel):
    """A ``let``-declared function's shape. The body is not modeled.

    ``let f = (x:int) { ... }`` yields a .NET ``FunctionDeclaration``, which is
    neither an expression nor a pipeline and so cannot ride on ``rhs_expr`` or
    ``rhs_pipeline``. Recording it explicitly keeps the unmodeled boundary
    legible instead of leaving three silent ``None``s that read as a bug.

    Parameter types, defaults, tabular-vs-scalar bodies and call-site expansion
    are out of scope; ``body_span`` locates the body in the source for callers
    that want the text.
    """

    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "let_function"
    kind: Literal["let_function"] = "let_function"
    # Parameter names in declaration order. The function's own name is on the
    # owning LetBinding.
    parameters: list[str] = []
    body_span: Span


class LetBinding(BaseModel):
    """One ``let`` statement. Exactly one ``rhs_*`` field is populated.

    There is no ``category`` discriminator: which field is set already says
    whether the binding is tabular, scalar or a function, and finer labels
    (time-scalar, alias, scalar-subquery) are recoverable from the populated
    right-hand side — ``rhs_expr.literal_kind == "timespan"``, a ``TableRef``
    source with no operators, a ``ToScalarExpr``. A stored label would also
    have entered ``semantic_hash``, making the hash sensitive to our
    classification choices rather than to query semantics.
    """

    model_config = {"extra": "forbid"}
    KIND: ClassVar[str] = "let_binding"
    kind: Literal["let_binding"] = "let_binding"
    name: str
    span: Span
    rhs_expr: AnyExpr | None = None
    rhs_pipeline: Pipeline | None = None
    rhs_function: LetFunction | None = None
    # Tables and time expressions found inside rhs_pipeline; empty otherwise.
    inner_tables: list[str] = []
    inner_time_exprs: list[AnyExpr] = []
```

Add `LetBinding.model_rebuild()` and `LetFunction.model_rebuild()` to the block of `model_rebuild()` calls at the end of the file, after `Pipeline.model_rebuild()`.

In `src/kustology/ir/__init__.py`, add `LetFunction` to the `from .query import (...)` list (alphabetically, right after `LetBinding`) and add `"LetFunction",` to `__all__` immediately after `"LetBinding",`.

- [ ] **Step 4: Implement the builder dispatch**

In `src/kustology/ir/builder.py`, add `LetFunction` to the existing `from .query import (...)` block alongside `LetBinding`.

Replace the let-binding comprehension (lines 231-238):

```python
        let_bindings: list[LetBinding] = [
            LetBinding(
                name=visit_name(ls.Name),
                span=to_span(ls),
                category="alias",
            )
            for ls in root.GetDescendants[LetStatement]()
        ]
```

with:

```python
        let_bindings: list[LetBinding] = [
            self._visit_let_statement(ls)
            for ls in root.GetDescendants[LetStatement]()
        ]
```

Then add this method to `IRBuilder`, immediately after `build_from_code`:

```python
    # -- let statements --------------------------------------------------

    def _visit_let_statement(self, ls: Any) -> LetBinding:
        """Build one :class:`LetBinding` from a .NET ``LetStatement``.

        ``ls.Expression`` carries the right-hand side and its .NET class says
        which shape it is:

        * ``FunctionDeclaration``  -> ``rhs_function``
        * ``PipeExpression`` / ``MaterializeExpression`` -> ``rhs_pipeline``
        * a ``NameReference`` the binder resolved to a table -> ``rhs_pipeline``
        * anything else            -> ``rhs_expr``

        A bare ``NameReference`` is only tabular when the binder can prove it
        (``let A = OtherTable`` with a schema). Unbound, it stays an
        expression rather than guessing a table into existence.
        """
        name = visit_name(ls.Name)
        span = to_span(ls)
        expr = getattr(ls, "Expression", None)
        if expr is None:  # pragma: no cover — defensive
            return LetBinding(name=name, span=span)

        net_kind = str(type(expr).__name__)

        if net_kind == "FunctionDeclaration":
            return LetBinding(
                name=name,
                span=span,
                rhs_function=self._visit_function_declaration(expr),
            )

        if net_kind in ("PipeExpression", "MaterializeExpression") or (
            net_kind == "NameReference"
            and is_table_symbol(getattr(expr, "ReferencedSymbol", None))
        ):
            pipeline = self._visit_pipeline(expr)
            return LetBinding(
                name=name,
                span=span,
                rhs_pipeline=pipeline,
                inner_tables=_collect_inner_tables(pipeline),
                inner_time_exprs=_collect_inner_time_exprs(pipeline),
            )

        return LetBinding(name=name, span=span, rhs_expr=self._visit_expr(expr))

    def _visit_function_declaration(self, node: Any) -> LetFunction:
        """Extract parameter names and the body's span from a FunctionDeclaration.

        ``node.Parameters`` is a ``FunctionParameters`` wrapper whose own
        ``.Parameters`` is a ``SyntaxList[SeparatedElement[FunctionParameter]]``
        — hence the unwrap.
        """
        params: list[str] = []
        outer = getattr(node, "Parameters", None)
        inner = getattr(outer, "Parameters", None) if outer is not None else None
        if inner is not None:
            for param in _iter_elements(inner):
                name_and_type = getattr(param, "NameAndType", None)
                if name_and_type is not None:
                    params.append(visit_name(name_and_type.Name))
        return LetFunction(parameters=params, body_span=to_span(node.Body))
```

Add `is_table_symbol` to the `from ._builder_helpers import (...)` block if it is not already imported there.

Then add these two module-level helpers next to `_iter_elements` near the top of the file:

```python
def _collect_inner_tables(pipeline: Any) -> list[str]:
    """Distinct table names inside a let binding's pipeline, in first-seen order."""
    from .query import TableRef
    from .walk import find_all

    seen: list[str] = []
    for ref in find_all(pipeline, TableRef):
        if ref.name not in seen:
            seen.append(ref.name)
    return seen


def _collect_inner_time_exprs(pipeline: Any) -> list[Any]:
    """Time-function calls inside a let binding's pipeline, in walk order.

    Time *literals* are reachable through ``rhs_pipeline`` already; this
    surfaces the calls (``ago``, ``now``, ``bin``, ...) that a lookback
    analyzer needs to find without re-walking the tree.
    """
    from .expr import FuncCall
    from .walk import find_all

    return [fc for fc in find_all(pipeline, FuncCall) if fc.is_time_func]
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/ir/test_let_bindings.py -v
```

Expected: PASS.

Note `test_bare_name_alias_is_not_silently_empty` uses an unbound parse, so `OtherTable` resolves to no symbol and lands in `rhs_expr`. That satisfies the "exactly one populated" assertion — the test guards against all three being `None`, not against a particular choice.

- [ ] **Step 6: Run the full suite**

```bash
.venv/bin/python -m pytest -q
LANG=de-DE .venv/bin/python -m pytest -q
```

Expected: `tests/ir/test_ir_roundtrip.py` and `tests/ir/test_ast_isolation.py` may fail if they assert on `category` or on a `semantic_hash` for a query containing a `let`. Update those assertions — `let_bindings` now carry real content, so hashes for let-bearing queries legitimately change.

- [ ] **Step 7: Commit**

```bash
git add src/kustology/ir/query.py src/kustology/ir/__init__.py \
        src/kustology/ir/builder.py tests/ir/
git commit -m "fix(ir): populate LetBinding from LetStatement.Expression

The builder set only name, span and a hardcoded category='alias' in a list
comprehension — no dispatch branch existed, so rhs_expr, rhs_pipeline,
inner_tables and inner_time_exprs were unreachable and let-resolution was
impossible on tier 2.

Removes category rather than defining it: nothing read it, every binding
got the same value, and it fed semantic_hash via transforms.py without
being stripped as volatile. Which rhs_* field is populated already carries
the distinction, and finer labels are recoverable from the RHS.

Adds LetFunction so function-valued lets have a home instead of three
silent Nones; the body is deliberately not modeled.

BREAKING (tier 2, pre-1.0): LetBinding.category removed — stored IR JSON
containing it now fails extra='forbid'. semantic_hash changes for queries
containing let statements."
```

---

### Task 5: Rename `get_time_range()` to `find_time_expressions()`

The function returns every time-related expression with spans in source order — a discovery list, not a resolved range. The name has already led a downstream consumer to use it as a lookback extractor and get wrong answers, because it returns bare `now()`, bare `1h` operands, and `!between` operands undifferentiated.

A semantic lookback extractor is **out of scope**: resolving an effective window needs let-resolution, `TimeGenerated` awareness and negation handling, which is analysis rather than projection.

**Files:**
- Modify: `src/kustology/utils/analysis.py:334-382` (rename; add deprecated alias)
- Modify: `src/kustology/core.py:16,77-79` (rename method; add deprecated alias)
- Modify: `examples/query_analysis.py:22,90-91` (use the new name)
- Test: `tests/test_advanced_utils.py` (update the three existing tests; add deprecation tests)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `kustology.utils.analysis.find_time_expressions(kusto_code) -> list[tuple[str, int, int]]`
  - `KustoQuery.find_time_expressions() -> list[tuple[str, int, int]]`
  - Both old names survive as `DeprecationWarning`-emitting aliases with identical behavior.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_advanced_utils.py`:

```python
def test_find_time_expressions_returns_tuples_in_source_order():
    query = "T | where TimeGenerated > ago(1h) | extend n = now()"
    times = parse(query).find_time_expressions()
    assert [t[0] for t in times] == ["ago(1h)", "now()"]
    assert times == sorted(times, key=lambda t: t[1])


def test_get_time_range_is_a_deprecated_alias():
    import warnings

    query = "T | where TimeGenerated > ago(1h)"
    result = parse(query)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        legacy = result.get_time_range()
    assert legacy == result.find_time_expressions()
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
    assert "find_time_expressions" in str(caught[0].message)


def test_module_level_get_time_range_is_a_deprecated_alias():
    import warnings

    from kustology.utils.analysis import find_time_expressions, get_time_range

    code = parse("T | where TimeGenerated > ago(1h)")._code
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        legacy = get_time_range(code)
    assert legacy == find_time_expressions(code)
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
```

Also rename the three existing tests to match the new name, changing both the test function names and the calls inside them:

- `test_get_time_range_returns_tuples_in_source_order` → `test_find_time_expressions_ignores_nothing_in_source_order` (it duplicates the new test above; delete it rather than keeping both)
- `test_get_time_range_ignores_string_literal_text` → `test_find_time_expressions_ignores_string_literal_text`, calling `.find_time_expressions()`
- `test_get_time_range_does_not_double_count_nested_literals` → `test_find_time_expressions_does_not_double_count_nested_literals`, calling `.find_time_expressions()`

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_advanced_utils.py -v -k time
```

Expected: FAIL with `AttributeError: 'KustoQuery' object has no attribute 'find_time_expressions'`.

- [ ] **Step 3: Rename in `analysis.py`**

Add `import warnings` to the imports at the top of `src/kustology/utils/analysis.py`.

Rename the function at line 334 from `get_time_range` to `find_time_expressions` and replace its docstring:

```python
def find_time_expressions(kusto_code) -> list[tuple[str, int, int]]:
    """Return ``[(text, start, length), ...]`` for every time-related expression
    in source order: time-function calls (``ago``, ``now``, ``bin``, ...) plus
    standalone datetime/timespan literals not already inside a matched call.

    A **discovery aid**, not a lookback extractor. The result is syntactic: it
    includes bare ``now()``, bare ``1h`` operands, and the operands of
    ``!between`` — with no indication of which bound a given expression is, or
    whether it constrains the query's time column at all. Resolving an
    effective time window additionally needs let-resolution, awareness of which
    column is temporal, and negation handling; build that on the tier-2 IR
    rather than on this list.
    """
```

The body is unchanged. Then add the deprecated alias immediately after it:

```python
def get_time_range(kusto_code) -> list[tuple[str, int, int]]:
    """Deprecated alias for :func:`find_time_expressions`.

    The old name promised a resolved range and returned a discovery list,
    which led callers to use it as a lookback extractor and get wrong answers.
    """
    warnings.warn(
        "get_time_range() is deprecated; use find_time_expressions(). It "
        "returns a source-ordered discovery list of time expressions — "
        "including bare now(), bare operands and !between operands — not a "
        "resolved time range.",
        DeprecationWarning,
        stacklevel=2,
    )
    return find_time_expressions(kusto_code)
```

In `__all__`, add `"find_time_expressions",` in alphabetical position (after `"find_table_references",`) and keep `"get_time_range",`.

- [ ] **Step 4: Rename in `core.py`**

Change the import at line 16 from `get_time_range,` to:

```python
    find_time_expressions,
```

placing it alphabetically (after `find_table_references,`).

Replace the method at lines 77-79:

```python
    def get_time_range(self) -> list[tuple[str, int, int]]:
        """Return [(text, start, length), ...] in source order."""
        return get_time_range(self._code)
```

with:

```python
    def find_time_expressions(self) -> list[tuple[str, int, int]]:
        """Return ``[(text, start, length), ...]`` in source order.

        A discovery aid, not a lookback extractor — see
        :func:`kustology.utils.analysis.find_time_expressions`.
        """
        return find_time_expressions(self._code)

    def get_time_range(self) -> list[tuple[str, int, int]]:
        """Deprecated alias for :meth:`find_time_expressions`."""
        import warnings

        warnings.warn(
            "KustoQuery.get_time_range() is deprecated; use "
            "find_time_expressions(). It returns a source-ordered discovery "
            "list of time expressions, not a resolved time range.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.find_time_expressions()
```

- [ ] **Step 5: Update the example**

In `examples/query_analysis.py`, change line 22's docstring entry from `get_time_range()` to `find_time_expressions()` (adjusting the alignment padding to match the surrounding lines), and change lines 90-91:

```python
    banner("get_time_range()")
    time_windows = result.get_time_range()
```

to:

```python
    banner("find_time_expressions()")
    time_windows = result.find_time_expressions()
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_advanced_utils.py -v -k time
.venv/bin/python -m pytest -q
```

Expected: PASS. `tests/test_examples.py` runs the examples, so a missed rename there surfaces here.

- [ ] **Step 7: Commit**

```bash
git add src/kustology/utils/analysis.py src/kustology/core.py \
        examples/query_analysis.py tests/test_advanced_utils.py
git commit -m "refactor: rename get_time_range() to find_time_expressions()

The function returns a source-ordered discovery list of time expressions —
including bare now(), bare operands and !between operands — not a resolved
range. The old name led a consumer to use it as a lookback extractor and
get wrong answers. Old name retained as a DeprecationWarning alias on both
the module function and KustoQuery, with identical behavior."
```

---

### Task 6: Document the corrected API traps and the release's breaking changes

Six traps cost the downstream consumer real time, and four of the original nine reported claims turned out to be misreadings. Documenting the corrected set is what stops the next consumer repeating them.

**Files:**
- Modify: `README.md` (add a "Working with Microsoft's syntax tree" section)
- Modify: `CHANGELOG.md` (add the release entry)
- Test: none — documentation. Verified by the doctest-style example in Task 2 and by reading.

**Interfaces:**
- Consumes: every public name introduced in Tasks 1-5.
- Produces: nothing code-facing.

- [ ] **Step 1: Add the trap section to the README**

Insert this section after the "Choosing a tier" table and before "Prerequisites":

```markdown
## Working with Microsoft's syntax tree

Tier 1 is a thin projection: you get Microsoft's nodes, with Microsoft's
shapes. These are the places that shape surprises people.

**`node.Kind` is a .NET enum, not a string.** It has no `__format__`, so any
f-string format spec raises `TypeError`. Call `str()` on it — always:

```python
f"{node.Kind:<30}"       # TypeError: unsupported format string
f"{str(node.Kind):<30}"  # fine
```

**List-valued properties yield `SeparatedElement` wrappers.**
`ProjectOperator.Expressions`, `QueryBlock.Statements` and
`FunctionParameters.Parameters` return `SyntaxList[SeparatedElement[T]]`; the
wrapper carries the trailing comma alongside the expression. Its `str()` reads
almost like the expression's, so a missing unwrap looks correct in printed
output while every `.Kind` check silently fails to match — the wrapper's
`Kind` is `SeparatedElement`, never the expression's. Use `iter_elements`,
which also passes through plain `SyntaxList[T]` such as
`SummarizeOperator.Parameters`:

```python
from kustology import iter_elements, parse

for expr in iter_elements(project_operator.Expressions):
    print(str(expr.Kind))   # NameReference, not SeparatedElement
```

**`BinaryExpression` is one generic class; the operator lives in `Kind`.**
All six comparisons share the class, so branching on type will not separate
them. Branch on `str(node.Kind)` (`GreaterThanExpression`, `EqualExpression`,
`NotEqualExpression`, ...) or read `node.Operator.ToString().strip()`.

**`!between` shares `BetweenExpression` with `between`.** The negation exists
only in `Kind` (`NotBetweenExpression`), so branching on class silently
inverts the predicate. Both put the column in `.Left` and the bounds in
`.Right` as an `ExpressionCouple` with `.First` / `.Second`.

**Symbols require a schema — there is no partial binding.** `parse(q)` calls
`KustoCode.Parse`, which does no semantic analysis: `has_semantics` is
`False` and every `ReferencedSymbol` is `None`, built-in functions included.
`parse(q, schema=...)` binds, and they all resolve. It is all-or-nothing.

**Read `TimeSpan.Ticks`, not `TotalSeconds`.** `TotalSeconds` is a float and
loses sub-second exactness. `ticks // 10` gives exact microseconds, which is
what makes `1microsecond` and `2tick` round-trip. On Tier 2, `LiteralExpr.ticks`
carries this directly.

**Unary minus wraps a positive literal.** `-1h` parses as a
`UnaryMinusExpression` over a `TimespanLiteralExpression` whose value is
`+01:00:00` — correct KQL grammar, the same way every language parses `-1`.
Read the sign from the parent. Tier 2 models this as
`UnaryOp(op="-", operand=LiteralExpr(...))`.

**Importing kustology pins .NET's culture to invariant, process-wide.** This
is deliberate and has no opt-out. Microsoft's parser evaluates `LiteralValue`
lazily on property access, using the culture live at that moment, so under a
comma-decimal locale `1.5h` parses as fifteen hours and under `fr-FR` it
parses as zero. Because the corruption happens in caller code, only a
process-global pin closes it. `CurrentUICulture` is left untouched.
```

- [ ] **Step 2: Add the CHANGELOG entry**

Add at the top of `CHANGELOG.md`, under a new version heading matching the file's existing format:

```markdown
### Fixed

- **Culture no longer corrupts duration literals (tier 1).** Importing
  `kustology` now pins .NET's culture to invariant, process-wide. Microsoft's
  parser evaluates `LiteralValue` lazily on property access, so the culture
  live in *caller* code decided the parsed value: under `de-DE` the decimal
  point was read as a group separator (`1.5h` → 15 hours, `2.25s` → 3m45s)
  and under `fr-FR` the parse failed to zero. Integer literals were
  unaffected, which is why the previous suite passed green under `de-DE`. A
  `de-DE`/`fr-FR` CI matrix now guards it. No opt-out.
- **`literal_kind` is read from the .NET node (tier 2).** It was re-inferred
  from the Python type of `LiteralValue`, so `real` reported as `int` and
  `datetime`, `timespan` and `guid` all reported as `string`.
- **`LiteralExpr.value` is culture-independent (tier 2).** Datetimes render
  as ISO 8601 round-trip and timespans in invariant constant form. The
  previous bare `ToString()` rendered through the ambient culture and reached
  `semantic_hash`, making the hash differ across machines for the same query.
- **`LetBinding` is populated (tier 2).** The builder set only `name`, `span`
  and a hardcoded `category="alias"`, leaving `rhs_expr`, `rhs_pipeline`,
  `inner_tables` and `inner_time_exprs` permanently empty.

### Added

- `iter_elements()` (tier 1) — unwraps the `SeparatedElement` wrappers that
  .NET list properties yield, and passes plain `SyntaxList` through unchanged.
- `LiteralExpr.ticks` (tier 2) — exact .NET ticks for `datetime` and
  `timespan` literals; `ticks // 10` gives exact microseconds.
- `LetFunction` (tier 2) — parameter names and body span for `let`-declared
  functions. The body is not modeled.
- `literal_kind` gains `"decimal"`.
- README section documenting the syntax-tree traps.

### Changed

- `get_time_range()` is renamed `find_time_expressions()` on both the module
  and `KustoQuery`. The old names remain as `DeprecationWarning` aliases with
  identical behavior. The function returns a source-ordered discovery list —
  including bare `now()`, bare operands and `!between` operands — not a
  resolved range.

### Breaking (tier 2, pre-1.0)

- `literal_kind` returns different values for `long`, `real`, `decimal`,
  `datetime`, `timespan`, `guid` and `null` literals.
- `LiteralExpr.value` changes format for `datetime` and `timespan`.
- `semantic_hash` changes for any query containing a datetime, timespan or
  real literal, or a `let` statement. This is the point — it was previously
  machine-dependent.
- `LetBinding.category` is removed. Nothing read it, every binding carried
  the same value, and it entered `semantic_hash` without being stripped as
  volatile. Which `rhs_*` field is populated already carries the distinction.
  Stored IR JSON containing `category` now fails `extra="forbid"` on load.
```

- [ ] **Step 3: Verify the README examples run**

```bash
.venv/bin/python -c "
from kustology import iter_elements, parse
from kustology.utils.analysis import collect_nodes
syntax = parse('T | project A, B').syntax
project = collect_nodes(syntax, lambda n: str(n.Kind) == 'ProjectOperator')[0]
print([str(e.Kind) for e in iter_elements(project.Expressions)])
"
```

Expected: `['NameReference', 'NameReference']`.

- [ ] **Step 4: Run the full suite one final time, across locales**

```bash
.venv/bin/python -m pytest -q
LANG=de-DE .venv/bin/python -m pytest -q
LANG=fr-FR .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests examples scripts
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document syntax-tree traps and the release's breaking changes

Six traps that cost a downstream consumer real time, in their corrected
form — four of the nine originally reported turned out to be misreadings
of the API rather than defects in it."
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: C1 → Task 1; C2 and G4 → Task 3; G1 → Task 4; unwrap export → Task 2; `get_time_range` rename → Task 5; trap documentation → Task 6. The spec's refuted claims (G2, G5, generic `BinaryExpression`, raw `node.Kind`) produce no code changes and are covered by Task 6's README section, as the spec requires. The spec's testing section maps to the test files created in Tasks 1-5, including the "verify the guard works with the pin reverted" step (Task 1 Step 7) and the baseline refresh (Task 3 Step 9).

**Type consistency.** `iter_elements` is defined in Task 2 and consumed under its private alias `_iter_elements` in Task 4; both names are stated in Task 2's Interfaces block. `LiteralExpr.ticks` is defined in Task 3 and asserted in Task 4's `test_scalar_binding_populates_rhs_expr`. `LetFunction.parameters` / `.body_span` are used consistently in the model, the builder and the tests. `find_time_expressions` has the same signature at module and method level.

**Ordering.** Task 2 precedes Task 4 because `_visit_function_declaration` needs the unwrap helper. Task 3 precedes Task 4 because a let-binding test asserts `literal_kind == "timespan"` and `ticks`. Task 1 is independent but goes first: it is a live data-corruption bug.
