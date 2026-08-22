# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

from kustology import format_query, parse


def test_get_operator_chain():
    query = "SecurityEvent | where EventID == 4624 | count"
    result = parse(query)
    chain = result.get_operator_chain()
    assert [str(node.Kind) for node in chain] == ["FilterOperator", "CountOperator"]


def test_get_operator_chain_excludes_the_source_table():
    """The chain is operators, so the source table is not one of them.

    Element 0 used to be the ``NameReference`` naming the table, which made
    every caller either special-case it or count it as an operator — and
    ``__repr__`` did the latter, reporting a two-operator query as ``3 ops``.
    """
    chain = parse("T | where a | take 1").get_operator_chain()
    assert [str(node.Kind) for node in chain] == ["FilterOperator", "TakeOperator"]
    assert "NameReference" not in {str(node.Kind) for node in chain}
    assert "2 ops" in repr(parse("T | where a | take 1"))


def test_get_operator_chain_of_a_bare_table_is_empty():
    """A query with no operators has an empty chain rather than a one-element
    one holding its source."""
    assert parse("T").get_operator_chain() == []
    assert "0 ops" in repr(parse("T"))


def test_every_public_kustoquery_member_is_documented():
    """`KustoQuery` is the whole tier-1 API and half of it delegated silently.

    Six members carried no docstring at all — including `get_structural_hash`,
    whose module-level function documents at length what the hash is blind to,
    and `get_referenced_columns`, whose two modes disagree by design. A caller
    reading `help(KustoQuery)` saw a bare signature and had no way to know
    there was anything to read.
    """
    from kustology import KustoQuery

    members = [
        (name, attr)
        for name, attr in vars(KustoQuery).items()
        # Methods and properties only. A plain class attribute has no
        # docstring of its own, so `attr.__doc__` would read its *type's* —
        # `int.__doc__` is a paragraph — and the check would silently pass
        # for something genuinely undocumented.
        if not name.startswith("_") and (callable(attr) or isinstance(attr, property))
    ]
    assert len(members) >= 15, "the walk found almost nothing; the filter is wrong"

    undocumented = sorted(
        name for name, attr in members if not (attr.__doc__ or "").strip()
    )
    assert undocumented == []


def test_to_dict_basic():
    data = parse("T | count").to_dict()
    assert data["kind"] == "QueryBlock"
    assert len(data["children"]) > 0


def test_to_dict_pipe_expression_descends():
    """Non-trivial query: confirm Project / Filter operators appear in the dict."""
    data = parse("T | where x == 1 | project x, y").to_dict()
    flat = []

    def walk(d):
        flat.append(d["kind"])
        for c in d["children"]:
            walk(c)

    walk(data)
    assert "FilterOperator" in flat
    assert "ProjectOperator" in flat
    assert flat.count("PipeExpression") >= 1


def test_referenced_columns_returns_columns_not_table_names():
    query = "SecurityEvent | where EventID == 4624 | project TimeGenerated, Account"
    cols = parse(query).get_referenced_columns()
    assert {"EventID", "TimeGenerated", "Account"}.issubset(cols)
    assert "SecurityEvent" not in cols


def test_referenced_columns_excludes_function_names():
    """Function callees like strcat/ago/bin must not be reported as columns."""
    query = "T | extend a = strcat(x, y) | project a, ago(1h), bin(TimeGenerated, 5m)"
    cols = parse(query).get_referenced_columns()
    assert {"strcat", "ago", "bin"}.isdisjoint(cols)
    assert {"x", "y"}.issubset(cols)


def test_referenced_columns_excludes_dollar_join_refs():
    """`$left` and `$right` are KQL join-side references, not columns."""
    query = "A | join (B) on $left.x == $right.y"
    cols = parse(query).get_referenced_columns()
    assert {"$left", "$right"}.isdisjoint(cols)
    assert {"x", "y"}.issubset(cols)

    schema = {"A": {"x": "string"}, "B": {"y": "string"}}
    sem_cols = parse(query, schema=schema).get_referenced_columns()
    assert {"$left", "$right"}.isdisjoint(sem_cols)
    assert {"x", "y"}.issubset(sem_cols)


def test_referenced_columns_excludes_wildcard_patterns():
    """`project-away Foo*` names a pattern; the pattern itself is not a column."""
    cols = parse("T | project-away Foo* | project-keep * | where x == 1").get_referenced_columns()
    assert {"*", "Foo*"}.isdisjoint(cols)
    assert "x" in cols


