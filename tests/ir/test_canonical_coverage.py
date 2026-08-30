# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Meta-tests guarding the IR's three hand-maintained class lists.

`canonical()`'s dispatch chain, `expr.AnyExpr` and `ir.__all__` each name
classes by hand, and each goes stale the same way: a model lands and only
one of them is updated. Every test here rebuilds its list by introspection
and diffs it against the written one. None writes down a count or a class
list, since that is the drift they catch.

## `canonical()`

`canonical()` is an if/elif chain over `Expr` subclasses ending in a
`raw_text` fallback, so it never crashes on an unmodeled shape. The cost is
that a new `Expr` subclass with no branch renders as the .NET node's raw
text, and since `canonical()` feeds both `Expr.canonical_form` and
`semantic_hash`, two semantically different queries can collapse to one hash.

The per-type tests cover what `canonical()` renders (see `test_literals.py`
and its siblings). This test inspects its *source* and asserts every live
`Expr` subclass name appears in the function body. A name inside a comment
would also pass, which is coarse but enough for the case that matters: a
class added to `expr.py` and never added to `canonical()`. Subclasses come
from walking `Expr.__subclasses__()`, so a new one is covered as soon as it
is defined.

## `AnyExpr` and `__all__`

A subclass missing from `AnyExpr` is missing from every *field* typed
`AnyExpr`, which is nearly every expression-holding field in the IR. The
builder still constructs the node in memory, so nothing fails at build time.
The break surfaces at `QueryIR.model_validate_json(ir.model_dump_json())`,
where no member of the union carries a matching `kind` literal and pydantic
rejects the dump the library itself wrote. The gap ships as "stored IR from
this version does not load".

A class missing from `__all__`, or from the module's imports, is not
importable as `from kustology.ir import X`. For a node the builder puts in
the tree, a consumer then cannot name the type it is handed: no `isinstance`
check, no `find_all(ir, X)`. The case this closes is a new model class
landing with its `__init__` line forgotten.

`Expr` itself is included: it is a member of `AnyExpr` (the permissive tail
of the union) and is exported, and a base class that is legal to construct
is legal to serialize.
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
    """Return the class names `expr.AnyExpr` names, forward references included.

    `AnyExpr` is `Annotated[Union[...], Field(discriminator="kind")]`, so
    `get_args` hands back the `Union` and the `FieldInfo` metadata. Strip the
    `Annotated` wrapper first. The union is defined before the classes it
    names, so its members are string literals and `get_args` yields a mix of
    `ForwardRef` and, once pydantic's `model_rebuild` resolves them, real
    classes. Normalize both to a bare name.
    """
    annotation = E.AnyExpr
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    out: set[str] = set()
    for arg in typing.get_args(annotation):
        name = getattr(arg, "__forward_arg__", None) or getattr(arg, "__name__", None)
        assert name is not None, f"unrecognized AnyExpr member: {arg!r}"
        out.add(name)
    return out


def _public_models() -> set[type]:
    """Return every public pydantic model defined anywhere in `kustology.ir`.

    The `__module__` filter attributes a class a module merely imported (the
    `expr` classes `query` names in its own annotations) to the module that
    defines it, so each is counted once. `pkgutil` supplies the submodule
    list, which is what reaches a model nobody thought to import: `Span`
    lives in `spans` and `Finding` in `analyzers` rather than in the obvious
    `expr`/`query` pair.
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
    # UnknownExpr is exempt: the `raw_text` fallthrough is how it renders
    # (see the `_builder_helpers` docstrings on Unknown*). Every other
    # subclass must be named.
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

    `AnyExpr` membership constrains only expression nodes, and many models
    are not expressions: `SortKey`, `ReorderKey`, `ForkBranch`,
    `MvExpandColumn`, `MakeSeriesAggregate`, `DataTableSource`, and
    `ExternalDataSource` are plain `BaseModel`s hanging off an operator or a
    pipeline source. Nothing but this test notices one missing from
    `__all__`: the IR builds, dumps, and reloads, and only a consumer trying
    to import the name finds out.
    """
    exported = set(ir_pkg.__all__)
    models = _public_models()
    missing = sorted(c.__name__ for c in models if c.__name__ not in exported)
    assert missing == [], f"kustology.ir.__all__ does not export: {missing}"


def test_everything_exported_resolves():
    """The reverse direction: no `__all__` entry without an import behind it.

    `from kustology.ir import *` raises `AttributeError` on a name in
    `__all__` that the module never bound, such as a class added to the list
    and not to the import block above it. Every test that imports names
    explicitly misses this.
    """
    dangling = sorted(n for n in ir_pkg.__all__ if not hasattr(ir_pkg, n))
    assert dangling == [], f"kustology.ir.__all__ names unbound attributes: {dangling}"


def test_the_binder_enricher_alias_is_gone():
    """No ``BinderEnricher`` alias for ``SchemaAttacher`` exists.

    An exported name is a promise: it turns up in `dir()`, in generated API
    documentation, and in `from kustology.ir import *`, and once a consumer
    imports it the alias has to keep working across the 1.0 line. A second
    spelling would put two names for one class in every search result, with
    nothing written down about which one is real.
    """
    from kustology.ir import binder

    assert not hasattr(ir_pkg, "BinderEnricher")
    assert "BinderEnricher" not in ir_pkg.__all__
    assert not hasattr(binder, "BinderEnricher")

    # The control: the class exists in `kustology.ir.binder` as an internal,
    # so this guard is about a duplicate name. Its own non-export is guarded
    # by test_schema_attacher_is_not_exported.
    assert inspect.isclass(binder.SchemaAttacher)


def test_schema_attacher_is_not_exported():
    """``SchemaAttacher`` stays off the public surface.

    The only production caller is ``KustoQuery.to_ir`` (``core.py``), which
    always holds the ``KustoCode`` it parsed, and no README snippet, example,
    or document constructs the class directly. The supported paths are
    ``parse(query, schema=...)`` and ``KustoQuery.to_ir(attach_schema=...)``,
    so the class stays internal in ``kustology.ir.binder`` where ``to_ir``
    and the binder tests reach it.
    """
    from kustology.ir import binder

    assert not hasattr(ir_pkg, "SchemaAttacher")
    assert "SchemaAttacher" not in ir_pkg.__all__

    # The control: `KustoQuery.to_ir` runs the class internally, so only
    # the export is absent.
    assert inspect.isclass(binder.SchemaAttacher)
