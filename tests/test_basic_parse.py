# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import warnings

import pytest

from kustology import format_query, parse, validate


@pytest.mark.parametrize(
    "query, expected_kind",
    [
        ("StormEvents | count", "PipeExpression"),
        ("print x = 1", "PrintOperator"),
        ("let x = 5; x", "LetStatement"),
    ],
)
def test_kql_node_types(query, expected_kind):
    statement = parse(query).syntax.Statements[0].Element
    actual = str(statement.Kind)
    if actual == "ExpressionStatement":
        actual = str(statement.Expression.Kind)
    assert actual == expected_kind


def test_format_query_basic():
    formatted = format_query("SecurityEvent|where EventID==4624")
    assert "| where" in formatted
    assert "==" in formatted


def test_validate_returns_parser_diagnostics_with_codes():
    """Pin behavior to diagnostic codes, not human-readable message text."""
    diagnostics = validate("SecurityEvent | where EventID == ")
    assert diagnostics
    assert all(isinstance(d["code"], str) and d["code"] for d in diagnostics)


def test_validate_with_schema_surfaces_semantic_diagnostics():
    schema = {"SecurityEvent": {"Account": "string"}}
    diagnostics = validate("SecurityEvent | where NoSuchCol == 1", schema=schema)
    messages = [d["message"] for d in diagnostics]
    assert any("NoSuchCol" in m for m in messages), messages


def test_validate_ignore_unknown_tables_filters_by_code():
    """ignore_unknown_tables drops KS204 diagnostics — the code, not a message match."""
    schema = {"Known": {"x": "string"}}
    diagnostics = validate("Unknown | where x == 1", schema=schema, ignore_unknown_tables=True)
    assert all(d["code"] != "KS204" for d in diagnostics)


def test_validate_unknown_table_default_emits_ks204():
    schema = {"Known": {"x": "string"}}
    diagnostics = validate("Unknown | count", schema=schema)
    assert any(d["code"] == "KS204" for d in diagnostics)


def test_diagnostics_matches_validate_on_an_unbound_parse():
    """``KustoQuery.diagnostics`` is ``validate()``'s answer for a query you
    already hold, so a caller that parsed once does not have to hand the text
    back to a function that parses it again."""
    query = "SecurityEvent | where EventID == "
    diagnostics = parse(query).diagnostics
    assert diagnostics == validate(query)
    # Pin a real value, not just the equality: an empty list would satisfy
    # the comparison above if both sides broke the same way.
    assert [d["code"] for d in diagnostics] == ["KS006"]
    assert diagnostics[0]["severity"] == "Error"
    assert diagnostics[0]["start"] == 32


def test_diagnostics_matches_validate_on_a_bound_parse():
    """A bound parse carries the binder's semantic diagnostics too."""
    schema = {"SecurityEvent": {"Account": "string"}}
    query = "SecurityEvent | where NoSuchCol == 1"
    bound = parse(query, schema=schema)
    assert bound.has_semantics
    diagnostics = bound.diagnostics
    assert diagnostics == validate(query, schema=schema)
    assert [d["code"] for d in diagnostics] == ["KS142"]
    assert "NoSuchCol" in diagnostics[0]["message"]


def test_diagnostics_does_not_reparse(monkeypatch):
    """The whole point of the property: read the diagnostics off the
    ``KustoCode`` we already have. Routing through ``validate()`` would parse
    the text a second time, and on a bound query would silently throw the
    binder's work away — the same seam ``tests/test_to_ir_seam.py`` guards
    for ``to_ir()``."""
    from kustology import services as services_module

    query = parse("Unknown | count", schema={"Known": {"x": "string"}})
    # Taken before the patch: an *unbound* parse of the same text reports
    # nothing, so the KS204 asserted below can only have come from the bound
    # code the property was handed.
    assert parse("Unknown | count").diagnostics == []
    calls = []

    class _NoParse:
        @staticmethod
        def Parse(text):
            calls.append(("Parse", text))
            raise AssertionError("diagnostics re-parsed the query text")

        @staticmethod
        def ParseAndAnalyze(text, state):
            calls.append(("ParseAndAnalyze", text))
            raise AssertionError("diagnostics re-parsed the query text")

    monkeypatch.setattr(services_module, "KustoCode", _NoParse)
    diagnostics = query.diagnostics

    assert calls == []
    assert [d["code"] for d in diagnostics] == ["KS204"]


