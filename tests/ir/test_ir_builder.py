# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Test core IR builder behavior.

Structural hash stability, JSON serialization, and binder enrichment with an
inline schema.
"""

import logging

import pytest

from kustology import parse
from kustology.ir import (
    BinOp,
    ColumnRef,
    FilterOp,
    FuncCall,
    IRBuilder,
    KustoType,
    LiteralExpr,
    Pipeline,
    QueryIR,
    Span,
    TableRef,
    TakeOp,
    TopHittersOp,
    ToScalarExpr,
    UnknownSource,
    find_all,
)
from kustology.ir.binder import SchemaAttacher


@pytest.fixture
def ir_builder():
    return IRBuilder()


@pytest.fixture
def binder(sample_schema):
    return SchemaAttacher(sample_schema)


def test_semantic_hash_carries_scheme_prefix(ir_builder):
    """The hash is prefixed with ``kustology-sem-v2:`` so the
    canonicalization rules themselves are versionable. Pinning the exact
    prefix keeps a future rename from slipping through silently.
    """
    ir = ir_builder.build("DeviceProcessEvents | where FileName == 'cmd.exe'")
    assert ir.semantic_hash.startswith("kustology-sem-v2:"), ir.semantic_hash
    # Digest portion is 64 hex chars — full SHA-256.
    digest = ir.semantic_hash.split(":", 1)[1]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_compute_semantic_hash_accepts_subtree(ir_builder):
    """``compute_semantic_hash`` must work on any IR ``BaseModel`` subtree,
    not only the root ``QueryIR``. That lets analyzers dedupe sub-shapes
    (for example, "have I seen this predicate before?").
    """
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
    equivalent.
    """
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
        let_bindings=[],
        main_pipeline=Pipeline(
            source=UnknownSource(raw_text="test", span=span),
            operators=[],
        ),
    )

    json_data = ir.model_dump_json()
    ir_back = QueryIR.model_validate_json(json_data)
    # A real recomputed digest survives the round trip -- not a coincidence
    # of two defaults, since it carries the scheme prefix.
    assert ir.semantic_hash.startswith("kustology-sem-v2:")
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


@pytest.mark.parametrize(
    "spelling, expected",
    [
        (r'@"\d+"', r"\d+"),          # verbatim: backslash is literal
        (r'"\\d+"', r"\d+"),          # escaped: same pattern, other spelling
        ("'[0-9]+'", "[0-9]+"),       # no backslash at all
        (r'@"^cmd\.exe$"', r"^cmd\.exe$"),
    ],
)
def test_regex_match_carries_the_pattern_text(ir_builder, spelling, expected):
    """``pattern`` holds the regex, not a placeholder.

    Asserted as an exact non-default value on a real parse across both KQL
    spellings of a backslash, because "is this field ever populated" is
    answered by a substring check on one query and a substring check is what
    lets an always-empty field look implemented.
    """
    from kustology.ir import RegexMatch, find_all

    (rm,) = find_all(ir_builder.build(f"T | where C matches regex {spelling}"), RegexMatch)
    assert rm.pattern == expected
    assert rm.pattern != ""


def test_two_different_regexes_differ_in_the_pattern_field(ir_builder):
    """Not merely in the digest. A field that always held ``""`` would still
    let the two queries hash apart -- ``raw_text`` and the spans differ --
    so the digest is not evidence that ``pattern`` carries anything.
    """
    from kustology.ir import RegexMatch, find_all

    (a,) = find_all(ir_builder.build(r'T | where C matches regex @"\d+"'), RegexMatch)
    (b,) = find_all(ir_builder.build(r'T | where C matches regex @"[a-z]+"'), RegexMatch)
    assert a.pattern != b.pattern
    assert (a.pattern, b.pattern) == (r"\d+", "[a-z]+")
    assert a.canonical_form != b.canonical_form


def test_an_illegal_escape_truncates_the_literal_for_every_reader(ir_builder):
    r"""``"\d+"`` is not a KQL string, and the empty ``pattern`` it yields is
    the parser's error recovery rather than a builder defect.

    ``\d`` is not one of KQL's escape sequences, so Microsoft's parser ends
    the string at the backslash and reports three diagnostics. What reaches
    the builder is a ``StringLiteralExpression`` whose text is a bare quote
    and whose ``LiteralValue`` is ``""`` -- there is no pattern left in the
    tree to extract, by any route. Pinned across ``matches regex`` *and* a
    plain equality so the next reader can see it is a property of the string
    literal and not of ``RegexMatch``: the same query written ``@"\d+"`` or
    ``"\\d+"`` carries the pattern fine, which the test above asserts.
    """
    from kustology import parse
    from kustology.ir import LiteralExpr, RegexMatch, find_all

    bad_regex = r'T | where C matches regex "\d+"'
    bad_literal = r'T | where C == "\d+"'

    # The queries really are malformed -- without this the assertions below
    # would also pass against a parser that accepted them and a builder that
    # dropped the value.
    for q in (bad_regex, bad_literal):
        codes = {d["code"] for d in parse(q).diagnostics}
        assert "KS002" in codes, (q, codes)

    (rm,) = find_all(ir_builder.build(bad_regex), RegexMatch)
    assert rm.pattern == ""
    (lit,) = find_all(ir_builder.build(bad_literal), LiteralExpr)
    assert lit.value == ""


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


def test_dynamic_element_type_is_populated_on_a_real_parse(ir_builder):
    """``result_type_inner`` on a bound parse, not merely its default.

    Asserting ``e.result_type_inner is None`` on a hand-built LiteralExpr
    passes identically whether the populating code works or probes a .NET
    member that does not exist; only a populated value is evidence.
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
        UnknownOp,
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
        assert not any(type(o) is Operator or isinstance(o, UnknownOp) for o in ops), (
            f"{query!r} produced an undispatched operator: "
            f"{[type(o).__name__ for o in ops]}"
        )
        # `find` parses as a leading operator in some forms; the bare-Operator
        # check above is the load-bearing assertion for that case.
        if expected_cls is not FindOp:
            assert matched, (
                f"{query!r} -> ops {[type(o).__name__ for o in ops]}; expected {expected_cls.__name__}"
            )


def test_tabular_subquery_in_membership_test_is_modeled(ir_builder):
    """Guards that `in ((P))` models the inner query as a real subtree
    rather than collapsing it into an UnknownExpr.

    The only corpus occurrence sits inside a `let` right-hand side, which
    the corpus gate walks. Asserted here rather than only via the corpus so
    a fixture change can't retire the coverage.
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


# --- externaldata ----------------------------------------------------------


def _external(query):
    from kustology.ir import ExternalDataExpr, IRBuilder, find_all

    return next(iter(find_all(IRBuilder().build(query), ExternalDataExpr)))


def test_externaldata_populates_uris_columns_and_format():
    """All three fields carry real values from the node, not placeholders.

    The data for all three is on the node, but the member is ``URIs`` --
    pythonnet member lookup is case-sensitive and silent about a miss, so a
    read of ``node.Uris`` returns None and leaves a placeholder in place
    without a warning. ``uris`` is plural because a singular field can hold
    only whichever URI comes first; see ``tests/ir/test_sources.py`` for the
    source-position form of the same construct, which shares the builder's
    reader with this one.
    """
    e = _external(
        'T | where C !in ((externaldata(id:string, n:long) '
        '[@"https://example.test/known.csv"] with (format="csv")))'
    )
    assert e.uris == ["https://example.test/known.csv"]
    assert e.columns == [("id", "string"), ("n", "long")]
    assert e.format == "csv"


def test_externaldata_without_a_with_clause_reports_no_format():
    """``format`` is None when the query does not state one -- never a
    placeholder string that reads as a real value.
    """
    e = _external('T | where C in ((externaldata(id:string) [@"https://example.test/x"]))')
    assert e.format is None
    assert e.columns == [("id", "string")]
    assert e.uris == ["https://example.test/x"]


def test_externaldata_in_the_corpus_is_modeled():
    """The bundled Sentinel fixture declares two columns and format=csv."""
    from pathlib import Path

    from kustology.ir import ExternalDataExpr, IRBuilder, find_all

    fixture = (
        Path(__file__).resolve().parent.parent
        / "fixtures" / "complex_queries" / "SuspiciousOAuthApp_OfflineAccess.kql"
    )
    ir = IRBuilder().build(fixture.read_text())
    e = next(iter(find_all(ir, ExternalDataExpr)))
    assert [n for n, _ in e.columns] == ["knownAppClientId", "knownAppDisplayName"]
    assert e.format == "csv"
    assert len(e.uris) == 1
    assert e.uris[0].startswith("https://")


# --- parse-kv and macro-expand ---------------------------------------------


def test_parse_kv_populates_its_declared_columns(ir_builder):
    """``ParseKvOp.columns`` holds the declared keys.

    ``Keys`` is a ``RowSchema``, which exposes ``Columns`` and has no
    ``Count`` -- so a guard probing ``keys is not None and hasattr(keys,
    "Count")`` never runs its loop body and leaves the field empty.
    """
    from kustology.ir import ParseKvOp, find_all

    ir = ir_builder.build("T | parse-kv Message as (b:string, c:long)")
    op = next(iter(find_all(ir, ParseKvOp)))
    assert op.columns == {"b": "string", "c": "long"}


def test_parse_kv_without_declared_keys_has_no_columns(ir_builder):
    from kustology.ir import ParseKvOp, find_all

    ir = ir_builder.build("T | parse-kv Message as (b:string) with (pair_delimiter=',')")
    op = next(iter(find_all(ir, ParseKvOp)))
    assert op.columns == {"b": "string"}


def test_macro_expand_models_its_inner_pipeline(ir_builder):
    """``MacroExpandOp.pipeline`` holds the inner pipeline. The member that
    carries it is ``StatementList``; probing ``Subquery`` or ``Body`` finds
    nothing and leaves the field None.
    """
    from kustology.ir import MacroExpandOp, Pipeline, TableRef, find_all

    ir = ir_builder.build("macro-expand EG as X (T | count)")
    op = next(iter(find_all(ir, MacroExpandOp)))
    assert isinstance(op.pipeline, Pipeline)
    assert [t.name for t in find_all(op.pipeline, TableRef)] == ["T"]


# --- set membership case sensitivity ---------------------------------------