def test_referenced_columns_excludes_a_bracketed_let_alias():
    """A bracketed `let` name must match its bracketed use site.

    The declaring side is a NameDeclaration and the use side a NameReference
    wrapping a BracketedName; while the two spelled the identifier
    differently (``['my-var']`` vs ``my-var``) the let filter missed the
    alias and reported the scalar as a column.
    """
    query = "let ['my-var'] = 5;\nT | project ['my-var'], Account"
    cols = parse(query).get_referenced_columns(force_syntactic=True)
    assert "my-var" not in cols
    assert "Account" in cols


def test_referenced_columns_keeps_a_column_that_shares_a_table_name():
    """A column is excluded by *position*, not by spelling.

    ``T2`` is the join's source table in one place and an ordinary column in
    another. Filtering the extracted names against the set of table names
    dropped both occurrences, so a query that filters on a column named after
    some other table reported that column as absent.
    """
    cols = parse("T | where T2 > 1 | join (T2) on a").get_referenced_columns(
        force_syntactic=True
    )
    assert {"T2", "a"}.issubset(cols)
    assert "T" not in cols


def test_referenced_columns_reports_only_the_root_of_a_dynamic_path():
    """``InitiatedBy.user.userPrincipalName`` is one column, not three.

    Only the root of the path is a column of the table; everything after a
    dot is a key inside a dynamic value, and no table has a column called
    ``userPrincipalName``. ``actor`` is a column the query creates.
    """
    query = "AuditLogs | extend actor = tostring(InitiatedBy.user.userPrincipalName)"
    cols = parse(query).get_referenced_columns(force_syntactic=True)
    assert cols == {"InitiatedBy", "actor"}


def test_referenced_columns_syntactic_reports_a_column_the_query_creates():
    """``extend a = …`` creates a column even where nothing reads it back.

    The alias is a ``NameDeclaration``, not a ``NameReference``, so a walk
    that only collected references saw ``x`` and ``y`` and missed ``a``
    entirely. Semantic mode is the mirror image here — the binder attaches a
    ``ColumnSymbol`` to references, and there is no reference — so the two
    modes are asserted separately rather than for equality.
    """
    query = "T | extend a = x + y"
    assert parse(query).get_referenced_columns(force_syntactic=True) == {"x", "y", "a"}
    schema = {"T": {"x": "long", "y": "long"}}
    assert parse(query, schema=schema).get_referenced_columns() == {"x", "y"}


def test_referenced_columns_excludes_named_parameter_names():
    """`kind` in `kind=inner` and `S` in `withsource=S` are not columns.

    All three are ``NameDeclaration`` nodes, the same node kind an ``extend``
    alias uses — so collecting declarations wholesale would report them.
    """
    cols = parse(
        "union isfuzzy=true withsource=S kind=inner A, B | where x == 1"
    ).get_referenced_columns(force_syntactic=True)
    assert cols == {"x"}


def test_referenced_columns_excludes_an_as_alias():
    """`| as X` binds a name for the rest of the query; it is not a column."""
    cols = parse("T | as X | join (X) on a").get_referenced_columns(
        force_syntactic=True
    )
    assert "X" not in cols
    assert "a" in cols


def test_referenced_columns_semantic_resolves_aliases():
    """Semantic mode includes both columns and extend-aliases as ColumnSymbols."""
    schema = {"T": {"x": "long", "y": "long"}}
    cols = parse("T | extend a = x + y | project a, x", schema=schema).get_referenced_columns()
    assert {"x", "y", "a"}.issubset(cols)


def test_structural_hash_ignores_literal_values():
    h1 = parse("T | where x == 1").get_structural_hash()
    h2 = parse("T | where x == 5").get_structural_hash()
    h3 = parse("T | project x").get_structural_hash()
    assert h1 == h2
    assert h1 != h3


def test_structural_hash_distinguishes_join_kind():
    """`kind=inner` and `kind=leftanti` are different queries, not one shape.

    The parameter value is a ``TokenLiteralExpression``, whose *kind* string
    contains "Token" — so a walker that skips every kind containing "Token"
    threw the value away and hashed an inner join identically to an anti-join.
    """
    inner = parse("T | join kind=inner (U) on a").get_structural_hash()
    leftanti = parse("T | join kind=leftanti (U) on a").get_structural_hash()
    assert inner != leftanti


def test_structural_hash_distinguishes_union_kind():
    inner = parse("union kind=inner A, B").get_structural_hash()
    outer = parse("union kind=outer A, B").get_structural_hash()
    assert inner != outer


