# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Tier 1 span types.

Pydantic-free on purpose: Tier 1 works without the ``[ir]`` extra, and
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
        return self.start + self.length

    def text(self, query: str) -> str:
        return query[self.start : self.end]


class TimeExpr(NamedTuple):
    """One result of ``find_time_expressions``; positionally ``(text, start, length)``."""

    text: str
    start: int
    length: int

    @property
    def span(self) -> TextSpan:
        return TextSpan(self.start, self.length)
