# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The five non-``let``, non-tabular statement kinds survive the lowering.

KQL admits five statement kinds beside ``let`` and a tabular expression::

    set querytrace;
    declare query_parameters(p:long = 1);
    declare pattern P = (a:string) { ("x") = { T | take 1 }; };
    alias database db1 = cluster('c').database('d');
    restrict access to (database("d"), T);

A builder that collects only ``let`` statements and pipelines reads none of
these, so what they say is absent from the IR and from ``semantic_hash``:
two different ``set query_now`` pins hash alike, and nothing records that a
statement was there.

``QueryIR.statements`` holds them in source order, one discriminated union
over the five modeled kinds plus a defensive ``UnknownStmt``. The hash
payload names the field explicitly, since ``compute_semantic_hash`` builds a
``{let_bindings, statements, main_pipeline, additional_pipelines}`` dict
instead of dumping the whole model, and a field left out of that dict is
invisible to the digest however well the builder populates it.

The pattern-body scoping pins live in ``test_let_bindings.py`` beside the
function-body ones they mirror; the minimal-pair collision rows live in
``test_hash_battery.py``.
"""

import pytest

from kustology import parse, validate
from kustology.ir import (
    AliasStmt,
    ColumnRef,
    IRBuilder,
    LetValueRef,
    LiteralExpr,
    PathExpr,
    PatternStmt,
    QueryIR,
    QueryParametersStmt,
    RestrictStmt,
    SetOptionStmt,
    TableRef,
    UnknownStmt,
    compute_semantic_hash,
    find_all,
)
from kustology.services import ANALYZE_FAILED_CODE

# The 12.4.1 binder crashes on this query: ``VisitPatternDeclaration`` indexes
# the *declared parameter* list with the *supplied value* index, so a match arm
# with more values than the pattern declares parameters runs off the end. The
# parse itself is clean, which makes it a live hazard.
ARITY_CRASH = (
    'declare pattern Logs = (Source:string)[Level:string] '
    '{ ("Kusto","Info") = { print "a" }; };\n'
    'Logs("Kusto").["Info"]'
)


def _ir(query: str, **kwargs) -> QueryIR:
    return parse(query, **kwargs).to_ir(attach_schema=False)


def _hash(query: str) -> str:
    return _ir(query).semantic_hash


def _only(query: str, type_):
    ir = _ir(query)
    (stmt,) = ir.statements
    assert isinstance(stmt, type_), f"{query!r} built {type(stmt).__name__}"
    return stmt


# -- set --------------------------------------------------------------------

def test_set_carries_its_name_and_value():
    stmt = _only("set query_now=datetime(2024-01-01); T | count", SetOptionStmt)
    assert stmt.name == "query_now"
    assert isinstance(stmt.value, LiteralExpr)
    assert stmt.value.literal_kind == "datetime"


def test_a_valueless_set_carries_no_value():
    """``set notruncation`` is a flag: the .NET ``ValueClause`` is ``None``,
    and ``value=None`` is the honest record rather than a synthesized one."""
    stmt = _only("set notruncation; T | count", SetOptionStmt)
    assert stmt.name == "notruncation"
    assert stmt.value is None


# -- declare query_parameters ----------------------------------------------

def test_query_parameters_carry_declared_types_and_defaults():
    stmt = _only(
        "declare query_parameters(p:long = 1, q:string); T | take 1",
        QueryParametersStmt,
    )
    assert [p.decl.name for p in stmt.parameters] == ["p", "q"]
    assert [p.decl.declared_type for p in stmt.parameters] == ["long", "string"]
    assert isinstance(stmt.parameters[0].default, LiteralExpr)
    assert stmt.parameters[0].default.value == 1
    assert stmt.parameters[1].default is None


def test_query_parameter_names_are_not_canonicalized():
    """A ``let`` name is a local label and is renamed on the hash's copy. A
    query parameter's name is the caller-facing API of the saved query, the
    thing a dashboard passes by name, so renaming it would merge two queries
    with different call contracts."""
    assert _hash("declare query_parameters(p:long); T | take 1") != _hash(
        "declare query_parameters(q:long); T | take 1"
    )


def test_a_function_bodys_query_parameters_are_scoped_to_the_body():
    """``FunctionBody``'s statement list admits ``let`` and
    ``declare query_parameters``; both are scoped to the body."""
    ir = _ir("let f = (x:long) { declare query_parameters(p:long = 1); T | take x }; f(1)")
    assert ir.statements == []
    fn = ir.let_bindings[0].rhs_function
    assert fn is not None
    (qp,) = fn.body_query_parameters
    assert [p.decl.name for p in qp.parameters] == ["p"]


# -- declare pattern --------------------------------------------------------

def test_a_forward_declared_pattern_is_declared_only():
    """``declare pattern P;`` declares the name and nothing else — the .NET
    ``Pattern`` child is ``None``, which is a different statement from a
    pattern whose body happens to be empty."""
    stmt = _only("declare pattern Logs; T | count", PatternStmt)
    assert stmt.name == "Logs"
    assert stmt.declared_only is True
    assert stmt.parameters == []
    assert stmt.matches == []


def test_a_full_pattern_carries_its_signature_and_every_match():
    stmt = _only(
        'declare pattern P = (a:string) '
        '{ ("x") = { T | take 1 }; ("y") = { U | take 2 }; }; V | count',
        PatternStmt,
    )
    assert stmt.name == "P"
    assert stmt.declared_only is False
    assert [p.name for p in stmt.parameters] == ["a"]
    assert [p.declared_type for p in stmt.parameters] == ["string"]
    assert stmt.path_parameter is None
    assert len(stmt.matches) == 2
    first, second = stmt.matches
    assert [v.value for v in first.values] == ["x"]
    assert first.path_value is None
    assert first.body_pipeline is not None
    assert first.body_pipeline.source.name == "T"
    assert second.body_pipeline.source.name == "U"


def test_a_pattern_path_parameter_and_path_value_are_modelled():
    """The ``[L:string]`` path parameter and the ``.["Info"]`` path value are
    the second half of the pattern's call shape — ``P("x").["Info"]`` reaches
    a different arm from ``P("x").["Warn"]``."""
    stmt = _only(
        'declare pattern P = (a:string)[L:string] '
        '{ ("x").["Info"] = { T | take 1 }; }; U | count',
        PatternStmt,
    )
    assert stmt.path_parameter is not None
    assert stmt.path_parameter.name == "L"
    assert stmt.path_parameter.declared_type == "string"
    (match,) = stmt.matches
    assert isinstance(match.path_value, LiteralExpr)
    assert match.path_value.value == "Info"


def test_a_pattern_body_scalar_tail_lands_on_body_expr():
    """The body is a ``FunctionBody``, so its tail dispatches exactly as a
    ``let`` function's does: tabular to ``body_pipeline``, scalar to
    ``body_expr``, never both."""
    stmt = _only(
        'declare pattern P = (a:string) { ("x") = { 1 + 2 }; }; T | count',
        PatternStmt,
    )
    (match,) = stmt.matches
    assert match.body_pipeline is None
    assert match.body_expr is not None
    assert match.body_expr.op == "+"


# -- alias database ---------------------------------------------------------

def test_alias_carries_the_name_and_the_expression():
    stmt = _only(
        "alias database db1 = cluster('c').database('d'); T | count", AliasStmt,
    )
    assert stmt.name == "db1"
    assert isinstance(stmt.expression, PathExpr)


# -- restrict access --------------------------------------------------------

def test_restrict_carries_its_targets_and_properties():
    stmt = _only(
        'restrict access to (database("d"), T) with (a=1, b="x"); T | count',
        RestrictStmt,
    )
    assert len(stmt.expressions) == 2
    # The shared ``named_param_value`` reader unquotes a literal, so values
    # come back decoded, as they do for ``parse-kv``'s ``with (...)`` clause.
    assert stmt.properties == [("a", "1"), ("b", "x")]


def test_restrict_with_a_single_literal_property_keeps_it():
    stmt = _only("restrict access to (T) with (a=1); T | count", RestrictStmt)
    assert stmt.properties == [("a", "1")]


def test_restrict_with_a_non_literal_property_keeps_it():
    """The grammar admits only a literal or a bare name in this slot: a call,
    a parenthesized expression, or a timespan literal each diagnose ``Missing
    value`` on a real parse. A bare name parses as a ``NameDeclaration``, not
    the ``NameReference`` ``named_param_value`` special-cases, so it reaches
    the ``node_text`` fallback and still lands in ``properties``."""
    stmt = _only("restrict access to (T) with (a=b); T | count", RestrictStmt)
    assert ("a", "b") in stmt.properties


def test_restrict_without_a_with_clause_has_no_properties():
    stmt = _only("restrict access to (T); T | count", RestrictStmt)
    assert stmt.properties == []


def test_restrict_over_a_let_bound_view_reads_the_binding():
    """The statement sweep runs after the ``let`` sweep, so a name an earlier
    ``let`` bound resolves to the binding rather than to a column of whatever
    the pipeline reads."""
    ir = _ir("let V = T | take 1; restrict access to (V); T | count")
    (stmt,) = ir.statements
    (target,) = stmt.expressions
    assert isinstance(target, LetValueRef)
    assert target.name == "V"


def test_a_restrict_target_that_is_not_bound_stays_a_plain_name():
    ir = _ir("restrict access to (T); T | count")
    (stmt,) = ir.statements
    (target,) = stmt.expressions
    assert isinstance(target, ColumnRef)
    assert target.name == "T"


# -- ordering and the empty case -------------------------------------------

def test_statements_are_kept_in_source_order():
    ir = _ir(
        "set notruncation; alias database d1 = cluster('c').database('d'); "
        "declare pattern P; T | count"
    )
    assert [type(s) for s in ir.statements] == [SetOptionStmt, AliasStmt, PatternStmt]


def test_a_query_with_no_statements_has_an_empty_list():
    assert _ir("T | count").statements == []
    assert _ir("let a = 5; T | where x > a").statements == []


def test_statement_order_is_hashed():
    """``set`` scopes the query it precedes, so the order two of them were
    written in is part of what the query says."""
    assert _hash("set notruncation; set querytrace; T | count") != _hash(
        "set querytrace; set notruncation; T | count"
    )


def test_no_modelled_statement_falls_through_to_unknown():
    for query in (
        "set notruncation; T | count",
        "declare query_parameters(p:long); T | count",
        "declare pattern P; T | count",
        "alias database d1 = cluster('c').database('d'); T | count",
        "restrict access to (T); T | count",
    ):
        assert not list(find_all(_ir(query), UnknownStmt)), query


@pytest.mark.parametrize("query", [
    "set; T | count",
    "declare query_parameters(); T | count",
    "declare query_parameters; T | count",
    "declare pattern; T | count",
    "declare pattern P = ; T | count",
    "declare pattern P = (a:string) { }; T | count",
    'declare pattern P = (a:string) { ("x") = { }; }; T | count',
    "declare pattern P = () { }; T | count",
    "alias database; T | count",
    "alias database D = ; T | count",
    "restrict access to; T | count",
    "restrict access to (); T | count",
    "restrict access to (T) with (); T | count",
])
def test_a_recovered_statement_builds_rather_than_raising(query):
    """KQL's parser recovers from a half-written statement by synthesizing the
    missing children, so every reader here meets nodes the grammar requires
    and the source never wrote. Raising on one turns a diagnosable typo into
    an exception out of ``to_ir()``."""
    ir = _ir(query)
    assert len(ir.statements) == 1
    assert not list(find_all(ir, UnknownStmt))


# -- the hash responds ------------------------------------------------------

def test_compute_semantic_hash_agrees_with_the_stored_field():
    ir = _ir("set querytrace; T | count")
    assert compute_semantic_hash(ir) == ir.semantic_hash


# -- the subtree is reachable ----------------------------------------------

def test_walk_reaches_into_a_statement():
    """``find_all`` is the documented traversal; an analyzer built on it
    needs to see what a statement says, not just the pipeline."""
    ir = _ir('declare pattern P = (a:string) { ("x") = { T | take 1 }; }; U | count')
    assert sorted(t.name for t in find_all(ir, TableRef)) == ["T", "U"]


def test_json_round_trip_keeps_every_statement():
    ir = _ir(
        "set query_now=datetime(2024-01-01); "
        'restrict access to (T) with (a=1); U | count'
    )
    reloaded = QueryIR.model_validate_json(ir.model_dump_json())
    assert reloaded.model_dump() == ir.model_dump()
    assert [type(s) for s in reloaded.statements] == [SetOptionStmt, RestrictStmt]


@pytest.mark.parametrize("query", [
    "set querytrace; T | count",
    'declare pattern P = (a:string) { ("x") = { T | take 1 }; }; U | count',
])
def test_llm_view_renders_statements(query):
    """``to_llm_dict`` derives from ``model_fields``, so the field appears
    with no per-field rule. Pinned so a view change cannot drop it silently."""
    view = _ir(query).to_llm_dict()
    assert len(view["statements"]) == 1


def test_the_binder_tolerates_a_statement_carrying_query(sample_schema):
    """The four statement kinds the provenance pass still does not walk --
    ``set``, ``query_parameters``, ``alias``, ``restrict`` -- must not trip it
    either: the main pipeline still enriches."""
    ir = parse(
        "set notruncation; DeviceFileEvents | where FileName == 'cmd.exe'",
        schema=sample_schema,
    ).to_ir()
    assert ir.main_pipeline.result_schema is not None
    (col,) = find_all(ir.main_pipeline, ColumnRef)
    assert col.table == "DeviceFileEvents"


# -- the binder crash is contained -----------------------------------------
#
# Every entry point that calls into Microsoft's analyzer goes through one
# guard: try to analyze, and on a .NET exception fall back to the unanalyzed
# parse plus a kustology-owned ``Error`` diagnostic. ``semantic_hash`` is
# bind-invariant, so the fallback digest is the right digest.

def test_the_repro_still_crashes_microsofts_binder_unguarded():
    """The guard is only worth its tests while the crash is real. A failure
    here means a DLL refresh fixed the binder and the pins below exercise
    nothing; rewrite them against a new repro or retire them."""
    from kustology.bridge import GlobalState, KustoCode

    assert parse(ARITY_CRASH).diagnostics == []
    raised: BaseException | None = None
    try:
        KustoCode.ParseAndAnalyze(ARITY_CRASH, GlobalState.Default)
    except Exception as exc:  # noqa: BLE001 — that *anything* escapes is the claim
        raised = exc
    assert raised is not None, "the binder no longer crashes on the repro"
    assert type(raised).__name__ == "IndexOutOfRangeException"


def _crashes(diagnostics) -> list:
    """The guard's own rows, from either diagnostic shape (dicts or models)."""
    return [
        d for d in diagnostics
        if (d["code"] if isinstance(d, dict) else d.code) == ANALYZE_FAILED_CODE
    ]


