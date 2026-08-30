# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Expression-position IR nodes: literals, operators, references, and calls."""

from typing import TYPE_CHECKING, Annotated, Literal, Union

from pydantic import BaseModel, Field

from .spans import Span
from .types import KustoType

if TYPE_CHECKING:
    # ``query`` imports this module, so ``Pipeline`` is a forward reference
    # here, resolved by the ``model_rebuild()`` calls at the bottom of
    # ``query.py`` from that module's namespace. See ``ToScalarExpr``.
    from .query import Pipeline

# Discriminated on the kind literal; member order is not load-bearing (see
# ``test_union_ordering``).
AnyExpr = Annotated[Union[
    "BinOp", "UnaryOp", "SetMembership", "Between", "And", "Or", "Not",
    "Exists", "RegexMatch", "CaseExpr", "ColumnRef", "LetValueRef",
    "TypedNameDecl",
    "LiteralExpr",
    "FuncCall", "PathExpr", "ElementExpr", "StarExpr", "NamedExpr",
    "CompoundNamedExpr", "BracketedExpr", "ToScalarExpr",
    "SubqueryExpr", "ExternalDataExpr", "DataTableExpr", "UnknownExpr", "Expr",
], Field(discriminator="kind")]


# Every subclass declares ``kind: Literal["..."] = "..."``, a snake_case
# KQL-aligned label (``filter``, ``column_ref``) independent of the CamelCase
# class name. It is the discriminator every pydantic union here builds on,
# ``AnyExpr`` included, and what ``ir.llm_view.to_llm_dict`` reads via
# ``model_fields["kind"].default`` to lead each emitted dict.
class Expr(BaseModel):
    """Base class for every expression-position IR node."""

    # ``extra="forbid"`` propagates to every Expr subclass. See
    # ``query.Operator`` for the matching policy on operator nodes.
    model_config = {"extra": "forbid"}

    kind: Literal["expr"] = "expr"
    span: Span
    result_type: KustoType = KustoType.UNRESOLVED
    # For DYNAMIC, the element type (for example, dynamic<bool>). None otherwise.
    result_type_inner: KustoType | None = None

    @property
    def canonical_form(self) -> str:
        """Stable, commutative-aware string form of this expression.

        Pure function of the subtree, recomputed on each access so it
        reflects current binder state (for example, ``ColumnRef.table``
        populated post-bind). It stays out of ``model_dump()``, since the
        tree already determines it.
        """
        # Lazy import: _normalize imports from this module.
        from ._normalize import canonical
        return canonical(self)


class LiteralExpr(Expr):
    """A literal value, with the KQL kind that produced it.

    Two spellings collapse here, and collapse the ``semantic_hash`` with
    them. Neither distinction changes what the query returns:

    * **Typed nulls.** ``real(null)`` and ``datetime(null)`` both build
      ``value=None, literal_kind="null"``. The type survives on
      ``result_type``, which is binder-populated and therefore stripped
      before hashing, so the two digest alike. Read ``result_type`` off a
      bound parse to get the declared type.
    * **Obfuscated strings.** ``h"x"`` and ``"x"`` both build
      ``value="x", literal_kind="string"``. The ``h`` marker asks the
      engine to redact the literal from telemetry and does not change
      which rows match.
    """

    kind: Literal["literal"] = "literal"
    value: str | int | float | bool | None
    literal_kind: Literal[
        "string", "int", "long", "real", "decimal", "bool", "datetime",
        "timespan", "dynamic", "guid", "null",
    ]
    # Exact .NET tick count (100ns units) for datetime and timespan literals;
    # None for every other kind. ``TimeSpan.TotalSeconds`` is a float and
    # loses sub-second exactness, so rebuild a ``timedelta`` from
    # ``ticks // 10`` microseconds. That is exact down to ``1microsecond``
    # (10 ticks); ``2tick`` floors to 0, and ``timedelta`` cannot represent
    # 200ns at any rate. This field is the only lossless form.
    ticks: int | None = None


