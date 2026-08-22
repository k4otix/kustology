# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import hashlib
import warnings

from ..bridge import ColumnSymbol, FunctionSymbol, TableSymbol
from ..reflection import syntax_kinds as _syntax_kinds
from .schema_state import build_global_state  # re-exported
from .walker import (  # re-exported
    KustoWalker,
    iter_elements,
    node_name,
    node_text,
    node_to_dict,
)

__all__ = [
    "KustoWalker",
    "build_global_state",
    "collect_nodes",
    "find_table_references",
    "find_time_expressions",
    "get_operator_chain",
    "get_operator_stats",
    "get_referenced_columns",
    "get_referenced_functions",
    "get_structural_hash",
    "get_tables_semantic",
    "get_tables_syntactic",
    "get_time_range",
    "iter_elements",
    "node_name",
    "node_text",
    "node_to_dict",
    "replace_table",
]


def collect_nodes(syntax, predicate) -> list:
    """Walk the syntax tree; return every node where ``predicate(node)`` is truthy.

    Most analyzers in this module collapse to a single-pass collect-by-predicate
    walk. This helper hides the ``KustoWalker`` subclass boilerplate so a new
    analyzer is one lambda instead of a five-line class. The walker visits every
    node in source order; ``predicate`` is called once per node.

    Example:
        # every FilterOperator in the query, in source order
        filters = collect_nodes(syntax, lambda n: str(n.Kind) == "FilterOperator")
    """
    results = []

    class _Collector(KustoWalker):
        def pre_visit(self, node):
            if predicate(node):
                results.append(node)

    _Collector().visit(syntax)
    return results


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

# Every SyntaxKind that names a *token* — punctuation, keywords, identifiers.
# Derived once from the enum, as a closed set: substring-matching "Token" also
# catches ``TokenLiteralExpression`` (the value half of ``kind=inner``) and
# ``TokenName``, neither of which is a token. See :func:`get_structural_hash`.
_TOKEN_KINDS = frozenset(k for k in _syntax_kinds() if k.endswith("Token"))


def _is_token_kind(kind: str) -> bool:
    """True when ``kind`` names a token rather than a node.

    ``syntax_kinds()`` returns empty rather than raising if enum reflection
    fails; falling back to the suffix test keeps the hash's meaning stable
    instead of silently folding every token kind into it.
    """
    if _TOKEN_KINDS:
        return kind in _TOKEN_KINDS
    return kind.endswith("Token")  # pragma: no cover — reflection failed


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


def _is_wildcard_name(node) -> bool:
    """True when this NameReference spells a wildcard pattern (``T*``).

    ``union T*`` names a *set* of tables by pattern, and a pattern is not a
    table name: it cannot be looked up, and rewriting it would silently
    change which tables the query reads. Only the syntactic walk needs this
    test — a bound parse gets the binder's expansion instead, which is a
    ``GroupSymbol`` for two or more matches and the ``TableSymbol`` itself
    when exactly one table matches.
    """
    name = getattr(node, "Name", None)
    return name is not None and str(name.Kind) == "WildcardedName"


