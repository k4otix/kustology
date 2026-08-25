# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Provenance and honesty tests for ``SchemaAttacher``.

Two contracts are under test and they are deliberately separate.

**Provenance** is this walk's own: every ``ColumnRef`` gets the table it came
from, across ``project`` / ``summarize`` / joins / unions / ``search`` /
``let`` threading, and nothing else in the library can supply it. Most of the
file is that.

**Honesty** is the other half: ``Pipeline.result_schema`` is Microsoft's
``ResultType`` or it is ``None``. The hand-rolled per-operator schema rules
that used to answer here are gone, so the *schemas* are pinned in
``tests/ir/test_binder_oracle.py`` against the binder rather than against a
hand-written expectation. What is pinned here is the shape of the contract —
where an answer appears, where ``None`` appears, and that ``None`` and an
empty ``TabularSchema`` stay distinguishable.
"""

import pytest

from kustology import parse
from kustology.ir import ColumnRef, IRBuilder, Pipeline, find_all
from kustology.ir.binder import SchemaAttacher


@pytest.fixture(scope="module")
def schema():
    return {
        "DeviceProcessEvents": {
            "FileName": "string",
            "AccountName": "string",
            "DeviceName": "string",
            "TimeGenerated": "datetime",
            "ProcessId": "long",
        },
        "DeviceFileEvents": {
            "DeviceId": "string",
            "FileName": "string",
            "TimeGenerated": "datetime",
        },
    }


@pytest.fixture
def builder():
    return IRBuilder()


@pytest.fixture
def attacher(schema):
    return SchemaAttacher(schema)


# Honesty: where Microsoft declines, so do we ----------------------------


def test_an_open_symbol_gets_no_invented_schema():
    """Partial schemas are the norm; where Microsoft declines to type an
    operator (open symbol -- the table is not in the dict), the IR now says
    result_schema=None instead of a hand-computed guess."""
    ir = parse("Unknown | project a, b").to_ir(attach_schema={"T": {"a": "long"}})
    (op,) = ir.main_pipeline.operators
    assert op.result_schema is None
    assert ir.main_pipeline.result_schema is None


def test_provenance_still_fills_under_an_open_symbol():
    """Deleting the schema rules must not delete provenance: a column read
    from a table the dict does describe keeps its table even when a later
    operator is open."""
    q = "T | where a > 1 | lookup Unknown on a | project a"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import FilterOp

    (where_op,) = [
        op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)
    ]
    assert {c.table for c in find_all(where_op, ColumnRef)} == {"T"}


def test_a_datatable_root_closes_with_no_schema_dict_at_all():
    """Closure does not need a schema: a ``datatable`` declares its own.

    ``to_ir()`` with no ``attach_schema`` still lands a real schema here,
    because the symbol was never open -- there is nothing for a dict to add.
    """
    ir = parse("datatable(a:long)[1] | project a").to_ir()
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


def test_a_symbol_can_close_mid_pipeline_over_an_undescribed_table():
    """``IsOpen`` is per node, not per query.

    ``T | count`` returns ``Count:long`` whatever ``T`` is, so the binder
    closes the symbol there even though the source is unknown, and the
    answer survives with no schema dict at all. ``getschema`` was probed the
    same way and answers the same: its four columns *describe* the input's
    shape rather than passing it through, so they can be named without
    knowing it.
    """
    assert parse("T | count").to_ir().main_pipeline.result_schema.columns == {
        "Count": "long",
    }
    assert parse("T | getschema").to_ir().main_pipeline.result_schema.columns == {
        "ColumnName": "string",
        "ColumnOrdinal": "long",
        "DataType": "string",
        "ColumnType": "string",
    }


def test_really_emitting_nothing_and_not_knowing_are_different_answers():
    """``columns={}`` is a claim: "this emits no columns". ``None`` is not.

    ``project-away *`` genuinely produces an empty schema, and only
    Microsoft says so -- the bound symbol closes empty and the stamp carries
    it. Without a schema the same query is open, and stamping ``{}`` there
    would make a query over an undescribed table indistinguishable from one
    that really returns nothing.
    """
    closed = _dict_path("T | project-away *")
    assert closed.main_pipeline.result_schema is not None
    assert closed.main_pipeline.result_schema.columns == {}
    assert parse("T | project-away *").to_ir().main_pipeline.result_schema is None


def test_pipeline_result_schema_populated_after_enrich(schema):
    """The ``TabularSchema`` plumbing itself: an answer arrives, and it is
    Microsoft's."""
    from kustology.ir import TabularSchema

    ir = parse("DeviceProcessEvents | project FileName, AccountName").to_ir(
        attach_schema=schema,
    )
    result = ir.main_pipeline.result_schema
    assert isinstance(result, TabularSchema)
    assert set(result.columns.keys()) == {"FileName", "AccountName"}


# The three schema value shapes the public dict path has to accept ----------


@pytest.mark.parametrize("value,expect_type", [
    ({"a": "long"}, "long"),          # typed columns
    ("(a:long, b:string)", "long"),   # a Kusto schema string
    (["a"], "string"),                # untyped columns, treated as string
])
def test_every_documented_schema_value_shape_reaches_the_walk(value, expect_type):
    """``parse(schema=…)`` documents three value shapes and so does
    ``build_global_state``, so ``to_ir(attach_schema=…)`` has to take all
    three -- it is the same schema argument on a different entry point.

    ``SchemaAttacher`` reads ``schemas[table][column]`` and takes only the
    first shape, so the other two are normalized before they reach it. Both
    halves are asserted: Microsoft's schema (the binder saw the columns) and
    the walk's provenance (the attacher did too).
    """
    ir = parse("T | project a | where a > 1").to_ir(attach_schema={"T": value})
    assert ir.main_pipeline.result_schema.columns == {"a": expect_type}
    assert {c.table for c in find_all(ir, ColumnRef)} == {"T"}


