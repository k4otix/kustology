# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Core IR builder behaviour: structural hash stability, JSON serialization,
binder enrichment with an inline schema."""

import pytest

from kustology.ir import (
    BinOp,
    ColumnRef,
    FilterOp,
    IRBuilder,
    KustoType,
    LiteralExpr,
    Pipeline,
    QueryIR,
    SchemaAttacher,
    Span,
    TableRef,
    UnknownSource,
)


@pytest.fixture
def ir_builder():
    return IRBuilder()


@pytest.fixture
def binder(sample_schema):
    return SchemaAttacher(sample_schema)


def test_semantic_hash_carries_scheme_prefix(ir_builder):
    """The hash is prefixed with ``kustology-sem-v2:`` so the
    canonicalization rules themselves are versionable. Tests pin the
    exact prefix so a future rename can't slip through silently."""
    ir = ir_builder.build("DeviceProcessEvents | where FileName == 'cmd.exe'")
    assert ir.semantic_hash.startswith("kustology-sem-v2:"), ir.semantic_hash
    # Digest portion is 64 hex chars — full SHA-256.
    digest = ir.semantic_hash.split(":", 1)[1]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_compute_semantic_hash_accepts_subtree(ir_builder):
    """``compute_semantic_hash`` must work on any IR ``BaseModel`` subtree,
    not only the root ``QueryIR``. That lets analyzers dedupe sub-shapes
    (e.g. "have I seen this predicate before?")."""
    from kustology.ir import BinOp, compute_semantic_hash, find_all
    ir = ir_builder.build("DeviceProcessEvents | where FileName == 'cmd.exe'")
    binops = list(find_all(ir, BinOp))
    assert binops, "expected at least one BinOp"
    h = compute_semantic_hash(binops[0])
    assert h.startswith("kustology-sem-v2:")
    # Same BinOp from a different query with the same shape collides.
    ir2 = ir_builder.build("DeviceProcessEvents\n| where FileName == 'cmd.exe' ")
    binops2 = list(find_all(ir2, BinOp))
    assert compute_semantic_hash(binops2[0]) == h


def test_semantic_hash_stability(ir_builder):
    """Reformatting must not change ``semantic_hash``. Rule authors rewrite
    queries cosmetically all the time; a semantic hash that flipped on
    whitespace would be useless for de-duplication. Different literal values,
    by contrast, must produce different hashes — they are not semantically
    equivalent."""
    pairs = [
        (
            "DeviceProcessEvents | where FileName == 'cmd.exe'",
            "DeviceProcessEvents\n| where FileName == 'cmd.exe'  ",
        ),
        (
            "DeviceProcessEvents | where A == 1 and B == 2",
            "DeviceProcessEvents\n   | where A == 1\n   and B == 2",
        ),
        (
            "DeviceProcessEvents | summarize count() by FileName",
            "DeviceProcessEvents|summarize count() by FileName",
        ),
    ]
    for a, b in pairs:
        ir_a = ir_builder.build(a)
        ir_b = ir_builder.build(b)
        assert ir_a.semantic_hash == ir_b.semantic_hash, (
            f"hash mismatch:\n  a: {a!r} -> {ir_a.semantic_hash}\n"
            f"  b: {b!r} -> {ir_b.semantic_hash}"
        )

    different = ir_builder.build("DeviceProcessEvents | where FileName == 'powershell.exe'")
    same = ir_builder.build("DeviceProcessEvents | where FileName == 'cmd.exe'")
    assert different.semantic_hash != same.semantic_hash


def test_ir_serialization():
    """IR round-trips through model_dump_json / model_validate_json without drift."""
    span = Span(text_start=0, width=10)
    ir = QueryIR(
        raw_text="test",
        semantic_hash="abc",
        let_bindings=[],
        main_pipeline=Pipeline(
            source=UnknownSource(raw_text="test", span=span),
            operators=[],
        ),
    )

    json_data = ir.model_dump_json()
    ir_back = QueryIR.model_validate_json(json_data)
    assert ir.semantic_hash == ir_back.semantic_hash
    assert ir.main_pipeline.source.span.text_start == 0


def test_binder_enrichment(binder):
    """Schema attachment resolves a bare ColumnRef to its owning table and type."""
    span = Span(text_start=0, width=0)

    col = ColumnRef(name="FileName", span=span)
    lit = LiteralExpr(value="cmd.exe", literal_kind="string", span=span)
    pred = BinOp(op="==", polarity="inclusion", case_sensitive=True, left=col, right=lit, span=span)

    ir = QueryIR(
        raw_text="...",
        semantic_hash="...",
        let_bindings=[],
        main_pipeline=Pipeline(
            source=TableRef(name="DeviceProcessEvents", span=span),
            operators=[FilterOp(predicate=pred, span=span)],
        ),
    )

    assert col.result_type == KustoType.UNRESOLVED

    binder.enrich(ir)

    assert col.result_type == KustoType.STRING
    assert ir.schema_attached is True