def test_structural_hash_distinguishes_evaluate_plugin():
    """`bag_unpack` expands a dynamic column; `pivot` reshapes the whole table.

    The plug-in name is an ordinary ``NameReference`` — not a
    ``TokenLiteralExpression`` — so retaining named-parameter values is not
    enough on its own to tell these two apart.
    """
    bag_unpack = parse("T | evaluate bag_unpack(d)").get_structural_hash()
    pivot = parse("T | evaluate pivot(d)").get_structural_hash()
    assert bag_unpack != pivot


def test_structural_hash_still_ignores_whitespace_and_literals():
    """The boundary the fix must not cross: same shape, different text."""
    base = parse("T | join kind=inner (U) on a | where x == 1").get_structural_hash()
    respaced = parse(
        "T\n| join   kind=inner (U) on a\n| where x == 5"
    ).get_structural_hash()
    assert base == respaced


def test_structural_hash_still_ignores_identifiers():
    """Table, column and ordinary function names stay outside the hash."""
    assert (
        parse("Alpha | where beta == 1").get_structural_hash()
        == parse("Gamma | where delta == 1").get_structural_hash()
    )
    assert (
        parse("T | extend a = tolower(x)").get_structural_hash()
        == parse("T | extend a = toupper(x)").get_structural_hash()
    )


def test_find_time_expressions_returns_tuples_in_source_order():
    query = "T | where TimeGenerated > ago(1h) | extend n = now()"
    times = parse(query).find_time_expressions()
    assert [t[0] for t in times] == ["ago(1h)", "now()"]
    assert times == sorted(times, key=lambda t: t[1])


def test_find_time_expressions_finds_bin():
    """`bin` is how nearly every real Sentinel query buckets time.

    Its first signature declares no return type, so reading signature zero
    alone put it in ``scalar_functions()`` and this discovery aid skipped
    the single most common temporal construct in the corpus.
    """
    times = parse("T | summarize count() by bin(TimeGenerated, 1h)").find_time_expressions()
    assert [t[0] for t in times] == ["bin(TimeGenerated, 1h)"]


def test_find_time_expressions_finds_bin_at():
    times = parse(
        "T | summarize count() by bin_at(TimeGenerated, 1d, datetime(2024-01-01))"
    ).find_time_expressions()
    assert [t[0] for t in times] == [
        "bin_at(TimeGenerated, 1d, datetime(2024-01-01))"
    ]


def test_find_time_expressions_ignores_arithmetic_abs():
    """``abs`` is in ``time_functions()`` but is never a temporal expression.

    Reflection classifies by return type and ``abs`` has a timespan overload,
    which is the right answer to "what can this return" and the wrong answer
    to "is this call about time". ``abs(x)`` on a number must not surface in
    a discovery list a rule author reads to find the query's time handling.
    """
    assert parse("T | extend a = abs(x)").find_time_expressions() == []
    # The timespan usage is still not reported: the exclusion is by name, and
    # `abs(1h)` contributes its own bare `1h` literal instead.
    assert [t[0] for t in parse("T | where abs(d) > 1h").find_time_expressions()] == ["1h"]


def test_find_time_expressions_keeps_floor():
    """``floor(TimeGenerated, 1h)`` buckets time exactly as ``bin`` does."""
    times = parse("T | summarize count() by floor(TimeGenerated, 1h)").find_time_expressions()
    assert [t[0] for t in times] == ["floor(TimeGenerated, 1h)"]


def test_find_time_expressions_reports_only_the_outer_of_two_nested_calls():
    """``startofday(now())`` is one time expression, not two.

    The inner ``now()`` is an argument of the outer call, so reporting both
    gave a reader two overlapping spans for one construct — the same
    double-count the literal pass already avoids for ``ago(1h)``, which this
    pass did not apply to itself.
    """
    times = parse("T | where Time > startofday(now())").find_time_expressions()
    assert [t[0] for t in times] == ["startofday(now())"]
    # The span is the outer call's, so a caller slicing the source with it
    # gets the whole expression back.
    text, start, length = times[0]
    assert "T | where Time > startofday(now())"[start:start + length] == text


def test_find_time_expressions_keeps_a_temporal_call_inside_a_non_temporal_one():
    """The containment rule is about *matched* calls only. ``tostring`` is not
    a time function, so it opens no range and the ``now()`` inside it is still
    the query's time expression."""
    times = parse("T | extend s = tostring(now())").find_time_expressions()
    assert [t[0] for t in times] == ["now()"]


