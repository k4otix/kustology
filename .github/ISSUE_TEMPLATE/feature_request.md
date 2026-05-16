---
name: Feature request
about: Suggest a new capability or analyzer
labels: enhancement
---

**Use case**
What problem are you trying to solve? Be concrete: a specific KQL query
shape or a specific consumer of the parsed AST.

**Proposed API**
If you have a shape in mind:

```python
result = parse(...)
result.<your_method>(...)
```

**Alternatives considered**
Why doesn't an existing analyzer (`get_referenced_tables`,
`get_operator_chain`, etc.) cover this?
