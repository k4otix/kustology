# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan


from pydantic import BaseModel


class Span(BaseModel):
    """A character range in the original query text: start offset plus width."""

    # ``extra="forbid"`` is the project-wide default for IR models: validation
    # of pre-existing JSON must fail loudly when fields drift, instead of
    # silently dropping data.
    model_config = {"extra": "forbid"}

    text_start: int
    width: int

    @property
    def text_end(self) -> int:
        return self.text_start + self.width

    def text(self, raw: str) -> str:
        """Slice the original query text covered by this span."""
        return raw[self.text_start : self.text_start + self.width]
