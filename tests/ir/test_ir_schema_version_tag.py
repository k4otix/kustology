# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Pin the ``ir_schema_version`` tag a ``QueryIR`` dump carries and a load checks.

Every IR model sets ``extra="forbid"`` and most fields are required. Checked
field by field, a dump from another IR schema fails only where its query
reaches a field that changed shape, and the error names that field. The tag
check runs before any field, so a dump tagged with another version, or with
none, fails with one error of type ``ir_schema_version`` whatever its query
uses.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from kustology import parse
from kustology.ir import Pipeline, QueryIR, Span, UnknownSource, compute_semantic_hash

PLAIN = "T | where x > 1 | project x"
SCAN = "T | scan declare (v:long=0) with (step s1: x > 1 => v = x;)"

LOADERS = {
    "model_validate": QueryIR.model_validate,
    "model_validate_json": lambda payload: QueryIR.model_validate_json(json.dumps(payload)),
}


def _dump(query: str) -> dict:
    return parse(query).to_ir().model_dump(mode="json")


def _tag_error(excinfo: pytest.ExceptionInfo[ValidationError]) -> dict:
    errors = excinfo.value.errors()
    assert [e["type"] for e in errors] == ["ir_schema_version"], errors
    return errors[0]


# Both options drop declared fields that hold their default or were never set.
# The tag has to survive them, or the smaller dump fails to load.
@pytest.mark.parametrize(
    "dump_kwargs", [{}, {"exclude_defaults": True}, {"exclude_unset": True}],
    ids=["plain", "exclude_defaults", "exclude_unset"],
)
def test_a_dump_carries_the_ir_schema_version(dump_kwargs):
    ir = parse(PLAIN).to_ir()
    assert ir.model_dump(mode="json", **dump_kwargs)["ir_schema_version"] == "0.3"
    assert json.loads(ir.model_dump_json(**dump_kwargs))["ir_schema_version"] == "0.3"


@pytest.mark.parametrize("loader", LOADERS.values(), ids=LOADERS.keys())
def test_a_tagged_dump_loads(loader):
    payload = _dump(SCAN)
    assert loader(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize("loader", [*LOADERS.values(), lambda p: QueryIR(**p)],
                         ids=[*LOADERS.keys(), "constructor"])
def test_a_dump_tagged_with_another_version_is_rejected(loader):
    payload = {**_dump(PLAIN), "ir_schema_version": "0.2"}
    with pytest.raises(ValidationError) as excinfo:
        loader(payload)
    error = _tag_error(excinfo)
    assert error["ctx"] == {"found": "0.2", "expected": "0.3"}


@pytest.mark.parametrize("loader", LOADERS.values(), ids=LOADERS.keys())
def test_an_untagged_dump_is_rejected(loader):
    payload = _dump(PLAIN)
    del payload["ir_schema_version"]
    with pytest.raises(ValidationError) as excinfo:
        loader(payload)
    assert _tag_error(excinfo)["ctx"] == {"found": None, "expected": "0.3"}


@pytest.mark.parametrize("loader", LOADERS.values(), ids=LOADERS.keys())
def test_untagged_dumps_fail_alike_whatever_their_query_uses(loader):
    """Check that both IR schema 0.2 dump shapes fail on the tag.

    An IR schema 0.2 dump carries no tag. ``plain`` reaches no field that
    changed shape in IR schema 0.3, and ``scan`` holds ``raw_text`` where IR
    schema 0.3 has typed ``steps``.
    """
    plain = _dump(PLAIN)
    scan = _dump(SCAN)
    op = scan["main_pipeline"]["operators"][0]
    del op["steps"]
    op["raw_text"] = "scan declare (v:long=0) with (step s1: x > 1 => v = x;)"

    # Control: tagged as current, ``plain`` loads and ``scan`` fails on its fields.
    loader(plain)
    with pytest.raises(ValidationError) as structural:
        loader(scan)
    assert {e["type"] for e in structural.value.errors()} == {"missing", "extra_forbidden"}

    for payload in (plain, scan):
        del payload["ir_schema_version"]
        with pytest.raises(ValidationError) as excinfo:
            loader(payload)
        _tag_error(excinfo)


def test_the_constructor_needs_no_tag():
    ir = QueryIR(
        raw_text="T",
        let_bindings=[],
        main_pipeline=Pipeline(
            source=UnknownSource(raw_text="T", span=Span(text_start=0, width=1)),
            operators=[],
        ),
    )
    assert QueryIR.model_validate(ir.model_dump()).raw_text == "T"


def test_the_tag_stays_out_of_the_digest(monkeypatch):
    """Check that changing ``IR_SCHEMA_VERSION`` alone leaves the digest unchanged.

    ``SEMANTIC_HASH_SCHEME`` is the tag that versions the digest.
    """
    import kustology.ir.query as query_module

    ir = parse(SCAN).to_ir()
    before = compute_semantic_hash(ir)
    monkeypatch.setattr(query_module, "IR_SCHEMA_VERSION", "9.9")
    assert ir.model_dump(mode="json", exclude={"semantic_hash"})["ir_schema_version"] == "9.9"
    assert compute_semantic_hash(ir) == before
