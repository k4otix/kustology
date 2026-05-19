# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import hashlib

from ..bridge import ColumnSymbol, TableSymbol
from .schema_state import build_global_state  # re-exported
from .walker import KustoWalker, node_to_dict  # re-exported

__all__ = [
    "build_global_state",
    "find_table_references",
    "get_operator_chain",
    "get_operator_stats",
    "get_referenced_columns",
    "get_structural_hash",
    "get_tables_semantic",
    "get_tables_syntactic",
    "get_time_range",
    "KustoWalker",
    "node_to_dict",
    "replace_table",
]


# See kustology.reflection.time_functions for the reflected source.
try:
    from ..reflection import time_functions as _time_functions

    _TIME_FUNCS = _time_functions()
except Exception:  # pragma: no cover — defensive
    _TIME_FUNCS = frozenset({
        "ago", "now", "datetime", "startofday", "endofday",
        "startofweek", "endofweek", "startofmonth", "endofmonth",
        "startofyear", "endofyear", "bin", "format_datetime", "todatetime",
        "totimespan", "datetime_add", "datetime_diff",
    })

_STRUCTURAL_NOISE_KINDS = frozenset({"List", "SeparatedElement"})

_TIME_LITERAL_KINDS = frozenset({
    "DateTimeLiteralExpression", "TimespanLiteralExpression",
})


def _path_expression_table(node):
    """Yield the trailing NameReference of a PathExpression source
    (``database("d").T``, ``cluster("c").database("d").T``)."""
    right = node.GetChild(2)
    if right is None:
        return
    yield from _unwrap_table_expr(right)


def _unwrap_table_expr(node):
    """Yield candidate NameReference nodes that occupy a table-source position."""
    if node is None:
        return
    kind = str(node.Kind)
    if kind == "NameReference":
        yield node
        return
    if kind == "ParenthesizedExpression":
        for i in range(node.ChildCount):
            yield from _unwrap_table_expr(node.GetChild(i))
        return
    if kind in _STRUCTURAL_NOISE_KINDS:
        for i in range(node.ChildCount):
            yield from _unwrap_table_expr(node.GetChild(i))
        return
    if kind == "PipeExpression":
        # Leftmost child is the source table feeding this sub-pipeline.
        yield from _unwrap_table_expr(node.GetChild(0))
        return
    if kind == "PathExpression":
        yield from _path_expression_table(node)
        return


def _is_function_callee(node) -> bool:
    """True when this NameReference is the callee of a FunctionCallExpression.

    Uses positional equality (TextStart/Width) because pythonnet returns fresh
    wrapper objects on each .NET property access, making `is` unreliable.
    """
    parent = node.Parent
    if parent is None or str(parent.Kind) != "FunctionCallExpression":
        return False
    callee = parent.GetChild(0)
    return (
        callee is not None
        and callee.TextStart == node.TextStart
        and callee.Width == node.Width
    )


def _collect_table_refs(syntax) -> list:
    """Return every (name, NameReference node) that occupies a table-source
    position. Filters out let-bound names but does NOT deduplicate by name —
    callers that want a set should dedupe themselves.
    """
    let_vars = set()
    refs = []

    class Walker(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)

            if kind == "LetStatement":
                name_node = node.GetChild(1)
                if name_node is not None:
                    let_vars.add(name_node.ToString().strip())
                rhs = node.GetChild(3)
                if rhs is not None:
                    for ref in _unwrap_table_expr(rhs):
                        refs.append(ref)
                return

            if kind in ("PipeExpression", "ExpressionStatement"):
                for ref in _unwrap_table_expr(node.GetChild(0)):
                    refs.append(ref)
                return

            if kind in ("JoinOperator", "LookupOperator", "FacetOperator"):
                expr = getattr(node, "Expression", None)
                if expr is not None:
                    for ref in _unwrap_table_expr(expr):
                        refs.append(ref)
                return

            if kind == "UnionOperator":
                for i in range(node.ChildCount):
                    for ref in _unwrap_table_expr(node.GetChild(i)):
                        refs.append(ref)

    Walker().visit(syntax)
    out = []
    for ref in refs:
        name = ref.ToString().strip()
        if not name or name in let_vars:
            continue
        out.append((name, ref))
    return out


def _collect_semantic_table_refs(syntax) -> list:
    """Return every (name, node) where node.ReferencedSymbol is a TableSymbol.

    Does NOT dedupe by name — callers that want a set should dedupe themselves.
    """
    refs = []

    class Walker(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) != "NameReference":
                return
            sym = node.ReferencedSymbol
            if sym is None:
                return
            if isinstance(sym, TableSymbol):
                refs.append((sym.Name, node))

    Walker().visit(syntax)
    return refs


def find_table_references(kusto_code, force_syntactic: bool = False) -> list:
    """Return [(name, node), ...] for every table reference (one entry per
    occurrence). Use ``get_referenced_tables`` for a deduplicated set of names.
    """
    if not force_syntactic and kusto_code.HasSemantics:
        return _collect_semantic_table_refs(kusto_code.Syntax)
    return _collect_table_refs(kusto_code.Syntax)


def get_tables_syntactic(kusto_code) -> set[str]:
    return {name for name, _ in _collect_table_refs(kusto_code.Syntax)}


