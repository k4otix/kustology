# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every .NET member name the library probes must exist in the assembly.

pythonnet resolves members by exact, case-sensitive name and says nothing
when one is absent: ``getattr(node, "Uris", None)`` on a node whose member
is ``URIs`` returns ``None``, the guard around it declines, and the field it
would have populated keeps its declared default. No exception, no log line,
no failing test — the surface reads as implemented forever.

Four separate defects shipped that way and were found together:

===========================  ==========================================
probe                        reality
===========================  ==========================================
``node.Uris``                the member is ``URIs``
``res_type.IsNullable``      a property on **no** type in the assembly
``res_type.Underlying``      a property on **no** type in the assembly
``n.Keys`` then ``.Count``   ``Keys`` is a ``RowSchema``; no ``Count``
===========================  ==========================================

This test closes most of the class rather than the four instances. It
parses ``src/kustology/`` for every PascalCase member name passed to
``getattr`` or ``hasattr`` and asserts each one resolves somewhere in
``Kusto.Language``.

Its limit, stated so nobody over-trusts it: the check is per name, not per
type. The fourth defect above probed ``Keys`` (which exists) for ``Count``
(which exists on plenty of other types, just not on ``RowSchema``), so a
name-level check cannot see it. Catching that needs the value assertion the
individual tests make -- a field asserted non-default on a real parse. Both
are needed; neither subsumes the other.

Verified to bite: reintroducing ``Uris``, ``IsNullable`` or ``Underlying``
turns this red.

Tier 1: no pydantic import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kustology  # noqa: F401  -- loads the CLR and the bundled assembly

SRC = Path(__file__).resolve().parent.parent / "src" / "kustology"

# Names that resolve against something other than a Kusto.Language type --
# System.Globalization, System.Exception, and so on. Each entry needs a
# reason: an unexplained entry re-opens the hole this test closes.
#
# Empty today, and that is the healthy state. Everything the library probes
# is a real Kusto.Language member.
ALLOWED_ELSEWHERE: dict[str, str] = {}


def _probed_member_names() -> dict[str, list[str]]:
    """{member name: [file:line, ...]} for every getattr/hasattr probe."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ("getattr", "hasattr") or len(node.args) < 2:
                continue
            arg = node.args[1]
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                continue
            name = arg.value
            # .NET members are PascalCase. Python attributes on our own
            # objects are snake_case (skipped) or SCREAMING_CASE for the
            # ClassVar discriminators like ``KIND`` (also skipped).
            if not name[:1].isupper() or name.isupper():
                continue
            found.setdefault(name, []).append(
                f"{path.relative_to(SRC.parent.parent)}:{node.lineno}"
            )
    return found


@pytest.fixture(scope="module")
def assembly_members() -> set[str]:
    import System
    from Kusto.Language.Symbols import ScalarTypes

    asm = System.Reflection.Assembly.GetAssembly(ScalarTypes.Long.GetType())
    names: set[str] = set()
    for t in asm.GetTypes():
        # All 913 types in the bundled assembly reflect cleanly; a bare
        # loop here means a type that stops doing so fails loudly rather
        # than quietly shrinking the set this test checks against.
        names.update(prop.Name for prop in t.GetProperties())
        names.update(m.Name for m in t.GetMethods())
        names.update(f.Name for f in t.GetFields())
    return names


def test_probed_dotnet_members_exist(assembly_members):
    probes = _probed_member_names()
    assert probes, "found no getattr/hasattr probes — the AST scan is broken"

    bogus = {
        name: sites for name, sites in probes.items()
        if name not in assembly_members and name not in ALLOWED_ELSEWHERE
    }
    assert not bogus, (
        "these member names resolve to nothing in Kusto.Language, so every "
        "guard around them silently declines and the field they would fill "
        "keeps its default:\n"
        + "\n".join(f"  {n}  at {', '.join(s)}" for n, s in sorted(bogus.items()))
    )


def test_allowlist_has_no_stale_entries(assembly_members):
    """An allowlist entry that is no longer probed, or that now resolves in
    the assembly anyway, is dead weight that hides the next real miss."""
    probed = set(_probed_member_names())
    stale = {
        name for name in ALLOWED_ELSEWHERE
        if name not in probed or name in assembly_members
    }
    assert not stale, f"remove these from ALLOWED_ELSEWHERE: {sorted(stale)}"
