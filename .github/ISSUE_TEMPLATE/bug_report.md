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
from kustology import parse
result = parse("...")
```

**Expected vs. actual**
What you expected to happen, and what happened instead.

**Environment**
- kustology version:
- Python version:
- OS / arch:
- .NET runtime version (`dotnet --info`):
- Bundled DLL (`cat src/kustology/bin/VERSION.txt`):