def test_a_schema_string_does_not_crash_the_walks_type_fallback():
    """Historical regression pin: ``_fill``'s type fallback used to do
    ``schemas[table].get(name)``, which raised ``AttributeError`` against a
    string schema value. Since the reroute, ``core.to_ir`` normalizes every
    schema shape (dict/string/list) through ``build_global_state`` before
    ``SchemaAttacher`` ever runs, so a raw string no longer reaches this
    code on the public path -- this test pins the public path against the
    historical crash rather than exercising the fallback directly.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ir = parse("T | project n").to_ir(attach_schema={"T": "(n:bogus)"})
    assert ir.main_pipeline.result_schema.columns == {"n": "unknown"}
    assert {c.table for c in find_all(ir, ColumnRef)} == {"T"}


def test_a_schema_string_does_not_crash_the_search_seeding():
    """Historical regression pin: ``search`` used to seed
    ``ScopeEntry(columns=dict(...))`` straight from the schema value, and
    ``dict("(a:long)")`` is a ``ValueError``. Since the reroute,
    ``core.to_ir`` normalizes the schema shape before ``SchemaAttacher`` ever
    runs, so this string no longer reaches the seeding code on the public
    path either -- a different crash site from the one above, pinned the
    same way, against a perfectly ordinary type.
    """
    ir = parse("search in (T) 'x'").to_ir(attach_schema={"T": "(a:long)"})
    assert ir.main_pipeline.result_schema.columns == {
        "$table": "string", "a": "long",
    }


# Make-series range fields ----------------------------------------------

def test_make_series_range_fields_populated(builder, attacher):
    """``range_from``, ``range_to``, ``step`` are populated from RangeClause."""
    ir = builder.build(
        "DeviceProcessEvents | make-series count() default=0 on TimeGenerated "
        "from datetime(2024-01-01) to datetime(2024-01-08) step 1h by DeviceName"
    )
    op = ir.main_pipeline.operators[0]
    assert op.range_from is not None
    assert op.range_to is not None
    assert op.step is not None


def test_make_series_step_only_populates_step(builder, attacher):
    """When only ``step`` is specified, from/to remain None but step is populated."""
    ir = builder.build(
        "DeviceProcessEvents | make-series count() default=0 on TimeGenerated step 1h by DeviceName"
    )
    op = ir.main_pipeline.operators[0]
    assert op.range_from is None
    assert op.range_to is None
    assert op.step is not None


# --- traversal completeness: every Expr child, every nested Pipeline --------
#
# These four shapes all failed the same way before the walker was derived
# from model_fields: a ColumnRef the binder never visited keeps table=None,
# so the *same* column resolves inconsistently within one query. Any lineage
# analyzer reading ColumnRef.table gets a silently wrong answer.


def _refs(ir):
    """{name: {table, ...}} for every ColumnRef in the IR."""
    from kustology.ir import ColumnRef, find_all

    out: dict[str, set] = {}
    for c in find_all(ir, ColumnRef):
        out.setdefault(c.name, set()).add(c.table)
    return out


def test_columns_resolve_inside_toscalar(schema):
    """A pipeline nested in an expression resolves against its own source.

    ``_fill`` recursed a hardcoded attribute tuple that had no ``pipeline``
    entry, so ToScalarExpr / MaterializeExpr / SubqueryExpr subtrees were
    never entered.
    """
    ir = parse(
        "DeviceProcessEvents "
        "| where ProcessId > toscalar(DeviceProcessEvents | summarize max(ProcessId)) "
        "| project AccountName"
    ).to_ir(attach_schema=schema)
    # The same column inside and outside the toscalar must agree.
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_columns_resolve_inside_case_arms(schema):
    """``CaseExpr.branches`` is tuple-nested and ``default`` was unlisted."""
    ir = parse(
        "DeviceProcessEvents "
        "| extend Risk = iif(ProcessId > 100, AccountName, DeviceName)"
    ).to_ir(attach_schema=schema)
    refs = _refs(ir)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}
    assert refs["DeviceName"] == {"DeviceProcessEvents"}


def test_columns_resolve_under_operators_without_a_scope_rule(schema):
    """`sort` and `top` carry expressions but reshape nothing.

    ``_walk_operator`` was an isinstance chain over 17 of 53 operator types
    with no fallback -- it fell off the end, so the other 36 filled nothing.
    """
    ir = parse(
        "DeviceProcessEvents "
        "| sort by ProcessId desc "
        "| top 5 by TimeGenerated "
        "| project AccountName"
    ).to_ir(attach_schema=schema)
    refs = _refs(ir)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["TimeGenerated"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}


def test_columns_resolve_through_a_nested_pipeline_source(schema):
    """``Pipeline.source`` may itself be a ``Pipeline``.

    ``let M = materialize(T | where X)`` is the shape that produces one --
    ``materialize(...)`` at the head of a bare statement is not accepted by
    Microsoft's parser at all. ``_source_entry`` returned an empty anonymous
    scope for the nested case and never walked the inner pipeline, so the
    outer pipeline started from nothing.
    """
    from kustology.ir.query import Pipeline

    ir = parse(
        "let M = materialize(DeviceProcessEvents | where ProcessId > 1); "
        "M | count"
    ).to_ir(attach_schema=schema)
    inner = ir.let_bindings[0].rhs_pipeline
    assert isinstance(inner.source, Pipeline), "expected a nested pipeline source"

    # The nested source's table reached the outer pipeline's scope, and the
    # ColumnRef inside it was visited.
    assert set(inner.result_schema.columns) >= {"ProcessId", "AccountName"}
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


# --- let threading ---------------------------------------------------------


def test_let_pipeline_is_enriched(schema):
    """``enrich`` walked ``main_pipeline`` only, so a tabular binding's
    ``result_schema`` stayed None and the ColumnRefs inside it kept
    ``table=None`` even on a fully bound parse."""
    ir = parse(
        "let Base = DeviceProcessEvents | where ProcessId > 1; Base | count"
    ).to_ir(attach_schema=schema)
    binding = ir.let_bindings[0]
    assert binding.rhs_pipeline.result_schema is not None
    assert "AccountName" in binding.rhs_pipeline.result_schema.columns
    assert _refs(ir)["ProcessId"] == {"DeviceProcessEvents"}


def test_main_pipeline_resolves_columns_through_a_let_name(schema):
    """The whole point: `Base | project AccountName` knows what Base holds."""
    ir = parse(
        "let Base = DeviceProcessEvents | where ProcessId > 1; "
        "Base | project AccountName"
    ).to_ir(attach_schema=schema)
    refs = _refs(ir)
    assert refs["AccountName"] == {"Base"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_let_threading_follows_a_chain(schema):
    ir = parse(
        "let A = DeviceProcessEvents | project AccountName, ProcessId; "
        "let B = A | where ProcessId > 2; "
        "B | project AccountName"
    ).to_ir(attach_schema=schema)
    a, b = ir.let_bindings
    assert set(a.rhs_pipeline.result_schema.columns) == {"AccountName", "ProcessId"}
    assert set(b.rhs_pipeline.result_schema.columns) == {"AccountName", "ProcessId"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_let_threading_is_gated_on_microsoft_closing_the_binding():
    """A binding only threads once something says what it emits.

    ``_let_schemas`` is filled from each binding pipeline's own
    ``result_schema``, which is Microsoft's answer or nothing. On the dict
    path the binding closes and the alias carries its columns. On a raw
    unbound IR handed to an attacher, the binding is open, so the alias
    registers nothing and a column read through it is honestly unresolved --
    rather than resolved from a shape nobody vouched for.
    """
    schemas = {"T": {"a": "long", "s": "string"}}
    query = "let Base = T | where a > 1; Base | project s"

    threaded = parse(query).to_ir(attach_schema=schemas)
    assert threaded.let_bindings[0].rhs_pipeline.result_schema.columns == schemas["T"]
    assert _refs(threaded)["s"] == {"Base"}

    ungated = IRBuilder().build(query)
    SchemaAttacher(schemas).enrich(ungated)
    assert ungated.let_bindings[0].rhs_pipeline.result_schema is None
    assert _refs(ungated)["s"] == {None}
    # The binding *body* still reads a real table, so its own columns place.
    assert _refs(ungated)["a"] == {"T"}


def test_let_threading_does_not_resolve_a_forward_reference(schema):
    """A binding naming one declared later is not a LetRef at all, so there
    is nothing to thread -- it stays an opaque table.

    Its ``result_schema`` is ``None``: nothing determined what the binding
    emits. An empty ``TabularSchema`` would state that it emits no columns,
    which is a different and false claim.
    """
    ir = parse(
        "let Early = Later | take 1; "
        "let Later = DeviceProcessEvents | take 1; "
        "Early | project AccountName"
    ).to_ir(attach_schema=schema)
    assert ir.let_bindings[0].rhs_pipeline.result_schema is None


def test_scalar_binding_is_untouched_by_threading(builder, attacher):
    ir = builder.build("let lookback = 1h; DeviceProcessEvents | count")
    attacher.enrich(ir)
    assert ir.let_bindings[0].rhs_pipeline is None
    assert ir.let_bindings[0].rhs_expr is not None


def test_let_names_do_not_leak_between_enrich_calls(schema):
    """The let scope is per-call state; a reused attacher must not carry
    one query's binding names into the next.

    Asserted on ``_let_schemas`` itself rather than on a downstream column,
    because a downstream column cannot see the leak: a second query's
    ``Base`` is a plain ``TableRef``, and ``_source_entry`` only consults
    ``_let_schemas`` for a ``LetRef``, which the builder emits only for a
    name an earlier ``let`` *in that query* bound. Reading the registry is
    what makes this falsifiable -- drop the reset at the top of ``enrich``
    and the second call's registry holds both names.

    The IRs are bound and enriched by hand (``attach_schema=False``, then
    one shared attacher) because that is the only way to reuse an attacher
    across two queries -- and because a binding registers a name only once
    Microsoft closes it, so an unbound pair would leave the registry empty
    either way and prove nothing.
    """
    attacher = SchemaAttacher(schema)

    first = parse(
        "let Base = DeviceProcessEvents | project AccountName; Base | count",
        schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(first)
    assert set(attacher._let_schemas) == {"Base"}, "premise: the name registered"

    second = parse(
        "let Other = DeviceProcessEvents | project DeviceName; "
        "Other | project DeviceName",
        schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(second)
    assert set(attacher._let_schemas) == {"Other"}

    # And a query with no bindings at all leaves nothing behind either.
    third = parse(
        "DeviceProcessEvents | project AccountName", schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(third)
    assert attacher._let_schemas == {}


# --- function and pattern bodies --------------------------------------------


def test_builder_schemas_snapshot_reaches_body_pipelines(schema):
    """Precondition, not behaviour: ``find_all(ir, Pipeline)`` is a generic
    walk, so it already reaches a let-function's ``body_pipeline`` -- the
    ``_builder_schemas`` snapshot ``enrich`` takes at entry covers it before
    ``_walk_function_body`` exists to consume it. Pinned directly rather
    than inferred from downstream provenance, so a regression here reads as
    this assertion failing, not as a mystifying resolution gap two layers
    away."""
    ir = parse(
        "let f = (n:long) { DeviceProcessEvents | where ProcessId > n }; "
        "DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=False)
    body_pipeline = ir.let_bindings[0].rhs_function.body_pipeline
    snapshot = {id(p): p.result_schema for p in find_all(ir, Pipeline)}
    assert id(body_pipeline) in snapshot


def test_a_function_bodys_columns_acquire_table_from_a_real_table_it_reads(schema):
    """The body is walked as its own scope: a column the body reads off a
    real table gets that table's provenance, the same as any other
    pipeline. The scalar parameter ``n`` names no table's column, so it
    stays honestly unresolved rather than borrowing one."""
    ir = parse(
        "let f = (n:long) { "
        "DeviceProcessEvents | where ProcessId > n | project AccountName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    refs = _refs(fn)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}
    assert refs["n"] == {None}


def test_a_tabular_parameters_columns_answer_no_table_rather_than_the_callers(schema):
    """A tabular parameter's ``TableRef`` masks to an empty scope: nothing
    says what columns it carries, so a column read off it stays
    ``table=None`` rather than resolving through the parameter's bare name
    as though it were a real, schema-described table.

    Probed and pinned rather than assumed: the alternative honest-looking
    answer was ``table="X"`` (the parameter's own name, still labelling the
    source even though no columns are known). Through this test's own
    resolution path -- an ordinary bare ``ColumnRef``, filled by
    ``_resolve_column_table``/``_column_origins`` -- that never happens:
    those only ever answer from a scope entry's *known* columns, so an
    unknown column of a masked entry resolves to ``None`` the same way an
    unknown column of any other schema-less table would. This is **not**
    the whole story, though -- ``_resolve_side``'s single-entry fallback (a
    *different* resolution path, reached only from inside a join's ``on``
    clause) used to answer the parameter's own name here, because
    ``ScopeEntry.table`` carried it regardless of masking; that leak and its
    fix (``_entry_table``) are covered separately by
    ``test_a_masked_tabular_parameters_own_name_does_not_surface_inside_a_joins_on_clause``.

    The parameter is named after a real schema table on purpose, and ``a``
    is a real column of that table's schema: without the mask, ``_table_
    schema`` would answer the real table's columns for this name and this
    same reference would resolve to ``DeviceProcessEvents`` -- a genuine
    leak of the caller's table into a value the body never actually reads.
    """
    ir = parse(
        "let f = (DeviceProcessEvents:(*)) { "
        "DeviceProcessEvents | where AccountName == 'x' | project AccountName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    assert _refs(fn)["AccountName"] == {None}


def test_a_masked_tabular_parameters_own_name_does_not_surface_inside_a_joins_on_clause(
    schema,
):
    """A second, separate leak path for the same root cause as the test
    above -- caught by review, not by the original probe.

    ``_source_entry``'s plain-``TableRef`` branch used to write the bare
    name into ``ScopeEntry.table`` unconditionally; only ``_table_schema``
    (the *columns*) knew about the mask. ``$left.AccountName`` has no known
    column to resolve by name here (the left side's one entry is masked to
    ``columns={}``), so it falls through to ``_resolve_side``'s
    single-entry fallback -- built for an honestly-unknown table, where one
    entry unambiguously names the side even with no columns to confirm it.
    That fallback read ``entries[0].table`` straight back out, which used
    to be the parameter's own (colliding) name: the exact leak masking
    exists to prevent, on a path the original test's docstring claimed
    (wrongly, as it turns out) could not reach it. ``_entry_table`` closes
    it at the source: a masked name never becomes a ``ScopeEntry.table``
    label in the first place, so this fallback answers ``None`` for free.

    The real right-hand table is a plain, unmasked reference and is
    unaffected -- only the masked left side used to leak.
    """
    ir = parse(
        "let f = (DeviceProcessEvents:(*)) { "
        "DeviceProcessEvents | join (DeviceFileEvents) "
        "on $left.AccountName == $right.FileName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    refs = {c.name: c for c in find_all(fn, ColumnRef)}
    assert refs["AccountName"].join_side == "left"
    assert refs["AccountName"].table is None
    assert refs["FileName"].join_side == "right"
    assert refs["FileName"].table == "DeviceFileEvents"


def test_body_lets_chain_inside_a_function_body(schema):
    """A tabular body ``let`` threads into the tail exactly as a top-level
    one threads into the main pipeline -- the whole point of walking the
    body as its own scope rather than leaving it opaque."""
    ir = parse(
        "let f = (n:long) { "
        "let Filtered = DeviceProcessEvents | where ProcessId > n; "
        "Filtered | project AccountName "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    (body_let,) = fn.body_lets
    assert body_let.rhs_pipeline.result_schema is not None
    assert "AccountName" in body_let.rhs_pipeline.result_schema.columns
    assert _refs(fn)["AccountName"] == {"Filtered"}


def test_a_scalar_parameter_colliding_with_a_schema_table_does_not_leak_it(schema):
    """A scalar parameter named after a real schema table masks that table
    for the length of the body -- a reference to the parameter must not
    resolve against the table it merely shares a name with -- and the mask
    is gone once the body is done: the main pipeline's own reference to the
    real table resolves normally right after."""
    ir = parse(
        "let f = (DeviceProcessEvents:long) { "
        "DeviceFileEvents | where TimeGenerated > DeviceProcessEvents "
        "}; DeviceProcessEvents | project AccountName",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    body_refs = _refs(fn)
    # The real table the body actually reads resolves normally.
    assert body_refs["TimeGenerated"] == {"DeviceFileEvents"}
    # The parameter reference does not borrow the colliding table's identity.
    assert body_refs["DeviceProcessEvents"] == {None}
    # Restored: the main pipeline's own real-table reference is unaffected.
    assert _refs(ir.main_pipeline)["AccountName"] == {"DeviceProcessEvents"}
    assert ir.main_pipeline.result_schema.columns["AccountName"] == "string"


def test_a_nested_function_bodys_own_let_function_is_recursed_into(schema):
    """A ``let`` written inside a function body can itself be a
    ``FunctionDeclaration`` -- ``_walk_function_body``'s own ``body_lets``
    loop gets the same three-way dispatch ``enrich``'s top-level loop does,
    so a nested function's body is walked (and masked) too, not silently
    skipped.

    Both maskings apply, and independently, during the nested walk:
    ``inner``'s own tabular parameter (``DeviceFileEvents``, colliding with
    a real schema table) is masked while ``inner``'s body runs, and
    ``outer``'s tabular parameter (``DeviceProcessEvents``, also colliding)
    is *still* masked throughout -- unioned in when entering ``inner``, not
    replaced. Restored correctly on the way back out: ``outer``'s own tail
    still can't see through its own mask after ``inner`` returns, and the
    main pipeline's real-table reference resolves normally once ``enrich``
    is done with both.
    """
    walked_ids: set[int] = set()
    attacher = SchemaAttacher(schema)
    original_walk_pipeline = attacher._walk_pipeline

    def recording_walk_pipeline(pipeline, inherited=None):
        walked_ids.add(id(pipeline))
        return original_walk_pipeline(pipeline, inherited)

    attacher._walk_pipeline = recording_walk_pipeline

    ir = parse(
        "let outer = (DeviceProcessEvents:(*)) { "
        "let inner = (DeviceFileEvents:(*)) { "
        "DeviceFileEvents | where FileName == 'x' | project FileName "
        "}; "
        "DeviceProcessEvents | where AccountName == 'x' | project AccountName "
        "}; "
        "DeviceProcessEvents | project AccountName",
        schema=schema,
    ).to_ir(attach_schema=False)
    attacher.enrich(ir)

    outer = ir.let_bindings[0].rhs_function
    (inner_binding,) = outer.body_lets
    inner = inner_binding.rhs_function

    # The nested body really was walked, not silently skipped.
    assert id(inner.body_pipeline) in walked_ids

    # Inner's own masked parameter does not leak the real DeviceFileEvents.
    assert _refs(inner)["FileName"] == {None}
    # Outer's masked parameter still doesn't leak, for outer's own tail,
    # even after the nested call unwound.
    assert _refs(outer)["AccountName"] == {None}
    # And the real thing, outside both scopes, resolves normally.
    assert _refs(ir.main_pipeline)["AccountName"] == {"DeviceProcessEvents"}


def test_masking_is_restored_even_if_the_body_walk_raises(schema):
    """``_masked_tables``/``_let_schemas`` are restored in ``finally``
    inside ``_walk_function_body``, not only on the happy path -- a bug (or
    a future exception) partway through one function's body must not leave
    the attacher permanently masking a real table, or permanently holding a
    stale let, for every query it enriches afterwards."""
    attacher = SchemaAttacher(schema)
    ir = parse(
        "let f = (DeviceProcessEvents:long) { "
        "DeviceFileEvents | where TimeGenerated > DeviceProcessEvents "
        "}; DeviceProcessEvents | project AccountName",
        schema=schema,
    ).to_ir(attach_schema=False)
    fn = ir.let_bindings[0].rhs_function

    original_walk_pipeline = attacher._walk_pipeline

    def exploding_walk_pipeline(pipeline, inherited=None):
        if pipeline is fn.body_pipeline:
            raise RuntimeError("boom")
        return original_walk_pipeline(pipeline, inherited)

    attacher._walk_pipeline = exploding_walk_pipeline
    with pytest.raises(RuntimeError, match="boom"):
        attacher.enrich(ir)

    assert attacher._masked_tables == set()
    assert "DeviceProcessEvents" not in attacher._let_schemas


def test_a_masked_search_tables_own_name_does_not_surface_through_a_later_join(
    schema,
):
    """The same ``_entry_table`` fix also closes ``search``/``find``'s table
    seeding (``_walk_operator_provenance``'s ``SearchOp``/``FindOp`` branch),
    which built its ``ScopeEntry`` the same unmasked-label way
    ``_source_entry`` did -- confirmed exploitable, not just consistent for
    its own sake.

    A ``join`` *right after* a masked ``search`` does not reproduce it:
    Microsoft always closes ``SearchOp.result_schema`` with at least a
    ``$table`` marker column (even over an open ``(*)`` parameter), so
    ``_overlay_result_schema`` always adds a second, anonymous entry
    alongside the masked one -- which defeats ``_resolve_side``'s
    *single*-entry fallback before masking ever needs to.

    Putting the masked ``search`` on the **right** side of a join does
    reproduce it: ``_flatten_side`` collapses a join's whole right-hand
    scope into one entry, and it decides that entry's ``table`` by reading
    every contributing entry's ``.table`` directly (not its columns) --
    "keeps a table when every contributing entry named the same one". A
    masked search-seeded entry with an unmasked label contributed its
    (colliding) name there, `_flatten_side` gave the merged right side that
    name outright, and ``_resolve_side``'s single-entry fallback (always
    reached for a join's right side, which is one entry by construction)
    read it straight back for an otherwise-unresolvable ``$right`` column.
    """
    ir = parse(
        "let f = (DeviceProcessEvents:(*)) { "
        "DeviceFileEvents | join (search in (DeviceProcessEvents) 'x') "
        "on $left.FileName == $right.Unknown "
        "}; DeviceProcessEvents | count",
        schema=schema,
    ).to_ir(attach_schema=schema)
    fn = ir.let_bindings[0].rhs_function
    refs = {c.name: c for c in find_all(fn, ColumnRef)}
    assert refs["FileName"].join_side == "left"
    assert refs["FileName"].table == "DeviceFileEvents"
    assert refs["Unknown"].join_side == "right"
    assert refs["Unknown"].table is None


def test_a_pattern_arms_body_is_walked_through_the_same_helper(schema):
    """``declare pattern`` reuses ``_walk_function_body`` with no parameters
    to mask: the arm's own columns still get their table, even though the
    arm has no parameters of its own and the call site acquires nothing
    from it either way."""
    ir = parse(
        'declare pattern P = (a:string) { '
        '("x") = { DeviceProcessEvents | where ProcessId > 1 '
        '| project AccountName }; '
        '}; DeviceProcessEvents | count',
        schema=schema,
    ).to_ir(attach_schema=schema)
    (stmt,) = ir.statements
    (match,) = stmt.matches
    refs = _refs(match)
    assert refs["ProcessId"] == {"DeviceProcessEvents"}
    assert refs["AccountName"] == {"DeviceProcessEvents"}


# Microsoft's per-operator schema (K-ARCH-1, Task 5.2) --------------------

def test_provenance_survives_an_authoritative_result_schema():
    """Taking names and types from the binder must not cost ``ColumnRef.table``.

    Microsoft's ``ResultType`` says which columns exist and how they are
    typed; it does not say which table each came from, and this walk is the
    only thing in the library that can. Replacing the scope with one
    anonymous entry carrying Microsoft's columns — the obvious shortcut —
    would silently drop provenance for every operator the binder can type.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, ProjectOp, SortOp, find_all

    schemas = {"DeviceProcessEvents": {"FileName": "string", "ProcessId": "long"}}
    ir = parse(
        "DeviceProcessEvents "
        "| where ProcessId > 4 "
        "| project FileName, ProcessId "
        "| sort by ProcessId desc",
        schema=schemas,
    ).to_ir()

    # The shortcut really did fire — otherwise this proves nothing.
    assert all(op.result_schema is not None for op in ir.main_pipeline.operators)

    project = next(op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp))
    sort = next(op for op in ir.main_pipeline.operators if isinstance(op, SortOp))
    assert {c.table for c in find_all(project, ColumnRef)} == {"DeviceProcessEvents"}
    assert {c.table for c in find_all(sort, ColumnRef)} == {"DeviceProcessEvents"}


