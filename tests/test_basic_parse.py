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
    """Pin diagnostic codes: message text is prose and can change."""
    diagnostics = validate("SecurityEvent | where EventID == ")
    assert diagnostics
    assert all(isinstance(d["code"], str) and d["code"] for d in diagnostics)


def test_validate_with_schema_surfaces_semantic_diagnostics():
    schema = {"SecurityEvent": {"Account": "string"}}
    diagnostics = validate("SecurityEvent | where NoSuchCol == 1", schema=schema)
    messages = [d["message"] for d in diagnostics]
    assert any("NoSuchCol" in m for m in messages), messages


def test_validate_ignore_unknown_tables_filters_by_code():
    """ignore_unknown_tables drops KS204 diagnostics by code."""
    schema = {"Known": {"x": "string"}}
    diagnostics = validate("Unknown | where x == 1", schema=schema, ignore_unknown_tables=True)
    assert all(d["code"] != "KS204" for d in diagnostics)


def test_validate_unknown_table_default_emits_ks204():
    schema = {"Known": {"x": "string"}}
    diagnostics = validate("Unknown | count", schema=schema)
    assert any(d["code"] == "KS204" for d in diagnostics)


def test_validate_ignore_unknown_tables_stays_narrower_than_the_ir_filter():
    """The two unknown-name waivers are scoped differently, and stay that way.

    ``validate`` reaches the binder only when the caller passes a schema, so
    the caller owns every name in the query and waives one dimension of it:
    tables their schema does not cover. An unknown *function* is still an
    error they need to see. The IR's schemaless build has no caller-chosen
    globals at all, so it waives the whole unknown-name family
    (``services._UNKNOWN_NAME_CODES``).
    """
    from kustology.services import _UNKNOWN_NAME_CODES

    schema = {"Known": {"x": "string"}}
    diagnostics = validate(
        "_Im_WebSession() | take 1", schema=schema, ignore_unknown_tables=True,
    )
    assert [d["code"] for d in diagnostics] == ["KS211"]
    assert "KS211" in _UNKNOWN_NAME_CODES


def test_diagnostics_matches_validate_on_an_unbound_parse():
    """``KustoQuery.diagnostics`` gives ``validate()``'s answer for a query you
    already hold, without handing the text back to be parsed again."""
    query = "SecurityEvent | where EventID == "
    diagnostics = parse(query).diagnostics
    assert diagnostics == validate(query)
    # An empty list satisfies the equality above if both sides break the same
    # way, so pin real values too.
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
    """The property reads diagnostics off the ``KustoCode`` it already holds.
    Routing through ``validate()`` would parse the text a second time and
    discard the binder's work on a bound query. ``tests/test_to_ir_seam.py``
    guards the same seam for ``to_ir()``."""
    from kustology import bridge as bridge_module
    from kustology import core as core_module
    from kustology import services as services_module

    query = parse("Unknown | count", schema={"Known": {"x": "string"}})
    # Runs before the patch: an unbound parse reports nothing, so the KS204
    # below can only come from the bound code the property holds.
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

    # `bridge` is where `KustoCode` comes from and `core` binds its own
    # reference at import, so patching only `services` would let a direct
    # `KustoCode.Parse(self.text)` slip past this guard, still green.
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
    """The untyped list form types every column ``string``."""
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

    A hardcoded ``stacklevel=3`` lands on ``build_global_state``, inside
    ``kustology/utils/schema_state.py``, so `-W error::RuntimeWarning` blames
    the library and the default "once per location" filter collapses every
    caller's typo into a single report.
    """
    with pytest.warns(RuntimeWarning) as record:
        parse("T | count", schema={"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__
    assert "not_a_real_type" in str(record[0].message)


def test_unknown_scalar_type_warning_is_attributed_to_the_caller_of_validate():
    """``validate`` reaches the helper through a different call chain; the
    computed depth serves that chain too."""
    with pytest.warns(RuntimeWarning) as record:
        validate("T | count", schema={"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__


def test_unknown_scalar_type_warning_is_attributed_to_the_caller_of_build_global_state():
    """``build_global_state`` is public and one frame shallower than ``parse``.

    A ``stacklevel`` tuned for ``parse`` overshoots here and attributes the
    warning a frame *past* the caller, which for a module-level call is the
    interpreter (``sys:1``). Computing the depth serves both call sites.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.warns(RuntimeWarning) as record:
        build_global_state({"T": {"x": "not_a_real_type"}})

    assert record[0].filename == __file__


