# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every .NET member name the library reads must exist in the assembly.

pythonnet resolves members by exact, case-sensitive name, and a misspelling
fails one of two ways depending on how the read is written.

``getattr(node, "Uris", None)`` on a node whose member is ``URIs`` returns
``None``, so the guard around it takes its fallback path and the field it
would have populated keeps its declared default, with no exception and no
failing test. The shapes that miss this way:

===========================  ==========================================
probe                        reality
===========================  ==========================================
``node.Uris``                the member is ``URIs``
``res_type.IsNullable``      a property on **no** type in the assembly
``res_type.Underlying``      a property on **no** type in the assembly
``n.Keys`` then ``.Count``   ``Keys`` is a ``RowSchema``; no ``Count``
===========================  ==========================================

Direct attribute access, ``n.ValueExpression``, raises ``AttributeError``
out of whatever public API the caller invoked, so a builder branch written
that way crashes ``to_ir()`` on valid KQL (``T | top-hitters 5 of a by b``,
``T | __partitionby a (take 1)``) while its kind sits in
``HANDLED_OPERATOR_KINDS`` claiming to be modeled.

This test closes most of both classes. It parses ``src/kustology/`` for
every PascalCase member name passed to ``getattr``/``hasattr`` or read as a
direct attribute, and asserts each resolves on a type this library interops
with.

Direct attribute access needs exclusions, because ``x.Foo`` is also
ordinary Python. They are structural, so nothing needs hand maintenance as
the source grows:

* A **pure attribute chain rooted at a name that file's own imports bind**
  is dropped in whole: ``argparse.Namespace``,
  ``System.Threading.Thread.CurrentThread.CurrentCulture``,
  ``CultureInfo.InvariantCulture``, ``KustoCode.ParseAndAnalyze``. A
  namespace segment and a member read are the same ``ast.Attribute`` node,
  and telling them apart needs name resolution this scan does not do.
* Annotation subtrees are skipped — an annotation names types, not members.
* SCREAMING_CASE is skipped, matching the ``getattr`` scan: it is this
  repo's ``ClassVar`` discriminator convention (``KIND``), not .NET's
  member convention.

Two limits bound what a pass here proves:

* **A chain rooted at an import loses real member reads along with the
  namespace segments.** ``GlobalState.Default.WithDatabase(db)`` in
  ``utils/schema_state.py`` loses both ``Default`` (a static property read)
  and ``WithDatabase`` (an instance member read off its result), because
  ``GlobalState`` is imported and nothing breaks the chain. A ``Call`` does
  break it: ``TableSymbol.From(cols).WithName(name)`` is dropped at ``From``
  and **checked** at ``WithName``, since a call result is an instance.
  ``KustoCode.Parse``/``.ParseAndAnalyze`` in ``services.py`` and
  ``builder.py``, ``DateTime.SpecifyKind`` in ``_builder_helpers.py``, and
  the culture pins in ``bridge.py`` drop the same way. All of them run
  eagerly on a hot path (module import, ``parse()``, or the literal lowering
  every IR build runs), so a typo there fails the suite loudly on the next
  run. This test exists for the rare operator branches no fixture covers,
  the shape that lets a ``ValueExpression`` reach a release.
* **The check is per name, not per type.** The ``Keys``/``Count`` shape
  above probes ``Keys`` (which exists) for ``Count`` (which exists on other
  types but not on ``RowSchema``), so a name-level check cannot see it, and
  for the same reason it misses ``PartitionByOperator.Expression``, where
  ``Expression`` is a real member of dozens of other nodes. Catching those
  needs the value assertion the individual tests make: a field asserted
  non-default on a real parse.

Verified to bite: writing ``Uris``, ``IsNullable``, ``Underlying`` or
``n.ValueExpression`` into ``src/`` turns this red.

Tier 1: no pydantic import.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import kustology  # noqa: F401  -- loads the CLR and the bundled assembly

SRC = Path(__file__).resolve().parent.parent / "src" / "kustology"

# .NET members are PascalCase: an initial capital and at least one more
# character. This library's own attributes are snake_case, except the
# SCREAMING_CASE ``ClassVar`` discriminators like ``KIND``, which match this
# pattern and are excluded separately.
_PASCAL_MEMBER = re.compile(r"^[A-Z][A-Za-z0-9]+$")

