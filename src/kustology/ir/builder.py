# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Build a :class:`QueryIR` from Microsoft's parsed KustoCode.

Two entry points: :meth:`IRBuilder.build` (string in, IR out, parses+binds)
and :meth:`IRBuilder.build_from_code` (use when the caller already has a
``KustoCode``, e.g. via :meth:`kustology.KustoQuery.to_ir`).

The handled-SyntaxKind sets are exposed as :attr:`HANDLED_OPERATOR_KINDS`
and :attr:`HANDLED_EXPR_KINDS` for the coverage audit script.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..bridge import GlobalState, KustoCode  # re-export-friendly; also triggers CLR init

# One definition of the unknown-table diagnostic code, shared with
# ``validate(..., ignore_unknown_tables=True)``. Two spellings of "KS204" is
# exactly the drift a DLL refresh turns into a silent behaviour split.
from ..services import _UNKNOWN_TABLE_CODE

# Moved to Tier 1 so consumers walking the .NET tree can reach it without the
# [ir] extra. The private alias keeps this module's call sites untouched.
from ..utils.walker import iter_elements as _iter_elements
from ..utils.walker import node_text as _node_text
from ._builder_helpers import (
    extract_hints,
    extract_named_param,
    extract_qualified_table_ref,
    is_table_symbol,
    is_wildcarded_name,
    literal_kind_for,
    literal_value_and_ticks,
    map_semantic_info,
    read_external_data,
    read_named_params,
    read_row_schema,
    read_to_typeof,
    table_symbol_columns,
    to_span,
    visit_name,
)
from .expr import (
    And,
    AnyExpr,
    Between,
    BinOp,
    BracketedExpr,
    CaseExpr,
    ColumnRef,
    CompoundNamedExpr,
    ElementExpr,
    Exists,
    ExternalDataExpr,
    FuncCall,
    LetValueRef,
    LiteralExpr,
    NamedExpr,
    Not,
    Or,
    PathExpr,
    RegexMatch,
    SetMembership,
    StarExpr,
    SubqueryExpr,
    ToScalarExpr,
    TypedNameDecl,
    UnaryOp,
    UnknownExpr,
)
from .query import (
    AsOp,
    AssertSchemaOp,
    Assignment,
    ConsumeOp,
    CountOp,
    DataTableSource,
    Diagnostic,
    DistinctOp,
    EvaluateOp,
    ExecuteAndCacheOp,
    ExtendOp,
    ExternalDataSource,
    FacetOp,
    FilterOp,
    FindOp,
    ForkBranch,
    ForkOp,
    FuncCallSource,
    GetSchemaOp,
    GraphMarkComponentsOp,
    GraphMatchOp,
    GraphShortestPathsOp,
    GraphToTableOp,
    GraphWhereEdgesOp,
    GraphWhereNodesOp,
    ImplicitSource,
    InvokeOp,
    JoinOp,
    LetBinding,
    LetFunction,
    LetRef,
    LookupOp,
    MacroExpandOp,
    MakeGraphOp,
    MakeSeriesAggregate,
    MakeSeriesOp,
    MvApplyOp,
    MvExpandColumn,
    MvExpandOp,
    Operator,
    ParseKvOp,
    ParseOp,
    ParseWhereOp,
    PartitionOp,
    Pipeline,
    PrintOp,
    ProjectAwayOp,
    ProjectByNamesOp,
    ProjectKeepOp,
    ProjectOp,
    ProjectRenameOp,
    ProjectReorderOp,
    QueryIR,
    RangeOp,
    RenderOp,
    ReorderKey,
    SampleDistinctOp,
    SampleOp,
    ScanOp,
    SearchOp,
    SerializeOp,
    SortKey,
    SortOp,
    SummarizeOp,
    TableRef,
    TabularSchema,
    TakeOp,
    TopHittersOp,
    TopNestedOp,
    TopOp,
    UnionOp,
    UnknownOp,
    UnknownSource,
)
from .spans import Span
from .transforms import compute_semantic_hash

logger = logging.getLogger(__name__)

# Bridge import above already triggered AddReference("Kusto.Language").
#
# ``IncludeTrivia`` selects how much of the whitespace and comments around a
# node ``ToString()`` renders. The no-argument overload is ``All``, which
# prepends the node's *leading* trivia -- so a ``raw_text`` recorded that way
# carries the newline, indentation and ``// comment`` that happened to sit
# between the previous token and this one. ``Minimal`` renders the node's own
# source: no leading trivia, interior comments dropped to a line break.
from Kusto.Language.Syntax import (
    ExpressionStatement,
    IncludeTrivia,
    LetStatement,
)


def _is_time_func_name(name: str) -> bool:
    """True when ``name`` is a known KQL time function.

    Reflects ``Kusto.Language.Functions`` for the answer, then subtracts
    ``_NON_TEMPORAL_ARITHMETIC``. That subtraction is the same one
    ``utils.analysis`` applies to ``_TIME_FUNCS``, and it is shared rather
    than restated so this field and ``find_time_expressions()`` cannot drift:
    ``time_functions()`` classifies by return type, so ``abs`` is a member
    on the strength of ``abs(timespan)`` alone and would otherwise set
    ``FuncCall.is_time_func`` on ``abs(x)`` over a numeric column.

    Falls back to a substring check (``time``/``ago``/``now``) if reflection
    is unavailable.
    """
    try:
        from ..reflection import time_functions
        from ..utils.analysis import _NON_TEMPORAL_ARITHMETIC
        return name in time_functions() and name not in _NON_TEMPORAL_ARITHMETIC
    except Exception:  # pragma: no cover — defensive
        lower = name.lower()
        return "time" in lower or "ago" in lower or "now" in lower


# Non-operator .NET node kinds that put a ``let`` right-hand side in tabular
# position. Every *other* tabular RHS is a query-operator node, matched
# structurally by the ``…Operator`` suffix in ``_is_tabular_let_rhs`` — the
# suffix is the .NET class hierarchy's own marker for "this is a query
# operator", and query operators only ever appear in tabular position.
_TABULAR_LET_RHS_KINDS = frozenset({
    "PipeExpression",         # let A = T | where …
    "MaterializeExpression",  # let A = materialize(T | where …)
    "DataTableExpression",    # let A = datatable(a:int)[1, 2]
    "ExternalDataExpression", # let A = externaldata(a:string)["https://x"]
})


# The four KQL null/empty tests and the polarity each one carries. A
# mapping rather than a "does the name start with ``isnot``" rule: the set
# is closed and small, and a name test would silently claim any future
# ``isnotXxx`` function is a null test.
_NULL_TEST_POLARITY: dict[str, Literal["inclusion", "exclusion"]] = {
    "isnotnull": "inclusion",
    "isnotempty": "inclusion",
    "isnull": "exclusion",
    "isempty": "exclusion",
}


# The arithmetic operators. Neither case sensitivity nor polarity is a
# property of arithmetic -- both are categories of *comparison* -- so a
# ``BinOp`` built from one of these records ``None`` for both rather than
# whatever the comparison rules happen to return.
_ARITHMETIC_OPS = frozenset({"+", "-", "*", "/", "%"})


def _is_case_sensitive_op(op: str) -> bool | None:
    """Whether a KQL binary operator compares case-sensitively.

    ``None`` means the question does not apply: an arithmetic operator does
    not compare text at all, and reporting ``True`` for ``a + 1`` -- which is
    what the comparison fall-through did -- states a fact about a query that
    is not one.

    Derived from the operator's own suffix rather than an allow-list of
    members. The allow-list this replaced named six operators and let
    everything else fall through to ``True``, so it was already wrong for
    ``hasprefix`` and ``hassuffix`` before anyone negated anything, and every
    negated string operator (``!has``, ``!contains``, ``!startswith``,
    ``!endswith``, ``!hasprefix``, ``!hassuffix``) was reported backwards.
    A new operator from a DLL refresh would land wrong the same way.

    KQL's rule, in the order it has to be applied:

    * arithmetic -> ``None``, the question does not apply
    * ``_cs`` suffix -> sensitive (``has_cs``, ``!contains_cs``)
    * ``~`` suffix -> insensitive (``=~``, ``!~``)
    * ``:`` -> insensitive. ``search Col:'x'`` is Microsoft's documented
      shorthand for ``Col has 'x'``, a term match, and term matches fold
      case. It is spelled as none of the stems below, so it fell through to
      the comparison default and reported the two equivalent spellings as
      comparing differently.
    * string operators -> insensitive by default, negation included
    * everything else, i.e. the comparisons -> sensitive
    """
    if op in _ARITHMETIC_OPS:
        return None
    if op.endswith("_cs"):
        return True
    if op.endswith("~"):
        return False
    if op == ":":
        return False
    return not op.lstrip("!").startswith(_CASE_INSENSITIVE_OP_STEMS)


# Stems of the KQL string operators, which fold case unless suffixed ``_cs``.
# Matched as a prefix after stripping a leading ``!`` so negated and future
# suffixed forms are covered without listing each one.
_CASE_INSENSITIVE_OP_STEMS = (
    "has", "contains", "startswith", "endswith", "matches",
)


def _as_bool(value: str | None) -> bool:
    """Read a boolean named parameter (``isfuzzy=true``) as a Python bool.

    ``extract_named_param`` returns the parameter's rendered value, and for
    a boolean literal that is .NET's ``"True"`` / ``"False"`` rather than
    KQL's own lowercase spelling. Both are accepted, along with the ``1`` /
    ``0`` form the grammar also admits; anything else -- including an
    unwritten parameter -- is ``False``, which is the operator's behaviour
    when the flag is absent.
    """
    return value is not None and value.strip().lower() in ("true", "1")


