# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The to_ir() seam must reuse the already-parsed KustoCode — not re-parse.

Re-parsing would (a) double the cost of `parse().to_ir()` and (b) discard the
binder's symbol resolution from the original `parse(..., schema=...)` call.
This test wraps ``KustoCode.Parse`` and ``KustoCode.ParseAndAnalyze`` to count
invocations and asserts the count stays at 1 across the full flow.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from pykusto_language import parse  # noqa: E402
from pykusto_language import services as _services  # noqa: E402
from pykusto_language.ir import builder as _builder  # noqa: E402


class _Counter:
    """Static-method shim around the real KustoCode that counts Parse calls."""

    real = None  # set by fixture
    count = 0

    @classmethod
    def Parse(cls, text):
        cls.count += 1
        return cls.real.Parse(text)

    @classmethod
    def ParseAndAnalyze(cls, text, state):
        cls.count += 1
        return cls.real.ParseAndAnalyze(text, state)


@pytest.fixture
def parse_counter(monkeypatch):
    _Counter.real = _services.KustoCode
    _Counter.count = 0
    monkeypatch.setattr(_services, "KustoCode", _Counter)
    monkeypatch.setattr(_builder, "KustoCode", _Counter)
    yield _Counter


def test_to_ir_does_not_reparse_syntactic(parse_counter):
    """Syntactic parse → to_ir() must call Parse exactly once."""
    query = parse("DeviceProcessEvents | where FileName == 'cmd.exe'")
    assert parse_counter.count == 1, "parse() should call Parse once"

    ir = query.to_ir()
    assert parse_counter.count == 1, "to_ir() must reuse the parsed code, not re-parse"
    assert ir.main_pipeline is not None


def test_to_ir_does_not_reparse_semantic(parse_counter):
    """Semantic parse → to_ir() must call ParseAndAnalyze exactly once."""
    schema = {
        "DeviceProcessEvents": {
            "FileName": "string",
            "TimeGenerated": "datetime",
        },
    }
    query = parse("DeviceProcessEvents | where FileName == 'cmd.exe'", schema=schema)
    assert parse_counter.count == 1, "parse(schema=...) should call ParseAndAnalyze once"
    assert query.has_semantics

    ir = query.to_ir()
    assert parse_counter.count == 1, "to_ir() on a bound query must reuse the parse"
    assert ir.schema_attached is False  # SchemaAttacher not invoked yet — that's by design
