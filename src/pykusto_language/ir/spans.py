# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from typing import Optional

from pydantic import BaseModel


class Span(BaseModel):
    text_start: int
    width: int
    source_text: Optional[str] = None

    @property
    def text_end(self) -> int:
        return self.text_start + self.width

    def text(self, raw: str) -> str:
        """Slice the original query text covered by this span."""
        return raw[self.text_start : self.text_start + self.width]