def test_unknown_scalar_type_warning_survives_extra_library_frames():
    """The depth must not depend on how many frames the library happens to use.

    PEP 709 inlines comprehensions from 3.12 on. Under 3.10 and 3.11, both
    inside this project's ``requires-python``, the two comprehensions in the
    ``build_global_state`` → ``_build_table_symbol`` chain each push a frame,
    putting the caller seven frames up instead of five, so a hardcoded number
    is attributed back into ``schema_state.py`` on those CI legs. A generator
    expression pushes a frame on every version, so evaluating the call
    through one compiled with an in-package filename reproduces that deeper
    stack here on 3.12: two extra in-package frames, and the same attribution.
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
    """The package namespace exports nothing beyond ``__version__`` itself.

    ``__version__`` is a plain literal in ``_version.py``. A leaked
    version-lookup helper, such as an ``importlib.metadata`` exception type,
    would show up in ``dir()`` and in generated documentation as part of this
    library's public API.
    """
    import kustology

    assert "PackageNotFoundError" not in dir(kustology)
    # The control: `__version__` is present and is a version string.
    assert kustology.__version__
    assert kustology.__version__[0].isdigit()


def test_top_level_schema_string_is_rejected_naming_the_dict_form():
    """`build_global_state` requires a mapping, and a bare
    `schema="(a:string)"` has no table name to attach columns to. Pin the
    raise and that the message names the per-table form `{"T": "(a:string)"}`."""
    with pytest.raises(TypeError) as exc_info:
        parse("T | count", schema="(a:string)")
    message = str(exc_info.value)
    assert "dict" in message
    assert "str" in message

    # The form the message names parses fine.
    assert parse("T | count", schema={"T": "(a:string)"}).has_semantics


def test_schema_like_alias_no_longer_advertises_a_bare_string():
    """`SchemaLike` annotates `schema` on `parse` and `validate`; admitting
    `str` would let a type checker green-light a call that always raises."""
    import types
    import typing

    from kustology.services import SchemaLike, validate
    from kustology.services import parse as parse_service

    assert set(typing.get_args(SchemaLike)) == {dict, types.NoneType}
    assert typing.get_type_hints(parse_service)["schema"] == SchemaLike
    assert typing.get_type_hints(validate)["schema"] == SchemaLike


def test_scalar_type_names_resolve_regardless_of_case():
    """`{"T": {"c": "LONG"}}` must resolve to the `long` column type.

    Microsoft's `ScalarTypes.GetSymbol` is a dictionary lookup on the exact
    lower-case spelling, so `LONG`, `Long`, `DateTime` or `Real` misses,
    falls through to the unknown-type branch, and silently produces a
    `string` column. A schema hand-written from a portal column list or
    lifted from a `.csl` file is full of those spellings, and the only
    symptom is a binder that resolves the wrong type.

    KQL scalar type names and their aliases (`int64`, `datetime`, `boolean`)
    are lower-case throughout the grammar, so case-folding the lookup key
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
    # resolved name, and none of the four falls back to `string`.
    assert extract_schemas_from_global_state(state) == {
        "T": {"a": "long", "b": "datetime", "c": "real", "d": "bool"},
    }

    # The lower-case spelling resolves to the identical symbol.
    assert extract_schemas_from_global_state(
        build_global_state({"T": {"a": "long"}})
    ) == {"T": {"a": "long"}}