class ColumnRef(Expr):
    """A column reference: ``T.Column`` or a bare column name."""

    kind: Literal["column_ref"] = "column_ref"
    name: str
    # A real table name, a scope name (a `let` alias), or None. The
    # `$left`/`$right` syntax a join's on-clause writes never lands here; its
    # side lives in `join_side`, so an unresolvable `$left.x` is honestly
    # None here. Reading through a `let` alias reports the alias -- see
    # ``SchemaAttacher.enrich``. Binder-populated, so ``semantic_hash``
    # strips it: the same query text hashes one way with or without a schema.
    table: str | None = None
    # Which side of a join the reference was written against, when the query
    # said so with `$left.` / `$right.`. The builder sets it on every such
    # reference, resolved or not, and it is the sole carrier of the side.
    # The side is semantic: `$left.a == $left.b` differs from the join
    # `$left.a == $right.b`, and losing it on a bound parse would collapse
    # the two.
    join_side: Literal["left", "right"] | None = None


class LetValueRef(Expr):
    """A reference, in expression position, to a name an earlier ``let`` bound.

    ``threshold`` in ``let threshold = 5; T | where Count > threshold`` is a
    query-local constant. Lowering it to a :class:`ColumnRef` would have the
    IR say the filter reads two columns. ``find_all(ir, ColumnRef)`` is the
    documented way to ask which columns a query touches, so column lineage,
    schema-drift checks, and rename impact analysis would count the ``let``
    name among them, and every binder lookup for it would fail.

    A name a ``let`` bound is neither a table nor a column. This node is the
    expression-position twin of :class:`~kustology.ir.query.LetRef`, which
    covers the *source* position (``let Base = T | …; Base | count``).

    It is not a ``ColumnRef`` subclass: the binder places columns by
    ``isinstance``, so a subclass would inherit the resolution this node
    exists to stop. Nothing types it but ``map_semantic_info``, which copies
    the .NET ``ResultType`` the parser already computed.

    It also carries an equivalence the hash is documented to have. A ``let``
    name is a local label, so ``compute_semantic_hash`` renames every binding
    to its declaration index. That rename reaches only nodes that *are*
    ``let`` names, since a real column called ``n`` is a different query;
    membership in ``transforms._LET_NAME_MODELS`` is what carries it to the
    use site along with the declaration. So ``let n = 5; T | where a > n``
    and ``let m = 5; T | where a > m`` hash together.

    Known limitation: a ``let`` name that shadows a real column. The
    classification comes from the query text alone ("is this name bound by
    an earlier ``let`` statement"), while KQL resolves an unqualified name to
    a **row-scope column first** and falls back to a ``let``-bound variable
    only when no column matches. So in

        let Count = 5; T | where Count > 1

    where ``T`` really has a ``Count`` column, KQL reads the column and this
    builder records a ``LetValueRef``. ``find_all(ir, ColumnRef)`` then does
    not report ``Count``, and ``semantic_hash`` collapses the query onto
    ``let Other = 5; T | where Other > 1``, which compares two constants,
    because the ``let`` rename reaches both. The same applies to a column an
    operator creates mid-pipeline: ``let a = 5; T | extend a = 1 | where a >
    0`` classifies the ``where``'s ``a`` as a ``LetValueRef`` too.

    Getting it right needs the binder. The .NET parser resolves the shadowed
    name to a ``ColumnSymbol`` and the non-shadowed one to a
    ``VariableSymbol``, but *only on a bound parse*; an unbound
    ``KustoCode.Parse`` leaves ``ReferencedSymbol`` ``None`` on every name.
    Classifying by symbol would build a ``ColumnRef`` with a schema and a
    ``LetValueRef`` without one for identical query text. That is a
    difference in IR *shape*, which no volatile-field stripping can hide (see
    ``transforms._VOLATILE_FIELDS``), so the same query would hash two ways
    depending on whether the caller supplied a schema, breaking the
    bind-independence invariant
    ``tests/ir/test_semantic_hash_bind_invariance.py`` holds. Shadowing a
    column with a ``let`` is rare, and a bind-dependent hash is load-bearing
    for every consumer that stores digests. The text-only rule is the one
    :class:`~kustology.ir.query.LetRef` applies at source position, and
    ``tests/ir/test_let_value_ref.py`` pins the behavior above.
    """

    kind: Literal["let_value_ref"] = "let_value_ref"
    name: str