def test_count_operator_dispatch(ir_builder):
    from kustology.ir import CountOp
    ir = ir_builder.build("DeviceProcessEvents | count")
    assert len(ir.main_pipeline.operators) == 1
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, CountOp)
    assert op.as_name is None


def test_count_operator_with_as_clause(ir_builder):
    from kustology.ir import CountOp
    ir = ir_builder.build("DeviceProcessEvents | count as Total")
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, CountOp)
    assert op.as_name == "Total"


def test_print_operator_dispatch(ir_builder):
    from kustology.ir import PrintOp
    ir = ir_builder.build("print x = 1, y = tolower('AB')")
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, PrintOp)
    assert len(op.columns) == 2


def test_case_lifts_to_caseexpr(ir_builder):
    from kustology.ir import CaseExpr, FilterOp
    ir = ir_builder.build(
        "DeviceProcessEvents "
        "| where case(FileName == 'cmd.exe', true, FileName == 'pwsh.exe', true, false)"
    )
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, FilterOp)
    assert isinstance(op.predicate, CaseExpr)
    assert len(op.predicate.branches) == 2
    assert op.predicate.default is not None


def test_iif_lifts_to_caseexpr(ir_builder):
    from kustology.ir import CaseExpr, ExtendOp
    ir = ir_builder.build(
        "DeviceProcessEvents | extend tag = iif(FileName == 'cmd.exe', 'shell', 'other')"
    )
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, ExtendOp)
    expr = op.assignments[0].expr
    assert isinstance(expr, CaseExpr)
    assert len(expr.branches) == 1
    assert expr.default is not None


def test_case_odd_arg_count_falls_back_to_funccall(ir_builder):
    # case(predicate, value) is malformed (no default) — keep as FuncCall.
    from kustology.ir import FuncCall
    ir = ir_builder.build("DeviceProcessEvents | extend x = case(true, 1)")
    expr = ir.main_pipeline.operators[0].assignments[0].expr
    assert isinstance(expr, FuncCall)


def test_isnotnull_lifts_to_exists(ir_builder):
    from kustology.ir import Exists, FilterOp
    ir = ir_builder.build("DeviceProcessEvents | where isnotnull(FileName)")
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, FilterOp)
    assert isinstance(op.predicate, Exists)


def test_isnotempty_lifts_to_exists(ir_builder):
    from kustology.ir import Exists
    ir = ir_builder.build("DeviceProcessEvents | where isnotempty(FileName)")
    assert isinstance(ir.main_pipeline.operators[0].predicate, Exists)


def test_matches_regex_lifts_to_regexmatch(ir_builder):
    from kustology.ir import RegexMatch
    ir = ir_builder.build(
        "DeviceProcessEvents | where FileName matches regex '^cmd.*\\\\.exe$'"
    )
    pred = ir.main_pipeline.operators[0].predicate
    assert isinstance(pred, RegexMatch)
    assert "cmd" in pred.pattern


def test_not_func_lifts_to_not(ir_builder):
    from kustology.ir import Not
    # The KQL `not(X)` function call lifts to Not via the FuncCall name lift.
    ir = ir_builder.build("DeviceProcessEvents | where not(FileName == 'cmd.exe')")
    pred = ir.main_pipeline.operators[0].predicate
    assert isinstance(pred, Not)


def test_cluster_database_qualified_source(ir_builder):
    from kustology.ir import TableRef
    ir = ir_builder.build(
        'cluster("c").database("d").DeviceProcessEvents '
        "| where FileName == 'cmd.exe'"
    )
    assert isinstance(ir.main_pipeline.source, TableRef)
    assert ir.main_pipeline.source.name == "DeviceProcessEvents"


def test_database_qualified_source(ir_builder):
    from kustology.ir import TableRef
    ir = ir_builder.build(
        'database("d").DeviceProcessEvents | where FileName == "cmd.exe"'
    )
    assert isinstance(ir.main_pipeline.source, TableRef)
    assert ir.main_pipeline.source.name == "DeviceProcessEvents"


