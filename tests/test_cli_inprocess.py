# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""In-process tests for the `kustology` CLI, so `--cov` actually sees it.

`tests/test_cli.py` drives the CLI through `subprocess.run([sys.executable,
"-m", "kustology.cli", ...])`. That's the right test for "does the shipped
entry point actually work end to end," but coverage instrumentation lives in
the parent process — it never sees a line executed inside the child
subprocess, so `cli.py` goes unmeasured no matter how many subprocess
cases exist. These tests call `kustology.cli.main()` directly
with `capsys` capturing stdout/stderr, so coverage attributes to the right
file. `tests/test_cli.py` remains the end-to-end layer and keeps its own copy
of the cases where "through the real entry point" is the point — notably the
byte ceiling, which needs the interpreter's own `sys.stdin.buffer` rather
than the double below.

These tests pin the exit-code contract `cli.py`'s module docstring documents:
0 success, 1 the input had errors, 2 a usage error. Every case below states
which of the three it is exercising and why, because leaving the code to
whichever exception happens to escape decides it by accident rather than by
what went wrong.
"""
from __future__ import annotations

import io
import json
import sys

import pytest

import kustology
from kustology.cli import main
from kustology.utils.walker import MAX_AST_DEPTH


def _stdin(text: str) -> io.TextIOWrapper:
    """A stdin double that has a real ``.buffer``, like the interpreter's.

    `_read_capped` reads bytes off `sys.stdin.buffer` so that
    `KUSTOLOGY_MAX_INPUT_BYTES` counts bytes and not characters. A bare
    `io.StringIO` has no `.buffer` at all, so a test double built from one
    could only ever exercise a character count — see
    `test_input_ceiling_counts_bytes_not_characters`.
    """
    return io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")


def test_version_prints_runtime_version(capsys):
    rc = main(["version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert kustology.__version__ in out
    assert "kustology" in out


def test_format_from_stdin(monkeypatch, capsys):
    messy = (
        'StormEvents|where EventType=="Tornado"|summarize '
        "Total=sum(DeathsDirect) by State"
    )
    monkeypatch.setattr(sys, "stdin", _stdin(messy))
    rc = main(["format", "-"])
    out = capsys.readouterr().out
    assert rc == 0
    # Formatted output should split across multiple lines with pipe indentation.
    assert "\n" in out
    assert "| where" in out
    assert "| summarize" in out


def test_format_from_file(tmp_path, capsys):
    q = tmp_path / "q.kql"
    q.write_text('StormEvents|where EventType=="Tornado"', encoding="utf-8")
    rc = main(["format", str(q)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| where" in out


def test_format_default_input_is_stdin(monkeypatch, capsys):
    # No FILE argument — should default to stdin, exercising the
    # `args.file in (None, "-")` branch via argparse's own default ("-").
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents|take 5"))
    rc = main(["format"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| take" in out


def test_format_empty_input_is_not_an_error(monkeypatch, capsys):
    """Empty input has no diagnostics, so it is a success, not a failure —
    and it must never surface a Python traceback either way. This is the
    control for `test_format_refuses_input_it_could_not_parse`: the two
    differ only in whether `validate` found an Error, so a `format` that
    returned 1 unconditionally would fail here."""
    monkeypatch.setattr(sys, "stdin", _stdin(""))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.out == "\n"
    assert "Traceback" not in captured.err, captured.err


def test_format_refuses_input_it_could_not_parse(monkeypatch, capsys):
    """`T | where` is missing the filter's expression (KS006, Error). The
    formatter happily returns `'T | where '` for it, so without a guard
    `format` would print half-parsed output and exit 0 — a caller piping the
    result into a file writes a query the parser has already rejected.
    `format` must exit 1 and write *nothing at all* to stdout; the
    diagnostics go to stderr."""
    monkeypatch.setattr(sys, "stdin", _stdin("T | where"))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "KS006" in captured.err
    assert "Error" in captured.err
    assert "Traceback" not in captured.err


def test_format_appends_a_newline_the_formatter_did_not_emit(monkeypatch, capsys):
    """`format_query` on a short single-line query returns no trailing
    newline; the CLI must add exactly one so shell output looks normal."""
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["format", "-"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "StormEvents | take 5\n"


def test_format_does_not_double_a_newline_the_formatter_already_emitted(
    monkeypatch, capsys
):
    """Same query, but the *input* already ends with `\\n`. `format_query`
    then preserves that trailing newline in its own output, so the CLI's
    `if not body.endswith("\\n")` guard must NOT append a second one — the
    guard checks the raw input, not the formatter's output, so this pins
    that the two never drift apart into a doubled blank line."""
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5\n"))
    rc = main(["format", "-"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "StormEvents | take 5\n"


def test_format_missing_file_is_a_usage_error(capsys):
    """A path that does not exist is a *usage* error — the docstring lists
    "missing file" under exit 2. `open()` raises `FileNotFoundError`, which
    without a guard falls through to the bare `except Exception` and exits
    1, the code reserved for "we read your query and it had errors" —
    leaving a CI job branching on 1-vs-2 unable to tell a typo'd path from a
    broken query."""
    rc = main(["format", "/no/such/path/does-not-exist.kql"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "FileNotFoundError" in captured.err
    assert "Traceback" not in captured.err


class _BrokenPipeStdout(io.StringIO):
    """A stdout whose writes fail the way a closed downstream pipe does."""

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")


class _DeferredBrokenPipeStdout(io.StringIO):
    """Writes succeed into the buffer; the pipe only breaks on flush.

    This is the shape a real pipe has — `write` fills a buffer and the dead
    reader is not discovered until the buffer drains — which is why `main`
    flushes inside its own guard rather than leaving it to interpreter
    shutdown.
    """

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_broken_pipe_is_a_success_not_a_usage_error(monkeypatch, capsys, tmp_path):
    """`kustology parse --ast --json big.kql | head` is a *correct*
    invocation whose reader stopped reading. `BrokenPipeError` is an
    `OSError`, so a blanket `except OSError: return 2` in `main` would sweep
    stdout writes in with the input reads and report exit 2 — the code the
    module docstring reserves for bad flags, a missing file and a malformed
    `--schema`. The mapping instead lives at the two read sites, so a broken
    pipe stops the emit without changing the command's own code, which for a
    clean `parse` is 0.

    `test_format_missing_file_is_a_usage_error` is the control: the read
    site must still produce 2, so a broken-pipe guard that swallowed every
    exit-2 case would not pass silently here."""
    q = tmp_path / "q.kql"
    q.write_text("StormEvents | take 5", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", _BrokenPipeStdout())
    rc = main(["parse", "--ast", "--json", str(q)])
    err = capsys.readouterr().err
    assert rc == 0
    assert "BrokenPipeError" not in err
    assert "error:" not in err


def test_broken_pipe_keeps_the_validation_verdict(monkeypatch, capsys, tmp_path):
    """A broken pipe must not erase the answer. `T | where` fails validation
    (KS006), so `validate` owes exit 1 whether or not the reader stayed to
    read the diagnostics — otherwise `kustology validate q.kql | head` in CI
    reads as a pass on a query that fails. Returning 0 for every broken pipe
    would trade "a broken pipe is a usage error" for "a broken pipe erases
    the result"; `_cmd_validate` decides `rc` before it writes, and the
    guard only stops the emit."""
    q = tmp_path / "q.kql"
    q.write_text("T | where", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", _BrokenPipeStdout())
    rc = main(["validate", str(q)])
    capsys.readouterr()
    assert rc == 1


def test_broken_pipe_on_a_valid_query_still_succeeds(monkeypatch, capsys, tmp_path):
    """The other half of the pair, and the reason the one above cannot pass
    by hard-coding 1: the same command, the same dead pipe, a query with no
    Error diagnostics — exit 0."""
    q = tmp_path / "q.kql"
    q.write_text("StormEvents | take 5", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", _BrokenPipeStdout())
    rc = main(["validate", str(q)])
    capsys.readouterr()
    assert rc == 0


def test_broken_pipe_on_stderr_keeps_the_verdict_too(monkeypatch, capsys, tmp_path):
    """`kustology format bad.kql 2>&1 | head` puts the *diagnostics* in the
    pipe, so the same erasure is reachable through stderr: the report is what
    fills the buffer, and losing exit 1 there is no better than losing it on
    stdout. `_report_error_diagnostics` decides `bool(errors)` before it
    writes, so the hang-up truncates the report and `format` still returns 1."""
    q = tmp_path / "q.kql"
    q.write_text("T | where", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", _BrokenPipeStdout())
    rc = main(["format", str(q)])
    capsys.readouterr()
    assert rc == 1


def test_broken_pipe_discovered_on_flush_is_handled_too(
    monkeypatch, capsys, tmp_path
):
    """The deferred case: every `write` lands in the buffer and the failure
    only appears when it drains. That is the shape a real pipe has, and if
    nothing flushes inside a guard it surfaces at interpreter shutdown as an
    unhandleable `Exception ignored` traceback.

    The exit code alone cannot prove the flush happened — this command
    returns 0 either way. The discriminating assertion is that
    `sys.stdout` was *replaced*: only `_silence_broken_stdout`, reached from
    a caught `BrokenPipeError`, does that. Delete the flush from the guard
    and nothing raises, nothing is replaced, and this fails."""
    q = tmp_path / "q.kql"
    q.write_text("StormEvents | take 5", encoding="utf-8")
    stub = _DeferredBrokenPipeStdout()
    monkeypatch.setattr(sys, "stdout", stub)
    rc = main(["parse", "--ast", "--json", str(q)])
    replaced = sys.stdout is not stub
    err = capsys.readouterr().err
    assert rc == 0
    assert replaced, "the broken stream was never flushed, so never replaced"
    assert "BrokenPipeError" not in err


def test_text_stdin_without_a_buffer_reports_why(monkeypatch, capsys):
    """An embedder calling `main()` with `sys.stdin` set to a `StringIO` has
    no `.buffer` for the byte ceiling to read. Measuring the text instead
    would silently revert the ceiling to counting characters, and the decode
    would then die with a bare `AttributeError` naming nothing. Unreachable
    from the shipped entry point; reachable from a library caller."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "AttributeError" not in captured.err
    assert ".buffer" in captured.err
    assert "stdin" in captured.err
    assert "Traceback" not in captured.err