@pytest.mark.parametrize(
    "op, case_sensitive, polarity",
    [
        ("in", True, "inclusion"),
        ("in~", False, "inclusion"),
        ("!in", True, "exclusion"),
        ("!in~", False, "exclusion"),
        ("has_any", False, "inclusion"),
        ("has_all", False, "inclusion"),
    ],
)
def test_set_membership_records_its_operator(
    ir_builder, op, case_sensitive, polarity
):
    """``op`` is the source of truth; polarity and case_sensitive are derived.

    Without it, ``polarity`` and ``case_sensitive`` are the only
    discriminators -- four states for six operators -- leaving ``in~``,
    ``has_any`` and ``has_all`` one indistinguishable node.
    """
    from kustology.ir import SetMembership, find_all

    ir = ir_builder.build(f'T | where C {op} ("a", "b")')
    m = next(iter(find_all(ir, SetMembership)))
    assert m.op == op
    assert m.case_sensitive is case_sensitive
    assert m.polarity == polarity


@pytest.mark.parametrize(
    "left, right",
    [
        # Opposite operators: OR of term matches vs AND of term matches.
        ('T | where C has_any ("a", "b")', 'T | where C has_all ("a", "b")'),
        # Term match vs whole-value equality.
        ('T | where C has_any ("a")', 'T | where C in~ ("a")'),
    ],
)
def test_distinct_membership_operators_do_not_collide(ir_builder, left, right):
    """``semantic_hash``'s contract is that different operators do not collide.

    These pairs collide unless the node records which operator produced it.
    ``has_any`` and ``has_all`` are semantically opposite.
    """
    a, b = ir_builder.build(left), ir_builder.build(right)
    assert a.semantic_hash != b.semantic_hash


def test_membership_canonical_form_names_the_real_operator(ir_builder):
    """``canonical()`` must spell the operator from ``op``. Rebuilding it
    from polarity + case_sensitive can only ever emit one of four strings,
    rendering ``has_any`` and ``has_all`` alike as ``in~`` -- a different
    predicate.
    """
    from kustology.ir import SetMembership, find_all

    for op in ("in", "!in", "in~", "!in~", "has_any", "has_all"):
        ir = ir_builder.build(f'T | where C {op} ("a")')
        m = next(iter(find_all(ir, SetMembership)))
        assert m.canonical_form == f'C {op} ("a")', op


def test_membership_operator_is_read_without_semantic_analysis(ir_builder):
    """``op`` comes from ``Operator.ToString()``, which a syntax-only parse
    has. ``ReferencedSymbol.OperatorKind`` would also identify the operator
    but is None unless the binder ran, which would make ``op`` depend on
    whether a schema was supplied.
    """
    from kustology import parse
    from kustology.ir import SetMembership, find_all

    unbound = parse('T | where C has_all ("a")').to_ir()
    m = next(iter(find_all(unbound, SetMembership)))
    assert m.op == "has_all"


# --- canonical_form coverage -----------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        ("T | where -X > 1", "-X > 1"),
        ("T | where D.a == 1", "D.a == 1"),
        ("T | where D['a'] == 1", 'D["a"] == 1'),
        ("T | where toscalar(S | summarize max(A)) > 1", "toscalar(S | ...) > 1"),
        ("T | where X in ((S | project X))", "X in ((S | ...))"),
        ("T | distinct *", "*"),
    ],
)
def test_canonical_form_covers_every_expr_shape(ir_builder, query, expected):
    """``canonical()`` must render every Expr type concretely. A shape it
    does not handle falls through to a bare ``"?"``, making ``-X > 1``,
    ``D.a == 1`` and ``toscalar(...) > 1`` indistinguishable as ``"? > 1"``.
    """
    from kustology.ir import Expr, FilterOp, find_all

    ir = ir_builder.build(query)
    filters = list(find_all(ir, FilterOp))
    if filters:
        node = filters[0].predicate
    else:  # `distinct *` has no predicate -- take the star itself
        node = next(e for e in find_all(ir, Expr) if type(e).__name__ == "StarExpr")
    form = node.canonical_form
    assert "?" not in form, f"unhandled Expr shape rendered as '?': {form}"
    assert form == expected


def test_canonical_form_distinguishes_shapes_that_used_to_collide(ir_builder):
    from kustology.ir import FilterOp, find_all

    def form(q):
        return next(iter(find_all(ir_builder.build(q), FilterOp))).predicate.canonical_form

    forms = {form("T | where -X > 1"), form("T | where D.a == 1"),
             form("T | where toscalar(S | summarize max(A)) > 1")}
    assert len(forms) == 3, f"distinct predicates collapsed to {forms}"


# --- canonical_form is faithful: precedence, escaping, KQL bool/null --------


def _form(query: str) -> str:
    """Return the canonical form of the one expression ``query``'s single
    operator holds -- a ``where``'s predicate or an ``extend``'s right-hand
    side.
    """
    from kustology.ir import ExtendOp, FilterOp, IRBuilder

    op = IRBuilder().build(query).main_pipeline.operators[0]
    if isinstance(op, FilterOp):
        return op.predicate.canonical_form
    assert isinstance(op, ExtendOp), type(op).__name__
    return op.assignments[0].expr.canonical_form


def test_a_disjunction_inside_a_conjunction_is_parenthesized():
    """``a and (b or c)`` and ``a and b or c`` are different predicates --
    ``or`` binds looser than ``and`` -- so rendering both as
    ``"a and b or c"`` shows anyone diffing two rules by canonical form, or
    reading one out of the LLM view, a predicate the query does not
    express.
    """
    assert _form("T | where a and (b or c)") == "a and (b or c)"


def test_a_conjunction_inside_a_disjunction_needs_no_parentheses():
    """The other direction, and the reason the renderer is a precedence
    table rather than "keep the parentheses the source wrote". ``and``
    already binds tighter than ``or``, so the parentheses in ``(a and b) or
    c`` are redundant: it is the *same* predicate as ``a and b or c``. The
    .NET tree does hold a ``ParenthesizedExpression`` for the first
    spelling, but ``_visit_expr`` unwraps it, so both build byte-identical
    IR and have to render one way. What matters is that it renders
    differently from the conjunction-of-a-disjunction above.
    """
    redundant = _form("T | where (a and b) or c")
    assert redundant == "a and b or c"
    assert redundant == _form("T | where a and b or c")
    assert redundant != _form("T | where a and (b or c)")


def test_arithmetic_is_parenthesized_by_precedence():
    assert _form("T | extend y = (x + y) * z") == "(x + y) * z"
    assert _form("T | extend y = x + y * z") == "x + y * z"


# Every ordered pair of same-precedence arithmetic operators. The rule under
# test is a property of *left-associativity*, which all of these share, not
# of the individual operator: a right operand of equal precedence cannot have
# come from an unparenthesized parse, so it always needs brackets back.
_SAME_PRECEDENCE_ARITHMETIC_PAIRS = [
    (outer, inner)
    for group in (("+", "-"), ("*", "/", "%"))
    for outer in group
    for inner in group
]


@pytest.mark.parametrize(
    "outer, inner",
    _SAME_PRECEDENCE_ARITHMETIC_PAIRS,
    ids=[f"{o}-{i}" for o, i in _SAME_PRECEDENCE_ARITHMETIC_PAIRS],
)
def test_a_right_operand_of_equal_precedence_is_parenthesized(outer, inner):
    """``x * (y / z)`` is not ``x * y / z``, and neither is ``x - (y - z)``
    ``x - y - z``.

    A rule keyed on the *parent* operator being one of ``-``, ``/``, ``%``
    -- "non-associative" in the sense that ``a - (b - c)`` differs from
    ``(a - b) - c`` -- is the right observation attached to the wrong
    operator. What makes the brackets load-bearing is that KQL's arithmetic
    is **left**-associative, so a right operand of equal precedence can only
    exist because the source wrote brackets; dropping them re-parses as the
    left-nested tree, which is a different tree. Rendering ``x * (y / z)``
    and ``x * y / z`` as one string while they hash apart misstates both --
    and under integer division they are different numbers: ``2 * (7 / 2)``
    is 6, ``2 * 7 / 2`` is 7.

    Parametrized over every ordered pair within each precedence group,
    including ``+``/``+`` and ``*``/``*``, whose brackets are redundant for
    exact arithmetic but not for floating point -- and which the renderer has
    no business deciding, since it does not know the operand types.
    """
    grouped = _form(f"T | extend r = x {outer} (y {inner} z)")
    flat = _form(f"T | extend r = x {outer} y {inner} z")
    assert grouped == f"x {outer} (y {inner} z)"
    assert flat == f"x {outer} y {inner} z"
    assert grouped != flat


def test_a_left_operand_of_equal_precedence_keeps_no_parentheses():
    """The other half of left-associativity: ``(x - y) - z`` *is* how
    ``x - y - z`` parses, so the brackets carry nothing and stay dropped.
    Bracketing both sides would be safe and unreadable.
    """
    assert _form("T | extend r = (x - y) - z") == "x - y - z"
    assert _form("T | extend r = (x / y) / z") == "x / y / z"


def test_a_higher_precedence_right_operand_still_needs_no_parentheses():
    """The boundary: only *equal* precedence forces the brackets. ``y * z``
    binds tighter than ``+``, so it can and did come from an unbracketed
    parse.
    """
    assert _form("T | extend r = x + y * z") == "x + y * z"
    assert _form("T | extend r = x - y * z") == "x - y * z"


def test_not_still_renders_its_own_parentheses():
    """``not`` renders as a call, so its operand never needs precedence
    parentheses on top of the ones already there.
    """
    assert _form("T | where not(a and b)") == "not(a and b)"


def test_a_quote_inside_a_string_literal_is_escaped():
    r"""``f("a\", \"b")`` is a call with ONE argument whose value contains
    quotes and a comma. Rendered without escaping it reads as
    ``f("a", "b")`` -- a call with two arguments, a different call. The
    canonical form is meant to be an unambiguous rendering of the tree, and
    that one describes a tree that does not exist.
    """
    assert _form(r'T | extend y = f("a\", \"b")') == r'f("a\", \"b")'


def test_a_backslash_in_a_string_literal_is_escaped():
    assert _form(r'T | where p == "C:\\Windows"') == r'p == "C:\\Windows"'


