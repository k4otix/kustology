# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""``iter_elements`` unwraps SeparatedElement across both .NET list shapes."""

from kustology import iter_elements, parse
from kustology.utils.analysis import collect_nodes


def _op(query: str, kind: str):
    nodes = collect_nodes(parse(query).syntax, lambda n: str(n.Kind) == kind)
    assert nodes, f"no {kind} in {query!r}"
    return nodes[0]


def test_unwraps_separated_element_wrappers():
    """ProjectOperator.Expressions is SyntaxList[SeparatedElement[Expression]]."""
    project = _op("T | project A, B, C", "ProjectOperator")

    # The trap: the raw list yields wrappers, not expressions.
    assert str(project.Expressions[0].Kind) == "SeparatedElement"

    kinds = [str(e.Kind) for e in iter_elements(project.Expressions)]
    assert kinds == ["NameReference", "NameReference", "NameReference"]


def test_passes_through_unwrapped_lists():
    """SummarizeOperator.Parameters is a plain SyntaxList[NamedParameter]."""
    summarize = _op("T | summarize hint.shufflekey=A count() by A", "SummarizeOperator")

    assert not hasattr(summarize.Parameters[0], "Element")

    kinds = [str(p.Kind) for p in iter_elements(summarize.Parameters)]
    assert kinds == ["NamedParameter"]


def test_yields_nothing_for_an_empty_list():
    summarize = _op("T | summarize count() by A", "SummarizeOperator")
    assert list(iter_elements(summarize.Parameters)) == []


def test_unwrapped_elements_carry_the_expression_text():
    project = _op("T | project Alpha, Beta", "ProjectOperator")
    texts = [e.ToString().strip() for e in iter_elements(project.Expressions)]
    assert texts == ["Alpha", "Beta"]