def test_join_provenance_survives_an_authoritative_result_schema():
    """The per-side scope entries a join builds are still built.

    ``$left`` / ``$right`` resolve against a scope that has both sides in it,
    and a right-hand column has a table only because the join walk appended
    an entry for it. Both are things the overlay cannot reconstruct, so
    ``join`` / ``lookup`` / ``union`` / ``search`` keep a structural branch
    even where Microsoft answered — one that brings the sources into scope
    and derives no columns of its own.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, ProjectOp, find_all

    schemas = {
        "DeviceProcessEvents": {"DeviceId": "string", "FileName": "string"},
        "DeviceFileEvents": {"DeviceId": "string", "TimeGenerated": "datetime"},
    }
    ir = parse(
        "DeviceProcessEvents "
        "| join DeviceFileEvents on DeviceId "
        "| project FileName, TimeGenerated",
        schema=schemas,
    ).to_ir()

    assert all(op.result_schema is not None for op in ir.main_pipeline.operators)
    project = next(op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp))
    tables = {c.name: c.table for c in find_all(project, ColumnRef)}
    assert tables == {
        "FileName": "DeviceProcessEvents",
        "TimeGenerated": "DeviceFileEvents",
    }


def test_microsoft_declining_is_not_an_invitation_to_answer(builder, attacher):
    """``IRBuilder().build`` binds against globals with no tables at all.

    Every symbol it produces is open, so nothing is recorded — and nothing
    fills in for it. The schema is Microsoft's to state and it declined, so
    the pipeline says ``None``. What the caller's dict still buys is the
    other contract: every column reference is placed, from the dict the
    binder never saw.
    """
    ir = builder.build(
        "DeviceProcessEvents | project FileName, AccountName "
        "| where FileName == 'cmd.exe'"
    )
    assert all(op.result_schema is None for op in ir.main_pipeline.operators)
    attacher.enrich(ir)
    assert ir.main_pipeline.result_schema is None
    assert {c.table for c in find_all(ir, ColumnRef)} == {"DeviceProcessEvents"}


def test_count_closes_a_symbol_even_from_an_unknown_table(builder):
    """``IsOpen`` is per node, not per query, and ``count`` is the proof.

    ``T | count`` returns exactly ``Count:long`` whatever ``T`` is, so
    Microsoft closes the symbol there even though the source is unknown --
    which is why the decline is read off each operator rather than decided
    once for the whole parse.
    """
    ir = builder.build("Whatever | where x > 1 | count")
    where_op, count_op = ir.main_pipeline.operators
    assert where_op.result_schema is None
    assert count_op.result_schema.columns == {"Count": "long"}


def test_microsoft_decides_the_column_order_not_the_scope_grouping():
    """A join's output is ordered by the engine, not by which side it came from.

    ``ScopeEntry`` groups columns by originating table so provenance
    survives, which means merging the scope orders a join's output
    left-side-first. Microsoft emits ``DeviceId, FileName, DeviceId1,
    TimeGenerated`` — the pipeline schema has to be its list, in its order.
    """
    from kustology import parse

    schemas = {
        "DeviceProcessEvents": {"DeviceId": "string", "FileName": "string"},
        "DeviceFileEvents": {"DeviceId": "string", "TimeGenerated": "datetime"},
    }
    ir = parse(
        "DeviceProcessEvents | join DeviceFileEvents on DeviceId", schema=schemas,
    ).to_ir()
    assert list(ir.main_pipeline.result_schema.columns.items()) == [
        ("DeviceId", "string"),
        ("FileName", "string"),
        ("DeviceId1", "string"),
        ("TimeGenerated", "datetime"),
    ]


# --- the dict entry point -------------------------------------------------
#
# ``parse(q).to_ir(attach_schema=dict)`` is the public path for a caller who
# has a schema and no cluster to bind against. Since the reroute it re-binds
# through ``build_global_state`` + ``Analyze``, so Microsoft answers for every
# symbol it can close and this walk supplies provenance over the top. That is
# what the rest of this file exercises.

DICT_SCHEMA = {
    "L": {"k": "string", "a": "long", "shared": "string"},
    "R": {"k": "string", "b": "real", "shared": "string"},
    "T": {
        "k": "string", "a": "long", "t": "datetime",
        "d": "dynamic", "s": "string", "g": "guid",
    },
    "U": {"k": "string", "a": "string", "z": "long"},
}


def _dict_path(query: str, schemas: dict | None = None):
    """``parse(query).to_ir(attach_schema=…)`` with the collision-heavy dict.

    ``L`` and ``R`` share ``k`` and ``shared``, which is what makes join
    collisions observable, and ``U`` types ``a`` differently from ``T`` so a
    union conflict has to split.
    """
    return parse(query).to_ir(
        attach_schema=DICT_SCHEMA if schemas is None else schemas,
    )


def _tables(ir) -> dict[str, set]:
    """``{column name: {table, ...}}`` over every ``ColumnRef`` in the IR."""
    from kustology.ir import ColumnRef, find_all

    out: dict[str, set] = {}
    for c in find_all(ir, ColumnRef):
        out.setdefault(c.name, set()).add(c.table)
    return out


# K28 (provenance): ScopeEntry.origins ---------------------------------------


def test_project_carries_provenance_into_a_later_operator():
    """``project`` replaced the scope with a table-less entry, so every
    column reference *after* it lost its table -- the same column resolved
    to ``T`` before the project and to ``None`` after it, in one query."""
    ir = _dict_path("T | project a, k | where a > 1")
    assert _tables(ir)["a"] == {"T"}


def test_project_away_and_project_keep_carry_provenance():
    away = _dict_path("T | project-away s | where a > 1")
    assert _tables(away)["a"] == {"T"}
    keep = _dict_path("T | project-keep a | where a > 1")
    assert _tables(keep)["a"] == {"T"}


def test_distinct_carries_provenance():
    ir = _dict_path("T | distinct k | where k == 'x'")
    assert _tables(ir)["k"] == {"T"}


def test_project_rename_carries_provenance_under_the_new_name():
    """The renamed column is still the source table's column.

    ``kk`` is a name no scope entry holds, which is the shape that files
    anonymously for a join collision or a union split variant. It does not
    here, and the difference is that the query *says so*:
    ``project-rename kk = k`` names the input column outright, so
    :func:`~kustology.ir.binder._renamed_columns` threads ``kk -> k`` into
    the overlay and ``T`` survives the rename.
    """
    ir = _dict_path("T | project-rename kk = k | where kk == 'x'")
    tables = _tables(ir)
    assert tables["kk"] == {"T"}
    assert tables["k"] == {"T"}


def test_project_rename_provenance_needs_a_real_column_on_the_right():
    """The thread is only followed where there is an input name to follow.

    Every ``project-rename`` term the parser accepts has a ``ColumnRef`` on
    the right, so this guards a hand-built or unmodelled IR rather than a
    query: with no column to carry from, the target files anonymously
    instead of borrowing a neighbour's table.
    """
    from kustology.ir.binder import _renamed_columns
    from kustology.ir.expr import LiteralExpr
    from kustology.ir.query import Assignment, ProjectRenameOp
    from kustology.ir.spans import Span

    span = Span(text_start=0, width=1)
    op = ProjectRenameOp(
        span=span,
        columns=[
            Assignment(
                name="kk",
                expr=LiteralExpr(value=1, literal_kind="long", span=span),
                span=span,
            ),
        ],
    )
    assert _renamed_columns(op) == {}


def test_a_computed_column_has_no_table_and_does_not_borrow_one():
    """``origins`` must record "invented here", not inherit the neighbours'."""
    ir = _dict_path("T | project n = a + 1, k | where n > 1")
    tables = _tables(ir)
    assert tables["k"] == {"T"}
    assert tables["n"] == {None}


