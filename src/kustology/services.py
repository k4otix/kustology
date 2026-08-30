# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Parse, format, and validate entry points, plus the guard around Microsoft's analyzer."""

from collections.abc import Callable
from typing import Any

from ._text import Utf16Offsets, check_utf16_encodable
from .bridge import (
    FormattingOptions,
    KustoCode,
    KustoCodeService,
    ensure_invariant_culture,
)

# A schema maps table name to column spec. `str` stays out of the union
# because `build_global_state` raises `TypeError` on anything that isn't a
# dict, so `parse(q, schema="(a:string)")` would type-check a call that can
# only fail at runtime. The single-table string form is a *value* inside the
# mapping: `{"T": "(a:string)"}`.
SchemaLike = dict | None

# Binder code for a name that doesn't refer to any known table/variable/function.
# The only code ``validate(..., ignore_unknown_tables=True)`` waives; see
# ``_UNKNOWN_NAME_CODES`` for why that stays narrow.
_UNKNOWN_TABLE_CODE = "KS204"

# Every binder code for "this name is not among the things the GlobalState
# describes". Microsoft raises one per *kind* of name, so the family runs
# eleven codes wider than KS204, eight of them ``Error`` severity:
#
#   KS142 item        KS204 table   KS205 fuzzy name   KS207 cluster
#   KS208 database    KS209 external table             KS210 materialized view
#   KS211 function    KS247 entity group               KS248 stored query result
#   KS260 graph model KS261 graph snapshot
#
# Pinned instead of reflected, so the filter is a frozenset lookup and the
# codes are greppable. ``tests/test_reflection_audit.py`` re-derives the set
# from ``Kusto.Language.DiagnosticFacts`` and fails if a DLL refresh moves or
# adds one, which is the drift AGENTS.md warns about by name.
#
# ``validate(ignore_unknown_tables=True)`` waives KS204 alone. It reaches the
# binder only when the caller passed a schema, so the caller owns every name
# and waives one dimension of it: tables outside their schema. Suppressing
# "unknown function" there would hide an error about a name that schema was
# supposed to describe. This set covers a bind against ``GlobalState.Default``,
# globals the caller never chose that describe nothing, where every
# name-resolution failure is an artifact of how the types were obtained.
_UNKNOWN_NAME_CODES = frozenset({
    "KS142", "KS204", "KS205", "KS207", "KS208", "KS209",
    "KS210", "KS211", "KS247", "KS248", "KS260", "KS261",
})

# kustology's own diagnostic code, outside Microsoft's ``KS***`` space: it
# reports a failure of this package's call into their binder. A consumer
# filtering on ``KS`` codes skips it; a consumer gating on
# ``severity == "Error"`` sees it, which matters because the IR it accompanies
# was built from an unbound tree.
ANALYZE_FAILED_CODE = "KUSTOLOGY001"


def _analyze_guarded(
    analyze: Callable[[], Any],
    unbound: Callable[[], Any],
) -> tuple[Any, dict | None]:
    """Bind a tree, or fall back to the unbound one and say so.

    The single guard around every call this package makes into Microsoft's
    analyzer: ``ParseAndAnalyze`` in :func:`parse`, :func:`validate` and
    :meth:`kustology.ir.IRBuilder.build`, and ``KustoCode.Analyze`` in
    :meth:`kustology.KustoQuery.to_ir`. The binder can crash on input the
    parser accepts without a single diagnostic: a ``declare pattern`` whose
    match arm supplies more values than the declaration has parameters sends
    ``Binder.NodeBinder.VisitPatternDeclaration`` indexing the
    declared-parameter list with the supplied-value index (Kusto.Language
    12.3.2 through 12.4.1). Unguarded, that ``IndexOutOfRangeException``
    reaches a caller who asked for diagnostics as a raw CLR traceback.

    Returns ``(code, failure)``. ``failure`` is ``None`` when the analysis
    succeeded; otherwise the tree is the **unanalyzed** parse and ``failure``
    is one diagnostic in this module's dict shape, carrying the .NET exception
    so the crash stays reportable upstream.

    Falling back keeps the digest: everything the binder writes is stripped
    before ``semantic_hash`` is computed, so an unbound build produces the
    same digest a bound one would. Lost are the binder's own answers,
    ``Expr.result_type``, ``Pipeline.result_schema`` and table provenance,
    which is why the failure carries ``Error`` severity.

    The row carries the same seven keys :func:`_diagnostic_dicts` emits, so a
    list holding both is uniform. ``start``/``length`` are zero because a
    fault in the analyzer has no source span, and the keys stay present
    because every consumer of this shape reads them. ``detail`` carries
    ``str(exc)``: a CLR exception reaching Python through pythonnet brings its
    stack trace along, thousands of characters across many lines, so
    ``message`` stays one short sentence naming the exception's type and a
    caller who logs diagnostics inline gets no stack trace in that line.

    ``MemoryError`` and ``RecursionError`` propagate: both mean the *host* is
    out of a resource, which is not a binder fault. That holds at every call
    site, including the two in :meth:`kustology.KustoQuery.to_ir` whose
    ``unbound`` only returns the tree the object already holds.

    The remaining ``except Exception`` is broad because pythonnet surfaces a
    .NET exception as an ordinary ``Exception`` subclass and there is no
    shared base class for "the binder gave up"; the arity crash is an
    ``IndexOutOfRangeException``. ``BaseException`` is *not* caught, so
    ``KeyboardInterrupt`` and ``SystemExit`` still propagate.
    """
    try:
        return analyze(), None
    except (MemoryError, RecursionError):
        raise  # resource exhaustion is the host's problem, not a binder fault
    except Exception as exc:  # noqa: BLE001 — see the docstring's last paragraph
        return unbound(), {
            "start": 0,
            "length": 0,
            "message": (
                "Kusto.Language's analyzer raised "
                f"{type(exc).__name__} on this query; it was built from the "
                "unanalyzed parse instead, so no binder-supplied types, "
                "schemas or provenance are present. The exception is in "
                "'detail'."
            ),
            "severity": "Error",
            "category": "General",
            "code": ANALYZE_FAILED_CODE,
            "detail": str(exc),
        }


