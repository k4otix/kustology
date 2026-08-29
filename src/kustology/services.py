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

# A schema is a mapping of table name to column spec. `str` is deliberately
# not in the union: `build_global_state` raises `TypeError` on anything that
# isn't a dict, so admitting `parse(q, schema="(a:string)")` would type-check
# a call that can only fail at runtime. The single-table string form is a
# *value* inside the mapping — `{"T": "(a:string)"}` — not a substitute for
# it.
SchemaLike = dict | None

# Binder code emitted when a name doesn't refer to any known table/variable/function.
# This one code, and no other, is what ``validate(..., ignore_unknown_tables=True)``
# waives — see ``_UNKNOWN_NAME_CODES`` for why that stays narrow.
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
# in the query and is waiving exactly one dimension of it — tables outside
# their schema. Suppressing "unknown function" there would hide an error
# about a name their schema was supposed to describe. This set is for the
# opposite case: a binding run against ``GlobalState.Default``, globals the
# caller never chose and which describe nothing, where every name-resolution
# failure is an artifact of how the types were obtained.
_UNKNOWN_NAME_CODES = frozenset({
    "KS142", "KS204", "KS205", "KS207", "KS208", "KS209",
    "KS210", "KS211", "KS247", "KS248", "KS260", "KS261",
})

# kustology's own diagnostic code, deliberately outside Microsoft's ``KS***``
# space: it reports a failure of *our* call into their binder, not a defect in
# the query. A consumer filtering on ``KS`` codes will not mistake it for one,
# and a consumer gating on ``severity == "Error"`` still sees it — which is
# the point, since the IR it accompanies was built from an unbound tree.
ANALYZE_FAILED_CODE = "KUSTOLOGY001"


def _analyze_guarded(
    analyze: Callable[[], Any],
    unbound: Callable[[], Any],
) -> tuple[Any, dict | None]:
    """Bind a tree, or fall back to the unbound one and say so.

    The single guard around every call this package makes into Microsoft's
    analyzer — ``KustoCode.ParseAndAnalyze`` in :func:`parse` and
    :func:`validate`, ``KustoCode.Analyze`` in
    :meth:`kustology.KustoQuery.to_ir`, and ``ParseAndAnalyze`` again in
    :meth:`kustology.ir.IRBuilder.build`. It exists because the binder can
    *crash* on input the parser accepts without a single diagnostic: a
    ``declare pattern`` whose match arm supplies more values than the
    declaration has parameters sends ``Binder.NodeBinder.VisitPatternDeclaration``
    indexing the declared-parameter list with the supplied-value index
    (Kusto.Language 12.3.2 through 12.4.1, unchanged). Unguarded, that
    ``IndexOutOfRangeException`` comes out of ``parse()``/``to_ir()`` raw —
    a caller who passed a schema gets a CLR traceback where they asked for
    diagnostics.

    Returns ``(code, failure)``. ``failure`` is ``None`` when the analysis
    succeeded; otherwise the tree is the **unanalyzed** parse and ``failure``
    is one diagnostic in this module's dict shape, carrying the .NET
    exception so the crash stays reportable upstream instead of being
    swallowed.

    Falling back is safe in the one way that matters here: everything the
    binder writes is stripped before ``semantic_hash`` is computed, so an
    unbound build produces the *same digest* a bound one would have. What is
    genuinely lost is the binder's own answers — ``Expr.result_type``,
    ``Pipeline.result_schema``, table provenance — which is why the failure
    is an ``Error`` rather than a warning.

    ``start``/``length`` are zero because the failure has no source location:
    it is a fault in the analyzer, not a span of the query. Every consumer of
    this shape already reads those two keys, so omitting them is not an
    option.

    The ``except Exception`` is deliberately broad. A .NET exception reaches
    Python through pythonnet as an ordinary ``Exception`` subclass, and there
    is no shared base class for "the binder gave up" — the arity crash is an
    ``IndexOutOfRangeException`` and the next one will be something else.
    ``BaseException`` is *not* caught, so ``KeyboardInterrupt`` and
    ``SystemExit`` still propagate.
    """
    try:
        return analyze(), None
    except Exception as exc:  # noqa: BLE001 — see the docstring's last paragraph
        return unbound(), {
            "start": 0,
            "length": 0,
            "message": (
                "Kusto.Language's analyzer raised "
                f"{type(exc).__name__} on this query; it was built from the "
                "unanalyzed parse instead, so no binder-supplied types, "
                f"schemas or provenance are present. {exc}"
            ),
            "severity": "Error",
            "category": "General",
            "code": ANALYZE_FAILED_CODE,
        }


def parse(query_text: str, schema: SchemaLike = None):
    """Parse a KQL query and return a ``KustoQuery``.

    When ``schema`` is provided the query is bound (semantic analysis runs);
    callers can use ``KustoQuery.has_semantics`` to check.
    Schema is a dict mapping table name to a column spec:
    ``{"Table": {"col": "type", ...}}``, ``{"Table": "(col:type, ...)"}`` or
    ``{"Table": ["col", ...]}`` — see
    :func:`kustology.utils.analysis.build_global_state`.

    If Microsoft's analyzer crashes on the query, the returned ``KustoQuery``
    holds the *unbound* parse (``has_semantics`` is ``False``) and reports one
    extra ``Error`` diagnostic naming the .NET exception — see
    :func:`_analyze_guarded`.

    Raises ``ValueError`` when ``query_text`` holds an unpaired surrogate,
    which UTF-16 cannot encode — see
    :func:`kustology._text.check_utf16_encodable` for why that has to be
    caught here rather than at the CLR boundary.
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
    go through it so the two cannot drift — they differ only in where the
    ``KustoCode`` came from, and the property's whole reason to exist is that
    it already has one and must not parse again.

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
            }
        )
    return results


def validate(
    query_text: str,
    schema: SchemaLike = None,
    ignore_unknown_tables: bool = False,
) -> list[dict]:
    """Validate a KQL query and return diagnostics.

    Without ``schema`` only parser diagnostics are returned. With ``schema`` the
    query is bound and semantic diagnostics (unresolved columns, type errors) are
    included. Set ``ignore_unknown_tables=True`` to suppress KS204 ("name does
    not refer to any known table") diagnostics for tables outside the schema.

    ``schema`` takes the same dict-of-tables shapes :func:`parse` accepts.

    This parses the text. When you already hold a ``KustoQuery``, read
    :attr:`kustology.KustoQuery.diagnostics` instead — same dicts, no second
    parse.

    A query that crashes Microsoft's analyzer returns the parser's own
    diagnostics plus one ``Error`` row naming the .NET exception, rather than
    raising it — see :func:`_analyze_guarded`.

    ``start`` and ``length`` are code-point offsets into ``query_text``, so
    they slice it directly.

    Raises ``ValueError`` on text UTF-16 cannot encode; see :func:`parse`.
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
