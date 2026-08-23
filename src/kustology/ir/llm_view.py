# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""LLM-friendly serialization of the IR.

``to_llm_dict`` renders any IR sub-tree into a JSON-safe dict optimized for
being fed to a language model:

* Every node carries a stable ``kind`` discriminator drawn from the class's
  ``KIND`` constant — the wire format uses snake_case KQL-aligned labels.
* A ``QueryIR`` root additionally carries ``ir_schema_version``. The view is
  a lossy projection with no validator behind it, so unlike
  ``model_dump_json`` — which pydantic re-validates — a dump from an earlier
  release is indistinguishable from a query that simply did not use the
  fields a reader expects. The tag is the same ``IR_SCHEMA_VERSION`` the
  CLI's JSON envelope publishes.
* Fields holding their declared default (``result_type=unresolved``,
  ``result_type_inner=None``, empty lists/dicts) are dropped.
* ``span`` (and ``LetFunction.body_span``) and ``schema_attached`` are
  stripped — character offsets aren't useful without source-text
  triangulation, and ``schema_attached`` is inferrable from whether
  ``result_schema`` is populated.
* ``Operator.result_schema`` is stripped; ``Pipeline.result_schema``
  survives. See :func:`_drop_operator_result_schema`.
* Enum values are unwrapped to their string form.
* ``canonical_form`` on ``ColumnRef`` / ``LiteralExpr`` leaves is dropped
  when it's a literal restatement of ``name`` / ``value``; survives on
  subtree expressions (``BinOp``, ``And``, …) where it summarizes the tree.
* ``polarity`` on ``BinOp`` / ``SetMembership`` / ``Exists`` / ``Between``
  is collapsed into ``op`` so the LLM reads natural KQL (``!=``,
  ``!contains``, ``!in``, ``isnull``, ``!between``) instead of IR-canonical
  ``op + polarity`` pairs. ``polarity`` and ``case_sensitive`` are dropped
  outright where they are ``None``, which on ``BinOp`` means the operator is
  arithmetic and neither question applies.

Use :meth:`~kustology.ir.QueryIR.model_dump_json` for canonical,
lossless round-trip; ``to_llm_dict`` for handing the IR off to a model.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

# Imported for isinstance/issubclass dispatch rather than matched by class
# name. Name-string dispatch silently stops firing when a class is renamed:
# the rule just never applies again and the LLM view quietly regresses.
from ._normalize import _kql_string
from .expr import (
    Between,
    BinOp,
    ColumnRef,
    Exists,
    LetValueRef,
    LiteralExpr,
    SetMembership,
)

# Stripped from every node by name. ``span`` and ``KIND``-ClassVar metadata
# aren't useful for the LLM (offsets need source-text triangulation, KIND
# duplicates the ``kind`` discriminator). ``schema_attached`` duplicates what
# ``result_schema`` already conveys.
# ``ticks`` is the machine-exact companion to ``value``; an LLM reads the
# rendered value, so emitting both is noise.
#
# ``body_span`` is here because the set matches field names exactly, and
# ``LetFunction`` is the one model whose span field is not called ``span``.
# Matching on a ``_span`` suffix instead would be the same trap one rename
# later; an explicit name is checkable.
_OMIT_FIELDS = {"span", "body_span", "schema_attached", "ticks"}

# Ceiling on ``DataTableSource.rows`` in the LLM view. Real threat-intel
# datatables run to thousands of IOC rows; handing all of them to a model
# buries the query's structure in data it cannot use and costs the context
# window that structure needs. The truncation is announced with a
# ``rows_omitted`` count so the reader is never shown a short table that
# looks complete. It is a *view* concern only: ``model_dump_json`` stays
# lossless, which is what round-trip and ``semantic_hash`` depend on.
_MAX_LLM_DATATABLE_ROWS = 20


def to_llm_dict(node: Any) -> Any:
    """Render ``node`` (a pydantic IR model, list, or primitive) into an
    LLM-optimized dict. See module docstring for the shape contract."""
    out = _convert(node)
    # Lazy imports: ``ir/__init__`` imports this module, and ``query``
    # participates in the expr <-> query cycle.
    from . import IR_SCHEMA_VERSION
    from .query import QueryIR

    if isinstance(node, QueryIR) and isinstance(out, dict):
        # Only the document root. Stamping every node would repeat one string
        # hundreds of times into the context window this view exists to
        # conserve, and a sub-tree dumped on its own is not a document.
        # Placed second so it reads before the body, after the ``kind``
        # discriminator that leads every node.
        out = {
            "kind": out["kind"],
            "ir_schema_version": IR_SCHEMA_VERSION,
            **{k: v for k, v in out.items() if k != "kind"},
        }
    return out


