# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Command-line interface for kustology.

Subcommands: version, format, validate, parse.

Exit codes:
  0 — success.
  1 — input had errors (parse failure, Error-severity diagnostic).
  2 — usage error (bad flags, missing or unreadable file, malformed
      ``--schema`` JSON, missing optional extra, input exceeded the size
      ceiling).

1 says the query is wrong and 2 says the invocation is wrong, which is what
a CI job branches on: an unreadable path or an unparseable ``--schema`` says
nothing about the KQL. Both ``format`` and ``parse`` run the validator before
they emit anything, so neither writes output derived from a query the parser
rejected.

Because 2 is a claim about the *invocation*, the mapping lives where one is
read: :func:`_read_input` and :func:`_load_schema`, which raise
:class:`_UsageError`. A blanket ``except OSError`` in :func:`main` would cover
every ``sys.stdout.write`` too and report a usage error for
``kustology parse --ast --json big.kql | head``, whose reader stopped reading.

A broken pipe is neither a usage error nor a verdict. Each command decides
its exit code before it writes and wraps only the writing in
:func:`_tolerate_broken_pipe`, so a reader hanging up stops the output and
nothing else: ``kustology validate q.kql | head`` still exits 1 on a query
that fails validation. A pipe breaking outside every guard, before a code was
ever decided, reaches :func:`main`'s own arm and exits 0.
"""
from __future__ import annotations

import argparse
import contextlib
import json as _json
import os
import sys

from . import __version__
from .services import format_query, parse, validate
from .utils.walker import MAX_AST_DEPTH, node_to_dict

# Bound on the bytes read from stdin or a file. KQL queries are not large, so
# a 10 MB ceiling makes an oversized payload (CI webhook abuse, misrouted log
# dump) fail fast instead of exhausting host memory. Override with
# ``KUSTOLOGY_MAX_INPUT_BYTES``.
_DEFAULT_MAX_INPUT_BYTES = 10 * 1024 * 1024


def _max_input_bytes() -> int:
    raw = os.environ.get("KUSTOLOGY_MAX_INPUT_BYTES")
    if raw is None:
        return _DEFAULT_MAX_INPUT_BYTES
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_MAX_INPUT_BYTES
    return max(0, n)


class _UsageError(Exception):
    """A failure of the invocation, exit 2.

    Raised where the invocation is read: a path that will not open, a
    ``--schema`` file that is not JSON. That keeps ``main`` free of a blanket
    ``except OSError``, which would report a ``BrokenPipeError`` from a
    ``sys.stdout.write`` as a usage error. The message renders verbatim after
    ``error: ``, so callers embed the exception's own class name where it
    helps (``FileNotFoundError: …``).
    """


class _InputTooLargeError(_UsageError):
    """Raised when stdin or a --schema/file payload exceeds the byte ceiling."""


def _add_io_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "file", nargs="?", default="-",
        help="Path to .kql file. Use '-' or omit to read from stdin.",
    )


def _read_capped(stream, limit: int, source: str) -> str:
    """Read up to ``limit`` **bytes** from ``stream``, then decode as UTF-8.

    The ceiling has to mean bytes: a decoded text stream's ``read(n)`` counts
    *characters*, which lets a multibyte payload occupy several times the
    ceiling in memory. The read goes through ``stream.buffer``, the undecoded
    byte stream behind ``sys.stdin``; a stream with no ``.buffer`` is already
    binary, which is how the file paths open it.

    Reads ``limit + 1`` to tell "exactly limit bytes" from "overflowed". The
    +1 is bounded.
    """
    raw = getattr(stream, "buffer", stream)
    data = raw.read(limit + 1)
    if isinstance(data, str):
        # An embedder calling `main()` in-process with `sys.stdin` set to a
        # `StringIO` lands here: no `.buffer`, and `read` hands back text.
        # Measuring its length would make the ceiling count characters, and
        # `.decode` would then fail with a bare `AttributeError`.
        raise TypeError(
            f"{source} is a decoded text stream with no .buffer, so the "
            f"{limit}-byte input ceiling cannot be enforced over it. Pass a "
            "binary stream, or an io.TextIOWrapper, which has one."
        )
    if len(data) > limit:
        raise _InputTooLargeError(
            f"{source} exceeded the {limit}-byte input ceiling "
            "(override via KUSTOLOGY_MAX_INPUT_BYTES)."
        )
    return data.decode("utf-8")


def _read_input(args: argparse.Namespace) -> str:
    limit = _max_input_bytes()
    try:
        if args.file in (None, "-"):
            return _read_capped(sys.stdin, limit, "stdin")
        with open(args.file, "rb") as f:
            return _read_capped(f, limit, args.file)
    except OSError as e:
        # Scoped to the read: a missing path, a directory, a permission
        # denial. Classifying OSError here keeps writes out of the net.
        raise _UsageError(f"{type(e).__name__}: {e}") from e


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kustology",
        description="KQL parser, formatter, and validator (CLI for kustology).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="Print kustology version and exit.")

    format_p = subparsers.add_parser(
        "format", help="Reformat a KQL query into canonical form.",
    )
    _add_io_arguments(format_p)

    validate_p = subparsers.add_parser(
        "validate", help="Print parser diagnostics for a KQL query.",
    )
    _add_io_arguments(validate_p)
    validate_p.add_argument(
        "--json", action="store_true",
        help="Emit diagnostics as JSON array instead of human-readable text.",
    )
    validate_p.add_argument(
        "--ignore-unknown-tables", action="store_true",
        help="Suppress 'table not found' (KS204) diagnostics.",
    )
    validate_p.add_argument(
        "--schema", metavar="PATH",
        help="Path to a JSON schema file ({table: {column: type}}) for binder lookup.",
    )

    parse_p = subparsers.add_parser(
        "parse", help="Parse a KQL query and print its AST or IR.",
    )
    _add_io_arguments(parse_p)
    mode = parse_p.add_mutually_exclusive_group()
    mode.add_argument(
        "--ast", action="store_const", const="ast", dest="mode",
        help="Emit the raw .NET syntax tree (default; works on base install).",
    )
    mode.add_argument(
        "--ir", action="store_const", const="ir", dest="mode",
        help="Emit the pydantic IR (requires the [ir] extras).",
    )
    parse_p.set_defaults(mode="ast")
    parse_p.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parse_p.add_argument(
        "--schema", metavar="PATH",
        help="Path to a JSON schema file ({table: {column: type}}). Binds the "
             "parse, which fills the IR's column types and table provenance. "
             "The --ast tree is unaffected by binding.",
    )

    return parser


@contextlib.contextmanager
def _tolerate_broken_pipe():
    """Stop emitting when the reader goes away, without losing the verdict.

    A broken pipe means the *reader* stopped listening, which says nothing
    about whether the input was valid. For ``validate`` the validity verdict
    *is* the exit code, so swallowing it turns
    ``kustology validate q.kql | head`` in CI into a pass on a query that
    fails validation.

    Each command therefore computes its exit code **before** it writes and
    wraps only the writing in this guard: the emit stops here, stdout is
    redirected so the interpreter's shutdown flush stays silent, and the
    caller falls through to its own ``return rc``. The flush sits inside the
    guard because a pipe write is buffered; a departed reader is not
    discovered until the buffer drains, and leaving that to interpreter
    shutdown puts it beyond every handler in this module.

    ``main`` keeps a ``BrokenPipeError`` arm as the last resort for anything
    that escapes a guard, where returning 0 is right because a command that
    never computed a code was still on the success path.
    """
    try:
        yield
        sys.stdout.flush()
    except BrokenPipeError:
        _silence_broken_stdout()


def _cmd_version() -> int:
    with _tolerate_broken_pipe():
        print(f"kustology {__version__}")
    return 0


def _format_diagnostic(d: dict) -> str:
    severity = d.get("severity", "?")
    code = d.get("code") or ""
    code_str = f"[{code}]" if code else ""
    start = d.get("start", 0)
    length = d.get("length", 0)
    return f"{start}+{length} {severity}{code_str} {d.get('message', '')}\n"


def _report_error_diagnostics(body: str) -> bool:
    """Write any Error-severity diagnostics to stderr; True if there were any.

    ``format`` and ``parse`` both derive their output from the parse tree, so
    emitting it for input the parser rejected hands the caller something that
    looks like a result: the formatter returns ``'T | where '`` for the
    truncated ``T | where``, and a shell redirect writes that to a file. The
    gate is unbound, parser diagnostics only. A table the schema does not
    describe is a schema gap, and ``validate`` is the subcommand for asking
    about that.
    """
    errors = [d for d in validate(body) if d.get("severity") == "Error"]
    # Same rule as stdout, on the other stream: the verdict is decided, so a
    # reader that hangs up mid-report stops the report and nothing else. Under
    # `kustology format bad.kql 2>&1 | head` these lines fill the pipe.
    with contextlib.suppress(BrokenPipeError):
        for d in errors:
            sys.stderr.write(_format_diagnostic(d))
    return bool(errors)


def _cmd_format(args: argparse.Namespace) -> int:
    body = _read_input(args)
    if _report_error_diagnostics(body):
        return 1
    formatted = format_query(body)
    with _tolerate_broken_pipe():
        sys.stdout.write(formatted)
        if not body.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _load_schema(path: str | None) -> dict | None:
    if not path:
        return None
    limit = _max_input_bytes()
    try:
        with open(path, "rb") as f:
            body = _read_capped(f, limit, path)
    except OSError as e:
        raise _UsageError(f"{type(e).__name__}: {e}") from e
    try:
        return _json.loads(body)
    except _json.JSONDecodeError as e:
        raise _UsageError(f"JSONDecodeError: {e}") from e


def _cmd_validate(args: argparse.Namespace) -> int:
    body = _read_input(args)
    schema = _load_schema(args.schema)
    diags = validate(
        body,
        schema=schema,
        ignore_unknown_tables=args.ignore_unknown_tables,
    )
    # The verdict is decided before a byte is written, so a reader that hangs
    # up mid-emit cannot turn a failing query into a passing exit code.
    rc = 1 if any(d.get("severity") == "Error" for d in diags) else 0
    with _tolerate_broken_pipe():
        if args.json:
            sys.stdout.write(_json.dumps(diags, indent=2))
            sys.stdout.write("\n")
        else:
            for d in diags:
                sys.stdout.write(_format_diagnostic(d))
    return rc


def _ast_dict_to_text(node: dict, indent: int = 0) -> str:
    """Render a :func:`~kustology.utils.walker.node_to_dict` tree as text.

    Both ``parse`` emitters — this one and the ``--json`` dump — render the
    library's dict, so the JSON and the text form describe the same tree by
    construction and the depth cap is enforced once, in the walker.
    """
    label = node["kind"]
    text = node["text"]
    if node.get("truncated"):
        return " " * indent + f"{label}  (truncated at depth {MAX_AST_DEPTH})\n"
    out = " " * indent + (f"{label}  {text!r}\n" if text else label + "\n")
    for child in node["children"]:
        out += _ast_dict_to_text(child, indent + 2)
    return out


def _cmd_parse(args: argparse.Namespace) -> int:
    body = _read_input(args)
    schema = _load_schema(args.schema)
    if _report_error_diagnostics(body):
        return 1

    if args.mode == "ir":
        try:
            from .ir import IR_SCHEMA_VERSION, SEMANTIC_HASH_SCHEME
        except ImportError as e:
            sys.stderr.write(
                "kustology parse --ir requires the [ir] extras (pydantic). "
                "Install with: pip install 'kustology[ir]'\n"
            )
            sys.stderr.write(f"({e})\n")
            return 2
        # `parse().to_ir()` is what makes `--schema` mean anything here:
        # `to_ir()` auto-attaches the schema on a bound parse, so the IR
        # carries column types and table provenance.
        ir = parse(body, schema=schema).to_ir()
        if args.json:
            # Both version tags are the consumer's compatibility contract: a
            # stored payload naming neither cannot be checked against the IR
            # shape that produced it.
            payload = {
                "ir_schema_version": IR_SCHEMA_VERSION,
                "semantic_hash_scheme": SEMANTIC_HASH_SCHEME,
                "ir": _json.loads(ir.model_dump_json()),
            }
            rendered = _json.dumps(payload, indent=2) + "\n"
        else:
            rendered = repr(ir) + "\n"
        with _tolerate_broken_pipe():
            sys.stdout.write(rendered)
        return 0

    tree = node_to_dict(parse(body, schema=schema).syntax)
    rendered = (
        _json.dumps(tree, indent=2) + "\n" if args.json else _ast_dict_to_text(tree)
    )
    with _tolerate_broken_pipe():
        sys.stdout.write(rendered)
    return 0


def _silence_broken_stdout() -> None:
    """Point stdout somewhere harmless after the downstream reader went away.

    CPython flushes ``sys.stdout`` once more at interpreter shutdown, and on a
    dead pipe that second failure prints ``Exception ignored … Broken pipe``
    to stderr after ``main`` has returned, so a caller that handled the pipe
    still gets noise on a run it considers clean. Redirecting the file
    *descriptor* is the stdlib's own recipe (the note on SIGPIPE in the
    ``signal`` docs). When stdout has no descriptor to redirect, such as a
    ``capsys`` buffer or a test stub, rebinding the name is the closest
    equivalent.
    """
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 — replaces a dead stream
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def main(argv: list[str] | None = None) -> int:
    """Run the ``kustology`` command line and return its exit code."""
    # KQL is arbitrary Unicode and this CLI emits UTF-8 on every platform. A
    # Windows console otherwise attaches `sys.stdout` with the OS charmap
    # codepage, which cannot encode most of Unicode, so `kustology
    # format`/`parse` on a query containing Japanese dies with a
    # `UnicodeEncodeError`. `reconfigure` lives only on `io.TextIOWrapper`,
    # which a test double standing in for stdout/stderr may lack, so the
    # `getattr` guard skips those; `ValueError`/`OSError` cover a stream that
    # is closed or has a detached buffer, and either way the stream keeps the
    # encoding it had. This runs before any command writes a byte and only
    # changes how bytes are encoded, never which file descriptor is open or
    # which object `sys.stdout`/`sys.stderr` name, so it cannot disturb the
    # broken-pipe handling.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # closed or exotic stream
                pass

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            rc = _cmd_version()
        elif args.command == "format":
            rc = _cmd_format(args)
        elif args.command == "validate":
            rc = _cmd_validate(args)
        elif args.command == "parse":
            rc = _cmd_parse(args)
        else:
            parser.error(f"unknown command: {args.command!r}")
            rc = 2  # unreachable; parser.error raises SystemExit
        # Backstop flush for anything written outside a command's own
        # `_tolerate_broken_pipe` block. A pipe write is buffered, so a
        # departed reader is not discovered until the buffer drains, and a
        # drain at interpreter shutdown lands outside every handler here and
        # becomes an unclassifiable traceback.
        #
        # The handler is local rather than the `BrokenPipeError` arm below,
        # because `rc` is decided by this line and that arm returns 0: a pipe
        # breaking *here* would turn `kustology validate bad.kql | head` into
        # a pass. Every stdout write in this module sits inside a guard that
        # flushes on the way out, so this flush finds an empty buffer and
        # cannot raise; the handler keeps that an implementation detail and
        # makes the arm below's "no code was ever decided" true.
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            _silence_broken_stdout()
        return rc
    except SystemExit:
        raise
    except BrokenPipeError:
        # Last resort for a pipe that broke where no command guarded. Each
        # command wraps its own writing in `_tolerate_broken_pipe` and returns
        # the code it already decided, so `validate | head` keeps reporting 1
        # on a query that fails validation. Reaching *here* means no code was
        # ever decided, which only happens on the success path.
        _silence_broken_stdout()
        return 0
    except _UsageError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
