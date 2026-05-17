# Security Policy

## Reporting a Vulnerability

Please report security issues privately using GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) feature for this repository:

**[Open a private security advisory](https://github.com/k4otix/pykusto-language/security/advisories/new)**

Do not file public issues for security vulnerabilities.

## Response

This project is maintained on a best-effort basis by a single maintainer. There is no SLA for triage or fixes. I will acknowledge reports when I am able
and prioritize based on severity and exploitability.

## Supported Versions

Only the latest released version receives security fixes. Older versions are not patched.

## Scope

In scope:
- The `pykusto-language` Python package and its public API
- Build, release, and CI workflows in this repository
- The bundled `Kusto.Language.dll` insofar as how this package loads or wraps it

Out of scope:
- Vulnerabilities in upstream `Kusto.Language` itself — report those to [Microsoft](https://msrc.microsoft.com/)
- Vulnerabilities in transitive Python dependencies — report to their respective maintainers