# Names that resolve against something no type below exposes. Each entry
# needs a reason; an unexplained entry re-opens the hole this test closes.
# Empty is the healthy state. A read that lands on a .NET type outside the
# bundled assembly belongs in ``_INTEROP_TYPES`` below, which names the
# object the library touches.
ALLOWED_ELSEWHERE: dict[str, str] = {}


def _is_dotnet_member_name(name: str) -> bool:
    return bool(_PASCAL_MEMBER.match(name)) and not name.isupper()


def _imported_names(tree: ast.Module) -> set[str]:
    """Every name this file's own imports bind at module or function scope.

    ``import a.b.c`` binds ``a``; ``import a.b as x`` binds ``x``;
    ``from m import Y as z`` binds ``z``. Function-local imports count --
    ``_builder_helpers`` imports ``CultureInfo`` inside the function that
    uses it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _attribute_chain_root(node: ast.Attribute) -> str | None:
    """The leftmost ``ast.Name`` of a pure attribute chain, else ``None``.

    Only ``ast.Attribute`` links are followed. ``a.B.C`` roots at ``a``;
    ``f().B`` roots at nothing, because its base is a call result, an
    instance whose members this test exists to check.
    """
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


class _DirectMemberAccessCollector(ast.NodeVisitor):
    """Collect PascalCase ``ast.Attribute`` reads that are .NET member reads."""

    def __init__(self, imported: set[str]) -> None:
        self.imported = imported
        self.hits: list[tuple[str, int]] = []

    def _visit_fields_except(self, node: ast.AST, *skip: str) -> None:
        for field, value in ast.iter_fields(node):
            if field in skip:
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            elif isinstance(value, ast.AST):
                self.visit(value)

    # An annotation names types, not members: ``x: System.DateTime`` is not
    # a read of a ``DateTime`` member off ``System``.
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_fields_except(node, "returns")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_fields_except(node, "annotation")

    def visit_arg(self, node: ast.arg) -> None:
        self._visit_fields_except(node, "annotation")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root = _attribute_chain_root(node)
        if root is not None and root in self.imported:
            return          # a module/type path, not an instance member read
        if _is_dotnet_member_name(node.attr):
            self.hits.append((node.attr, node.lineno))
        self.visit(node.value)


def _getattr_probe_names(tree: ast.Module) -> list[tuple[str, int]]:
    """``getattr(node, "Member", …)`` / ``hasattr(node, "Member")`` names."""
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("getattr", "hasattr") or len(node.args) < 2:
            continue
        arg = node.args[1]
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            continue
        if _is_dotnet_member_name(arg.value):
            hits.append((arg.value, node.lineno))
    return hits


def _probed_member_names() -> dict[str, list[str]]:
    """{member name: [file:line, ...]} for every .NET member read in ``src/``."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        direct = _DirectMemberAccessCollector(_imported_names(tree))
        direct.visit(tree)
        for name, lineno in _getattr_probe_names(tree) + direct.hits:
            found.setdefault(name, []).append(
                f"{path.relative_to(SRC.parent.parent)}:{lineno}"
            )
    return found


# Not every member this library reads belongs to a Kusto.Language type.
# ``LiteralExpression.LiteralValue`` hands back a boxed BCL primitive, and
# ``_builder_helpers`` reads members off it. Naming the *types* the library
# touches is honest but loose: admitting one admits every name on it, most
# of which the bundled assembly does not have.
#
# So the widening is gated by its own footprint. ``BCL_ONLY_MEMBERS`` pins
# the probes that ``_INTEROP_TYPES`` -- and nothing else -- explains, and
# ``test_bcl_footprint_is_exactly_justified`` asserts set *equality* against
# what the scan finds, giving the widening the "no stale entries" property
# ``ALLOWED_ELSEWHERE`` has. A new read only a BCL type explains must be
# declared here with its reason, an entry whose last probe disappears must
# be removed, and a wrong-member read one of these types happens to explain
# fails instead of passing unnoticed.
_INTEROP_TYPES = (
    "System.DateTime",
    "System.TimeSpan",
    "System.Object",
    "System.Globalization.CultureInfo",
    "System.Threading.Thread",
)

