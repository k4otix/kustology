# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every kind in ``IRBuilder.HANDLED_OPERATOR_KINDS`` builds from real KQL.

``scripts/audit_syntax_kinds.py`` reads that frozenset as the list of shapes
the builder models, so a kind can sit in it while its dispatch arm raises
``AttributeError`` on valid KQL by reading a .NET member its node type lacks.

Each sample asserts three things: the query parses to the kind it is filed
under, ``to_ir()`` returns, and the result holds no ``UnknownOp``, the
fallback for dispatch falling through. A crashing arm fails the second; a
kind listed as handled with no arm fails the third.

The first assertion ties a sample to its kind. Microsoft's parser is
error-tolerant, so a mistyped sample parses, builds, and emits no
``UnknownOp`` while never producing the operator it is filed under. Checking
the parse tree for the class name catches that.

``test_sample_covers_every_handled_operator_kind`` keeps the two sets in
lockstep.
"""

import pytest

from kustology import parse
from kustology.ir import IRBuilder, UnknownOp, find_all

SAMPLES = {
  "FilterOperator": "T | where a == 1", "ExtendOperator": "T | extend b = 1", "SummarizeOperator": "T | summarize count() by a",
  "JoinOperator": "T | join (U) on a", "LookupOperator": "T | lookup (U) on a", "PartitionByOperator": "T | __partitionby a (take 1)",
  "PartitionOperator": "T | partition by a (top 1 by b)", "ProjectOperator": "T | project a", "ProjectAwayOperator": "T | project-away a",
  "ProjectKeepOperator": "T | project-keep a", "ProjectReorderOperator": "T | project-reorder a", "ProjectRenameOperator": "T | project-rename b = a",
  "ProjectByNamesOperator": "T | project-by-names a", "DistinctOperator": "T | distinct a", "TakeOperator": "T | take 1", "SampleOperator": "T | sample 1",
  "SortOperator": "T | sort by a", "TopOperator": "T | top 1 by a", "TopHittersOperator": "T | top-hitters 5 of a by b", "SearchOperator": "search 'x'",
  "UnionOperator": "union T, U", "MakeSeriesOperator": "T | make-series n=count() on t step 1h", "MvExpandOperator": "T | mv-expand a",
  "MvApplyOperator": "T | mv-apply a on (where a > 1)", "ParseOperator": "T | parse a with 'x' b", "ParseWhereOperator": "T | parse-where a with 'x' b",
  "AsOperator": "T | as X", "RangeOperator": "range x from 1 to 3 step 1", "RenderOperator": "T | render timechart", "EvaluateOperator": "T | evaluate bag_unpack(d)",
  "CountOperator": "T | count", "PrintOperator": "print 1", "FacetOperator": "T | facet by a", "GetSchemaOperator": "T | getschema", "InvokeOperator": "T | invoke f()",
  "FindOperator": "find in (T) where a == 1", "ForkOperator": "T | fork (take 1) (count)", "ScanOperator": "T | scan declare (s:long=0) with (step x: true => s = 1;)",
  "SerializeOperator": "T | serialize", "ConsumeOperator": "T | consume", "AssertSchemaOperator": "T | assert-schema (a:long)", "ExecuteAndCacheOperator": "T | __executeAndCache",
  "ParseKvOperator": "T | parse-kv a as (k:string)", "SampleDistinctOperator": "T | sample-distinct 1 of a", "TopNestedOperator": "T | top-nested 1 of a by count()",
  "MakeGraphOperator": "T | make-graph a --> b", "MacroExpandOperator": "macro-expand X as Y (T | count)", "GraphMatchOperator": "T | make-graph a --> b | graph-match (n)-[e]->(m) project n",
  "GraphMarkComponentsOperator": "T | make-graph a --> b | graph-mark-components", "GraphShortestPathsOperator": "T | make-graph a --> b | graph-shortest-paths (n)-[e*1..2]->(m) project n",
  "GraphToTableOperator": "T | make-graph a --> b | graph-to-table nodes", "GraphWhereEdgesOperator": "T | make-graph a --> b | graph-where-edges a == 1",
  "GraphWhereNodesOperator": "T | make-graph a --> b | graph-where-nodes a == 1",
}


def test_sample_covers_every_handled_operator_kind():
    """A kind with no sample is a coverage claim nothing exercises, and a
    sample for no kind is dead weight."""
    assert set(SAMPLES) == set(IRBuilder.HANDLED_OPERATOR_KINDS)


def _syntax_class_names(root) -> set[str]:
    """Return every Python class name in a parsed .NET syntax tree.

    ``ChildCount``/``GetChild`` is the generic descent
    ``scripts/audit_syntax_kinds.py`` uses. It passes through the structural
    wrappers (``List``, ``SeparatedElement``) a field-name walk would have to
    know about, and it reports class names, which is what ``IRBuilder``
    dispatches on.
    """
    seen: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        seen.add(type(node).__name__)
        try:
            child_count = node.ChildCount
        except AttributeError:
            continue                  # a token, not a node
        stack.extend(node.GetChild(i) for i in range(child_count))
    return seen


@pytest.mark.parametrize("kind,q", sorted(SAMPLES.items()))
def test_every_handled_operator_builds_without_unknown_op(kind, q):
    query = parse(q)
    assert kind in _syntax_class_names(query.syntax), (
        f"{q!r} does not parse to a {kind}, so this sample exercises some "
        f"other branch and {kind} is untested. The parser is error-tolerant, "
        f"so a wrong sample still builds cleanly -- fix the KQL."
    )
    ir = query.to_ir()                           # must not raise
    assert not list(find_all(ir, UnknownOp)), kind
