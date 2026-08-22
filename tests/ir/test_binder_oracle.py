# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Oracle: our ``Pipeline.result_schema`` must equal Microsoft's ``ResultType``.

The binder in ``Kusto.Language`` already computes, exactly, the columns and
types a query returns. ``SchemaAttacher`` re-derived the same answer from
hand-written per-operator rules, and a pre-release audit found a dozen places
where the two disagree (K07–K14, K28) — join-collision renaming, wildcard
``project-keep``, ``mv-expand``'s element type, ``arg_max(t, *)``, and so on.
Every one of those is a rule we wrote guessing at something Microsoft had
already answered.

This module is the disagreement detector. For each query it asks Microsoft
for ``code.ResultType.Columns`` and asks us for
``ir.main_pipeline.result_schema.columns``, and requires them to be equal
**as ordered lists** — KQL's column order is part of a query's result, and a
plain dict comparison would let a reordering through.

Cases that still fail are marked ``xfail`` naming Task 5.3, which removes the
marker as it fixes the fallback rule underneath. A case fails here only where
Microsoft declined to answer (an *open* table symbol, i.e. a schema it could
not fully determine) and the hand-rolled walk had to; where Microsoft
answered, the answer is taken verbatim and there is nothing left to diverge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kustology import parse
from kustology.bridge import KustoCode
from kustology.utils.analysis import build_global_state

# A deliberately collision-heavy schema: ``L`` and ``R`` share ``k`` and
# ``shared``, which is what makes join renaming (``shared1``) observable, and
# ``U`` types ``a`` differently from ``T`` so a union conflict has to produce
# two columns rather than one.
SCHEMA: dict[str, dict[str, str]] = {
    "L": {"k": "string", "a": "long", "shared": "string"},
    "R": {"k": "string", "b": "real", "shared": "string"},
    "T": {
        "k": "string", "a": "long", "t": "datetime",
        "d": "dynamic", "s": "string", "g": "guid",
    },
    "U": {"k": "string", "a": "string", "z": "long"},
}

# All twelve spellings the parser accepts — its KS005 message lists them. The
# three aliases matter to Task 5.3's fallback rule, which groups kinds by the
# side that survives rather than by name.
_JOIN_KINDS = (
    "innerunique", "inner", "leftouter", "rightouter", "fullouter",
    "leftanti", "rightanti", "leftsemi", "rightsemi",
    "anti", "leftantisemi", "rightantisemi",
)

