# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""LLM-friendly serialization of the IR.

``to_llm_dict`` renders any IR sub-tree into a JSON-safe dict optimized for
being fed to a language model:

* Every node carries a stable ``kind`` discriminator drawn from the class's
  ``KIND`` constant — the wire format uses snake_case KQL-aligned labels.
* Fields holding their declared default (``nullable=True``,
  ``result_type=unknown``, empty lists/dicts) are dropped.
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

# Stripped from every node by name. Spans aren't useful without source-text
# triangulation; ``schema_attached`` duplicates what ``result_schema`` already
# conveys.
_OMIT_FIELDS = {"span", "schema_attached"}

# Field renames keyed by class. Lazy-populated to avoid a circular import
# at module load (this module imports the IR, which imports model classes
# that have ``KIND`` constants defined in their own modules).
_FIELD_RENAMES: dict[type, dict[str, str]] = {}


def _ensure_renames_initialized() -> None:
    if _FIELD_RENAMES:
        return
    from .query import JoinOp, LookupOp, RenderOp
    _FIELD_RENAMES[RenderOp] = {"kind": "render_kind"}
    _FIELD_RENAMES[JoinOp] = {"kind": "join_kind"}
    _FIELD_RENAMES[LookupOp] = {"kind": "lookup_kind"}


def to_llm_dict(node: Any) -> Any:
    """Render ``node`` (a pydantic IR model, list, or primitive) into an
    LLM-optimized dict. See module docstring for the shape contract."""
    _ensure_renames_initialized()
    return _convert(node)


def _convert(node: Any) -> Any:
    if isinstance(node, BaseModel):
        cls = type(node)
        out: dict[str, Any] = {"kind": getattr(cls, "KIND", cls.__name__)}
        renames = _FIELD_RENAMES.get(cls, {})
        for name, field_info in cls.model_fields.items():
            if name in _OMIT_FIELDS:
                continue
            v = getattr(node, name)
            if _is_default(v, field_info.default):
                continue
            if isinstance(v, (list, dict)) and len(v) == 0:
                continue
            out[renames.get(name, name)] = _convert(v)
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
    # Enum comparison: a KustoType field with default KustoType.UNKNOWN
    # should match an actual KustoType.UNKNOWN instance.
    return value == default


def _drop_redundant_canonical_form(out: dict[str, Any], cls: type) -> None:
    """Remove ``canonical_form`` on leaf nodes where it duplicates ``name`` or
    ``value``. Higher-level expressions (BinOp, And, …) keep theirs because
    the canonical form summarizes a subtree the LLM would otherwise walk."""
    cf = out.get("canonical_form")
    if cf is None:
        return
    name = cls.__name__
    if name == "ColumnRef" and cf == out.get("name"):
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
