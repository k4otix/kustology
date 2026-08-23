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


def test_validate_ignore_unknown_tables_stays_narrower_than_the_ir_filter():
    """One code here, twelve on the schemaless IR path — deliberately.

    ``validate`` only reaches the binder when the caller passed a schema, so
    the caller owns every name in the query and is waiving exactly one
    dimension of that: tables their schema does not cover. An unknown
    *function* is still an error they need to see. The IR's schemaless build
    is the opposite case — globals the caller never chose, describing
    nothing — so it waives the whole unknown-name family
    (``services._UNKNOWN_NAME_CODES``).

    Pinned so the two are not "unified" into whichever is nearest to hand.
    """
    from kustology.services import _UNKNOWN_NAME_CODES

    schema = {"Known": {"x": "string"}}
    diagnostics = validate(
        "_Im_WebSession() | take 1", schema=schema, ignore_unknown_tables=True,
    )
    assert [d["code"] for d in diagnostics] == ["KS211"]
    assert "KS211" in _UNKNOWN_NAME_CODES


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
    from kustology import bridge as bridge_module
    from kustology import core as core_module
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

    # Patch every name a re-parse could reach the parser through, not just
    # `services`: `bridge` is where `KustoCode` comes from and `core` binds
    # its own reference to it at import, so an implementation calling
    # `KustoCode.Parse(self.text)` directly would otherwise slip past this
    # guard with the test still green.
    monkeypatch.setattr(services_module, "KustoCode", _NoParse)
    monkeypatch.setattr(bridge_module, "KustoCode", _NoParse)
    monkeypatch.setattr(core_module, "KustoCode", _NoParse)
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
    every caller's typo down to a single report.
    """
    with pytest.warns(RuntimeWarning) as record:
        parse("T | count", schema={"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__
    assert "not_a_real_type" in str(record[0].message)


def test_unknown_scalar_type_warning_is_attributed_to_the_caller_of_validate():
    """``validate`` reaches the helper through a different call chain, and the
    computed attribution serves both without either being counted by hand."""
    with pytest.warns(RuntimeWarning) as record:
        validate("T | count", schema={"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__


def test_unknown_scalar_type_warning_is_attributed_to_the_caller_of_build_global_state():
    """``build_global_state`` is public and one frame shallower than ``parse``.

    A single hardcoded ``stacklevel`` cannot be right for both, and the one
    tuned for ``parse`` overshot here — the warning was attributed a frame
    *past* the caller, which for a module-level call is the interpreter
    (``sys:1``). Computing the depth fixes both at once.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.warns(RuntimeWarning) as record:
        build_global_state({"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__


def test_unknown_scalar_type_warning_survives_extra_library_frames():
    """The depth must not depend on how many frames the library happens to use.

    PEP 709 inlined list/dict/set comprehensions in **3.12**; on 3.10 and 3.11
    — both inside this project's ``requires-python`` — the two comprehensions
    in the ``build_global_state`` → ``_build_table_symbol`` chain each push a
    frame, so the caller sits seven frames up rather than five and any
    hardcoded number is attributed back into ``schema_state.py`` on two of the
    CI legs.

    A **generator expression** pushes a frame on every version, so evaluating
    the call through one whose code object is compiled with a filename inside
    the package reproduces that deeper stack here, on 3.12. Two extra
    in-package frames sit between this test and the resolver; the attribution
    must be unchanged.
    """
    import os

    from kustology.utils import schema_state
    from kustology.utils.schema_state import build_global_state

    in_package = os.path.join(os.path.dirname(schema_state.__file__), "_deeper.py")
    deeper = compile(
        "out = list(build(s) for s in schemas)", in_package, "exec"
    )
    with pytest.warns(RuntimeWarning) as record:
        exec(  # noqa: S102 — the code object is compiled here, from a literal
            deeper,
            {"build": build_global_state, "schemas": [{"T": {"x": "not_a_real_type"}}]},
        )

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


def test_scalar_type_names_resolve_regardless_of_case():
    """`{"T": {"c": "LONG"}}` typed the column `string`, with a warning.

    Microsoft's `ScalarTypes.GetSymbol` is a dictionary lookup on the exact
    lower-case spelling, so every capitalisation a caller might reasonably
    write — `LONG`, `Long`, `DateTime`, `Real` — missed, fell through to the
    unknown-type branch and silently produced a `string` column. A schema
    hand-written from a portal column list or lifted from a `.csl` file is
    full of them, and the only symptom is a binder that resolves the wrong
    type.

    KQL scalar type names and their aliases (`int64`, `datetime`, `boolean`,
    …) are lower-case throughout the grammar, so case-folding the lookup key
    cannot collide with a real name.
    """
    from kustology.utils.schema_state import (
        build_global_state,
        extract_schemas_from_global_state,
    )

    schema = {"T": {"a": "LONG", "b": "DateTime", "c": "Real", "d": "BOOLEAN"}}
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        state = build_global_state(schema)

    # Read back off the real .NET ColumnSymbols: `bool` is the alias's
    # resolved name, and none of the four is the old `string` fallback.
    assert extract_schemas_from_global_state(state) == {
        "T": {"a": "long", "b": "datetime", "c": "real", "d": "bool"},
    }

    # The lower-case spelling was never broken; it must still resolve to the
    # identical symbol rather than through some new normalization path.
    assert extract_schemas_from_global_state(
        build_global_state({"T": {"a": "long"}})
    ) == {"T": {"a": "long"}}


def test_a_non_string_scalar_type_raises_type_error_not_a_clr_exception():
    """`{"T": {"c": None}}` reached `ScalarTypes.GetSymbol(None)` and came
    back as a raw `System.ArgumentNullException` with a .NET stack trace
    through `System.Collections.Generic.Dictionary` — a CLR type with no
    Python class to catch by name, and a message that says nothing about
    schemas.

    A non-`str` type name is the caller's mistake in their own dict, so it
    is a `TypeError`, raised before the CLR boundary and worded like the
    other two schema-shape errors this module raises.
    """
    from kustology.utils.schema_state import build_global_state

    for bad in (None, 5, ["long"], {"type": "long"}):
        with pytest.raises(TypeError) as exc_info:
            build_global_state({"T": {"c": bad}})
        message = str(exc_info.value)
        assert type(bad).__name__ in message, message
        assert "str" in message, message
        # Not the CLR's words.
        assert "ArgumentNullException" not in message
        assert "Kusto.Language" not in message


def test_a_non_string_scalar_type_error_is_distinct_from_the_two_shape_errors():
    """Three positions in a schema can be the wrong type, and each names its
    own position: the schema itself, a table's value, and a column's type.

    Conflating them was the trap the top-level string form fell into — one
    message that could mean any of three mistakes sends the reader looking in
    the wrong place.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.raises(TypeError) as top_level:
        build_global_state("(a:string)")
    with pytest.raises(TypeError) as table_value:
        build_global_state({"T": 5})
    with pytest.raises(TypeError) as column_type:
        build_global_state({"T": {"c": 5}})

    assert "schema must be a dict" in str(top_level.value)
    assert "table 'T'" in str(table_value.value)
    assert "column 'c'" in str(column_type.value)
    assert len({str(e.value) for e in (top_level, table_value, column_type)}) == 3


def test_the_schema_string_form_warns_about_a_column_it_could_not_type():
    """`{"T": "(n:bogus)"}` was silent; `{"T": {"n": "bogus"}}` warned.

    Two spellings of one schema, one of them with a typo in it, and only one
    of them said so. Microsoft's schema-string parser does not reject an
    unrecognized type name — it types the column `unknown`, which binds
    without an error and resolves nothing — so the typo reached the binder
    and the caller saw only columns that would not resolve.
    """
    from kustology.utils.schema_state import (
        build_global_state,
        extract_schemas_from_global_state,
    )

    with pytest.warns(RuntimeWarning) as record:
        state = build_global_state({"T": "(a:long, n:bogus)"})

    assert record[0].filename == __file__, "must blame the caller, not the library"
    assert "'n'" in str(record[0].message)
    assert "'T'" in str(record[0].message)

    # Microsoft's answer is kept: `unknown` is what its parser decided, and
    # substituting `string` here would invent a type the caller never wrote.
    assert extract_schemas_from_global_state(state) == {
        "T": {"a": "long", "n": "unknown"},
    }


def test_an_untyped_column_in_a_schema_string_warns_the_same_way():
    """`"(a)"` is not the documented `"(col:type, ...)"` form, and Microsoft
    types the column `unknown` rather than rejecting it.

    The list form `{"T": ["a"]}` is the documented way to say "untyped", and
    it means `string`; a bare name inside a schema string means neither, so
    it gets the same warning the typo does.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.warns(RuntimeWarning, match="'a'"):
        build_global_state({"T": "(a)"})

    # The control: the documented untyped form is silent and means `string`.
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        build_global_state({"T": ["a"]})


def test_a_valid_schema_string_warns_about_nothing():
    """The guard must not fire on the form the docstring advertises."""
    from kustology.utils.schema_state import build_global_state

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        state = build_global_state({"T": "(x:long, y:string, z:dynamic)"})
    assert state.Database.Tables[0].Columns.Count == 3


def test_dict_keys_are_raw_column_names_and_the_docstring_says_so():
    """`{"T": {"['my col']": "string"}}` builds a column whose *name* is the
    ten characters `['my col']`, brackets and quotes included.

    The bracket-quoting is KQL *query* syntax for referring to a name that
    is not a bare identifier; a schema dict key is not query text, it is the
    name itself, so quoting it produces a column nothing can reference. The
    behaviour is Microsoft's `ColumnSymbol(name, type)` and is correct — a
    column really can be named anything — so this is a documentation fix,
    and the docstring is what the test pins alongside it.
    """
    from kustology.utils.schema_state import (
        build_global_state,
        extract_schemas_from_global_state,
    )

    state = build_global_state({"T": {"['my col']": "string", "my col": "long"}})
    assert extract_schemas_from_global_state(state) == {
        "T": {"['my col']": "string", "my col": "long"},
    }

    doc = build_global_state.__doc__.lower()
    assert "raw name" in doc and "bracket-quoting" in doc, doc


def test_an_empty_schema_string_raises_value_error_not_a_clr_exception():
    """`{"T": ""}` was the last raw CLR escape in this module.

    `TableSymbol.From("")` raises `System.InvalidOperationException: Invalid
    schema:` with a .NET stack trace. It is neither a `TypeError` nor a
    `ValueError`, so a caller can only catch it with a bare `except
    Exception` and cannot name the type without importing from the CLR — the
    same defect that made `{"T": {"c": None}}` unusable, one function away.

    The blast radius is exactly the empty and whitespace-only strings.
    Microsoft's schema parser is otherwise permissive to a fault and accepts
    every malformed string tried below without complaint, so nothing else
    changes shape here.
    """
    from kustology.utils.schema_state import build_global_state

    for empty in ("", "   ", "\t\n"):
        with pytest.raises(ValueError) as exc_info:
            build_global_state({"T": empty})
        message = str(exc_info.value)
        assert "'T'" in message, message
        assert "(col:type, ...)" in message, message
        assert "InvalidOperationException" not in message
        assert "Kusto.Language" not in message

    # It reaches the public entry point as a `ValueError` too.
    with pytest.raises(ValueError):
        parse("T | take 1", schema={"T": ""})

    # The control: everything else Microsoft accepts, it still accepts. The
    # guard must not become a hand-rolled schema-string validator.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for permissive in ("(", ")", "(a:)", "(a long)", "(a:long", "a:long", "junk"):
            build_global_state({"T": permissive})


def test_a_non_string_table_or_column_name_raises_type_error():
    """Names had the defect the *type* position just had fixed.

    `{"T": {5: "long"}}` came back as pythonnet's "No method matches given
    arguments for ColumnSymbol..ctor" — the same unnameable, schema-silent
    wording this module's own docstring criticises `GetSymbol(5)` for. Keys
    become a symbol's `Name` verbatim, so a key that is not a `str` cannot
    be one.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.raises(TypeError) as table_name:
        build_global_state({5: {"c": "long"}})
    with pytest.raises(TypeError) as column_name:
        build_global_state({"T": {5: "long"}})
    with pytest.raises(TypeError) as list_column_name:
        build_global_state({"T": [5]})

    for exc in (table_name, column_name, list_column_name):
        assert "No method matches" not in str(exc.value), str(exc.value)
        assert "int" in str(exc.value), str(exc.value)
    assert "table name" in str(table_name.value)
    assert "column name" in str(column_name.value)
    assert "'T'" in str(column_name.value)
    assert "'T'" in str(list_column_name.value)


def test_the_two_forms_agree_on_a_deliberate_unknown_type():
    """`"unknown"` is Microsoft's own name for "no type", so writing it is
    not a typo — but `ScalarTypes.GetSymbol("unknown")` returns `None`.

    So the dict form scolded the caller for a real type name and handed back
    `string`, while `{"T": "(c:unknown)"}` accepted it and kept `unknown`.
    Two spellings of one schema, disagreeing about both the warning and the
    resulting type. It is also what `extract_schemas_from_global_state`
    emits for a column the binder could not type, so round-tripping its
    output through `build_global_state` silently retyped those columns.
    """
    from kustology.utils.schema_state import (
        build_global_state,
        extract_schemas_from_global_state,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        from_dict = build_global_state({"T": {"c": "unknown", "d": "long"}})

    assert extract_schemas_from_global_state(from_dict) == {
        "T": {"c": "unknown", "d": "long"},
    }

    # The schema-string form has always kept it; the two now agree, so the
    # extractor's output round-trips unchanged.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        from_string = build_global_state({"T": "(c:unknown, d:long)"})
    assert extract_schemas_from_global_state(from_string) == extract_schemas_from_global_state(
        from_dict
    )

    # The control: a genuine typo still warns and still falls back.
    with pytest.warns(RuntimeWarning, match="bogus"):
        typo = build_global_state({"T": {"c": "bogus"}})
    assert extract_schemas_from_global_state(typo) == {"T": {"c": "string"}}


def test_every_untyped_schema_string_column_is_attributed_to_the_caller():
    """One warning per column, and the stack is walked once for all of them.

    `_caller_stacklevel()` was called inside the loop, re-walking the whole
    stack for every column. The depth cannot differ between iterations — it
    is the same frame — so hoisting it is free, and this pins that hoisting
    did not cost the attribution the walk exists to provide.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.warns(RuntimeWarning) as record:
        build_global_state({"T": "(a:bogus, b:alsobogus, c)"})

    assert len(record) == 3, [str(w.message) for w in record]
    assert {w.filename for w in record} == {__file__}
    assert {w.lineno for w in record} == {record[0].lineno}