def test_summarize_keys_keep_provenance_and_aggregates_do_not():
    ir = _dict_path("T | summarize c = count() by k | where c > 1 and k == 'x'")
    tables = _tables(ir)
    assert tables["k"] == {"T"}
    assert tables["c"] == {None}


def test_an_ambiguous_unqualified_column_resolves_to_no_table():
    """``T | union U`` puts ``k`` in two scope entries with different tables.

    Picking the most recently appended side was a guess: KQL's own answer is
    that the unqualified name is ambiguous, so the honest provenance is
    "unknown", not "U".
    """
    ir = _dict_path("T | union U | where k == 'x'")
    assert _tables(ir)["k"] == {None}


# Joins: what the overlay cannot reconstruct ----------------------------------


def test_a_right_side_only_column_reports_the_right_table():
    """The entry the join walk appends is what gives a right-hand column a
    table at all.

    ``kind=rightsemi`` emits the right side's columns only, and ``b`` is
    ``R``'s alone, so it places there even though the operator that produced
    it is on the left of the pipeline.
    """
    from kustology.ir import FilterOp

    ir = _dict_path("L | join kind=rightsemi (R) on k | where b > 1")
    where = next(op for op in ir.main_pipeline.operators if isinstance(op, FilterOp))
    assert {c.table for c in find_all(where, ColumnRef)} == {"R"}


