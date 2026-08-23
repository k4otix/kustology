# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Counts written into the docs must match what they count.

Six closed counts in the 0.2.0 documentation pass turned out wrong -- "eight
operators" (nine), "five jobs" (six), "four operator modifiers" (six across
four), "four of the eight literal kinds" (four of eleven), and two more. The
durable fix is to stop writing counts: derive them at runtime where the file
runs, and describe the mechanism where it does not.

Two survived that treatment, because a Markdown file cannot compute and the
number is the point. Those two are pinned here, so the suite fails when they
drift instead of a reader discovering it. Both rebuild the true value by
introspection -- neither writes the expected number down.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"

# The three jobs the documented local loop (pytest / ruff / mypy) stands in
# for. Everything else in the workflow is a job CONTRIBUTING must list as
# having no local counterpart.
_LOCALLY_COVERED_JOBS = {"test", "test-ir", "lint"}

_NUMBER_WORDS = {
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _workflow_jobs() -> set[str]:
    """Job names from ``test.yml``, without a YAML parser.

    A regex, not ``yaml.safe_load``, so this module imports with no optional
    dependency at all -- it makes claims about the repository's own files and
    should keep running in the barest environment that can collect it. (An
    earlier draft was right that ``pyyaml`` was unreachable from the test
    extras; it now ships in ``[test]``, but the regex has no reason to change.
    The grammar it needs is tiny: a job is a two-space-indented mapping key
    under the top-level ``jobs:``.)
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

    # ...and the prose count beside the table.
    stated = re.search(r"defines (\w+) jobs", text)
    assert stated, "CONTRIBUTING.md no longer states how many jobs test.yml defines"
    word = stated.group(1)
    assert _NUMBER_WORDS.get(word) == len(jobs), (
        f"CONTRIBUTING.md says test.yml defines {word!r} jobs; it defines "
        f"{len(jobs)} ({', '.join(sorted(jobs))})"
    )

    stated_uncovered = re.search(r"the other \*\*(\w+)\*\* have no local counterpart", text)
    assert stated_uncovered, "CONTRIBUTING.md no longer states the uncovered-job count"
    assert _NUMBER_WORDS.get(stated_uncovered.group(1)) == len(uncovered), (
        f"CONTRIBUTING.md says {stated_uncovered.group(1)!r} jobs have no local "
        f"counterpart; {len(uncovered)} do ({', '.join(sorted(uncovered))})"
    )


def test_architecture_states_the_real_corpus_split():
    """``extract_complex_corpus.py`` regenerates only part of the corpus.

    ARCHITECTURE says how much. The script writes one ``.kql`` per entry in
    ``RELATIVE_PATHS`` and deletes nothing, so the rest are hand-written --
    a claim that goes stale the moment either number moves.
    """
    script = REPO_ROOT / "scripts" / "extract_complex_corpus.py"
    tree = ast.parse(script.read_text())
    paths = None
    for node in ast.walk(tree):
        target = getattr(node, "target", None)
        if isinstance(node, ast.AnnAssign) and getattr(target, "id", "") == "RELATIVE_PATHS":
            paths = [e.value for e in node.value.elts]
    assert paths, "RELATIVE_PATHS not found in extract_complex_corpus.py"

    extracted = len(paths)
    total = len(list((REPO_ROOT / "tests" / "fixtures" / "complex_queries").glob("*.kql")))
    text = ARCHITECTURE.read_text()
    assert f"({extracted} of {total})" in text, (
        f"ARCHITECTURE.md does not say '({extracted} of {total})' for the "
        f"corpus split: the script writes {extracted} fixtures and the "
        f"directory holds {total}"
    )
    assert f"the other {total - extracted} are hand-written" in text, (
        f"ARCHITECTURE.md does not say the other {total - extracted} fixtures "
        f"are hand-written"
    )
