# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every .NET member name the library reads must exist in the assembly.

pythonnet resolves members by exact, case-sensitive name, and it fails two
different ways depending on how the read is written.

``getattr(node, "Uris", None)`` on a node whose member is ``URIs`` returns
``None``: the guard around it declines, and the field it would have
populated keeps its declared default. No exception, no log line, no failing
test — the surface reads as implemented forever. Four separate defects
shipped that way and were found together:

===========================  ==========================================
probe                        reality
===========================  ==========================================
``node.Uris``                the member is ``URIs``
``res_type.IsNullable``      a property on **no** type in the assembly
``res_type.Underlying``      a property on **no** type in the assembly
``n.Keys`` then ``.Count``   ``Keys`` is a ``RowSchema``; no ``Count``
===========================  ==========================================

Direct attribute access — ``n.ValueExpression`` — is the opposite: it
raises ``AttributeError`` out of whatever public API the caller invoked.
Two builder branches shipped that way, so ``T | top-hitters 5 of a by b``
and ``T | __partitionby a (take 1)`` crashed ``to_ir()`` on valid KQL while
both kinds sat in ``HANDLED_OPERATOR_KINDS`` claiming to be modelled.

This test closes most of both classes rather than the six instances. It
parses ``src/kustology/`` for every PascalCase member name that is either
passed to ``getattr``/``hasattr`` or read as a direct attribute, and
asserts each one resolves on a type this library actually interops with.

Direct attribute access needs exclusions, because ``x.Foo`` is also
ordinary Python. They are structural, so nothing has to be maintained by
hand as the source grows:

* A **pure attribute chain rooted at a name that file's own imports bind**
  is dropped, in whole — ``argparse.Namespace``,
  ``System.Threading.Thread.CurrentThread.CurrentCulture``,
  ``CultureInfo.InvariantCulture``, ``KustoCode.ParseAndAnalyze``. A
  namespace segment and a member read are the same ``ast.Attribute`` node,
  and telling them apart needs name resolution this scan does not do.
* Annotation subtrees are skipped — an annotation names types, not members.
* SCREAMING_CASE is skipped, matching the ``getattr`` scan: it is this
  repo's ``ClassVar`` discriminator convention (``KIND``), not .NET's
  member convention.

Two limits, stated so nobody over-trusts this.

**The import rule drops real member reads, not just namespace segments.**
``GlobalState.Default.WithDatabase(db)`` in ``utils/schema_state.py`` loses
both ``Default`` (a static property read) and ``WithDatabase`` (an instance
member read off its result) — the whole chain goes, because ``GlobalState``
is imported and nothing breaks the chain. A ``Call`` does break it:
``TableSymbol.From(cols).WithName(name)`` is dropped at ``From`` and
**checked** at ``WithName``, since a call result is an instance whose
members are exactly what this test exists to check. The same is true of
``KustoCode.Parse(...)``/``.ParseAndAnalyze(...)`` in ``services.py`` and
``builder.py``, ``DateTime.SpecifyKind(...)`` in ``_builder_helpers.py``,
and the culture pins in ``bridge.py``. What makes that acceptable rather
than a hole: every one of those reads is *eagerly executed* on a hot path —
module import, ``parse()``, or the literal lowering every IR build runs — so
a typo there fails the suite loudly on the next run. The reads this test
exists for are the opposite: rare operator branches that no fixture covered,
which is how ``ValueExpression`` survived to a release.

**The check is per name, not per type.** The fourth defect above probed
``Keys`` (which exists) for ``Count`` (which exists on plenty of other
types, just not on ``RowSchema``), so a name-level check cannot see it —
and for the same reason it does not see ``PartitionByOperator.Expression``,
where ``Expression`` is a real member of dozens of other nodes. Catching
those needs the value assertion the individual tests make: a field asserted
non-default on a real parse. Both are needed; neither subsumes the other.

Verified to bite: reintroducing ``Uris``, ``IsNullable``, ``Underlying`` or
``n.ValueExpression`` turns this red.

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
# character. Python attributes on our own objects are snake_case (no match)
# or SCREAMING_CASE for the ClassVar discriminators like ``KIND``, which
# matches this pattern and is excluded separately.
_PASCAL_MEMBER = re.compile(r"^[A-Z][A-Za-z0-9]+$")