def test_a_post_join_collision_is_ambiguous_and_says_so():
    """The accepted narrowing that came with retiring the renaming rule.

    Microsoft emits ``shared`` and ``shared1`` for ``L | join (R) on k``, and
    both sides are in scope with a ``shared`` of their own, so the walk
    cannot say which is which: the honest provenance for an unqualified name
    is ``None``. The old hand rule renamed the right side itself and so knew
    the answer by construction — it also had to invent the name, which is
    the part that kept disagreeing with the engine.

    A qualified reference is unaffected: ``$left`` / ``$right`` name a side
    outright, and that is what the sides are kept for.
    """
    from kustology.ir import JoinOp

    ir = _dict_path("L | join (R) on k | project shared, shared1")
    assert list(ir.main_pipeline.result_schema.columns) == ["shared", "shared1"]
    tables = _tables(ir)
    assert tables["shared"] == {None}
    assert tables["shared1"] == {None}

    qualified = _dict_path("L | join (R) on $left.shared == $right.shared")
    join = next(
        op for op in qualified.main_pipeline.operators if isinstance(op, JoinOp)
    )
    left, right = [c for e in join.on for c in find_all(e, ColumnRef)]
    assert (left.table, right.table) == ("L", "R")


# K10 / K11: resolving inside a join's on-clause ------------------------------


