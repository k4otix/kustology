# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Oracle: our ``Pipeline.result_schema`` must equal Microsoft's ``ResultType``.

The binder in ``Kusto.Language`` already computes, exactly, the columns and
types a query returns. ``SchemaAttacher`` re-derives the same answer from
hand-written per-operator rules, and a pre-release audit found a dozen places
where the two disagreed (K07–K14, K28) — join-collision renaming, wildcard
``project-keep``, ``mv-expand``'s element type, ``arg_max(t, *)``, and so on.
Every one of those was a rule we wrote guessing at something Microsoft had
already answered. Since the dict-path reroute (``to_ir(attach_schema=dict)``
now re-binds through ``build_global_state`` + ``KustoCode.Analyze`` rather
than only decorating the IR through the attacher), Microsoft's own answer
reaches every closed symbol on *both* entry points, and the hand rules are
shadowed there — they still answer, but their answer is overlaid by
Microsoft's own before it reaches ``result_schema``, and they run un-overlaid
only where Microsoft itself leaves a symbol open.

This module is the disagreement detector. For each query it asks Microsoft
for ``code.ResultType.Columns`` and asks us for
``ir.main_pipeline.result_schema.columns``, and requires them to be equal
**as ordered lists** — KQL's column order is part of a query's result, and a
plain dict comparison would let a reordering through.

**Two legs, and both now compare Microsoft's capture against Microsoft's own
direct answer.** The bound leg parses with a schema up front
(``parse(q, schema=...).to_ir()``); ``ResultType`` is *taken* wherever
Microsoft could compute one, so most of this matrix compares Microsoft's
answer with itself and can only fail where a symbol is open. Its MATRIX run
is therefore a fixed representative sample (``BOUND_LEG_IDS``), not the full
matrix — it shares the parse-time plumbing (column identity, join-collision
renaming, wildcard selection, multi-output aggregates) that every other test
in this suite already exercises, so a hundred near-identical cases would add
nothing here. The **dict leg** — ``parse(q).to_ir(attach_schema=schema)`` —
is the public ``attach_schema=dict`` entry point itself: a caller with a
schema and no cluster to bind against. It re-binds through the same
``build_global_state`` + ``Analyze`` seam and is therefore byte-identical, IR
shape included, to the bound leg wherever ``schema`` is non-empty — but it is
that public seam being proven end-to-end, so it keeps the full MATRIX rather
than sampling. Both legs run the same 49 corpus fixtures, each deriving its
own per-query schema from the query text (see ``_heuristic_schema``).

One boundary the two legs do *not* share: an **empty** schema dict binds
differently on each entry point. ``parse(q, schema={})`` still binds — an
explicit, real, empty database — so the bound leg gets a real answer even
for a fixture with no syntactically-recognizable table. ``to_ir(attach_schema
={})`` is documented to treat ``{}`` as a no-op, the same as
``attach_schema=False``: no re-bind at all. The dict leg's corpus test skips
those fixtures rather than comparing, since nothing about the reroute is
being exercised when it never ran.

Each leg carries its own ``strict=True`` xfail list — ``XFAIL_5_3`` for the
bound leg, ``XFAIL_FALLBACK`` for the dict leg — and the two mostly name the
same corpus fixtures, since an open symbol is (mostly) a property of the
query and schema rather than of which entry point reached them. The
``{}``-boundary case above is the one entry that differs between the lists
for exactly the reason just given. ``strict=True`` means a case someone
fixes fails here rather than sitting as a silent xpass, so the entry has to
be deleted with the fix.

Both legs compare full ``(name, type)`` pairs now: there is no more
names-only/``unknown``-leniency to grant a leg that never reached Microsoft's
answer, because both of them do.
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
    # A multi-output aggregate *alongside* another one. Every other entry
    # here has its multi-output aggregate alone, which is what let a
    # bind-dependent alignment guard through the bind-state sweep below:
    # `arg_max(t, *)` reports six columns bound and one unbound, so a
    # `summarize` holding it plus anything else lines up in exactly one bind
    # state and the *other* aggregate is named two different ways.
    (
        "arg-max-star-beside-another-aggregate",
        "T | summarize arg_max(t, *), buildschema(d)",
    ),
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

# The bound leg's MATRIX run: one representative id per construct family
# (join, union-conflict, mv-expand `to typeof`, wildcard project-keep, typed
# parse, a multi-output summarize aggregate, datatable, search, evaluate,
# getschema) rather than the whole matrix -- see the module docstring for why
# most of the matrix cannot fail there. None of these ids appears in
# ``XFAIL_5_3``, so filtering to this set changes no case's outcome.
BOUND_LEG_IDS: set[str] = {
    "join-inner",
    "union-conflict",
    "mv-expand-to-typeof",
    "project-keep-wildcard",
    "parse-typed",
    "arg-max-star",
    "datatable",
    "search",
    "evaluate-bag-unpack",
    "getschema",
}