def test_find_time_expressions_still_reports_sibling_calls():
    """Control: suppression is containment, not "one per query"."""
    times = parse(
        "T | where t between (ago(2h) .. now())"
    ).find_time_expressions()
    assert [t[0] for t in times] == ["ago(2h)", "now()"]


def test_find_time_expressions_ignores_string_literal_text():
    """Substring 'ago(' embedded in a string literal must not match."""
    query = "T | where Note == 'this query uses ago()' | count"
    assert parse(query).find_time_expressions() == []


def test_find_time_expressions_does_not_double_count_nested_literals():
    """ago(1h) reports the call once; the inner 1h timespan is suppressed."""
    times = parse("T | where t > ago(1h)").find_time_expressions()
    texts = [text for text, _, _ in times]
    assert texts == ["ago(1h)"]


def test_get_time_range_is_a_deprecated_alias():
    import warnings

    query = "T | where TimeGenerated > ago(1h)"
    result = parse(query)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        legacy = result.get_time_range()
    assert legacy == result.find_time_expressions()
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
    assert "find_time_expressions" in str(caught[0].message)


def test_module_level_get_time_range_is_a_deprecated_alias():
    import warnings

    from kustology.utils.analysis import find_time_expressions, get_time_range

    code = parse("T | where TimeGenerated > ago(1h)")._code
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        legacy = get_time_range(code)
    assert legacy == find_time_expressions(code)
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)


def test_format_query_round_trip():
    queries = [
        "T | count",
        "SecurityEvent|where EventID==4624|project Account",
        "let x = 5; T | where y == x",
        "T | join kind=inner (U) on Id",
        "union A, B | summarize count() by Type",
    ]
    for q in queries:
        once = format_query(q)
        twice = format_query(once)
        assert once == twice, f"format_query is not idempotent for: {q!r}"


def test_format_query_accepts_options():
    """The optional FormattingOptions argument is part of the public surface."""
    from Kusto.Language.Editor import PlacementStyle

    from kustology.bridge import FormattingOptions

    smart = FormattingOptions.Default.WithPipeOperatorStyle(PlacementStyle.Smart)
    newline = FormattingOptions.Default.WithPipeOperatorStyle(PlacementStyle.NewLine)
    smart_out = format_query("T|count", options=smart)
    newline_out = format_query("T|count", options=newline)
    assert smart_out == "T | count"
    assert newline_out == "T\n| count"


def test_repr_reports_op_count_and_binding():
    syntactic = parse("T | where x == 1 | count")
    rep = repr(syntactic)
    assert "ops" in rep
    assert "has_semantics=False" in rep

    bound = parse("T | count", schema={"T": {"x": "long"}})
    assert "has_semantics=True" in repr(bound)


def test_referenced_functions_returns_callee_names():
    query = "T | extend a = strcat(x, y) | where ago(1h) > TimeGenerated"
    funcs = parse(query).get_referenced_functions()
    assert {"strcat", "ago"}.issubset(funcs)


def test_referenced_functions_excludes_columns_and_tables():
    query = "SecurityEvent | where EventID == 4624 | extend a = tolower(Account)"
    funcs = parse(query).get_referenced_functions()
    assert "tolower" in funcs
    assert "SecurityEvent" not in funcs
    assert "EventID" not in funcs
    assert "Account" not in funcs


def test_referenced_functions_handles_nested_calls():
    query = "T | extend out = tolower(strcat(x, tostring(y)))"
    funcs = parse(query).get_referenced_functions()
    assert {"tolower", "strcat", "tostring"}.issubset(funcs)


def test_referenced_functions_dedupes_repeated_callers():
    query = "T | extend a = tolower(x), b = tolower(y) | where tolower(z) == 'foo'"
    funcs = parse(query).get_referenced_functions()
    assert "tolower" in funcs
    # Set semantics: one entry per name regardless of call count.
    assert len([f for f in funcs if f == "tolower"]) == 1


def test_referenced_functions_semantic_mode_matches_syntactic_for_builtins():
    """For built-in callee names, semantic and syntactic results should agree."""
    schema = {"T": {"x": "string", "y": "string"}}
    query = "T | extend a = strcat(x, y) | where tolower(x) == 'foo'"
    syntactic = parse(query).get_referenced_functions()
    semantic = parse(query, schema=schema).get_referenced_functions()
    assert {"strcat", "tolower"}.issubset(syntactic)
    assert {"strcat", "tolower"}.issubset(semantic)