def test_validate_clean_query_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["validate"])
    capsys.readouterr()
    assert rc == 0


def test_validate_broken_query_exits_1(monkeypatch, capsys):
    # Missing RHS of the comparison — produces an Error-severity diagnostic.
    monkeypatch.setattr(
        sys, "stdin", _stdin("StormEvents | where EventType ==  | project State")
    )
    rc = main(["validate"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Error" in out


def test_validate_json_output_shape(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "stdin", _stdin("StormEvents | where EventType ==")
    )
    rc = main(["validate", "--json"])
    out = capsys.readouterr().out
    assert rc == 1
    diags = json.loads(out)
    assert isinstance(diags, list)
    assert len(diags) >= 1
    d = diags[0]
    for key in ("start", "length", "message", "severity"):
        assert key in d, f"missing key {key} in {d}"
    assert isinstance(d["severity"], str)


def test_validate_ignore_unknown_tables_flag(tmp_path, monkeypatch, capsys):
    """`--ignore-unknown-tables` flips exit code from 1 to 0 for KS204."""
    schema = tmp_path / "schema.json"
    schema.write_text(
        '{"StormEvents": {"State": "string", "DeathsDirect": "int"}}',
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "stdin", _stdin("NoSuchTable | take 1"))
    rc_strict = main(["validate", "--schema", str(schema)])
    capsys.readouterr()
    assert rc_strict == 1, "expected exit 1 (KS204)"

    monkeypatch.setattr(sys, "stdin", _stdin("NoSuchTable | take 1"))
    rc_lax = main(
        ["validate", "--schema", str(schema), "--ignore-unknown-tables"]
    )
    capsys.readouterr()
    assert rc_lax == 0, "expected exit 0 (KS204 suppressed)"


def test_validate_schema_file_too_large_is_a_clean_usage_error(
    tmp_path, monkeypatch, capsys
):
    """`--schema` reads through the same `_read_capped` ceiling as stdin —
    a distinct call site (`_load_schema`) from the query body's own read,
    which runs first in `_cmd_validate`. The stdin body is kept to a single
    byte, well under the cap, so it is specifically the schema file's read
    that overflows — otherwise this would silently retest the stdin-over-cap
    path instead of the schema path."""
    schema = tmp_path / "schema.json"
    schema.write_text('{"T": {"c": "string"}}', encoding="utf-8")
    monkeypatch.setenv("KUSTOLOGY_MAX_INPUT_BYTES", "5")
    monkeypatch.setattr(sys, "stdin", _stdin("T"))
    rc = main(["validate", "--schema", str(schema)])
    captured = capsys.readouterr()
    assert rc == 2
    assert str(schema) in captured.err
    assert "exceeded" in captured.err


def test_validate_schema_malformed_json_is_a_usage_error(tmp_path, monkeypatch, capsys):
    """A `--schema` file that isn't valid JSON is a flag the caller got
    wrong, not a query the caller got wrong: exit 2. Without a guard,
    `json.JSONDecodeError` reaches the bare `except Exception` and reports
    1, the code that says "your KQL has errors" — here the KQL is fine.

    `_cmd_validate` reads the query body before the schema, so stdin must
    hold valid input — otherwise a failure to reach `_load_schema` at all
    could masquerade as this one exiting 2 for the wrong reason.
    """
    schema = tmp_path / "schema.json"
    schema.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["validate", "--schema", str(schema)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "JSONDecodeError" in captured.err
    assert "Traceback" not in captured.err


def test_parse_schema_malformed_json_is_a_usage_error(tmp_path, monkeypatch, capsys):
    """`parse --schema` reaches `_load_schema` through its own call site, so
    the exit-2 mapping has to hold there too and not only in `validate`."""
    schema = tmp_path / "schema.json"
    schema.write_text("[1, 2,", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["parse", "--ir", "--schema", str(schema)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "JSONDecodeError" in captured.err
    assert captured.out == ""


def test_parse_ast_text_default(monkeypatch, capsys):
    """Default `parse` output is a text AST dump showing the table and operators."""
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["parse"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "TakeOperator" in out or "Take" in out
    assert "StormEvents" in out


def test_parse_ast_json_shape(monkeypatch, capsys):
    """`parse --ast --json` emits a recursive {kind, text, children} tree."""
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["parse", "--ast", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    tree = json.loads(out)
    for key in ("kind", "text", "children"):
        assert key in tree, f"missing key {key} in top-level node"
    assert isinstance(tree["children"], list)

    def collect_kinds(n):
        yield n["kind"]
        for c in n["children"]:
            yield from collect_kinds(c)

    kinds = set(collect_kinds(tree))
    assert any("TakeOperator" in k or "PipeExpression" in k for k in kinds), kinds


def test_parse_ast_json_is_the_librarys_own_tree(monkeypatch, capsys):
    """The CLI's JSON is `walker.node_to_dict` output, byte for byte — not a
    second implementation of it. A parallel implementation risks drifting in
    ways nobody notices: leaving each node's *leading trivia* in `text`
    would serialize `| where x == 1`'s `where` token as `' where'` and the
    pipe token as `'\\n|'`. Comparing against `KustoQuery.to_dict()` is what
    stops a copy from coming back."""
    query = 'StormEvents\n| where EventType == "Tornado" // c\n| take 5'
    monkeypatch.setattr(sys, "stdin", _stdin(query))
    rc = main(["parse", "--ast", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    tree = json.loads(out)
    assert tree == kustology.parse(query).to_dict()

    def texts(n):
        yield n["text"]
        for c in n["children"]:
            yield from texts(c)

    assert all(t == t.strip() for t in texts(tree))


def test_parse_refuses_input_it_could_not_parse(monkeypatch, capsys):
    """Same contract as `format`: an Error-severity diagnostic means exit 1.
    Without this guard, `parse` would dump the AST of the broken query and
    exit 0, so a script that only checks the status code would treat
    `T | where` as a good parse."""
    monkeypatch.setattr(sys, "stdin", _stdin("T | where"))
    rc = main(["parse"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "KS006" in captured.err


def test_parse_ast_with_schema_binds_without_changing_the_tree(
    tmp_path, monkeypatch, capsys
):
    """`--schema` is accepted on `parse` in both modes. Binding runs the
    semantic analyzer; it does not rewrite the syntax tree, so the `--ast`
    output must be identical with and without it. Pinned so a future change
    that folds binder results into the AST dump cannot land unnoticed."""
    schema = tmp_path / "schema.json"
    schema.write_text('{"StormEvents": {"State": "string"}}', encoding="utf-8")

    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | project State"))
    assert main(["parse", "--ast", "--json"]) == 0
    unbound = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | project State"))
    assert main(["parse", "--ast", "--json", "--schema", str(schema)]) == 0
    bound = json.loads(capsys.readouterr().out)

    assert bound == unbound
    assert bound["children"], "expected a non-empty tree, not two empty dicts"


def test_parse_ir_json_is_wrapped_in_a_versioned_envelope(monkeypatch, capsys):
    """Without the envelope, `--ir --json` would emit the bare `QueryIR`
    dump, leaving a consumer holding a stored payload no way to tell which
    IR shape produced it even though both version tags exist. The output is
    an envelope naming both, with the IR under `"ir"`."""
    pytest.importorskip("pydantic")
    from kustology.ir import IR_SCHEMA_VERSION, SEMANTIC_HASH_SCHEME

    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["parse", "--ir", "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    payload = json.loads(captured.out)
    assert set(payload) == {"ir_schema_version", "semantic_hash_scheme", "ir"}
    assert payload["ir_schema_version"] == IR_SCHEMA_VERSION == "0.2"
    assert payload["semantic_hash_scheme"] == SEMANTIC_HASH_SCHEME == "kustology-sem-v2"
    assert payload["ir"]["main_pipeline"]["operators"]
    assert payload["ir"]["semantic_hash"].startswith(SEMANTIC_HASH_SCHEME + ":")


def test_parse_ir_missing_extras_hint_is_a_usage_error(monkeypatch, capsys):
    """The `[ir]` extras are installed in this venv, so force the import to
    fail the way a base install does: exit 2 with the install command, and
    nothing on stdout. Without this the branch is unreachable here and the
    hint could rot."""
    # A ``None`` entry in ``sys.modules`` is the documented way to make an
    # import of that name raise ``ImportError``, which is exactly what a
    # base install (no pydantic) does at ``kustology.ir`` import time.
    monkeypatch.setitem(sys.modules, "kustology.ir", None)
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["parse", "--ir", "--json"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "[ir]" in captured.err
    assert captured.out == ""


def test_parse_ir_schema_flag_attaches_the_schema(tmp_path, monkeypatch, capsys):
    """`--schema` binds the parse, and `to_ir()` auto-attaches on a bound
    parse — so `schema_attached` flips to true and the projected column
    acquires its declared type. The no-schema run is the control: same
    query, `schema_attached` false and the type unresolved."""
    pytest.importorskip("pydantic")
    schema = tmp_path / "schema.json"
    schema.write_text(
        '{"StormEvents": {"State": "string", "DeathsDirect": "int"}}',
        encoding="utf-8",
    )
    query = "StormEvents | project State"

    monkeypatch.setattr(sys, "stdin", _stdin(query))
    assert main(["parse", "--ir", "--json"]) == 0
    plain = json.loads(capsys.readouterr().out)["ir"]
    assert plain["schema_attached"] is False

    monkeypatch.setattr(sys, "stdin", _stdin(query))
    rc = main(["parse", "--ir", "--json", "--schema", str(schema)])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    attached = json.loads(captured.out)["ir"]
    assert attached["schema_attached"] is True
    assert attached["main_pipeline"]["result_schema"] is not None
    assert plain["main_pipeline"]["result_schema"] is None


def test_parse_ast_json_truncates_a_paren_bomb_instead_of_recursing(
    monkeypatch, capsys
):
    """1200 nested parentheses nest the AST past 2400 levels — deeper than
    CPython's own 1000-frame limit. A depth cap set at 1000 would sit past
    that limit and go unreachable: the walk raises `RecursionError` first,
    and the CLI reports it as exit 1 with no output. `walker.MAX_AST_DEPTH`
    at 300 sits well inside the frame budget, so the emitter reaches it and
    writes a truncation marker."""
    query = "T | where " + "(" * 1200 + "1" + ")" * 1200
    monkeypatch.setattr(sys, "stdin", _stdin(query))
    rc = main(["parse", "--ast", "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "RecursionError" not in captured.err
    assert '"truncated": true' in captured.out

    tree = json.loads(captured.out)

    def depths(node, depth=0):
        yield depth, node
        for child in node["children"]:
            yield from depths(child, depth + 1)

    measured = list(depths(tree))
    assert max(d for d, _ in measured) == MAX_AST_DEPTH
    assert {d for d, n in measured if n.get("truncated")} == {MAX_AST_DEPTH}


def test_parse_ast_text_truncates_a_paren_bomb(monkeypatch, capsys):
    """The text renderer shares the capped dict, so it stops at the same
    depth rather than growing its own limit."""
    query = "T | where " + "(" * 1200 + "1" + ")" * 1200
    monkeypatch.setattr(sys, "stdin", _stdin(query))
    rc = main(["parse"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert f"truncated at depth {MAX_AST_DEPTH}" in captured.out


def test_parse_ir_text_default_format(monkeypatch, capsys):
    """`--ir` without `--json` emits a model_dump pprint that mentions the IR class."""
    try:
        import pydantic  # noqa: F401
    except ImportError:
        pytest.skip("requires [ir] extras")
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["parse", "--ir"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "QueryIR" in out or "main_pipeline" in out


def test_stdin_over_the_default_env_var_override_ceiling_is_a_usage_error(
    monkeypatch, capsys
):
    """`KUSTOLOGY_MAX_INPUT_BYTES` overrides the default 10 MiB ceiling.
    Set it well below a real query's length and confirm the CLI reports a
    usage error (exit 2) instead of truncating silently or crashing."""
    monkeypatch.setenv("KUSTOLOGY_MAX_INPUT_BYTES", "5")
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "5-byte input ceiling" in captured.err
    assert "KUSTOLOGY_MAX_INPUT_BYTES" in captured.err


def test_input_ceiling_counts_bytes_not_characters(monkeypatch, capsys):
    """`KUSTOLOGY_MAX_INPUT_BYTES` is named in bytes and must mean bytes.
    Reading `_read_capped`'s stream as *decoded text* would make
    `len(data)` count characters, so a payload of 20 characters would sail
    under a 22-byte ceiling while occupying 28 bytes on the wire — the
    ceiling exists to bound memory, and characters do not bound memory.

    The payload below is chosen so the two counts disagree across the cap:
    20 characters, 28 bytes, cap 22. A character count accepts it; a byte
    count rejects it. The second half raises the cap to 28 and shows the
    same payload going through, so this cannot pass by rejecting
    everything."""
    payload = 'T | where a== "日本語だ"'
    assert len(payload) == 20
    assert len(payload.encode("utf-8")) == 28

    monkeypatch.setenv("KUSTOLOGY_MAX_INPUT_BYTES", "22")
    monkeypatch.setattr(sys, "stdin", _stdin(payload))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "22-byte input ceiling" in captured.err
    assert captured.out == ""

    monkeypatch.setenv("KUSTOLOGY_MAX_INPUT_BYTES", "28")
    monkeypatch.setattr(sys, "stdin", _stdin(payload))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "日本語だ" in captured.out


def test_non_integer_env_var_override_falls_back_to_the_default_ceiling(
    monkeypatch, capsys
):
    """An unparseable `KUSTOLOGY_MAX_INPUT_BYTES` must not crash the CLI —
    `_max_input_bytes` catches `ValueError` and falls back to the 10 MiB
    default, so an ordinary query still succeeds."""
    monkeypatch.setenv("KUSTOLOGY_MAX_INPUT_BYTES", "not-a-number")
    monkeypatch.setattr(sys, "stdin", _stdin("StormEvents | take 5"))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Traceback" not in captured.err


def test_unknown_subcommand_is_an_argparse_usage_error(capsys):
    """`parser.parse_args(argv)` runs *before* `main`'s try block starts, so
    argparse's own SystemExit(2) for an invalid subcommand propagates
    straight out of `main` -- it never reaches `main`'s
    `except SystemExit: raise`, which exists to re-propagate a SystemExit
    raised from inside command handling instead."""
    with pytest.raises(SystemExit) as exc_info:
        main(["not-a-real-command"])
    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_no_subcommand_is_also_a_systemexit_usage_error(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 2
    capsys.readouterr()
