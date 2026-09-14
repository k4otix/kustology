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
import subprocess
import tokenize
from pathlib import Path, PurePosixPath

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


ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"

# The directories ARCHITECTURE's layout tree lists file by file. A module
# added to one of them belongs in the tree with its one-liner.
_LAYOUT_FILE_BY_FILE = {
    "src/kustology",
    "src/kustology/ir",
    "src/kustology/utils",
    "scripts",
}
# The directories the tree lists by their subdirectories. It names a file
# inside one of these only as an illustration, so the files are not pinned;
# a new subdirectory is, because that is a place a reader has to be sent.
_LAYOUT_BY_SUBDIRECTORY = {"tests"}
# Package plumbing carries no description a reader of the tree would use.
_LAYOUT_UNLISTED = {"__init__.py", "py.typed"}


def _layout_tree_entries() -> dict[str, int]:
    """Return ``{path: line number}`` for every entry in the layout tree.

    An entry is the text left of the ``#`` description column, and its
    indentation nests it under the entry above. A line holding only a
    description continues the entry above it and names nothing. One line may
    hold several comma-separated names, none of which can have children.
    """
    lines = ARCHITECTURE.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Layout")
    entries: dict[str, int] = {}
    stack: list[tuple[int, str]] = []
    in_block = False
    for lineno, line in enumerate(lines[start:], start + 1):
        if line.startswith("```"):
            if in_block:
                break
            in_block = True
            continue
        if not in_block:
            continue
        column = line.split("#", 1)[0]
        if not column.strip():
            continue
        indent = len(column) - len(column.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        prefix = stack[-1][1] if stack else ""
        names = [name.strip() for name in column.strip().split(",") if name.strip()]
        for name in names:
            entries.setdefault(prefix + name.rstrip("/"), lineno)
        if len(names) == 1 and names[0].endswith("/"):
            stack.append((indent, prefix + names[0]))
    return entries


def _tracked_files() -> set[str]:
    """Return every file git tracks, as a repo-relative POSIX path.

    ``git ls-files`` rather than a directory walk: it reports what the
    repository has, leaving out build output, ``__pycache__``, and everything
    else ``.gitignore`` covers, which is what the layout tree describes.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {path for path in listing.stdout.split("\0") if path}


def test_architecture_layout_tree_matches_the_repository():
    """ARCHITECTURE's layout tree is a hand-maintained map of the source.

    It is the first thing a new contributor reads to find where a change
    goes, so a module missing from it is invisible and a name that outlived
    its file sends the reader somewhere that is not there. Both directions
    are checked, at the granularity the tree itself keeps: the package
    directories and ``scripts`` file by file, ``tests`` by its
    subdirectories.
    """
    entries = _layout_tree_entries()
    assert entries, "no entries parsed out of ARCHITECTURE.md's layout tree"
    tracked = _tracked_files()
    tracked_dirs = {
        str(parent)
        for path in tracked
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    }

    stale = sorted(
        f"ARCHITECTURE.md:{lineno}: {path}"
        for path, lineno in entries.items()
        if path not in tracked and path not in tracked_dirs
    )
    assert stale == [], "the layout tree names paths the repository does not have:\n" + "\n".join(stale)

    missing_files = sorted(
        path
        for path in tracked
        if str(PurePosixPath(path).parent) in _LAYOUT_FILE_BY_FILE
        and PurePosixPath(path).name not in _LAYOUT_UNLISTED
        and path not in entries
    )
    assert missing_files == [], (
        "the layout tree lists these directories file by file, so add a row "
        "with a one-liner for:\n" + "\n".join(missing_files)
    )

    listed_dirs = _LAYOUT_FILE_BY_FILE | _LAYOUT_BY_SUBDIRECTORY
    missing_dirs = sorted(
        directory
        for directory in tracked_dirs
        if str(PurePosixPath(directory).parent) in listed_dirs
        and directory not in entries
    )
    assert missing_dirs == [], (
        "the layout tree sends a reader to every subdirectory of the "
        "directories it lists, so add a row for:\n" + "\n".join(missing_dirs)
    )


# A citation into a file by line number: ``builder.py:512``, the
# ``builder.py:512-518`` range form, and the bare ``:239-241`` continuation a
# second reference to the same file takes. `CHANGELOG.md` is out of scope
# because a released entry describes the tree as it stood at the release.
_LINE_CITATION = re.compile(
    r"[\w][\w./-]*\.(?:py|md|ya?ml|toml|json|txt|cfg|ini|sh|dll):\d+(?:-\d+)?"
    r"|`:\d+(?:-\d+)?`"
)

_CITATION_FILES = [
    *(REPO_ROOT / "docs").glob("*.md"),
    REPO_ROOT / "README.md",
    REPO_ROOT / "ARCHITECTURE.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "AGENTS.md",
]


def _markdown_prose(path: Path):
    """Yield ``(lineno, text)`` for the lines of a Markdown file outside fences.

    A fenced block holds sample output and file listings, where a line number
    is data rather than a pointer a reader follows.
    """
    fenced = False
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            yield lineno, line


def test_prose_cites_symbols_rather_than_line_numbers():
    """A pointer into a file survives edits only when it names a symbol.

    ``builder.py:512`` is right the day it is written and silently wrong
    after the next insertion above it, and nothing tells the reader which of
    the two they are looking at. Cite the function, class, heading, or test
    name instead; a rename breaks loudly at the grep the reader runs.
    """
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {match.group(0)}"
        for path in _CITATION_FILES
        for lineno, line in _markdown_prose(path)
        for match in _LINE_CITATION.finditer(line)
    ]
    assert hits == [], (
        "these pointers cite a line number and go stale on the next edit "
        "above them; name the symbol instead:\n" + "\n".join(hits)
    )
