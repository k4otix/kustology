import kustology


def _spans(q, method, **kw):
    return [s.text(q) for s in getattr(kustology.parse(q), method)(**kw)]


def test_two_comments_in_one_trivia_run():
    assert _spans("let x=5;  // a\n  // b\nT | where A==1", "comment_spans") == ["// a", "// b"]


def test_trailing_comment_on_end_of_text():
    assert _spans("T | where A==1 // trailing", "comment_spans") == ["// trailing"]


def test_slashes_inside_a_string_are_not_a_comment():
    assert _spans('T | where A == "http://x.com"', "comment_spans") == []


def test_crlf_comment_excludes_the_carriage_return():
    assert _spans("T // c\r\n| take 1", "comment_spans") == ["// c"]


def test_comment_offsets_are_code_points():
    q = "T | where A == '😀' // c"
    (span,) = kustology.parse(q).comment_spans()
    assert span.text(q) == "// c"


def test_string_literal_prefixes():
    q = 'T | where A == "a" and B == @"v//w" and C == h"o" and D == ```m\nl```'
    assert _spans(q, "string_literal_spans") == ['"a"', '@"v//w"', 'h"o"', "```m\nl```"]
    assert _spans(q, "string_literal_spans", include_prefix=False) == ['"a"', '"v//w"', '"o"', "```m\nl```"]


def test_statement_spans_exclude_separators_and_ignore_semicolons_in_strings():
    q = 'let s = "a;b";\nT | where x == s'
    assert _spans(q, "statement_spans") == ['let s = "a;b"', "T | where x == s"]


def test_tokens_expose_kind_text_and_trivia():
    q = "T | take 1"
    toks = kustology.parse(q).tokens()
    assert [t.kind for t in toks[:2]] == ["IdentifierToken", "BarToken"]
    assert toks[1].trivia == " " and toks[1].span.text(q) == "|"
    assert toks[-1].kind == "EndOfTextToken"
