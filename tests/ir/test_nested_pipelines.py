# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Nested pipelines are typed, so a JSON round-trip keeps the subtree.

``ToScalarExpr.pipeline`` and ``SubqueryExpr.pipeline`` are typed as
:class:`~kustology.ir.Pipeline`, not ``Any`` — the escape hatch available for
the ``expr`` ↔ ``query`` import cycle. ``Any`` would tell pydantic nothing
about the field, so an in-memory IR built with a real ``Pipeline`` there
would still look right and every ``find_all`` would still reach inside, but
``QueryIR.model_validate_json(ir.model_dump_json())`` would validate against
no type at all and reload the whole nested query as a **plain dict**.

That failure shape has two consequences, and this suite guards both, because
neither is visible from a dump alone:

* The reloaded IR would not equal the one it came from, breaking the
  round-trip contract ``tests/ir/test_ir_roundtrip.py`` asserts for every
  other shape.
* ``walk`` yields ``BaseModel`` descendants only, and a dict of primitives
  has none — so the entire inner query would vanish from
  ``walk``/``find_all`` after a round-trip even though the same query walks
  fine before it. An analyzer run on stored IR would see a ``toscalar(...)``
  with nothing in it, and because ``compute_semantic_hash`` strips volatile
  fields by walking, every span inside the nested pipeline would stay in the
  digest, so rehashing stored IR would not reproduce its own hash.

The assertion that proves the subtree survived is the ``walk`` count, not
just the equality: two dicts can compare equal while holding no models at
all. Both are checked, in both bind states.
"""

import pytest

from kustology import parse
from kustology.ir import (
    ColumnRef,
    Pipeline,
    QueryIR,
    SubqueryExpr,
    TableRef,
    ToScalarExpr,
    find_all,
    walk,
)

_SCHEMA = {
    "T": {"a": "long", "User": "string"},
    "U": {"a": "long"},
    "Suspicious": {"User": "string"},
}

# One query per pipeline-bearing expression class.
QUERIES = [
    "T | where a > toscalar(U | summarize max(a))",
    "T | where User in ((Suspicious | project User))",
    # Both in one query, and a nested pipeline that itself carries a filter,
    # a projection and a second level of nesting.
    (
        "T | where User in ((Suspicious | where User != 'svc' | project User)) "
        "and a > toscalar(U | where a > toscalar(U | count) | summarize max(a))"
    ),
]

MODES = [None, _SCHEMA]
MODE_IDS = ["unbound", "bound"]


def _ir(query: str, schema: dict | None) -> QueryIR:
    return (
        parse(query, schema=schema).to_ir()
        if schema
        else parse(query).to_ir(attach_schema=False)
    )


@pytest.mark.parametrize("schema", MODES, ids=MODE_IDS)
@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q[:44])
def test_json_round_trip_preserves_the_nested_pipeline(query, schema):
    ir = _ir(query, schema)
    reloaded = QueryIR.model_validate_json(ir.model_dump_json())

    assert reloaded == ir
    # The count is the load-bearing half: equality alone can hold between two
    # models whose ``Any`` fields are both dicts.
    assert len(list(walk(reloaded))) == len(list(walk(ir)))


@pytest.mark.parametrize("schema", MODES, ids=MODE_IDS)
@pytest.mark.parametrize("query", QUERIES, ids=lambda q: q[:44])
def test_the_reloaded_nested_pipeline_is_a_pipeline(query, schema):
    """An ``Any`` field would accept the dict silently; the declared
    ``Pipeline`` type is what makes validation rebuild the model."""
    reloaded = QueryIR.model_validate_json(_ir(query, schema).model_dump_json())
    nested = [
        *find_all(reloaded, ToScalarExpr),
        *find_all(reloaded, SubqueryExpr),
    ]
    assert nested, "expected a pipeline-bearing expression"
    for node in nested:
        assert isinstance(node.pipeline, Pipeline)


@pytest.mark.parametrize("schema", MODES, ids=MODE_IDS)
def test_find_all_still_reaches_inside_after_a_round_trip(schema):
    """The regression a consumer would hit: an analyzer over stored IR.

    ``Suspicious`` and ``U`` are named only inside nested pipelines, and
    ``max(a)`` is the only place ``a`` appears in the ``toscalar``."""
    query = QUERIES[2]
    ir = _ir(query, schema)
    reloaded = QueryIR.model_validate_json(ir.model_dump_json())

    before = sorted(t.name for t in find_all(ir, TableRef))
    after = sorted(t.name for t in find_all(reloaded, TableRef))
    assert before == ["Suspicious", "T", "U", "U"]
    assert after == before

    assert sorted(c.name for c in find_all(reloaded, ColumnRef)) == sorted(
        c.name for c in find_all(ir, ColumnRef)
    )


@pytest.mark.parametrize("schema", MODES, ids=MODE_IDS)
def test_the_hash_of_a_reloaded_ir_matches(schema):
    """Rehashing stored IR must reproduce the digest it was stored with.

    Guards ``compute_semantic_hash`` against the failure mode above: it
    strips volatile fields by walking the tree, and ``walk`` cannot enter a
    dict of primitives, so every span inside a round-tripped nested pipeline
    would survive into the digest and the same query would hash two ways
    depending on whether it had gone through JSON. ``QueryIR.semantic_hash``
    is computed at build time, so a stale shipped value would hide this; the
    field's own docstring tells consumers to call ``compute_semantic_hash``
    again after mutating the IR, which is exactly the path this guards.
    """
    from kustology.ir import compute_semantic_hash

    ir = _ir(QUERIES[2], schema)
    reloaded = QueryIR.model_validate_json(ir.model_dump_json())
    assert compute_semantic_hash(reloaded) == compute_semantic_hash(ir)


@pytest.mark.parametrize("cls", [ToScalarExpr, SubqueryExpr])
def test_the_forward_reference_really_resolved(cls):
    """The mechanism, pinned directly.

    ``Pipeline`` is a ``TYPE_CHECKING``-only name in ``expr.py``; the classes
    are rebuilt at the bottom of ``query.py``, which resolves the reference
    from *that* module's namespace. If a pydantic change stopped reaching the
    calling module, the model would fall back to an unresolved annotation and
    every symptom above would return quietly — an in-memory IR would still
    look right. This fails instead.
    """
    assert cls.__pydantic_complete__
    assert cls.model_fields["pipeline"].annotation == Pipeline | None


def test_a_nested_pipeline_of_the_wrong_shape_is_rejected():
    """An ``Any`` field would validate anything at all. The typed field
    turns a corrupt stored dump into a ``ValidationError`` instead of an IR
    whose ``toscalar`` holds a string."""
    import json

    from pydantic import ValidationError

    ir = _ir(QUERIES[0], None)
    payload = json.loads(ir.model_dump_json())
    predicate = payload["main_pipeline"]["operators"][0]["predicate"]
    assert predicate["right"]["kind"] == "to_scalar"

    predicate["right"]["pipeline"] = "U | summarize max(a)"
    with pytest.raises(ValidationError):
        QueryIR.model_validate(payload)
