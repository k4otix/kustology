# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""LLM-friendly serialization of the IR.

``to_llm_dict`` renders any IR sub-tree into a JSON-safe dict optimized for
being fed to a language model:

* Every node carries a stable ``kind`` discriminator drawn from the class's
  ``KIND`` constant — the wire format uses snake_case KQL-aligned labels.
* A ``QueryIR`` root also carries ``ir_schema_version``, the same
  ``IR_SCHEMA_VERSION`` the CLI's JSON envelope publishes. The view is a
  lossy projection with no validator behind it, unlike ``model_dump_json``
  which pydantic re-validates, so without the tag a dump from an earlier
  release is indistinguishable from a query that did not use the fields a
  reader expects.
* Fields holding their declared default (``result_type=unresolved``,
  ``result_type_inner=None``, empty lists/dicts) are dropped.
* ``span`` (and ``LetFunction.body_span``) and ``schema_attached`` are
  stripped: character offsets need source-text triangulation, and
  ``schema_attached`` follows from whether ``result_schema`` is populated.
* ``Operator.result_schema`` is stripped; ``Pipeline.result_schema``
  survives. See :func:`_drop_operator_result_schema`.
* Enum values are unwrapped to their string form.
* ``canonical_form`` on ``ColumnRef`` / ``LiteralExpr`` leaves is dropped
  when it restates ``name`` / ``value``; it survives on subtree expressions
  (``BinOp``, ``And``, …) where it summarizes the tree. On literals the test
  is ``cf == _canonical_literal_repr(value)``, which re-renders ``value`` as
  KQL and so double-quotes any Python ``str``. The drop therefore fires
  unless ``value`` is a ``str`` for a kind that is no KQL string literal;
  such a kind keeps a ``canonical_form`` identical to its ``value``, so
  ``7d`` emits ``"value": "7.00:00:00"`` beside
  ``"canonical_form": "7.00:00:00"``. ``examples/llm_view.py`` probes every
  member of ``literal_kind`` and prints which side each falls on, so the
  membership stays measurable.
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

# Imported for isinstance/issubclass dispatch. Matching on a class name stops
# firing on a rename, and the rule it guards never applies again.
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

# Stripped from every node by name. ``span`` offsets need source-text
# triangulation, ``KIND`` duplicates the ``kind`` discriminator,
# ``schema_attached`` duplicates what ``result_schema`` conveys, and ``ticks``
# is the machine-exact companion to a ``value`` the LLM already reads.
# ``body_span`` is listed because the set matches field names exactly and
# ``LetFunction`` is the one model whose span field carries another name;
# matching a ``_span`` suffix would be the same trap one rename later.
_OMIT_FIELDS = {"span", "body_span", "schema_attached", "ticks"}

# Ceiling on ``DataTableSource.rows`` in the LLM view. Real threat-intel
# datatables run to thousands of IOC rows, which bury the query's structure in
# the context window. A ``rows_omitted`` count announces the truncation, so a
# short table is never mistaken for a complete one. ``model_dump_json`` stays
# lossless, which is what round-trip and ``semantic_hash`` depend on.
_MAX_LLM_DATATABLE_ROWS = 20


