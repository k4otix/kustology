# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Every kind in ``IRBuilder.HANDLED_OPERATOR_KINDS`` builds from real KQL.

``HANDLED_OPERATOR_KINDS`` is a public contract -- ``scripts/audit_syntax_kinds.py``
reads it as the list of shapes the builder claims to model, and the coverage
audit trusts it. Nothing checked that the claim held for every entry: two
branches (``TopHittersOperator``, ``PartitionByOperator``) read .NET members
that do not exist on their node types and raised ``AttributeError`` on valid
KQL, while still being listed as handled.

So this file pins one buildable sample per handled kind and asserts two
things about each: that ``to_ir()`` returns at all, and that the result
contains no ``UnknownOp`` -- the fallback the builder emits when dispatch
falls through. A branch that crashes fails the first; a kind that is listed
as handled but has no dispatch arm fails the second.

``test_sample_covers_every_handled_operator_kind`` keeps the two sets in
lockstep, so adding a kind to the frozenset without a sample here is a
failure rather than an untested claim.
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
    """A kind added to the frozenset without a sample here would be a
    coverage claim nothing exercises; a sample for a kind no longer handled
    is dead weight. Equality catches both directions."""
    assert set(SAMPLES) == set(IRBuilder.HANDLED_OPERATOR_KINDS)


@pytest.mark.parametrize("kind,q", sorted(SAMPLES.items()))
def test_every_handled_operator_builds_without_unknown_op(kind, q):
    ir = parse(q).to_ir()                        # must not raise
    assert not list(find_all(ir, UnknownOp)), kind
