# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from .bridge import KustoCode, KustoCodeService, FormattingOptions

SchemaLike = dict | str | None

# Binder code emitted when a name doesn't refer to any known table/variable/function.
_UNKNOWN_TABLE_CODE = "KS204"


def parse(query_text: str, schema: SchemaLike = None):
    """
    Parse a KQL query and return a KustoQuery.

    When ``schema`` is provided the query is bound (semantic analysis runs);
    callers can use ``KustoQuery.has_semantics`` to check.
    Schema may be either a dict ``{"Table": {"col": "type", ...}}`` or a Kusto
    schema string ``"(col:type, ...)"`` (single-table form).
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
    """
    from .utils.analysis import build_global_state

    if schema is None:
        code = KustoCode.Parse(query_text)
    else:
        state = build_global_state(schema)
        code = KustoCode.ParseAndAnalyze(query_text, state)
    diagnostics = code.GetDiagnostics()

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
