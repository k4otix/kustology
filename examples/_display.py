# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Presentation helpers shared by the examples.

The examples teach the kustology API. This module holds the furniture
around it: headings, narrative blocks, callouts, and highlighted query
and JSON blocks. Keeping that here is what lets an example body call
one function per idea and carry no formatting branches.

Colour and syntax highlighting are optional. Install
``pip install 'kustology[examples]'`` for the `Rich
<https://rich.readthedocs.io>`_ rendering; without it every function
here falls back to plain text. Two environment variables override the
choice: ``NO_COLOR`` turns colour off, and
``KUSTOLOGY_EXAMPLES_PLAIN=1`` takes the fallback path even when Rich
is installed.

The leading underscore in the file name keeps it out of
``tests/test_examples.py``, which imports every ``examples/*.py`` that
does not start with one and calls its ``main()``.

Nothing here is part of the kustology API. Copy an example without it
and replace these calls with ``print``.
"""

import os
import shutil
import sys
import textwrap

_MAX_WIDTH = 78
_MIN_WIDTH = 56
_MIN_CELL = 12

_ANSI = {
    "title": "\033[1m",
    "heading": "\033[1;36m",
    "rule": "\033[2;36m",
    "lede": "\033[3m",
    "note": "\033[36m",
    "accent": "\033[35m",
    "dim": "\033[2m",
    "error": "\033[1;31m",
    "warning": "\033[33m",
    "info": "\033[36m",
}
_RESET = "\033[0m"

_PLAIN = os.environ.get("KUSTOLOGY_EXAMPLES_PLAIN") == "1"

if _PLAIN:
    _console = None
else:
    try:
        from rich.console import Console
        from rich.padding import Padding
        from rich.syntax import Syntax
        from rich.table import Table

        # markup=False so a literal "[warning]" in an example's output
        # stays text instead of being read as a Rich style tag.
        _console = Console(markup=False, highlight=False)
    except ImportError:
        _console = None


def _kql_lexer() -> str:
    """Return the Pygments lexer name to highlight KQL with."""
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return "sql"
    try:
        get_lexer_by_name("kusto")
    except ClassNotFound:
        # Older Pygments releases ship no Kusto lexer; SQL is the closest.
        return "sql"
    return "kusto"


def _width() -> int:
    """Return the wrap width for narrative text."""
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(_MIN_WIDTH, min(_MAX_WIDTH, columns - 2))


def _colour() -> bool:
    """Report whether the current stdout should carry ANSI colour."""
    if "NO_COLOR" in os.environ:
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def paint(text: str, style: str) -> str:
    """Return ``text`` wrapped in the ANSI codes for ``style``."""
    if not _colour() or style not in _ANSI:
        return text
    return f"{_ANSI[style]}{text}{_RESET}"


def _wrapped(text: str, indent: str, first: str | None = None) -> list[str]:
    return textwrap.wrap(
        " ".join(text.split()),
        width=_width(),
        initial_indent=indent if first is None else first,
        subsequent_indent=indent,
    )


def banner(title: str, shows: str, look_for: str = "") -> None:
    """Open the run with the example's title and what it demonstrates.

    ``shows`` states the subject in a sentence or two. ``look_for``
    points at the part of the output that carries the lesson.
    """
    width = _width()
    print()
    print(paint("═" * width, "rule"))
    print(paint(f" {title}", "title"))
    print(paint("═" * width, "rule"))
    for line in _wrapped(shows, " "):
        print(line)
    if look_for:
        print()
        for line in _wrapped(f"What to look for: {look_for}", " "):
            print(paint(line, "lede"))


def section(title: str, lede: str = "") -> None:
    """Start a section, with an optional sentence framing its output."""
    width = _width()
    heading = f"── {title} "
    print()
    print(paint(heading + "─" * max(0, width - len(heading)), "heading"))
    if lede:
        for line in _wrapped(lede, " "):
            print(paint(line, "lede"))
        print()


def note(text: str) -> None:
    """Attach an explanation to the output above it."""
    for line in _wrapped(text, "    ", first="  → "):
        print(paint(line, "note"))


def takeaway(text: str, more: str = "") -> None:
    """Close the run with the point of the example and where to read on."""
    section("Takeaway")
    for line in _wrapped(text, " "):
        print(line)
    if more:
        print()
        for line in _wrapped(f"Read on: {more}", " "):
            print(paint(line, "dim"))
    print()


def severity(name: str, width: int = 7) -> str:
    """Return a severity name padded to ``width`` and coloured by level."""
    return paint(f"{name:<{width}}", name)


def kql(query: str, indent: int = 2) -> None:
    """Print a KQL block, highlighted when Rich is installed."""
    _block(query, _kql_lexer(), indent)


def data(text: str, lang: str = "json", indent: int = 2) -> None:
    """Print a data block such as JSON, highlighted when Rich is installed."""
    _block(text, lang, indent)


def _block(text: str, lang: str, indent: int) -> None:
    if _console is None:
        pad = " " * indent
        for line in text.splitlines():
            print(f"{pad}{line}")
        return
    # word_wrap keeps long lines (a raw_text field, a canonical form) whole;
    # the default crops them to the terminal width and loses the tail.
    syntax = Syntax(
        text, lang, theme="ansi_dark", background_color="default", word_wrap=True,
    )
    _console.print(Padding(syntax, (0, 0, 0, indent), expand=False))


def table(headers: list[str], rows: list[list[str]], indent: int = 2) -> None:
    """Print a small table, boxed when Rich is installed."""
    if _console is None:
        _plain_table(headers, rows, indent)
        return
    rich_table = Table(show_edge=False, pad_edge=False, header_style="bold")
    for header in headers:
        rich_table.add_column(header)
    for row in rows:
        rich_table.add_row(*(str(cell) for cell in row))
    _console.print(Padding(rich_table, (0, 0, 0, indent), expand=False))


def _plain_table(headers: list[str], rows: list[list[str]], indent: int) -> None:
    gap = 2
    columns = len(headers)
    widths = [
        max(len(str(cell)) for cell in [headers[i], *(row[i] for row in rows)])
        for i in range(columns)
    ]
    # Give back the overflow one character at a time, always from the widest
    # column, so a long prose column wraps and the short label columns don't.
    budget = _width() - indent - gap * (columns - 1)
    while sum(widths) > budget and max(widths) > _MIN_CELL:
        widths[widths.index(max(widths))] -= 1

    pad = " " * indent
    _plain_row(headers, widths, pad, gap)
    print(f"{pad}{(' ' * gap).join('-' * w for w in widths)}")
    for row in rows:
        _plain_row([str(cell) for cell in row], widths, pad, gap)


def _plain_row(cells: list[str], widths: list[int], pad: str, gap: int) -> None:
    wrapped = [textwrap.wrap(cell, width=widths[i]) or [""] for i, cell in enumerate(cells)]
    for line_index in range(max(len(w) for w in wrapped)):
        parts = [
            (w[line_index] if line_index < len(w) else "").ljust(widths[i])
            for i, w in enumerate(wrapped)
        ]
        print(f"{pad}{(' ' * gap).join(parts)}".rstrip())
