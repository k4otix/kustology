# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""LLM-friendly serialization of the IR.

``to_llm_dict`` renders any IR sub-tree into a JSON-safe dict optimized for
being fed to a language model:

* Every node carries a stable ``kind`` discriminator drawn from the class's
  ``KIND`` constant — the wire format uses snake_case KQL-aligned labels.
* Fields holding their declared default (``result_type=unknown``,
  ``result_type_inner=None``, empty lists/dicts) are dropped.
* ``span`` and ``schema_attached`` are stripped — character offsets aren't
  useful without source-text triangulation, and ``schema_attached`` is
  inferrable from whether ``result_schema`` is populated.
* Enum values are unwrapped to their string form.
* ``canonical_form`` on ``ColumnRef`` / ``LiteralExpr`` leaves is dropped
  when it's a literal restatement of ``name`` / ``value``; survives on
  subtree expressions (``BinOp``, ``And``, …) where it summarizes the tree.
* ``polarity`` on ``BinOp`` / ``SetMembership`` / ``Between`` is collapsed
  into ``op`` so the LLM reads natural KQL (``!=``, ``!contains``, ``!in``,
  ``!between``) instead of IR-canonical ``op + polarity`` pairs.
* Three operators (``render``, ``join``, ``lookup``) carry a KQL ``kind``
  field that collides with the discriminator key; they're renamed to
  ``render_kind`` / ``join_kind`` / ``lookup_kind`` in the LLM output.

Use :meth:`~kustology.ir.QueryIR.model_dump_json` for canonical,
lossless round-trip; ``to_llm_dict`` for handing the IR off to a model.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

# Stripped from every node by name. ``span`` and ``KIND``-ClassVar metadata
# aren't useful for the LLM (offsets need source-text triangulation, KIND
# duplicates the ``kind`` discriminator). ``schema_attached`` duplicates what
# ``result_schema`` already conveys.
# ``ticks`` is the machine-exact companion to ``value``; an LLM reads the
# rendered value, so emitting both is noise.
_OMIT_FIELDS = {"span", "schema_attached", "ticks"}


def to_llm_dict(node: Any) -> Any:
    """Render ``node`` (a pydantic IR model, list, or primitive) into an
    LLM-optimized dict. See module docstring for the shape contract."""
    return _convert(node)


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
        _collapse_polarity_into_op(out, cls)
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
    """
    cf = out.get("canonical_form")
    if cf is None:
        return
    name = cls.__name__
    if name == "ColumnRef":
        col_name = out.get("name")
        table = out.get("table")
        if cf == col_name or (table and cf == f"{table}.{col_name}"):
            del out["canonical_form"]
    elif name == "LiteralExpr" and cf == _canonical_literal_repr(out.get("value")):
        del out["canonical_form"]


def _canonical_literal_repr(value: Any) -> str:
    """Reproduce the KQL canonical form for a primitive literal: strings get
    double-quoted, bools/None lowercase, numbers stringified."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _collapse_polarity_into_op(out: dict[str, Any], cls: type) -> None:
    """Collapse ``polarity`` so the LLM view reads natural KQL operators.

    Builder behavior differs by node:

    * ``BinOp.op`` already carries the literal KQL string with ``!`` baked
      in (``!=``, ``!contains``). Polarity is redundant → drop it.
    * ``SetMembership`` / ``Between`` have no ``op`` field on the model;
      polarity is the only signal. Synthesize ``op: "in"/"!in"`` or
      ``op: "between"/"!between"`` and drop polarity.
    """
    polarity = out.get("polarity")
    if polarity is None:
        return
    name = cls.__name__
    if name == "BinOp":
        del out["polarity"]
    elif name == "SetMembership":
        out["op"] = "!in" if polarity == "exclusion" else "in"
        del out["polarity"]
    elif name == "Between":
        out["op"] = "!between" if polarity == "exclusion" else "between"
        del out["polarity"]
