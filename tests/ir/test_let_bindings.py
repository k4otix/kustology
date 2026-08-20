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