def _convert(node: Any) -> Any:
    if isinstance(node, BaseModel):
        from .expr import Expr  # lazy import: avoids cycle at module load

        cls = type(node)
        # ``kind`` is the Pydantic discriminator field on every IR class;
        # emit it first so it leads the dict (LLM scanning convention).
        out: dict[str, Any] = {"kind": getattr(cls, "KIND", cls.__name__)}
        for name, field_info in cls.model_fields.items():
            if name in _OMIT_FIELDS or name == "kind":
                continue
            v = getattr(node, name)
            if _is_default(v, field_info.default):
                continue
            if isinstance(v, (list, dict)) and len(v) == 0:
                continue
            out[name] = _convert(v)
        # ``Expr.canonical_form`` is a derived property (not a model field);
        # surface it for the LLM since it summarizes subtrees the model would
        # otherwise have to walk.
        if isinstance(node, Expr):
            out["canonical_form"] = node.canonical_form
        _drop_redundant_canonical_form(out, cls)
        _drop_operator_result_schema(out, cls)
        _drop_inapplicable_operator_flags(out, cls)
        _collapse_polarity_into_op(out, cls)
        _cap_datatable_rows(out, cls)
        return out
    if isinstance(node, list):
        return [_convert(v) for v in node]
    if isinstance(node, tuple):
        # JSON has no tuple — emit as list. Used by CaseExpr.branches and
        # ExternalDataExpr.columns.
        return [_convert(v) for v in node]
    if isinstance(node, dict):
        return {k: _convert(v) for k, v in node.items()}
    if isinstance(node, Enum):
        return node.value
    return node


def _is_default(value: Any, default: Any) -> bool:
    if default is PydanticUndefined:
        return False
    # Enum comparison: a KustoType field with default KustoType.UNRESOLVED
    # should match an actual KustoType.UNRESOLVED instance.
    return value == default


def _drop_redundant_canonical_form(out: dict[str, Any], cls: type) -> None:
    """Remove ``canonical_form`` on leaf nodes where it duplicates ``name`` or
    ``value``. Higher-level expressions (BinOp, And, …) keep theirs because
    the canonical form summarizes a subtree the LLM would otherwise walk.

    For ColumnRef the bare-name match covers unbound nodes; bound nodes
    canonicalize to ``"table.name"``, which is also a literal restatement
    once the LLM has the surrounding ``table`` field — so drop that too.
    ``LetValueRef`` is the same shape with no ``table`` to qualify it.
    """
    cf = out.get("canonical_form")
    if cf is None:
        return
    if issubclass(cls, (ColumnRef, LetValueRef)):
        col_name = out.get("name")
        table = out.get("table")
        if cf == col_name or (table and cf == f"{table}.{col_name}"):
            del out["canonical_form"]
    elif issubclass(cls, LiteralExpr) and cf == _canonical_literal_repr(out.get("value")):
        del out["canonical_form"]


