#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan
"""
Verify that the bundled Kusto.Language.dll matches what nuget.org publishes.

This script downloads the Microsoft.Azure.Kusto.Language NuGet package at the
pinned version, extracts every Kusto.Language.dll inside it (one per TFM),
hashes each, and confirms that one of them is byte-identical to the DLL
shipped in src/kustology/bin/.

This converts "trust the maintainer" into "trust Microsoft + you can verify
offline." Run this in CI on every PR, and re-run it locally when you need to
prove provenance to your security team.

Usage:
    python scripts/verify_dll.py             # verify against pinned version
    python scripts/verify_dll.py --version 12.3.2

Exit codes:
    0  bundled DLL matches an exact byte-for-byte copy in the NuGet package
    1  mismatch — the bundled DLL is NOT what nuget.org currently ships
    2  configuration error (no pin, network failure, missing files)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
BIN_DIR = REPO_ROOT / "src" / "kustology" / "bin"
DLL_NAME = "Kusto.Language.dll"
PACKAGE = "Microsoft.Azure.Kusto.Language"

NUGET_FLATCONTAINER = "https://api.nuget.org/v3-flatcontainer"


def read_pinned_version() -> str | None:
    if not PYPROJECT.exists():
        return None
    data = tomllib.loads(PYPROJECT.read_text())
    return data.get("tool", {}).get("kustology", {}).get("kusto_language_version")


def read_version_txt_sha() -> tuple[str | None, str | None]:
    """Return (version, sha256) from bin/VERSION.txt, if present."""
    version_file = BIN_DIR / "VERSION.txt"
    if not version_file.exists():
        return None, None
    data = {}
    for line in version_file.read_text().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data.get("version"), data.get("sha256")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_nupkg(version: str) -> bytes:
    """Download the .nupkg for PACKAGE@version from nuget.org."""
    pkg_lower = PACKAGE.lower()
    url = (
        f"{NUGET_FLATCONTAINER}/{pkg_lower}/{version}/"
        f"{pkg_lower}.{version}.nupkg"
    )
    print(f"  fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"FAIL: nuget.org returned HTTP {e.code} for {PACKAGE} {version}. "
            "Check the version is correct and the package is public."
        ) from e
    except urllib.error.URLError as e:
        raise SystemExit(f"FAIL: network error fetching {url}: {e.reason}") from e


def find_dll_hashes(nupkg_bytes: bytes) -> dict[str, str]:
    """Return {nupkg_internal_path: sha256} for every Kusto.Language.dll in the .nupkg."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(nupkg_bytes)) as z:
        for member in z.namelist():
            if member.endswith(f"/{DLL_NAME}") or member == DLL_NAME:
                with z.open(member) as f:
                    out[member] = sha256_of(f.read())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        help=f"Override the version of {PACKAGE} to verify against. "
             "Defaults to the pin in pyproject.toml.",
    )
    args = parser.parse_args()

    version = args.version or read_pinned_version()
    if not version:
        print(
            "FAIL: no version pin found. "
            "Set [tool.kustology] kusto_language_version in pyproject.toml "
            "or pass --version.",
            file=sys.stderr,
        )
        return 2

    bundled_path = BIN_DIR / DLL_NAME
    if not bundled_path.exists():
        print(f"FAIL: {bundled_path} does not exist.", file=sys.stderr)
        return 2

    bundled_sha = sha256_of_file(bundled_path)
    print(f"Bundled  {DLL_NAME} : {bundled_sha}")

    pinned_version, pinned_sha = read_version_txt_sha()
    if pinned_sha and pinned_sha != bundled_sha:
        print(
            f"FAIL: bundled DLL hash does not match bin/VERSION.txt.\n"
            f"  bundled  : {bundled_sha}\n"
            f"  pin says : {pinned_sha}",
            file=sys.stderr,
        )
        return 1
    if pinned_version and pinned_version != version:
        print(
            f"WARN: bin/VERSION.txt records version {pinned_version!r} "
            f"but verifying against {version!r}.",
            file=sys.stderr,
        )

    print(f"Fetching {PACKAGE} {version} from nuget.org...")
    nupkg = fetch_nupkg(version)

    candidates = find_dll_hashes(nupkg)
    if not candidates:
        print(
            f"FAIL: no {DLL_NAME} found inside the NuGet package.",
            file=sys.stderr,
        )
        return 1

    print("NuGet package contents:")
    for path, sha in sorted(candidates.items()):
        match = " <-- match" if sha == bundled_sha else ""
        print(f"  {sha}  {path}{match}")

    if bundled_sha in candidates.values():
        print(
            f"\nOK: bundled {DLL_NAME} is byte-identical to a copy inside "
            f"{PACKAGE} {version} on nuget.org."
        )
        return 0

    print(
        f"\nFAIL: bundled {DLL_NAME} does NOT match any DLL in "
        f"{PACKAGE} {version} on nuget.org. "
        "The bundled binary may have been tampered with or built from "
        "a different version. Re-run scripts/refresh_dll.py to refresh.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
