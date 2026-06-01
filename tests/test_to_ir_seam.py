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

from kustology import parse  # noqa: E402
from kustology import services as _services  # noqa: E402
from kustology.ir import builder as _builder  # noqa: E402


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

    ir = query.to_ir(attach_schema=False)
    assert parse_counter.count == 1, "to_ir() on a bound query must reuse the parse"
    assert ir.schema_attached is False  # explicit opt-out keeps SchemaAttacher disabled


def test_to_ir_default_attaches_when_schema_available():
    """Default ``to_ir()`` on a bound query should auto-run ``SchemaAttacher``.

    Without this, callers that legitimately want table provenance had to write
    ``parse(query, schema=schema).to_ir(attach_schema=True)`` — the schema
    appears twice in the call chain even though only one schema is in play.
    """
    schema = {
        "DeviceProcessEvents": {
            "FileName": "string",
            "DeviceName": "string",
        },
    }
    ir = parse(
        "DeviceProcessEvents "
        "| where FileName == 'cmd.exe' "
        "| summarize attempts = count() by DeviceName",
        schema=schema,
    ).to_ir()

    assert ir.schema_attached is True
    assert ir.main_pipeline.result_schema is not None

    from kustology.ir import FilterOp
    filter_op = next(op for op in ir.main_pipeline.operators if isinstance(op, FilterOp))
    assert filter_op.predicate.left.table == "DeviceProcessEvents"


def test_to_ir_default_skips_attach_when_no_schema():
    """Default ``to_ir()`` on a syntactic-only parse must not crash and must
    not falsely report ``schema_attached``."""
    ir = parse("DeviceProcessEvents | where FileName == 'cmd.exe'").to_ir()
    assert ir.schema_attached is False


def test_to_ir_explicit_attach_schema_true_still_works():
    """``attach_schema=True`` keeps its original meaning (force-attach)."""
    schema = {"DeviceProcessEvents": {"FileName": "string"}}
    ir = parse(
        "DeviceProcessEvents | where FileName == 'cmd.exe'",
        schema=schema,
    ).to_ir(attach_schema=True)
    assert ir.schema_attached is True


def test_to_ir_explicit_attach_schema_dict_overrides_parse_schema():
    """Passing a dict to ``attach_schema`` overrides the parse-time schema —
    useful when the binder ran against a partial schema but the attacher
    should see a more complete one."""
    parse_schema = {"DeviceProcessEvents": {"FileName": "string"}}
    attach_only = {
        "DeviceProcessEvents": {
            "FileName": "string",
            "DeviceName": "string",
        },
    }
    ir = parse(
        "DeviceProcessEvents "
        "| where FileName == 'cmd.exe' "
        "| summarize attempts = count() by DeviceName",
        schema=parse_schema,
    ).to_ir(attach_schema=attach_only)
    assert ir.schema_attached is True
    assert ir.main_pipeline.result_schema is not None
    assert "DeviceName" in dict(ir.main_pipeline.result_schema.columns)