# (id, query) — every entry is a *shape*, not a rule, so the id names the
# construct it exercises and Task 5.3 can read the failing ids as a work list.
MATRIX: list[tuple[str, str]] = [
    *(
        (f"join-{kind}", f"L | join kind={kind} (R) on k")
        for kind in _JOIN_KINDS
    ),
    ("join-bare", "L | join (R) on k"),
    ("join-on-equality", "L | join (R) on $left.k == $right.k"),
    ("lookup", "L | lookup (R) on k"),
    ("lookup-kind-leftouter", "L | lookup kind=leftouter (R) on k"),
    ("project-keep-wildcard", "T | project-keep k, a*"),
    ("project-keep-plain", "T | project-keep k, a"),
    ("project-away-wildcard", "T | project-away a*"),
    ("project-away-plain", "T | project-away a, s"),
    ("project-rename", "T | project-rename kk = k"),
    ("project-reorder", "T | project-reorder s, k"),
    ("project-reorder-desc", "T | project-reorder * desc"),
    ("mv-expand-plain", "T | mv-expand d"),
    ("mv-expand-to-typeof", "T | mv-expand d to typeof(long)"),
    ("mv-expand-with-itemindex", "T | mv-expand with_itemindex=i d"),
    ("mv-expand-bagexpansion", "T | mv-expand bagexpansion=array d"),
    ("mv-expand-two-columns", "T | mv-expand d, s"),
    ("mv-apply", "T | mv-apply d on (take 1)"),
    ("arg-max-star", "T | summarize arg_max(t, *)"),
    ("arg-min-star", "T | summarize arg_min(t, *)"),
    ("arg-max-columns", "T | summarize arg_max(t, a, s)"),
    ("make-set", "T | summarize make_set(s)"),
    ("make-list", "T | summarize make_list(s)"),
    ("take-any", "T | summarize take_any(a)"),
    ("take-any-star", "T | summarize take_any(*)"),
    ("percentile", "T | summarize percentile(a, 95)"),
    ("percentiles", "T | summarize percentiles(a, 5, 50, 95)"),
    ("summarize-by-bin", "T | summarize c = count() by bin(t, 1h)"),
    ("parse-typed", "T | parse s with 'x' n:long 'y' m:datetime"),
    ("parse-untyped", "T | parse s with 'x' n 'y' m"),
    ("parse-where-typed", "T | parse-where s with 'x' n:long"),
    ("parse-kv", "T | parse-kv s as (n:long, m:string)"),
    ("union-conflict", "T | union U"),
    ("union-withsource", "T | union withsource=src U"),
    ("union-kind-outer", "T | union kind=outer U"),
    ("union-kind-inner", "T | union kind=inner U"),
    ("print", "print x = 1, y = 'a'"),
    ("range", "range n from 1 to 10 step 1"),
    ("datatable", "datatable(a:long, b:string)[1, 'x']"),
    (
        "externaldata",
        "externaldata(a:long, b:string)[@'https://example.com/x.csv']",
    ),
    ("getschema", "T | getschema"),
    ("search", "search in (T) 'x'"),
    ("find", "find in (T, U) where k == 'x'"),
    ("scan", "T | scan declare (v:long = 0) with (step s1: true => v = 1;)"),
    ("serialize-row-number", "T | serialize | extend rn = row_number()"),
    ("serialize-with-column", "T | serialize rn = row_number()"),
    ("extend", "T | extend n = a + 1"),
    ("distinct", "T | distinct k, a"),
    ("count", "T | count"),
    ("count-as", "T | count as Hits"),
    ("top-by", "T | top 5 by a"),
    ("sample", "T | sample 5"),
    ("as-operator", "T | as X"),
    ("make-series", "T | make-series c = count() on t step 1h by k"),
    ("evaluate-bag-unpack", "T | evaluate bag_unpack(d)"),
    ("facet-by", "T | facet by k"),
    ("partition", "T | partition by k (top 1 by a)"),
    ("sort", "T | sort by a desc"),
    ("where", "T | where a > 1"),
    ("project-assignment", "T | project n = a + 1, k"),
    ("toscalar-in-extend", "T | extend m = toscalar(U | count)"),
    ("let-then-pipeline", "let B = T | where a > 1; B | project k, a"),
]

CORPUS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "complex_queries"

# Cases the fallback walk still gets wrong, and what each one reveals. Every
# one of them is a query where Microsoft's own ``ResultType`` is *open* — it
# declined to determine the full schema — so ``Operator.result_schema`` is
# ``None`` throughout and the hand-rolled rules answer alone. They diverge
# from even the partial list Microsoft did give.
#
# **Task 5.3 owns this dict.** Fix the rule, delete the entry; `strict=True`
# turns a fixed case into a failure here rather than letting it sit as a
# silent xpass.
XFAIL_5_3: dict[str, str] = {
    "ADFSRemoteHTTPNetworkConnection": (
        "Task 5.3: an `evaluate` mid-pipeline opens every symbol after it, "
        "and three fallback rules then diverge at once — the walk keeps "
        "`Key`/`Value` that `parse`/`mv-expand` consumed, misses the "
        "join-collision suffixes `TechniqueId1`/`TechniqueName1` (K07), and "
        "emits the columns in a different order from the engine."
    ),
    "AnomalyFoundInNetworkSessionTraffic": (
        "Task 5.3: `extend (a, b, c) = series_decompose_anomalies(...)` is a "
        "multi-output assignment. The builder lowers it to a single "
        "Assignment named after the whole `(a, b, c) = ...` source text, so "
        "the scope grows one bogus column instead of three real ones; the "
        "`union` above it also drops `DvcAction`."
    ),
    "Brute_Force_Attack_against_GitHub_Account": (
        "Task 5.3: `let f = (t:string){ … }; union f('A'), f('B')` — a "
        "`let`-declared *function* is recorded and not expanded, so both "
        "union arms contribute no columns and result_schema comes out "
        "empty. Microsoft expands the body and reports 17 columns."
    ),
    "Qualified_ClusterDatabaseTable": (
        "Task 5.3: a `cluster(...).database(...).T` source the schema does "
        "not describe leaves Microsoft open, and the fallback's join "
        "renaming then invents `IndicatorValue1` — the K07 collision rule "
        "again, this time on the left-hand `project`ed right side."
    ),
}


def _case(case_id: str, query: str):
    """One parametrized case, xfailed when Task 5.3 still owns it."""
    reason = XFAIL_5_3.get(case_id)
    marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
    return pytest.param(case_id, query, id=case_id, marks=marks)