def _is_tabular_let_rhs(net_kind: str) -> bool:
    """True when a ``let`` RHS of this .NET node kind is a tabular expression.

    Covers the operator-rooted forms (``union``/``range``/``search``/``print``/
    ``find``, plus any operator Microsoft adds later) as well as the four
    non-operator tabular kinds. Does *not* cover a bare ``NameReference`` —
    that is only tabular when the binder proves it, which the caller checks
    separately.

    ``ExternalDataExpression`` is one of the four. It used to be excluded,
    because there was no source class to build a pipeline around and routing
    it through ``_visit_pipeline`` would have manufactured an
    ``UnknownSource``. :class:`~kustology.ir.query.ExternalDataSource` is
    that class, so ``let X = externaldata(...)`` now lands on
    ``rhs_pipeline`` like every other tabular binding — and
    ``rhs_pipeline is not None`` is a reliable "is this binding tabular"
    test again.
    """
    return net_kind.endswith("Operator") or net_kind in _TABULAR_LET_RHS_KINDS


def _collect_inner_tables(pipeline: Any) -> list[str]:
    """Distinct table names inside a let binding's pipeline, in first-seen order.

    ``TableRef`` only, so a hop to an earlier binding does not appear here --
    that is a ``LetRef``. Use ``find_all(pipeline, LetRef)`` for the aliases.
    """
    from .query import TableRef
    from .walk import find_all

    seen: list[str] = []
    for ref in find_all(pipeline, TableRef):
        if ref.name not in seen:
            seen.append(ref.name)
    return seen


def _collect_inner_time_exprs(pipeline: Any) -> list[Any]:
    """Time-function calls inside a let binding's pipeline, in walk order.

    Time *literals* are reachable through ``rhs_pipeline`` already; this
    surfaces the calls (``ago``, ``now``, ``bin``, ...) that a lookback
    analyzer needs to find without re-walking the tree.
    """
    from .expr import FuncCall
    from .walk import find_all

    return [fc for fc in find_all(pipeline, FuncCall) if fc.is_time_func]


