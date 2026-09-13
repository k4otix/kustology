# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Define ``Span``, the code-point text range shared by every IR node."""

from pydantic import BaseModel


class Span(BaseModel):
    """A character range in the original query text: start offset plus width.

    Both are **code-point** offsets, so they index the Python ``str`` passed
    to ``parse()`` directly. Microsoft reports UTF-16 code units, which the
    builder translates over the whole tree once after the build. A raw syntax
    node's ``TextStart`` is still UTF-16, so cross it with
    :func:`kustology.utf16_to_codepoint`.

    A ``Span`` is immutable. Assigning to ``text_start`` or ``width`` raises;
    build a new ``Span`` and install it on the field that holds the old one.
    """

    # ``extra="forbid"`` is the project-wide default for IR models: validating
    # existing JSON fails loudly when fields drift instead of silently dropping
    # data. ``frozen`` is what makes :meth:`__deepcopy__` below safe, since a
    # shared instance that could be mutated would let one copy of an IR rewrite
    # another's offsets.
    model_config = {"extra": "forbid", "frozen": True}

    text_start: int
    width: int

    def __deepcopy__(self, memo: dict[int, object] | None = None) -> "Span":
        """Return this instance: a frozen value needs no copy.

        Spans are about half the nodes in an IR, and copying one costs what
        copying any pydantic model costs. Sharing the instance keeps that cost
        out of ``copy.deepcopy(ir)`` and out of the private copy each digest is
        built from.
        """
        return self

    @property
    def text_end(self) -> int:
        """Return the code-point offset one past the span's last character."""
        return self.text_start + self.width

    def text(self, raw: str) -> str:
        """Slice the original query text covered by this span."""
        return raw[self.text_start : self.text_start + self.width]