class TypedNameDecl(Expr):
    """A ``name:type`` declaration sitting in expression position.

    The parser's ``NameAndTypeDeclaration``: the typed capture of
    ``parse a with 'x' b:long``, a typed column of ``find … project a:string``,
    a ``scan declare`` slot. It is a *declaration*, so the column does not
    exist until this operator creates it. That is why it is its own node
    instead of a :class:`ColumnRef` carrying a type.

    Both halves are data. Lowering the declaration to a bare name would read
    the ``Name`` child and never the ``Type``, so ``parse a with 'x' b:long``
    and ``parse a with 'x' b`` would build identical IR and carry the same
    ``semantic_hash``, though the first states the capture's type and the
    second leaves it a string. ``declared_type`` is source text: the type as
    the query wrote it (``long``, ``string``, ``datetime``), never a resolved
    :class:`~kustology.ir.types.KustoType`. A consumer that wants the enum
    can map it; one that wants to know what the query said cannot recover it
    from a mapped-and-defaulted value.
    """

    kind: Literal["typed_name"] = "typed_name"
    name: str
    declared_type: str


class FuncCall(Expr):
    """A function call: ``name(args...)``."""

    kind: Literal["func_call"] = "func_call"
    name: str
    args: list[AnyExpr]
    is_time_func: bool = False


class BinOp(Expr):
    """A binary operation: ``left op right``."""

    kind: Literal["bin_op"] = "bin_op"
    op: str
    # ``None`` on the arithmetic operators (``+ - * / %``): both fields are
    # categories of *comparison*, so neither is a question the node can be
    # asked there. Defaulting them would have every ``a + 1`` answer a
    # consumer that filters on ``case_sensitive`` (the example in ``walk``'s
    # own docstring).
    polarity: Literal["inclusion", "exclusion"] | None
    case_sensitive: bool | None = True
    left: AnyExpr
    right: AnyExpr


class SetMembership(Expr):
    """A set-membership test: ``column in (values)``."""

    kind: Literal["set_membership"] = "set_membership"
    # The literal KQL operator: in, !in, in~, !in~, has_any, has_all. Source
    # of truth, as on ``BinOp``: ``polarity`` and ``case_sensitive`` are
    # derived from it and kept for convenience. Two booleans give four states
    # for six operators, so without ``op`` the node would collapse ``in~``,
    # ``has_any`` and ``has_all`` into one indistinguishable shape with an
    # identical ``semantic_hash``, though ``has_any`` and ``has_all`` are
    # opposites (OR against AND of term matches) and ``in~`` compares whole
    # values rather than terms.
    op: str
    column: AnyExpr
    values: list[AnyExpr]
    polarity: Literal["inclusion", "exclusion"]
    case_sensitive: bool = False


class Between(Expr):
    """A range test: ``target between (low .. high)``."""

    kind: Literal["between"] = "between"
    target: AnyExpr
    low: AnyExpr
    high: AnyExpr
    polarity: Literal["inclusion", "exclusion"]


class And(Expr):
    """A conjunction of operands: ``a and b and ...``."""

    kind: Literal["and"] = "and"
    operands: list[AnyExpr]


class Or(Expr):
    """A disjunction of operands: ``a or b or ...``."""

    kind: Literal["or"] = "or"
    operands: list[AnyExpr]


