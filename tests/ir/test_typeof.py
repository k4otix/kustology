# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``typeof(...)`` in argument position lowers to :class:`TypeOfExpr`.

The plugin operators take their output schema this way: ``evaluate
python(typeof(*, Score:real), ...)`` extends the input schema and ``evaluate
python(typeof(a:long, b:string), ...)`` replaces it. A structural node keeps
the construct's source text out of the digest, so interior spacing inside
``typeof(...)`` does not move ``semantic_hash``.

``star_indexes`` records every index at which ``*`` was written among the
elements, because each position contributes to the output column order:
bound against ``T(x, y)``, ``typeof(*, a:long)`` returns ``x, y, a``,
``typeof(a:long, *)`` returns ``a, x, y``, and ``typeof(*, *, a:long)``
returns ``x, y, x, y, a`` — a different column list from either single-star
spelling. A single index cannot carry that: it would record only the last
star's position, collapsing ``typeof(*, *, a:long)`` onto
``typeof(a:long, *)``.
"""

from kustology import parse
from kustology.ir import TypeOfExpr, find_all, walk


def _typeof(query: str) -> TypeOfExpr:
    ir = parse(query).to_ir(attach_schema=False)
    (node,) = find_all(ir, TypeOfExpr)
    return node


def test_a_bare_type_name_reports_type_names_only():
    node = _typeof("T | extend y = f(typeof(string))")
    assert node.type_names == ["string"]
    assert node.columns == []
    assert node.star_indexes == []


def test_declared_columns_carry_name_and_type_in_written_order():
    node = _typeof('T | evaluate python(typeof(a:long, b:string), "c")')
    assert [(c.name, c.declared_type) for c in node.columns] == [
        ("a", "long"), ("b", "string"),
    ]


def test_star_position_is_recorded_not_only_star_presence():
    """Each spelling carries a star, but at different positions: the field the
    output column order turns on."""
    leading = _typeof('T | evaluate python(typeof(*, a:long), "c")')
    assert leading.star_indexes == [0]
    assert len(leading.columns) == 1

    trailing = _typeof('T | evaluate python(typeof(a:long, *), "c")')
    assert trailing.star_indexes == [1]
    assert len(trailing.columns) == 1


def test_every_star_position_is_recorded_not_only_the_last():
    """A repeated star needs every one of its positions: recording only the
    last would make this indistinguishable from a single trailing star."""
    node = _typeof('T | evaluate python(typeof(*, *, a:long), "c")')
    assert node.star_indexes == [0, 1]
    assert len(node.columns) == 1


def test_typeof_in_argument_position_no_longer_reaches_unknown_expr():
    """``typeof(string)`` as ``extract()``/``extract_json()``'s optional type
    argument, in argument position: neither query builds a node whose class
    name starts with ``Unknown``."""
    queries = [
        'T | extend x = extract(@"a", 1, M, typeof(string))',
        'T | extend x = extract_json("$.a", M, typeof(string))',
    ]
    for query in queries:
        ir = parse(query).to_ir(attach_schema=False)
        unknowns = [n for n in walk(ir) if type(n).__name__.startswith("Unknown")]
        assert unknowns == []


def test_canonical_form_renders_the_kql_spelling():
    assert _typeof("T | extend y = f(typeof(string))").canonical_form == "typeof(string)"
    assert (
        _typeof('T | evaluate python(typeof(a:long, b:string), "c")').canonical_form
        == "typeof(a:long, b:string)"
    )
    assert (
        _typeof('T | evaluate python(typeof(*, a:long), "c")').canonical_form
        == "typeof(*, a:long)"
    )
    assert (
        _typeof('T | evaluate python(typeof(a:long, *), "c")').canonical_form
        == "typeof(a:long, *)"
    )
    assert (
        _typeof('T | evaluate python(typeof(*, *, a:long), "c")').canonical_form
        == "typeof(*, *, a:long)"
    )