def test_a_bool_literal_renders_as_kql_not_python():
    """``True`` is Python's spelling. KQL's is ``true``, and the canonical
    form is supposed to be KQL.
    """
    assert _form("T | where x == true") == "x == true"
    assert _form("T | where x == false") == "x == false"


def test_a_null_literal_renders_as_null():
    assert _form("T | where x == real(null)") == "x == null"


def test_the_llm_view_drops_a_bool_literal_canonical_form_as_redundant():
    """The redundant-form drop compares ``canonical_form`` against a
    reconstruction of the literal, and the reconstruction spells a bool the
    KQL way. If ``canonical()`` spells it the Python way the two disagree,
    and every bool literal in the LLM view carries a ``canonical_form:
    "True"`` restating a ``value: true`` two lines above it.
    """
    from kustology.ir import IRBuilder, LiteralExpr, find_all, to_llm_dict

    ir = IRBuilder().build("T | where e == true")
    (lit,) = find_all(ir, LiteralExpr)
    dumped = to_llm_dict(lit)
    assert dumped["value"] is True
    assert "canonical_form" not in dumped


def test_the_llm_view_drops_an_escaped_string_canonical_form_as_redundant():
    """The redundant-form check has to spell a string exactly the way
    ``canonical()`` does, or it stops firing for the strings that need
    escaping. One function serves both sides.
    """
    from kustology.ir import IRBuilder, LiteralExpr, find_all, to_llm_dict

    ir = IRBuilder().build(r'T | where p == "C:\\Windows"')
    (lit,) = find_all(ir, LiteralExpr)
    dumped = to_llm_dict(lit)
    assert dumped["value"] == r"C:\Windows"
    assert "canonical_form" not in dumped


def test_a_redundant_bracket_still_renders_identically():
    """The boundary the precedence table must not cross. Parentheses that
    carry no grouping information are still dropped -- ``(X) > 1`` and
    ``X > 1`` are one predicate and must be one string.
    """
    assert _form("T | where (x) > 1") == _form("T | where x > 1") == "x > 1"


# --- Exists records which function produced it ------------------------------


@pytest.mark.parametrize(
    "fn, polarity",
    [
        ("isnotnull", "inclusion"), ("isnotempty", "inclusion"),
        ("isnull", "exclusion"), ("isempty", "exclusion"),
    ],
)
def test_exists_records_its_source_function_and_polarity(ir_builder, fn, polarity):
    """``op`` records which function produced the node; ``polarity`` its
    direction. With only ``target``, both positive functions lower to the
    same node with the same hash -- though ``isnotempty`` also rejects
    ``""`` -- and ``polarity`` is what lets the negative pair lower to
    ``Exists`` at all.
    """
    from kustology.ir import Exists, find_all

    ir = ir_builder.build(f"T | where {fn}(C)")
    e = next(iter(find_all(ir, Exists)))
    assert e.op == fn
    assert e.polarity == polarity


def test_all_four_null_tests_hash_distinctly(ir_builder):
    """The four are four different predicates: ``isnotempty`` also rejects
    ``""`` where ``isnotnull`` does not, and each pair is the other's
    negation. None of them may share a digest.
    """
    seen = {
        fn: ir_builder.build(f"T | where {fn}(C)").semantic_hash
        for fn in ("isnull", "isnotnull", "isempty", "isnotempty")
    }
    assert len(set(seen.values())) == 4, seen


def test_the_negative_null_tests_are_lowered_too(ir_builder):
    """``isnull`` / ``isempty`` lower to ``Exists`` alongside their
    negations. Lowering only the positive pair models one half of a
    symmetric family: a consumer asking "which columns does this query
    null-check" through ``find_all(ir, Exists)`` sees the positive tests and
    misses the negative ones -- and the shape it falls back on, a
    ``FuncCall`` named by string, is the shape ``Exists`` exists to
    replace.
    """
    from kustology.ir import Exists, FuncCall, find_all

    for fn in ("isnull", "isempty"):
        ir = ir_builder.build(f"T | where {fn}(C)")
        (e,) = find_all(ir, Exists)
        assert (e.op, e.polarity) == (fn, "exclusion")
        assert e.target.name == "C"
        assert not list(find_all(ir, FuncCall)), fn


def test_exists_polarity_is_dropped_from_the_llm_view(ir_builder):
    """Same rule as ``BinOp`` and ``SetMembership``: ``op`` already spells
    the negation, so ``polarity`` restates it.
    """
    from kustology.ir import Exists, find_all, to_llm_dict

    dumped = to_llm_dict(next(iter(find_all(ir_builder.build("T | where isnull(C)"), Exists))))
    assert dumped["op"] == "isnull"
    assert "polarity" not in dumped


# --- tolower/toupper equality rewrite is sound (K04) ------------------------


def test_tolower_equality_against_mismatched_case_literal_is_not_rewritten(ir_builder):
    """``tolower(X) == "Y"`` (capital Y) is always false -- ``tolower`` never
    returns anything but lowercase. ``X =~ "Y"`` is a case-insensitive match
    that is often true. Folding the first into the second would make
    hash-based dedup merge two predicates with different truth values.
    """
    from kustology.ir import BinOp, FuncCall, find_all

    always_false = ir_builder.build('T | where tolower(X) == "Y"')
    case_insensitive_match = ir_builder.build('T | where X =~ "Y"')
    assert always_false.semantic_hash != case_insensitive_match.semantic_hash
    # Pin that no rewrite happened at all, not just that the hash landed
    # somewhere different -- inequality alone is also satisfied by a future
    # rewrite that is wrong in a new way.
    assert [f.name for f in find_all(always_false, FuncCall)] == ["tolower"]
    assert next(iter(find_all(always_false, BinOp))).op == "=="


def test_tolower_equality_against_matching_case_literal_still_rewrites(ir_builder):
    """The genuinely-equivalent case: the literal is already lowercase, so
    ``tolower(X) == "y"`` and ``X =~ "y"`` agree for every value of X.
    """
    a = ir_builder.build('T | where tolower(X) == "y"')
    b = ir_builder.build('T | where X =~ "y"')
    assert a.semantic_hash == b.semantic_hash


def test_tolower_equality_rewrites_with_the_literal_on_either_side(ir_builder):
    a = ir_builder.build('T | where "y" == tolower(X)')
    b = ir_builder.build('T | where X =~ "y"')
    assert a.semantic_hash == b.semantic_hash


def test_toupper_equality_rewrite_is_symmetric_with_tolower(ir_builder):
    a = ir_builder.build('T | where toupper(X) == "Y"')
    b = ir_builder.build('T | where X =~ "Y"')
    assert a.semantic_hash == b.semantic_hash


def test_tolower_equality_against_a_non_literal_is_not_rewritten(ir_builder):
    """``tolower(X) == Col`` is not equivalent to ``X =~ Col`` -- ``Col`` is
    not a literal, so there is no fixed case to know the rewrite is sound
    for. Whatever value ``Col`` holds, some X that fails the exact-lowercase
    comparison would pass the case-insensitive one.
    """
    from kustology.ir import BinOp, FuncCall, find_all

    a = ir_builder.build("T | where tolower(X) == Col")
    b = ir_builder.build("T | where X =~ Col")
    assert a.semantic_hash != b.semantic_hash
    # Pin that no rewrite happened at all, not just that the hash landed
    # somewhere different.
    assert [f.name for f in find_all(a, FuncCall)] == ["tolower"]
    assert next(iter(find_all(a, BinOp))).op == "=="


# --- BinOp case sensitivity across the whole string-operator family ---------


@pytest.mark.parametrize(
    "op, case_sensitive",
    [
        # Comparison operators compare exactly.
        ("==", True), ("!=", True), ("<", True), (">", True),
        # The tilde forms fold case.
        ("=~", False), ("!~", False),
        # KQL string operators are case-INsensitive by default...
        ("has", False), ("contains", False), ("startswith", False),
        ("endswith", False), ("hasprefix", False), ("hassuffix", False),
        # ...their negations too -- negating a predicate does not change how
        # it compares. All six of these reported True.
        ("!has", False), ("!contains", False), ("!startswith", False),
        ("!endswith", False), ("!hasprefix", False), ("!hassuffix", False),
        # ...and only the _cs suffix makes one sensitive.
        ("has_cs", True), ("contains_cs", True), ("startswith_cs", True),
        ("!has_cs", True), ("!contains_cs", True),
    ],
)
def test_binop_case_sensitivity_follows_the_operator_suffix(
    ir_builder, op, case_sensitive
):
    """The flag derives from a hand-maintained allow-list, so anything
    absent falls through to True -- the failure shape this battery guards:
    an unlisted ``hasprefix``/``hassuffix`` or a missing negated form
    reports its case sensitivity backwards.
    """
    from kustology.ir import BinOp, find_all

    ir = ir_builder.build(f'T | where C {op} "a"')
    m = next(b for b in find_all(ir, BinOp) if b.op == op)
    assert m.case_sensitive is case_sensitive


@pytest.mark.parametrize("op", ["+", "-", "*", "/", "%"])
def test_arithmetic_binops_carry_no_case_or_polarity(ir_builder, op):
    """``a + 1`` is not case-sensitive and it is not an inclusion -- both
    fields are categories of *comparison*, and arithmetic is not one.

    Populating them anyway from comparison rules -- ``polarity`` from
    ``"!" in op`` and ``case_sensitive`` from the string-operator suffix
    check -- makes every ``+`` in every query report
    ``polarity="inclusion", case_sensitive=True``. Both readings are
    meaningless rather than merely uninteresting, and a consumer filtering
    ``walk(ir, lambda n: n.case_sensitive)`` -- the example in ``walk``'s own
    docstring -- has arithmetic answering it. ``None`` says "does not apply",
    which is the only true answer.
    """
    from kustology.ir import BinOp, find_all

    ir = ir_builder.build(f"T | extend y = a {op} 2")
    b = next(x for x in find_all(ir, BinOp) if x.op == op)
    assert b.case_sensitive is None
    assert b.polarity is None


def test_comparisons_and_string_operators_keep_their_flags(ir_builder):
    """The other side of the boundary: only arithmetic loses the fields.
    A comparison compares exactly and a string operator folds case, and both
    are real answers that must survive.
    """
    from kustology.ir import BinOp, find_all

    eq = next(iter(find_all(ir_builder.build('T | where C == "a"'), BinOp)))
    assert (eq.case_sensitive, eq.polarity) == (True, "inclusion")

    nothas = next(iter(find_all(ir_builder.build('T | where C !has "a"'), BinOp)))
    assert (nothas.case_sensitive, nothas.polarity) == (False, "exclusion")


