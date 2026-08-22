# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Meta-tests guarding the IR's three hand-maintained class lists.

`canonical()`'s dispatch chain, `expr.AnyExpr` and `ir.__all__` are each a
list of class names written out by hand, and all three go stale the same
way: a model lands and only one of them is updated. Every test here rebuilds
its list by introspection and diffs it against the written one, so the drift
fails CI instead of shipping. None of them writes a count or a class list
down — that is the drift they exist to catch.

## `canonical()`

`canonical()` is a long if/elif chain over `Expr` subclasses, ending in a
`raw_text` fallback for anything it doesn't recognize. That fallback exists
so `canonical()` never crashes on an unmodelled shape, but it means a NEW
`Expr` subclass added later without a matching branch renders as garbage
(the .NET node's raw text) instead of failing loudly -- and `canonical()`
feeds both `Expr.canonical_form` and `semantic_hash`, so two semantically
different queries could silently collapse to the same hash.

This test does not exercise `canonical()`'s behaviour -- the per-type tests
already do that (see `test_literals.py`, etc). It inspects `canonical()`'s
*source* and asserts every live `Expr` subclass name appears somewhere in
the function body. That's a coarse check (a name appearing in a comment
would also pass), but it is exactly calibrated to catch what matters here:
a class added to `expr.py` and never added to `canonical()` at all. It
covers every non-`UnknownExpr` subclass there is, discovered by walking
`Expr.__subclasses__()` rather than listed here, so that adding one more
without touching `canonical()` fails CI instead of shipping a builder that
renders the new shape as its raw source text.

The count is deliberately not written down. It used to say "all 22 ... adding
a 23rd" and went stale the first time a subclass landed (`TypedNameDecl`, then
`LetValueRef`), which is the hand-maintained-list drift AGENTS.md warns about
-- in a docstring whose whole subject is a list that must not be
hand-maintained.

## `AnyExpr` and `__all__`

The same drift, two more places, and both are quieter than a bad render.

A subclass missing from `AnyExpr` is missing from every *field* typed
`AnyExpr` -- which is nearly every expression-holding field in the IR. The
builder can still construct the node in memory, so nothing fails at build
time; the break surfaces one layer out, at
`QueryIR.model_validate_json(ir.model_dump_json())`, where pydantic has no
member of the union whose `kind` literal matches and rejects the dump the
library itself wrote. So the gap ships as "stored IR from this version does
not load", not as a missing feature.

