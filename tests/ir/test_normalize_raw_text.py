# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Unit tests for the token rule applied to ``raw_text`` before hashing."""

from kustology.ir.transforms import _normalize_raw_text


def test_the_normalized_form_is_token_texts_joined_by_single_spaces():
    """Pin the exact output for one non-empty input.

    The comparisons in the other tests here survive any uniform change to the
    output. A trailing space from the end-of-text token, or a change to the
    join, moves every digest that reads source text, and this assertion is
    what catches it.
    """
    assert _normalize_raw_text("with (step s: a == 1)") == "with ( step s : a == 1 )"


def test_a_reflowed_step_clause_matches_its_single_line_spelling():
    """Pin that the rule reaches the spacing between two tokens.

    A ``scan`` clause written across lines arrives with a newline and an
    indent between two tokens, and the same clause on one line arrives with
    neither. Narrow the rule to folding whitespace and ``with (step`` stays a
    different string from ``with ( step``, which gives one operator two
    digests.
    """
    one_line = _normalize_raw_text("with (step s: a == 1 => x = 1)")
    reflowed = _normalize_raw_text("with (\n  step s: a == 1 => x = 1\n)")

    assert one_line == reflowed


def test_tightened_operator_spacing_matches_the_spaced_spelling():
    """Pin that a binary operator written tight matches one written spaced.

    ``IncludeTrivia.Minimal`` keeps whatever the author wrote between two
    tokens, so ``a==b`` and ``a == b`` reach this function as two strings.
    They are one predicate, and skipping the re-lex gives them two digests.
    """
    assert _normalize_raw_text("a==b") == _normalize_raw_text("a == b")


def test_spacing_inside_a_string_literal_survives():
    """Pin that the interior of a string literal is data.

    A literal lexes as one token, so the run of spaces inside it reaches the
    output intact. Widen the rule to collapsing every whitespace run and a
    detection matching ``"error  occurred"`` merges with one matching
    ``"error occurred"``.
    """
    two_spaces = _normalize_raw_text('Msg == "error  occurred"')
    one_space = _normalize_raw_text('Msg == "error occurred"')

    assert two_spaces != one_space


def test_a_url_inside_a_string_literal_keeps_its_double_slash():
    """Pin that ``//`` inside a literal survives.

    ``//`` opens a comment and is also the middle of every URL a detection
    rule matches on. The lexer reads the whole literal as one token, so a
    comment strip that ran from ``//`` to end of line would truncate
    ``Url == "http://a"`` and ``Url == "http://b"`` to one string.
    """
    assert '"http://a"' in _normalize_raw_text('Url == "http://a"')


def test_a_multi_line_string_literal_keeps_its_newline():
    """Pin that a newline inside a literal is data.

    A backtick-quoted literal spans lines and lexes as one token, so its
    newline is part of the value. Fold line breaks ahead of the lex and two
    literals differing only there become one.
    """
    assert "\n" in _normalize_raw_text("x = ```line1\nline2```")


def test_empty_text_stays_empty():
    """Pin that an empty ``raw_text`` normalizes to an empty string.

    Lexing ``""`` yields one ``EndOfTextToken`` whose text is empty, so the
    join runs over nothing. The builder writes ``raw_text=""`` on an
    ``UnknownExpr`` for an empty node, which reaches this function.
    """
    assert _normalize_raw_text("") == ""
