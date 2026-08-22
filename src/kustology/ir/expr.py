# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from typing import Any, ClassVar, Literal, Union

from pydantic import BaseModel

from .spans import Span
from .types import KustoType

AnyExpr = Union[
    "BinOp", "UnaryOp", "SetMembership", "Between", "And", "Or", "Not",
    "Exists", "RegexMatch", "CaseExpr", "ColumnRef", "LetValueRef",
    "TypedNameDecl",
    "LiteralExpr",
    "FuncCall", "PathExpr", "ElementExpr", "StarExpr", "NamedExpr",
    "CompoundNamedExpr", "BracketedExpr", "ToScalarExpr",
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
    # None when the binder hasn't placed it. Reading through a `let` alias
    # reports the alias, not the table behind it -- see
    # ``SchemaAttacher.enrich``. Binder-populated, so it is stripped before
    # ``semantic_hash``: the same query text must hash one way whether or not
    # a schema was supplied.
    table: str | None = None
    # Which side of a join the reference was written against, when the query
    # said so with `$left.` / `$right.`. Separate from ``table`` because the
    # binder *overwrites* that sentinel with the table it resolves to, so a
    # bound parse would otherwise lose the side entirely -- and the side is
    # semantic: `$left.a == $left.b` is not the join `$left.a == $right.b`.
    join_side: Literal["left", "right"] | None = None


class LetValueRef(Expr):
    """A reference, in expression position, to a name an earlier ``let`` bound.

    ``threshold`` in ``let threshold = 5; T | where Count > threshold`` --
    a query-local constant, not a column of ``T``. It used to lower to a
    :class:`ColumnRef`, which made the IR state something the query does not:
    that the filter reads two columns. ``find_all(ir, ColumnRef)`` is the
    documented way to ask which columns a query touches, so column lineage,
    schema-drift checks and rename impact analysis all counted the ``let``
    name among them, and the binder spent every lookup failing to place a
    column that does not exist.

    It is the expression-position twin of
    :class:`~kustology.ir.query.LetRef`, which already covered the *source*
    position (``let Base = T | …; Base | count``). Both exist for the same
    reason: a name a ``let`` bound is neither a table nor a column, and
    saying it is either one is a wrong answer rather than a missing one.

    Deliberately **not** a ``ColumnRef`` subclass. The binder places columns
    by ``isinstance``, so a subclass would inherit exactly the resolution
    this node exists to stop. Nothing types it but ``map_semantic_info``,
    which copies the .NET ``ResultType`` the parser already computed.

    It also restores an equivalence the hash is documented to have. A
    ``let`` name is a local label, so ``compute_semantic_hash`` renames every
    binding to its declaration index -- but the rename can only touch nodes
    that *are* ``let`` names, and a ``ColumnRef`` is not one (a real column
    called ``n`` is a different query). The declaration was canonicalized
    while the use site kept the name the query wrote, so
    ``let n = 5; T | where a > n`` and ``let m = 5; T | where a > m`` hashed
    apart. Adding this class to ``transforms._LET_NAME_MODELS`` closes that.
    """

    KIND: ClassVar[str] = "let_value_ref"
    kind: Literal["let_value_ref"] = "let_value_ref"
    name: str


class TypedNameDecl(Expr):
    """A ``name:type`` declaration sitting in expression position.

    The parser's ``NameAndTypeDeclaration``: the typed capture of
    ``parse a with 'x' b:long``, a typed column of ``find … project a:string``,
    a ``scan declare`` slot. It is a *declaration*, not a reference — the
    column does not exist until this operator creates it — which is why it is
    its own node rather than a :class:`ColumnRef` with a type hanging off it.

    Both halves used to be thrown away. The builder lowered the whole
    declaration to ``ColumnRef(name=visit_name(node))``, which reads the
    ``Name`` child and never the ``Type``, so ``parse a with 'x' b:long`` and
    ``parse a with 'x' b`` built identical IR and carried the same
    ``semantic_hash`` — though the first states the capture's type and the
    second leaves it a string. ``declared_type`` is the type as the query
    wrote it (``long``, ``string``, ``datetime``), not a resolved
    :class:`~kustology.ir.types.KustoType`: it is source text, and a
    consumer that wants the enum can map it, while a consumer that wants to
    know what the query said cannot recover it from a mapped-and-defaulted
    value.
    """

    KIND: ClassVar[str] = "typed_name"
    kind: Literal["typed_name"] = "typed_name"
    name: str
    declared_type: str


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
    # The literal KQL operator: in, !in, in~, !in~, has_any, has_all.
    # Source of truth, as on ``BinOp`` -- ``polarity`` and ``case_sensitive``
    # are derived from it and kept for convenience.
    #
    # Without it those two fields were the only discriminators, giving four
    # states for six operators: ``in~``, ``has_any`` and ``has_all`` collapsed
    # into one indistinguishable node with an identical ``semantic_hash``,
    # though ``has_any`` and ``has_all`` are opposites (OR vs AND of term
    # matches) and ``in~`` compares whole values rather than terms.
    op: str
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
    """A positive null/empty test, lowered from ``isnotnull`` or ``isnotempty``.

    Note the asymmetry: the *negative* forms ``isnull`` and ``isempty`` are
    not lowered at all -- they stay :class:`FuncCall`. Whether to lower them
    too is an open question, not an oversight.
    """

    KIND: ClassVar[str] = "exists"
    kind: Literal["exists"] = "exists"
    # Which function produced this: "isnotnull" or "isnotempty". They are not
    # equivalent -- isnotempty also rejects "" -- and without this field both
    # lowered to the same node with the same semantic_hash.
    op: str
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


class ToScalarExpr(Expr):
    KIND: ClassVar[str] = "to_scalar"
    kind: Literal["to_scalar"] = "to_scalar"
    pipeline: Any  # forward ref to Pipeline (cycle avoidance)


class SubqueryExpr(Expr):
    """A bare tabular subquery sitting in expression position.

    KQL allows a pipeline as the value set of a membership test —
    ``| where User in ((Suspicious | project User))``. Unlike
    ``ToScalarExpr`` there is no wrapping function to name it after, so the
    pipeline arrives naked. Modeling it keeps the
    subtree reachable by ``walk``/``find_all`` instead of collapsing a whole
    inner query into an ``UnknownExpr`` blob of raw text.
    """

    KIND: ClassVar[str] = "subquery"
    kind: Literal["subquery"] = "subquery"
    pipeline: Any  # forward ref to Pipeline (cycle avoidance)


class ExternalDataExpr(Expr):
    """``externaldata(...)[...]`` in expression position — a membership set.

    The source-position form is
    :class:`~kustology.ir.query.ExternalDataSource`; both are filled by
    :func:`~kustology.ir._builder_helpers.read_external_data` so they cannot
    drift apart.

    ``uris`` replaced a singular ``uri: str`` that held whichever URI came
    first. ``externaldata`` takes a list, and a feed stitched from two URIs
    is not the feed from either one of them — the dropped entries made two
    different queries build the same node. As on the source class, an entry
    is **not guaranteed to be a URI**: an element that does not fold to a
    literal (a ``let``-bound feed URL, ``strcat(...)``) is recorded as its
    own source text.
    """

    KIND: ClassVar[str] = "external_data"
    kind: Literal["external_data"] = "external_data"
    columns: list[tuple[str, str]]
    uris: list[str]
    format: str | None = None


class UnknownExpr(Expr):
    KIND: ClassVar[str] = "unknown_expr"
    kind: Literal["unknown_expr"] = "unknown_expr"
    raw_text: str
    ast_kind: str
    reason: str


for _cls in (
    LiteralExpr, ColumnRef, LetValueRef, TypedNameDecl, BinOp, SetMembership,
    Between, And, Or, Not,
    FuncCall, CaseExpr, RegexMatch, Exists, PathExpr, ElementExpr, StarExpr,
    NamedExpr, CompoundNamedExpr, UnaryOp, BracketedExpr,
    ToScalarExpr, SubqueryExpr, ExternalDataExpr,
):
    _cls.model_rebuild()