# Guard against BOUND_LEG_IDS drifting from MATRIX -- e.g. a rename inside
# the generated join-kind family -- which would otherwise silently shrink
# the bound leg's coverage below its ten categories with no test failure.
_unknown_bound_ids = BOUND_LEG_IDS - {case_id for case_id, _ in MATRIX}
assert not _unknown_bound_ids, (
    f"BOUND_LEG_IDS names ids not in MATRIX: {sorted(_unknown_bound_ids)}"
)

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


# The same comparison, run against the *dict* path -- ``to_ir(attach_schema=
# schema)``, which since Task 1 re-binds through Microsoft's own binder. The
# hand-rolled rules only still answer where an operator's own per-node
# ``result_schema`` is ``None`` -- Microsoft left *that* symbol open, even on
# a query whose whole-query ``ResultType`` (what ``theirs`` reads) resolves a
# shape. Every surviving entry below is that: an open per-operator symbol,
# not a case the reroute failed to fix. Task 3 replaces each of these with an
# honest ``result_schema is None`` assertion once the hand rules are gone.
XFAIL_FALLBACK: dict[str, str] = {
    "ADFSRemoteHTTPNetworkConnection": (
        "Microsoft's own per-operator `result_schema` is open from the "
        "`evaluate` onward (five operators down of twenty-one), even though "
        "this test's whole-query `ResultType.Columns` still resolves a full "
        "29-column answer. With the operator open, `_walk_pipeline` falls "
        "back to the hand-rolled merge, which does not reproduce the "
        "join-collision suffixes `TechniqueId1`/`TechniqueName1`, keeps "
        "`Key`/`Value` that `parse`/`mv-expand` consumed, and orders the "
        "columns differently. Task 3: once the hand rules are "
        "gone, an open operator honestly reports `result_schema=None`."
    ),
    "AnomalyFoundInNetworkSessionTraffic": (
        "Microsoft's own per-operator `result_schema` is open on both "
        "operators (the `union` and the final `extend`), so the hand-rolled "
        "merge answers. It also surfaces a *builder* defect: `extend "
        "(anomalies, score, baseline) = series_decompose_anomalies(...)` "
        "lowers to one `Assignment` named after the whole `(anomalies, "
        "score, baseline) = ...` source text, so that bogus column lands in "
        "the merged scope alongside -- not instead of -- the three real "
        "ones (a later `mv-expand anomalies, score, baseline, ...` adds "
        "them back by name). `DvcAction`, which the whole-query `ResultType` "
        "carries, does not survive the merge. Task 3: the open-operator "
        "half becomes an honest `result_schema=None` assertion; the "
        "multi-output-assignment defect is the builder's, not the "
        "reroute's, and stays out of scope here."
    ),
    "Qualified_ClusterDatabaseTable": (
        "Microsoft's own per-operator `result_schema` is open on both "
        "operators (the `where` and the `join`) -- a `cluster(...)."
        "database(...).T` source the schema does not describe -- so the "
        "hand-rolled merge answers and invents the join-collision suffix "
        "`IndicatorValue1`, which the engine's own (partially-typed) answer "
        "does not have. Task 3: once open, this flips to an honest "
        "`result_schema=None` assertion."
    ),
}


def _case(case_id: str, query: str):
    """One parametrized case, xfailed when Task 5.3 still owns it."""
    reason = XFAIL_5_3.get(case_id)
    marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
    return pytest.param(case_id, query, id=case_id, marks=marks)