BCL_ONLY_MEMBERS: dict[str, str] = {
    "Ticks": "System.DateTime/TimeSpan — the lossless form of a datetime or "
             "timespan literal, read off LiteralValue in _builder_helpers.",
    "ToUniversalTime": "System.DateTime — normalizes a Local-kind datetime "
                       "literal to UTC before it is rendered or hashed.",
    "CurrentCulture": "System.Threading.Thread — the culture Kusto reads a "
                      "LiteralValue against; bridge.ensure_invariant_culture "
                      "checks and repairs it.",
    "DefaultThreadCurrentCulture": "System.Globalization.CultureInfo — the "
                                   "culture threads created later inherit, "
                                   "reassigned alongside CurrentCulture.",
    "ReferenceEquals": "System.Object — identifies the InvariantCulture "
                       "singleton. A clone of it compares equal by name "
                       "while parsing differently, so the check is by "
                       "reference.",
}


def _member_names(types) -> set[str]:
    names: set[str] = set()
    for t in types:
        # Every type in the bundled assembly reflects cleanly, so a bare loop
        # fails loudly instead of shrinking the set this test checks against.
        names.update(prop.Name for prop in t.GetProperties())
        names.update(m.Name for m in t.GetMethods())
        names.update(f.Name for f in t.GetFields())
    return names


@pytest.fixture(scope="module")
def assembly_members() -> set[str]:
    """Every member name in the bundled Kusto.Language assembly."""
    import System
    from Kusto.Language.Symbols import ScalarTypes

    asm = System.Reflection.Assembly.GetAssembly(ScalarTypes.Long.GetType())
    return _member_names(asm.GetTypes())


@pytest.fixture(scope="module")
def dotnet_members(assembly_members) -> set[str]:
    """The assembly plus the BCL types the library reads members off."""
    import System

    extra = []
    for name in _INTEROP_TYPES:
        t = System.Type.GetType(name)
        assert t is not None, f"{name} did not resolve — _INTEROP_TYPES is stale"
        extra.append(t)
    return assembly_members | _member_names(extra)


def test_bcl_footprint_is_exactly_justified(assembly_members):
    """``_INTEROP_TYPES`` may only admit names something actually reads.

    Widening the universe by a type is unenforced on its own:
    ``System.DateTime`` and ``System.TimeSpan`` bring in ``Date``, ``Day``,
    ``Days``, ``Duration``, ``Hours``, ``MaxValue`` and the whole
    ``Add*``/``From*`` family, none of which the bundled assembly has and
    each a plausible *wrong* member read on a Kusto syntax node that would
    then pass. Pinning the footprint as an exact set holds the admitted
    surface at the names the library genuinely reads.

    Equality makes this stale-proof in both directions: a new BCL read must
    be declared here with its reason, and an entry whose last probe
    disappears must be removed. An unused type in ``_INTEROP_TYPES``
    pre-admits nothing, because the first probe to land on one of its names
    still has to be declared and justified."""
    outside = {
        name: sites for name, sites in _probed_member_names().items()
        if name not in assembly_members
    }
    assert set(outside) == set(BCL_ONLY_MEMBERS), (
        "the set of probed members the bundled assembly does not explain has "
        "changed. Add a BCL_ONLY_MEMBERS entry naming the type and why, or "
        "drop the entry that no longer has a probe:\n"
        + "\n".join(f"  {n}  at {', '.join(s)}" for n, s in sorted(outside.items()))
        + f"\n  declared: {sorted(BCL_ONLY_MEMBERS)}"
    )


def test_probed_dotnet_members_exist(dotnet_members):
    probes = _probed_member_names()
    assert probes, "found no member reads at all — the AST scan is broken"

    bogus = {
        name: sites for name, sites in probes.items()
        if name not in dotnet_members and name not in ALLOWED_ELSEWHERE
    }
    assert not bogus, (
        "these member names resolve to nothing on any .NET type this "
        "library interops with. Read via getattr/hasattr, the guard around "
        "one silently declines and the field it would fill keeps its "
        "default; read as a direct attribute, it raises AttributeError out "
        "of the public API on valid input:\n"
        + "\n".join(f"  {n}  at {', '.join(s)}" for n, s in sorted(bogus.items()))
    )


