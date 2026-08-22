# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from .bridge import FormattingOptions, KustoCode, KustoCodeService

# A schema is a mapping of table name to column spec. `str` used to be in
# this union and never worked: `build_global_state` raises `TypeError` on
# anything that isn't a dict, so `parse(q, schema="(a:string)")` was a
# type-checked call that could only fail at runtime. The single-table string
# form is a *value* inside the mapping — `{"T": "(a:string)"}` — not a
# substitute for it.
SchemaLike = dict | None

# Binder code emitted when a name doesn't refer to any known table/variable/function.
# This one code, and no other, is what ``validate(..., ignore_unknown_tables=True)``
# waives -- see ``_UNKNOWN_NAME_CODES`` for why that stays narrow.
_UNKNOWN_TABLE_CODE = "KS204"

# Every binder code for "this name is not among the things the GlobalState
# describes". Microsoft raises one per *kind* of name, so the family is wider
# than KS204 by eleven codes, eight of them ``Error`` severity:
#
#   KS142 item        KS204 table   KS205 fuzzy name   KS207 cluster
#   KS208 database    KS209 external table             KS210 materialized view
#   KS211 function    KS247 entity group               KS248 stored query result
#   KS260 graph model KS261 graph snapshot
#
# The set is pinned rather than reflected so the filter is a frozenset lookup
# and the codes are greppable; ``tests/test_reflection_audit.py`` re-derives
# it from ``Kusto.Language.DiagnosticFacts`` and fails if a DLL refresh moves
# or adds one, which is the drift AGENTS.md warns about by name.
#
# **Deliberately not what ``validate(ignore_unknown_tables=True)`` waives.**
# The two flags answer different questions. ``validate`` only reaches the
# binder when the caller passed a schema, so there the caller owns every name
# in the query and is waiving exactly one dimension of it -- tables outside
# their schema. Suppressing "unknown function" there would hide an error
# about a name their schema was supposed to describe. This set is for the
# opposite case: a binding run against ``GlobalState.Default``, globals the
# caller never chose and which describe nothing, where every name-resolution
# failure is an artifact of how the types were obtained.
_UNKNOWN_NAME_CODES = frozenset({
    "KS142", "KS204", "KS205", "KS207", "KS208", "KS209",
    "KS210", "KS211", "KS247", "KS248", "KS260", "KS261",
})


def parse(query_text: str, schema: SchemaLike = None):
    """
    Parse a KQL query and return a KustoQuery.

    When ``schema`` is provided the query is bound (semantic analysis runs);
    callers can use ``KustoQuery.has_semantics`` to check.
    Schema is a dict mapping table name to a column spec:
    ``{"Table": {"col": "type", ...}}``, ``{"Table": "(col:type, ...)"}`` or
    ``{"Table": ["col", ...]}`` — see
    :func:`kustology.utils.analysis.build_global_state`.
    """
    from .core import KustoQuery
    from .utils.analysis import build_global_state

    if schema is None:
        code = KustoCode.Parse(query_text)
    else:
        state = build_global_state(schema)
        code = KustoCode.ParseAndAnalyze(query_text, state)
    return KustoQuery(code)


def format_query(query_text: str, options: FormattingOptions | None = None) -> str:
    """Format a KQL query using Microsoft's KustoCodeService.

    The .NET formatter emits ``Environment.NewLine`` for ``PlacementStyle.NewLine``
    (CRLF on Windows, LF elsewhere). Normalize to LF so output bytes are
    platform-consistent — the KQL canonical form is LF-only.
    """
    formatted = KustoCodeService(query_text).GetFormattedText(options)
    return str(formatted.Text).replace("\r\n", "\n")


def _diagnostic_dicts(diagnostics, ignore_unknown_tables: bool = False) -> list[dict]:
    """Render a .NET diagnostic list as this library's list-of-dicts shape.

    The one place that decides what a diagnostic looks like on the Python
    side. :func:`validate` and :attr:`kustology.KustoQuery.diagnostics` both
    go through it so the two cannot drift — they differ only in where the
    ``KustoCode`` came from, and the property's whole reason to exist is that
    it already has one and must not parse again.
    """
    results = []
    for d in diagnostics:
        code_str = str(d.Code)
        if ignore_unknown_tables and code_str == _UNKNOWN_TABLE_CODE:
            continue
        results.append(
            {
                "start": d.Start,
                "length": d.Length,
                "message": str(d.Message),
                "severity": str(d.Severity),
                "category": str(d.Category),
                "code": code_str,
            }
        )
    return results


def validate(
    query_text: str,
    schema: SchemaLike = None,
    ignore_unknown_tables: bool = False,
) -> list[dict]:
    """
    Validate a KQL query and return diagnostics.

    Without ``schema`` only parser diagnostics are returned. With ``schema`` the
    query is bound and semantic diagnostics (unresolved columns, type errors) are
    included. Set ``ignore_unknown_tables=True`` to suppress KS204 ("name does
    not refer to any known table") diagnostics for tables outside the schema.

    ``schema`` takes the same dict-of-tables shapes :func:`parse` accepts.

    This parses the text. When you already hold a ``KustoQuery``, read
    :attr:`kustology.KustoQuery.diagnostics` instead — same dicts, no second
    parse.
    """
    from .utils.analysis import build_global_state

    if schema is None:
        code = KustoCode.Parse(query_text)
    else:
        state = build_global_state(schema)
        code = KustoCode.ParseAndAnalyze(query_text, state)
    return _diagnostic_dicts(code.GetDiagnostics(), ignore_unknown_tables)