def test_the_llm_view_omits_the_inapplicable_flags(ir_builder):
    """A ``null`` field is worse than an absent one for a model reading the
    dump: it invites the question of what a null case-sensitivity means.
    """
    from kustology.ir import BinOp, find_all, to_llm_dict

    ir = ir_builder.build("T | extend y = a + 2")
    dumped = to_llm_dict(next(iter(find_all(ir, BinOp))))
    assert "case_sensitive" not in dumped
    assert "polarity" not in dumped
    assert dumped["op"] == "+"


def test_search_colon_folds_case_like_has(ir_builder):
    """``search Col:'x'`` is KQL's shorthand for ``Col has 'x'`` -- a
    term match, and term matches fold case.

    ``:`` needs its own entry in ``_is_case_sensitive_op``'s rules;
    unlisted, it falls through to the "everything else is a comparison"
    default and reports ``case_sensitive=True``. Rules reading that flag
    would call ``search Col:'x'`` an exact match and ``Col has 'x'`` a
    folded one, though Kusto runs both the same way.
    """
    from kustology.ir import BinOp, SearchOp, find_all

    (search,) = find_all(ir_builder.build("search Col:'x'"), SearchOp)
    assert isinstance(search.predicate, BinOp)
    assert search.predicate.op == ":"
    assert search.predicate.case_sensitive is False
    assert search.predicate.polarity == "inclusion"


def test_case_folding_variants_do_not_collide(ir_builder):
    """``has`` and ``has_cs`` are different predicates and must not share a
    hash. ``op`` alone already keeps the digests apart; the flag each node
    carries must also agree with the operator it names.
    """
    seen = {
        op: ir_builder.build(f'T | where C {op} "a"').semantic_hash
        for op in ("has", "has_cs", "!has", "!has_cs", "contains", "contains_cs")
    }
    assert len(set(seen.values())) == len(seen)


def test_exists_canonical_form_names_the_real_function(ir_builder):
    """``canonical()`` must spell the source function; a generic
    ``exists(...)`` is a spelling that appears in no KQL query.
    """
    from kustology.ir import Exists, find_all

    for fn in ("isnotnull", "isnotempty"):
        ir = ir_builder.build(f"T | where {fn}(C)")
        e = next(iter(find_all(ir, Exists)))
        assert e.canonical_form == f"{fn}(C)", fn


def test_materialize_appears_only_on_a_let_right_hand_side():
    """Pins the grammar fact that makes ``MaterializeExpr`` unnecessary.

    ``materialize`` is a KQL keyword the parser accepts in exactly one
    position -- as a ``let`` statement's right-hand side -- where
    ``_visit_pipeline`` turns it into a nested ``Pipeline``. It is never an
    expression, so the IR has no expression node for it.

    Written against the .NET tree rather than the IR, so a DLL refresh that
    widens the grammar turns this red instead of silently introducing an
    unmodeled shape.
    """
    from Kusto.Language import KustoCode

    import kustology  # noqa: F401  -- loads the CLR

    def parents_of_materialize(query):
        code = KustoCode.Parse(query)
        found = []

        def walk(node, parent):
            if node is None:
                return
            if type(node).__name__ == "MaterializeExpression":
                found.append(type(parent).__name__ if parent else "<root>")
            for i in range(node.ChildCount):
                walk(node.GetChild(i), node)

        walk(code.Syntax, None)
        return found, code.GetSyntaxDiagnostics().Count

    # The one valid position.
    parents, diags = parents_of_materialize("let A = materialize(S | count); A | count")
    assert diags == 0
    assert parents == ["LetStatement"]

    # Every expression position is a parse error, and none builds a node.
    # Note `.Count` rather than truthiness: an empty .NET IReadOnlyList is
    # truthy under pythonnet, so `if not diagnostics` never fires.
    for query in (
        "T | where materialize(S | count) > 1",
        "T | extend X = materialize(S | count)",
        "T | where X in ((materialize(S | count)))",
        "T | where toscalar(materialize(S | count)) > 1",
        "materialize(S | count) | count",
        # Cannot hide under the paren unwrap in _visit_let_statement either.
        "let A = (materialize(S | count)); A | count",
    ):
        parents, diags = parents_of_materialize(query)
        assert diags > 0, query
        assert [p for p in parents if p != "LetStatement"] == [], query


def test_semantic_info_probe_fallthrough_logs_debug(caplog):
    """`map_semantic_info`'s inner ElementType probe is wrapped in a bare
    `except Exception`, because .NET member access can raise things other
    than AttributeError (see the `getattr` gotchas in AGENTS.md). A silent
    `except: pass` there would make a real binder bug indistinguishable
    from "no element type" -- both produce `result_type_inner is None`.

    This drives that except clause with a mock node whose `ResultType`
    reports `ElementType` present (not None, so the probe is entered) but
    whose `ElementType.Name` raises on access. The outer `result_type`
    assignment must still happen (only the inner probe is defensive), and
    the exception must be logged at DEBUG rather than swallowed silently.
    """
    from types import SimpleNamespace

    from kustology.ir._builder_helpers import map_semantic_info

    class _Boom:
        """Stand in for an ElementType symbol whose Name access raises --
        for example, a .NET member lookup failure that isn't an
        AttributeError.
        """

        @property
        def Name(self):
            raise RuntimeError("probe")

    node = SimpleNamespace(
        ResultType=SimpleNamespace(Name="dynamic", ElementType=_Boom())
    )
    expr = LiteralExpr(value="[]", literal_kind="dynamic", span=Span(text_start=0, width=2))

    with caplog.at_level(logging.DEBUG, logger="kustology.ir._builder_helpers"):
        map_semantic_info(node, expr)

    # The outer probe (ResultType.Name -> "dynamic") is unaffected by the
    # inner probe's failure -- it's a non-default value, not the field's
    # declared default, so this line would fail if the outer assignment
    # were accidentally guarded by the same try/except.
    assert expr.result_type == KustoType.DYNAMIC
    # Substring of the real message _builder_helpers.py emits.
    assert "inner result-type probe fell through" in caplog.text


@pytest.mark.parametrize("q", [
    "let n = 10; T | take n",
    "T | take toscalar(U | count)",
    "let n = 5; T | top n by x",
    "let n = 3; T | sample n",
    "let n = 3; T | sample-distinct n of a",
    "let n = 3; T | top-hitters n of a by b",
])
def test_non_literal_counts_build(q):
    """K01: KQL allows any scalar expression as a take / sample / top
    count, and ``let n = 10; T | take n`` / ``take toscalar(...)`` are both
    ordinary, valid queries (the latter is common in real Sentinel hunting
    queries). Coercing the count with ``int(node.ToString())`` raises
    ``ValueError`` on anything that isn't a bare integer literal. Building
    the IR must not raise, and the count must come through as the visited
    expression rather than being coerced or defaulted to a number.
    """
    ir = parse(q).to_ir()                       # must not raise
    op = ir.main_pipeline.operators[-1]
    assert not isinstance(op.count, int)        # an expression, not a number


def test_literal_take_count_is_int():
    """The common case -- a literal count -- returns a plain ``int`` so
    ``op.count == 5`` assertions (and downstream consumers keyed on the
    field being a number) work verbatim; only the non-literal case widens
    to an expression.
    """
    op = parse("T | take 5").to_ir().main_pipeline.operators[0]
    assert op.count == 5 and isinstance(op.count, int)


def test_count_field_round_trips_correct_shape_through_json():
    """``count: int | AnyExpr`` lists ``int`` first per the repo's
    union-ordering convention. This test has two parts.

    The first round-trips both shapes through the wire format end to end
    -- ``QueryIR.model_validate_json(ir.model_dump_json())`` on real
    builder output -- and confirms each survives as the class it started
    as. That covers the pipeline this repo actually runs, but the "why" is
    not declaration order: every ``AnyExpr`` member is a dict-shaped,
    ``extra="forbid"`` ``BaseModel`` keyed by a ``kind`` discriminator, and
    a bare JSON integer can never satisfy one -- Pydantic's smart union
    mode picks correctly regardless of declared order for *this* type
    combination. So declaration order is not a behavioral surface worth
    pinning (asserting it via ``__annotations__``/``get_type_hints`` would
    just encode an implementation detail that provably doesn't affect
    outcomes).

    The second part is the actual tripwire for the union surface: it
    hand-writes both payload shapes -- independent of anything the IR
    builder produces -- and validates them directly against ``TakeOp``. A
    bare integer must resolve to ``int``, not get wrapped or coerced into
    an expression model; a ``kind``-tagged object must resolve to that
    expression class, not get flattened to a number or silently dropped.
    This is what fails if a future Pydantic version, or a ``model_config``
    change, starts coercing in either direction -- the risk that actually
    matters here, as opposed to declaration order.
    """
    literal_ir = parse("T | take 5").to_ir()
    rebuilt_op = QueryIR.model_validate_json(
        literal_ir.model_dump_json()
    ).main_pipeline.operators[0]
    assert rebuilt_op.count == 5
    assert isinstance(rebuilt_op.count, int)

    expr_ir = parse("T | take toscalar(U | count)").to_ir()
    rebuilt_expr_op = QueryIR.model_validate_json(
        expr_ir.model_dump_json()
    ).main_pipeline.operators[-1]
    assert isinstance(rebuilt_expr_op.count, ToScalarExpr)

    literal_payload = {
        "kind": "take",
        "span": {"text_start": 0, "width": 1},
        "count": 5,
    }
    literal_op = TakeOp.model_validate(literal_payload)
    assert literal_op.count == 5
    assert isinstance(literal_op.count, int)

    expr_payload = {
        "kind": "take",
        "span": {"text_start": 0, "width": 1},
        "count": {
            "kind": "column_ref",
            "span": {"text_start": 0, "width": 1},
            "name": "n",
        },
    }
    expr_op = TakeOp.model_validate(expr_payload)
    assert isinstance(expr_op.count, ColumnRef)
    assert expr_op.count.name == "n"