def _on_refs(ir, index: int = -1) -> list:
    """Every ``ColumnRef`` in the ``on`` clause of one join, in order."""
    from kustology.ir import ColumnRef, JoinOp, find_all

    joins = [op for op in ir.main_pipeline.operators if isinstance(op, JoinOp)]
    return [c for e in joins[index].on for c in find_all(e, ColumnRef)]


def test_dollar_left_resolves_by_name_across_the_whole_left_side():
    """``$left`` is the accumulated left row set, not the last entry in it.

    The rule read ``scope[-2]``, which is the entry appended by the *previous*
    join. After ``L | join (R) …`` a second join's ``$left.a`` therefore
    reported ``R`` — a table that does not have an ``a`` at all — while the
    column plainly comes from ``L``.
    """
    ir = _dict_path("L | join (R) on k | join (T) on $left.a == $right.a")
    left, right = _on_refs(ir)
    assert left.name == "a" and left.join_side == "left"
    assert left.table == "L"
    assert right.table == "T"


def test_dollar_right_resolves_against_the_appended_right_entry():
    ir = _dict_path("L | join (R) on $left.k == $right.b")
    left, right = _on_refs(ir)
    assert (left.table, right.table) == ("L", "R")


def test_dollar_right_resolves_through_the_right_pipelines_own_operators():
    """The right side is a pipeline, so its scope may be anonymous with the
    provenance carried in ``origins`` -- ``$right.b`` is still ``R``'s.

    A union on the right is the same question with several entries to
    reconcile: it is one row set and a join has one right side, so
    ``_flatten_side`` merges them and ``b``, which only one arm carries,
    keeps that arm's table.
    """
    ir = _dict_path("L | join (R | project b) on $left.k == $right.b")
    _left, right = _on_refs(ir)
    assert right.table == "R"

    unioned = _dict_path(
        "L | join (union (L | where a > 1), (R | where b > 1)) "
        "on $left.k == $right.b"
    )
    _left, right = _on_refs(unioned)
    assert right.table == "R"


def test_an_unresolvable_dollar_side_answers_none_and_keeps_its_side():
    """The sentinel never reaches `.table` anymore: an unresolvable side is
    honestly None, and `join_side` -- set by the builder even unbound -- is
    the side's carrier."""
    ir = _dict_path("L | join (datatable(z:long)[1]) on $left.k == $right.z")
    _left, right = _on_refs(ir)
    assert right.table is None
    assert right.join_side == "right"


def test_an_unenriched_dollar_ref_has_no_sentinel_either():
    (ref,) = [c for c in find_all(parse("L | join (R) on $left.k == $right.b").to_ir(), ColumnRef) if c.name == "k"]
    assert ref.table is None
    assert ref.join_side == "left"


def test_a_bare_on_key_resolves_against_the_left_side():
    """``on k`` is shorthand for ``$left.k == $right.k``, and both sides have
    a ``k``. The scope holds the right side too at that point, so the general
    ambiguity rule would answer ``None``; the left is the side the engine
    keeps the column from."""
    ir = _dict_path("L | join (R) on k")
    (key,) = _on_refs(ir)
    assert key.table == "L"


def test_lookups_bare_on_key_resolves_to_the_left_side_too():
    """``lookup`` drops the right side's key outright, so the left is the
    only side whose column survives -- the same answer for a stronger
    reason."""
    from kustology.ir import ColumnRef, LookupOp, find_all

    ir = _dict_path("L | lookup (R) on k")
    lookup = next(
        op for op in ir.main_pipeline.operators if isinstance(op, LookupOp)
    )
    (key,) = [c for e in lookup.on for c in find_all(e, ColumnRef)]
    assert key.table == "L"


# Re-enriching one IR: the schema is fixed at bind time -----------------------


