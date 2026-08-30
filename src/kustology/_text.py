# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Translate between Python's code-point offsets and .NET's UTF-16 offsets.

A Python ``str`` is indexed by code point. A .NET ``System.String`` is
indexed by UTF-16 code unit, so every offset ``Kusto.Language`` reports
(``TextStart``, ``Width``, a diagnostic's ``Start``) counts code units. The
two agree across the whole Basic Multilingual Plane and diverge by one per
astral character (an emoji, a rare CJK ideograph, or a historic script) that
precedes the offset. Slicing the query text you passed in at a .NET offset is
therefore correct for almost all input and silently wrong for the rest.

:class:`Utf16Offsets` converts between the two units, and
:func:`check_utf16_encodable` rejects the text .NET cannot represent at all.
"""

from bisect import bisect_left

# A code point above this needs a surrogate pair in UTF-16, so it counts as
# one Python character and two .NET ones.
_ASTRAL_FLOOR = 0x10000


def check_utf16_encodable(text: str, what: str = "query text") -> bytes:
    r"""Return ``text`` encoded as UTF-16LE, or raise ``ValueError``.

    A Python ``str`` can hold an unpaired surrogate (``"\\ud800"``), which no
    UTF-16 byte sequence encodes. Ordinary input reaches this: a YAML file
    containing ``query: "\\ud800"`` decodes to exactly one.

    Call this before handing text to ``Kusto.Language``. pythonnet marshals a
    ``str`` argument by encoding it to UTF-16, and when that fails it raises
    on the CLR side, where the exception is unhandled and aborts the process
    with ``SIGABRT``. No Python ``except`` clause intercepts that, including
    ``except BaseException``, so the check has to happen first.

    ``what`` names the position for the message, so a bad schema column reads
    as one instead of as query text.

    Hands back the encoded bytes so a caller that also needs
    :class:`Utf16Offsets` does not encode twice.
    """
    try:
        return text.encode("utf-16-le")
    except UnicodeEncodeError as e:
        raise ValueError(
            f"{what} is not encodable to UTF-16 at position {e.start}: "
            f"{text[e.start:e.end]!r} ({e.reason}). Kusto.Language cannot "
            "receive this string."
        ) from e


class Utf16Offsets:
    """Offset translation for one string, in both directions.

    Build one per query and reuse it. Construction is a single encode plus,
    for text that needs it, one scan; each translation is then a binary
    search over the astral characters alone.

    All-BMP text takes an identity fast path where both methods return their
    argument unchanged. That covers every query holding no emoji and no other
    astral character.
    """

    __slots__ = ("_astral_utf16", "_identity")

    def __init__(self, text: str, encoded: bytes | None = None):
        """Index ``text``, reusing ``encoded`` when you already have the bytes."""
        if encoded is None:
            encoded = check_utf16_encodable(text)
        # One code unit per code point means no surrogate pairs, so the two
        # offset spaces coincide. Comparing lengths reads that off the encode
        # already done, rather than scanning the string a second time.
        self._identity = len(encoded) // 2 == len(text)
        # Each entry is the UTF-16 offset of an astral character's leading
        # surrogate. The k-th such character sits k units further into the
        # UTF-16 string than into the Python one, which is what makes the
        # difference between the two offsets countable by bisection.
        self._astral_utf16: list[int] = (
            []
            if self._identity
            else [
                cp_index + k
                for k, cp_index in enumerate(
                    i for i, ch in enumerate(text) if ord(ch) >= _ASTRAL_FLOOR
                )
            ]
        )

    @property
    def is_identity(self) -> bool:
        """True when the string is all-BMP, so both units agree everywhere."""
        return self._identity

    def to_codepoint(self, offset: int) -> int:
        """Convert a UTF-16 offset to a code-point offset.

        An offset pointing at the trailing half of a surrogate pair rounds
        **down** to the pair's start, since that half is not a character a
        Python index can name. Token boundaries never land there, because a
        lexer splits on characters, so this governs only hand-computed
        offsets.
        """
        if self._identity:
            return offset
        return offset - bisect_left(self._astral_utf16, offset)

    def to_utf16(self, offset: int) -> int:
        """Convert a code-point offset to a UTF-16 offset."""
        if self._identity:
            return offset
        # Each astral character at or before ``offset`` in code-point space
        # adds one unit. Walk the same index in the other direction: the
        # k-th entry sits at code point ``entry - k``.
        low, high = 0, len(self._astral_utf16)
        while low < high:
            mid = (low + high) // 2
            if self._astral_utf16[mid] - mid < offset:
                low = mid + 1
            else:
                high = mid
        return offset + low

    def span_to_codepoints(self, start: int, width: int) -> tuple[int, int]:
        """Convert a UTF-16 ``(start, width)`` pair to code-point units.

        This translates the end and re-derives the width from it. A width is
        a difference between two positions, so an astral character inside the
        span shrinks it by one while leaving the start alone.
        """
        if self._identity:
            return start, width
        cp_start = self.to_codepoint(start)
        return cp_start, self.to_codepoint(start + width) - cp_start


def utf16_to_codepoint(text: str, offset: int) -> int:
    r"""Convert a UTF-16 offset into ``text`` to a code-point offset.

    Use this to index a Python ``str`` with an offset ``Kusto.Language``
    reported. ``node.TextStart``, ``node.Width`` and every other offset on a
    raw syntax node count UTF-16 code units:

        >>> from kustology import parse, utf16_to_codepoint
        >>> q = 'let e="\\U0001F600"; T | where X > 1'
        >>> tok = next(t for t in parse(q).syntax.GetTokens() if t.Text == "where")
        >>> q[tok.TextStart:][:5]
        'here '
        >>> start = utf16_to_codepoint(q, tok.TextStart)
        >>> q[start:][:5]
        'where'

    Every offset kustology itself reports is already in code points (Tier 2
    :class:`~kustology.ir.spans.Span`, the ``start``/``length`` of a
    diagnostic dict, and ``find_time_expressions``), so this is for callers
    reading Microsoft's nodes directly.

    Indexing ``text`` costs O(len(text)) per call. Translating more than a
    couple of offsets from one query is what
    :class:`~kustology._text.Utf16Offsets` is for.
    """
    return Utf16Offsets(text).to_codepoint(offset)


def codepoint_to_utf16(text: str, offset: int) -> int:
    """Convert a code-point offset into ``text`` to a UTF-16 offset.

    The inverse of :func:`utf16_to_codepoint`, for comparing an offset you
    computed in Python against one a raw syntax node reports. Carries the
    same per-call indexing cost.
    """
    return Utf16Offsets(text).to_utf16(offset)