def test_top_hitters_reads_of_and_by():
    """K02: ``TopHittersOperator``'s shape is three separate members --
    ``Expression`` (the count), ``OfExpression`` (the ``of C`` column being
    counted) and ``ByClause.Expression`` (the ``by C`` column).
    ``ValueExpression`` exists on no node in the assembly, and reading it
    raises ``AttributeError`` out of ``to_ir()`` for every ``top-hitters``
    query. Asserting all three distinct operands is what proves the branch
    reads the node it was handed rather than any one member twice.
    """
    op = parse("T | top-hitters 5 of a by b").to_ir().main_pipeline.operators[0]
    assert op.of.canonical_form == "a"
    assert op.by.canonical_form == "b"
    assert op.count == 5


def test_top_hitters_without_a_by_clause_builds():
    """``by`` is optional in the grammar: ``top-hitters 5 of a`` parses with
    ``ByClause`` as a plain ``None`` (verified on a real parse), so the
    branch must guard it rather than dereference ``.Expression``. ``of``
    still has to come through -- a null-guard that dropped both operands
    would pass a weaker test.
    """
    op = parse("T | top-hitters 5 of a").to_ir().main_pipeline.operators[0]
    assert op.by is None
    assert op.of.canonical_form == "a"
    assert op.count == 5


def test_partitionby_builds():
    """K02, same class of trap: ``PartitionByOperator``'s partition key is
    ``Entity``. There is no ``Expression`` member, so reading one raises
    ``AttributeError`` out of every ``__partitionby`` query. The subquery
    pipeline comes from ``Subquery`` -- assert both key and body, so
    reaching the key by breaking the body does not pass.
    """
    op = parse("T | __partitionby a (take 1)").to_ir().main_pipeline.operators[0]
    assert op.by.canonical_form == "a"
    assert [o.kind for o in op.right.operators] == ["take"]


def test_top_hitters_of_is_required_in_the_wire_format():
    """``of`` is declared without a default, so a payload that omits it must
    fail validation rather than round-tripping an operator missing its
    mandatory operand. ``extra="forbid"`` does not cover this -- it rejects
    unknown keys, not absent ones -- so the only thing standing between a
    truncated payload and a silently half-built ``top-hitters`` is the
    field's requiredness. Assert it directly, and assert the complete
    payload still validates so this cannot pass by rejecting everything.
    """
    from pydantic import ValidationError

    complete = {
        "kind": "top_hitters",
        "span": {"text_start": 0, "width": 1},
        "count": 5,
        "of": {"kind": "column_ref", "span": {"text_start": 0, "width": 1}, "name": "a"},
    }
    op = TopHittersOp.model_validate(complete)
    assert op.of.name == "a" and op.by is None

    with pytest.raises(ValidationError) as excinfo:
        TopHittersOp.model_validate({k: v for k, v in complete.items() if k != "of"})
    # Pin the reason, not just that something failed: a loose `match=` would
    # also be satisfied by an unrelated error that happened to mention "of".
    assert [(e["loc"], e["type"]) for e in excinfo.value.errors()] == [(("of",), "missing")]


# --- volatile source text must not reach semantic_hash (K06) ---------------


def test_a_comment_before_a_let_function_does_not_change_the_hash(ir_builder):
    """``LetFunction.body_span`` is a character offset into the source, so a
    comment anywhere ahead of the body shifts it. The volatile-field strip
    must drop it along with the keys named ``span`` -- otherwise the same
    declaration hashes two ways depending on what precedes it in the
    file.
    """
    plain = ir_builder.build("let f = (x:int){x+1}; T | extend y = f(a)")
    commented = ir_builder.build("// c\nlet f = (x:int){x+1}; T | extend y = f(a)")

    # Pin that the offsets genuinely differ, so the equality below is a claim
    # about the hash rather than about two identical IRs.
    assert plain.let_bindings[0].rhs_function.body_span.text_start == 15
    assert commented.let_bindings[0].rhs_function.body_span.text_start == 20

    assert plain.semantic_hash == commented.semantic_hash


def test_reformatting_a_raw_text_operator_does_not_change_the_hash(ir_builder):
    """The handful of operators the IR keeps as source text (``scan``,
    ``top-nested``, the ``graph-*`` family) must not record the node's
    *leading trivia* -- every space, newline and comment between the
    previous token and this one, which a bare ``node.ToString()`` includes.
    Recording it hashes two spellings of one operator differently.
    """
    plain = ir_builder.build("T | top-nested 3 of a by max(b)")
    spaced = ir_builder.build("T\n|   top-nested 3 of a by max(b)")
    commented = ir_builder.build("T | top-nested 3 // c\n of a by max(b)")

    # The recorded text is the operator itself -- no leading blank, no comment.
    assert plain.main_pipeline.operators[0].raw_text == "top-nested 3 of a by max(b)"

    assert plain.semantic_hash == spaced.semantic_hash
    assert plain.semantic_hash == commented.semantic_hash


def test_a_url_inside_raw_text_still_separates_two_scan_operators(ir_builder):
    """Guard on the whitespace normalization applied to ``raw_text`` before
    hashing: ``//`` is a comment introducer *and* the middle of every URL a
    detection rule ever matches on. Comments are already gone by this point
    (the builder records ``IncludeTrivia.Minimal``), so the normalizer must
    not go looking for them again -- stripping from ``//`` to end-of-line
    would truncate both operators to ``Url == "http:`` and collide them.
    """
    a = ir_builder.build(
        "T | scan declare (x:string='') with (step s: Url == \"http://a\" => x = \"y\")"
    )
    b = ir_builder.build(
        "T | scan declare (x:string='') with (step s: Url == \"http://b\" => x = \"y\")"
    )

    assert 'Url == "http://a"' in a.main_pipeline.operators[0].raw_text
    assert a.semantic_hash != b.semantic_hash


def test_interior_spacing_in_a_raw_text_string_literal_is_not_collapsed(ir_builder):
    """The same trap as the URL guard above, one step narrower.

    Collapsing *every* whitespace run flattens the run that sits **inside a
    string literal**, where it is data rather than formatting -- so a rule
    matching ``"error  occurred"`` and one matching ``"error occurred"``
    become the same query. Only a newline-and-its-surrounding-indent may be
    collapsed: KQL string literals cannot contain a raw newline, so that rule
    can never reach inside one. Interior spacing outside a literal needs no
    handling at all, because ``IncludeTrivia.Minimal`` has already normalized
    it (``top-nested 3  of  a`` is recorded as ``top-nested 3 of a``).
    """
    a = ir_builder.build(
        "T | scan declare (x:string='') with (step s: Msg == \"error  occurred\" => x = \"y\")"
    )
    b = ir_builder.build(
        "T | scan declare (x:string='') with (step s: Msg == \"error occurred\" => x = \"y\")"
    )

    assert 'Msg == "error  occurred"' in a.main_pipeline.operators[0].raw_text
    assert 'Msg == "error occurred"' in b.main_pipeline.operators[0].raw_text

    assert a.semantic_hash != b.semantic_hash


def test_double_negation_collapses_at_the_root_of_a_bare_expr(ir_builder):
    """``normalize_expressions`` collapses ``not(not(X))`` by *returning* the
    replacement, and only a parent field assignment installs it. At the root
    of the tree there is no parent, so ``compute_semantic_hash`` must adopt
    the returned root itself -- discarding it hashes the un-collapsed shape,
    making the collapse work for a whole ``QueryIR`` and not for the
    predicate on its own.
    """
    from kustology.ir import Not, compute_semantic_hash

    negated = ir_builder.build("T | where not(not(A > 1))")
    plain = ir_builder.build("T | where A > 1")
    pred_not_not = negated.main_pipeline.operators[0].predicate
    pred_plain = plain.main_pipeline.operators[0].predicate

    # The double negation really is still in the IR -- the builder is faithful.
    assert isinstance(pred_not_not, Not)
    assert isinstance(pred_not_not.operand, Not)

    assert compute_semantic_hash(pred_not_not) == compute_semantic_hash(pred_plain)


# --- semantic_hash is canonical over commutative shapes (K34) ---------------


def test_and_operand_order_does_not_change_the_hash(ir_builder):
    """``and`` is commutative, so ``a == 1 and b == 2`` and its mirror are the
    same predicate. Hashing the operand list in source order hands a rule
    author who reorders a conjunction a new digest for a query that has not
    changed meaning.
    """
    a = ir_builder.build("T | where a == 1 and b == 2")
    b = ir_builder.build("T | where b == 2 and a == 1")

    # The IR itself stays in source order -- the sort lives on the hash's
    # private copy, not in the builder or in ``normalize_expressions``.
    assert [o.left.name for o in a.main_pipeline.operators[0].predicate.operands] == ["a", "b"]
    assert [o.left.name for o in b.main_pipeline.operators[0].predicate.operands] == ["b", "a"]

    assert a.semantic_hash == b.semantic_hash


def test_nested_or_inside_an_and_sorts_bottom_up(ir_builder):
    """The sort key is the child's own dumped JSON, so a parent can only be
    ordered correctly once its children are.

    This pair is chosen to discriminate: both mean
    ``(a or b) and (a or z)``, and the sibling ``(a or z)`` sorts *between*
    the two spellings of the other operand -- ``(a or b)`` before it,
    ``(b or a)`` after. A top-down sort therefore keys the ``And`` on an
    operand it has not canonicalized yet, puts the two ``And``s in opposite
    orders, and leaves them different once the ``Or``s are fixed. Ordering the
    walk so every descendant is sorted before its ancestor is what makes them
    agree; a pair whose operands do not straddle like this passes either way
    and would prove nothing.
    """
    a = ir_builder.build("T | where (b == 1 or a == 1) and (a == 1 or z == 1)")
    b = ir_builder.build("T | where (a == 1 or z == 1) and (a == 1 or b == 1)")

    assert [type(o).__name__ for o in a.main_pipeline.operators[0].predicate.operands] == [
        "Or", "Or",
    ]
    assert [
        o.left.name for o in a.main_pipeline.operators[0].predicate.operands[0].operands
    ] == ["b", "a"]

    assert a.semantic_hash == b.semantic_hash


def test_in_list_value_order_does_not_change_the_hash(ir_builder):
    """``x in ("a", "b")`` is a set test; the order the set was written in
    carries no meaning.
    """
    a = ir_builder.build('T | where x in ("a", "b")')
    b = ir_builder.build('T | where x in ("b", "a")')

    assert [v.value for v in a.main_pipeline.operators[0].predicate.values] == ["a", "b"]
    assert [v.value for v in b.main_pipeline.operators[0].predicate.values] == ["b", "a"]

    assert a.semantic_hash == b.semantic_hash