def parse(query_text: str, schema: SchemaLike = None):
    """Parse a KQL query and return a ``KustoQuery``.

    With ``schema`` the query is bound (semantic analysis runs) and
    ``KustoQuery.has_semantics`` reports which. Schema is a dict mapping table
    name to a column spec: ``{"Table": {"col": "type", ...}}``,
    ``{"Table": "(col:type, ...)"}`` or ``{"Table": ["col", ...]}`` — see
    :func:`kustology.utils.analysis.build_global_state`.

    If Microsoft's analyzer crashes on the query, the returned ``KustoQuery``
    holds the *unbound* parse (``has_semantics`` is ``False``) and reports one
    extra ``Error`` diagnostic naming the .NET exception — see
    :func:`_analyze_guarded`.

    Raises ``ValueError`` when ``query_text`` holds an unpaired surrogate,
    which UTF-16 cannot encode; see
    :func:`kustology._text.check_utf16_encodable`.
    """
    from .core import KustoQuery
    from .utils.analysis import build_global_state

    check_utf16_encodable(query_text)
    ensure_invariant_culture()
    if schema is None:
        return KustoQuery(KustoCode.Parse(query_text))
    state = build_global_state(schema)
    code, failure = _analyze_guarded(
        lambda: KustoCode.ParseAndAnalyze(query_text, state),
        lambda: KustoCode.Parse(query_text),
    )
    return KustoQuery(code, extra_diagnostics=None if failure is None else [failure])


def format_query(query_text: str, options: FormattingOptions | None = None) -> str:
    """Format a KQL query using Microsoft's KustoCodeService.

    The .NET formatter emits ``Environment.NewLine`` for ``PlacementStyle.NewLine``
    (CRLF on Windows, LF elsewhere). Normalize to LF so output bytes are
    platform-consistent — the KQL canonical form is LF-only.

    Raises ``ValueError`` on text UTF-16 cannot encode; see :func:`parse`.
    """
    check_utf16_encodable(query_text)
    ensure_invariant_culture()
    formatted = KustoCodeService(query_text).GetFormattedText(options)
    return str(formatted.Text).replace("\r\n", "\n")


def _diagnostic_dicts(
    diagnostics,
    offsets: Utf16Offsets,
    ignore_unknown_tables: bool = False,
) -> list[dict]:
    """Render a .NET diagnostic list as this library's list-of-dicts shape.

    The one place that decides what a diagnostic looks like on the Python
    side. :func:`validate` and :attr:`kustology.KustoQuery.diagnostics` both
    go through it so the two cannot drift; they differ only in where the
    ``KustoCode`` came from, and the property exists to answer without a
    second parse.

    ``start`` and ``length`` are translated to code points, so they index the
    Python ``str`` the caller passed in. Microsoft reports them in UTF-16
    code units, which differ as soon as the query holds an astral character.
    """
    results = []
    for d in diagnostics:
        code_str = str(d.Code)
        if ignore_unknown_tables and code_str == _UNKNOWN_TABLE_CODE:
            continue
        start, length = offsets.span_to_codepoints(d.Start, d.Length)
        results.append(
            {
                "start": start,
                "length": length,
                "message": str(d.Message),
                "severity": str(d.Severity),
                "category": str(d.Category),
                "code": code_str,
                # Always present so a caller reads the key without asking
                # which kind of row it holds; ``None`` for a parser
                # diagnostic, filled by :func:`_analyze_guarded`'s crash row.
                "detail": None,
            }
        )
    return results


def validate(
    query_text: str,
    schema: SchemaLike = None,
    ignore_unknown_tables: bool = False,
) -> list[dict]:
    """Validate a KQL query and return diagnostics.

    Without ``schema`` only parser diagnostics are returned. With ``schema``
    the query is bound and semantic diagnostics (unresolved columns, type
    errors) are included. Set ``ignore_unknown_tables=True`` to suppress KS204
    ("name does not refer to any known table") diagnostics for tables outside
    the schema. ``schema`` takes the same dict-of-tables shapes :func:`parse`
    accepts.

    This parses the text. When you already hold a ``KustoQuery``, read
    :attr:`kustology.KustoQuery.diagnostics` instead: same dicts, no second
    parse.

    A query that crashes Microsoft's analyzer returns the parser's own
    diagnostics plus one ``Error`` row naming the .NET exception, and raises
    nothing — see :func:`_analyze_guarded`.

    ``start`` and ``length`` are code-point offsets into ``query_text``, so
    they slice it directly. Raises ``ValueError`` on text UTF-16 cannot
    encode; see :func:`parse`.
    """
    from .utils.analysis import build_global_state

    offsets = Utf16Offsets(query_text, check_utf16_encodable(query_text))
    ensure_invariant_culture()
    if schema is None:
        return _diagnostic_dicts(
            KustoCode.Parse(query_text).GetDiagnostics(), offsets, ignore_unknown_tables,
        )
    state = build_global_state(schema)
    code, failure = _analyze_guarded(
        lambda: KustoCode.ParseAndAnalyze(query_text, state),
        lambda: KustoCode.Parse(query_text),
    )
    results = _diagnostic_dicts(code.GetDiagnostics(), offsets, ignore_unknown_tables)
    if failure is not None:
        results.append(failure)
    return results
