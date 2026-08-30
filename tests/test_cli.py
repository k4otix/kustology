# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Subprocess-based tests for the `kustology` CLI.

The tests invoke the CLI via `python -m kustology.cli` so they don't depend on
`pip install -e .` having been run. The installed `kustology` entry point
(declared in pyproject.toml `[project.scripts]`) shares the same `main()`, so
the module form covers both.

`tests/test_cli_inprocess.py` calls `main()` directly and owns the behavioral
contract: every exit code, output shape, and edge case. This file keeps only
what a direct call cannot prove: that the shipped entry point launches, plus
the cases needing the real interpreter underneath it, such as its own
`sys.stdin.buffer` and stream encodings. A case that duplicates an in-process
assertion is a slower second copy.
"""
from __future__ import annotations

import os
import subprocess
import sys

import kustology


def _run(
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "kustology.cli", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )


def test_version_prints_runtime_version():
    result = _run("version")
    assert result.returncode == 0, result.stderr
    assert kustology.__version__ in result.stdout
    assert "kustology" in result.stdout


def test_missing_file_is_a_usage_error():
    """Pin exit 2 through the real entry point: shell scripts branch on it."""
    result = _run("format", "/no/such/path/does-not-exist.kql")
    assert result.returncode == 2, result.stderr
    assert "FileNotFoundError" in result.stderr
    assert "Traceback" not in result.stderr


def test_input_ceiling_counts_bytes_on_real_stdin():
    """Prove the byte accounting on the interpreter's own `sys.stdin.buffer`.

    The in-process suite fakes `sys.stdin` with a `TextIOWrapper`. The same
    20-character, 28-byte payload runs through the real object here: cap 22
    rejects it, cap 28 lets it through.
    """
    payload = 'T | where a== "日本語だ"'
    assert (len(payload), len(payload.encode("utf-8"))) == (20, 28)

    tight = _run(
        "format", stdin=payload, env={"KUSTOLOGY_MAX_INPUT_BYTES": "22"}
    )
    assert tight.returncode == 2, tight.stderr
    assert "22-byte input ceiling" in tight.stderr
    assert tight.stdout == ""

    loose = _run(
        "format", stdin=payload, env={"KUSTOLOGY_MAX_INPUT_BYTES": "28"}
    )
    assert loose.returncode == 0, loose.stderr
    assert "日本語だ" in loose.stdout


def test_validate_broken_query_exits_1():
    # Missing RHS of the comparison — produces an Error-severity diagnostic.
    result = _run("validate", stdin='StormEvents | where EventType ==  | project State')
    assert result.returncode == 1
    assert "Error" in result.stdout or "Error" in result.stderr


def test_non_ascii_output_survives_a_charmap_stdout():
    """Guard the CLI's UTF-8 stream reconfiguration under a charmap stdout.

    Windows consoles default to a charmap codec that cannot encode most of
    Unicode, and KQL is arbitrary Unicode, so the CLI reconfigures its streams
    to UTF-8. PYTHONIOENCODING=cp1252 reproduces that console anywhere.
    """
    query = 'T | where s == "日本語"'
    result = _run("format", stdin=query, env={"PYTHONIOENCODING": "cp1252"})
    assert result.returncode == 0, result.stderr
    assert "日本語" in result.stdout