def test_a_non_commutative_operator_is_not_sorted(ir_builder):
    """The boundary on the sort: only ``and`` / ``or`` operands and ``in``
    values commute. ``a < b`` and ``b < a`` are opposite predicates, and a
    sort applied to ``BinOp`` operands would have merged them.
    """
    a = ir_builder.build("T | where a < b")
    b = ir_builder.build("T | where b < a")

    assert a.semantic_hash != b.semantic_hash


def test_normalize_expressions_does_not_reorder_operands(ir_builder):
    """The public transform stays faithful to the query as written. It is
    documented as semantic-preserving *rewrites*, and callers use it on their
    own IR alongside spans that still have to line up with the source; the
    canonical ordering belongs to the hash's private copy.
    """
    from kustology.ir import normalize_expressions

    ir = ir_builder.build("T | where b == 2 and a == 1")
    normalize_expressions(ir)

    assert [o.left.name for o in ir.main_pipeline.operators[0].predicate.operands] == ["b", "a"]


def test_renaming_a_tabular_let_binding_does_not_change_the_hash(ir_builder):
    """A ``let`` name is a local label. Two rules that differ only in whether
    the analyst called the intermediate result ``X`` or ``Y`` are the same
    query, and a dedup keyed on the hash has to see that.
    """
    a = ir_builder.build("let X = T | where a == 1; X | take 1")
    b = ir_builder.build("let Y = T | where a == 1; Y | take 1")

    # The names really are different and really are in the IR, at both the
    # declaration and the use site.
    assert a.let_bindings[0].name == "X"
    assert a.main_pipeline.source.name == "X"
    assert b.let_bindings[0].name == "Y"
    assert b.main_pipeline.source.name == "Y"

    assert a.semantic_hash == b.semantic_hash


def test_let_canonicalization_is_positional_not_a_blanket_erasure(ir_builder):
    """Names are replaced by their *declaration index*, so which binding a
    reference points at is preserved. Erasing the names outright -- or
    mapping them all to one token -- would collapse these two queries, which
    select different rows.
    """
    first = ir_builder.build(
        "let X = T | where a == 1; let Y = T | where b == 2; X | take 1"
    )
    second = ir_builder.build(
        "let X = T | where a == 1; let Y = T | where b == 2; Y | take 1"
    )

    assert first.main_pipeline.source.name == "X"
    assert second.main_pipeline.source.name == "Y"

    assert first.semantic_hash != second.semantic_hash


def test_a_shadowed_let_name_still_canonicalizes_by_declaration_index(ir_builder):
    """``$letN`` has to mean "the Nth declaration" unconditionally, and a
    name-keyed rename map cannot deliver that: two bindings sharing a name
    share a key, and the later one wins for both.

    Reaching this through the parser needs a redeclaration, which Kusto
    *does* diagnose (``KS201``) -- so this is not a shape a clean query
    produces. It still matters, for two reasons. The IR is deliberately
    error-tolerant: it builds the query anyway and ``semantic_hash`` hashes
    it anyway, so "the parser complained" is not a guard. And
    ``compute_semantic_hash`` is public and accepts hand-built IR, where no
    parser is in the loop to enforce unique names at all.

    The two queries below are the same query modulo renaming -- bind
    ``T | where a == 1``, filter that by ``b == 2``, take 1 -- written once
    with distinct names and once with the second binding shadowing the first.
    Under a name-keyed map both declarations became ``$let1``, so the second
    binding's body appeared to read from *itself* rather than from the first,
    and the two spellings hashed apart. Renaming declarations by position and
    resolving each reference against the bindings visible where it is written
    fixes both halves.
    """
    distinct = ir_builder.build("let x = T | where a == 1; let y = x | where b == 2; y | take 1")
    shadowed = ir_builder.build("let x = T | where a == 1; let x = x | where b == 2; x | take 1")

    # The premise, pinned rather than assumed: the redeclaration is what
    # Kusto objects to (and the only extra thing it objects to), and the
    # builder emits two bindings sharing one name plus LetRefs into them
    # regardless.
    extra = [d.code for d in shadowed.diagnostics if d.code not in
             [c.code for c in distinct.diagnostics]]
    assert extra == ["KS201"]
    assert [b.name for b in shadowed.let_bindings] == ["x", "x"]
    assert shadowed.let_bindings[1].rhs_pipeline.source.name == "x"
    assert shadowed.main_pipeline.source.name == "x"

    assert distinct.semantic_hash == shadowed.semantic_hash


def test_renaming_a_scalar_let_binding_does_not_change_the_hash(ir_builder):
    """A scalar binding's name is as local a label as a tabular one's.

    ``n`` in ``where a > n`` builds a ``LetValueRef``, which is renamed with
    the declaration. A ``ColumnRef`` at the use site could not be:
    ``_canonicalize_let_names`` cannot rename a ``ColumnRef``, since a real
    column of that name is a different query, and the two spellings would
    hash apart. See ``test_let_value_ref.py`` for the node itself and for
    the near-miss that must still hash apart.
    """
    a = ir_builder.build("let n = 5; T | where a > n")
    b = ir_builder.build("let m = 5; T | where a > m")

    assert a.semantic_hash == b.semantic_hash


def _time_flags(query: str) -> dict[str, bool]:
    """Return ``{func name: is_time_func}`` for every FuncCall in an unbound build."""
    from kustology.ir import FuncCall, find_all

    ir = parse(query).to_ir(attach_schema=False)
    return {fc.name: fc.is_time_func for fc in find_all(ir, FuncCall)}


def test_is_time_func_marks_bin_and_bin_at():
    """``bin`` is how a real query buckets time, and the field must say so.

    The temporal return type shows up on only some of ``bin`` /
    ``bin_at``'s overloads, so a return-type scan that reads fewer than all
    of them reports both as plain scalars -- leaving this published field
    ``False`` on the single most common temporal construct in KQL.
    """
    assert _time_flags("T | summarize count() by bin(TimeGenerated, 1h)") == {
        "count": False,
        "bin": True,
    }
    assert _time_flags(
        "T | summarize count() by bin_at(TimeGenerated, 1d, datetime(2024-01-01))"
    ) == {"count": False, "bin_at": True}


def test_is_time_func_is_false_for_arithmetic_abs():
    """``abs`` is in ``time_functions()`` and must not be flagged here.

    Its overload list is ``['long', None, 'timespan']``, so the "any overload
    declares datetime/timespan" rule that flags ``bin`` catches ``abs`` as
    well. That rule is right for a return-type question and wrong for "is
    this call about time": ``abs(x)`` on a number is not, in any usage.
    """
    from kustology.reflection import time_functions

    assert "abs" in time_functions()
    assert _time_flags("T | extend a = abs(x)") == {"abs": False}


def test_is_time_func_keeps_floor():
    """``floor(TimeGenerated, 1h)`` is a genuine bucketing idiom.

    Unlike ``abs`` it is hand-listed as temporally relevant, so it stays
    flagged — and the flag is a property of the *name*, so numeric
    ``floor(x, 1)`` is over-reported. Deliberate, and documented on
    ``find_time_expressions``.
    """
    assert _time_flags("T | extend a = floor(TimeGenerated, 1h)") == {"floor": True}
    assert _time_flags("T | extend a = floor(x, 1)") == {"floor": True}


def test_is_time_func_agrees_with_find_time_expressions():
    """The two consumers of the temporal-function set must not drift apart."""
    from kustology.ir.builder import _is_time_func_name
    from kustology.utils.analysis import _TIME_FUNCS

    for name in ("bin", "bin_at", "floor", "ago", "now"):
        assert _is_time_func_name(name) is True
        assert name in _TIME_FUNCS
    assert _is_time_func_name("abs") is False
    assert "abs" not in _TIME_FUNCS


# -- schemaless analysis against default globals (K27, Task 5.1) ----------

_SCHEMALESS_Q = "T | where a > ago(1h) and b == 1.5"


def _typed(ir):
    """Return ``{probe label: result_type}`` for the K27 probes."""
    from kustology.ir import FuncCall, find_all

    out: dict[str, KustoType] = {}
    for lit in find_all(ir, LiteralExpr):
        out[f"literal:{lit.literal_kind}"] = lit.result_type
    for call in find_all(ir, FuncCall):
        out[f"call:{call.name}"] = call.result_type
    return out


def test_schemaless_to_ir_types_literals_and_builtins():
    """``parse(q).to_ir()`` with no schema must still carry real types.

    Microsoft's binder resolves ``1h``, ``1.5`` and ``ago()`` against
    ``GlobalState.Default`` without needing a single table, so leaving them
    ``UNRESOLVED`` would be our omission rather than a limit of the parser.
    """
    ir = parse(_SCHEMALESS_Q).to_ir()
    types = _typed(ir)
    assert types["literal:timespan"] == KustoType.TIMESPAN
    assert types["literal:real"] == KustoType.REAL
    assert types["call:ago"] == KustoType.DATETIME


def test_schemaless_to_ir_reports_no_unknown_table_diagnostic():
    """Analyzing against default globals must not invent a KS204 for ``T``.

    The user asked for no schema; reporting every table as missing would be
    an artifact of how we got the types, not something they wrote.
    """
    ir = parse(_SCHEMALESS_Q).to_ir()
    assert [(d.code, d.message) for d in ir.diagnostics] == []


def test_ir_builder_build_agrees_with_the_schemaless_to_ir_path():
    """The two schemaless entry points must annotate and diagnose alike.

    ``IRBuilder().build`` binds against default globals and so gets the
    types for free; what that bind also yields, unfiltered, is a false
    KS204 for every table -- the half of the agreement that lands here.
    """
    ir = IRBuilder().build(_SCHEMALESS_Q)
    types = _typed(ir)
    assert types["literal:timespan"] == KustoType.TIMESPAN
    assert types["literal:real"] == KustoType.REAL
    assert types["call:ago"] == KustoType.DATETIME
    assert [(d.code, d.message) for d in ir.diagnostics] == []