def test_collect_nodes_helper_returns_matching_nodes():
    """The reusable walker helper drives the analyzers above."""
    from kustology.utils.analysis import collect_nodes

    code = parse("T | where x == 1 | project x").syntax
    filters = collect_nodes(code, lambda n: str(n.Kind) == "FilterOperator")
    projects = collect_nodes(code, lambda n: str(n.Kind) == "ProjectOperator")
    assert len(filters) == 1
    assert len(projects) == 1


def test_collect_nodes_visits_every_node_when_predicate_is_constant():
    from kustology.utils.analysis import collect_nodes

    code = parse("T | count").syntax
    everything = collect_nodes(code, lambda n: True)
    # Walker yields multiple kinds of nodes; just confirm it's non-trivial
    # and the operator nodes are in there.
    kinds = {str(n.Kind) for n in everything}
    assert "CountOperator" in kinds
    assert "PipeExpression" in kinds


# --- AST depth cap -------------------------------------------------------
#
# `MAX_AST_DEPTH` lives in `kustology.utils.walker` and bounds both the
# `node_to_dict` serializer and `KustoWalker.visit`. Before it existed the
# only cap was a CLI-local constant of 1000 that CPython's own 1000-frame
# recursion limit made unreachable, so `KustoQuery.to_dict()` on deeply
# nested input raised `RecursionError` out of the library.

_PAREN_BOMB = "T | where " + "(" * 1200 + "1" + ")" * 1200


def _walk_depths(node, depth=0):
    """Yield ``(depth, node)`` for every node of a ``node_to_dict`` tree."""
    yield depth, node
    for child in node["children"]:
        yield from _walk_depths(child, depth + 1)


def test_to_dict_truncates_instead_of_raising_recursion_error():
    """1200 nested parens nest the AST past 2400 levels. `to_dict()` used to
    raise `RecursionError`; it now returns a tree capped at `MAX_AST_DEPTH`
    whose deepest nodes carry `truncated: True` and no children."""
    import json as _json

    from kustology.utils.walker import MAX_AST_DEPTH

    data = parse(_PAREN_BOMB).to_dict()

    measured = list(_walk_depths(data))
    assert max(d for d, _ in measured) == MAX_AST_DEPTH
    truncated = [(d, n) for d, n in measured if n.get("truncated")]
    assert truncated, "expected a truncation marker at the cap"
    assert {d for d, _ in truncated} == {MAX_AST_DEPTH}
    assert all(n["children"] == [] and n["text"] for _, n in truncated)
    assert '"truncated": true' in _json.dumps(data, indent=2)


def test_to_dict_of_an_ordinary_query_carries_no_truncation_marker():
    """Control for the cap: a real query nests ~17 levels, so nothing is
    marked truncated and the key is absent everywhere. Without this, a
    `node_to_dict` that truncated at depth 0 would pass the test above."""
    data = parse(
        'StormEvents | where EventType == "Tornado" | summarize C=count() by State'
    ).to_dict()

    def flatten(d):
        yield d
        for c in d["children"]:
            yield from flatten(c)

    nodes = list(flatten(data))
    assert len(nodes) > 20
    assert all("truncated" not in n for n in nodes)


def test_kusto_walker_stops_descending_at_the_depth_cap():
    """`KustoWalker.visit` recurses too, so `collect_nodes` and every
    analyzer built on it hit the same wall. The cap makes the walk finish;
    the depth it reaches pins that it stopped at `MAX_AST_DEPTH` rather than
    bailing out early or running away."""
    from kustology.utils.walker import MAX_AST_DEPTH, KustoWalker

    class DepthProbe(KustoWalker):
        def __init__(self):
            self.deepest = 0
            self.visited = 0

        def pre_visit(self, node):
            self.visited += 1

        def visit(self, node, depth=0):
            self.deepest = max(self.deepest, depth)
            super().visit(node, depth)

    probe = DepthProbe()
    probe.visit(parse(_PAREN_BOMB).syntax)
    assert probe.deepest == MAX_AST_DEPTH
    assert probe.visited > 300


def test_kusto_walker_still_reaches_the_leaves_of_an_ordinary_query():
    """Control: the cap must not shorten a normal walk. `collect_nodes` on a
    shallow query still finds the operator that lives at its deepest point."""
    from kustology.utils.analysis import collect_nodes

    code = parse("T | where x == 1 | project x, y | take 5").syntax
    kinds = {str(n.Kind) for n in collect_nodes(code, lambda n: True)}
    assert {"FilterOperator", "ProjectOperator", "TakeOperator"} <= kinds
