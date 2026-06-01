# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Skip the whole tier-2 test directory when the ``[ir]`` extra isn't installed.

Base-install CI runs ``pytest`` against a venv without pydantic; without this
hook, every collection under ``tests/ir/`` would raise ``ImportError``.
``pytest.importorskip`` short-circuits the entire directory cleanly.
"""

import pytest

pytest.importorskip("pydantic")


# Minimal inline schema for the tier-2 tests. Covers exactly the tables and
# columns the round-trip and binder tests reference. Keep small.
_SAMPLE_SCHEMA: dict[str, dict[str, str]] = {
    "DeviceProcessEvents": {
        "FileName": "string",
        "ProcessCommandLine": "string",
        "AccountName": "string",
        "DeviceName": "string",
        "DeviceId": "string",
        "TimeGenerated": "datetime",
        "ProcessId": "long",
        "A": "long",
        "B": "long",
        "C": "long",
        "InitiatingProcessFileName": "string",
    },
    "DeviceFileEvents": {
        "DeviceId": "string",
        "FileName": "string",
        "TimeGenerated": "datetime",
    },
    "SecurityEvent": {
        "ProcessId": "long",
        "Computer": "string",
        "TimeGenerated": "datetime",
        "Account": "string",
        "EventID": "long",
    },
    "SigninLogs": {
        "Account": "string",
        "TimeGenerated": "datetime",
    },
}


@pytest.fixture(scope="module")
def sample_schema() -> dict[str, dict[str, str]]:
    """Inline ``{table: {column: type}}`` schema for the tier-2 tests."""
    return _SAMPLE_SCHEMA
