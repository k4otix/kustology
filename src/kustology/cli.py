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

The distinction between 1 and 2 is "your query is wrong" versus "your
invocation is wrong", and it is what a CI job branches on: an unreadable
path or an unparseable ``--schema`` says nothing about the KQL. Both
``format`` and ``parse`` run the validator before they emit anything, so
neither writes output derived from a query the parser rejected.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import sys

from . import __version__
from .services import format_query, parse, validate
from .utils.walker import MAX_AST_DEPTH, node_to_dict

# Bound the bytes we'll read from stdin or a file. KQL queries are not large;
# a 10 MB ceiling means a deliberately oversized payload (CI webhook abuse,
# misrouted log dump) fails fast instead of OOM-ing the host. Override with
# ``KUSTOLOGY_MAX_INPUT_BYTES`` for legitimate edge cases.
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


class _InputTooLargeError(Exception):
    """Raised when stdin or a --schema/file payload exceeds the byte ceiling."""


def _add_io_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "file", nargs="?", default="-",
        help="Path to .kql file. Use '-' or omit to read from stdin.",
    )


def _read_capped(stream, limit: int, source: str) -> str:
    """Read up to ``limit`` **bytes** from ``stream``, then decode as UTF-8.

    The ceiling is named in bytes and has to mean bytes: a decoded text
    stream's ``read(n)`` counts *characters*, so measuring it lets a
    multibyte payload occupy several times the ceiling in memory — which is
    the one thing the ceiling exists to prevent. We therefore read through
    ``stream.buffer``, the undecoded byte stream behind ``sys.stdin``, and
    treat a stream that has no ``.buffer`` as already binary (that is how
    the file paths open it).

    Reads ``limit + 1`` so we can distinguish "exactly limit bytes" from
    "overflowed." The +1 is bounded, not the entire stream.
    """
    raw = getattr(stream, "buffer", stream)
    data: bytes = raw.read(limit + 1)
    if len(data) > limit:
        raise _InputTooLargeError(
            f"{source} exceeded the {limit}-byte input ceiling "
            "(override via KUSTOLOGY_MAX_INPUT_BYTES)."
        )
    return data.decode("utf-8")


def _read_input(args: argparse.Namespace) -> str:
    limit = _max_input_bytes()
    if args.file in (None, "-"):
        return _read_capped(sys.stdin, limit, "stdin")
    with open(args.file, "rb") as f:
        return _read_capped(f, limit, args.file)


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


def _cmd_version() -> int:
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
    looks like a result and is not one — the formatter returns ``'T | where '``
    for the truncated ``T | where``, and a shell redirect writes that to a
    file. The gate is unbound (parser diagnostics only): a table the schema
    does not describe is a schema gap, not a malformed query, and ``validate``
    is the subcommand for asking about that.
    """
    errors = [d for d in validate(body) if d.get("severity") == "Error"]
    for d in errors:
        sys.stderr.write(_format_diagnostic(d))
    return bool(errors)


def _cmd_format(args: argparse.Namespace) -> int:
    body = _read_input(args)
    if _report_error_diagnostics(body):
        return 1
    sys.stdout.write(format_query(body))
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _load_schema(path: str | None) -> dict | None:
    if not path:
        return None
    limit = _max_input_bytes()
    with open(path, "rb") as f:
        body = _read_capped(f, limit, path)
    return _json.loads(body)


def _cmd_validate(args: argparse.Namespace) -> int:
    body = _read_input(args)
    schema = _load_schema(args.schema)
    diags = validate(
        body,
        schema=schema,
        ignore_unknown_tables=args.ignore_unknown_tables,
    )
    if args.json:
        sys.stdout.write(_json.dumps(diags, indent=2))
        sys.stdout.write("\n")
    else:
        for d in diags:
            sys.stdout.write(_format_diagnostic(d))
    has_error = any(d.get("severity") == "Error" for d in diags)
    return 1 if has_error else 0


def _ast_dict_to_text(node: dict, indent: int = 0) -> str:
    """Render a :func:`~kustology.utils.walker.node_to_dict` tree as text.

    The CLI used to carry its own ``_ast_to_dict`` / ``_ast_to_text`` pair
    alongside the library's serializer. Three copies of one traversal meant
    three depth caps to keep honest, and they had already drifted: the CLI's
    dict left each node's leading trivia in ``text`` where the library's
    stripped it. Both emitters now render the library's dict, so the JSON and
    the text form describe the same tree by construction and the depth cap is
    enforced once, in the walker.
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
        # Going through `parse().to_ir()` rather than `IRBuilder().build()`
        # is what makes `--schema` mean anything here: `to_ir()` auto-attaches
        # the schema on a bound parse, so the IR carries column types and
        # table provenance instead of an unenriched skeleton.
        ir = parse(body, schema=schema).to_ir()
        if args.json:
            # Envelope, not the bare dump: both version tags are the
            # consumer's compatibility contract, and a stored payload that
            # names neither cannot be checked against the IR shape that
            # produced it.
            payload = {
                "ir_schema_version": IR_SCHEMA_VERSION,
                "semantic_hash_scheme": SEMANTIC_HASH_SCHEME,
                "ir": _json.loads(ir.model_dump_json()),
            }
            sys.stdout.write(_json.dumps(payload, indent=2))
            sys.stdout.write("\n")
        else:
            sys.stdout.write(repr(ir))
            sys.stdout.write("\n")
        return 0

    tree = node_to_dict(parse(body, schema=schema).syntax)
    if args.json:
        sys.stdout.write(_json.dumps(tree, indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_ast_dict_to_text(tree))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            return _cmd_version()
        if args.command == "format":
            return _cmd_format(args)
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "parse":
            return _cmd_parse(args)
        parser.error(f"unknown command: {args.command!r}")
        return 2  # unreachable; parser.error raises SystemExit
    except SystemExit:
        raise
    except _InputTooLargeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    except (OSError, _json.JSONDecodeError) as e:
        # Both are failures of the *invocation*: a path we could not open,
        # or a --schema file that is not JSON. Neither says anything about
        # the KQL, so neither may borrow exit 1 from the query that was
        # never read. FileNotFoundError used to land in the handler below
        # and report 1.
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 2
    except Exception as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
