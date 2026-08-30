# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Define ``Span``, the code-point text range shared by every IR node."""

from pydantic import BaseModel


class Span(BaseModel):
    """A character range in the original query text: start offset plus width.

    Both are **code-point** offsets, so they index the Python ``str`` passed
    to ``parse()`` directly. Microsoft reports UTF-16 code units; the builder
    translates the whole tree once, after the build. A raw syntax node's
    ``TextStart`` counts UTF-16 code units, so cross it with
    :func:`kustology.utf16_to_codepoint`.
    """

    # ``extra="forbid"`` is the project-wide default for IR models: validation
    # of pre-existing JSON must fail loudly when fields drift, instead of
    # silently dropping data.
    model_config = {"extra": "forbid"}

    text_start: int
    width: int

    @property
    def text_end(self) -> int:
        """Return the code-point offset one past the span's last character."""
        return self.text_start + self.width

    def text(self, raw: str) -> str:
        """Slice the original query text covered by this span."""
        return raw[self.text_start : self.text_start + self.width]