def _fallback_case(case_id: str, query: str):
    """One parametrized case for the dict-path leg."""
    reason = XFAIL_FALLBACK.get(case_id)
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

    Both the column list and the table list are **sorted**. They come from
    ``set``s, so their iteration order varies with ``PYTHONHASHSEED``, and
    that order reaches the assertion: it decides the column order of the
    ``GlobalState``, which decides Microsoft's output order, which this gate
    compares as an ordered list. Every case agrees on every seed today, so
    nothing is flaky now — but a gate whose expected value depends on the
    seed is one change away from passing on CI and failing on a laptop, and
    the ``xfail(strict=True)`` markers would turn that into an xpass failure
    with no visible cause.
    """
    q = parse(query)
    columns = {
        c: "string"
        for c in sorted(q.get_referenced_columns(force_syntactic=True))
    }
    return {
        t: dict(columns)
        for t in sorted(q.get_referenced_tables(force_syntactic=True))
    }


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


def our_columns(query: str, schema: dict, ir=None) -> list[tuple[str, str]] | None:
    """Our answer. Pass ``ir`` when the caller already built one."""
    if ir is None:
        ir = parse(query, schema=schema).to_ir()
    result_schema = ir.main_pipeline.result_schema
    return list(result_schema.columns.items()) if result_schema is not None else None


def assert_agrees(query: str, schema: dict, ir=None) -> None:
    theirs = microsoft_columns(query, schema)
    if theirs is None:
        # ``facet`` and ``fork`` return several tables, so there is no single
        # ``ResultType`` to compare against — not a divergence, an absence.
        pytest.skip("Microsoft reports no tabular ResultType for this query")
    ours = our_columns(query, schema, ir)
    assert ours == theirs, (
        f"result_schema disagrees with Microsoft's binder for {query!r}\n"
        f"  ours: {ours}\n"
        f"  ms:   {theirs}"
    )


@pytest.mark.parametrize(
    "query_id,query",
    [_case(cid, q) for cid, q in MATRIX if cid in BOUND_LEG_IDS],
)
def test_operator_matrix_matches_microsoft(query_id: str, query: str):
    assert_agrees(query, SCHEMA)


@pytest.mark.parametrize("name,query", [_case(n, q) for n, q in CORPUS])
def test_corpus_fixture_matches_microsoft(name: str, query: str):
    schema = _heuristic_schema(query)
    ir = parse(query, schema=schema).to_ir()
    # ``code.ResultType`` describes the *last* statement and ``main_pipeline``
    # the first, so the comparison is only meaningful for a single-statement
    # query. No fixture has a second one today; the guard is here so that
    # adding one produces this sentence rather than a mystifying column diff.
    assert not ir.additional_pipelines, (
        f"{name} has {len(ir.additional_pipelines)} extra tabular statements; "
        "compare the last pipeline, not main_pipeline"
    )
    assert_agrees(query, schema, ir)


# --- the same matrix, against the dict-path leg ------------------------------


def dict_path_columns(query: str, schema: dict, ir=None) -> list[tuple[str, str]] | None:
    """Our answer through the public ``attach_schema=dict`` entry point.

    ``parse(query)`` alone is syntactic; ``to_ir(attach_schema=schema)``
    re-binds that same tree through Microsoft's own binder
    (``build_global_state(schema)`` + ``KustoCode.Analyze``) before building
    the IR, so this is the caller-facing path a schema dict with no cluster
    to bind against actually takes -- and, since Task 1, it is byte-identical
    to ``parse(query, schema=schema).to_ir()``. Pass ``ir`` when the caller
    already built one.
    """
    if ir is None:
        ir = parse(query).to_ir(attach_schema=schema)
    result_schema = ir.main_pipeline.result_schema
    return list(result_schema.columns.items()) if result_schema is not None else None


def assert_dict_path_agrees(query: str, schema: dict, ir=None) -> None:
    """Ordered ``(name, type)`` pairs, exactly -- both sides are Microsoft now.

    The rerouted dict path takes its answer from ``ResultType`` the same as
    the bound leg does, so there is no more names-only/``unknown``-leniency
    to grant: a real divergence here is a defect in the reroute plumbing
    (per-operator capture, ordering, the ``Analyze`` seam), not a hand rule
    guessing wrong.
    """
    theirs = microsoft_columns(query, schema)
    if theirs is None:
        pytest.skip("Microsoft reports no tabular ResultType for this query")
    ours = dict_path_columns(query, schema, ir)
    assert ours == theirs, (
        f"the dict path disagrees with Microsoft's binder for {query!r}\n"
        f"  ours: {ours}\n"
        f"  ms:   {theirs}"
    )


@pytest.mark.parametrize(
    "query_id,query", [_fallback_case(cid, q) for cid, q in MATRIX]
)
def test_operator_matrix_matches_microsoft_via_dict_path(
    query_id: str, query: str,
):
    assert_dict_path_agrees(query, SCHEMA)


@pytest.mark.parametrize(
    "name,query", [_fallback_case(n, q) for n, q in CORPUS]
)
def test_corpus_fixture_matches_microsoft_via_dict_path(
    name: str, query: str,
):
    """Types included -- both sides are Microsoft's own answer here."""
    schema = _heuristic_schema(query)
    if not schema:
        # No table is referenced syntactically (a `let`-function corpus
        # fixture, or one whose only source is a call-style ASIM parser), so
        # the heuristic schema is `{}` -- and `to_ir` treats an *empty*
        # ``attach_schema`` dict as a no-op, the same as ``attach_schema=
        # False``: no re-bind at all (see the `to_ir` docstring). There is
        # nothing here for the dict path specifically to exercise: any
        # agreement or divergence with `microsoft_columns`, which always
        # binds against `build_global_state({})` directly, would be an
        # artifact of that guard rather than of the reroute plumbing.
        pytest.skip(
            "empty heuristic schema: attach_schema={} is a documented "
            "no-op, so the dict path never re-binds for this fixture"
        )
    ir = parse(query).to_ir(attach_schema=schema)
    if ir.additional_pipelines:
        pytest.skip(
            "multi-statement query: code.ResultType describes the last "
            "statement, main_pipeline the first"
        )
    assert_dict_path_agrees(query, schema, ir)


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
