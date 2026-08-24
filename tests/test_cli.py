# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Subprocess-based tests for the `kustology` CLI.

We invoke the CLI via `python -m kustology.cli` so the tests don't
depend on `pip install -e .` having been run. The installed `kustology`
entry point (declared in pyproject.toml `[project.scripts]`) shares the
same `main()` function, so testing the module form covers both.

`tests/test_cli_inprocess.py` calls `main()` directly and owns the CLI's
behavioral contract — every exit code, every output shape, every edge case.
This file keeps only what a direct call to `main()` cannot prove: that the
shipped entry point launches at all, and the handful of cases that need the
real interpreter underneath it. That's an entry-point smoke test (`version`),
one exit-code case pinned through the real binary rather than only through
`main()` (a missing file, exit 2), one through-the-binary check of the
human-branch exit-1 contract, and the byte-ceiling test that needs the
interpreter's actual `sys.stdin.buffer` — a `TextIOWrapper` double can't
stand in for that one. Every other case that used to live here duplicated
an in-process test asserting the identical contract and was deleted rather
than kept as a slower second copy.
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
    """Exit 2 through the real entry point, not just through `main()` —
    the docstring's contract is what a shell script branches on."""
    result = _run("format", "/no/such/path/does-not-exist.kql")
    assert result.returncode == 2, result.stderr
    assert "FileNotFoundError" in result.stderr
    assert "Traceback" not in result.stderr


def test_input_ceiling_counts_bytes_on_real_stdin():
    """The in-process suite fakes `sys.stdin` with a `TextIOWrapper`; this
    runs the same 20-character/28-byte payload through the interpreter's own
    `sys.stdin.buffer`, so the byte accounting is proved on the real object
    and not only on the double. Cap 22 rejects it, cap 28 lets it through."""
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
    # Human-readable output: at least one line with "Error" severity.
    assert "Error" in result.stdout or "Error" in result.stderr


def test_non_ascii_output_survives_a_charmap_stdout():
    """Windows consoles default to a charmap codec that cannot encode most
    of Unicode; KQL is arbitrary Unicode, so the CLI reconfigures its
    streams to UTF-8. PYTHONIOENCODING=cp1252 reproduces the Windows
    failure mode on any platform (PR #17's windows-latest cell)."""
    query = 'T | where s == "日本語"'
    result = _run("format", stdin=query, env={"PYTHONIOENCODING": "cp1252"})
    assert result.returncode == 0, result.stderr
    assert "日本語" in result.stdout
