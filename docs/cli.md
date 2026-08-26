# CLI

The `kustology` console script ships with the base install and covers formatting, validation, and parsing from a shell or a CI step.

## Commands

### version

Prints the package version and exits. It takes no file argument.

```bash
kustology version
```

### format

Reformats a KQL query into canonical form.

```bash
kustology format query.kql
```

`format` runs the validator before it prints anything. See [Exit codes](#exit-codes) for what happens when the input fails validation.

### validate

Prints parser diagnostics for a query.

```bash
kustology validate query.kql                   # print parser diagnostics
kustology validate --json query.kql            # diagnostics as JSON
kustology validate --schema s.json query.kql   # bind first: semantic diagnostics too
kustology validate --schema s.json \
                   --ignore-unknown-tables query.kql   # waive KS204 only
```

- `--json` emits the diagnostics as a JSON array instead of text.
- `--schema` binds the parse against a schema file, so `validate` also reports semantic diagnostics. See [Schema files](#schema-files).
- `--ignore-unknown-tables` suppresses the "table not found" diagnostic (KS204) only. Other diagnostics still report.

### parse

Prints a query's syntax tree or IR.

```bash
kustology parse query.kql                      # print the .NET AST
kustology parse --json query.kql               # the AST as JSON
kustology parse --ir query.kql                 # print the Pydantic IR (needs [ir])
kustology parse --ir --json query.kql          # the IR as JSON, in an envelope
kustology parse --ir --schema s.json query.kql # enriched IR: types + provenance
```

- The default output is Microsoft's .NET syntax tree (the `--ast` flag selects the same output explicitly). See [the Tier 1 syntax tree](tier1-syntax-tree.md).
- `--ir` prints [the Tier 2 IR](tier2-ir.md) instead. It requires the `[ir]` extra; without it, the command exits 2.
- `--json` emits JSON instead of human-readable text, for either `--ast` or `--ir`.
- `--schema` binds the parse. See [Schema files](#schema-files) for what that changes.

`parse` also runs the validator before it prints anything, the same as `format`.

## Schema files

A `--schema` file is JSON in the shape `parse(query, schema=...)` takes: `{"Table": {"column": "type"}}`.

On `validate` and `parse`, a schema file binds the parse. On `parse --ir`, `to_ir()` auto-attaches the schema from a bound parse, so the IR carries column types, table provenance, and `"schema_attached": true` instead of an unenriched skeleton.

`parse --ast` also accepts `--schema`. Binding does not change the syntax tree it emits.

## JSON output and the envelope

`parse --ir --json` wraps the IR in an envelope that names its schema version and hash scheme:

```json
{
  "ir_schema_version": "0.2",
  "semantic_hash_scheme": "kustology-sem-v2",
  "ir": { "kind": "query", "...": "..." }
}
```

Both tags are part of the IR's compatibility contract. They let you check a stored payload against the IR shape that produced it. See [semantic-hash.md](semantic-hash.md) for what each tag covers and when it changes.

## Input and limits

`format`, `validate`, and `parse` read the query from the `file` argument. Pass `-`, or omit the argument, to read from stdin. `version` takes no file.

Input is capped at 10 MB. Set `KUSTOLOGY_MAX_INPUT_BYTES` to override the cap. The cap counts bytes, not characters.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The input had Error-severity diagnostics, or the command failed at runtime. |
| `2` | The invocation was wrong: bad flags, a file that cannot be read, a `--schema` file that is not JSON, input over the byte cap, or `parse --ir` without the `[ir]` extra. |

Code 1 means the query is wrong. Code 2 means the command is wrong. A CI job can branch on this distinction: an unreadable path or a malformed `--schema` file says nothing about the KQL itself.

`format` and `parse` both run the validator before they emit anything. If the input has Error-severity diagnostics, neither command writes output derived from the rejected parse. The diagnostics go to stderr, stdout stays empty, and the command exits 1.

### Broken pipes

Each subcommand computes its exit code before it writes output, so a reader that stops early does not change the result. For example, `kustology validate q.kql | head` still exits `1` on a query that fails validation, even though `head` closes the pipe before the diagnostics finish printing.

Only a pipe that breaks before any exit code is computed results in exit `0`. Either way, the interpreter's shutdown flush is silenced, so no `Exception ignored ... Broken pipe` message follows the command.