def test_a_non_string_scalar_type_raises_type_error_not_a_clr_exception():
    """Unguarded, `{"T": {"c": None}}` reaches `ScalarTypes.GetSymbol(None)`
    and comes back as a raw `System.ArgumentNullException`: a CLR type with no
    Python class to catch by name, and a message that says nothing about
    schemas.

    A non-`str` type name is the caller's mistake in their own dict, so it
    raises `TypeError` before the CLR boundary, worded like the other two
    schema-shape errors this module raises.
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
    """Each wrong-type position names itself: the schema, a table's value,
    and a column's type. One shared message would send the reader looking in
    the wrong place."""
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
    """`{"T": "(n:bogus)"}` and `{"T": {"n": "bogus"}}` are two spellings of
    one schema, and a typo in either must warn.

    Microsoft's schema-string parser types an unrecognized name `unknown`
    instead of rejecting it, which binds without an error and resolves
    nothing, so unguarded the caller sees only columns that do not resolve.
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
    types the column `unknown` instead of rejecting it. The documented way to
    say "untyped" is the list form `{"T": ["a"]}`, which means `string`, so a
    bare name inside a schema string gets the same warning a typo does."""
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


def test_dict_keys_are_raw_column_names_not_bracket_quoted_query_syntax():
    """`{"T": {"['my col']": "string"}}` builds a column whose *name* keeps
    the brackets and quotes.

    Bracket-quoting is KQL *query* syntax for a name that is not a bare
    identifier. A schema dict key is the name itself, so quoting it produces
    a column nothing can reference. Microsoft's `ColumnSymbol(name, type)` is
    right to accept it: a column really can be named anything.
    """
    from kustology.utils.schema_state import (
        build_global_state,
        extract_schemas_from_global_state,
    )

    state = build_global_state({"T": {"['my col']": "string", "my col": "long"}})
    assert extract_schemas_from_global_state(state) == {
        "T": {"['my col']": "string", "my col": "long"},
    }


def test_an_empty_schema_string_raises_value_error_not_a_clr_exception():
    """Unguarded, `{"T": ""}` reaches `TableSymbol.From("")`, which raises
    `System.InvalidOperationException: Invalid schema:`. That is neither a
    `TypeError` nor a `ValueError`, so a caller can catch it only with a bare
    `except Exception` and cannot name the type without importing from the
    CLR.

    The guard covers exactly the empty and whitespace-only strings.
    Microsoft's schema parser accepts every other malformed string tried
    below, so nothing else changes shape.
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

    # The control: everything else Microsoft accepts still passes, so the
    # guard cannot grow into a hand-rolled schema-string validator.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for permissive in ("(", ")", "(a:)", "(a long)", "(a:long", "a:long", "junk"):
            build_global_state({"T": permissive})


def test_a_non_string_table_or_column_name_raises_type_error():
    """A non-`str` name carries the same defect a non-`str` type does.

    Unguarded, `{"T": {5: "long"}}` reaches pythonnet as "No method matches
    given arguments for ColumnSymbol..ctor", which names no schema and no
    catchable Python type. Keys become a symbol's `Name` verbatim, so a key
    that is not a `str` cannot be one.
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
    """`"unknown"` is Microsoft's own name for "no type", yet
    `ScalarTypes.GetSymbol("unknown")` returns `None`.

    Unguarded, the dict form warns about a real type name and hands back
    `string` while `{"T": "(c:unknown)"}` keeps `unknown`, so two spellings
    of one schema disagree about both the warning and the resulting type.
    `extract_schemas_from_global_state` also emits `unknown` for a column the
    binder could not type, so round-tripping its output through
    `build_global_state` must not retype those columns.
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

    # The schema-string form keeps `unknown` too, so the two agree and the
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

    The depth is the same frame on every iteration, so `_caller_stacklevel()`
    is hoisted out of the loop. This pins that the hoisting keeps the
    attribution the walk exists to provide.
    """
    from kustology.utils.schema_state import build_global_state

    with pytest.warns(RuntimeWarning) as record:
        build_global_state({"T": "(a:bogus, b:alsobogus, c)"})

    assert len(record) == 3, [str(w.message) for w in record]
    assert {w.filename for w in record} == {__file__}
    assert {w.lineno for w in record} == {record[0].lineno}
