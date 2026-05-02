# pykusto-language

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![.NET 10.0+](https://img.shields.io/badge/.NET-10.0+-purple.svg)](https://dotnet.microsoft.com/download/dotnet/10.0)

`pykusto-language` is a high-fidelity Python wrapper for the official **Microsoft.Kusto.Language** C# library. It enables robust KQL (Kusto Query Language) parsing, syntax validation, and Abstract Syntax Tree (AST) analysis directly in Python.

Unlike regex-based or partial Python implementations, this library utilizes the official .NET engine via `pythonnet` to ensure 100% parity with the parser used by Azure Data Explorer, Azure Monitor, and Microsoft Sentinel.

## Features

* **Full Syntax Support:** Complete KQL language coverage including the latest operators and functions.
* **AST Analysis:** Deep inspection of query structure for detection engineering, linting, and transpilation.
* **Diagnostics:** Access to high-quality compiler-grade error messages and syntax warnings.
* **Cross-Platform:** Developed to run seamlessly on Windows, Linux, and macOS.
* **Type Fidelity:** Direct access to the underlying .NET object model.

## Prerequisites

* **Python 3.10+**
* **[.NET Runtime 10.0+](https://dotnet.microsoft.com/download/dotnet/10.0)**: The library requires the .NET runtime to host the C# parsing engine.

## Installation

```bash
pip install pykusto-language
```

## Usage

**Basic Parsing and Validation**

```python
from pykusto_language.parser import parse_query

query = "StormEvents | where State == 'TEXAS' | count"
result = parse_query(query)

# Check for syntax errors (Diagnostics)
diagnostics = result.GetDiagnostics()
if len(diagnostics) > 0:
    for diag in diagnostics:
        print(f"[{diag.Severity}] Line {diag.Start}: {diag.Message}")
else:
    print("Query is syntactically valid.")
```

**Accessing the Syntax Tree**
```python
root = result.Syntax
print(f"Root Node Kind: {root.Kind}") # e.g., QueryStatement

# Accessing children or specific properties
# Note: These properties follow the C# Kusto.Language API naming conventions
statements = root.Items
for stmt in statements:
    print(f"Statement Kind: {stmt.Kind}")
```

**Architecture**

This project acts as a bridge between CPython and the .NET Common Language Runtime (CLR). It bundles a specific version of the `Kusto.Language.dll` compiled from the [official Microsoft repository](https://github.com/microsoft/Kusto-Query-Language).

1. Python Layer: Provides a clean, Pythonic entry point.
2. Bridge (`pythonnet`): Manages the data marshalling between the Python interpreter and the .NET runtime.
3. Engine (`Kusto.Language.dll`): The official Microsoft library that performs the actual lexing and parsing.

## Development

To set up a development environment with all testing tools:

```bash
# Clone the repository
git clone [https://github.com/k4otix/pykusto-language.git](https://github.com/k4otix/pykusto-language.git)
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