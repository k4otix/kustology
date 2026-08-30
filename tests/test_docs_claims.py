# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Claims written into the docs must match what they describe.

The standing rule is that a written-out count is never trusted: a doc
derives its numbers where it runs and describes the producing mechanism
where it does not, so no count exists here for this file to re-derive and
compare.

The same discipline extends to hand-maintained *lists*, which go stale the
same way a count does and for the same reason: something is added on one
side of the repository and not the other. Each list pinned here is rebuilt
from the directory or file it claims to enumerate.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# The three jobs the documented local loop (pytest / ruff / mypy) stands in
# for. Everything else in the workflow is a job CONTRIBUTING must list as
# having no local counterpart.
_LOCALLY_COVERED_JOBS = {"test", "test-ir", "lint"}


def _workflow_jobs() -> set[str]:
    """Return the job names from ``test.yml``, without a YAML parser.

    A regex, not ``yaml.safe_load``, so this module imports with no optional
    dependency at all -- it makes claims about the repository's own files and
    should keep running in the barest environment that can collect it. The
    grammar it needs is tiny: a job is a two-space-indented mapping key under
    the top-level ``jobs:``.
    """
    jobs: set[str] = set()
    in_jobs = False
    for line in WORKFLOW.read_text().splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs:
            if re.match(r"^\S", line):  # a new top-level key ends the block
                break
            match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
            if match:
                jobs.add(match.group(1))
    return jobs


def test_contributing_names_every_ci_job_without_a_local_counterpart():
    """CONTRIBUTING's CI table is a hand-maintained list of workflow jobs.

    Adding a job to ``test.yml`` and not to the table is the drift this
    catches -- the table is what a contributor reads to know what CI does
    that their local loop does not.

    Scoped to ``test.yml`` on purpose. ``canary.yml`` is scheduled and
    manually dispatched, never triggered by a pull request, so it is not
    part of the loop the paragraph describes and its absence from the table
    is deliberate rather than drift.
    """
    jobs = _workflow_jobs()
    uncovered = jobs - _LOCALLY_COVERED_JOBS
    text = CONTRIBUTING.read_text()

    missing = sorted(job for job in uncovered if f"`{job}`" not in text)
    assert not missing, (
        f"CONTRIBUTING.md does not mention CI job(s) {missing}. Every job in "
        f"{WORKFLOW.name} that the local pytest/ruff/mypy loop does not cover "
        f"belongs in its table."
    )


def test_readme_points_at_every_runnable_example():
    """README's example table is a hand-maintained list of `examples/*.py`.

    Adding an example and not the row is the drift this catches. The README
    is the only one of the three docs a new user is guaranteed to read, so
    the table is where they learn ``examples/`` exists at all -- a stale
    list is the same failure one step later than a missing one.

    Deliberately not asserting a count: the table counts itself, and a
    written-out count is exactly the kind of claim this file exists to keep
    out of the docs.
    """
    readme = (REPO_ROOT / "README.md").read_text()
    on_disk = {
        path.name
        for path in (REPO_ROOT / "examples").glob("*.py")
        if not path.name.startswith("_")
    }
    missing = sorted(name for name in on_disk if f"examples/{name}" not in readme)
    assert not missing, (
        f"README.md does not link {missing}. Every runnable example belongs "
        f"in its table -- the README is the only one of the three docs a new "
        f"user is guaranteed to read."
    )

    # ...and the reverse: a linked example missing from examples/.
    linked = set(re.findall(r"examples/([A-Za-z0-9_]+\.py)", readme))
    stale = sorted(linked - on_disk)
    assert not stale, f"README.md links {stale}, which are not in examples/"


_NOT_GREENFIELD = re.compile(r"\b(previously|no longer|used to be|e\.g\.|i\.e\.)", re.IGNORECASE)
_PROSE_FILES = [
    *Path("src").rglob("*.py"),
    *Path("docs").glob("*.md"),
    Path("README.md"),
    Path("ARCHITECTURE.md"),
    Path("CONTRIBUTING.md"),
]


def test_prose_is_greenfield_and_spells_out_latin():
    """AGENTS.md's documentation-style rules; CHANGELOG.md and AGENTS.md are exempt."""
    hits = [
        f"{path}:{lineno}: {line.strip()}"
        for path in _PROSE_FILES
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _NOT_GREENFIELD.search(line)
    ]
    assert hits == [], "\n".join(hits)