def test_to_ir_on_an_unbound_parse_survives_the_crash():
    """``core.py``'s default-globals seam."""
    ir = parse(ARITY_CRASH).to_ir()
    (crash,) = _crashes(ir.diagnostics)
    assert crash.severity == "Error"
    assert ir.semantic_hash.startswith("kustology-sem-v2:")


def test_to_ir_with_a_schema_dict_survives_the_crash():
    """``core.py``'s dict-rebind seam."""
    ir = parse(ARITY_CRASH).to_ir(attach_schema={"T": {"a": "long"}})
    assert len(_crashes(ir.diagnostics)) == 1


def test_the_ir_builder_entry_point_survives_the_crash():
    """``builder.py``'s ``ParseAndAnalyze`` seam."""
    ir = IRBuilder().build(ARITY_CRASH)
    assert len(_crashes(ir.diagnostics)) == 1


def test_parse_with_a_schema_survives_the_crash():
    """``services.parse``'s seam. The receiver keeps the unbound tree, so it
    reports ``has_semantics=False`` rather than claiming a binding it does
    not have."""
    query = parse(ARITY_CRASH, schema={"T": {"a": "long"}})
    assert len(_crashes(query.diagnostics)) == 1
    assert query.has_semantics is False


def test_validate_with_a_schema_survives_the_crash():
    """``services.validate``'s seam."""
    diags = validate(ARITY_CRASH, schema={"T": {"a": "long"}})
    (crash,) = _crashes(diags)
    assert crash["severity"] == "Error"


def test_the_crash_diagnostic_names_the_dotnet_exception():
    """The exception reaches the diagnostic's ``detail`` field, so the failure
    stays reportable upstream through ``to_ir()`` while ``message`` stays one
    short sentence."""
    (crash,) = _crashes(parse(ARITY_CRASH).to_ir().diagnostics)
    assert "IndexOutOfRangeException" in crash.message
    assert "\n" not in crash.message
    assert "VisitPatternDeclaration" in crash.detail


def test_the_fallback_digest_is_the_bound_digest():
    """``semantic_hash`` is bind-invariant, so falling back to the unanalyzed
    parse costs the binder's types and provenance but leaves the digest.
    Pinned on a query the binder handles, since the crashing one has no bound
    digest to compare against."""
    query = "T | where a > 1"
    assert (
        parse(query).to_ir().semantic_hash
        == parse(query, schema={"T": {"a": "long"}}).to_ir().semantic_hash
    )


def test_a_clean_query_gets_no_crash_diagnostic():
    assert _crashes(parse("T | count").to_ir().diagnostics) == []
    assert _crashes(validate("T | count", schema={"T": {"a": "long"}})) == []