def to_llm_dict(node: Any) -> Any:
    """Render ``node`` (a pydantic IR model, list, or primitive) into an LLM-optimized dict.

    See the module docstring for the shape contract.
    """
    out = _convert(node)
    # Lazy imports: ``ir/__init__`` imports this module, and ``query``
    # participates in the expr <-> query cycle.
    from . import IR_SCHEMA_VERSION
    from .query import QueryIR

    if isinstance(node, QueryIR) and isinstance(out, dict):
        # Only the document root: a sub-tree dumped on its own is not a
        # document, and stamping every node would repeat one string hundreds
        # of times into the context window this view conserves. Placed second,
        # after the ``kind`` discriminator that leads every node.
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
        # ``Span`` has no ``kind`` field, so it falls back to the class name.
        out: dict[str, Any] = {
            "kind": cls.model_fields["kind"].default
            if "kind" in cls.model_fields
            else cls.__name__
        }
        for name, field_info in cls.model_fields.items():
            if name in _OMIT_FIELDS or name == "kind":
                continue
            v = getattr(node, name)
            if _is_default(v, field_info.default):
                continue
            if isinstance(v, (list, dict)) and len(v) == 0:
                continue
            out[name] = _convert(v)
        # A computed field (``QueryIR.semantic_hash``) has no ``model_fields``
        # entry, so the loop above never reaches it -- read it explicitly.
        # Reading forces the digest, the same cost ``model_dump()`` pays.
        for name in cls.model_computed_fields:
            if name in _OMIT_FIELDS:
                continue
            out[name] = _convert(getattr(node, name))
        # ``Expr.canonical_form`` is a derived property, surfaced for the LLM
        # because it summarizes subtrees the model would otherwise walk.
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
    """Remove ``canonical_form`` on leaf nodes where it duplicates ``name`` or ``value``.

    Higher-level expressions (BinOp, And, …) keep theirs because the canonical
    form summarizes a subtree the LLM would otherwise walk.

    For ColumnRef the bare-name match covers unbound nodes. Bound nodes
    canonicalize to ``"table.name"``, which restates the surrounding ``table``
    field, so that form drops too. ``LetValueRef`` is the same shape with no
    ``table`` to qualify it.
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
    """Reproduce the KQL canonical form for a primitive literal.

    Strings get double-quoted, bools and ``None`` render lowercase, and
    numbers are stringified.

    ``_normalize._kql_string`` does the quoting. This function answers one
    question, "is ``canonical_form`` a restatement of ``value``", and two
    independent renderings of the same thing answer it wrongly the moment they
    disagree: a string holding a quote or a backslash would then keep a
    ``canonical_form`` that restates the ``value`` beside it.
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

    See :data:`_MAX_LLM_DATATABLE_ROWS`. ``rows_omitted`` appears only when
    rows really were omitted, so its presence means what it says and a short
    datatable carries no such key.
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

    ``Operator.result_schema`` is the column list the operator emits, read off
    Microsoft's binder. On a bound parse most operators emit the columns the
    one before them emitted, so a pipeline of *n* steps restates one column
    list *n* times, which a view built for context economy cannot afford.
    Measured across the 49-query fixture corpus, bound against a schema naming
    every referenced column, the per-operator copies were 35% of the whole LLM
    view (295,156 of 851,224 bytes): with them the view was a median 28%
    smaller than ``model_dump_json`` on the same query, without them it is
    45%. ``CHANGELOG.md``'s ``to_llm_dict`` entry carries those numbers; keep
    the two in step.

    ``Pipeline.result_schema`` stays: "what columns does this query return" is
    one answer per pipeline, and it is the answer a reader asks for. The
    per-*step* column list is what goes, and ``model_dump_json`` keeps it, the
    same split :func:`_cap_datatable_rows` makes for ``DataTableSource.rows``.
    ``issubclass`` does the scoping; the field name in :data:`_OMIT_FIELDS`
    would match across every model and take ``Pipeline``'s with it.
    """
    from .query import Operator  # lazy import: avoids cycle at module load

    if issubclass(cls, Operator):
        out.pop("result_schema", None)


def _drop_inapplicable_operator_flags(out: dict[str, Any], cls: type) -> None:
    """Remove ``BinOp``'s ``polarity`` / ``case_sensitive`` when ``None``.

    ``None`` there means the operator is arithmetic and the question does not
    apply — see :class:`~kustology.ir.expr.BinOp`. ``polarity`` is a
    *required* field, so the default-stripping pass above cannot drop it and
    the dump would carry an explicit ``"polarity": null``, which invites a
    model to ask what a null case-sensitivity means when the answer is that
    the node was never asked.

    ``issubclass`` scopes this to ``BinOp``, like both its siblings. Applied
    to every node in the IR it would strip a future model's legitimately
    optional ``case_sensitive``, leaving the reader unable to tell an absent
    field from a null one. It runs before
    :func:`_collapse_polarity_into_op`, whose ``None`` guard then reads the
    key as absent and leaves the node alone, since there is no polarity left
    to fold into ``op``.
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

    Synthesizing ``SetMembership`` the way ``Between`` is synthesized would
    emit ``op: "in"`` for ``has_any`` and ``has_all``, and since
    ``case_sensitive`` defaults to ``False`` the default-stripping pass above
    would remove that field too, so a model would read ``has_all`` as a bare,
    case-sensitive ``in``.
    """
    polarity = out.get("polarity")
    if polarity is None:
        return
    if issubclass(cls, (BinOp, Exists, SetMembership)):
        del out["polarity"]
    elif issubclass(cls, Between):
        out["op"] = "!between" if polarity == "exclusion" else "between"
        del out["polarity"]