def test_a_bound_parse_still_reports_its_unknown_tables():
    """Suppression is scoped to the schemaless path, not to the IR at large.

    A caller who supplied a schema and named a table it does not describe
    has a real error, and it must survive into ``ir.diagnostics``.
    """
    ir = parse(
        "Known | join Missing on x", schema={"Known": {"x": "string"}},
    ).to_ir()
    assert any(d.code == "KS204" for d in ir.diagnostics)


def test_the_three_schemaless_paths_hash_the_same():
    """``result_type`` is volatile, so acquiring it must not move the digest."""
    a = parse(_SCHEMALESS_Q).to_ir(attach_schema=False).semantic_hash
    b = parse(_SCHEMALESS_Q).to_ir().semantic_hash
    c = IRBuilder().build(_SCHEMALESS_Q).semantic_hash
    assert a == b == c


# -- Operator.result_schema from Microsoft's binder (K-ARCH-1, Task 5.2) --

_BINDER_SCHEMA = {"T": {"a": "long", "s": "string"}}


def test_operator_result_schema_is_captured_from_the_bound_parse():
    """Every operator carries the columns Microsoft says it emits.

    Read off ``<operator node>.ResultType``, so ``summarize`` reports the
    aggregate's real type (``count()`` is a ``long``) rather than whatever
    our own rule would have inferred from the expression.
    """
    ir = parse(
        "T | where a > 1 | summarize n = count() by s", schema=_BINDER_SCHEMA,
    ).to_ir()
    where_op, summarize_op = ir.main_pipeline.operators
    assert where_op.result_schema.columns == {"a": "long", "s": "string"}
    assert summarize_op.result_schema.columns == {"s": "string", "n": "long"}


def test_operator_result_schema_is_none_when_microsoft_declines():
    """An unknown table leaves Microsoft's symbol *open*, and open is a decline.

    An open ``TableSymbol`` still lists the columns the query happened to
    name, typed ``unknown``. Recording that would present a guess as the
    binder's answer, and would then override the real schema a caller hands
    to ``SchemaAttacher`` afterwards — which is exactly how
    ``IRBuilder().build(q)`` (bound against ``GlobalState.Default``, which
    knows no tables) reaches the attacher.
    """
    ir = IRBuilder().build("T | where a > 1 | summarize n = count() by s")
    assert [op.result_schema for op in ir.main_pipeline.operators] == [None, None]


def test_operator_result_schema_is_stripped_from_the_semantic_hash():
    """Supplying a schema must not move the digest.

    ``_VOLATILE_FIELDS`` is keyed by model *field name* rather than by
    owning class, so one ``result_schema`` entry covers ``Pipeline`` and
    ``Operator`` alike. That is worth an assertion rather than an
    assumption: without the strip every operator's schema enters the digest
    and the same query hashes two ways depending on whether the caller had
    a schema.
    """
    query = "T | where a > 1 | summarize n = count() by s"
    bound = parse(query, schema=_BINDER_SCHEMA).to_ir()
    unbound = parse(query).to_ir()

    # Not vacuous: one side really does carry the schemas the other lacks.
    assert all(op.result_schema is not None for op in bound.main_pipeline.operators)
    assert all(op.result_schema is None for op in unbound.main_pipeline.operators)

    assert bound.semantic_hash == unbound.semantic_hash


def test_result_schema_is_named_once_in_the_volatile_set():
    """The guard above depends on the set being keyed by field name."""
    from kustology.ir.transforms import _VOLATILE_FIELDS

    assert "result_schema" in _VOLATILE_FIELDS


def test_table_symbol_columns_declines_an_open_symbol():
    """The reader's guard, asserted against a real pair of parses.

    Same operator, same query text; the only difference is whether the
    binder was given the table. Closed answers, open declines.
    """
    from kustology.bridge import KustoCode
    from kustology.ir._builder_helpers import table_symbol_columns
    from kustology.utils.analysis import build_global_state, collect_nodes

    def _filter_result_type(schema):
        code = KustoCode.ParseAndAnalyze(
            "T | where a > 1", build_global_state(schema),
        )
        (node,) = collect_nodes(
            code.Syntax, lambda n: str(n.Kind) == "FilterOperator",
        )
        return node.ResultType

    assert table_symbol_columns(_filter_result_type(_BINDER_SCHEMA)) == {
        "a": "long", "s": "string",
    }
    assert table_symbol_columns(_filter_result_type({})) is None
    assert table_symbol_columns(None) is None


# -- the schemaless artifact family, not just KS204 (Task 5.1 follow-up) --

# One reproducer per code the fixture corpus produces on a schemaless build.
# Every one is the binder saying "the globals I was handed describe nothing",
# which is the same sentence KS204 says about a table.
_SCHEMALESS_ARTIFACTS = [
    ("KS204", "T | take 1", "unknown table"),
    ("KS205", "union isfuzzy=true Foo, Bar", "fuzzy union member"),
    ("KS207", 'cluster("x.kusto.windows.net").database("d").T | take 1',
     "unreachable cluster"),
    ("KS211", "_Im_WebSession(starttime=ago(1d)) | take 1", "unknown function"),
    ("KS142", "union Security* | take 1", "wildcard table pattern"),
]


@pytest.mark.parametrize(
    "code,query,label", _SCHEMALESS_ARTIFACTS, ids=[c[0] for c in _SCHEMALESS_ARTIFACTS],
)
def test_no_schemaless_path_reports_an_unknown_name_artifact(code, query, label):
    """Every family here is an artifact of default globals, not of the query.

    A filter naming only ``KS204`` leaves the rest through, and some carry
    ``Error`` severity — so a consumer gating on ``any(d.severity ==
    "Error" ...)`` flips for `union Security*` and for every ASIM-parser
    query, on a call where the caller asked for no schema at all.
    """
    assert [d.code for d in parse(query).to_ir().diagnostics] == []
    assert [d.code for d in IRBuilder().build(query).diagnostics] == []


def test_the_fixture_corpus_is_clean_on_both_schemaless_paths():
    """Both schemaless paths come back clean across the whole fixture corpus."""
    from pathlib import Path

    corpus = sorted(
        (Path(__file__).resolve().parent.parent / "fixtures" / "complex_queries")
        .glob("*.kql")
    )
    assert corpus, "corpus fixtures missing"
    found: list[tuple[str, str, str]] = []
    for path in corpus:
        text = path.read_text().strip()
        for label, ir in (
            ("to_ir", parse(text).to_ir()),
            ("build", IRBuilder().build(text)),
        ):
            found += [(path.stem, label, d.code) for d in ir.diagnostics]
    assert found == []


def test_a_bound_parse_still_reports_every_unknown_name():
    """Suppression is scoped to globals the caller never chose.

    A caller who supplied a schema owns the names in their query — an
    unknown function is as real an error there as an unknown table, and
    neither may be filtered.
    """
    schema = {"Known": {"x": "string"}}
    codes = {
        d.code
        for query in ("Known | join Missing on x", "_Im_WebSession() | take 1")
        for d in parse(query, schema=schema).to_ir().diagnostics
    }
    assert {"KS204", "KS211"} <= codes


def test_both_schemaless_docstrings_describe_the_family_they_actually_filter():
    """The entry-point docstrings must describe the family the code filters.

    A docstring promising a one-code (``KS204``-only) filter misleads: a
    reader taking it at its word would expect `union isfuzzy=true Foo, Bar`
    or an ASIM parser call to surface an ``Error`` diagnostic from a
    schemaless ``to_ir()``, and would build exactly the gate the filter
    exists to keep from flipping.

    The live set lives in ``services._UNKNOWN_NAME_CODES``;
    ``tests/test_reflection_audit.py`` re-derives it from
    ``Kusto.Language.DiagnosticFacts``, so a DLL refresh that adds a code
    fails there first. This test pins the behavior the docstrings
    promise, not their wording.
    """
    from kustology.services import _UNKNOWN_NAME_CODES

    # The behavior the corrected sentence describes: a query whose only
    # schemaless artifact is *not* KS204 also comes back clean.
    assert parse("_Im_WebSession(starttime=ago(1d)) | take 1").to_ir().diagnostics == []
    assert "KS211" in _UNKNOWN_NAME_CODES


def test_table_symbol_columns_declines_a_tuple_symbol():
    """The docstring promises a ``TableSymbol``; the guard must check for one.

    ``TupleSymbol`` — what ``arg_max(a, *)`` puts on the aggregate expression
    — carries ``Columns`` and has no ``IsOpen`` at all, so a duck-typed
    ``getattr(sym, "IsOpen", False)`` read it as a *closed table* and would
    have published its columns as an operator's result schema. Nothing routes
    one here today (only operator and pipeline nodes call in, and those carry
    ``TableSymbol``), which is exactly why the promise has to be enforced by
    the code rather than by the call sites that happen to exist.
    """
    from kustology.bridge import KustoCode
    from kustology.ir._builder_helpers import is_table_symbol, table_symbol_columns
    from kustology.utils.analysis import build_global_state, collect_nodes

    code = KustoCode.ParseAndAnalyze(
        "T | summarize arg_max(a, *)",
        build_global_state({"T": {"a": "long", "s": "string"}}),
    )
    tuples = [
        n.ResultType
        for n in collect_nodes(
            code.Syntax,
            lambda n: type(getattr(n, "ResultType", None)).__name__ == "TupleSymbol",
        )
    ]
    assert tuples, "no TupleSymbol in this parse — pick another query"
    sym = tuples[0]

    # It really does look like a closed table to a duck-typed guard.
    assert sym.Columns.Count > 0
    assert getattr(sym, "IsOpen", False) is False
    assert is_table_symbol(sym) is False

    assert table_symbol_columns(sym) is None