A class missing from `__all__` (or from the module's imports) is simply not
importable as `from kustology.ir import X`, which for a node the builder
puts in the tree means a consumer cannot name the type it is being handed --
no `isinstance` check, no `find_all(ir, X)`. WS4 added nine model classes
across nine tasks; the risk this test closes is the tenth one landing with
its `__init__` line forgotten.

Both are checked by introspection over the live modules, so a new class is
covered the moment it is defined. `Expr` itself is included deliberately:
it is a member of `AnyExpr` (the permissive tail of the union) and is
exported, and a base class that is legal to construct is legal to serialize.
"""

import importlib
import inspect
import pkgutil
import typing

import pydantic

import kustology.ir as ir_pkg
from kustology.ir import expr as E
from kustology.ir._normalize import canonical


def _all_expr_subclasses(cls: type = E.Expr) -> set[type]:
    out: set[type] = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _all_expr_subclasses(sub)
    return out


def _anyexpr_member_names() -> set[str]:
    """The class names named by `expr.AnyExpr`, forward references included.

    Members are written as string literals (the union is defined before the
    classes it names), so `get_args` hands back a mix of `ForwardRef` and --
    once pydantic's `model_rebuild` has resolved them -- real classes.
    Normalize both to a bare name.
    """
    out: set[str] = set()
    for arg in typing.get_args(E.AnyExpr):
        name = getattr(arg, "__forward_arg__", None) or getattr(arg, "__name__", None)
        assert name is not None, f"unrecognized AnyExpr member: {arg!r}"
        out.add(name)
    return out


def _public_models() -> set[type]:
    """Every public pydantic model defined anywhere in `kustology.ir`.

    Filtered by `__module__` so a class a module merely imported (the `expr`
    classes `query` uses in its own annotations) is attributed to the module
    that defines it and counted exactly once.

    The submodule list comes from `pkgutil`, not from a pair of imports.
    Naming `expr` and `query` by hand made this function the very thing the
    module docstring rails against -- a hand-maintained list -- and it was
    already incomplete: it saw 99 models where the package defines 101,
    missing `Span` (in `spans`) and `Finding` (in `analyzers`). Both are
    exported, so the guard could not have caught either one going missing.
    A model in a submodule nobody thought to import is exactly the case
    this test exists for.
    """
    out: set[type] = set()
    for info in pkgutil.iter_modules(ir_pkg.__path__):
        mod = importlib.import_module(f"{ir_pkg.__name__}.{info.name}")
        for name, obj in vars(mod).items():
            if (
                not name.startswith("_")
                and inspect.isclass(obj)
                and issubclass(obj, pydantic.BaseModel)
                and obj.__module__ == mod.__name__
            ):
                out.add(obj)
    return out


def test_every_expr_subclass_has_a_render_branch():
    src = inspect.getsource(canonical)
    missing = sorted(
        c.__name__
        for c in _all_expr_subclasses()
        if c.__name__ not in src and c is not E.UnknownExpr
    )
    # UnknownExpr is deliberately exempt: it is rendered by the `raw_text`
    # fallthrough by design (see `_builder_helpers` docstrings on
    # Unknown*), not by omission. Every other subclass must be named.
    assert missing == [], f"canonical() has no branch for: {missing}"


def test_every_expr_subclass_is_a_member_of_any_expr():
    members = _anyexpr_member_names()
    missing = sorted(c.__name__ for c in _all_expr_subclasses() if c.__name__ not in members)
    assert missing == [], (
        f"expr.AnyExpr does not name: {missing}. Every field typed AnyExpr "
        f"rejects these nodes on the way back in, so IR containing one dumps "
        f"but does not reload -- QueryIR.model_validate_json raises because "
        f"no member of the union carries their `kind` literal."
    )


def test_every_expr_subclass_is_exported():
    exported = set(ir_pkg.__all__)
    # `Expr` is the base and is exported alongside its subclasses.
    classes = _all_expr_subclasses() | {E.Expr}
    missing = sorted(c.__name__ for c in classes if c.__name__ not in exported)
    assert missing == [], (
        f"kustology.ir.__all__ does not export: {missing}. The builder puts "
        f"these nodes in the tree, so a consumer needs the name to isinstance "
        f"or find_all against them."
    )


def test_every_ir_model_is_exported():
    """The same guard, widened past `Expr` to every model in the package.

    `AnyExpr` membership only constrains expression nodes, and WS4's new
    classes were mostly *not* expressions -- `SortKey`, `ReorderKey`,
    `ForkBranch`, `MvExpandColumn`, `MakeSeriesAggregate`, `DataTableSource`,
    `ExternalDataSource` are plain `BaseModel`s hanging off an operator or a
    pipeline source. Nothing but this test would have noticed one of them
    missing from `__all__`: the IR builds, dumps and reloads perfectly, and
    only a consumer trying to import the name finds out.
    """
    exported = set(ir_pkg.__all__)
    models = _public_models()
    missing = sorted(c.__name__ for c in models if c.__name__ not in exported)
    assert missing == [], f"kustology.ir.__all__ does not export: {missing}"


def test_everything_exported_resolves():
    """The reverse direction: no `__all__` entry without an import behind it.

    `from kustology.ir import *` raises `AttributeError` on a name in
    `__all__` that the module never bound -- a class added to the list and
    not to the import block above it. Cheap to get wrong, and invisible to
    every test that imports names explicitly.
    """
    dangling = sorted(n for n in ir_pkg.__all__ if not hasattr(ir_pkg, n))
    assert dangling == [], f"kustology.ir.__all__ names unbound attributes: {dangling}"


def test_the_binder_enricher_alias_is_gone():
    """``BinderEnricher = SchemaAttacher`` was a bare assignment in `binder.py`,
    re-exported through `kustology.ir.__all__`, and never documented — no
    mention in the README, the CHANGELOG, an example or a test.

    An exported name is a promise: it turns up in `dir()`, in generated API
    documentation and in `from kustology.ir import *`, and once a consumer
    imports it the alias has to be kept working across the 1.0 line. Nothing
    was ever written down about which of the two names was the real one, so
    the pair only cost — two spellings of one class in every search result.
    Removed before 0.2.0 rather than documented, because the class that does
    the work already has the name that says what it does.
    """
    from kustology.ir import binder

    assert not hasattr(ir_pkg, "BinderEnricher")
    assert "BinderEnricher" not in ir_pkg.__all__
    assert not hasattr(binder, "BinderEnricher")

    # The control: the name that survives still resolves to the same class,
    # so this is a deletion of a duplicate and not of the functionality.
    assert ir_pkg.SchemaAttacher is binder.SchemaAttacher
    assert "SchemaAttacher" in ir_pkg.__all__
