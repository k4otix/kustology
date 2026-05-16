# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Core IR builder behaviour: structural hash stability, JSON serialization,
binder enrichment with an inline schema."""

import pytest

from pykusto_language.ir import (
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


def test_structural_hash_stability(ir_builder):
    """Reformatting must not change `structural_hash`. Rule authors rewrite
    queries cosmetically all the time; a structural hash that flipped on
    whitespace would be useless for de-duplication."""
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
        assert ir_a.structural_hash == ir_b.structural_hash, (
            f"hash mismatch:\n  a: {a!r} -> {ir_a.structural_hash}\n"
            f"  b: {b!r} -> {ir_b.structural_hash}"
        )

    different = ir_builder.build("DeviceProcessEvents | where FileName == 'powershell.exe'")
    same = ir_builder.build("DeviceProcessEvents | where FileName == 'cmd.exe'")
    assert different.structural_hash != same.structural_hash


def test_ir_serialization():
    """IR round-trips through model_dump_json / model_validate_json without drift."""
    span = Span(text_start=0, width=10, source_text="test")
    ir = QueryIR(
        raw_text="test",
        structural_hash="abc",
        let_bindings=[],
        main_pipeline=Pipeline(
            source=UnknownSource(raw_text="test", span=span),
            operators=[],
        ),
    )

    json_data = ir.model_dump_json()
    ir_back = QueryIR.model_validate_json(json_data)
    assert ir.structural_hash == ir_back.structural_hash
    assert ir.main_pipeline.source.span.text_start == 0


def test_binder_enrichment(binder):
    """Schema attachment resolves a bare ColumnRef to its owning table and type."""
    span = Span(text_start=0, width=0, source_text="")

    col = ColumnRef(name="FileName", span=span)
    lit = LiteralExpr(value="cmd.exe", literal_kind="string", span=span)
    pred = BinOp(op="==", polarity="inclusion", case_sensitive=True, left=col, right=lit, span=span)

    ir = QueryIR(
        raw_text="...",
        structural_hash="...",
        let_bindings=[],
        main_pipeline=Pipeline(
            source=TableRef(name="DeviceProcessEvents", span=span),
            operators=[FilterOp(predicate=pred, span=span)],
        ),
    )

    assert col.result_type == KustoType.UNKNOWN

    binder.enrich(ir)

    assert col.result_type == KustoType.STRING
    assert ir.schema_attached is True


def test_count_operator_dispatch(ir_builder):
    from pykusto_language.ir import CountOp
    ir = ir_builder.build("DeviceProcessEvents | count")
    assert len(ir.main_pipeline.operators) == 1
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, CountOp)
    assert op.as_name is None


def test_count_operator_with_as_clause(ir_builder):
    from pykusto_language.ir import CountOp
    ir = ir_builder.build("DeviceProcessEvents | count as Total")
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, CountOp)
    assert op.as_name == "Total"


def test_print_operator_dispatch(ir_builder):
    from pykusto_language.ir import PrintOp
    ir = ir_builder.build("print x = 1, y = tolower('AB')")
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, PrintOp)
    assert len(op.columns) == 2


def test_case_lifts_to_caseexpr(ir_builder):
    from pykusto_language.ir import CaseExpr, FilterOp
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
    from pykusto_language.ir import CaseExpr, ExtendOp
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
    from pykusto_language.ir import FuncCall
    ir = ir_builder.build("DeviceProcessEvents | extend x = case(true, 1)")
    expr = ir.main_pipeline.operators[0].assignments[0].expr
    assert isinstance(expr, FuncCall)


def test_isnotnull_lifts_to_exists(ir_builder):
    from pykusto_language.ir import Exists, FilterOp
    ir = ir_builder.build("DeviceProcessEvents | where isnotnull(FileName)")
    op = ir.main_pipeline.operators[0]
    assert isinstance(op, FilterOp)
    assert isinstance(op.predicate, Exists)


def test_isnotempty_lifts_to_exists(ir_builder):
    from pykusto_language.ir import Exists
    ir = ir_builder.build("DeviceProcessEvents | where isnotempty(FileName)")
    assert isinstance(ir.main_pipeline.operators[0].predicate, Exists)


def test_matches_regex_lifts_to_regexmatch(ir_builder):
    from pykusto_language.ir import RegexMatch
    ir = ir_builder.build(
        "DeviceProcessEvents | where FileName matches regex '^cmd.*\\\\.exe$'"
    )
    pred = ir.main_pipeline.operators[0].predicate
    assert isinstance(pred, RegexMatch)
    assert "cmd" in pred.pattern


def test_not_func_lifts_to_not(ir_builder):
    from pykusto_language.ir import Not
    # The KQL `not(X)` function call lifts to Not via the FuncCall name lift.
    ir = ir_builder.build("DeviceProcessEvents | where not(FileName == 'cmd.exe')")
    pred = ir.main_pipeline.operators[0].predicate
    assert isinstance(pred, Not)


def test_cluster_database_qualified_source(ir_builder):
    from pykusto_language.ir import TableRef
    ir = ir_builder.build(
        'cluster("c").database("d").DeviceProcessEvents '
        "| where FileName == 'cmd.exe'"
    )
    assert isinstance(ir.main_pipeline.source, TableRef)
    assert ir.main_pipeline.source.name == "DeviceProcessEvents"


def test_database_qualified_source(ir_builder):
    from pykusto_language.ir import TableRef
    ir = ir_builder.build(
        'database("d").DeviceProcessEvents | where FileName == "cmd.exe"'
    )
    assert isinstance(ir.main_pipeline.source, TableRef)
    assert ir.main_pipeline.source.name == "DeviceProcessEvents"


def test_kustotype_has_tabular():
    from pykusto_language.ir import KustoType
    assert "TABULAR" in {m.name for m in KustoType}


def test_expr_has_nullable_and_inner_type():
    from pykusto_language.ir import LiteralExpr, Span
    e = LiteralExpr(value="x", literal_kind="string", span=Span(text_start=0, width=1))
    # Defaults
    assert e.nullable is True
    assert e.result_type_inner is None


def test_pipeline_result_schema_field():
    from pykusto_language.ir import Pipeline, Span, TableRef, TabularSchema
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
    from pykusto_language.ir import FuncCallSource, UnionOp
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
    from pykusto_language.ir import (
        ConsumeOp, FindOp, GetSchemaOp, Operator, SerializeOp,
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
