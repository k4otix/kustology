# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""In-process tests for the `kustology` CLI, so `--cov` actually sees it.

`tests/test_cli.py` drives the CLI through `subprocess.run([sys.executable,
"-m", "kustology.cli", ...])`. That's the right test for "does the shipped
entry point actually work end to end," but coverage instrumentation lives in
the parent process — it never sees a line executed inside the child
subprocess, so `cli.py`'s 294 lines were effectively unmeasured no matter how
many subprocess cases existed. `tests/test_cli.py` stays as-is; these tests
call `kustology.cli.main()` directly with `capsys` capturing stdout/stderr,
so coverage attributes to the right file.

These tests target the CLI's behaviour *as it exists today*. A later task
(3.6 in the pre-release remediation plan) changes several exit codes, adds
`parse --schema`, and wraps `parse --ir --json` output in a versioned
envelope — that task owns updating the assertions below to match. Nothing
here should be read as "this is the exit code that should exist."
"""
from __future__ import annotations

import io
import json
import sys

import pytest

import kustology
from kustology.cli import main


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
    monkeypatch.setattr(sys, "stdin", io.StringIO(messy))
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
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents|take 5"))
    rc = main(["format"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "| take" in out


def test_format_invalid_input_returns_clean_error(monkeypatch, capsys):
    """Empty input must never surface a Python traceback to the user."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc in (0, 1), f"unexpected exit code {rc}"
    assert "Traceback" not in captured.err, captured.err
    assert "Traceback" not in captured.out, captured.out


def test_format_appends_a_newline_the_formatter_did_not_emit(monkeypatch, capsys):
    """`format_query` on a short single-line query returns no trailing
    newline; the CLI must add exactly one so shell output looks normal."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
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
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5\n"))
    rc = main(["format", "-"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "StormEvents | take 5\n"


def test_format_missing_file_reports_a_clean_error_not_a_traceback(capsys):
    """Drives `main`'s bare `except Exception` handler: `open()` on a
    missing path raises `FileNotFoundError`, which isn't `_InputTooLargeError`
    or `SystemExit`, so it falls through to the generic handler and exits 1
    with a one-line message instead of propagating."""
    rc = main(["format", "/no/such/path/does-not-exist.kql"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FileNotFoundError" in captured.err
    assert "Traceback" not in captured.err


def test_validate_clean_query_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
    rc = main(["validate"])
    capsys.readouterr()
    assert rc == 0


def test_validate_broken_query_exits_1(monkeypatch, capsys):
    # Missing RHS of the comparison — produces an Error-severity diagnostic.
    monkeypatch.setattr(
        sys, "stdin", io.StringIO("StormEvents | where EventType ==  | project State")
    )
    rc = main(["validate"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Error" in out


def test_validate_json_output_shape(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO("StormEvents | where EventType ==")
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

    monkeypatch.setattr(sys, "stdin", io.StringIO("NoSuchTable | take 1"))
    rc_strict = main(["validate", "--schema", str(schema)])
    capsys.readouterr()
    assert rc_strict == 1, "expected exit 1 (KS204)"

    monkeypatch.setattr(sys, "stdin", io.StringIO("NoSuchTable | take 1"))
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
    monkeypatch.setattr(sys, "stdin", io.StringIO("T"))
    rc = main(["validate", "--schema", str(schema)])
    captured = capsys.readouterr()
    assert rc == 2
    assert str(schema) in captured.err
    assert "exceeded" in captured.err


def test_validate_schema_malformed_json_is_a_clean_error(tmp_path, monkeypatch, capsys):
    """A `--schema` file that isn't valid JSON raises `json.JSONDecodeError`
    inside `_load_schema` — not `_InputTooLargeError` — so it must fall
    through to the generic exception handler (exit 1) rather than crash.

    `_cmd_validate` reads the query body before the schema, so stdin must
    hold valid input here — otherwise a real failure to reach `_load_schema`
    at all could masquerade as this one exiting 1 for the wrong reason.
    """
    schema = tmp_path / "schema.json"
    schema.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
    rc = main(["validate", "--schema", str(schema)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "JSONDecodeError" in captured.err
    assert "Traceback" not in captured.err


def test_parse_ast_text_default(monkeypatch, capsys):
    """Default `parse` output is a text AST dump showing the table and operators."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
    rc = main(["parse"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "TakeOperator" in out or "Take" in out
    assert "StormEvents" in out


def test_parse_ast_json_shape(monkeypatch, capsys):
    """`parse --ast --json` emits a recursive {kind, text, children} tree."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
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


def test_parse_ir_json_requires_extras(monkeypatch, capsys):
    """`--ir --json` emits the IR when pydantic is present, else exits 2
    with a hint. This venv has the `[ir]` extras installed, so the assertion
    below exercises the success path; the `except ImportError` branch (exit
    2, missing extras) is not reachable from a full-extras install and is
    left to whatever CI job runs the base install."""
    try:
        import pydantic  # noqa: F401
        have_ir = True
    except ImportError:
        have_ir = False

    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
    rc = main(["parse", "--ir", "--json"])
    captured = capsys.readouterr()
    if have_ir:
        assert rc == 0
        ir = json.loads(captured.out)
        assert "main_pipeline" in ir
        assert "operators" in ir["main_pipeline"]
    else:
        assert rc == 2
        assert "[ir]" in captured.err or "pydantic" in captured.err


def test_parse_ir_text_default_format(monkeypatch, capsys):
    """`--ir` without `--json` emits a model_dump pprint that mentions the IR class."""
    try:
        import pydantic  # noqa: F401
    except ImportError:
        pytest.skip("requires [ir] extras")
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
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
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
    rc = main(["format", "-"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "5-byte input ceiling" in captured.err
    assert "KUSTOLOGY_MAX_INPUT_BYTES" in captured.err


def test_non_integer_env_var_override_falls_back_to_the_default_ceiling(
    monkeypatch, capsys
):
    """An unparseable `KUSTOLOGY_MAX_INPUT_BYTES` must not crash the CLI —
    `_max_input_bytes` catches `ValueError` and falls back to the 10 MiB
    default, so an ordinary query still succeeds."""
    monkeypatch.setenv("KUSTOLOGY_MAX_INPUT_BYTES", "not-a-number")
    monkeypatch.setattr(sys, "stdin", io.StringIO("StormEvents | take 5"))
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
