# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Let bindings carry their right-hand side, not just a name and a span."""

import pytest

from kustology import parse
from kustology.ir import LetBinding, LiteralExpr, Pipeline


def _binding(query: str, name: str, schema: dict | None = None) -> LetBinding:
    ir = parse(query, schema=schema).to_ir() if schema else parse(query).to_ir()
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


def test_parenthesized_tabular_binding_populates_rhs_pipeline():
    """`let X = ( T | where … );` — the dominant Sentinel idiom.

    The right-hand side arrives as a ``ParenthesizedExpression`` wrapping the
    ``PipeExpression``. Dispatching on the wrapper's class dropped the entire
    subtree into ``rhs_expr`` as an ``UnknownExpr`` while the unparenthesized
    spelling of the same query worked — the exact "looks implemented, isn't"
    shape this suite exists to catch.
    """
    lb = _binding(
        'let ADFS_Servers = (\n'
        'Event\n'
        '| where Source == "Microsoft-Windows-Sysmon"\n'
        ');\n'
        'ADFS_Servers | count',
        "ADFS_Servers",
    )
    assert isinstance(lb.rhs_pipeline, Pipeline)
    assert lb.inner_tables == ["Event"]
    assert lb.rhs_expr is None
    assert lb.rhs_function is None


def test_parenthesized_and_bare_tabular_bindings_agree():
    """Parentheses are formatting; the two spellings must build the same IR.

    Compared by semantic hash, which strips the spans that legitimately shift
    by one character between the two spellings.
    """
    from kustology.ir import compute_semantic_hash

    bare = _binding("let A = SecurityEvent | where EventID == 1; A | count", "A")
    wrapped = _binding("let A = (SecurityEvent | where EventID == 1); A | count", "A")
    assert bare.inner_tables == wrapped.inner_tables == ["SecurityEvent"]
    assert bare.rhs_pipeline is not None
    assert wrapped.rhs_pipeline is not None
    assert (
        compute_semantic_hash(bare.rhs_pipeline)
        == compute_semantic_hash(wrapped.rhs_pipeline)
    )


def test_nested_parentheses_are_unwrapped():
    lb = _binding("let A = ((SecurityEvent | where EventID == 1)); A | count", "A")
    assert isinstance(lb.rhs_pipeline, Pipeline)
    assert lb.inner_tables == ["SecurityEvent"]


def test_parenthesized_scalar_binding_is_still_scalar():
    """Unwrapping parens must not push a scalar right-hand side into the
    tabular branch — ``_visit_expr`` unwraps them itself."""
    lb = _binding(
        "let m = (toscalar(SecurityEvent | summarize max(EventID))); T | where X == m",
        "m",
    )
    assert type(lb.rhs_expr).__name__ == "ToScalarExpr"
    assert lb.rhs_pipeline is None
    assert lb.inner_tables == []


def test_union_rooted_binding_populates_rhs_pipeline():
    """An operator-rooted right-hand side is tabular with no pipe in sight."""
    lb = _binding("let U = union SigninLogs, AuditLogs; U | count", "U")
    assert isinstance(lb.rhs_pipeline, Pipeline)
    assert lb.inner_tables == ["SigninLogs", "AuditLogs"]
    assert lb.rhs_expr is None


@pytest.mark.parametrize(
    "query,name",
    [
        ("let R = range Step from 1 to 10 step 1; R | count", "R"),
        ('let S = search "boom"; S | count', "S"),
        ("let P = print Answer = 42; P | count", "P"),
        ("let D = datatable(a: int)[1, 2]; D | count", "D"),
        ("let F = find in (SecurityEvent) where EventID == 1; F | count", "F"),
    ],
)
def test_other_operator_rooted_bindings_populate_rhs_pipeline(query, name):
    lb = _binding(query, name)
    assert isinstance(lb.rhs_pipeline, Pipeline), (
        f"{name}: operator-rooted let RHS fell through to "
        f"{type(lb.rhs_expr).__name__}"
    )
    assert lb.rhs_expr is None


def test_bare_materialize_binding_populates_rhs_pipeline():
    """`let X = materialize(P)` — no enclosing PipeExpression to dispatch on."""
    lb = _binding(
        "let Cached = materialize(SecurityEvent | where EventID == 1); Cached | count",
        "Cached",
    )
    assert isinstance(lb.rhs_pipeline, Pipeline)
    assert lb.inner_tables == ["SecurityEvent"]
    assert lb.rhs_expr is None


def test_binder_resolved_table_alias_populates_rhs_pipeline():
    """`let A = OtherTable` is tabular only when the binder proves it.

    With a schema the ``NameReference`` carries a ``TableSymbol``, so the
    binding becomes a pipeline over a ``TableRef``. This branch drives the
    documented ``semantic_hash`` divergence between a bound and an unbound
    parse — see the note on ``transforms._VOLATILE_FIELDS``.
    """
    lb = _binding(
        "let A = OtherTable; A | count",
        "A",
        schema={"OtherTable": {"EventID": "int"}},
    )
    assert isinstance(lb.rhs_pipeline, Pipeline)
    assert lb.inner_tables == ["OtherTable"]
    assert lb.rhs_expr is None


def test_unbound_table_alias_stays_an_expression():
    """Without a schema there is nothing to prove the name is a table, and the
    builder does not guess one into existence."""
    lb = _binding("let A = OtherTable; A | count", "A")
    assert lb.rhs_pipeline is None
    assert type(lb.rhs_expr).__name__ == "ColumnRef"


def test_semantic_hash_diverges_across_bind_state_for_a_table_alias():
    """Documented, accepted divergence — not a bug, and not fixable by
    stripping fields: the IR *shape* differs. Pinned so a future change to
    the let dispatch cannot silently alter it."""
    query = "let A = OtherTable; A | count"
    unbound = parse(query).to_ir()
    bound = parse(query, schema={"OtherTable": {"EventID": "int"}}).to_ir()
    assert unbound.semantic_hash != bound.semantic_hash

    # Control: a query with no table-aliasing let hashes identically.
    control = "T | where x > 1"
    assert (
        parse(control).to_ir().semantic_hash
        == parse(control, schema={"T": {"x": "int"}}).to_ir().semantic_hash
    )


def test_let_pipeline_result_schema_is_not_populated():
    """SchemaAttacher walks main_pipeline only — documented boundary (R6).

    Pinned so the field's emptiness is a stated contract rather than a
    silent gap, and so extending the binder later has to update this test.
    """
    ir = parse(
        "let Base = OtherTable | where EventID == 1; Base | count",
        schema={"OtherTable": {"EventID": "int"}},
    ).to_ir()
    assert ir.schema_attached is True
    assert ir.main_pipeline.result_schema is not None
    assert ir.let_bindings[0].rhs_pipeline is not None
    assert ir.let_bindings[0].rhs_pipeline.result_schema is None


def test_tabular_binding_is_reachable_by_generic_traversal():
    """The consumer-visible traversal change: `find_all` descends into the
    binding, so a lineage analyzer sees the let's source tables."""
    from kustology.ir import TableRef, find_all

    ir = parse(
        "let Base = SecurityEvent | where EventID == 1; Base | count"
    ).to_ir()
    assert [t.name for t in find_all(ir, TableRef)] == ["SecurityEvent", "Base"]


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
