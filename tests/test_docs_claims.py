# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Claims written into the docs must match what they describe.

A written-out count is never trusted: a doc derives its numbers where it
runs and describes the producing mechanism where it does not, so no count
exists here for this file to re-derive.

Hand-maintained *lists* go stale the same way, when something is added on
one side of the repository and not the other. Each list pinned here is
rebuilt from the directory or file it claims to enumerate.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
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
    catches; the table is what a contributor reads to know what CI does that
    their local loop does not.

    The scope is ``test.yml``. ``canary.yml`` is scheduled and manually
    dispatched, never triggered by a pull request, so it is outside the loop
    the paragraph describes and belongs outside the table.
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
    the table is where they learn ``examples/`` exists at all, and a stale
    row fails them one step later than a missing one.

    No count is asserted: the table counts itself, and a written-out count is
    the kind of claim this file exists to keep out of the docs.
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


# Markers of prose that narrates history instead of stating what is true, plus
# the two Latin abbreviations AGENTS.md spells out. Every pattern was checked
# against the whole tree before being added, so a hit is a real finding rather
# than a phrase this repository happens to use.
#
# ``used to`` also catches the passive "X is used to build Y", which the style
# guide's active-voice rule already rejects. Rephrase as "for".
#
# This is a tripwire, not a proof. "This was hardcoded ``False``", "the bare
# ``ToString()`` this replaced", and "the drop stopped firing" all break the
# same rule; only the third of those matches anything here, and phrasings like
# the first are why a review still has to read the prose. Widen the pattern
# when one gets past it.
_NOT_GREENFIELD = re.compile(
    r"\b(previously|formerly|originally|historically|no longer|used to"
    r"|at one point|now that|(?:this|that|which|it) replaced"
    r"|before this (?:change|commit|fix)|e\.g\.|i\.e\.)",
    re.IGNORECASE,
)
# Anchored on ``REPO_ROOT`` like every other path here, not on the working
# directory: this list is built at import time, so a CWD-relative ``Path`` both
# fails collection from elsewhere and -- for the two globbed roots, which just
# come back empty -- makes the gate vacuous without failing.
_PROSE_FILES = [
    *(REPO_ROOT / "src").rglob("*.py"),
    *(REPO_ROOT / "tests").rglob("*.py"),
    *(REPO_ROOT / "docs").glob("*.md"),
    REPO_ROOT / "README.md",
    REPO_ROOT / "ARCHITECTURE.md",
    REPO_ROOT / "CONTRIBUTING.md",
]

# This file quotes the phrasings it bans, in the comment above
# ``_NOT_GREENFIELD`` and in the pattern itself, so it cannot be its own
# subject. Everything else under ``tests`` is in scope.
_EXEMPT = {Path(__file__).resolve()}


def _prose(path: Path):
    """Yield ``(lineno, text)`` for the prose in one file.

    A Markdown file is prose throughout. A Python file is scanned for comments
    and docstrings only: an assertion message such as "the binder no longer
    crashes on the repro" is test data the reader needs to see verbatim, and a
    name such as ``test_shapes_that_used_to_collide`` is an identifier. Reading
    whole lines would flag both, and the false positives are what makes a gate
    get widened until it means nothing.

    This narrows ``src`` too: the text of an exception message is out of scope
    wherever it lives. Those are short and read at the call site, so the
    explanatory prose around them is where the rule earns its keep.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        yield from enumerate(text.splitlines(), 1)
        return
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            yield tok.start[0], tok.string
    # Docstrings come from the tree rather than the token stream, so a string
    # that merely opens a line is not mistaken for one.
    for node in ast.walk(ast.parse(text)):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for stmt in block:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, str):
                    for offset, line in enumerate(ast.get_source_segment(text, stmt).splitlines()):
                        yield stmt.lineno + offset, line


def test_prose_is_greenfield_and_spells_out_latin():
    """AGENTS.md's documentation-style rules; CHANGELOG.md and AGENTS.md are exempt."""
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
        for path in _PROSE_FILES
        if path.resolve() not in _EXEMPT
        for lineno, line in _prose(path)
        if _NOT_GREENFIELD.search(line)
    ]
    assert hits == [], "\n".join(hits)