def _heuristic_schema(query: str) -> dict[str, dict[str, str]]:
    """Give every table the query names every column it references, as string.

    A stand-in for the real Log Analytics schemas, which are not in the repo.
    It is wrong about types and generous about columns, and that is fine for
    this gate: both sides of the comparison see the *same* ``GlobalState``,
    so any disagreement is ours. What it buys over an empty schema is a
    *closed* table symbol — Microsoft only computes a result schema it can
    fully determine, and an unknown table leaves every downstream symbol
    open, so an empty schema would make the corpus sweep assert nothing.
    """
    q = parse(query)
    columns = {c: "string" for c in q.get_referenced_columns(force_syntactic=True)}
    return {t: dict(columns) for t in q.get_referenced_tables(force_syntactic=True)}


def _load_corpus() -> list[tuple[str, str]]:
    if not CORPUS_DIR.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(CORPUS_DIR.glob("*.kql")):
        text = path.read_text().strip()
        if text:
            out.append((path.stem, text))
    return out


CORPUS = _load_corpus()


def microsoft_columns(query: str, schema: dict) -> list[tuple[str, str]] | None:
    """``[(name, type), …]`` for the whole query, straight from the binder."""
    code = KustoCode.ParseAndAnalyze(query, build_global_state(schema))
    result_type = getattr(code, "ResultType", None)
    columns = getattr(result_type, "Columns", None) if result_type is not None else None
    if columns is None:
        return None
    return [
        (str(columns[i].Name), str(columns[i].Type.Name))
        for i in range(columns.Count)
    ]


def our_columns(query: str, schema: dict) -> list[tuple[str, str]] | None:
    ir = parse(query, schema=schema).to_ir()
    result_schema = ir.main_pipeline.result_schema
    return list(result_schema.columns.items()) if result_schema is not None else None


def assert_agrees(query: str, schema: dict) -> None:
    theirs = microsoft_columns(query, schema)
    if theirs is None:
        # ``facet`` and ``fork`` return several tables, so there is no single
        # ``ResultType`` to compare against — not a divergence, an absence.
        pytest.skip("Microsoft reports no tabular ResultType for this query")
    ours = our_columns(query, schema)
    assert ours == theirs, (
        f"result_schema disagrees with Microsoft's binder for {query!r}\n"
        f"  ours: {ours}\n"
        f"  ms:   {theirs}"
    )


@pytest.mark.parametrize(
    "query_id,query", [_case(cid, q) for cid, q in MATRIX]
)
def test_operator_matrix_matches_microsoft(query_id: str, query: str):
    assert_agrees(query, SCHEMA)


@pytest.mark.parametrize("name,query", [_case(n, q) for n, q in CORPUS])
def test_corpus_fixture_matches_microsoft(name: str, query: str):
    schema = _heuristic_schema(query)
    ir = parse(query, schema=schema).to_ir()
    if ir.additional_pipelines:
        pytest.skip(
            "multi-statement query: code.ResultType describes the last "
            "statement, main_pipeline the first"
        )
    assert_agrees(query, schema)


def test_auto_names_do_not_depend_on_the_bind_state():
    """``Assignment.name`` must be the same bound and unbound.

    The builder prefers Microsoft's own ``ResultType`` column name for an
    unnamed aggregate, and that list is *shorter* when the binder cannot
    determine the schema: bound, ``arg_max(t, *)`` reports six columns;
    against a table nobody described, one. A per-index read would therefore
    give the same query two different names -- and ``Assignment.name`` is
    hashed, so it would give it two different ``semantic_hash`` values.

    The guard in the builder is a length check plus a skip-list of the
    multi-output aggregates. This asserts the guard holds over the whole
    matrix and corpus rather than over the two cases that motivated it.
    """
    from kustology.ir import compute_semantic_hash

    divergent = []
    for case_id, query in MATRIX:
        bound = compute_semantic_hash(parse(query, schema=SCHEMA).to_ir())
        unbound = compute_semantic_hash(parse(query).to_ir())
        if bound != unbound:
            divergent.append(case_id)
    for name, query in CORPUS:
        schema = _heuristic_schema(query)
        bound = compute_semantic_hash(parse(query, schema=schema).to_ir())
        unbound = compute_semantic_hash(parse(query).to_ir())
        if bound != unbound:
            divergent.append(name)
    assert divergent == []
