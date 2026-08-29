# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""The unwritten-default strip is one table-driven pass over the dumped payload.

``compute_semantic_hash`` drops an operator's modifier keys when every one is
still at its unwritten default, so declaring a modifier does not move the
digest of a query that never writes one. ``tests/ir/test_hash_battery.py``
covers that end to end. These tests pin the helper's own contract, which is
subtle in three places the end-to-end tests cannot isolate: the gate is
all-or-nothing, a dict missing the keys is left alone rather than crashing,
and a list-valued default has to compare by equality.
"""

import pytest

pytest.importorskip("pydantic")

from kustology.ir.transforms import (
    _UNWRITTEN_DEFAULTS,
    _strip_unwritten_fields,
)


def test_an_all_default_operator_loses_its_modifier_keys():
    payload = {"kind": "evaluate", "declared_schema": None, "declared_schema_star": False}

    _strip_unwritten_fields(payload)

    assert payload == {"kind": "evaluate"}


def test_a_partly_written_operator_keeps_every_key():
    """One written flag holds the whole row, so the digest reflects the write."""
    payload = {
        "kind": "mv_apply",
        "to_typeof": None,
        "row_limit": 5,
        "item_index": None,
    }

    _strip_unwritten_fields(payload)

    assert payload == {
        "kind": "mv_apply",
        "to_typeof": None,
        "row_limit": 5,
        "item_index": None,
    }


def test_a_foreign_dict_carrying_the_same_kind_tag_is_left_alone():
    """Without the presence check this matches the gate and crashes on ``del``."""
    payload = {"kind": "consume", "something": "else"}

    _strip_unwritten_fields(payload)

    assert payload == {"kind": "consume", "something": "else"}


def test_a_list_valued_default_strips():
    """``[]`` compares by equality; identity would never match a fresh dump."""
    payload = {"kind": "parse_kv", "properties": []}

    _strip_unwritten_fields(payload)

    assert payload == {"kind": "parse_kv"}


def test_a_written_list_survives():
    payload = {"kind": "parse_kv", "properties": ["a"]}

    _strip_unwritten_fields(payload)

    assert payload == {"kind": "parse_kv", "properties": ["a"]}


def test_zero_does_not_read_as_false():
    """Scalar defaults compare by identity, so ``0 == False`` cannot strip."""
    payload = {"kind": "getschema", "output_kind": 0}

    _strip_unwritten_fields(payload)

    assert payload == {"kind": "getschema", "output_kind": 0}


def test_the_pass_reaches_every_kind_wherever_it_sits():
    """One traversal covers the whole table, at any depth and inside lists."""
    payload = {
        "kind": "query",
        "ops": [
            {"kind": "evaluate", "declared_schema": None, "declared_schema_star": False},
            {"nested": {"kind": "consume", "decodeblocks": None}},
        ],
        "sub": {"kind": "getschema", "output_kind": None},
    }

    _strip_unwritten_fields(payload)

    assert payload == {
        "kind": "query",
        "ops": [{"kind": "evaluate"}, {"nested": {"kind": "consume"}}],
        "sub": {"kind": "getschema"},
    }


@pytest.mark.parametrize("kind", sorted(_UNWRITTEN_DEFAULTS))
def test_every_row_in_the_table_strips(kind):
    """A row added to the table without a working default fails here."""
    payload = {"kind": kind, **_UNWRITTEN_DEFAULTS[kind]}

    _strip_unwritten_fields(payload)

    assert payload == {"kind": kind}
