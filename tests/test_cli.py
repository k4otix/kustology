# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Subprocess-based tests for the `kustology` CLI.

We invoke the CLI via `python -m kustology.cli` so the tests don't
depend on `pip install -e .` having been run. The installed `kustology`
entry point (declared in pyproject.toml `[project.scripts]`) shares the
same `main()` function, so testing the module form covers both.
"""
from __future__ import annotations

import json
import subprocess
import sys

import kustology


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "kustology.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_version_prints_runtime_version():
    result = _run("version")
    assert result.returncode == 0, result.stderr
    assert kustology.__version__ in result.stdout
    assert "kustology" in result.stdout


def test_format_from_stdin():
    messy = (
        'StormEvents|where EventType=="Tornado"|summarize '
        "Total=sum(DeathsDirect) by State"
    )
    result = _run("format", "-", stdin=messy)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    # Formatted output should split across multiple lines with pipe indentation.
    assert "\n" in out
    assert "| where" in out
    assert "| summarize" in out


def test_format_from_file(tmp_path):
    q = tmp_path / "q.kql"
    q.write_text('StormEvents|where EventType=="Tornado"', encoding="utf-8")
    result = _run("format", str(q))
    assert result.returncode == 0, result.stderr
    assert "| where" in result.stdout


def test_format_default_input_is_stdin():
    # No FILE argument — should default to stdin.
    result = _run("format", stdin="StormEvents|take 5")
    assert result.returncode == 0, result.stderr
    assert "| take" in result.stdout


def test_format_invalid_input_returns_clean_error():
    """Empty input must never surface a Python traceback to the user."""
    result = _run("format", stdin="")
    assert result.returncode in (0, 1), f"unexpected exit code {result.returncode}"
    assert "Traceback" not in result.stderr, result.stderr
    assert "Traceback" not in result.stdout, result.stdout


def test_validate_clean_query_exits_0():
    result = _run("validate", stdin="StormEvents | take 5")
    assert result.returncode == 0, result.stderr


def test_validate_broken_query_exits_1():
    # Missing RHS of the comparison — produces an Error-severity diagnostic.
    result = _run("validate", stdin='StormEvents | where EventType ==  | project State')
    assert result.returncode == 1
    # Human-readable output: at least one line with "Error" severity.
    assert "Error" in result.stdout or "Error" in result.stderr


def test_validate_json_output_shape():
    result = _run("validate", "--json", stdin='StormEvents | where EventType ==')
    assert result.returncode == 1
    diags = json.loads(result.stdout)
    assert isinstance(diags, list)
    assert len(diags) >= 1
    d = diags[0]
    for key in ("start", "length", "message", "severity"):
        assert key in d, f"missing key {key} in {d}"
    # severity is a string like "Error" / "Warning" / "Suggestion".
    assert isinstance(d["severity"], str)


def test_validate_ignore_unknown_tables_flag(tmp_path):
    """`--ignore-unknown-tables` flips exit code from 1 to 0 for KS204."""
    schema = tmp_path / "schema.json"
    schema.write_text(
        '{"StormEvents": {"State": "string", "DeathsDirect": "int"}}',
        encoding="utf-8",
    )

    result_strict = _run(
        "validate", "--schema", str(schema),
        stdin="NoSuchTable | take 1",
    )
    assert result_strict.returncode == 1, (
        f"expected exit 1 (KS204), got {result_strict.returncode}: "
        f"stdout={result_strict.stdout!r} stderr={result_strict.stderr!r}"
    )

    result_lax = _run(
        "validate", "--schema", str(schema), "--ignore-unknown-tables",
        stdin="NoSuchTable | take 1",
    )
    assert result_lax.returncode == 0, (
        f"expected exit 0 (KS204 suppressed), got {result_lax.returncode}: "
        f"stdout={result_lax.stdout!r} stderr={result_lax.stderr!r}"
    )


def test_parse_ast_text_default():
    """Default `parse` output is a text AST dump showing the table and operators."""
    result = _run("parse", stdin="StormEvents | take 5")
    assert result.returncode == 0, result.stderr
    assert "TakeOperator" in result.stdout or "Take" in result.stdout
    assert "StormEvents" in result.stdout


def test_parse_ast_json_shape():
    """`parse --ast --json` emits a recursive {kind, text, children} tree."""
    result = _run("parse", "--ast", "--json", stdin="StormEvents | take 5")
    assert result.returncode == 0, result.stderr
    tree = json.loads(result.stdout)
    for key in ("kind", "text", "children"):
        assert key in tree, f"missing key {key} in top-level node"
    assert isinstance(tree["children"], list)

    def collect_kinds(n):
        yield n["kind"]
        for c in n["children"]:
            yield from collect_kinds(c)
    kinds = set(collect_kinds(tree))
    assert any("TakeOperator" in k or "PipeExpression" in k for k in kinds), kinds


def test_parse_ir_json_requires_extras():
    """`--ir --json` emits the IR when pydantic is present, else exits 2 with a hint."""
    try:
        import pydantic  # noqa: F401
        have_ir = True
    except ImportError:
        have_ir = False

    result = _run("parse", "--ir", "--json", stdin="StormEvents | take 5")
    if have_ir:
        assert result.returncode == 0, result.stderr
        ir = json.loads(result.stdout)
        assert "main_pipeline" in ir
        assert "operators" in ir["main_pipeline"]
    else:
        assert result.returncode == 2
        assert "[ir]" in result.stderr or "pydantic" in result.stderr


def test_parse_ir_text_default_format():
    """`--ir` without `--json` emits a model_dump pprint that mentions the IR class."""
    try:
        import pydantic  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("requires [ir] extras")
    result = _run("parse", "--ir", stdin="StormEvents | take 5")
    assert result.returncode == 0, result.stderr
    assert "QueryIR" in result.stdout or "main_pipeline" in result.stdout
