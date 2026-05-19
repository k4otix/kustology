# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Command-line interface for kustology.

Subcommands: version, format, validate, parse.

Exit codes:
  0 — success.
  1 — input had errors (parse failure, Error-severity diagnostic).
  2 — usage error (bad flags, missing file, missing optional extra).
"""
from __future__ import annotations

import argparse
import json as _json
import sys

from . import __version__
from .services import format_query, parse, validate


def _add_io_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "file", nargs="?", default="-",
        help="Path to .kql file. Use '-' or omit to read from stdin.",
    )


def _read_input(args: argparse.Namespace) -> str:
    if args.file in (None, "-"):
        return sys.stdin.read()
    with open(args.file, encoding="utf-8") as f:
        return f.read()


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

    return parser


def _cmd_version() -> int:
    print(f"kustology {__version__}")
    return 0


def _cmd_format(args: argparse.Namespace) -> int:
    body = _read_input(args)
    sys.stdout.write(format_query(body))
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _load_schema(path: str | None) -> dict | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return _json.load(f)


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
            severity = d.get("severity", "?")
            code = d.get("code") or ""
            start = d.get("start", 0)
            length = d.get("length", 0)
            msg = d.get("message", "")
            code_str = f"[{code}]" if code else ""
            sys.stdout.write(f"{start}+{length} {severity}{code_str} {msg}\n")
    has_error = any(d.get("severity") == "Error" for d in diags)
    return 1 if has_error else 0


def _ast_to_dict(node) -> dict:
    """Recursively convert a .NET syntax node into a {kind, text, children} dict."""
    children = []
    # AttributeError on leaf nodes lacking ChildCount/GetChild is expected.
    try:
        cc = node.ChildCount
        for i in range(cc):
            c = node.GetChild(i)
            if c is not None:
                children.append(_ast_to_dict(c))
    except AttributeError:
        pass
    return {
        "kind": type(node).__name__,
        "text": str(node.ToString()),
        "children": children,
    }


def _ast_to_text(node, indent: int = 0) -> str:
    try:
        text = str(node.ToString()).strip()
    except AttributeError:
        text = ""
    label = type(node).__name__
    if text:
        out = " " * indent + f"{label}  {text!r}\n"
    else:
        out = " " * indent + label + "\n"
    try:
        cc = node.ChildCount
        for i in range(cc):
            c = node.GetChild(i)
            if c is not None:
                out += _ast_to_text(c, indent + 2)
    except AttributeError:
        pass
    return out


def _cmd_parse(args: argparse.Namespace) -> int:
    body = _read_input(args)

    if args.mode == "ir":
        try:
            from .ir import IRBuilder
        except ImportError as e:
            sys.stderr.write(
                "kustology parse --ir requires the [ir] extras (pydantic). "
                "Install with: pip install 'kustology[ir]'\n"
            )
            sys.stderr.write(f"({e})\n")
            return 2
        ir = IRBuilder().build(body)
        if args.json:
            sys.stdout.write(ir.model_dump_json(indent=2))
            sys.stdout.write("\n")
        else:
            sys.stdout.write(repr(ir))
            sys.stdout.write("\n")
        return 0

    q = parse(body)
    root = q.syntax
    if args.json:
        sys.stdout.write(_json.dumps(_ast_to_dict(root), indent=2))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_ast_to_text(root))
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
    except Exception as e:
        sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
