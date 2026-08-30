# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Text that UTF-16 cannot encode raises ``ValueError`` before it reaches the CLR.

A Python ``str`` can hold an unpaired surrogate; UTF-16 has no encoding for
one. pythonnet marshals a ``str`` argument by encoding it to UTF-16, so the
failure lands as an unhandled CLR exception that terminates the interpreter
with ``SIGABRT``. No Python ``except`` clause intercepts that, ``except
BaseException`` included, so every entry point checks the text before it
crosses.

The probes run in a child process. In-process, a regressed guard takes the
whole pytest session down with no summary; from a child, the parent reports
which position regressed and the rest of the suite still runs. The child
emits one JSON record per position, so a single CLR startup covers all of
them.
"""

import json
import subprocess
import sys

import pytest

from kustology import parse

_CHILD = r'''
import json
from kustology import format_query, parse, validate
from kustology.utils.schema_state import build_global_state

S = "\ud800"
PROBES = {
    "parse": lambda: parse('T | where X == "' + S + '"'),
    "validate": lambda: validate('T | where X == "' + S + '"'),
    "format_query": lambda: format_query('T | where X == "' + S + '"'),
    "schema-table-name": lambda: build_global_state({S: {"a": "long"}}),
    "schema-column-name": lambda: build_global_state({"T": {S: "long"}}),
    "schema-string": lambda: build_global_state({"T": "(" + S + ":long)"}),
    "schema-column-type": lambda: build_global_state({"T": {"a": S}}),
    "parse-with-bad-schema": lambda: parse("T | count", schema={"T": {S: "long"}}),
}

out = {}
for label, probe in PROBES.items():
    try:
        probe()
        out[label] = {"raised": None, "message": "", "cause": ""}
    except ValueError as exc:
        out[label] = {
            "raised": "ValueError",
            "message": str(exc),
            "cause": type(exc.__cause__).__name__,
        }
    except BaseException as exc:
        out[label] = {"raised": type(exc).__name__, "message": str(exc), "cause": ""}

print("BEGIN-JSON", flush=True)
print(json.dumps(out), flush=True)
'''

# Each position with the word its message must carry, so a guard firing from
# the wrong place cannot pass by raising the right type.
EXPECTED_POSITION_WORDS = {
    "parse": "query text",
    "validate": "query text",
    "format_query": "query text",
    "schema-table-name": "table name",
    "schema-column-name": "column name",
    "schema-string": "string for table",
    "schema-column-type": "column type",
    "parse-with-bad-schema": "column name",
}


@pytest.fixture(scope="module")
def child_result():
    """Run every surrogate probe once, in a child, and return its report."""
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, (
        f"child exited {proc.returncode} "
        f"({'killed by signal' if proc.returncode < 0 else 'error'}) — a lone "
        f"surrogate reached the CLR.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    _, _, payload = proc.stdout.partition("BEGIN-JSON\n")
    return json.loads(payload)


@pytest.mark.parametrize("label", sorted(EXPECTED_POSITION_WORDS))
def test_every_boundary_rejects_a_surrogate(child_result, label):
    record = child_result[label]
    assert record["raised"] == "ValueError", (
        f"{label} raised {record['raised']!r}, expected ValueError"
    )
    assert "not encodable to UTF-16" in record["message"]
    assert EXPECTED_POSITION_WORDS[label] in record["message"]


def test_the_message_names_the_offending_position(child_result):
    assert "position 16" in child_result["parse"]["message"]


def test_the_underlying_encode_error_is_chained(child_result):
    assert child_result["parse"]["cause"] == "UnicodeEncodeError"


def test_astral_text_is_not_rejected():
    """A surrogate *pair* is ordinary input; only a lone half is unencodable."""
    assert parse('T | where X == "\U0001F600"').has_semantics is False
