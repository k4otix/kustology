# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier 1 span types.

Pydantic-free: Tier 1 works without the ``[ir]`` extra, and
``kustology.ir.Span`` is a pydantic model. Offsets are code points into the
Python ``str`` the query was parsed from.
"""

from __future__ import annotations

from typing import NamedTuple


class TextSpan(NamedTuple):
    """A run of ``length`` code points starting at ``start``."""

    start: int
    length: int

    @property
    def end(self) -> int:
        """Return the code-point offset one past the span's last character."""
        return self.start + self.length

    def text(self, query: str) -> str:
        """Slice the code points this span covers out of ``query``."""
        return query[self.start : self.end]


class SourceRef(NamedTuple):
    """One thing a query reads, with the kind alongside the name.

    ``kind`` is one of ``"table"``, ``"function"``, ``"externaldata"``, or
    ``"datatable"``. ``name`` carries the table or function name; the two
    anonymous kinds have no name and carry ``None``.

    ``span`` covers the table's name for a table and the whole construct for
    the other three kinds, so ``span.text(query)`` reads back
    ``SecurityEvent`` for a table and ``_GetWatchlist("AllowedRanges")`` for a
    function call.

    A wildcard resolved by the binder is the exception. ``union T*`` bound
    against a schema with exactly one matching table reports that table's
    ``name``, ``T1``, against the span holding the pattern ``T*``.
    """

    kind: str
    name: str | None
    span: TextSpan


class TimeExpr(NamedTuple):
    """One result of ``find_time_expressions``; positionally ``(text, start, length)``."""

    text: str
    start: int
    length: int

    @property
    def span(self) -> TextSpan:
        """Return this expression's location as a :class:`TextSpan`."""
        return TextSpan(self.start, self.length)