def _collect_table_refs(syntax) -> list:
    """Return every (name, NameReference node) that occupies a table-source
    position. Does NOT deduplicate — callers that want a set should dedupe
    themselves.

    Four kinds of name occupy a table-source position without being a table,
    and each is excluded here:

    * a name bound by ``let`` — but see the shadowing rule below;
    * a name bound by ``| as X``, which is visible for the rest of the query;
    * a table-typed parameter of a user-defined function, which is visible
      only inside that function's body (so the exclusion is scoped to it,
      and a real table sharing the parameter's name is still reported);
    * a wildcard pattern such as ``union T*``.

    **Shadowing.** In ``let T = T | where ...; T | take 1`` the right-hand
    side ``T`` is the real table: KQL evaluates a binding's RHS in the scope
    *outside* its own name, so a ``let`` cannot be recursive. Every later
    ``T`` is the alias. A flat name-keyed filter gets this exactly backwards
    and drops both, so the RHS occurrences of the name a statement is itself
    binding are recorded by source span and exempted from the filter. Names
    bound by *earlier* ``let`` statements are in scope on a RHS and stay
    excluded there.
    """
    let_vars = set()  # names bound by let statements visited so far
    exclude = set()  # `as` aliases — visible for the remainder of the query
    param_scopes = []  # (name, start, end) — function parameters, body-scoped
    unshadowed = set()  # (TextStart, Width) of let-RHS refs that are tables
    refs = []

    class Walker(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)

            if kind == "LetStatement":
                rhs = node.GetChild(3)
                if rhs is not None:
                    for ref in _unwrap_table_expr(rhs):
                        # `let_vars` here holds only the *earlier* bindings:
                        # this statement's own name is added below.
                        if node_name(ref) not in let_vars:
                            unshadowed.add((ref.TextStart, ref.Width))
                        refs.append(ref)
                name_node = node.GetChild(1)
                if name_node is not None:
                    let_vars.add(node_name(name_node))
                return

            if kind == "FunctionDeclaration":
                start = node.TextStart
                end = start + node.Width
                for param in collect_nodes(
                    node, lambda n: str(n.Kind) == "FunctionParameter"
                ):
                    name_and_type = getattr(param, "NameAndType", None)
                    if name_and_type is None:
                        continue
                    param_name = getattr(name_and_type, "Name", None)
                    if param_name is not None:
                        param_scopes.append((node_name(param_name), start, end))
                return

            if kind == "AsOperator":
                alias = getattr(node, "Name", None)
                if alias is not None:
                    exclude.add(node_name(alias))
                return

            if kind in ("PipeExpression", "ExpressionStatement"):
                refs.extend(_unwrap_table_expr(node.GetChild(0)))
                return

            if kind in ("JoinOperator", "LookupOperator", "FacetOperator"):
                expr = getattr(node, "Expression", None)
                if expr is not None:
                    refs.extend(_unwrap_table_expr(expr))
                return

            if kind == "UnionOperator":
                for i in range(node.ChildCount):
                    refs.extend(_unwrap_table_expr(node.GetChild(i)))
                return

            if kind in ("FindOperator", "SearchOperator"):
                # `find in (T1, T2) …` / `search in (T1, T2) …`. Without
                # this, `find in (S1, S2)` reported *no* tables at all, and
                # replace_table returned the query unchanged with no error.
                in_clause = getattr(node, "InClause", None)
                if in_clause is not None:
                    for el in iter_elements(in_clause.Expressions):
                        refs.extend(_unwrap_table_expr(el))

    Walker().visit(syntax)

    def _is_function_parameter(name: str, start: int) -> bool:
        return any(
            name == param and body_start <= start < body_end
            for param, body_start, body_end in param_scopes
        )

    out = []
    for ref in refs:
        if _is_wildcard_name(ref):
            continue
        name = node_name(ref)
        if not name or name in exclude:
            continue
        if name in let_vars and (ref.TextStart, ref.Width) not in unshadowed:
            continue
        if _is_function_parameter(name, ref.TextStart):
            continue
        out.append((name, ref))
    return out


def _collect_semantic_table_refs(syntax) -> list:
    """Return every (name, node) where node.ReferencedSymbol is a TableSymbol.

    Does NOT dedupe by name — callers that want a set should dedupe themselves.
    """
    refs = []
    for node in collect_nodes(syntax, lambda n: str(n.Kind) == "NameReference"):
        sym = node.ReferencedSymbol
        if sym is not None and isinstance(sym, TableSymbol):
            refs.append((sym.Name, node))
    return refs


def _merge_unresolved_table_refs(syntax) -> list:
    """Semantic table refs, plus the syntactic ones the binder left unresolved.

    A schema is almost always partial — a detection rule joins tables from
    workspaces the caller did not describe — and the binder resolves only
    what it was told about. Returning its answer alone means every unknown
    table silently disappears, which is the worst possible failure for
    ``replace_table``: it rewrites nothing and reports nothing.

    A syntactic ref is added when the binder produced no symbol for that
    exact node (``ReferencedSymbol is None``) and no semantic ref already
    occupies its span. Both lists hold ``NameReference`` nodes, which cannot
    nest, so two refs either share a span exactly or do not overlap at all —
    equality is full coverage, and there is no partial-overlap case to
    arbitrate. The syntactic side is the filtered walk of
    :func:`_collect_table_refs`, so ``let`` and ``as`` aliases, function
    parameters and wildcard patterns stay out of the union.
    """
    merged = _collect_semantic_table_refs(syntax)
    covered = {(node.TextStart, node.Width) for _, node in merged}
    for name, node in _collect_table_refs(syntax):
        span = (node.TextStart, node.Width)
        if span in covered or node.ReferencedSymbol is not None:
            continue
        covered.add(span)
        merged.append((name, node))
    merged.sort(key=lambda ref: (ref[1].TextStart, ref[1].Width))
    return merged


