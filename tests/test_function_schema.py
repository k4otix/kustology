# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Coverage for ``FunctionSchema``, the schema-dict entry that declares a function.

A KQL query that calls a workspace function (the ASIM parsers are the common
case) binds against nothing without a declaration for it, so every column the
call produces reads as an unknown name. Each case here binds a real query
through ``parse(query, schema=...)`` and reads what Microsoft's binder resolved.
"""

import logging
import warnings

import pytest

from kustology import FunctionSchema, parse
from kustology.utils.analysis import collect_nodes
from kustology.utils.schema_state import (
    build_global_state,
    extract_schemas_from_global_state,
)

ASIM = {
    "imProcessCreate": FunctionSchema(
        parameters=(("starttime", "datetime"), ("endtime", "datetime")),
        returns="(TimeGenerated:datetime, ActorUsername:string)",
        required=0,
    ),
}
IDIOM = (
    "imProcessCreate(starttime=ago(1h), endtime=now()) "
    "| where isnotempty(ActorUsername)"
)


def _call_result_type(query) -> str:
    """Return the bound result type name of the first function call in ``query``."""
    calls = collect_nodes(query.syntax, lambda n: str(n.Kind) == "FunctionCallExpression")
    return str(calls[0].ResultType.Name)


def test_a_closed_tabular_return_binds_the_asim_idiom_clean():
    """A declared tabular return gives the pipeline downstream of the call its columns."""
    query = parse(IDIOM, schema=ASIM)

    assert query.diagnostics == []
    # Semantic mode keeps a name only when its ReferencedSymbol is a
    # ColumnSymbol, so this set is what the declared return resolved.
    assert query.get_referenced_columns() == {"ActorUsername"}
    assert query.get_referenced_functions() == {
        "ago",
        "imProcessCreate",
        "isnotempty",
        "now",
    }


def test_a_column_the_declaration_does_not_name_is_a_diagnostic():
    """A column the declaration does not name is a diagnostic, so the return schema is closed."""
    misspelled = IDIOM.replace("ActorUsername", "ActorUserName")

    diagnostics = parse(misspelled, schema=ASIM).diagnostics

    assert len(diagnostics) == 1
    assert "does not refer to any known column" in diagnostics[0]["message"]


def test_an_open_return_resolves_any_column():
    """``returns=None`` declares the signature and leaves the result columns open."""
    schema = {"openF": FunctionSchema(returns=None)}

    query = parse("openF() | where whateverColumn == 1", schema=schema)

    assert query.diagnostics == []
    assert query.get_referenced_functions() == {"openF"}


def test_a_scalar_return_declares_a_scalar_udf():
    """A ``returns`` that names a scalar type declares a scalar function."""
    schema = {
        "T": {"a": "long"},
        "myScalar": FunctionSchema(parameters=(("a", "long"),), returns="long"),
    }

    query = parse("T | extend y = myScalar(a)", schema=schema)

    assert query.diagnostics == []
    assert _call_result_type(query) == "long"


def test_required_zero_makes_every_parameter_optional():
    """``required=0`` lets a call omit every declared parameter."""
    query = parse("imProcessCreate()", schema=ASIM)

    assert query.diagnostics == []
    assert query.get_referenced_functions() == {"imProcessCreate"}


def test_required_none_keeps_the_arity_diagnostic():
    """Leaving ``required`` unset makes every parameter mandatory.

    ``docs/tier1-syntax-tree.md`` quotes Microsoft's sentence in full, so a DLL
    refresh that rewords it lands here.
    """
    schema = {
        "imProcessCreate": FunctionSchema(
            parameters=(("starttime", "datetime"), ("endtime", "datetime")),
            returns="(TimeGenerated:datetime, ActorUsername:string)",
        ),
    }

    diagnostics = parse("imProcessCreate()", schema=schema).diagnostics

    assert [d["message"] for d in diagnostics] == [
        "The function 'imProcessCreate' expects 2 arguments."
    ]


def test_required_below_the_parameter_count_makes_only_the_tail_optional():
    """``required=1`` of two parameters leaves the first parameter mandatory."""
    schema = {
        "T": {"a": "long"},
        "f": FunctionSchema(
            parameters=(("a", "long"), ("b", "long")), required=1, returns="long"
        ),
    }

    assert parse("T | extend y = f(a)", schema=schema).diagnostics == []
    zero_arg = parse("T | extend y = f()", schema=schema).diagnostics
    # Microsoft's sentence counts the declared parameters, so it reads here the
    # way it reads for a declaration whose parameters are all required.
    assert [d["message"] for d in zero_arg] == ["The function 'f' expects 2 arguments."]

    signature = build_global_state(schema).Database.Functions[0].Signatures[0]
    assert [p.MinOccurring for p in signature.Parameters] == [1, 0]


def test_an_unknown_parameter_type_warns_and_falls_back_to_string():
    """A parameter type goes through the same resolver a column type does."""
    schema = {"f": FunctionSchema(parameters=(("p", "notatype"),), returns="long")}

    with pytest.warns(RuntimeWarning, match="Unknown KQL scalar type") as record:
        state = build_global_state(schema)

    assert "parameter 'p' of function 'f'" in str(record[0].message)
    declared = state.Database.Functions[0]
    assert str(declared.Signatures[0].Parameters[0].TypeKind) == "Declared"
    assert [str(t.Name) for t in declared.Signatures[0].Parameters[0].DeclaredTypes] == [
        "string"
    ]


def test_a_builtin_of_the_same_name_wins_over_the_declaration():
    """Microsoft's binder resolves the built-in, so a declaration under its name is inert.

    The docs tell you to pick a name that is not a built-in. This pins the
    precedence a DLL refresh could move.
    """
    schema = {
        "T": {"a": "long"},
        "tolower": FunctionSchema(parameters=(("x", "long"),), returns="long"),
    }

    query = parse("T | extend y = tolower('X')", schema=schema)

    assert query.diagnostics == []
    # The declaration says ``long``; the built-in ``tolower`` returns ``string``.
    assert _call_result_type(query) == "string"

    # A ``long`` argument is what the declaration is written for, and the call
    # still resolves the way it does with nothing under that name in the
    # schema, so the declaration is never a candidate.
    declared = parse("T | extend y = tolower(5)", schema=schema)
    control = parse("T | extend y = tolower(5)", schema={"T": {"a": "long"}})
    assert declared.diagnostics == control.diagnostics == []
    assert _call_result_type(declared) == _call_result_type(control) == "string"


def test_required_above_the_parameter_count_is_a_value_error():
    """``required`` counts leading parameters, so it cannot exceed the declaration."""
    schema = {
        "f": FunctionSchema(
            parameters=(("a", "long"), ("b", "long")),
            required=5,
        ),
    }

    with pytest.raises(ValueError, match="2 parameter"):
        build_global_state(schema)


def test_a_non_str_parameter_type_is_a_type_error():
    """A parameter type is checked before the CLR boundary, worded for a parameter."""
    schema = {"f": FunctionSchema(parameters=(("p", 5),))}

    with pytest.raises(TypeError, match="Parameter 'p' of function 'f'"):
        build_global_state(schema)


@pytest.mark.parametrize(
    "returns",
    [
        "(TimeGenerated:datetime, ActorUsername:string)",
        {"TimeGenerated": "datetime", "ActorUsername": "string"},
        ["TimeGenerated", "ActorUsername"],
    ],
    ids=["schema-string", "dict", "list"],
)
def test_every_tabular_returns_form_declares_the_same_columns(returns):
    """The three table value forms declare the same closed column set.

    An open return resolves any name. The misspelled column is what makes
    each assertion here depend on the columns the form declares.
    """
    schema = {"f": FunctionSchema(returns=returns)}

    query = parse("f() | project TimeGenerated, ActorUsername", schema=schema)

    assert query.diagnostics == []
    assert query.get_referenced_columns() == {"TimeGenerated", "ActorUsername"}

    misspelled = parse("f() | project TimeGeneratd", schema=schema).diagnostics

    assert len(misspelled) == 1
    assert "does not refer to any known column" in misspelled[0]["message"]


def test_a_returns_of_an_unsupported_type_is_a_type_error():
    """``returns`` takes a scalar type name, one of the table forms, or ``None``."""
    with pytest.raises(TypeError, match="FunctionSchema.returns for function 'f'"):
        build_global_state({"f": FunctionSchema(returns=5)})


def test_parameters_of_none_names_the_field_and_the_function():
    """``parameters=None`` reads as a mirror of ``returns=None`` and is a shape error."""
    with pytest.raises(TypeError, match="FunctionSchema.parameters for function 'f'"):
        build_global_state({"f": FunctionSchema(parameters=None)})


def test_a_bool_required_is_a_type_error():
    """``True`` is an ``int`` to Python, so ``required`` rejects it by type."""
    schema = {"f": FunctionSchema(parameters=(("a", "long"),), required=True)}

    with pytest.raises(TypeError, match="must be an int"):
        build_global_state(schema)


def test_functions_and_tables_share_one_dict():
    """One dict carries both, and the table extractor still reports only tables."""
    schema = {
        "SignInEvents": {"IPAddress": "string"},
        "imProcessCreate": FunctionSchema(
            parameters=(("starttime", "datetime"),),
            returns="(TimeGenerated:datetime, ActorUsername:string)",
            required=0,
        ),
    }

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        state = build_global_state(schema)

    # `to_ir(attach_schema=dict)` hands this dict to `SchemaAttacher`, which
    # takes tables only.
    assert extract_schemas_from_global_state(state) == {
        "SignInEvents": {"IPAddress": "string"},
    }

    query = parse(
        "SignInEvents | join (imProcessCreate()) on "
        "$left.IPAddress == $right.ActorUsername",
        schema=schema,
    )
    assert query.diagnostics == []
    assert query.get_referenced_columns() == {"IPAddress", "ActorUsername"}


# A callable `returns`: the columns depend on the call's own arguments -------

WATCHLISTS = {
    "HighValueAssets": {"SearchKey": "string", "AssetTier": "long"},
    "TerminatedEmployees": {"SearchKey": "string", "LastDay": "datetime"},
}


def test_a_resolver_returns_a_different_schema_per_argument():
    """A callable ``returns`` declares one column set per call site.

    ``_GetWatchlist('name')`` is the shape: one function, a different table
    behind every name a caller passes it.
    """
    schema = {
        "_GetWatchlist": FunctionSchema(
            parameters=(("watchlistName", "string"),),
            returns=lambda values: WATCHLISTS.get(values[0]),
        ),
    }

    assets = parse(
        "_GetWatchlist('HighValueAssets') | project SearchKey, AssetTier",
        schema=schema,
    )
    assert assets.diagnostics == []
    assert assets.get_referenced_columns() == {"SearchKey", "AssetTier"}

    terminated = parse(
        "_GetWatchlist('TerminatedEmployees') | project SearchKey, LastDay",
        schema=schema,
    )
    assert terminated.diagnostics == []
    assert terminated.get_referenced_columns() == {"SearchKey", "LastDay"}

    # The column the other watchlist declares is what proves each call site got
    # its own closed symbol rather than the union of both.
    crossed = parse(
        "_GetWatchlist('HighValueAssets') | project LastDay", schema=schema
    ).diagnostics
    assert len(crossed) == 1
    assert "does not refer to any known column" in crossed[0]["message"]


def test_a_non_literal_argument_reaches_the_resolver_as_none():
    """An argument with no literal value reaches the resolver as ``None``."""
    seen = []

    def resolver(values):
        seen.append(values)
        # The closed branch is what makes the query below depend on the value:
        # a name the resolver could read would close the columns and turn
        # ``anyColumn`` into a diagnostic.
        return None if values[0] is None else {"SearchKey": "string"}

    schema = {
        "T": {"name": "string"},
        "_GetWatchlist": FunctionSchema(
            parameters=(("watchlistName", "string"),), returns=resolver
        ),
    }

    query = parse(
        "let n = toscalar(T | take 1 | project name); "
        "_GetWatchlist(n) | where anyColumn == 1",
        schema=schema,
    )

    assert query.diagnostics == []
    assert seen == [(None,)]


def test_a_datetime_literal_reaches_the_resolver_as_a_string():
    """A datetime literal crosses as its invariant-culture ``str``."""
    from System import DateTime

    seen = []

    def resolver(values):
        seen.append(values)
        return {"SearchKey": "string"}

    schema = {
        "getRange": FunctionSchema(
            parameters=(("starttime", "datetime"),), returns=resolver
        ),
    }

    query = parse(
        "getRange(datetime(2024-01-01)) | project SearchKey", schema=schema
    )

    assert query.diagnostics == []
    assert isinstance(seen[0][0], str)
    assert seen == [(str(DateTime(2024, 1, 1)),)]


def test_a_resolver_that_raises_leaves_the_columns_open(caplog):
    """A resolver that raises is contained: a warning, and the call binds open.

    Microsoft calls the resolver on the CLR's own stack while binding, so an
    exception that escapes leaves ``parse`` from a frame the caller never
    wrote.
    """

    def resolver(values):
        raise RuntimeError("kaboom")

    schema = {
        "getWatchlist": FunctionSchema(
            parameters=(("watchlistName", "string"),), returns=resolver
        ),
    }

    with caplog.at_level(logging.WARNING, logger="kustology.utils.schema_state"):
        query = parse("getWatchlist('x') | where anyColumn == 1", schema=schema)

    assert query.diagnostics == []
    assert query.get_referenced_columns() == {"anyColumn"}
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(records) == 1
    assert "'getWatchlist'" in records[0].getMessage()
    assert records[0].exc_info is not None


def test_a_resolver_returning_a_scalar_type_name_falls_back_with_a_warning(caplog):
    """A resolver declares columns, so a scalar type name back from it fails the same way."""
    schema = {
        "getWatchlist": FunctionSchema(
            parameters=(("watchlistName", "string"),),
            returns=lambda values: "long",
        ),
    }

    with caplog.at_level(logging.WARNING, logger="kustology.utils.schema_state"):
        query = parse("getWatchlist('x') | where anyColumn == 1", schema=schema)

    assert query.diagnostics == []
    assert query.get_referenced_columns() == {"anyColumn"}
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(records) == 1
    assert "'getWatchlist'" in records[0].getMessage()
