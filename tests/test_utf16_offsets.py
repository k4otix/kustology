# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every offset kustology reports indexes the Python ``str``, not the .NET one.

.NET counts string offsets in UTF-16 code units; a Python ``str`` is indexed
by code point. The two agree across the whole Basic Multilingual Plane and
diverge by one per astral character — an emoji, a rare CJK ideograph, a
historic script — that precedes the offset.

That divergence is data-dependent and silent, which is what makes it worth a
test file: a corpus containing no astral character reports every one of these
surfaces as correct. So each test here pairs an astral query against the same
query without one, and the assertion is that slicing the text at the reported
offset returns the construct the offset names.

Raw ``Kusto.Language`` nodes are excluded on purpose. ``node.TextStart`` is
Microsoft's value and stays in UTF-16; :func:`kustology.utf16_to_codepoint` is
the supported way across, and it is tested here too.
"""

import pytest

from kustology import codepoint_to_utf16, parse, utf16_to_codepoint, validate
from kustology._text import Utf16Offsets

EMOJI = "\U0001F600"  # one code point, two UTF-16 code units
ASTRAL_CJK = "\U00020000"


def reference_utf16_offset(text: str, codepoint_offset: int) -> int:
    """Independent answer, by encoding the prefix rather than by bisection."""
    return len(text[:codepoint_offset].encode("utf-16-le")) // 2


# -- the translator itself ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "T | count",
        f"let e='{EMOJI}'; T | count",
        f"{EMOJI}{EMOJI}{EMOJI}",
        f"a{ASTRAL_CJK}b{EMOJI}c",
        "ß中x\n",
    ],
)
def test_both_directions_agree_with_encoding_the_prefix(text):
    offsets = Utf16Offsets(text)
    for cp in range(len(text) + 1):
        u16 = reference_utf16_offset(text, cp)
        assert offsets.to_utf16(cp) == u16
        assert offsets.to_codepoint(u16) == cp


def test_bmp_text_takes_the_identity_path():
    assert Utf16Offsets("T | where x > 1h").is_identity is True


def test_astral_text_does_not():
    assert Utf16Offsets(f"T | where x == '{EMOJI}'").is_identity is False


def test_an_offset_inside_a_surrogate_pair_rounds_down():
    """The trailing half is not a position any Python index can name."""
    text = f"ab{EMOJI}cd"
    offsets = Utf16Offsets(text)
    lead = reference_utf16_offset(text, 2)
    assert offsets.to_codepoint(lead) == 2
    assert offsets.to_codepoint(lead + 1) == 2
    assert offsets.to_codepoint(lead + 2) == 3


def test_a_span_is_translated_by_its_end_not_by_its_width():
    """An astral character *inside* a span shrinks it, leaving the start put."""
    text = f"ab{EMOJI}cd"
    # UTF-16: the four characters from index 1 span five code units.
    assert Utf16Offsets(text).span_to_codepoints(1, 5) == (1, 4)


def test_the_public_helpers_round_trip():
    text = f"let e='{EMOJI}'; T | where X > 1"
    assert utf16_to_codepoint(text, 16) == 15
    assert codepoint_to_utf16(text, 15) == 16


def test_the_helper_answers_the_documented_example():
    """The case from the issue: slicing at a raw ``TextStart`` is off by one."""
    query = f"let e=\"{EMOJI}\"; T | where X > 1"
    token = next(t for t in parse(query).syntax.GetTokens() if t.Text == "where")
    assert query[token.TextStart:][:5] != "where"
    start = utf16_to_codepoint(query, token.TextStart)
    assert query[start:][:5] == "where"


# -- the surfaces that report offsets -------------------------------------


def test_replace_table_rewrites_the_right_characters():
    query = f'let e="{EMOJI}"; T | where X > 1'
    assert parse(query).replace_table("T", "Z") == f'let e="{EMOJI}"; Z | where X > 1'


def test_replace_table_is_unchanged_without_an_astral_character():
    query = 'let e="ok"; T | where X > 1'
    assert parse(query).replace_table("T", "Z") == 'let e="ok"; Z | where X > 1'


def test_an_ir_span_slices_its_own_construct():
    query = f'let e="{EMOJI}"; T | where X > 1'
    source = parse(query).to_ir(attach_schema=False).main_pipeline.source
    assert source.span.text(query) == "T"


def test_every_ir_span_slices_something_the_query_contains():
    """No span may run off the end or land mid-construct after translation."""
    from kustology.ir import find_all
    from kustology.ir.spans import Span

    query = f'let e="{EMOJI}"; T | where X > 1 | summarize c = count() by Y'
    ir = parse(query).to_ir(attach_schema=False)
    spans = list(find_all(ir, Span))
    assert spans, "expected the IR to carry spans"
    for span in spans:
        assert span.text_start + span.width <= len(query)


def test_find_time_expressions_offsets_slice_their_own_text():
    query = f'let e="{EMOJI}"; T | where t > ago(1h)'
    found = parse(query).find_time_expressions()
    assert found, "expected ago(1h) to be found"
    for text, start, length in found:
        assert query[start:start + length] == text


def test_diagnostic_offsets_slice_the_name_they_complain_about():
    query = f'let e="{EMOJI}"; NoSuchTable | count'
    diags = [d for d in validate(query, schema={"T": {"a": "long"}}) if d["code"] == "KS204"]
    assert len(diags) == 1
    d = diags[0]
    assert query[d["start"]:d["start"] + d["length"]] == "NoSuchTable"


def test_the_diagnostics_property_agrees_with_validate():
    query = f'let e="{EMOJI}"; NoSuchTable | count'
    schema = {"T": {"a": "long"}}
    assert parse(query, schema=schema).diagnostics == validate(query, schema=schema)


def test_bmp_offsets_are_microsofts_own():
    """The fast path must not move anything for a query without astral text."""
    query = 'let e="ok"; NoSuchTable | count'
    diags = [d for d in validate(query, schema={"T": {"a": "long"}}) if d["code"] == "KS204"]
    assert diags[0]["start"] == query.index("NoSuchTable")
    source = parse(query).to_ir(attach_schema=False).main_pipeline.source
    assert source.span.text(query) == "NoSuchTable"
