# Kustology manual

Reference documentation for people using the library. The
[README](../README.md) covers what Kustology is, how to install it, and a
quickstart. These pages cover the rest.

| Page | Read it when |
| --- | --- |
| [Working with the syntax tree](tier1-syntax-tree.md) | You use Tier 1 and want the full picture of Microsoft's syntax tree, including the pythonnet behavior that catches people out. |
| [The Tier 2 IR](tier2-ir.md) | You write analyzers against the IR and need to know how `let` names, operators, and function bodies lower into nodes. |
| [CLI reference](cli.md) | You run `kustology` from a shell or wire it into CI, and need the flags, the JSON output, or the exit codes. |
| [Versioning and `semantic_hash`](semantic-hash.md) | You store IR JSON or hashes, deduplicate queries, or need to know what the digest treats as equal. |

For the code layout and how to extend the library, see
[ARCHITECTURE.md](../ARCHITECTURE.md). For the development loop, see
[CONTRIBUTING.md](../CONTRIBUTING.md).

Every example referenced in these pages lives in [`examples/`](../examples) and
runs standalone. `tests/test_examples.py` runs all of them, so they stay in step
with the library.
