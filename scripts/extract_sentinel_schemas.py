#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Extract a {table: {column: type}} schema dict from a Sentinel
reference markdown file.

The markdown is expected to use the following per-table layout:

    ### `TableName`
    ...
    **Key Columns:**
    | Column | Type | Description |
    | `ColumnName` | type | description |
    | `ColumnName` | type | description |
    ...

Output is JSON written to --output (default tests/fixtures/sentinel_schemas.json,
which is gitignored — see .gitignore).

The committed code here does NOT reference any external repository path.
Pass --reference-md to point it at the source markdown on your machine.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "tests/fixtures/sentinel_schemas.json"

TABLE_HEADER = re.compile(r"^###\s+`([A-Za-z0-9_]+)`")
COLUMN_ROW = re.compile(r"^\|\s*`([A-Za-z0-9_]+)`\s*\|\s*([A-Za-z0-9_]+)\s*\|")
KEY_COLUMNS_MARKER = "**Key Columns:**"


def parse_reference(md_path: Path) -> dict[str, dict[str, str]]:
    schemas: dict[str, dict[str, str]] = {}
    current_table: str | None = None
    in_key_columns: bool = False
    for line in md_path.read_text(encoding="utf-8").splitlines():
        header = TABLE_HEADER.match(line)
        if header:
            current_table = header.group(1)
            schemas[current_table] = {}
            in_key_columns = False
            continue
        if current_table is None:
            continue
        if line.strip() == KEY_COLUMNS_MARKER:
            in_key_columns = True
            continue
        if in_key_columns:
            col = COLUMN_ROW.match(line)
            if col:
                col_name, col_type = col.group(1), col.group(2)
                if col_name.lower() == "column":
                    continue
                schemas[current_table][col_name] = col_type
            elif line.lstrip().startswith("**") and line.strip() != KEY_COLUMNS_MARKER:
                # Next bold section (e.g. **Detection Use Cases:**) ends the block.
                in_key_columns = False
    return schemas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-md", type=Path, required=True,
                        help="Path to a Sentinel reference markdown file")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.reference_md.exists():
        print(f"error: reference markdown not found at {args.reference_md}",
              file=sys.stderr)
        return 1

    schemas = parse_reference(args.reference_md)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schemas, indent=2, sort_keys=True),
                           encoding="utf-8")

    n_tables = len(schemas)
    n_cols = sum(len(cols) for cols in schemas.values())
    print(f"wrote {n_tables} tables ({n_cols} columns) to {args.output}")
    if n_tables == 0:
        print("warning: no tables parsed — check markdown format",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
