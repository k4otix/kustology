from kustology import parse
from kustology.ir import JoinOp, LetFunction, Pipeline, Span, find_all, span_of


def _ir(q):
    return parse(q).to_ir(semantic_hash=False)


def test_pipeline_envelope_excludes_the_let_statement():
    q = "let n = 5;\nT | where a > n | take 1"
    assert span_of(_ir(q).main_pipeline).text(q) == "T | where a > n | take 1"


def test_query_envelope_covers_the_whole_text():
    q = "let n = 5;\nT | where a > n | take 1"
    assert span_of(_ir(q)).text(q) == q


def test_join_subquery_pipeline_text():
    q = "T | join (S | where b == 1) on x"
    inner = next(find_all(next(find_all(_ir(q), JoinOp)), Pipeline))
    assert span_of(inner).text(q) == "S | where b == 1"


def test_node_with_its_own_span_returns_that_span():
    ir = _ir("T | where a > 1")
    op = ir.main_pipeline.operators[0]
    assert span_of(op) == op.span


def test_let_function_envelope_includes_the_body():
    q = "let f = (x: long) { T | where a > x };\nf()"
    fn = next(find_all(_ir(q), LetFunction))
    assert "T | where a > x" in span_of(fn).text(q)


def test_no_span_gives_none():
    assert span_of(Span(text_start=0, width=0)) is None
