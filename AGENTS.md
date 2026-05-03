# AI Agent Intelligence: KQL Wrapper Guidelines

This document provides foundational technical context and mandatory patterns for agents modifying this repository. It captures critical structural realizations and .NET interop requirements discovered during implementation.

## .NET Runtime and pythonnet Interop

### Observation: The Stubborn Type Impasse
Certain static classes in the `Kusto.Language.Editor` namespace (e.g., `KustoFormatter`) are visible to the CLR but fail to map into the Python namespace via standard import mechanisms.
- **Rule**: Do not attempt direct imports for these types. 
- **Mandate**: Utilize the reflection-based fallback in `bridge.py`. Access these types via the internal `_KustoFormatter` reference and use the `call_static` helper.

### Observation: Type Conversion and Signatures
Pythonnet's implicit conversion can fail on specific .NET signatures, particularly with numeric types and nulls.
- **Rule**: Reflection-based calls require explicit type casting for .NET parameters.
- **Mandate**: When using `call_static`, wrap Python integers in `System.Int32()` and explicitly handle nulls to match the .NET method signature exactly.

### Observation: CoreCLR Initialization
On non-Windows platforms, pythonnet may default to searching for a Mono runtime, which leads to initialization failures.
- **Mandate**: Always call `pythonnet.load("coreclr")` explicitly before importing `clr`. This logic is centralized in `bridge.py`.

## AST Structure and Navigation

### Observation: Left-Associative Binary Trees
The Kusto AST is structured as a left-associative binary tree. A pipe chain (e.g., `A | B | C`) is nested as `PipeExpression(PipeExpression(A, B), C)`.
- **Context**: The primary data source is often the deepest leftmost leaf.
- **Mandate**: When extracting sources, recursively traverse `GetChild(0)` until a `NameReference` or similar terminal node is reached.

### Observation: Structural vs. Semantic Nodes
The AST contains numerous structural wrappers (e.g., `SeparatedElement`, `SyntaxList`, `ExpressionStatement`) that do not carry logical weight but must be traversed.
- **Mandate**: Use the `walk_tree.py` example to map the specific path to semantic nodes before implementing new analysis logic.

### Observation: Node Property Access
Nodes expose both semantic properties (e.g., `node.Condition`) and raw index-based slots (e.g., `node.GetChild(1)`). 
- **Rule**: .NET properties accessed via pythonnet may return types that require explicit string conversion.
- **Mandate**: Always use `.ToString()` for text extraction from .NET nodes to bypass pythonnet's implicit conversion limits.

### Observation: PipeExpression Child Indices
In a `PipeExpression` node, children are indexed as follows:
- `GetChild(0)`: The left-hand expression (previous part of the pipeline).
- `GetChild(1)`: The `|` token (`BarToken`).
- `GetChild(2)`: The right-hand operator (e.g., `FilterOperator`, `SummarizeOperator`).
- **Mandate**: When flattening the pipeline into an operator chain, always target index 2 for the semantic operator.

## Analysis Logic

### Observation: Structural Hashing
The library provides a `get_structural_hash()` utility that generates a SHA256 fingerprint based on node kinds while ignoring literal values and whitespace.
- **Rule**: This is intended for query deduplication and templating.

### Observation: AST-based Table Replacement
The `replace_table()` utility performs surgical renaming by identifying `NameReference` nodes in source positions and replacing their source text based on exact offsets.
- **Mandate**: This method is preferred over string regex replacement as it preserves the AST integrity and avoids accidental keyword replacement.

### Observation: Table vs. Variable Distinction
A `NameReference` node may refer to a physical table or a local variable defined in a `let` statement.
- **Rule**: Syntactic analysis must be stateful.
- **Mandate**: Track names defined in `LetStatement` nodes during traversal. Exclude these names from primary data source extraction results.

### Observation: Source Offsets
AST nodes use `TextStart` and `Width` for positioning within the source text, not `Start` or `Length`.
- **Mandate**: Use `node.TextStart` and `node.Width` for all offset-based calculations. When performing text replacements (e.g., masking), process the source from back to front to maintain offset validity.