class Not(Expr):
    """A negation: ``not (operand)``."""

    kind: Literal["not"] = "not"
    operand: AnyExpr


class RegexMatch(Expr):
    """A regular-expression match: ``target matches regex pattern``."""

    kind: Literal["regex_match"] = "regex_match"
    target: AnyExpr
    pattern: str
    case_sensitive: bool = True


class Exists(Expr):
    """A null/empty test: ``isnull``, ``isnotnull``, ``isempty``, ``isnotempty``.

    All four lower here, the full symmetric set, so a consumer asking "which
    columns does this query null-check" through ``find_all(ir, Exists)`` sees
    ``isnull(C)`` and ``isnotnull(C)`` alike. None of them falls back to a
    :class:`FuncCall` identified by a name string.
    """

    kind: Literal["exists"] = "exists"
    # Which function produced this. The four are four different predicates:
    # ``isnotempty`` also rejects ``""`` where ``isnotnull`` does not, and
    # each pair is the other's negation. Without this field the two positive
    # forms would lower to the same node with the same semantic_hash.
    op: str
    # ``inclusion`` for ``isnotnull``/``isnotempty``, ``exclusion`` for
    # ``isnull``/``isempty``. Redundant with ``op``, the way
    # ``BinOp.polarity`` is redundant with ``!=``, and kept for the same
    # reason: a caller asking "does this query test for absence" filters on
    # it without needing a table of which names carry a negation. ``op``
    # stays the source of truth; ``canonical()`` and the LLM view render ``op``.
    polarity: Literal["inclusion", "exclusion"]
    target: AnyExpr


class CaseExpr(Expr):
    """A ``case(cond1, val1, ..., default)`` expression."""

    kind: Literal["case"] = "case"
    branches: list[tuple[AnyExpr, AnyExpr]]
    default: AnyExpr | None = None


class PathExpr(Expr):
    """A dotted path access: ``expression.selector``."""

    kind: Literal["path"] = "path"
    expression: AnyExpr
    selector: AnyExpr


class ElementExpr(Expr):
    """An indexed access: ``expression[selector]``."""

    kind: Literal["element"] = "element"
    expression: AnyExpr
    selector: AnyExpr


class StarExpr(Expr):
    """A wildcard: ``*``."""

    kind: Literal["star"] = "star"


class NamedExpr(Expr):
    """A named expression: ``name = expression``."""

    kind: Literal["named"] = "named"
    name: str
    expression: AnyExpr


class CompoundNamedExpr(Expr):
    """A multi-name expression: ``(name1, name2) = expression``."""

    kind: Literal["compound_named"] = "compound_named"
    names: list[str]
    expression: AnyExpr


class UnaryOp(Expr):
    """A unary operation: ``op operand``."""

    kind: Literal["unary_op"] = "unary_op"
    op: str
    operand: AnyExpr


class BracketedExpr(Expr):
    """A parenthesized expression: ``(expression)``."""

    kind: Literal["bracketed"] = "bracketed"
    expression: AnyExpr


class ToScalarExpr(Expr):
    """``toscalar(...)``: a whole tabular pipeline reduced to one value.

    ``pipeline`` is a typed :class:`~kustology.ir.query.Pipeline`, and the
    typing is load-bearing. Declared ``Any`` (the cheap way around the
    ``expr`` / ``query`` import cycle), pydantic would be told nothing: an
    in-memory IR would look correct and ``walk`` would reach inside it, but
    ``model_validate_json`` would reload the nested query as a plain dict.
    The reloaded IR would not equal the one it came from; ``walk`` yields
    models, so a dict of primitives hides the inner query from it; and
    ``compute_semantic_hash`` strips spans by walking, so every offset inside
    would stay in the digest and rehashing stored IR would not reproduce its
    own hash.

    ``query.py`` imports this module, so ``Pipeline`` is a forward reference
    here. ``query.py`` rebuilds this class and :class:`SubqueryExpr` once
    ``Pipeline`` is defined, resolving the reference from its own namespace.

    ``| None`` keeps the accepted payloads wider than the produced ones: the
    builder always populates the field, and ``None`` stays valid so a payload
    carrying it still loads.
    """

    kind: Literal["to_scalar"] = "to_scalar"
    pipeline: "Pipeline | None"