def find_table_references(kusto_code, force_syntactic: bool = False) -> list:
    """Return [(name, node), ...] for every table reference (one entry per
    occurrence), in source order. Use ``get_referenced_tables`` for a
    deduplicated set of names.

    On a bound parse the binder's own references are used, and the syntactic
    walk fills in the tables the supplied schema did not describe — those
    would otherwise vanish. Pass ``force_syntactic=True`` for the syntactic
    walk alone.
    """
    if not force_syntactic and kusto_code.HasSemantics:
        return _merge_unresolved_table_refs(kusto_code.Syntax)
    return _collect_table_refs(kusto_code.Syntax)


def get_tables_syntactic(kusto_code) -> set[str]:
    """Return tables found by the syntactic walk alone, ignoring the binder."""
    return {name for name, _ in _collect_table_refs(kusto_code.Syntax)}


def get_tables_semantic(kusto_code) -> set[str]:
    """Return tables resolved by the binder. Requires a bound KustoCode.

    Strictly the binder's answer: a table the supplied schema does not
    describe is *not* in this set. ``get_referenced_tables`` /
    :func:`find_table_references` add those back — prefer them unless you
    specifically want to know what resolved.
    """
    if not kusto_code.HasSemantics:
        raise ValueError(
            "get_tables_semantic requires a bound KustoCode "
            "(use parse(text, schema=...))."
        )
    return {name for name, _ in _collect_semantic_table_refs(kusto_code.Syntax)}


def get_operator_stats(kusto_code) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in collect_nodes(kusto_code.Syntax, lambda n: "Operator" in str(n.Kind)):
        kind = str(node.Kind)
        counts[kind] = counts.get(kind, 0) + 1
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
        for node in collect_nodes(
            kusto_code.Syntax, lambda n: str(n.Kind) == "NameReference"
        ):
            sym = node.ReferencedSymbol
            if sym is not None and isinstance(sym, ColumnSymbol):
                cols.add(sym.Name)
        return cols

    table_names = {name for name, _ in _collect_table_refs(kusto_code.Syntax)}
    let_vars = set()

    class LetCollector(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) == "LetStatement":
                name_node = node.GetChild(1)
                if name_node is not None:
                    let_vars.add(node_name(name_node))

    LetCollector().visit(kusto_code.Syntax)

    cols = set()

    class ColumnExtractor(KustoWalker):
        def pre_visit(self, node):
            if str(node.Kind) != "NameReference":
                return
            if _is_function_callee(node):
                return
            # A `union T*` pattern is not a column either, and it no longer
            # lands in `table_names` to be filtered out that way.
            if _is_wildcard_name(node):
                return
            name = node_name(node)
            if not name or name in table_names or name in let_vars:
                return
            # `$left` / `$right` and other `$`-prefixed names are KQL macros.
            if name.startswith("$"):
                return
            cols.add(name)

    ColumnExtractor().visit(kusto_code.Syntax)
    return cols


def get_referenced_functions(kusto_code, force_syntactic: bool = False) -> set[str]:
    """Return the set of function names called in the query.

    Semantic mode reads ``ReferencedSymbol`` and keeps only ``FunctionSymbol``
    references, so user-defined / let-bound functions resolve to their declared
    names and built-ins return their canonical names. Syntactic mode falls back
    to ``NameReference`` nodes that occupy a function-callee position — fast,
    no schema required, but cannot distinguish a built-in from a let-bound
    callable of the same name.
    """
    syntax = kusto_code.Syntax

    if not force_syntactic and kusto_code.HasSemantics:
        funcs = set()
        for node in collect_nodes(
            syntax, lambda n: str(n.Kind) == "NameReference"
        ):
            sym = node.ReferencedSymbol
            if sym is not None and isinstance(sym, FunctionSymbol):
                funcs.add(sym.Name)
        return funcs

    return {
        node_name(node)
        for node in collect_nodes(
            syntax,
            lambda n: str(n.Kind) == "NameReference" and _is_function_callee(n),
        )
    }


def _is_plugin_callee(node) -> bool:
    """True when this NameReference names the plug-in of an ``evaluate``.

    ``evaluate bag_unpack(d)`` parses as a plain ``FunctionCallExpression``
    directly under the ``EvaluateOperator``, so the plug-in name is an
    ordinary identifier in the tree with nothing to mark it as a plug-in.
    The grandparent's kind is what distinguishes it.
    """
    parent = node.Parent
    if parent is None or str(parent.Kind) != "FunctionCallExpression":
        return False
    if not _is_function_callee(node):
        return False
    grandparent = parent.Parent
    return grandparent is not None and str(grandparent.Kind) == "EvaluateOperator"


