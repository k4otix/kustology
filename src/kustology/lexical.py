"""Lexical helpers over Microsoft's token stream (Tier 1, pydantic-free).

Each helper reports positions the lexer already decided — comments, string
literals, statements — as code-point :class:`TextSpan`s. None reinterprets
the tree: "the main pipeline without its joins" is a Tier 2 question, see
``kustology.ir.walk(prune=...)`` and ``span_of``.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from ._text import Utf16Offsets
from .spans import TextSpan
from .utils.walker import iter_elements

_STRING_PREFIX = re.compile(r"[hH]?@?")


class Token(NamedTuple):
    kind: str            # SyntaxKind name, for example "StringLiteralToken"
    text: str
    span: TextSpan       # the token's own text
    trivia: str          # whitespace and comments preceding the token
    trivia_span: TextSpan


def tokens(kusto_code: Any) -> list[Token]:
    """Every token, including the final ``EndOfTextToken`` that owns trailing trivia."""
    offsets = Utf16Offsets(str(kusto_code.Text))
    return [
        Token(
            kind=str(tok.Kind),
            text=str(tok.Text),
            span=TextSpan(*offsets.span_to_codepoints(tok.TextStart, tok.Width)),
            trivia=str(tok.Trivia),
            trivia_span=TextSpan(*offsets.span_to_codepoints(tok.TriviaStart, tok.TriviaWidth)),
        )
        # ``True`` is ``includeZeroWidthTokens``: the final ``EndOfTextToken``
        # has zero width, so the default-arg call silently drops it.
        for tok in kusto_code.Syntax.GetTokens(True)
    ]


def comment_spans(kusto_code: Any) -> list[TextSpan]:
    """Every ``//`` comment in source order.

    KQL has no block comments, so ``//`` to end of line inside trivia is the
    complete rule. A trivia run can hold several comments; the trailing one
    belongs to ``EndOfTextToken``. The line terminator is not included.
    """
    out: list[TextSpan] = []
    for tok in tokens(kusto_code):
        trivia, base = tok.trivia, tok.trivia_span.start
        i = 0
        while (j := trivia.find("//", i)) >= 0:
            k = trivia.find("\n", j)
            end = len(trivia) if k < 0 else k
            if end > j and trivia[end - 1] == "\r":
                end -= 1
            out.append(TextSpan(base + j, end - j))
            i = max(end, j + 2)
    return out


def string_literal_spans(kusto_code: Any, *, include_prefix: bool = True) -> list[TextSpan]:
    """Every string literal. Microsoft's token includes the ``@`` and ``h``
    prefixes; ``include_prefix=False`` starts at the opening quote or backtick."""
    out: list[TextSpan] = []
    for tok in tokens(kusto_code):
        if tok.kind != "StringLiteralToken":
            continue
        start, length = tok.span
        if not include_prefix:
            m = _STRING_PREFIX.match(tok.text)
            skip = m.end() if m else 0
            start, length = start + skip, length - skip
        out.append(TextSpan(start, length))
    return out


def statement_spans(kusto_code: Any) -> list[TextSpan]:
    """Top-level statements in source order, the ``;`` separator excluded."""
    offsets = Utf16Offsets(str(kusto_code.Text))
    return [
        TextSpan(*offsets.span_to_codepoints(stmt.TextStart, stmt.Width))
        for stmt in iter_elements(kusto_code.Syntax.Statements)
        if stmt.Width > 0
    ]
