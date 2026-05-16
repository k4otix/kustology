---
name: Bug report
about: Something is broken or behaves unexpectedly
labels: bug
---

**Description**
A clear description of the bug.

**Reproducer**
The smallest KQL input and Python snippet that demonstrates the issue:

```python
from pykusto_language import parse
result = parse("...")
```

**Expected vs. actual**
What you expected to happen, and what happened instead.

**Environment**
- pykusto-language version:
- Python version:
- OS / arch:
- .NET runtime version (`dotnet --info`):
- Bundled DLL (`cat src/pykusto_language/bin/VERSION.txt`):
