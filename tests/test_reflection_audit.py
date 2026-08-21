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

* An attribute chain rooted at a name **imported in that same file** is a
  module or type path, not an instance member read — ``argparse.Namespace``,
  ``System.Reflection.Assembly``, ``CultureInfo.InvariantCulture``,
  ``KustoCode.ParseAndAnalyze``. The chain is walked through ``ast.Attribute``
  links only, so a member read off a *call result* is still checked:
  ``TableSymbol.From(cols).WithName(...)`` is excluded at ``From`` and
  checked at ``WithName``.
* Annotation subtrees are skipped — an annotation names types, not members.
* SCREAMING_CASE is skipped, matching the ``getattr`` scan: it is this
  repo's ``ClassVar`` discriminator convention (``KIND``), not .NET's
  member convention.

Its limit, stated so nobody over-trusts it: the check is per name, not per
type. The fourth defect above probed ``Keys`` (which exists) for ``Count``
(which exists on plenty of other types, just not on ``RowSchema``), so a
name-level check cannot see it — and for the same reason it does not see
``PartitionByOperator.Expression``, where ``Expression`` is a real member
of dozens of other nodes. Catching those needs the value assertion the
individual tests make: a field asserted non-default on a real parse. Both
are needed; neither subsumes the other.

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


# The .NET objects this library reads members off. Mostly Kusto.Language
# syntax and symbol nodes, plus the boxed BCL primitives that
# ``LiteralExpression.LiteralValue`` hands back -- ``_builder_helpers``
# reads ``.Ticks`` off the DateTime and TimeSpan it returns. Keep this to
# types whose members are actually read: every type added widens the set of
# names that pass unnoticed.
_INTEROP_TYPES = ("System.DateTime", "System.TimeSpan")


@pytest.fixture(scope="module")
def dotnet_members() -> set[str]:
    import System
    from Kusto.Language.Symbols import ScalarTypes

    asm = System.Reflection.Assembly.GetAssembly(ScalarTypes.Long.GetType())
    types = list(asm.GetTypes())
    for name in _INTEROP_TYPES:
        t = System.Type.GetType(name)
        assert t is not None, f"{name} did not resolve — _INTEROP_TYPES is stale"
        types.append(t)

    names: set[str] = set()
    for t in types:
        # All 913 types in the bundled assembly reflect cleanly; a bare
        # loop here means a type that stops doing so fails loudly rather
        # than quietly shrinking the set this test checks against.
        names.update(prop.Name for prop in t.GetProperties())
        names.update(m.Name for m in t.GetMethods())
        names.update(f.Name for f in t.GetFields())
    return names


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

    ``URIs`` is only ever reached through ``getattr`` -- it is the original
    defect this file was written for -- and ``Entity`` only as a direct
    attribute, in the ``__partitionby`` branch whose crash motivated the
    direct-access scan. One landmark per collector; the point is that each
    style of read reaches the audit, not the particular names."""
    by_style: dict[str, set[str]] = {"getattr": set(), "direct": set()}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        by_style["getattr"].update(n for n, _ in _getattr_probe_names(tree))
        collector = _DirectMemberAccessCollector(_imported_names(tree))
        collector.visit(tree)
        by_style["direct"].update(n for n, _ in collector.hits)

    assert "URIs" in by_style["getattr"] and "URIs" not in by_style["direct"]
    assert "Entity" in by_style["direct"] and "Entity" not in by_style["getattr"]


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
