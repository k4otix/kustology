# Third-Party Notices

This distribution includes the following third-party components:

## 1. Microsoft.Azure.Kusto.Language

* **Source:** [https://github.com/microsoft/Kusto-Query-Language](https://github.com/microsoft/Kusto-Query-Language)
* **NuGet package:** [Microsoft.Azure.Kusto.Language](https://www.nuget.org/packages/Microsoft.Azure.Kusto.Language)
* **License:** Apache License 2.0
* **Copyright:** Copyright (c) Microsoft Corporation. All rights reserved.
* **Component:** `Kusto.Language.dll` (bundled at `src/kustology/bin/`, version pinned in `bin/VERSION.txt` and `pyproject.toml`)
* **Modifications:** None. The DLL is taken byte-for-byte from the NuGet package's `lib/net6.0/` directory and is redistributed unmodified. Verifiable via `python scripts/verify_dll.py`.
* **Upstream NOTICE file:** The upstream Kusto-Query-Language repository ships a LICENSE file but no NOTICE file. Apache License 2.0 §4(d) requires propagation of any NOTICE file that exists in the original work; since none exists upstream, no additional attribution beyond this section is required.

---

### Apache License 2.0 (for Kusto.Language.dll)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

---

## Trademarks

"Kusto", "KQL", "Microsoft", "Azure Data Explorer", "Azure Monitor", and
"Microsoft Sentinel" are trademarks of Microsoft Corporation. This project is
not affiliated with, endorsed by, or sponsored by Microsoft Corporation.
References to those trademarks are nominative and used only to identify the
upstream library that this package wraps. Apache License 2.0 §6 explicitly
does not grant trademark rights.
