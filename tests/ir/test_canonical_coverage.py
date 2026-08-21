# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Meta-test guarding `_normalize.canonical()` against a silent fallthrough.

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
passes today over all 22 non-`UnknownExpr` subclasses; it exists so that
adding a 23rd subclass without touching `canonical()` fails CI instead of
shipping a builder that renders the new shape as its raw source text.
"""

import inspect

from kustology.ir import expr as E
from kustology.ir._normalize import canonical


def _all_expr_subclasses(cls: type = E.Expr) -> set[type]:
    out: set[type] = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out |= _all_expr_subclasses(sub)
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