class SubqueryExpr(Expr):
    """A bare tabular subquery sitting in expression position.

    KQL allows a pipeline as the value set of a membership test:
    ``| where User in ((Suspicious | project User))``. The pipeline arrives
    naked, with no wrapping function to name it after the way
    ``ToScalarExpr`` has one. Modeling it keeps the subtree reachable by
    ``walk``/``find_all``, where an ``UnknownExpr`` would collapse a whole
    inner query into a blob of raw text.

    See :class:`ToScalarExpr` for why ``pipeline`` is a forward reference and
    why its typing is load-bearing.
    """

    kind: Literal["subquery"] = "subquery"
    pipeline: "Pipeline | None"


class ExternalDataExpr(Expr):
    """``externaldata(...)[...]`` in expression position: a membership set.

    The source-position form is
    :class:`~kustology.ir.query.ExternalDataSource`; both are filled by
    :func:`~kustology.ir._builder_helpers.read_external_data` so they cannot
    drift apart.

    ``uris`` is a list because the construct takes one, and a feed stitched
    from two URIs differs from the feed from either one, so dropping entries
    would make two different queries build the same node. As on the source
    class, an entry is **not guaranteed to be a URI**: an element that does
    not fold to a literal (a ``let``-bound feed URL, ``strcat(...)``) is
    recorded as its own source text.

    ``properties`` mirrors the source class: the whole ``with (...)`` clause,
    keys verbatim, with ``format`` promoted to its own field as well. See
    :class:`~kustology.ir.query.ExternalDataSource` for why dropping the
    other properties would be a collision.
    """

    kind: Literal["external_data"] = "external_data"
    columns: list[tuple[str, str]]
    uris: list[str]
    format: str | None = None
    properties: dict[str, str] = {}


class DataTableExpr(Expr):
    """``datatable(...)[...]`` in expression position: a membership set.

    The source-position form is
    :class:`~kustology.ir.query.DataTableSource`; both are filled by the
    builder's ``_read_datatable`` so they cannot drift apart. The values are
    the query. See ``DataTableSource`` for why dropping them would be a
    collision; in expression position the unmodeled fallback is worse still,
    an ``UnknownExpr`` hashing its own source text.
    """

    kind: Literal["datatable"] = "datatable"
    columns: list[tuple[str, str]]
    rows: list[list[AnyExpr]]


class UnknownExpr(Expr):
    """An expression the builder does not model, kept as raw source text."""

    kind: Literal["unknown_expr"] = "unknown_expr"
    raw_text: str
    ast_kind: str
    reason: str


# Rebuilt at the bottom of ``query.py``, the first point at which both halves
# of the ``expr`` <-> ``query`` cycle exist. ``ToScalarExpr.pipeline`` and
# ``SubqueryExpr.pipeline`` are forward references to ``Pipeline``, and both
# classes are members of ``AnyExpr``, so *every* expression class with an
# ``AnyExpr`` field needs them resolved: rebuilding any of them in this module
# fails. ``query.py`` imports this one, so a module-level rebuild there runs
# after these classes are defined.
REBUILT_BY_QUERY_MODULE: tuple[type[BaseModel], ...] = (
    LiteralExpr, ColumnRef, LetValueRef, TypedNameDecl, BinOp, SetMembership,
    Between, And, Or, Not,
    FuncCall, CaseExpr, RegexMatch, Exists, PathExpr, ElementExpr, StarExpr,
    NamedExpr, CompoundNamedExpr, UnaryOp, BracketedExpr,
    ToScalarExpr, SubqueryExpr, ExternalDataExpr, DataTableExpr,
)