def get_structural_hash(kusto_code) -> str:
    """SHA256 over the AST shape — a "same query modulo the data" fingerprint.

    **Blind to** literal values (``x == 1`` and ``x == 5`` hash alike),
    identifiers (table, column and ordinary function names — ``Alpha | where
    beta == 1`` matches ``Gamma | where delta == 1``, and ``tolower(x)``
    matches ``toupper(x)``), whitespace, and comments. Not a
    logical-equivalence hash either: parenthesization and other cosmetic
    rewrites still change it.

    **Sensitive to** the keyword value of every named parameter and to the
    plug-in an ``evaluate`` names — ``join kind=inner`` versus
    ``kind=leftanti``, ``union kind=inner`` versus ``kind=outer``,
    ``evaluate bag_unpack(d)`` versus ``evaluate pivot(d)``. Those are not
    cosmetic: they select the operator's semantics, and folding them together
    made this hash claim two genuinely different queries were one shape.

    The exception inside that sensitivity is a named parameter whose value is
    an ordinary literal — ``union isfuzzy=true`` and ``isfuzzy=false``, or
    ``parse flags='i'`` and ``flags='m'`` — which stays invisible, because
    blindness to literals is the property this hash exists for. Only the
    enumerated-keyword values (a ``TokenLiteralExpression``) are kept.
    """
    parts = []

    class HashWalker(KustoWalker):
        def pre_visit(self, node):
            kind = str(node.Kind)
            if kind in _STRUCTURAL_NOISE_KINDS:
                return
            if _is_token_kind(kind):
                return
            if kind == "TokenLiteralExpression":
                # `kind=inner` / `hint.strategy=shuffle`: the value is one of a
                # closed set of keywords, not data.
                parts.append(f"{kind}:{node_text(node)}")
                return
            if kind == "NameReference" and _is_plugin_callee(node):
                parts.append(f"{kind}:{node_name(node)}")
                return
            parts.append(kind)

    HashWalker().visit(kusto_code.Syntax)
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def find_time_expressions(kusto_code) -> list[tuple[str, int, int]]:
    """Return ``[(text, start, length), ...]`` for every time-related expression
    in source order: time-function calls (``ago``, ``now``, ``bin``, ...) plus
    standalone datetime/timespan literals not already inside a matched call.

    A **discovery aid**, not a lookback extractor. The result is syntactic: it
    includes bare ``now()``, bare ``1h`` operands, and the operands of
    ``!between`` — with no indication of which bound a given expression is, or
    whether it constrains the query's time column at all. Resolving an
    effective time window additionally needs let-resolution, awareness of which
    column is temporal, and negation handling; build that on the tier-2 IR
    rather than on this list.
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
            if node_name(callee) not in _TIME_FUNCS:
                return
            start = node.TextStart
            end = start + node.Width
            fn_ranges.append((start, end))
            out.append((node_text(node), start, node.Width))

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
            out.append((node_text(node), start, node.Width))

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


def get_time_range(kusto_code) -> list[tuple[str, int, int]]:
    """Deprecated alias for :func:`find_time_expressions`.

    The old name promised a resolved range and returned a discovery list,
    which led callers to use it as a lookback extractor and get wrong answers.
    """
    warnings.warn(
        "get_time_range() is deprecated; use find_time_expressions(). It "
        "returns a source-ordered discovery list of time expressions — "
        "including bare now(), bare operands and !between operands — not a "
        "resolved time range.",
        DeprecationWarning,
        stacklevel=2,
    )
    return find_time_expressions(kusto_code)


def replace_table(kusto_code, old_name: str, new_name: str, force_syntactic: bool = False) -> str:
    """Rename every reference to ``old_name`` to ``new_name``; return the new text.

    Rewrites exactly the spans ``find_table_references`` reports, so the two
    always agree on what a table is. On a bound parse that includes tables
    the supplied schema does not describe — the binder cannot resolve them,
    but they are still in the query and still have to be retargeted. Names
    that only look like tables (``let`` and ``as`` aliases, function
    parameters, wildcard patterns) are left alone; so is a shadowed alias,
    while the binding's own right-hand side is rewritten.
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