# Names that resolve against something no type below exposes. Each entry
# would need a reason: an unexplained entry re-opens the hole this test
# closes.
#
# Empty today, and that is the healthy state. Everything the library reads
# is a real member of a type it genuinely interops with. When a read turns
# out to land on a .NET type outside the bundled assembly, the honest fix
# is to add that type to ``_INTEROP_TYPES`` below — naming the object the
# library touches — rather than to excuse the member name here.
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
    ``f().B`` roots at nothing, because its base is a call result -- an
    instance -- whose members are exactly what this test exists to check.
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
# touches is more honest than excusing member names -- but it is also
# looser, because adding one type admits everything on it: these two add 136
# names the bundled assembly does not have, of which the library reads two.
#
# So the widening is gated by its own footprint. ``BCL_ONLY_MEMBERS`` pins
# the probes that ``_INTEROP_TYPES`` -- and nothing else -- explains, and
# ``test_bcl_footprint_is_exactly_justified`` asserts set *equality* against
# what the scan actually finds. That gives the widening the same "no stale
# entries" property ``ALLOWED_ELSEWHERE`` has, in both directions: a new
# read that only a BCL type explains must be declared here with its reason,
# an entry whose last probe disappears must be removed, and a wrong-member
# read that one of these types happens to explain fails instead of passing
# unnoticed. The effective admitted surface is these two names, not 136.
_INTEROP_TYPES = ("System.DateTime", "System.TimeSpan")

BCL_ONLY_MEMBERS: dict[str, str] = {
    "Ticks": "System.DateTime/TimeSpan — the lossless form of a datetime or "
             "timespan literal, read off LiteralValue in _builder_helpers.",
    "ToUniversalTime": "System.DateTime — normalizes a Local-kind datetime "
                       "literal to UTC before it is rendered or hashed.",
}


def _member_names(types) -> set[str]:
    names: set[str] = set()
    for t in types:
        # All 913 types in the bundled assembly reflect cleanly; a bare
        # loop here means a type that stops doing so fails loudly rather
        # than quietly shrinking the set this test checks against.
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

    Widening the universe by a type is the honest way to describe interop,
    but on its own it is unenforced: these two types bring in ``Date``,
    ``Day``, ``Days``, ``Duration``, ``Hours``, ``MaxValue`` and the whole
    ``Add*``/``From*`` family, none of which the bundled assembly has and
    every one of them a plausible *wrong* member read on a Kusto syntax node
    that would then pass. Pinning the footprint as an exact set is what
    keeps the admitted surface at the two names the library genuinely reads
    instead of the 136 the two types expose.

    Equality, not containment, so this is stale-proof in both directions: a
    new BCL read must be declared here with its reason, and an entry whose
    last probe disappears must be removed. An unused type left in
    ``_INTEROP_TYPES`` is bounded by the same rule rather than special-cased
    -- it pre-admits nothing, because the first probe that actually lands on
    one of its names still has to be declared here and justified."""
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
    """The two collectors are independent, and either could be silently
    disabled by a refactor of the visitor or the call matcher -- leaving a
    green test that checks half of what it claims. Assert each contributes.

    Deliberately name-free. Pinning a landmark member per style would go red
    the day someone rewrites that one read in the other style -- a change
    with no effect on what this test is actually about, which is that both
    styles reach the audit. What is asserted instead is that each collector
    finds names, and that each finds at least one the other does not, so a
    collector cannot be "passing" on names the other one supplies."""
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
    ``TableSymbol.From`` is a type path and excluded -- but the same
    expression continues ``.WithName(name)`` off the call result, which is
    a real .NET member read and must survive. A rule that excluded by name
    would drop both."""
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
    """An allowlist entry that is no longer probed, or that now resolves in
    the assembly anyway, is dead weight that hides the next real miss."""
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
    a frozenset lookup and every code is greppable. That trades one risk for
    another: AGENTS.md warns that refreshing the DLL "can shift diagnostic
    codes (KS204 etc.)", and a shifted code would turn the filter into a
    silent no-op for one family. So the pin is re-derived here from
    ``Kusto.Language.DiagnosticFacts``, whose method *names* say which
    family each belongs to — ``GetNameDoesNotReferToAnyKnown<X>`` and
    ``GetFuzzy<X>NotDefined`` are, by construction, "this name is not among
    the things the GlobalState describes".

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
        # Arity differs across the family (a graph snapshot names its model
        # too); the message text is irrelevant here, only the code is.
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
