# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Interpolating a caller-supplied table name into KQL without injection.

A server that builds KQL from a name its caller chose has two jobs: quote
the name so it can only be a name, and verify the result before running it.
Quoting is `[@'...']`, Kusto's verbatim bracketed identifier. Verifying is a
canary check: parse a query built from a name you trust, then require every
candidate's parse to match that shape.

Tier 1 only, so this runs on the base install.
"""

from _display import banner, kql, note, section, table, takeaway

from kustology import parse

TEMPLATE = "{} | getschema"

# Names a caller might supply. The third targets an unquoted interpolation
# with its own pipe and comment. The fourth targets the string literal
# quoting produces, with a raw newline. The fifth lands at the head of the
# template, where a leading dot makes the whole query a control command. The
# sixth, empty, quotes to a query with no diagnostics, no command shape, and
# the canary's own structural hash; only the referenced-table check catches
# it.
CANDIDATE_NAMES = (
    "StormEvents",
    "weird-name",
    "sensitive_data | project Secret //",
    "StormEvents\n.drop table Victim",
    ".drop table Victim",
    "",
)

# Bracket-quoted, each of these is only ever a table name. Bare, each is also
# valid KQL syntax with a meaning of its own.
KEYWORD_NAMES = ("Table1", "where", "let", "by", "print", "datatable", "union")


def quote_name(name: str) -> str:
    """Return `name` as a Kusto verbatim bracketed identifier."""
    return "[@'" + name.replace("'", "''") + "']"


def plain_quote(name: str) -> str:
    """Return `name` in the plain, non-verbatim bracketed form `['...']`."""
    return "['" + name.replace("'", "''") + "']"


def facts(query_text: str) -> dict:
    """Parse `query_text` and collect the facts that describe its shape.

    `check` compares the first five. The rest are here to read.
    """
    q = parse(query_text)
    return {
        "is_command": q.is_command,
        "command_kinds": q.command_kinds,
        "structural_hash": q.get_structural_hash(),
        "diagnostics": q.diagnostics,
        "referenced_tables": q.get_referenced_tables(),
        "comment_spans": q.comment_spans(),
        "skipped_token_spans": q.skipped_token_spans(),
    }


def summarize(result: dict) -> str:
    """Return `result`'s diagnostic count, or its referenced table name."""
    if result["diagnostics"]:
        return f"{len(result['diagnostics'])} diagnostic(s)"
    return f"table name {sorted(result['referenced_tables'])}"


def check(candidate: dict, canary: dict, name: str) -> tuple[bool, str]:
    """Compare `candidate`'s facts to `canary`'s and return (accepted, reason).

    `name` is the table name interpolated into `candidate`'s query. An
    accepted candidate's `referenced_tables` must equal `{name}`.
    """
    if candidate["diagnostics"]:
        return False, f"{len(candidate['diagnostics'])} diagnostic(s)"
    # Ahead of the hash test, so a rendering that became a command is named
    # as one. A command block also changes the hash, which reports less.
    if candidate["is_command"]:
        kinds = ", ".join(sorted(candidate["command_kinds"])) or "no command kind"
        return False, f"control command: {kinds}"
    if candidate["structural_hash"] != canary["structural_hash"]:
        return False, "different structural hash"
    if candidate["referenced_tables"] != {name}:
        return False, (
            f"references {sorted(candidate['referenced_tables'])}; expected "
            f"{name!r}"
        )
    return True, "matches the canary"


