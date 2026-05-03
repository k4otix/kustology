# pykusto-language

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![.NET 10.0+](https://img.shields.io/badge/.NET-10.0+-purple.svg)](https://dotnet.microsoft.com/download/dotnet/10.0)

`pykusto-language` is a high-fidelity KQL (Kusto Query Language) analysis and automation library for Python. While it is built on a robust .NET 10 bridge to the official **Microsoft.Kusto.Language** engine, it provides an extensive suite of Pythonic utilities for deep AST analysis, structural reasoning, and query transformation.

Unlike regex-based tools, this library ensures 100% parity with the parser used by Azure Data Explorer, Azure Monitor, and Microsoft Sentinel, while offering high-level abstractions for detection engineering and automated query management.

## Key Capabilities

*   **Unified Language Services:** Integrated tools for compiler-grade formatting (`format`), diagnostic validation (`validate`), and AST parsing.
*   **Deep Structural Analysis:** Utilities to flatten complex binary ASTs into linear pipeline chains, enabling easy reasoning about operator flow.
*   **Intelligent Symbol Binding:** Support for both fast syntactic heuristics and rigorous semantic binding using provided schemas to verified table and column symbols.
*   **Query Fingerprinting:** Stable structural hashing that identifies logical patterns while ignoring literals and whitespace—ideal for deduplication and templating.
*   **Advanced Transformations:** Surgical search-and-replace for table references and type-aware literal masking to redact sensitive PII (e.g., strings and regex) for safe logging.
*   **JSON Interop:** Recursive export of the complete AST to Python dictionaries or JSON for use in web frontends or data pipelines.

## Prerequisites

*   **Python 3.10+**
*   **[.NET Runtime 10.0+](https://dotnet.microsoft.com/download/dotnet/10.0)**: Required to host the underlying parsing engine.

## Installation

```bash
pip install pykusto-language
```

## Quick Start

```python
from pykusto_language import parse, format, validate

# 1. Format and Validate
query = "SecurityEvent|where EventID==4624"
print(format(query))

# 2. Deep Analysis
result = parse(query)
print(f"Tables: {result.get_referenced_tables()}")
print(f"Logic Fingerprint: {result.get_structural_hash()}")

# 3. Pipeline Reasoning
for op in result.get_operator_chain():
    print(f"Step: {op.Kind}")
```

## Architecture

This library bridges CPython and the .NET Common Language Runtime (CLR) via `pythonnet`, bundling the official Microsoft `Kusto.Language.dll`.

1.  **Python Layer**: A high-level, Pythonic API for analysis and transformation.
2.  **Bridge**: Managed marshalling and a reflection-based fallback system for robust interop.
3.  **Engine**: The official Microsoft parser ensuring total KQL fidelity.

## Development

To set up a development environment with all testing tools:

```bash
# Clone the repository
git clone https://github.com/k4otix/pykusto-language.git
cd pykusto-language

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run the test suite
pytest
```

**Building the Package**

```bash
python -m build
```

## License

This project is licensed under the Apache License 2.0.

**Third-Party Notices**

This distribution bundles the `Kusto.Language.dll` binary, which is owned by Microsoft Corporation and licensed under the Apache License 2.0. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for full attribution.