def _canonical_literal_repr(value: Any) -> str:
    """Reproduce the KQL canonical form for a primitive literal: strings get
    double-quoted, bools/None lowercase, numbers stringified.

    The quoting is delegated to ``_normalize._kql_string`` rather than
    re-spelled here. This function only exists to answer "is
    ``canonical_form`` a restatement of ``value``", and two independent
    renderings of the same thing answer that question wrongly the moment they
    disagree — which they did, for every string containing a quote or a
    backslash: the drop stopped firing and the LLM view carried a
    ``canonical_form`` restating the ``value`` beside it.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _kql_string(value)
    return str(value)


def _cap_datatable_rows(out: dict[str, Any], cls: type) -> None:
    """Truncate a ``DataTableSource``'s rows, recording how many were cut.

    See :data:`_MAX_LLM_DATATABLE_ROWS`. ``rows_omitted`` is added only when
    something really was omitted, so a short datatable reads exactly as it
    did before and the key's presence means what it says.
    """
    from .query import DataTableSource  # lazy import: avoids cycle at module load

    if not issubclass(cls, DataTableSource):
        return
    rows = out.get("rows")
    if not isinstance(rows, list) or len(rows) <= _MAX_LLM_DATATABLE_ROWS:
        return
    out["rows"] = rows[:_MAX_LLM_DATATABLE_ROWS]
    out["rows_omitted"] = len(rows) - _MAX_LLM_DATATABLE_ROWS


def _drop_operator_result_schema(out: dict[str, Any], cls: type) -> None:
    """Remove ``result_schema`` from an operator node. Pipelines keep theirs.

    ``Operator.result_schema`` is the column list the operator emits, read
    off Microsoft's binder. It is the right thing to carry on the model and
    the wrong thing to repeat in a view whose whole purpose is context
    economy: on a bound parse most operators emit the columns the one before
    them emitted, so a pipeline of *n* steps restates one column list *n*
    times. Measured across the 49-query fixture corpus, bound against a
    schema naming every referenced column, the per-operator copies were 68%
    of the whole LLM view — they took its size advantage over
    ``model_dump_json`` from a median 51% down to 28%.

    ``Pipeline.result_schema`` is not dropped: "what columns does this query
    return" is one answer per pipeline, and it is the answer a reader
    actually asks for. What is lost is the per-*step* column list;
    ``model_dump_json`` keeps it, which is the same split
    :func:`_cap_datatable_rows` makes for ``DataTableSource.rows``.

    Scoped by ``issubclass`` rather than by putting the field name in
    :data:`_OMIT_FIELDS`, which matches names across every model and would
    take ``Pipeline``'s with it.
    """
    from .query import Operator  # lazy import: avoids cycle at module load

    if issubclass(cls, Operator):
        out.pop("result_schema", None)


def _drop_inapplicable_operator_flags(out: dict[str, Any], cls: type) -> None:
    """Remove ``BinOp``'s ``polarity`` / ``case_sensitive`` when ``None``.

    ``None`` there means "this operator is arithmetic, so the question does
    not apply" — see :class:`~kustology.ir.expr.BinOp`. ``polarity`` is a
    *required* field, so the default-stripping pass above cannot drop it and
    the dump would carry an explicit ``"polarity": null``. A null field is
    worse than an absent one for a model reading the dump: it invites the
    question of what a null case-sensitivity means, when the answer is that
    the node was never asked.

    Scoped to ``BinOp`` by ``issubclass``, like both its siblings. Written
    without the class it took only the dict, so it reached every node in the
    IR and would silently strip a future model's legitimately-optional
    ``case_sensitive`` — leaving the reader unable to tell an absent field
    from a null one, which is the exact distinction this function exists to
    make.

    Runs before :func:`_collapse_polarity_into_op`, whose ``None`` guard then
    reads the key as absent and leaves the node alone — the right outcome,
    since there is no polarity to fold into ``op``.
    """
    if not issubclass(cls, BinOp):
        return
    for field in ("polarity", "case_sensitive"):
        if field in out and out[field] is None:
            del out[field]


def _collapse_polarity_into_op(out: dict[str, Any], cls: type) -> None:
    """Collapse ``polarity`` so the LLM view reads natural KQL operators.

    Builder behavior differs by node:

    * ``BinOp.op``, ``SetMembership.op`` and ``Exists.op`` already carry the
      literal KQL string with the negation baked in (``!=``, ``!contains``,
      ``!in~``, ``isnull``). Polarity is redundant → drop it.
    * ``Between`` has no ``op`` field on the model; polarity is the only
      signal, and ``between``/``!between`` is a closed two-member set that
      polarity fully determines. Synthesize it and drop polarity.

    ``SetMembership`` used to be synthesized like ``Between``, which was
    the worst of both: it emitted ``op: "in"`` for ``has_any`` and
    ``has_all``, and because ``case_sensitive`` defaults to ``False`` the
    default-stripping pass above removed that field too — so a model was
    shown ``has_all`` as a bare, case-sensitive ``in``.
    """
    polarity = out.get("polarity")
    if polarity is None:
        return
    if issubclass(cls, (BinOp, Exists, SetMembership)):
        del out["polarity"]
    elif issubclass(cls, Between):
        out["op"] = "!between" if polarity == "exclusion" else "between"
        del out["polarity"]