def test_kustotype_has_tabular():
    """``TABULAR`` is declared but unreachable from ``map_net_type``.

    Reaching it needs a .NET symbol whose ``Name`` is literally "tabular";
    ``TableSymbol.Name`` is the table's own name and ``ScalarTypes`` has no
    such entry. Kept as a member of the type system, pinned here as a stated
    boundary rather than asserted into looking implemented.
    """
    from kustology.ir import KustoType
    from kustology.ir._builder_helpers import map_net_type

    assert "TABULAR" in {m.name for m in KustoType}
    # Only a literal "tabular" type name would produce it, and nothing emits one.
    assert map_net_type("tabular") is KustoType.TABULAR
    assert map_net_type("long") is KustoType.LONG


def test_dynamic_element_type_is_populated_on_a_real_parse(ir_builder):
    """``result_type_inner`` on a bound parse, not merely its default.

    The previous test asserted ``e.result_type_inner is None`` on a
    hand-built LiteralExpr, which passed identically whether the populating
    code worked or -- as it did -- probed a .NET member that does not exist.
    """
    from kustology.ir import KustoType, find_all
    from kustology.ir.expr import Expr

    ir = ir_builder.build("print x = dynamic([1, 2, 3])")
    inners = [
        e.result_type_inner for e in find_all(ir, Expr)
        if e.result_type_inner is not None
    ]
    assert inners, "no expression carried a dynamic element type"
    assert all(isinstance(i, KustoType) for i in inners)


def test_pipeline_result_schema_field():
    from kustology.ir import Pipeline, Span, TableRef, TabularSchema
    pipe = Pipeline(
        source=TableRef(name="T", span=Span(text_start=0, width=1)),
        operators=[],
    )
    # Default
    assert pipe.result_schema is None
    # Settable
    pipe.result_schema = TabularSchema(columns={"x": "string"})
    assert pipe.result_schema.columns["x"] == "string"


def test_funccall_as_pipeline_source(ir_builder):
    """User-defined functions returning tables resolve to FuncCallSource in union branches."""
    from kustology.ir import FuncCallSource, UnionOp
    ir = ir_builder.build(
        "union findAnomalies('foo'), findAnomalies('bar')"
    )
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, UnionOp)
    for pipe in op.pipelines:
        assert isinstance(pipe.source, FuncCallSource), (
            f"branch source: {type(pipe.source).__name__}"
        )
        assert pipe.source.name == "findAnomalies"


def test_misc_operators_dispatch_to_specific_classes(ir_builder):
    """getschema / consume / serialize / find each dispatch to their own Operator subclass."""
    from kustology.ir import (
        ConsumeOp,
        FindOp,
        GetSchemaOp,
        Operator,
        SerializeOp,
    )
    cases = [
        ("DeviceProcessEvents | getschema", GetSchemaOp),
        ("DeviceProcessEvents | consume", ConsumeOp),
        ("DeviceProcessEvents | serialize x = 1", SerializeOp),
        ("find in (DeviceProcessEvents) where FileName == 'cmd.exe'", FindOp),
    ]
    for query, expected_cls in cases:
        ir = ir_builder.build(query)
        ops = ir.main_pipeline.operators
        matched = any(isinstance(o, expected_cls) for o in ops)
        assert not any(type(o) is Operator for o in ops), (
            f"{query!r} produced a bare Operator: {[type(o).__name__ for o in ops]}"
        )
        # `find` parses as a leading operator in some forms; the bare-Operator
        # check above is the load-bearing assertion for that case.
        if expected_cls is not FindOp:
            assert matched, (
                f"{query!r} -> ops {[type(o).__name__ for o in ops]}; expected {expected_cls.__name__}"
            )


def test_tabular_subquery_in_membership_test_is_modeled(ir_builder):
    """`in ((P))` used to collapse the whole inner query into an UnknownExpr.

    Surfaced when the corpus gate started walking `let` right-hand sides —
    the only corpus occurrence sits inside one. Asserted here rather than
    only via the corpus so a fixture change can't retire the coverage.
    """
    from kustology.ir import SetMembership, SubqueryExpr, UnknownExpr, find_all

    ir = ir_builder.build(
        "let Suspicious = SigninLogs | project UserPrincipalName;\n"
        "SigninLogs | where UserPrincipalName in ((Suspicious | project UserPrincipalName))"
    )
    assert not list(find_all(ir, UnknownExpr))

    membership = next(iter(find_all(ir, SetMembership)))
    subqueries = [v for v in membership.values if isinstance(v, SubqueryExpr)]
    assert len(subqueries) == 1
    # The inner pipeline is a real subtree, not a raw-text blob.
    assert isinstance(subqueries[0].pipeline, Pipeline)
    # `Suspicious` is a let alias, so the inner source is a LetRef.
    from kustology.ir import LetRef

    assert [r.name for r in find_all(subqueries[0].pipeline, LetRef)] == ["Suspicious"]
    assert not list(find_all(subqueries[0].pipeline, TableRef))