def test_get_referenced_tables_syntactic():
    query = "SecurityEvent | where EventID == 4624 | join kind=inner (SigninLogs) on Account"
    tables = parse(query).get_referenced_tables()
    assert tables == {"SecurityEvent", "SigninLogs"}


def test_parse_with_dict_schema():
    schema = {"MyCustomTable": {"Col1": "string"}}
    bound = parse("MyCustomTable | count", schema=schema)
    assert bound.has_semantics
    assert bound.get_referenced_tables() == {"MyCustomTable"}


def test_parse_with_kusto_schema_string():
    """Single-table schema string form: '(col:type, ...)'."""
    bound = parse("MyT | where x == 1", schema={"MyT": "(x:long, y:string)"})
    assert bound.has_semantics
    assert bound.get_referenced_tables() == {"MyT"}


def test_parse_with_legacy_list_schema():
    """Backwards-compatible with the untyped list form (treated as string columns)."""
    bound = parse("MyT | count", schema={"MyT": ["Col1"]})
    assert bound.get_referenced_tables() == {"MyT"}


def test_unknown_scalar_type_falls_back_with_warning():
    schema = {"T": {"x": "not_a_real_type"}}
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        parse("T | count", schema=schema)
    assert any(
        issubclass(w.category, RuntimeWarning) and "not_a_real_type" in str(w.message)
        for w in captured
    )


def test_unknown_scalar_type_warning_is_attributed_to_the_caller():
    """The warning must point at the line that wrote the bad type.

    ``stacklevel=3`` landed on ``build_global_state`` — inside
    ``kustology/utils/schema_state.py`` — so the warning's location was a
    library file the caller does not own, `-W error::RuntimeWarning` blamed
    the wrong module, and the default "once per location" filter deduplicated
    every caller's typo down to a single report. Five frames up from
    ``_resolve_scalar_type`` is ``_build_table_symbol`` →
    ``build_global_state`` → ``parse`` → here.
    """
    with pytest.warns(RuntimeWarning) as record:
        parse("T | count", schema={"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__
    assert "not_a_real_type" in str(record[0].message)


def test_unknown_scalar_type_warning_is_attributed_to_the_caller_of_validate():
    """``validate`` reaches the same helper through the same number of frames,
    so the one ``stacklevel`` serves both entry points."""
    with pytest.warns(RuntimeWarning) as record:
        validate("T | count", schema={"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__


def test_package_namespace_does_not_leak_its_version_lookup_imports():
    """``kustology.PackageNotFoundError`` was importable and meant nothing.

    The name is an implementation detail of reading ``__version__`` from the
    installed metadata; exported by accident it reads as part of this
    library's API, shows up in ``dir()`` and in generated documentation, and
    invites `from kustology import PackageNotFoundError`. ``__all__`` never
    listed it, which is exactly why nothing caught it.
    """
    import kustology

    assert "PackageNotFoundError" not in dir(kustology)
    # The control: `__version__`, the thing the import exists to compute, is
    # still there and still a version string.
    assert kustology.__version__
    assert kustology.__version__[0].isdigit()


def test_top_level_schema_string_is_rejected_naming_the_dict_form():
    """`parse(q, schema="(a:string)")` was documented and typed as supported
    and has never worked: `build_global_state` requires a mapping. The type
    alias and the docstring both said otherwise, so the only way to find out
    was the `TypeError`. Pin the raise *and* that the message points at the
    form that does work — the per-table string `{"T": "(a:string)"}`."""
    with pytest.raises(TypeError) as exc_info:
        parse("T | count", schema="(a:string)")
    message = str(exc_info.value)
    assert "dict" in message
    assert "str" in message

    # The form the message points at parses fine, which is what makes the
    # rejection a documentation bug rather than a missing feature.
    assert parse("T | count", schema={"T": "(a:string)"}).has_semantics


def test_schema_like_alias_no_longer_advertises_a_bare_string():
    """`SchemaLike` is the public annotation on `parse` and `validate`. It
    included `str`, so a type checker green-lit the call that raises."""
    import types
    import typing

    from kustology.services import SchemaLike, validate
    from kustology.services import parse as parse_service

    assert set(typing.get_args(SchemaLike)) == {dict, types.NoneType}
    assert typing.get_type_hints(parse_service)["schema"] == SchemaLike
    assert typing.get_type_hints(validate)["schema"] == SchemaLike
    for fn in (parse_service, validate):
        assert "schema string" not in fn.__doc__, fn.__name__