class IRBuilder:
    """Builds a :class:`QueryIR` from a Microsoft Kusto syntax tree.

    Dispatch tables are explicit (sets, not method-ref dicts) so the audit
    script can read them statically without instantiating the builder.
    """

    HANDLED_OPERATOR_KINDS = frozenset({
        "FilterOperator", "ExtendOperator", "SummarizeOperator", "JoinOperator",
        "LookupOperator", "PartitionByOperator", "PartitionOperator", "ProjectOperator",
        "ProjectAwayOperator", "ProjectKeepOperator", "ProjectReorderOperator",
        "ProjectRenameOperator", "ProjectByNamesOperator", "DistinctOperator",
        "TakeOperator", "SampleOperator", "SortOperator", "TopOperator",
        "TopHittersOperator", "SearchOperator", "UnionOperator",
        "MakeSeriesOperator", "MvExpandOperator", "MvApplyOperator",
        "ParseOperator", "ParseWhereOperator", "AsOperator", "RangeOperator",
        "RenderOperator", "EvaluateOperator",
        "CountOperator", "PrintOperator",
        "FacetOperator", "GetSchemaOperator", "InvokeOperator", "FindOperator",
        "ForkOperator", "ScanOperator", "SerializeOperator", "ConsumeOperator",
        "AssertSchemaOperator", "ExecuteAndCacheOperator", "ParseKvOperator",
        "SampleDistinctOperator", "TopNestedOperator", "MakeGraphOperator",
        "MacroExpandOperator", "GraphMatchOperator",
        "GraphMarkComponentsOperator", "GraphShortestPathsOperator",
        "GraphToTableOperator", "GraphWhereEdgesOperator",
        "GraphWhereNodesOperator",
    })

    HANDLED_EXPR_KINDS = frozenset({
        "ParenthesizedExpression", "NameReference", "NameDeclaration",
        "NameAndTypeDeclaration", "TypedColumnReference",
        "PathExpression", "ElementExpression",
        "SimpleNamedExpression", "CompoundNamedExpression", "BracketedExpression",
        "PrefixUnaryExpression", "StarExpression", "LiteralExpression",
        "CompoundStringLiteralExpression",
        "DynamicExpression",
        "BinaryExpression",
        "OrderedExpression",
        # OrderedExpression is handled, but not by ``_visit_expr``: it is an
        # ordering key, not an expression, and lowering it to its bare inner
        # expression is what threw away ``asc``/``desc`` and ``nulls
        # first``/``last``. It stays listed for the same reason
        # MaterializeExpression below does -- the kind *is* modelled, and
        # dropping it would make the coverage audit report a fully-handled
        # shape as unhandled.
        #
        # It has FOUR owners, and the count matters: deleting the
        # ``_visit_expr`` branch for the sake of the first two silently
        # regressed the third to ``UnknownExpr``, losing the column identity
        # a bare ``project-reorder x`` keeps. Enumerated by parsing each
        # construct and walking to the nearest ``*Operator`` ancestor of every
        # ``OrderedExpression`` in the tree:
        #
        #   SortOperator (``sort by`` / ``order by``)  -> ``_visit_sort_key``
        #   TopOperator (``top N by``)                 -> ``_visit_sort_key``
        #   ProjectReorderOperator                     -> ``_visit_reorder_key``
        #   TopNestedOperator (``top-nested … by x asc``)
        #       -> not visited at all. ``TopNestedOp`` is one of the
        #          preserve-raw-text operators, so the whole operator is
        #          recorded as source text and no child expression, ordered
        #          or otherwise, is ever handed to ``_visit_expr``.
        #
        # Add a fifth owner and it needs a case here, or it lands on
        # ``UnknownExpr`` while this list claims otherwise.
        "InExpression", "HasAnyExpression", "HasAllExpression",
        "BetweenExpression", "FunctionCallExpression", "MaterializeExpression",
        # MaterializeExpression is handled, but by ``_visit_pipeline`` --
        # ``materialize`` is a keyword the grammar admits only as a ``let``
        # right-hand side, where ``_TABULAR_LET_RHS_KINDS`` routes it to a
        # nested ``Pipeline``. It stays listed because the kind *is* modelled;
        # dropping it would make the coverage audit report a fully-handled
        # shape as unhandled.
        "ToScalarExpression", "PipeExpression", "ExternalDataExpression",
        "DataTableExpression",
        # DataTableExpression is handled by ``_visit_pipeline`` rather than
        # ``_visit_expr``: ``datatable`` is a tabular literal, so it only
        # ever occupies source position (its own or a ``let`` right-hand
        # side's). It joins MaterializeExpression and ForkExpression above
        # in being modelled somewhere other than the expression visitor.
        "MakeSeriesExpression",
        "ForkExpression",
        # ForkExpression joins the same two: handled, but by the
        # ``ForkOperator`` branch of ``_visit_operator``, which reads its
        # ``NameEquals`` into ``ForkBranch.name`` and its ``Expression`` into
        # a nested ``Pipeline``. It was listed as *unhandled* for as long as
        # the branches were empty, which was accurate then and is not now.
    })

    def __init__(self, global_state: GlobalState | None = None):
        self.global_state = global_state or GlobalState.Default
        # Names bound by ``let`` statements already visited in the current
        # build. Reset per build so a reused builder cannot leak names
        # between queries. See ``_visit_pipeline``'s source-position branch.
        self._let_names: set[str] = set()

    # -- entry points ----------------------------------------------------

    def build(self, query: str) -> QueryIR:
        """Parse, bind, build. Use ``build_from_code`` when the caller already
        has a ``KustoCode``.

        Binding happens against ``self.global_state``, which defaults to
        ``GlobalState.Default`` — a state that describes Kusto's built-in
        functions and no tables at all. Every table the query names is
        therefore "unknown" to it, so the ``KS204`` those bindings raise
        describes how the IR was built rather than anything the caller wrote,
        and is filtered out (:data:`_UNKNOWN_TABLE_CODE`). A caller who
        supplied a real ``global_state`` and wants the unknown-table rows
        should call :meth:`build_from_code` directly, which keeps them.
        """
        code = KustoCode.ParseAndAnalyze(query, self.global_state)
        return self.build_from_code(code, ignore_unknown_tables=True)

    def build_from_code(
        self, code: KustoCode, *, ignore_unknown_tables: bool = False,
    ) -> QueryIR:
        """Build the IR from an already-parsed ``KustoCode``.

        ``ignore_unknown_tables`` drops ``KS204`` ("the name X does not refer
        to any known table") from :attr:`QueryIR.diagnostics`. Set it when
        the binding was done against globals the *caller* never chose — the
        schemaless paths, :meth:`build` and
        :meth:`kustology.KustoQuery.to_ir` on an unbound parse, both analyze
        against ``GlobalState.Default`` purely to get literal and built-in
        types, and reporting every table in the query as missing would be an
        artifact of that. A parse the caller bound with their own schema
        keeps the diagnostic: there, a table the schema does not describe is
        a real error.
        """
        raw_text = str(code.Text)

        diagnostics: list[Diagnostic] = []
        for diag in code.GetDiagnostics():
            code_val: str | None = None
            category_val: str | None = None
            try:
                if diag.Code:
                    code_val = str(diag.Code)
            except Exception as e:  # pragma: no cover
                logger.debug("diagnostic Code probe fell through: %s", e)
            if ignore_unknown_tables and code_val == _UNKNOWN_TABLE_CODE:
                continue
            try:
                if diag.Category:
                    category_val = str(diag.Category)
            except Exception as e:  # pragma: no cover
                logger.debug("diagnostic Category probe fell through: %s", e)
            diagnostics.append(Diagnostic(
                message=str(diag.Message),
                severity=str(diag.Severity),
                span=Span(text_start=diag.Start, width=diag.Length),
                code=code_val,
                category=category_val,
            ))

        root = code.Syntax
        self._let_names = set()
        let_bindings: list[LetBinding] = []
        for ls in root.GetDescendants[LetStatement]():
            binding = self._visit_let_statement(ls)
            let_bindings.append(binding)
            # Registered only *after* its own right-hand side is visited, so
            # ``let A = A | where …`` and a reference to a binding declared
            # further down still name whatever the cluster has. Resolving
            # those to the binding would be a guess, not a reading.
            self._let_names.add(binding.name)

        main_pipeline: Pipeline | None = None
        # Every tabular statement, not just the first. ``T | count; U | count``
        # used to build exactly the IR of ``T | count`` -- same nodes, same
        # ``semantic_hash`` -- with the second statement unreachable through
        # ``walk``/``find_all``. A function body is not in this list: its
        # tabular expression hangs off the ``FunctionBody`` rather than being
        # an ``ExpressionStatement``, so ``let f = (x:long) { T | where a > x
        # }; T | count`` still reports exactly one.
        additional_pipelines: list[Pipeline] = []
        expr_stmts = root.GetDescendants[ExpressionStatement]()
        if expr_stmts is not None and expr_stmts.Count > 0:
            main_pipeline = self._visit_pipeline(expr_stmts[0].Expression)
            for i in range(1, expr_stmts.Count):
                additional_pipelines.append(
                    self._visit_pipeline(expr_stmts[i].Expression)
                )
        if not main_pipeline:
            main_pipeline = self._visit_pipeline(root)

        ir = QueryIR(
            raw_text=raw_text,
            semantic_hash="",  # populated below from the canonical IR shape
            let_bindings=let_bindings,
            main_pipeline=main_pipeline,
            additional_pipelines=additional_pipelines,
            diagnostics=diagnostics,
        )
        ir.semantic_hash = compute_semantic_hash(ir)
        return ir

    # -- let statements --------------------------------------------------

    def _visit_let_statement(self, ls: Any) -> LetBinding:
        """Build one :class:`LetBinding` from a .NET ``LetStatement``.

        ``ls.Expression`` carries the right-hand side and its .NET class says
        which shape it is:

        * ``FunctionDeclaration``  -> ``rhs_function``
        * any tabular kind (see :func:`_is_tabular_let_rhs`) -> ``rhs_pipeline``
        * a ``NameReference`` the binder resolved to a table -> ``rhs_pipeline``
        * anything else            -> ``rhs_expr``

        Parentheses are unwrapped first. ``let A = (T | where …)`` is the
        dominant Sentinel idiom and arrives as a ``ParenthesizedExpression``
        wrapping the ``PipeExpression``; dispatching on the wrapper's class
        would drop the whole subtree into ``rhs_expr`` as an ``UnknownExpr``.
        Unwrapping is safe for the scalar path too — ``_visit_expr`` unwraps
        parens itself, so ``let m = (toscalar(...))`` still yields a
        ``ToScalarExpr``.

        A bare ``NameReference`` is only tabular when the binder can prove it
        (``let A = OtherTable`` with a schema). Unbound, it stays an
        expression rather than guessing a table into existence.
        """
        name = visit_name(ls.Name)
        span = to_span(ls)
        expr = getattr(ls, "Expression", None)
        while expr is not None and str(type(expr).__name__) == "ParenthesizedExpression":
            expr = getattr(expr, "Expression", None)
        if expr is None:  # pragma: no cover — defensive
            return LetBinding(name=name, span=span)

        net_kind = str(type(expr).__name__)

        if net_kind == "FunctionDeclaration":
            return LetBinding(
                name=name,
                span=span,
                rhs_function=self._visit_function_declaration(expr),
            )

        if _is_tabular_let_rhs(net_kind) or (
            net_kind == "NameReference"
            and is_table_symbol(getattr(expr, "ReferencedSymbol", None))
        ):
            pipeline = self._visit_pipeline(expr)
            return LetBinding(
                name=name,
                span=span,
                rhs_pipeline=pipeline,
                inner_tables=_collect_inner_tables(pipeline),
                inner_time_exprs=_collect_inner_time_exprs(pipeline),
            )

        return LetBinding(name=name, span=span, rhs_expr=self._visit_expr(expr))

    def _visit_function_declaration(self, node: Any) -> LetFunction:
        """Extract parameter names and the body's span from a FunctionDeclaration.

        ``node.Parameters`` is a ``FunctionParameters`` wrapper whose own
        ``.Parameters`` is a ``SyntaxList[SeparatedElement[FunctionParameter]]``
        — hence the unwrap.
        """
        params: list[str] = []
        outer = getattr(node, "Parameters", None)
        inner = getattr(outer, "Parameters", None) if outer is not None else None
        if inner is not None:
            for param in _iter_elements(inner):
                name_and_type = getattr(param, "NameAndType", None)
                if name_and_type is not None:
                    params.append(visit_name(name_and_type.Name))
        return LetFunction(parameters=params, body_span=to_span(node.Body))

    # -- pipeline / operator dispatch ------------------------------------

    def _visit_pipeline(self, node: Any) -> Pipeline:
        operators: list[Any] = []
        # The placeholder records the node's own text rather than the literal
        # string "unknown". A shape the builder cannot model is exactly the
        # one whose provenance a consumer needs, and a constant made every
        # unmodelled source hash the same -- the reason ``UnknownExpr`` and
        # ``UnknownOp`` have carried their text all along.
        source: (
            TableRef | LetRef | FuncCallSource | DataTableSource
            | ExternalDataSource | ImplicitSource | UnknownSource | Pipeline
        ) = UnknownSource(raw_text=_node_text(node), span=to_span(node))

        def walk(n: Any) -> None:
            nonlocal source
            if not n:
                return
            kind = str(type(n).__name__)

            if kind == "PipeExpression":
                walk(n.Expression)
                walk(n.Operator)
                return

            if kind == "ParenthesizedExpression":
                # `join (T)` and `join (T | where X)` arrive wrapped in parens;
                # without unwrapping the RHS pipeline gets UnknownSource.
                walk(n.Expression)
                return

            if kind == "ForkExpression":
                # `fork`'s own branch handling descends to `.Expression`
                # before calling here, so this is the belt to that braces:
                # falling through on a `ForkExpression` is precisely the bug
                # that left every fork branch empty, and any future caller
                # that reaches one by another route gets the operators rather
                # than silence. The `a=` name is not readable from here --
                # a `Pipeline` has nowhere to put it -- so it is recorded by
                # `_visit_operator`'s `ForkOperator` branch, which is why
                # that branch does not route through this case.
                walk(n.Expression)
                return

            if kind == "MaterializeExpression":
                # `materialize(P)` at source position becomes a nested Pipeline.
                if isinstance(source, UnknownSource):
                    source = self._visit_pipeline(n.Expression)
                return

            if kind == "PathExpression" and isinstance(source, UnknownSource):
                # `cluster("c").database("d").T` / `database("d").T` /
                # `database("d").*`. The qualifiers are part of what the
                # query reads, so they ride on the TableRef rather than
                # being dropped on the way to the bare name.
                qualified = extract_qualified_table_ref(n)
                if qualified is not None and qualified[2]:
                    source = self._visit_table_ref(n)
                    return

            if kind == "FunctionCallExpression":
                # User-defined table-valued function at source position
                # (e.g. `findAnomalies(field) | summarize ...`).
                if isinstance(source, UnknownSource):
                    name = "unknown"
                    name_node = getattr(n, "Name", None)
                    if name_node is not None:
                        if hasattr(name_node, "SimpleName"):
                            name = str(name_node.SimpleName)
                        else:
                            name = visit_name(name_node)
                    args: list[AnyExpr] = []
                    arg_list = getattr(n, "ArgumentList", None)
                    if arg_list is not None and hasattr(arg_list, "Expressions"):
                        for el in _iter_elements(arg_list.Expressions):
                            args.append(self._visit_expr(el))
                    source = FuncCallSource(name=name, args=args, span=to_span(n))
                return

            if kind == "DataTableExpression":
                # `datatable(schema)[values]` — inline tabular literal. The
                # values are the query, so they are modeled rather than
                # collapsed into an argument-less FuncCallSource.
                if isinstance(source, UnknownSource):
                    source = self._visit_datatable(n)
                return

            if kind == "ExternalDataExpression":
                # `externaldata(schema)[uris] with (...)` at source position.
                if isinstance(source, UnknownSource):
                    columns, uris, fmt, props = read_external_data(n)
                    source = ExternalDataSource(
                        columns=columns, uris=uris, format=fmt,
                        properties=props, span=to_span(n),
                    )
                return

            if kind.endswith("Operator"):
                op = self._visit_operator(n)
                if op:
                    operators.append(op)
                return

            if (kind in ("TableReference", "NameReference") or "Reference" in kind) and isinstance(
                source, UnknownSource
            ):
                ref = self._visit_table_ref(n)
                if ref.name.lower() not in (
                    "and", "or", "in", "in~", "has", "has_any", "not", "search",
                ):
                    source = ref

        walk(node)
        # Operators-but-no-explicit-source means the source is implicit (parent
        # rows: union-at-root, mv-apply/partition/fork subqueries, join RHS).
        if isinstance(source, UnknownSource) and operators:
            source = ImplicitSource(span=to_span(node))
        pipeline = Pipeline(source=source, operators=operators)
        # ``node`` is the whole pipe chain, so its own ``ResultType`` is the
        # last operator's -- or, for a source-only pipeline, the source's.
        # One read therefore covers both, and it cannot drift from the
        # per-operator reads above because it is literally the same symbol.
        # ``SchemaAttacher`` recomputes the same value; the point of setting
        # it here is that ``to_ir(attach_schema=False)`` gets the output
        # shape without paying for the provenance pass.
        columns = table_symbol_columns(getattr(node, "ResultType", None))
        if columns is not None:
            pipeline.result_schema = TabularSchema(columns=columns)
        return pipeline

    def _visit_table_ref(self, node: Any) -> TableRef | LetRef:
        """A table named in a *naming* position, wherever the grammar puts one.

        Three positions share this reading and used to read it three ways:
        the pipeline's own source, ``search in (A, B)`` and
        ``find in (T, U)``. The last two recorded a bare string -- ``find``
        with ``el.ToString().strip()``, which is ``IncludeTrivia.All``, so
        ``find in (// note`` ↵ ``T)`` hashed differently from ``find in (T)``
        -- and neither could express a qualifier, a wildcard or a ``let``
        alias, all three of which change what the query reads.

        The three distinctions this preserves are the ones
        :class:`~kustology.ir.query.TableRef` documents: ``database('d').T``
        names a table in another database, ``T*`` names a *set* of tables
        where ``['T*']`` names one table called that, and a name an earlier
        ``let`` bound is an alias rather than anything the cluster holds.
        """
        span = to_span(node)
        if str(type(node).__name__) == "PathExpression":
            qualified = extract_qualified_table_ref(node)
            if qualified is not None and qualified[2]:
                cluster, database, tbl, is_wildcard = qualified
                return TableRef(
                    name=tbl,
                    database=database,
                    cluster=cluster,
                    is_wildcard=is_wildcard,
                    span=span,
                )
        name_node = getattr(node, "Name", None)
        if name_node is not None:
            name = visit_name(name_node)
        elif hasattr(node, "SimpleName"):
            name = str(node.SimpleName).strip()
        else:
            # ``node_text`` (``IncludeTrivia.Minimal``) rather than
            # ``ToString()``: the no-argument overload prepends the node's
            # leading trivia, which puts a preceding comment in the table
            # name and from there into ``semantic_hash``.
            name = _node_text(node).strip()
        if name in self._let_names:
            return LetRef(name=name, span=span)
        return TableRef(
            name=name, is_wildcard=is_wildcarded_name(name_node), span=span,
        )

    def _visit_datatable(self, node: Any) -> DataTableSource:
        """Build a :class:`DataTableSource` from a ``DataTableExpression``.

        ``node.Values`` is a *flat* ``SyntaxList`` — the parser imposes no
        row structure on it, so the rows come from chunking it by the
        schema's column count. A trailing partial chunk is kept rather than
        discarded: the query is malformed (the parser says so with its own
        diagnostic) and dropping the values would hide what it wrote.

        The schema comes from :func:`read_row_schema`, the single reader for
        every ``name:type`` list in the grammar — see its docstring for why
        that is shared rather than copied.
        """
        columns = read_row_schema(getattr(node, "Schema", None))
        cells = [self._visit_expr(el) for el in _iter_elements(node.Values)]
        width = len(columns) or len(cells) or 1
        rows = [cells[i:i + width] for i in range(0, len(cells), width)]
        return DataTableSource(columns=columns, rows=rows, span=to_span(node))

    def _visit_operator(self, node: Any) -> Operator | None:
        """Dispatch one operator and stamp the ``hint.*`` parameters on it.

        The hints are read here rather than in each branch on purpose. Many
        operators admit them (``join``, ``summarize``, ``mv-expand``,
        ``partition``, ``evaluate``, …), the reading is identical for all of
        them, and a per-branch call is a list to maintain -- the shape
        AGENTS.md records as drifting every time. One call site cannot miss
        an operator, including one added later.

        The same argument decides where Microsoft's post-operator schema is
        read. ``<operator node>.ResultType`` is the columns the operator
        emits, and every operator has one; wrapping the dispatch rather than
        touching 53 branches means a new operator gets the binder's answer
        for free instead of getting a hand-written rule.
        """
        op = self._dispatch_operator(node)
        if op is not None:
            hints = extract_hints(node)
            if hints:
                op.hints = hints
            columns = table_symbol_columns(getattr(node, "ResultType", None))
            if columns is not None:
                op.result_schema = TabularSchema(columns=columns)
        return op

    def _dispatch_operator(self, node: Any) -> Operator | None:
        kind = str(type(node).__name__)
        span = to_span(node)
        n = node

        if kind == "FilterOperator":
            return FilterOp(predicate=self._visit_expr(n.Condition), span=span)

        if kind == "ExtendOperator":
            assigns = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    assigns.append(self._visit_assignment(el))
            return ExtendOp(assignments=assigns, span=span)

        if kind == "SummarizeOperator":
            aggs = []
            if hasattr(n, "Aggregates"):
                for el in _iter_elements(n.Aggregates):
                    aggs.append(self._visit_assignment(el, mode="aggregation"))
            by = []
            if hasattr(n, "ByClause") and n.ByClause and hasattr(n.ByClause, "Expressions"):
                for el in _iter_elements(n.ByClause.Expressions):
                    by.append(self._visit_expr_as_assignment(el, mode="grouping"))
            return SummarizeOp(aggregations=aggs, by=by, span=span)

        if kind == "JoinOperator":
            rhs = self._visit_pipeline(n.Expression)
            on_exprs: list[AnyExpr] = []
            if hasattr(n, "ConditionClause") and n.ConditionClause:
                cc = n.ConditionClause
                cc_kind = str(type(cc).__name__)
                if cc_kind == "JoinOnClause":
                    for expr_node in _iter_elements(cc.Expressions):
                        on_exprs.append(self._visit_expr(expr_node))
            return JoinOp(
                # KQL's effective default is ``innerunique``, not ``inner``
                # -- a different operator. See JoinOp.
                join_kind=extract_named_param(n, "kind", default="innerunique")
                or "innerunique",
                right=rhs,
                on=on_exprs,
                span=span,
            )

        if kind == "LookupOperator":
            rhs = self._visit_pipeline(n.Expression)
            on_exprs = []
            # On-clause surfaces as `LookupClause` or `ConditionClause` by build.
            cc = getattr(n, "LookupClause", None) or getattr(n, "ConditionClause", None)
            if cc and str(type(cc).__name__) == "JoinOnClause":
                for expr_node in _iter_elements(cc.Expressions):
                    on_exprs.append(self._visit_expr(expr_node))
            return LookupOp(
                # KQL's effective default -- see LookupOp.
                lookup_kind=extract_named_param(n, "kind", default="leftouter")
                or "leftouter",
                right=rhs,
                on=on_exprs,
                span=span,
            )

        if kind == "PartitionByOperator":
            # The partition key is ``Entity``, not ``Expression`` -- the
            # latter is a member of no PartitionByOperator, so reading it
            # raised AttributeError on every `__partitionby` query.
            return PartitionOp(
                by=self._visit_expr(n.Entity),
                right=self._visit_pipeline(n.Subquery),
                span=span,
            )

        if kind == "PartitionOperator":
            # `partition [hint.strategy=…] by C (...)`. AST exposes ByExpression
            # and Operand.Subquery (PartitionSubquery → .Subquery). Hints in
            # Parameters are not surfaced by PartitionOp.
            by_node = getattr(n, "ByExpression", None)
            operand = getattr(n, "Operand", None)
            sub_node = getattr(operand, "Subquery", None) if operand is not None else None
            return PartitionOp(
                by=self._visit_expr(by_node) if by_node is not None else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None", reason="Missing partition by",
                ),
                right=self._visit_pipeline(sub_node) if sub_node is not None else Pipeline(
                    source=UnknownSource(raw_text="?", span=span), operators=[],
                ),
                span=span,
            )

        if kind == "ProjectOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr_as_assignment(el))
            return ProjectOp(columns=cols, span=span)

        if kind == "ProjectAwayOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            return ProjectAwayOp(columns=cols, span=span)

        if kind == "ProjectKeepOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            return ProjectKeepOp(columns=cols, span=span)

        if kind == "ProjectReorderOperator":
            reorder_cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    reorder_cols.append(self._visit_reorder_key(el))
            return ProjectReorderOp(columns=reorder_cols, span=span)

        if kind == "ProjectRenameOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_assignment(el))
            return ProjectRenameOp(columns=cols, span=span)

        if kind == "ProjectByNamesOperator":
            names = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    names.append(self._visit_expr(el))
            return ProjectByNamesOp(names=names, span=span)

        if kind == "DistinctOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr_as_assignment(el))
            return DistinctOp(columns=cols, span=span)

        if kind == "TakeOperator":
            return TakeOp(count=self._visit_count(n.Expression), span=span)

        if kind == "SampleOperator":
            return SampleOp(count=self._visit_count(n.Expression), span=span)

        if kind == "SortOperator":
            keys = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    keys.append(self._visit_sort_key(el))
            return SortOp(expressions=keys, span=span)

        if kind == "TopOperator":
            return TopOp(
                count=self._visit_count(n.Expression),
                by=self._visit_sort_key(n.ByExpression),
                span=span,
            )

        if kind == "TopHittersOperator":
            # `top-hitters N of C [by V]` spreads over three members --
            # Expression (N), OfExpression (C) and ByClause (whose
            # .Expression is V). ``ValueExpression`` is a member of no node
            # in the assembly; reading it raised AttributeError. ByClause is
            # a plain None when the optional `by` is absent.
            by_clause = n.ByClause
            return TopHittersOp(
                count=self._visit_count(n.Expression),
                of=self._visit_expr(n.OfExpression),
                by=self._visit_expr(by_clause.Expression) if by_clause is not None else None,
                span=span,
            )

        if kind == "SearchOperator":
            # ``in (A, B)`` scopes the search to those tables; without it the
            # search covers the whole database, which is a different query.
            search_tables: list[Any] = []
            search_in = getattr(n, "InClause", None)
            if search_in is not None and hasattr(search_in, "Expressions"):
                for el in _iter_elements(search_in.Expressions):
                    search_tables.append(self._visit_table_ref(el))
            return SearchOp(
                predicate=self._visit_expr(n.Condition) if hasattr(n, "Condition") else None,
                # KQL's effective default, never None -- see SearchOp.
                search_kind=extract_named_param(n, "kind", default="default")
                or "default",
                tables=search_tables,
                span=span,
            )

        if kind == "UnionOperator":
            pipes = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    pipes.append(self._visit_pipeline(el))
            return UnionOp(
                pipelines=pipes,
                # KQL's effective default, never None -- see UnionOp.
                union_kind=extract_named_param(n, "kind", default="outer") or "outer",
                is_fuzzy=_as_bool(extract_named_param(n, "isfuzzy")),
                withsource=extract_named_param(n, "withsource"),
                span=span,
            )

        if kind == "MakeSeriesOperator":
            aggs = []
            if hasattr(n, "Aggregates"):
                for el in _iter_elements(n.Aggregates):
                    # Aggregates arrive as MakeSeriesExpression wrapping the
                    # actual SimpleNamedExpression (Count = count()) plus the
                    # `default=` clause. Unwrapping to `.Expression` -- what
                    # this branch used to do -- dropped the default, so a
                    # series gap-filled with 0 and one gap-filled with 1
                    # built the same node.
                    inner = getattr(el, "Expression", el)
                    assign = self._visit_assignment(inner, mode="aggregation")
                    default_clause = getattr(el, "DefaultExpression", None)
                    default_expr = (
                        getattr(default_clause, "Expression", None)
                        if default_clause is not None else None
                    )
                    aggs.append(MakeSeriesAggregate(
                        name=assign.name,
                        expr=assign.expr,
                        default=(
                            self._visit_expr(default_expr)
                            if default_expr is not None else None
                        ),
                        span=to_span(el),
                    ))
            by = []
            if hasattr(n, "ByClause") and n.ByClause:
                for el in _iter_elements(n.ByClause.Expressions):
                    by.append(self._visit_assignment(el, mode="grouping"))
            on_col = None
            on_clause = getattr(n, "OnClause", None)
            if on_clause is not None:
                on_expr = getattr(on_clause, "Expression", None)
                if on_expr is not None:
                    on_col = self._visit_expr(on_expr)
            r_from = r_to = r_step = None
            range_clause = getattr(n, "RangeClause", None)
            if range_clause is not None and str(
                type(range_clause).__name__
            ) == "MakeSeriesInRangeClause":
                # `in range(from, to, step)` is a *different clause class*
                # from `from … to … step …`, with the three bounds as
                # positional `Arguments` rather than named sub-clauses. Only
                # the second shape was read, so every `in range(...)` query
                # recorded no bounds at all -- and two series over different
                # windows hashed alike.
                arguments = getattr(range_clause, "Arguments", None)
                exprs = getattr(arguments, "Expressions", None) if arguments is not None else None
                if exprs is not None:
                    bounds = [self._visit_expr(el) for el in _iter_elements(exprs)]
                    r_from = bounds[0] if len(bounds) > 0 else None
                    r_to = bounds[1] if len(bounds) > 1 else None
                    r_step = bounds[2] if len(bounds) > 2 else None
            elif range_clause is not None:
                fc = getattr(range_clause, "MakeSeriesFromClause", None)
                if fc is not None and getattr(fc, "Expression", None) is not None:
                    r_from = self._visit_expr(fc.Expression)
                tc = getattr(range_clause, "MakeSeriesToClause", None)
                if tc is not None and getattr(tc, "Expression", None) is not None:
                    r_to = self._visit_expr(tc.Expression)
                sc = getattr(range_clause, "MakeSeriesStepClause", None)
                if sc is not None and getattr(sc, "Expression", None) is not None:
                    r_step = self._visit_expr(sc.Expression)
            return MakeSeriesOp(
                aggregations=aggs, by=by, on_column=on_col,
                range_from=r_from, range_to=r_to, step=r_step,
                span=span,
            )

        if kind == "MvExpandOperator":
            # Each element is an ``MvExpandExpression``: the column plus its
            # optional ``to typeof(...)``. Unwrapping to ``.Expression`` --
            # what this branch used to do -- discarded the declared element
            # type, and the operator's own modifiers (``limit``,
            # ``with_itemindex``, ``bagexpansion``, ``kind``) were never read
            # at all, so six different queries built one node.
            mv_cols: list[Any] = []
            if hasattr(n, "Expressions"):
                for mve in _iter_elements(n.Expressions):
                    mv_cols.append(MvExpandColumn(
                        expression=self._visit_expr(mve.Expression),
                        to_typeof=read_to_typeof(mve),
                        span=to_span(mve),
                    ))
            row_limit_clause = getattr(n, "RowLimitClause", None)
            limit_node = (
                getattr(row_limit_clause, "RowLimit", None)
                if row_limit_clause is not None else None
            )
            return MvExpandOp(
                columns=mv_cols,
                row_limit=self._visit_count(limit_node) if limit_node is not None else None,
                with_item_index=extract_named_param(n, "with_itemindex"),
                # One field for two spellings of one modifier, defaulting to
                # KQL's effective ``bag`` -- see MvExpandOp. ``kind`` is read
                # first so a query writing both records the modern spelling.
                expand_kind=(
                    extract_named_param(n, "kind")
                    or extract_named_param(n, "bagexpansion")
                    or "bag"
                ),
                span=span,
            )

        if kind == "MvApplyOperator":
            assigns = []
            if hasattr(n, "Expressions"):
                for mve in _iter_elements(n.Expressions):
                    assigns.append(self._visit_assignment(mve.Expression))
            # n.Subquery wraps the real pipe/operator at .Expression.
            sub = getattr(n, "Subquery", None)
            inner = getattr(sub, "Expression", sub) if sub is not None else None
            return MvApplyOp(
                assignments=assigns,
                right=self._visit_pipeline(inner) if inner is not None else Pipeline(
                    source=UnknownSource(raw_text="?", span=span), operators=[],
                ),
                span=span,
            )

        if kind == "ParseOperator":
            patterns = []
            if hasattr(n, "Patterns"):
                for i in range(n.Patterns.Count):
                    p = n.Patterns[i]
                    patterns.append(self._visit_expr(p))
            return ParseOp(
                target=self._visit_expr(n.Expression) if hasattr(n, "Expression")
                else UnknownExpr(span=span, raw_text="?", ast_kind="None", reason="Missing parse target"),
                patterns=patterns,
                # KQL's effective default, never None -- see ParseOp.
                parse_kind=extract_named_param(n, "kind", default="simple") or "simple",
                flags=extract_named_param(n, "flags"),
                span=span,
            )

        if kind == "ParseWhereOperator":
            patterns = []
            if hasattr(n, "Patterns"):
                for i in range(n.Patterns.Count):
                    p = n.Patterns[i]
                    patterns.append(self._visit_expr(p))
            return ParseWhereOp(
                target=self._visit_expr(n.Expression) if hasattr(n, "Expression")
                else UnknownExpr(span=span, raw_text="?", ast_kind="None", reason="Missing parse target"),
                patterns=patterns,
                parse_kind=extract_named_param(n, "kind", default="simple") or "simple",
                flags=extract_named_param(n, "flags"),
                span=span,
            )

        if kind == "AsOperator":
            return AsOp(name=visit_name(n.Name), span=span)

        if kind == "RangeOperator":
            return RangeOp(
                column=visit_name(n.Name),
                start=self._visit_expr(n.From),
                end=self._visit_expr(n.To),
                step=self._visit_expr(n.Step),
                span=span,
            )

        if kind == "RenderOperator":
            # ``with (title="a")`` and the legacy bare ``kind=stacked`` are
            # the same property list written two ways -- see RenderOp -- so
            # they merge into one dict, with the ``with`` clause last because
            # it is the modern spelling and wins a collision.
            properties = read_named_params(getattr(n, "Parameters", None))
            with_clause = getattr(n, "WithClause", None)
            if with_clause is not None:
                properties.update(read_named_params(getattr(with_clause, "Properties", None)))
            return RenderOp(
                render_kind=n.ChartType.ToString().strip() if hasattr(n, "ChartType") else "table",
                properties=properties,
                span=span,
            )

        if kind == "EvaluateOperator":
            # `evaluate <plugin>(...)` — .NET node exposes FunctionCall.
            func_expr = self._visit_expr(n.FunctionCall) if hasattr(n, "FunctionCall") else None
            if not isinstance(func_expr, FuncCall):
                func_expr = FuncCall(name="<unparsed>", args=[], span=span)
            return EvaluateOp(func=func_expr, span=span)

        if kind == "CountOperator":
            as_name: str | None = None
            clause = getattr(n, "AsIdentifier", None) or getattr(n, "AsClause", None)
            if clause is not None:
                name_node = getattr(clause, "Identifier", None) or getattr(clause, "Name", None)
                if name_node is not None:
                    as_name = visit_name(name_node)
            return CountOp(as_name=as_name, span=span)

        if kind == "PrintOperator":
            cols: list = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr_as_assignment(el))
            return PrintOp(columns=cols, span=span)

        if kind == "GetSchemaOperator":
            return GetSchemaOp(span=span)

        if kind == "ConsumeOperator":
            return ConsumeOp(span=span)

        if kind == "ExecuteAndCacheOperator":
            return ExecuteAndCacheOp(span=span)

        if kind == "SerializeOperator":
            assigns = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    assigns.append(self._visit_assignment(el))
            return SerializeOp(assignments=assigns, span=span)

        if kind == "InvokeOperator":
            func_node = getattr(n, "Function", None) or getattr(n, "FunctionCall", None)
            func_expr = self._visit_expr(func_node) if func_node is not None else None
            if not isinstance(func_expr, FuncCall):
                func_expr = FuncCall(name="<unparsed>", args=[], span=span)
            return InvokeOp(func=func_expr, span=span)

        if kind == "FindOperator":
            pred = (
                self._visit_expr(n.Condition)
                if hasattr(n, "Condition") and n.Condition else None
            )
            # ``el.ToString().strip()`` -- what this read before -- is the
            # no-argument overload, ``IncludeTrivia.All``, so the table name
            # carried whatever comment preceded it into the hash. The shared
            # reader uses ``IncludeTrivia.Minimal`` and, more to the point,
            # keeps the qualifier / wildcard / ``let``-alias distinctions a
            # bare string could not express.
            tables: list[Any] = []
            in_clause = getattr(n, "InClause", None)
            if in_clause is not None and hasattr(in_clause, "Expressions"):
                for el in _iter_elements(in_clause.Expressions):
                    tables.append(self._visit_table_ref(el))
            project_cols: list[AnyExpr] = []
            project_clause = getattr(n, "Project", None)
            if project_clause is not None and hasattr(project_clause, "Columns"):
                for el in _iter_elements(project_clause.Columns):
                    project_cols.append(self._visit_expr(el))
            return FindOp(
                predicate=pred,
                tables=tables,
                withsource=extract_named_param(n, "withsource"),
                project=project_cols,
                span=span,
            )

        if kind == "FacetOperator":
            cols = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    cols.append(self._visit_expr(el))
            with_pipeline = None
            with_clause = getattr(n, "WithClause", None) or getattr(n, "Subquery", None)
            if with_clause is not None:
                # WithClause may wrap the pipeline at .Expression.
                inner = getattr(with_clause, "Expression", with_clause)
                with_pipeline = self._visit_pipeline(inner)
            return FacetOp(columns=cols, with_pipeline=with_pipeline, span=span)

        if kind == "ForkOperator":
            # Each element is a ``ForkExpression`` -- the parens belong to it
            # (``OpenParen``/``CloseParen``), the optional ``a=`` prefix is
            # its ``NameEquals`` (a ``NameEqualsClause``), and the branch
            # body is its ``Expression``. Handing the ``ForkExpression``
            # itself to ``_visit_pipeline`` is what produced empty branches:
            # the walker has no case for that kind, so it fell through
            # without collecting a single operator. Descend to
            # ``.Expression``.
            branches = []
            if hasattr(n, "Expressions"):
                for el in _iter_elements(n.Expressions):
                    name_equals = getattr(el, "NameEquals", None)
                    branches.append(ForkBranch(
                        name=visit_name(name_equals.Name) if name_equals is not None else None,
                        pipeline=self._visit_pipeline(el.Expression),
                        span=to_span(el),
                    ))
            return ForkOp(branches=branches, span=span)

        if kind == "AssertSchemaOperator":
            # ``read_row_schema`` reads the type with
            # ``ToString(IncludeTrivia.Minimal)``. The bare ``ToString()``
            # this replaced is ``IncludeTrivia.All``, which prepends the
            # node's leading trivia: ``assert-schema (a: // note\n long)``
            # recorded the type as ``"// note\n long"`` and hashed
            # differently from the identical query without the comment.
            # ``columns`` became load-bearing for the hash when the
            # volatile-field set stopped filtering the dump by key name.
            # The dict is the only thing this site does not share with the
            # other three readers.
            return AssertSchemaOp(
                columns=dict(read_row_schema(getattr(n, "Schema", None))),
                span=span,
            )

        if kind == "ParseKvOperator":
            target = (
                self._visit_expr(n.Expression)
                if hasattr(n, "Expression") else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None",
                    reason="Missing parse-kv target",
                )
            )
            # ``Keys`` is a RowSchema -- the same shape ``Schema`` is on the
            # other three readers, under a different member name, which is
            # why ``read_row_schema`` accepts the owner as well as the
            # schema. A previous guard tested ``Keys`` for a ``Count``
            # member, which RowSchema does not have, so the loop body never
            # ran and the field was always empty.
            return ParseKvOp(
                target=target,
                columns=dict(read_row_schema(getattr(n, "Keys", None))),
                span=span,
            )

        if kind == "SampleDistinctOperator":
            # ``Expression`` is unreachable-missing on a real parse (it's a
            # required grammar element, unlike ``OfExpression`` below), but
            # now that ``count`` accepts ``AnyExpr`` the "missing" case can
            # say so explicitly instead of fabricating a literal ``0`` --
            # indistinguishable from a real ``sample-distinct 0 of x`` --
            # matching the ``UnknownExpr`` sentinel convention used for
            # ``of`` three lines below.
            count = self._visit_count(n.Expression) if hasattr(n, "Expression") else UnknownExpr(
                span=span, raw_text="?", ast_kind="None",
                reason="Missing sample-distinct count",
            )
            # ``Of`` is not a member of any Kusto.Language type; the
            # fallback never fired. tests/test_reflection_audit.py now
            # rejects probes for names the assembly does not have.
            of_node = getattr(n, "OfExpression", None)
            of = self._visit_expr(of_node) if of_node is not None else UnknownExpr(
                span=span, raw_text="?", ast_kind="None",
                reason="Missing sample-distinct 'of'",
            )
            return SampleDistinctOp(count=count, of=of, span=span)

        if kind == "GraphWhereEdgesOperator":
            return GraphWhereEdgesOp(predicate=self._visit_expr(n.Condition), span=span)

        if kind == "GraphWhereNodesOperator":
            return GraphWhereNodesOp(predicate=self._visit_expr(n.Condition), span=span)

        # Preserve-raw-text ops for elaborate state-machine operators.
        if kind == "ScanOperator":
            return ScanOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)
        if kind == "TopNestedOperator":
            return TopNestedOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)
        if kind == "MakeGraphOperator":
            return MakeGraphOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)
        if kind == "MacroExpandOperator":
            # The body is a ``StatementList``. The previous probe read
            # ``Subquery`` then ``Body``; neither is a member of
            # MacroExpandOperator, so the field was always None.
            inner = None
            statements = getattr(n, "StatementList", None)
            if statements is not None and statements.Count > 0:
                for stmt in _iter_elements(statements):
                    expr = getattr(stmt, "Expression", None)
                    if expr is not None:
                        inner = self._visit_pipeline(expr)
                        break
            return MacroExpandOp(raw_text=node.ToString(IncludeTrivia.Minimal), pipeline=inner, span=span)
        if kind == "GraphMatchOperator":
            return GraphMatchOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)
        if kind == "GraphMarkComponentsOperator":
            return GraphMarkComponentsOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)
        if kind == "GraphShortestPathsOperator":
            return GraphShortestPathsOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)
        if kind == "GraphToTableOperator":
            return GraphToTableOp(raw_text=node.ToString(IncludeTrivia.Minimal), span=span)

        return UnknownOp(
            raw_text=node.ToString(IncludeTrivia.Minimal),
            ast_kind=kind,
            reason="Operator dispatch fell through — kind not in IRBuilder.HANDLED_OPERATOR_KINDS",
            span=span,
        )

    def _visit_count(self, node: Any) -> int | AnyExpr:
        """Take/sample/top/top-hitters/sample-distinct count operand.

        KQL allows any scalar expression here, not just an integer literal
        -- the previous ``safe_int`` helper called ``int(node.ToString())``
        and raised ``ValueError`` on ordinary, valid queries like
        ``let n = 10; T | take n`` or ``take toscalar(U | count)``. The
        literal case still returns a plain ``int`` so existing
        ``op.count == 5`` assertions (and downstream consumers) keep
        working; anything else becomes the visited expression.
        """
        if str(node.Kind) in ("LongLiteralExpression", "IntLiteralExpression"):
            return int(node.LiteralValue)
        return self._visit_expr(node)

    def _visit_sort_key(self, node: Any) -> SortKey:
        """One ``sort by`` / ``order by`` / ``top … by`` ordering key.

        Two AST shapes reach here and only one of them carries modifiers.
        A key with any modifier is an ``OrderedExpression`` wrapping the
        expression plus an ``OrderingClause``; a bare key is *not* wrapped at
        all — ``sort by x`` puts a plain ``NameReference`` straight into
        ``SortOperator.Expressions`` (verified on a real parse). So the
        ``else`` branch is not a defensive fallback, it is the common case.

        Both keywords of the clause are independently optional:
        ``sort by x nulls first`` yields an ``OrderingClause`` whose
        ``AscOrDescKeyword`` is ``None``. Unwritten direction becomes KQL's
        effective default, ``desc`` — never ``None``. See :class:`SortKey`
        for why the field is declared required.

        Both keyword reads go through :meth:`_ordering_keyword`, which
        validates the text against the ``Literal``'s own two values rather
        than trusting the token's presence — see there for the malformed
        input that made the difference.
        """
        span = to_span(node)
        if type(node).__name__ != "OrderedExpression":
            return SortKey(
                expression=self._visit_expr(node), direction="desc", span=span,
            )

        ordering = getattr(node, "Ordering", None)
        # ``or "desc"`` supplies the effective default for both the
        # not-written case and the unreadable one. See :class:`SortKey`.
        direction = self._ordering_keyword(
            ordering, "AscOrDescKeyword", ("asc", "desc"),
        ) or "desc"
        nulls = self._ordering_keyword(
            getattr(ordering, "NullsClause", None) if ordering is not None else None,
            "FirstOrLastKeyword", ("first", "last"),
        )
        return SortKey(
            expression=self._visit_expr(node.Expression),
            direction=direction,
            nulls=nulls,
            span=span,
        )

    def _visit_reorder_key(self, node: Any) -> ReorderKey:
        """One ``project-reorder`` term.

        The same ``OrderedExpression`` shape as :meth:`_visit_sort_key`, and
        the same bare-node common case — ``project-reorder x`` puts a plain
        ``NameReference`` into ``Expressions``, and so does the ``*`` of
        ``project-reorder *, a``, which is why a wildcard arrives here as a
        ``ColumnRef`` whose name is the wildcard text rather than as a
        ``StarExpr``.

        What differs is the missing modifier. There is no effective default
        to supply: ``direction`` stays ``None``. See :class:`ReorderKey` for
        why that is not the same decision as ``SortKey``'s, and why making it
        the same would reintroduce a collision.

        ``NullsClause`` is not read. The grammar attaches one only to a sort
        ordering, and ``project-reorder x nulls first`` is a syntax error
        (checked on a real parse), so there is nothing to record.
        """
        span = to_span(node)
        if type(node).__name__ != "OrderedExpression":
            return ReorderKey(expression=self._visit_expr(node), span=span)
        return ReorderKey(
            expression=self._visit_expr(node.Expression),
            direction=self._ordering_keyword(
                getattr(node, "Ordering", None), "AscOrDescKeyword", ("asc", "desc"),
            ),
            span=span,
        )

    @staticmethod
    def _ordering_keyword(
        clause: Any, member: str, allowed: tuple[str, ...],
    ) -> str | None:
        """One keyword token off an ordering clause, or ``None``.

        Kusto's error recovery has two ways of saying "the keyword isn't
        there" and only one of them is ``None``. ``sort by x nulls`` builds
        an ``OrderingNullsClause`` that *exists*, holding a
        ``FirstOrLastKeyword`` that also exists but is a missing token whose
        ``Text`` is ``""`` (``IsMissing`` is ``True``). A presence check
        alone therefore let an empty string reach ``Literal["first",
        "last"]``, and ``to_ir()`` raised ``ValidationError`` on a typo —
        a hard crash where ``T | take``, ``T | where``, ``T | summarize by``
        and ``T | sort by`` all build a degraded operator and leave the
        complaint to the diagnostics. ``nulls firs`` and ``nulls xyz``
        recover the same way.

        Checking membership in ``allowed`` rather than ``IsMissing`` is the
        deliberate choice: it is the same check the ``Literal`` will apply,
        so nothing can get past here that pydantic would then reject, and it
        holds for a recovery shape that keeps the garbage text as well as for
        one that blanks it. It is applied to the direction keyword too. The
        argument that a bad direction cannot happen — ``ascending`` and
        ``descending`` are syntax errors that never reach the token, and
        ``ASC``/``Desc`` case-fold cleanly — is empirical, and an empirical
        argument is exactly what ``FirstOrLastKeyword`` had until this input
        turned up.
        """
        if clause is None:
            return None
        token = getattr(clause, member, None)
        if token is None:
            return None
        text = str(token.Text).strip().lower()
        return text if text in allowed else None

    # -- expression dispatch ---------------------------------------------

    def _visit_expr(self, node: Any) -> AnyExpr:
        if not node:
            return UnknownExpr(
                span=Span(text_start=0, width=0),
                raw_text="", ast_kind="None", reason="Empty node",
            )

        span = to_span(node)
        kind = str(type(node).__name__)
        res: AnyExpr | None = None

        if kind == "ParenthesizedExpression":
            res = self._visit_expr(node.Expression)

        elif kind == "NameReference":
            name = visit_name(node.Name)
            # A bare ``*`` is *every remaining column*, not a column called
            # ``*``. The parser spells it as a ``NameReference`` -- the same
            # class it uses for an ordinary column, separated only by the
            # inner name node's ``WildcardedName`` kind -- so it lowered to
            # ``ColumnRef(name="*")`` and ``find_all(ir, ColumnRef)`` named a
            # column that does not exist. ``StarExpr`` is the node the IR
            # already has for it, and what ``distinct *`` has always built.
            #
            # A *prefix* wildcard (``a*``) stays a ``ColumnRef``: it names a
            # set of real columns by pattern, the pattern text is the only
            # record of which ones, and ``StarExpr`` has nowhere to keep it.
            # Hence the text check as well as the kind check -- keying on the
            # kind alone would collapse every prefix wildcard onto ``*``.
            if name == "*" and is_wildcarded_name(node.Name):
                res = StarExpr(span=span)
            # A name an *earlier* ``let`` bound is a query-local value, not a
            # column of whatever the pipeline reads -- the expression-position
            # twin of the ``LetRef`` check in ``_visit_source``, and decided
            # from the statement text alone so it does not depend on whether a
            # schema was supplied. Lowering it to a ``ColumnRef`` made
            # ``find_all(ir, ColumnRef)`` report a column that does not exist.
            elif name in self._let_names:
                res = LetValueRef(name=name, span=span)
            else:
                res = ColumnRef(name=name, span=span)

        elif kind == "NameDeclaration":
            res = ColumnRef(name=visit_name(node), span=span)

        elif kind in ("NameAndTypeDeclaration", "TypedColumnReference"):
            # The same ``name:type`` shape under two node classes:
            # ``NameAndTypeDeclaration`` is a ``parse`` capture (``b:long``,
            # name in ``Name``) and ``TypedColumnReference`` is a
            # ``find … project`` column (``a:string``, name in ``Column``).
            # The first shared a branch with ``NameDeclaration`` and so
            # lowered to a bare ``ColumnRef``: ``visit_name`` reads the name
            # child and the declared type went nowhere, making a typed and an
            # untyped capture one node with one hash. ``node_text`` on the
            # type rather than ``ToString()`` for the reason every other type
            # read in this builder uses it -- the no-argument overload is
            # ``IncludeTrivia.All`` and would put a preceding comment in the
            # type string, and from there into the hash.
            name_node = getattr(node, "Name", None)
            if name_node is None:
                name_node = getattr(node, "Column", None)
            type_node = getattr(node, "Type", None)
            res = TypedNameDecl(
                name=visit_name(name_node),
                declared_type=(
                    _node_text(type_node).strip() if type_node is not None else "unknown"
                ),
                span=span,
            )

        elif kind == "PathExpression":
            # Collapse `$left.X` / `$right.X` (syntactic) and `T.X` (via binder
            # ReferencedSymbol) into ColumnRef. Dynamic property access stays
            # as PathExpr.
            expr_node = node.Expression
            sel_node = node.Selector
            expr_kind = str(type(expr_node).__name__)
            sel_kind = str(type(sel_node).__name__)
            if expr_kind == "NameReference" and sel_kind == "NameReference":
                lhs_name = visit_name(expr_node.Name)
                rhs_name = visit_name(sel_node.Name)
                if lhs_name in ("$left", "$right") or is_table_symbol(getattr(expr_node, "ReferencedSymbol", None)):
                    side = lhs_name[1:] if lhs_name in ("$left", "$right") else None
                    res = ColumnRef(
                        name=rhs_name, table=lhs_name, join_side=side, span=span,
                    )
                else:
                    res = PathExpr(
                        expression=self._visit_expr(expr_node),
                        selector=self._visit_expr(sel_node),
                        span=span,
                    )
            else:
                res = PathExpr(
                    expression=self._visit_expr(expr_node),
                    selector=self._visit_expr(sel_node),
                    span=span,
                )

        elif kind == "ElementExpression":
            res = ElementExpr(
                expression=self._visit_expr(node.Expression),
                selector=self._visit_expr(node.Selector),
                span=span,
            )

        elif kind == "SimpleNamedExpression":
            res = NamedExpr(
                name=visit_name(node.Name),
                expression=self._visit_expr(node.Expression),
                span=span,
            )

        elif kind == "CompoundNamedExpression":
            names: list[str] = []
            if hasattr(node, "Names") and node.Names:
                sub_list = node.Names.Names
                for el in _iter_elements(sub_list):
                    names.append(visit_name(el))
            res = CompoundNamedExpr(
                names=names,
                expression=self._visit_expr(node.Expression),
                span=span,
            )

        elif kind == "BracketedExpression":
            res = BracketedExpr(expression=self._visit_expr(node.Expression), span=span)

        elif kind == "PrefixUnaryExpression":
            op_str = node.Operator.ToString().strip()
            operand = self._visit_expr(node.Expression)
            if op_str == "!":
                res = Not(operand=operand, span=span)
            else:
                res = UnaryOp(op=op_str, operand=operand, span=span)

        elif kind == "StarExpression":
            res = StarExpr(span=span)

        elif kind == "LiteralExpression":
            # The .NET node already carries the exact kind; read it rather than
            # re-inferring from the Python type of LiteralValue, which cannot
            # distinguish long from real and collapses datetime/timespan/guid
            # into "string".
            value, ticks = literal_value_and_ticks(node)
            res = LiteralExpr(
                value=value,
                literal_kind=literal_kind_for(node),
                ticks=ticks,
                span=span,
            )

        elif kind == "CompoundStringLiteralExpression":
            # KQL concatenates adjacent string literals, C-style: ``'a' 'b'``
            # is the one value ``"ab"``. The parser has already done the
            # joining -- ``LiteralValue`` is the concatenated string -- so
            # this is a ``LiteralExpr`` like any other, and the multi-token
            # spelling is a source detail that ``span`` still records.
            # Without the branch the whole comparison's right-hand side fell
            # through to ``UnknownExpr``.
            res = LiteralExpr(
                value=str(node.LiteralValue), literal_kind="string", span=span,
            )

        elif kind == "DynamicExpression":
            # LiteralValue is the JSON body as a string; consumers can json.loads.
            body = node.LiteralValue if hasattr(node, "LiteralValue") else node.ToString()
            res = LiteralExpr(value=str(body), literal_kind="dynamic", span=span)

        elif kind == "BinaryExpression":
            op = node.Operator.ToString().strip().lower()
            left = self._visit_expr(node.Left)
            right = self._visit_expr(node.Right)
            if op == "and":
                res = And(operands=[left, right], span=span)
            elif op == "or":
                res = Or(operands=[left, right], span=span)
            elif op == "matches regex":
                if isinstance(right, LiteralExpr) and isinstance(right.value, str):
                    pattern_str = right.value
                else:
                    pattern_str = node.Right.ToString().strip(" \"'")
                res = RegexMatch(
                    target=left,
                    pattern=pattern_str,
                    case_sensitive=True,
                    span=span,
                )
            else:
                res = BinOp(
                    op=op,
                    # Arithmetic is neither an inclusion nor an exclusion.
                    # The ``"!" in op`` rule has no arithmetic case, so it
                    # answered "inclusion" for every ``+`` ever parsed.
                    polarity=(
                        None if op in _ARITHMETIC_OPS
                        else "inclusion" if "!" not in op
                        else "exclusion"
                    ),
                    case_sensitive=_is_case_sensitive_op(op),
                    left=left,
                    right=right,
                    span=span,
                )

        elif kind in ("InExpression", "HasAnyExpression", "HasAllExpression"):
            # KQL `in` / `!in` compare exactly; only the tilde forms fold
            # case. This was hardcoded False, so `in` and `in~` were
            # indistinguishable in the IR and canonical_form rendered a
            # case-sensitive `in` as `in~` -- a different predicate.
            # `has_any` / `has_all` are term matches and always fold case.
            # `in`, `!in`, `in~` and `!in~` all share the class
            # `InExpression` and differ only in `.Kind`, so dispatching on the
            # class name -- as this branch does -- discards the distinction.
            # `Operator.ToString()` recovers it, and unlike
            # `ReferencedSymbol.OperatorKind` it is present on a syntax-only
            # parse, so `op` does not depend on whether a schema was supplied.
            membership_op = node.Operator.ToString().strip().lower()
            res = SetMembership(
                op=membership_op,
                column=self._visit_expr(node.Left),
                values=self._visit_list(node.Right),
                polarity="inclusion" if "!" not in membership_op else "exclusion",
                case_sensitive=(
                    kind == "InExpression" and not membership_op.endswith("~")
                ),
                span=span,
            )

        elif kind == "BetweenExpression":
            # Bounds live in an `ExpressionCouple` via `First`/`Second` —
            # not `Left`/`Right` like other binary expressions.
            couple = node.Right
            low_node = getattr(couple, "First", None)
            high_node = getattr(couple, "Second", None)
            res = Between(
                target=self._visit_expr(node.Left),
                low=self._visit_expr(low_node) if low_node is not None else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None", reason="Missing between low",
                ),
                high=self._visit_expr(high_node) if high_node is not None else UnknownExpr(
                    span=span, raw_text="?", ast_kind="None", reason="Missing between high",
                ),
                polarity="inclusion" if "!" not in node.Operator.ToString() else "exclusion",
                span=span,
            )

        elif kind == "FunctionCallExpression":
            # Prefer binder-resolved name; fall back to syntactic.
            name = "unknown"
            ref_sym = getattr(node, "ReferencedSymbol", None)
            if ref_sym is not None:
                try:
                    name = ref_sym.Name
                except AttributeError:
                    ref_sym = None
            if not ref_sym:
                try:
                    name_node = node.Name
                    if hasattr(name_node, "SimpleName"):
                        name = str(name_node.SimpleName)
                    else:
                        name = visit_name(name_node)
                except AttributeError as e:  # pragma: no cover
                    logger.debug("FunctionCall name resolution fell through: %s", e)

            args: list[AnyExpr] = []
            if hasattr(node, "ArgumentList") and node.ArgumentList and hasattr(node.ArgumentList, "Expressions"):
                for el in _iter_elements(node.ArgumentList.Expressions):
                    args.append(self._visit_expr(el))

            res = FuncCall(
                name=name, args=args,
                is_time_func=_is_time_func_name(name),
                span=span,
            )

            # Lift case()/iif()/isnotnull()/not() into structural nodes.
            lname = name.lower()
            if lname == "case" and len(args) >= 3 and len(args) % 2 == 1:
                branches = [(args[i], args[i + 1]) for i in range(0, len(args) - 1, 2)]
                res = CaseExpr(branches=branches, default=args[-1], span=span)
            elif lname == "iif" and len(args) == 3:
                res = CaseExpr(branches=[(args[0], args[1])], default=args[2], span=span)
            elif lname in _NULL_TEST_POLARITY and len(args) == 1:
                # All four null/empty tests lower here. Only the positive
                # pair used to, so ``find_all(ir, Exists)`` -- "which columns
                # does this query null-check" -- saw half the query.
                #
                # Record which function: isnotempty also rejects "", so
                # lowering the pair to a bare Exists made two different
                # predicates indistinguishable. ``polarity`` is the
                # derived-but-useful companion, as on ``BinOp``.
                res = Exists(
                    op=lname,
                    polarity=_NULL_TEST_POLARITY[lname],
                    target=args[0],
                    span=span,
                )
            elif lname == "not" and len(args) == 1:
                res = Not(operand=args[0], span=span)

        elif kind == "ToScalarExpression":
            res = ToScalarExpr(pipeline=self._visit_pipeline(node.Expression), span=span)

        elif kind == "PipeExpression":
            # A bare pipeline in expression position — the value set of a
            # membership test, `| where User in ((Suspicious | project User))`.
            # No wrapping function names it, so it arrives naked here.
            res = SubqueryExpr(pipeline=self._visit_pipeline(node), span=span)

        elif kind == "ExternalDataExpression":
            # Shared with the source-position branch of ``_visit_pipeline``:
            # the same construct read two ways is how the two readings drift.
            cols, uris, fmt, props = read_external_data(node)
            res = ExternalDataExpr(
                columns=cols, uris=uris, format=fmt, properties=props, span=span,
            )

        elif kind == "MakeSeriesExpression":
            res = self._visit_expr(node.Expression)

        if not res:
            res = UnknownExpr(
                span=span, raw_text=node.ToString(IncludeTrivia.Minimal),
                ast_kind=kind, reason="Unsupported expression type",
            )

        map_semantic_info(node, res)
        return res

    # -- visitor-stateful helpers ----------------------------------------

    def _visit_assignment(self, node: Any, mode: str = "aggregation") -> Assignment:
        kind = str(type(node).__name__)
        if kind == "SimpleNamedExpression":
            return Assignment(
                name=visit_name(node.Name),
                expr=self._visit_expr(node.Expression),
                span=to_span(node),
            )
        auto = self._auto_name(node, mode)
        return Assignment(
            name=auto if auto is not None else node.ToString().strip(),
            expr=self._visit_expr(node),
            span=to_span(node),
        )

    def _visit_expr_as_assignment(self, node: Any, mode: str = "aggregation") -> ColumnRef | Assignment | AnyExpr:
        kind = str(type(node).__name__)
        if kind == "SimpleNamedExpression":
            return self._visit_assignment(node, mode=mode)
        # Bare column refs carry their own name; leave them unwrapped.
        if kind == "NameReference":
            return self._visit_expr(node)
        # In grouping context, function-wrapped column refs project the inner
        # column name (KQL's auto-naming for ``by bin(C, ...)`` -> ``C``).
        # Wrap as Assignment so downstream consumers see the canonical name.
        if mode == "grouping":
            auto = self._auto_name(node, "grouping")
            if auto is not None:
                return Assignment(
                    name=auto,
                    expr=self._visit_expr(node),
                    span=to_span(node),
                )
        return self._visit_expr(node)

    def _auto_name(self, node: Any, mode: str) -> str | None:
        """Canonical KQL output column name for an unnamed expression.

        Mirrors the binder's auto-naming so ``Assignment.name`` matches what
        ``ResultType.Columns`` reports. Returns None when no canonical name is
        derivable (caller falls back to source text).

        - ``mode="aggregation"``: ``count()`` -> ``count_``, ``avg(X)`` -> ``avg_X``.
        - ``mode="grouping"``: any function wrapping a column ref projects the
          inner column name (``bin(TG, 1h)`` -> ``TG``, ``tostring(D)`` -> ``D``).
        """
        kind = str(type(node).__name__)
        if kind == "ParenthesizedExpression" and hasattr(node, "Expression"):
            return self._auto_name(node.Expression, mode)
        if kind == "NameReference":
            try:
                return visit_name(node.Name) if hasattr(node, "Name") else None
            except Exception:  # pragma: no cover
                return None
        if kind != "FunctionCallExpression":
            return None

        fname: str | None = None
        ref_sym = getattr(node, "ReferencedSymbol", None)
        if ref_sym is not None:
            try:
                fname = ref_sym.Name
            except AttributeError:
                fname = None
        if not fname:
            try:
                name_node = node.Name
                fname = (
                    str(name_node.SimpleName)
                    if hasattr(name_node, "SimpleName")
                    else visit_name(name_node)
                )
            except AttributeError:
                fname = None

        first_col: str | None = None
        if hasattr(node, "ArgumentList") and node.ArgumentList:
            try:
                for el in _iter_elements(node.ArgumentList.Expressions):
                    inner_kind = str(type(el).__name__)
                    if inner_kind == "NameReference":
                        try:
                            first_col = visit_name(el.Name)
                        except Exception as e:  # pragma: no cover
                            logger.debug("first-column name probe fell through: %s", e)
                        break
            except Exception as e:  # pragma: no cover
                logger.debug("argument-list walk fell through: %s", e)

        if mode == "grouping":
            return first_col
        # aggregation mode
        if not fname:
            return None
        return f"{fname}_{first_col}" if first_col else f"{fname}_"

    def _visit_list(self, node: Any) -> list[AnyExpr]:
        exprs: list[AnyExpr] = []
        if not node:
            return exprs
        kind = str(type(node).__name__)
        if kind == "ParenthesizedExpression":
            return self._visit_list(node.Expression)
        if kind == "ExpressionList" and hasattr(node, "Expressions"):
            return self._visit_list(node.Expressions)
        if "SyntaxList" in kind or hasattr(node, "Count"):
            for i in range(node.Count):
                element = node[i]
                if hasattr(element, "Element"):
                    exprs.append(self._visit_expr(element.Element))
                else:
                    exprs.append(self._visit_expr(element))
        else:
            r = self._visit_expr(node)
            if r:
                exprs.append(r)
        return exprs