def get_tables_semantic(kusto_code) -> set[str]:
    """Return tables resolved by the binder. Requires a bound KustoCode."""
    if not kusto_code.HasSemantics:
        raise ValueError(
            "get_tables_semantic requires a bound KustoCode "
            "(use parse(text, schema=...))."
        )
    return {name for name, _ in _collect_semantic_table_refs(kusto_code.Syntax)}


def get_operator_stats(kusto_code) -> dict[str, int]:
    counts: dict[str, int] = {}

    class OperatorCounter(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)
            if "Operator" in kind:
                counts[kind] = counts.get(kind, 0) + 1

    OperatorCounter().visit(kusto_code.Syntax)
    return counts


def get_operator_chain(kusto_code) -> list:
    """Flatten pipe expressions into a left-to-right list of operator nodes."""
    chain = []

    def walk(node):
        if node is None:
            return
        kind = str(node.Kind)
        if kind == "QueryBlock" or kind in _STRUCTURAL_NOISE_KINDS:
            for i in range(node.ChildCount):
                walk(node.GetChild(i))
        elif kind == "ExpressionStatement":
            walk(node.GetChild(0))
        elif kind == "PipeExpression":
            walk(node.GetChild(0))
            chain.append(node.GetChild(2))
        elif "Operator" in kind or kind == "NameReference":
            chain.append(node)

    walk(kusto_code.Syntax)
    return chain


def get_referenced_columns(kusto_code, force_syntactic: bool = False) -> set[str]:
    """Return the set of column names referenced in the query.

    Semantic mode keeps only NameReferences whose ReferencedSymbol is a
    ColumnSymbol — function names and aliases drop out naturally. Syntactic
    mode skips function callees but cannot distinguish columns from aliases.
    """
    if not force_syntactic and kusto_code.HasSemantics:
        cols = set()

        class Walker(KustoWalker):
            def pre_visit(self, node):
                if str(node.Kind) != "NameReference":
                    return
                sym = node.ReferencedSymbol
                if sym is not None and isinstance(sym, ColumnSymbol):
                    cols.add(sym.Name)

        Walker().visit(kusto_code.Syntax)
        return cols

    table_names = {name for name, _ in _collect_table_refs(kusto_code.Syntax)}
    let_vars = set()

    class LetCollector(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) == "LetStatement":
                name_node = node.GetChild(1)
                if name_node is not None:
                    let_vars.add(name_node.ToString().strip())

    LetCollector().visit(kusto_code.Syntax)

    cols = set()

    class ColumnExtractor(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) != "NameReference":
                return
            if _is_function_callee(node):
                return
            name = node.ToString().strip()
            if not name or name in table_names or name in let_vars:
                return
            # `$left` / `$right` and other `$`-prefixed names are KQL macros.
            if name.startswith("$"):
                return
            cols.add(name)

    ColumnExtractor().visit(kusto_code.Syntax)
    return cols


def get_structural_hash(kusto_code) -> str:
    """SHA256 over the AST shape — dedupes queries that differ only in literal
    values or whitespace. Not a logical-equivalence hash: parenthesization and
    other cosmetic rewrites may still produce different hashes.
    """
    parts = []

    class HashWalker(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)
            if kind in _STRUCTURAL_NOISE_KINDS:
                return
            if "Token" in kind:
                return
            parts.append(kind)

    HashWalker().visit(kusto_code.Syntax)
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def get_time_range(kusto_code) -> list[tuple[str, int, int]]:
    """Return [(text, start, length), ...] for every time-related expression in
    source order: time-function calls (``ago``, ``now``, ``bin``, ...) plus
    standalone datetime/timespan literals not already inside a matched call.
    """
    fn_ranges = []  # (start, end) of matched time-function calls
    out = []

    class FnPass(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) != "FunctionCallExpression":
                return
            callee = node.GetChild(0)
            if callee is None:
                return
            if callee.ToString().strip() not in _TIME_FUNCS:
                return
            start = node.TextStart
            end = start + node.Width
            fn_ranges.append((start, end))
            out.append((node.ToString().strip(), start, node.Width))

    FnPass().visit(kusto_code.Syntax)

    def _within_function(start: int, end: int) -> bool:
        return any(fs <= start and end <= fe for fs, fe in fn_ranges)

    class LiteralPass(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) not in _TIME_LITERAL_KINDS:
                return
            start = node.TextStart
            end = start + node.Width
            if _within_function(start, end):
                return
            out.append((node.ToString().strip(), start, node.Width))

    LiteralPass().visit(kusto_code.Syntax)

    seen = set()
    deduped = []
    for entry in out:
        key = (entry[1], entry[2])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    deduped.sort(key=lambda t: t[1])
    return deduped


def replace_table(kusto_code, old_name: str, new_name: str, force_syntactic: bool = False) -> str:
    """Rename every reference to ``old_name`` to ``new_name``; return the new text.

    Semantic mode replaces every NameReference whose ReferencedSymbol is the
    matching TableSymbol (covers join/union/lookup). Syntactic mode matches by
    source position via the same allowlist used by ``find_table_references``.
    """
    refs = find_table_references(kusto_code, force_syntactic=force_syntactic)
    seen = set()
    replacements = []
    for name, node in refs:
        if name != old_name:
            continue
        key = (node.TextStart, node.Width)
        if key in seen:
            continue
        seen.add(key)
        replacements.append(key)

    text = kusto_code.Text
    for start, length in sorted(replacements, key=lambda t: t[0], reverse=True):
        text = text[:start] + new_name + text[start + length:]
    return text
