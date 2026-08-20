# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from typing import Any, ClassVar, Literal, Union

from pydantic import BaseModel

from .spans import Span
from .types import KustoType

AnyExpr = Union[
    "BinOp", "UnaryOp", "SetMembership", "Between", "And", "Or", "Not",
    "Exists", "RegexMatch", "CaseExpr", "ColumnRef", "LiteralExpr",
    "FuncCall", "PathExpr", "ElementExpr", "StarExpr", "NamedExpr",
    "CompoundNamedExpr", "BracketedExpr", "MaterializeExpr", "ToScalarExpr",
    "SubqueryExpr", "ExternalDataExpr", "UnknownExpr", "Expr",
]


# KIND is the LLM-facing discriminator surfaced by ``ir.llm_view.to_llm_dict``.
# Keeping it separate from the Python class name lets the wire format use
# snake_case KQL-aligned labels (``filter``, ``column_ref``) regardless of
# the CamelCase Python naming conventions.
class Expr(BaseModel):
    # ``extra="forbid"`` propagates to every Expr subclass — see
    # ``query.Operator`` for the matching policy on operator nodes.
    model_config = {"extra": "forbid"}

    KIND: ClassVar[str] = "expr"
    kind: Literal["expr"] = "expr"
    span: Span
    result_type: KustoType = KustoType.UNRESOLVED
    # For DYNAMIC, the element type (e.g. dynamic<bool>). None otherwise.
    result_type_inner: KustoType | None = None

    @property
    def canonical_form(self) -> str:
        """Stable, commutative-aware string form of this expression.

        Pure function of the subtree — recomputed on each access so the
        result always reflects current binder state (e.g. ``ColumnRef.table``
        populated post-bind). Not a serialized field: kept out of
        ``model_dump()`` to avoid storing data that the tree already
        determines.
        """
        # Lazy import: _normalize imports from this module.
        from ._normalize import canonical
        return canonical(self)


class LiteralExpr(Expr):
    KIND: ClassVar[str] = "literal"
    kind: Literal["literal"] = "literal"
    value: str | int | float | bool | None
    literal_kind: Literal[
        "string", "int", "long", "real", "decimal", "bool", "datetime",
        "timespan", "dynamic", "guid", "null",
    ]
    # Exact .NET tick count (100ns units) for datetime and timespan literals;
    # None for every other kind. TimeSpan.TotalSeconds is a float and loses
    # sub-second exactness, so consumers rebuilding a ``timedelta`` use
    # ``ticks // 10`` for microseconds — exact down to ``1microsecond``
    # (10 ticks -> 1us). Finer literals do not survive that conversion:
    # ``2tick`` is 2 ticks, ``2 // 10 == 0``, and ``timedelta`` cannot
    # represent 200ns at any rate. This field is the only lossless form —
    # read it directly rather than a reconstructed ``timedelta``.
    ticks: int | None = None


class ColumnRef(Expr):
    KIND: ClassVar[str] = "column_ref"
    kind: Literal["column_ref"] = "column_ref"
    name: str
    # "$left"/"$right" in join on-clauses, concrete table name when resolved,
    # None when the binder hasn't placed it.
    table: str | None = None


class FuncCall(Expr):
    KIND: ClassVar[str] = "func_call"
    kind: Literal["func_call"] = "func_call"
    name: str
    args: list[AnyExpr]
    is_time_func: bool = False


class BinOp(Expr):
    KIND: ClassVar[str] = "bin_op"
    kind: Literal["bin_op"] = "bin_op"
    op: str
    polarity: Literal["inclusion", "exclusion"]
    case_sensitive: bool = True
    left: AnyExpr
    right: AnyExpr


class SetMembership(Expr):
    KIND: ClassVar[str] = "set_membership"
    kind: Literal["set_membership"] = "set_membership"
    column: AnyExpr
    values: list[AnyExpr]
    polarity: Literal["inclusion", "exclusion"]
    case_sensitive: bool = False


class Between(Expr):
    KIND: ClassVar[str] = "between"
    kind: Literal["between"] = "between"
    target: AnyExpr
    low: AnyExpr
    high: AnyExpr
    polarity: Literal["inclusion", "exclusion"]