def test_the_schemaless_docstrings_do_not_claim_built_ins_fail_to_resolve():
    """Default globals are not empty, and the whole feature depends on that.

    A claim that default globals describe nothing — no tables, functions,
    clusters, databases, external tables, materialized views, entity groups
    or stored query results, so *every* name the query brings with it fails
    to resolve — is false: `GlobalState.Default` carries hundreds of
    built-in functions plus the aggregates and plug-ins, `ago` among them,
    which is precisely why a schemaless parse can promise `ago(1h)` a
    `datetime`. A docstring claiming otherwise contradicts that promise
    three lines away and leaves the reader no way to decide what a clean
    schemaless `to_ir()` actually means.

    What *is* empty is the default **database** — and the cluster list. That
    is the accurate scope, and it is the scope that explains the filter: the
    suppressed family is every "this name is not in the database I was
    handed", never "this built-in does not exist".
    """
    from kustology import KustoQuery
    from kustology.bridge import GlobalState

    # The facts the sentence has to reflect.
    globals_ = GlobalState.Default
    assert globals_.Functions.Count > 400, globals_.Functions.Count
    assert globals_.Aggregates.Count > 0
    assert globals_.Database.Tables.Count == 0
    assert globals_.Clusters.Count == 0

    # …and the behavior they produce: a built-in resolves, with no
    # diagnostic to suppress.
    ir = parse("print x = ago(1h)").to_ir()
    assert ir.diagnostics == []
    call = next(iter(find_all(ir, FuncCall)))
    assert call.name == "ago"
    assert call.result_type == KustoType.DATETIME

    for raw in (KustoQuery.to_ir.__doc__, IRBuilder.build.__doc__):
        doc = " ".join(raw.split())
        assert "built-in" in doc, doc
        assert "database" in doc.lower(), doc


# Aggregate auto-names from Microsoft's symbol properties ---------------------


def test_buildschema_takes_microsofts_name_even_beside_a_multi_output_aggregate():
    """`arg_max(t, *)` reports six columns bound and one unbound, so an
    operator-level alignment read has to give up on the whole summarize --
    dropping `buildschema(d)` to a hand rule's `buildschema_d` where the
    engine says `schema_d` (Aggregates.cs declares PrefixAndFirstArgument +
    prefix "schema"). Reading ResultNameKind off the resolved symbol is
    per-expression, so the alignment problem does not exist.
    """
    ir = parse("T | summarize arg_max(t, *), buildschema(d)").to_ir()
    names = [a.name for a in ir.main_pipeline.operators[0].aggregations]
    assert names == ["t", "schema_d"]


def test_auto_names_are_bind_invariant_under_the_symbol_read():
    """``binary_all_and`` is ``ResultNameKind.FirstArgument`` (probed against
    the DLL directly, and cross-checked against ``SummarizeOperator.
    ResultType.Columns``): the name is the first argument's own name, ``a``
    -- not ``binary_all_and_a``, the generic prefix-plus-argument spelling.
    Aggregates live in ``GlobalState.Default``, so the symbol
    (and therefore its ``ResultNameKind``) resolves identically whether or
    not the table has a schema.
    """
    q = "T | summarize buildschema(d), make_set(s), binary_all_and(a)"
    bound = parse(q, schema={"T": {"d": "dynamic", "s": "string", "a": "long"}}).to_ir()
    unbound = parse(q).to_ir()
    pick = lambda ir: [a.name for a in ir.main_pipeline.operators[0].aggregations]
    assert pick(bound) == pick(unbound) == ["schema_d", "set_s", "a"]


def test_aggregate_auto_names_match_microsofts_for_the_whole_library():
    """For every aggregate in Microsoft's own library that our probe can call
    with a single *bare-column* argument, the Assignment.name we derive must
    equal the column name Microsoft reports for `T | summarize fn(col)`. This
    pins the ResultNameKind port against the DLL, upgrade after upgrade --
    but the bare-column probe cannot see a divergence that only shows up for
    a *non*-bare first argument (an expression, not a `NameReference`); see
    `test_auto_names_hold_a_prefix_even_without_a_bare_column_argument` for
    that battery.
    """
    from Kusto.Language import Aggregates

    from kustology.bridge import KustoCode
    from kustology.utils.analysis import build_global_state

    schema = {"T": {
        "s": "string", "n": "long", "r": "real", "b": "bool",
        "t": "datetime", "ts": "timespan", "d": "dynamic", "g": "guid",
    }}
    by_type = {"string": "s", "long": "n", "int": "n", "real": "r",
               "decimal": "r", "bool": "b", "datetime": "t",
               "timespan": "ts", "dynamic": "d", "guid": "g"}
    state = build_global_state(schema)
    mismatches, probed = [], 0
    for sym in Aggregates.All:
        name = str(sym.Name)
        probe = None
        for arg in by_type.values():
            q = f"T | summarize {name}({arg})"
            code = KustoCode.ParseAndAnalyze(q, state)
            result_type = getattr(code, "ResultType", None)
            columns = getattr(result_type, "Columns", None)
            if columns is None or columns.Count != 1 or any(
                str(d.Severity) == "Error" for d in code.GetDiagnostics()
            ):
                continue
            probe = (q, str(columns[0].Name))
            break
        if probe is None:
            continue  # needs literals/multiple args; the MATRIX covers the famous ones
        probed += 1
        q, expected = probe
        ir = parse(q, schema=schema).to_ir()
        (agg,) = ir.main_pipeline.operators[0].aggregations
        if agg.name != expected:
            mismatches.append((name, agg.name, expected))
    assert mismatches == []
    # The loop must not silently degrade into probing nothing. Measured
    # against the vendored DLL: 35 of ``Aggregates.All`` are callable with a
    # single column argument (the ``*if``/``covariance*``/percentile-family
    # ones need a second literal or predicate column and are skipped) --
    # this is a coverage floor a few below that, not the measured count.
    assert probed >= 30, f"only {probed} aggregates were probe-able"


def test_grouping_key_names_match_microsofts_for_the_whole_library():
    """For every *scalar* function whose ``ResultNameKind`` is not ``None``,
    the grouping-key name we derive for ``T | summarize count() by fn(col)``
    must equal the column name Microsoft reports for that same query. This
    pins the grouping-mode symbol read (``_auto_name(mode="grouping")``)
    against the DLL, the same way
    `test_aggregate_auto_names_match_microsofts_for_the_whole_library` pins
    the aggregation-mode read -- but iterates ``Functions.All`` rather than
    ``Aggregates.All``, since a grouping key is usually a scalar call.

    A function whose ``ResultNameKind`` *is* ``None`` is out of scope here
    because those names are deliberately ours: we use first-bare-column naming
    instead of Microsoft's ``ColumnN`` counter (see ``_auto_name``'s grouping
    bullet and CHANGELOG's Known limitations for the design rationale). This
    parity probe checks only Microsoft-compatible names.
    """
    from Kusto.Language import Functions

    from kustology.bridge import KustoCode
    from kustology.utils.analysis import build_global_state

    schema = {"T": {
        "s": "string", "n": "long", "r": "real", "b": "bool",
        "t": "datetime", "ts": "timespan", "d": "dynamic", "g": "guid",
    }}
    by_type = {"string": "s", "long": "n", "int": "n", "real": "r",
               "decimal": "r", "bool": "b", "datetime": "t",
               "timespan": "ts", "dynamic": "d", "guid": "g"}
    state = build_global_state(schema)
    mismatches, probed, candidates = [], 0, 0
    for sym in Functions.All:
        kind = getattr(sym, "ResultNameKind", None)
        if kind is None or str(kind) == "None":
            continue
        candidates += 1
        name = str(sym.Name)
        probe = None
        for arg in by_type.values():
            q = f"T | summarize count() by {name}({arg})"
            code = KustoCode.ParseAndAnalyze(q, state)
            if any(str(d.Severity) == "Error" for d in code.GetDiagnostics()):
                continue
            result_type = getattr(code, "ResultType", None)
            columns = getattr(result_type, "Columns", None)
            if columns is None or columns.Count < 1:
                continue
            probe = (q, str(columns[0].Name))
            break
        if probe is None:
            continue  # needs literals/multiple args; not this probe's job
        probed += 1
        q, expected = probe
        ir = parse(q, schema=schema).to_ir()
        (key,) = ir.main_pipeline.operators[0].by
        got = getattr(key, "name", None)
        if got != expected:
            mismatches.append((name, got, expected, q))
    assert mismatches == []
    # Measured against the vendored DLL: 82 of Functions.All declare a
    # ResultNameKind other than None, 45 of those are callable with a single
    # bare-column argument in a grouping position. This floor is a few below
    # that, not the measured count.
    assert candidates >= 70, f"only {candidates} candidate functions found"
    assert probed >= 40, f"only {probed} functions were probe-able"


def test_auto_names_hold_a_prefix_even_without_a_bare_column_argument():
    """C# string-concatenates a null argument name (``prefix + "_" + name``,
    Binder_Projection.cs:634-641/652-663), so ``PrefixAndFirstArgument``/
    ``PrefixAndOnlyArgument`` still produce ``f"{prefix}_"`` when the first
    argument isn't a bare column reference -- Microsoft-confirmed via direct
    ``ResultType.Columns`` probes:

    - ``make_list(x + y)`` -> ``list_`` (not the generic fallback's
      ``make_list_``)
    - ``make_set(x + y)`` -> ``set_``
    - ``buildschema(pack('a', x))`` -> ``schema_``

    Each is bind-stable: aggregates resolve their symbol (and so their
    ``ResultNameKind``/``ResultNamePrefix``) the same way whether or not the
    table has a schema.

    ``by treepath(D[0])`` is a *known, accepted* residual divergence:
    Microsoft's ``GetExpressionResultName`` derives ``D_0`` from the
    ``ElementExpression`` (``PathExpression``/``ElementExpression`` cases,
    Binder_Projection.cs:706-), giving ``tree_D_0`` -- this port only reads a
    bare ``NameReference`` for the argument name, so it produces ``tree_``
    instead. Pinned here rather than silently drifting; widening the
    argument-name walk to cover element/path expressions is out of scope for
    this port.
    """
    schema = {"T": {"x": "long", "y": "long", "D": "dynamic"}}
    cases = [
        ("T | summarize make_list(x + y)", "list_"),
        ("T | summarize make_set(x + y)", "set_"),
        ("T | summarize buildschema(pack('a', x))", "schema_"),
    ]
    for q, expected in cases:
        bound = parse(q, schema=schema).to_ir()
        unbound = parse(q).to_ir()
        pick = lambda ir: [a.name for a in ir.main_pipeline.operators[0].aggregations]
        assert pick(bound) == pick(unbound) == [expected], q

    q = "T | summarize count() by treepath(D[0])"
    bound = parse(q, schema=schema).to_ir()
    unbound = parse(q).to_ir()
    pick_by = lambda ir: [b.name for b in ir.main_pipeline.operators[0].by]
    # Bind-stable, and pinned at today's (known-divergent) answer: "tree_",
    # not Microsoft's "tree_D_0" -- see the docstring above.
    assert pick_by(bound) == pick_by(unbound) == ["tree_"]