def test_both_scans_find_something():
    """A refactor of the visitor or the call matcher can silently disable
    either collector, leaving a green test that checks half of what it
    claims. Each one must contribute.

    Name-free: pinning a landmark member per style would go red the day that
    one read is rewritten in the other style, which has no bearing on
    whether both styles reach the audit. Each collector must find names, and
    each must find one the other does not, so neither can pass on names the
    other supplies."""
    by_style: dict[str, set[str]] = {"getattr": set(), "direct": set()}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        by_style["getattr"].update(n for n, _ in _getattr_probe_names(tree))
        collector = _DirectMemberAccessCollector(_imported_names(tree))
        collector.visit(tree)
        by_style["direct"].update(n for n, _ in collector.hits)

    getattr_only = by_style["getattr"] - by_style["direct"]
    direct_only = by_style["direct"] - by_style["getattr"]
    assert getattr_only, "the getattr/hasattr scan contributes nothing of its own"
    assert direct_only, "the direct-attribute scan contributes nothing of its own"


def test_imported_names_are_excluded_but_call_results_are_not():
    """The exclusion is a rule about attribute *chains*, not a name list.

    ``TableSymbol`` is imported in ``schema_state.py``, so
    ``TableSymbol.From`` is a type path and excluded. The same expression
    continues ``.WithName(name)`` off the call result, a real .NET member
    read that must survive; a name-based rule would drop both."""
    path = SRC / "utils" / "schema_state.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    imported = _imported_names(tree)
    assert "TableSymbol" in imported

    collector = _DirectMemberAccessCollector(imported)
    collector.visit(tree)
    names = {n for n, _ in collector.hits}
    assert "WithName" in names
    assert "From" not in names


def test_allowlist_has_no_stale_entries(dotnet_members):
    """An allowlist entry with no matching probe, or one that already
    resolves in the assembly, is dead weight that hides the next real miss."""
    probed = set(_probed_member_names())
    stale = {
        name for name in ALLOWED_ELSEWHERE
        if name not in probed or name in dotnet_members
    }
    assert not stale, f"remove these from ALLOWED_ELSEWHERE: {sorted(stale)}"


# -- diagnostic codes are also a .NET fact -----------------------------------

def test_unknown_name_codes_match_the_assembly():
    """``_UNKNOWN_NAME_CODES`` is pinned; the DLL decides whether it is right.

    The set is written out rather than reflected at runtime, so the filter is
    a frozenset lookup and every code is greppable. AGENTS.md warns that
    refreshing the DLL "can shift diagnostic codes (KS204 etc.)", and a
    shifted code turns the filter into a silent no-op for one family, so the
    pin is re-derived here from ``Kusto.Language.DiagnosticFacts``. Its
    method *names* say which family a code belongs to:
    ``GetNameDoesNotReferToAnyKnown<X>`` and ``GetFuzzy<X>NotDefined`` both
    mean "this name is not among the things the GlobalState describes".

    A refresh that renumbers a code, adds a name kind, or renames a factory
    fails here with the new set to paste in.
    """
    # The CLR and the bundled assembly are already loaded by this module's
    # top-level ``import kustology``.
    from Kusto.Language import DiagnosticFacts

    from kustology.services import _UNKNOWN_NAME_CODES, _UNKNOWN_TABLE_CODE

    factories = [
        name for name in dir(DiagnosticFacts)
        if name.startswith("GetNameDoesNotReferToAnyKnown")
        or (name.startswith("GetFuzzy") and name.endswith("NotDefined"))
    ]
    assert len(factories) >= 20, "DiagnosticFacts lost its unknown-name family"

    derived = set()
    for name in factories:
        factory = getattr(DiagnosticFacts, name)
        # Arity differs across the family, and only the code matters here.
        for args in (("x",), ("x", "y")):
            try:
                derived.add(str(factory(*args).Code))
                break
            except TypeError:
                continue
        else:  # pragma: no cover — a new arity would need handling
            raise AssertionError(f"could not call DiagnosticFacts.{name}")

    assert derived == set(_UNKNOWN_NAME_CODES)
    # The narrow Tier 1 waiver must stay a member of the wide one, or the two
    # filters disagree about the case they do share.
    assert _UNKNOWN_TABLE_CODE in _UNKNOWN_NAME_CODES