class And(Expr):
    KIND: ClassVar[str] = "and"
    kind: Literal["and"] = "and"
    operands: list[AnyExpr]


class Or(Expr):
    KIND: ClassVar[str] = "or"
    kind: Literal["or"] = "or"
    operands: list[AnyExpr]


class Not(Expr):
    KIND: ClassVar[str] = "not"
    kind: Literal["not"] = "not"
    operand: AnyExpr


class RegexMatch(Expr):
    KIND: ClassVar[str] = "regex_match"
    kind: Literal["regex_match"] = "regex_match"
    target: AnyExpr
    pattern: str
    case_sensitive: bool = True


class Exists(Expr):
    KIND: ClassVar[str] = "exists"
    kind: Literal["exists"] = "exists"
    target: AnyExpr


class CaseExpr(Expr):
    KIND: ClassVar[str] = "case"
    kind: Literal["case"] = "case"
    branches: list[tuple[AnyExpr, AnyExpr]]
    default: AnyExpr | None = None


class PathExpr(Expr):
    KIND: ClassVar[str] = "path"
    kind: Literal["path"] = "path"
    expression: AnyExpr
    selector: AnyExpr


class ElementExpr(Expr):
    KIND: ClassVar[str] = "element"
    kind: Literal["element"] = "element"
    expression: AnyExpr
    selector: AnyExpr


class StarExpr(Expr):
    KIND: ClassVar[str] = "star"
    kind: Literal["star"] = "star"


class NamedExpr(Expr):
    KIND: ClassVar[str] = "named"
    kind: Literal["named"] = "named"
    name: str
    expression: AnyExpr


class CompoundNamedExpr(Expr):
    KIND: ClassVar[str] = "compound_named"
    kind: Literal["compound_named"] = "compound_named"
    names: list[str]
    expression: AnyExpr


class UnaryOp(Expr):
    KIND: ClassVar[str] = "unary_op"
    kind: Literal["unary_op"] = "unary_op"
    op: str
    operand: AnyExpr


class BracketedExpr(Expr):
    KIND: ClassVar[str] = "bracketed"
    kind: Literal["bracketed"] = "bracketed"
    expression: AnyExpr


class MaterializeExpr(Expr):
    KIND: ClassVar[str] = "materialize"
    kind: Literal["materialize"] = "materialize"
    pipeline: Any  # forward ref to Pipeline (cycle avoidance)


class ToScalarExpr(Expr):
    KIND: ClassVar[str] = "to_scalar"
    kind: Literal["to_scalar"] = "to_scalar"
    pipeline: Any  # forward ref to Pipeline (cycle avoidance)


class SubqueryExpr(Expr):
    """A bare tabular subquery sitting in expression position.

    KQL allows a pipeline as the value set of a membership test —
    ``| where User in ((Suspicious | project User))``. Unlike
    ``MaterializeExpr`` / ``ToScalarExpr`` there is no wrapping function to
    name it after, so the pipeline arrives naked. Modeling it keeps the
    subtree reachable by ``walk``/``find_all`` instead of collapsing a whole
    inner query into an ``UnknownExpr`` blob of raw text.
    """

    KIND: ClassVar[str] = "subquery"
    kind: Literal["subquery"] = "subquery"
    pipeline: Any  # forward ref to Pipeline (cycle avoidance)


class ExternalDataExpr(Expr):
    KIND: ClassVar[str] = "external_data"
    kind: Literal["external_data"] = "external_data"
    columns: list[tuple[str, str]]
    uri: str
    format: str | None = None


class UnknownExpr(Expr):
    KIND: ClassVar[str] = "unknown_expr"
    kind: Literal["unknown_expr"] = "unknown_expr"
    raw_text: str
    ast_kind: str
    reason: str


for _cls in (
    LiteralExpr, ColumnRef, BinOp, SetMembership, Between, And, Or, Not,
    FuncCall, CaseExpr, RegexMatch, Exists, PathExpr, ElementExpr, StarExpr,
    NamedExpr, CompoundNamedExpr, UnaryOp, BracketedExpr, MaterializeExpr,
    ToScalarExpr, SubqueryExpr, ExternalDataExpr,
):
    _cls.model_rebuild()