def main() -> None:
    banner(
        "Safe interpolation",
        "A caller-supplied table name goes into the same fixed query "
        "template two ways: quoted with `quote_name`, and left bare. "
        "Compare each rendering against a canary parsed from a trusted "
        "name to find which ones are safe to run.",
        "which candidate names pass the canary check quoted, and what the "
        "same names do to the query when left bare.",
    )

    section(
        "Quoting",
        "`quote_name(name)` wraps `name` in Kusto's verbatim bracketed "
        "identifier, `[@'...']`, doubling every embedded `'`.",
    )
    for name in ("StormEvents", "O'Brien's Table"):
        print(f"  {name!r:<20} -> {quote_name(name)}")

    round_trip_rows = []
    for name in ("it's", "back\\slash"):
        plain = facts(f"{plain_quote(name)} | count")
        verbatim = facts(f"{quote_name(name)} | count")
        round_trip_rows.append(
            [repr(name), plain_quote(name), summarize(plain), summarize(verbatim)]
        )
    table(["Name", "Plain ['...'] form", "Plain result", "Verbatim result"], round_trip_rows)
    note(
        "The plain ['...'] form is not enough. Doubling a quote inside it "
        "yields a different name than the one you started with, and a "
        "backslash fails to parse. Both are silent corruptions the "
        "round-trip check below catches."
    )

    section(
        "The canary",
        "Build the query once from a name you trust, StormEvents, and "
        "record the shape every candidate has to match.",
    )
    canary_query = TEMPLATE.format(quote_name("StormEvents"))
    kql(canary_query)
    canary = facts(canary_query)
    table(
        ["Fact", "Value"],
        [
            ["is_command", canary["is_command"]],
            ["command_kinds", sorted(canary["command_kinds"]) or "(none)"],
            ["structural_hash", canary["structural_hash"]],
            ["diagnostics", canary["diagnostics"] or "(none)"],
            ["referenced_tables", sorted(canary["referenced_tables"])],
            ["comment_spans", canary["comment_spans"] or "(none)"],
            ["skipped_token_spans", canary["skipped_token_spans"] or "(none)"],
        ],
    )

    section(
        "Candidate names",
        "Each name below renders twice into the same template, bare and "
        "quoted, and each rendering is checked against the canary above. No "
        "bare rendering can match, because the canary is the quoted form. "
        "Read the bare rows for what each name turned the query into.",
    )
    rows = []
    rejections = []
    for name in CANDIDATE_NAMES:
        for form, rendered in (
            ("bare", TEMPLATE.format(name)),
            ("quoted", TEMPLATE.format(quote_name(name))),
        ):
            candidate = facts(rendered)
            accepted, reason = check(candidate, canary, name)
            rows.append([repr(name), form, "accept" if accepted else "reject", reason])
            if not accepted:
                rejections.append((name, form, candidate))
    table(["Name", "Form", "Verdict", "Reason"], rows)

    print()
    for name, form, candidate in rejections:
        print(
            f"  {form:<6} {name!r}: referenced tables "
            f"{sorted(candidate['referenced_tables'])}, "
            f"{len(candidate['comment_spans'])} comment span(s), "
            f"{len(candidate['diagnostics'])} diagnostic(s)"
        )

    note(
        "Bare, `.drop table Victim` parses as a control command and reports "
        "no diagnostic, so a caller who controls the head of the template "
        "controls the whole query. Quoted, the same text is a table name. "
        "The newline case fails even quoted, because a KQL string literal "
        "cannot hold a raw newline, and the canary check catches it."
    )

    section(
        "Keywords as table names",
        "Try each of these bare against a bare canary and bracket-quoted "
        "against a quoted canary.",
    )
    bare_canary = facts(TEMPLATE.format("StormEvents"))
    quoted_canary = canary

    keyword_rows = []
    bare_changed = []
    quoted_changed = []
    for name in KEYWORD_NAMES:
        bare_hash = facts(TEMPLATE.format(name))["structural_hash"]
        quoted_hash = facts(TEMPLATE.format(quote_name(name)))["structural_hash"]
        bare_differs = bare_hash != bare_canary["structural_hash"]
        quoted_differs = quoted_hash != quoted_canary["structural_hash"]
        if bare_differs:
            bare_changed.append(name)
        if quoted_differs:
            quoted_changed.append(name)
        keyword_rows.append(
            [
                name,
                "changes" if bare_differs else "keeps",
                "changes" if quoted_differs else "keeps",
            ]
        )
    table(["Name", "Bare vs. bare canary", "Bracket-quoted vs. quoted canary"], keyword_rows)
    note(
        f"{len(bare_changed)} of {len(KEYWORD_NAMES)} names change the bare "
        f"canary's structural hash: {', '.join(bare_changed)}. "
        f"Bracket-quoted, {len(KEYWORD_NAMES) - len(quoted_changed)} of "
        f"{len(KEYWORD_NAMES)} keep the quoted canary's hash."
    )

    note(
        "This example carries no allowlist of read-only CommandKind "
        "values. CommandKind strings track the bundled Kusto.Language DLL, "
        "and kustology does not classify them. A server that must accept "
        "caller-issued commands writes its own policy against "
        "command_kinds. Quoting the caller's name into a fixed template is "
        "the safer shape: nothing the caller supplies can become a command "
        "at all."
    )

    takeaway(
        "Quote a caller-supplied name with `quote_name`, then check the "
        "result against a canary built from a trusted name before running "
        "it. `kustology validate` applies the same principle at the CLI's "
        "own boundary: it rejects input whose tail the parser skipped, "
        "under `KUSTOLOGY002`.",
        more="docs/tier1-syntax-tree.md",
    )


if __name__ == "__main__":
    main()