def test_enriching_twice_does_not_change_result_schema():
    """The invariant inverted when the walk stopped deriving schemas.

    ``enrich`` used to compute the shape, so handing a second attacher a
    different dict changed the answer. It now only ever writes back a copy
    of what Microsoft stamped, so a second call is a no-op on
    ``result_schema`` no matter what dict it carries — and *re-binding* is
    how a caller changes a schema. The operator-less branch is the one that
    reads the pipeline's own field back, so it is the one that has to be
    pinned.
    """
    ir = parse("T").to_ir(attach_schema={"T": {"a": "long", "s": "string"}})
    assert not ir.main_pipeline.operators, "premise: the operator-less branch"
    assert ir.main_pipeline.result_schema.columns == {"a": "long", "s": "string"}

    SchemaAttacher({"T": {"a": "real", "s": "guid"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long", "s": "string"}

    rebound = parse("T").to_ir(attach_schema={"T": {"a": "real", "s": "guid"}})
    assert rebound.main_pipeline.result_schema.columns == {"a": "real", "s": "guid"}


def test_enriching_twice_does_not_wipe_an_operator_less_let_binding():
    """The regression the unconditional snapshot exists to prevent.

    ``enrich`` reads each binding's ``result_schema`` to register what the
    alias holds. With no operators there is nothing else to read the shape
    off, so if the snapshot of the builder's value were skipped on a second
    call — as it used to be once ``schema_attached`` was set — the binding
    and everything resolving through it would go to ``None``.
    """
    ir = parse("let M = materialize(T); M | project a").to_ir(
        attach_schema={"T": {"a": "long"}},
    )
    binding = ir.let_bindings[0]
    assert not binding.rhs_pipeline.operators, "premise: the operator-less branch"
    assert binding.rhs_pipeline.result_schema.columns == {"a": "long"}

    SchemaAttacher({"T": {"a": "real"}}).enrich(ir)
    assert binding.rhs_pipeline.result_schema.columns == {"a": "long"}
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


def test_enriching_twice_with_an_operator_present_is_also_a_no_op():
    """The other branch, for the same reason: the last operator's stamp is
    the answer and a second ``enrich`` copies it again."""
    ir = parse("T | where a > 1").to_ir(attach_schema={"T": {"a": "long"}})
    SchemaAttacher({"T": {"a": "real"}}).enrich(ir)
    assert ir.main_pipeline.result_schema.columns == {"a": "long"}


# K12: union ------------------------------------------------------------------


def test_union_split_columns_are_names_no_arm_ever_had():
    """The second accepted narrowing.

    ``T.a`` is a long and ``U.a`` a string, so the engine emits ``a_long``
    and ``a_string`` and no unsuffixed ``a`` at all. Those names exist only
    in Microsoft's answer -- neither arm's scope entry carries one -- so the
    overlay files them anonymously and they report ``None``. The old rule
    synthesised the split itself and could therefore keep a side per
    variant; it also had to guess when *not* to split, which is where it
    kept diverging.
    """
    ir = _dict_path("T | union U | where a_string == 'x' and a_long > 1")
    assert list(ir.main_pipeline.result_schema.columns)[:3] == [
        "k", "a_long", "a_string",
    ]
    tables = _tables(ir)
    assert tables["a_long"] == {None}
    assert tables["a_string"] == {None}


# K13 / K14: aggregate output column names ------------------------------------


def test_the_auto_name_lands_on_the_assignment_not_only_the_schema():
    """``Assignment.name`` is what a consumer reads to label the column."""
    from kustology import parse
    from kustology.ir import SummarizeOp

    ir = parse("T | summarize make_set(s), percentile(a, 95)").to_ir()
    op = next(
        o for o in ir.main_pipeline.operators if isinstance(o, SummarizeOp)
    )
    assert [a.name for a in op.aggregations] == ["set_s", "percentile_a_95"]


# search: an implicit source, so the seeding is provenance's job ---------------


def test_search_columns_keep_their_table():
    ir = _dict_path("search in (T) 'x' | where a > 1")
    assert _tables(ir)["a"] == {"T"}


def test_a_search_predicate_resolves_against_the_tables_being_searched():
    """``search`` has an implicit source, so the scope *before* it is empty.

    Its own predicate was filled against that empty scope, so
    ``search in (T) a > 1`` left ``a`` with no table while the same column one
    operator later (``search in (T) 'x' | where a > 1``) resolved to ``T``.
    The walk has the searched entries in hand; filling the predicate after it
    seeds them is what makes the two agree.
    """
    from kustology.ir import ColumnRef, SearchOp, find_all

    ir = _dict_path("search in (T) a > 1")
    search_op = next(
        op for op in ir.main_pipeline.operators if isinstance(op, SearchOp)
    )
    assert {c.table for c in find_all(search_op, ColumnRef)} == {"T"}


def test_search_provenance_survives_an_authoritative_result_schema():
    """``search`` keeps its structural branch on a bound parse, as ``join``
    does.

    It has an implicit source, so the pre-operator scope is empty and the
    overlay would file every column it emits as anonymous -- a following
    ``where a > 1`` would report no table for a column that plainly comes
    from ``T``.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, FilterOp, find_all

    schemas = {"T": {"k": "string", "a": "long"}}
    ir = parse("search in (T) 'x' | where a > 1", schema=schemas).to_ir()
    search_op = ir.main_pipeline.operators[0]
    assert search_op.result_schema is not None, "premise: Microsoft answered"
    where = next(
        op for op in ir.main_pipeline.operators if isinstance(op, FilterOp)
    )
    assert {c.table for c in find_all(where, ColumnRef)} == {"T"}


# find: the fourth source-bringing operator, seeded like search --------------


def test_find_seeds_its_tables_like_search():
    """`find in (T) where a > 1`'s predicate resolves against T -- the fourth
    source-bringing operator finally has its branch."""
    ir = parse("find in (T) where a > 1").to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"T"}


def test_find_project_columns_resolve_too():
    ir = parse("find in (T) where a > 1 project a").to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.project[0], ColumnRef)} == {"T"}


def test_a_find_or_search_over_a_let_alias_resolves_through_it():
    q = "let A = T | where a > 1; find in (A) where a > 5"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, FindOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, FindOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"A"}


def test_a_search_over_a_let_alias_resolves_through_it_too():
    """The sibling gap the map identified: `search`'s branch resolved a
    `LetRef` table's schema against nothing, even though `_source_entry`
    (a pipeline's own source position) has always threaded it through
    ``_let_schemas``."""
    q = "let A = T | where a > 1; search in (A) a > 5"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    from kustology.ir import ColumnRef, SearchOp, find_all
    (op,) = [o for o in ir.main_pipeline.operators if isinstance(o, SearchOp)]
    assert {c.table for c in find_all(op.predicate, ColumnRef)} == {"A"}


# K28: "nothing is known" is None, not an empty schema ------------------------


def test_a_pipeline_the_walk_learned_nothing_about_has_no_result_schema():
    """``result_schema = {}`` is a claim: "this emits no columns".

    A query over a table nobody described emits an unknown set of columns,
    which is a different statement, and stamping ``{}`` made the two
    indistinguishable to a consumer.
    """
    ir = _dict_path("Unknown | take 1", {"T": {"a": "long"}})
    assert ir.main_pipeline.result_schema is None


def test_an_unmodelled_sub_pipeline_does_not_inherit_the_enclosing_scope():
    """A branch the builder could not model is not a branch that emits the
    input unchanged, so it gets no schema rather than the caller's.

    ``UnknownSource`` with no operators is the builder's "I could not model
    this at all"; every other implicit-source sub-pipeline (``mv-apply``,
    ``partition``, ``fork``, ``facet``) does run against the enclosing rows
    and does inherit.
    """
    from kustology.ir.binder import ScopeEntry
    from kustology.ir.query import Pipeline, UnknownSource
    from kustology.ir.spans import Span

    span = Span(text_start=0, width=1)
    branch = Pipeline(source=UnknownSource(raw_text="?", span=span), operators=[])
    attacher = SchemaAttacher(DICT_SCHEMA)
    inherited = [ScopeEntry(table="T", columns={"a": "long"})]

    assert attacher._walk_pipeline(branch, inherited) == []
    assert branch.result_schema is None


def test_enrich_does_not_clobber_a_type_the_binder_already_resolved():
    """The walk is not the only thing that knows a column's type.

    ``evaluate`` opens the symbol, so ``project`` gets no authoritative
    schema and the pipeline honestly reports ``None`` -- but the binder
    still typed the ``ColumnRef`` from the parse-time schema, and that is on
    the node whatever the pipeline says. An ``enrich`` that knows nothing
    about ``T`` must leave it alone rather than reset it.
    """
    from kustology import parse
    from kustology.ir import ColumnRef, KustoType, ProjectOp, find_all

    schemas = {"T": {"k": "string", "a": "long", "d": "dynamic"}}
    ir = parse(
        "T | evaluate bag_unpack(d) | project a", schema=schemas,
    ).to_ir(attach_schema=False)
    project = next(
        op for op in ir.main_pipeline.operators if isinstance(op, ProjectOp)
    )
    assert project.result_schema is None, "premise: evaluate opened the symbol"
    (ref,) = find_all(project, ColumnRef)
    assert ref.result_type == KustoType.LONG, "premise: the binder typed it"

    # An attacher that knows nothing about ``T`` -- the schema is on the IR,
    # not in this dict.
    SchemaAttacher({}).enrich(ir)
    assert ref.result_type == KustoType.LONG
    assert ir.main_pipeline.result_schema is None


# K28: schema_attached means a schema was actually available ------------------


def test_schema_attached_stays_false_when_no_schema_was_available():
    """``enrich`` set the flag unconditionally, so an attacher with no
    schemas, over an IR the binder could not type either, still reported the
    IR as enriched -- the flag said "these types are real" about nothing."""
    ir = IRBuilder().build("Unknown | take 1")
    SchemaAttacher().enrich(ir)
    assert ir.schema_attached is False


def test_schema_attached_is_true_for_a_schema_dict():
    ir = IRBuilder().build("Unknown | take 1")
    SchemaAttacher({"Unknown": {"a": "long"}}).enrich(ir)
    assert ir.schema_attached is True


def test_schema_attached_is_true_when_the_binder_answered():
    """``count`` closes its symbol whatever the source is, so the IR carries
    a real schema even though the attacher was given none."""
    ir = IRBuilder().build("Unknown | count")
    assert ir.main_pipeline.operators[-1].result_schema is not None
    SchemaAttacher().enrich(ir)
    assert ir.schema_attached is True


# Arithmetic is not a predicate ------------------------------------------------


def _syntactic(query: str, schemas: dict | None = None):
    """Enrich an IR built from a *syntactic-only* parse.

    ``KustoCode.Parse`` produces a tree with no semantics at all, so every
    ``Expr.result_type`` starts ``UNRESOLVED`` and ``_fill``'s own type
    fallback is the only thing that can set one. That is what makes it the
    harness for the fallback: on any bound path the binder has already
    answered and the fallback never runs.
    """
    from kustology.bridge import KustoCode

    ir = IRBuilder().build_from_code(KustoCode.Parse(query))
    assert all(
        op.result_schema is None for op in ir.main_pipeline.operators
    ), "premise: a syntactic-only parse leaves the type fallback to answer"
    return SchemaAttacher(
        schemas if schemas is not None else DICT_SCHEMA,
    ).enrich(ir)


def test_arithmetic_is_not_typed_as_a_boolean():
    """Every ``BinOp`` was typed ``bool``, arithmetic included.

    ``extend n = a + 1`` recorded ``n:bool`` -- the same answer the node
    gives for ``a > 1``, which is a predicate and this is not. ``bool`` is a
    wrong answer where "unresolved" is merely an incomplete one; the
    fallback does not do numeric promotion, and saying so is the honest
    position.
    """
    from kustology.ir import ExtendOp, KustoType

    ir = _syntactic("T | extend n = a + 1, flag = a > 1")
    extend = next(
        op for op in ir.main_pipeline.operators if isinstance(op, ExtendOp)
    )
    typed = {a.name: a.expr.result_type for a in extend.assignments}
    assert typed["flag"] == KustoType.BOOL
    assert typed["n"] == KustoType.UNRESOLVED


# Pipelines the builder could not model ---------------------------------------


def test_an_unparseable_query_gets_no_result_schema():
    """``UnknownSource`` with no operators is reachable from a real string.

    The builder emits it for anything it cannot model as a source at all, and
    the pipeline then claims nothing about its own output -- where before it
    claimed, with ``columns={}``, that the query emits no columns.
    """
    ir = IRBuilder().build("not a query at all")
    from kustology.ir.query import UnknownSource

    assert isinstance(ir.main_pipeline.source, UnknownSource)
    assert not ir.main_pipeline.operators
    SchemaAttacher(DICT_SCHEMA).enrich(ir)
    assert ir.main_pipeline.result_schema is None


@pytest.mark.parametrize("schema,query,expect", [
    # Two separate effects, and only the first is pre-existing.
    #
    # (1) An *unqualified* `search` seeds every table the dict describes --
    #     the dict standing in for "every table in the database". That is
    #     older than this walk and unchanged by it; cases 2 and 3 are it.
    #
    # (2) The seeded entries are now *appended* to whatever scope the
    #     operator inherited, where the old rule replaced the scope
    #     wholesale (`scope[:] = [...]`). This is new here, and it applies
    #     to a qualified `search in (U)` just as much as an unqualified one
    #     -- case 1 is qualified. Replacing the scope would be a statement
    #     about the operator's *output*, which is Microsoft's to make and
    #     which it does make; appending keeps the walk to what it is for.
    #
    # Both cost provenance only, and cost it as ambiguity rather than as a
    # wrong table: a name two entries disagree about answers `None`. Case 1
    # is (2) alone -- `T` (inherited) and `U` (searched) both have an `a`.
    # Case 3 is (1) and (2) together. Case 2 is the one where neither bites,
    # because only the inherited `T` has an `a` at all.
    ({"T": {"a": "long", "k": "string"}, "U": {"a": "long"}},
     "T | partition by k (search in (U) a > 1)", {"k": "T", "a": None}),
    ({"T": {"a": "long", "k": "string"}, "U": {"z": "long"}},
     "T | partition by k (search a > 1)", {"k": "T", "a": "T"}),
    ({"T": {"a": "long", "k": "string"}, "U": {"a": "long"}},
     "T | partition by k (search a > 1)", {"k": "T", "a": None}),
])
def test_search_inside_partition_resolves_scope(schema, query, expect):
    ir = parse(query).to_ir()
    SchemaAttacher(schema).enrich(ir)
    got = {c.name: c.table for c in find_all(ir, ColumnRef) if c.name in expect}
    assert got == expect


def test_the_unknown_column_sentinel_is_microsofts_word_and_only_microsofts():
    """``TabularSchema.columns`` maps a column to a type *string*, and the
    string for "no type known" is ``"unknown"`` — not
    ``KustoType.UNRESOLVED.value``, which is ``"unresolved"``.

    Two sentinels for one idea, and nothing said which lived where: a
    consumer reading ``Expr.result_type`` learns to test against
    ``KustoType.UNRESOLVED`` and then finds the *other* spelling one field
    away, in a plain ``dict[str, str]`` a ``KustoType`` never validates. The
    reason for the split is that ``columns`` values are Microsoft's type
    *names*: ``ScalarTypes.Unknown.Name`` is literally ``"unknown"``.

    Since the schema rules were retired there is only one producer of that
    dict — the builder, copying the binder's stamp — so the two spellings
    can no longer meet in one field by accident. Both halves are pinned:
    Microsoft's word arrives, and ``enrich`` cannot introduce a second
    spelling over the top of it because it no longer writes type strings at
    all.
    """
    import warnings

    from kustology import parse
    from kustology.ir.types import KustoType

    assert KustoType.UNRESOLVED.value == "unresolved"

    # Microsoft's schema parser types `n` `ScalarTypes.Unknown` and the
    # builder publishes its `Name`.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        bound = parse("T | project n | extend m = n", schema={"T": "(n:bogus)"}).to_ir()
    assert bound.main_pipeline.result_schema.columns == {"n": "unknown", "m": "unknown"}

    # A second pass over the same IR leaves it exactly as Microsoft left it.
    SchemaAttacher({}).enrich(bound)
    assert bound.main_pipeline.result_schema.columns == {"n": "unknown", "m": "unknown"}


# Task 3 hash-silence: table/result_type are volatile, join_side is not -----


def test_enrichment_is_hash_silent_for_a_join_and_a_find_query():
    """Both of this task's changes -- the sentinel's retirement and find's
    new seeding branch -- touch only ``ColumnRef.table`` (and, for find,
    ``result_type``), and both are stripped from the hash payload before
    ``semantic_hash`` is computed (``transforms.py``'s ``_VOLATILE_FIELDS``).
    ``join_side`` is written by the builder, not this walk, so enriching
    must not move either query's hash at all."""
    from kustology.ir import compute_semantic_hash
    from kustology.ir.binder import SchemaAttacher

    join_ir = parse("L | join (R) on $left.k == $right.b").to_ir(attach_schema=False)
    join_before = compute_semantic_hash(join_ir)
    SchemaAttacher(DICT_SCHEMA).enrich(join_ir)
    assert compute_semantic_hash(join_ir) == join_before

    find_ir = parse("find in (T) where a > 1").to_ir(attach_schema=False)
    find_before = compute_semantic_hash(find_ir)
    SchemaAttacher(DICT_SCHEMA).enrich(find_ir)
    assert compute_semantic_hash(find_ir) == find_before
