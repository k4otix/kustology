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

from kustology import core as _core
from kustology import parse
from kustology import services as _services
from kustology.ir import builder as _builder


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
    # ``core`` binds ``KustoCode`` at module scope too, and ``core.to_ir``
    # is the function every test in this file is actually about. Leaving it
    # out points the instrument away from the code under test; see
    # ``test_the_counter_is_wired_to_every_module_to_ir_parses_through``.
    monkeypatch.setattr(_core, "KustoCode", _Counter)
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
    """Default ``to_ir()`` on a bound query auto-runs ``SchemaAttacher``.

    Without the default, a caller who wants table provenance writes
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


def test_a_dict_attach_reaches_microsofts_binder():
    """`attach_schema=dict` must bind through build_global_state + Analyze.

    `scan declare` adds columns (`v`, `match_id`) that only Microsoft's
    binder computes -- ScanOp is modeled as raw text and the hand rules
    never answered for it -- so their presence proves the dict reached
    Microsoft rather than the mirror."""
    q = "T | scan declare (v: long = 0) with (step s1: true => v = 1;)"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    cols = list(ir.main_pipeline.result_schema.columns)
    assert "v" in cols
    assert "match_id" in cols


def test_the_dict_path_equals_the_parse_time_binding():
    """`to_ir(attach_schema=d)` and `parse(q, schema=d).to_ir()` are the same
    computation, shape included: `let A = T` lowers to rhs_pipeline in both.
    An unbound build produces rhs_expr instead — the divergence this pins."""
    schema = {"T": {"a": "long"}}
    q = "let A = T; A | project a"
    via_dict = parse(q).to_ir(attach_schema=schema)
    via_parse = parse(q, schema=schema).to_ir()
    assert via_dict.model_dump(mode="json") == via_parse.model_dump(mode="json")


def test_a_dict_override_on_a_bound_parse_rebinds():
    """A dict on an already-bound parse re-binds against the dict, rather
    than overlaying the parse-time answer — which would keep its types."""
    ir = parse("T | project a", schema={"T": {"a": "long"}}).to_ir(
        attach_schema={"T": {"a": "real"}}
    )
    assert ir.main_pipeline.result_schema.columns == {"a": "real"}


def test_the_dict_path_leaves_the_receiver_syntactic():
    kq = parse("T | take 1")
    kq.to_ir(attach_schema={"T": {"a": "long"}})
    assert kq.has_semantics is False


def test_a_partial_dict_stays_lenient():
    """Unknown tables under a partial dict are the Sentinel norm: the IR
    builds, and operators Microsoft leaves open report result_schema=None
    rather than raising or guessing."""
    q = "Unknown | where x > 1"
    ir = parse(q).to_ir(attach_schema={"T": {"a": "long"}})
    assert ir.main_pipeline is not None


def test_an_empty_dict_means_no_attach_and_no_rebind():
    """`attach_schema={}` is falsy: no rebind, no provenance pass -- same
    as False. The dict reroute triggers only on a non-empty dict."""
    ir = parse("T | take 1").to_ir(attach_schema={})
    assert ir.schema_attached is False
    assert ir.main_pipeline.result_schema is None


def test_schemaless_to_ir_analyzes_without_a_second_parse(parse_counter):
    """Schemaless ``to_ir()`` must analyze with ``Analyze``, not a re-parse.

    ``KustoCode.Analyze(globals)`` binds the tree already in hand and returns
    a new bound ``KustoCode``; ``ParseAndAnalyze`` would throw the tree away
    and lex the text again. The counter cannot tell the two apart from the
    IR, which is exactly why it is asserted here.
    """
    query = parse("DeviceProcessEvents | where FileName == 'cmd.exe'")
    assert parse_counter.count == 1

    ir = query.to_ir()
    assert parse_counter.count == 1, "schemaless to_ir() must not re-parse"
    assert query.has_semantics is False, (
        "Analyze returns a new KustoCode; the receiver stays syntactic so "
        "Tier 1 keeps its syntactic path"
    )
    assert ir.schema_attached is False


def test_result_schema_survives_an_opted_out_attach_on_a_bound_parse():
    """``attach_schema=False`` skips provenance, not Microsoft's shape.

    ``result_schema`` comes from the binder, which already knows it;
    ``SchemaAttacher`` adds only the ``ColumnRef.table`` provenance pass. A
    caller who wants the output columns and not the provenance does not pay
    for both.
    """
    schema = {"DeviceProcessEvents": {"FileName": "string", "DeviceName": "string"}}
    ir = parse(
        "DeviceProcessEvents | project FileName", schema=schema,
    ).to_ir(attach_schema=False)

    assert ir.schema_attached is False
    assert ir.main_pipeline.result_schema.columns == {"FileName": "string"}


def test_a_schemaless_parse_still_has_no_result_schema():
    """Default-globals analysis must not fabricate an output schema.

    Default globals know no tables, so the symbol is *open* and Microsoft
    declines. The columns an open symbol does list are the ones the query
    named, typed ``unknown`` — publishing those as the result schema would
    be a guess wearing the binder's authority.
    """
    ir = parse("DeviceProcessEvents | project FileName").to_ir()
    assert ir.main_pipeline.result_schema is None


def test_a_partial_dict_keeps_the_receivers_diagnostic_leniency():
    """The dict path binds like parse(q, schema=...) for schemas, types and
    shape -- but diagnostics follow the receiver: an unbound receiver stays
    lenient about unknown names, a bound receiver keeps them."""
    q = "Unknown | where x > 1"
    d = {"T": {"a": "long"}}
    lenient = parse(q).to_ir(attach_schema=d)
    strict = parse(q, schema=d).to_ir()
    assert lenient.diagnostics == []
    assert any(diag.code == "KS204" for diag in strict.diagnostics)


def test_the_counter_is_wired_to_every_module_to_ir_parses_through(parse_counter):
    """The fixture must patch ``core``, or the tests above guard nothing.

    ``core.to_ir`` is where the reuse decision lives, and ``core`` binds
    ``KustoCode`` at module scope (``from .bridge import GlobalState,
    KustoCode``). Patching ``services`` and ``ir.builder`` leaves that
    binding pointing at the real class, so a re-parse introduced *there* --
    the one place a re-parse would actually be introduced -- would not move
    the counter and every assertion in this file would stay green through
    it. The invariant is real; without this the instrument is not.
    """
    import kustology.bridge

    patched = {
        name
        for name, module in (
            ("core", _core), ("services", _services), ("builder", _builder),
        )
        if getattr(module, "KustoCode", None) is not kustology.bridge.KustoCode
    }
    assert patched == {"core", "services", "builder"}
