# Security Policy

## Reporting a Vulnerability

Please report security issues privately using GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) feature for this repository:

**[Open a private security advisory](https://github.com/k4otix/kustology/security/advisories/new)**

Do not file public issues for security vulnerabilities.

## Response

This project is maintained on a best-effort basis by a single maintainer. There is no SLA for triage or fixes. I will acknowledge reports when I am able
and prioritize based on severity and exploitability.

## Supported Versions

Only the latest released version receives security fixes. Older versions are not patched.

## Scope

In scope:
- The `kustology` Python package and its public API
- Build, release, and CI workflows in this repository
- The bundled `Kusto.Language.dll` insofar as how this package loads or wraps it

Out of scope:
- Vulnerabilities in upstream `Kusto.Language` itself — report those to [Microsoft](https://msrc.microsoft.com/)
- Vulnerabilities in transitive Python dependencies — report to their respective maintainers

## Verifying the bundled binary

Every release pins `Kusto.Language.dll` to a specific
`Microsoft.Azure.Kusto.Language` NuGet version with a SHA-256 hash. The
pin lives in `src/kustology/bin/VERSION.txt` and `pyproject.toml` under
`[tool.kustology] kusto_language_version`.

Two verification paths, both run in CI on every push:

- **Offline hash check:** `shasum -a 256 src/kustology/bin/Kusto.Language.dll`
  must match the `sha256` field in `bin/VERSION.txt`.
- **NuGet re-fetch:** `python scripts/verify_dll.py` downloads the
  pinned NuGet package, hashes every `Kusto.Language.dll` inside, and
  confirms one is byte-identical to the bundled file. Pure Python — no
  `dotnet` required. This converts "trust the maintainer" into "trust
  Microsoft + you can verify offline."

If your policy requires NuGet's signed-package signature, run
`nuget verify -All <path-to-nupkg>` against a freshly fetched package.

## Refreshing the bundled DLL

If you'd rather use a DLL you fetched yourself:

```bash
python scripts/refresh_dll.py            # uses the pinned version
python scripts/refresh_dll.py --version 12.3.2 --pin
```

This requires `dotnet` 8.0+, runs `dotnet publish` against a temporary
`.csproj`, copies the resulting `Kusto.Language.dll` into `bin/`, and
updates `VERSION.txt`.
