#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan
"""Verify that the bundled Kusto.Language.dll matches what nuget.org publishes.

This script downloads the Microsoft.Azure.Kusto.Language NuGet package at the
pinned version, extracts the Kusto.Language.dll for the pinned TFM
(lib/net6.0/) inside it, hashes it, and confirms it is byte-identical to the
DLL shipped in src/kustology/bin/.

This converts "trust the maintainer" into "trust Microsoft + you can verify
offline." Run this in CI on every PR, and re-run it locally when you need to
prove provenance to your security team.

Usage:
    python scripts/verify_dll.py             # verify against pinned version
    python scripts/verify_dll.py --version 12.3.2
    python scripts/verify_dll.py --offline    # no network; check the local
                                               # pin in bin/VERSION.txt only

Exit codes:
    0  bundled DLL matches an exact byte-for-byte copy in the NuGet package
       (or, under --offline, matches the recorded sha256 pin)
    1  hash mismatch -- the bundled DLL is NOT what it claims to be
    2  configuration or network error (no pin, missing files, nuget.org
       unreachable, unexpected package layout) -- NOT evidence of tampering
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

# Pinned so the byte-identity check is deterministic: a nupkg can carry the
# DLL under several lib/<TFM>/ folders (net6.0, netstandard2.0, ...), and
# comparing against "any of them" would pass even if the specific build we
# bundle drifted from the one this pin names. refresh_dll.py targets the
# same TFM when it fetches, so the two scripts agree on which copy is "the"
# DLL.
TFM = "net6.0"

NUGET_FLATCONTAINER = "https://api.nuget.org/v3-flatcontainer"


class VerifyDLLError(Exception):
    """Base class for verify_dll failures."""


class ConfigError(VerifyDLLError):
    """Local misconfiguration: no pin, missing bundled file, bad package layout.

    Not evidence the bundled DLL is wrong -- the check couldn't run.
    """


class NetworkError(VerifyDLLError):
    """nuget.org could not be reached or returned an unexpected response.

    Not evidence the bundled DLL is wrong -- the check couldn't run.
    """


class HashMismatchError(VerifyDLLError):
    """The bundled DLL's sha256 does not match the expected reference hash.

    The one case that means the bundled binary itself is suspect.
    """


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
        raise NetworkError(
            f"nuget.org returned HTTP {e.code} for {PACKAGE} {version}. "
            "Check the version is correct and the package is public."
        ) from e
    except urllib.error.URLError as e:
        raise NetworkError(f"network error fetching {url}: {e.reason}") from e


def find_dll_hash(nupkg_bytes: bytes) -> str | None:
    """Return the sha256 of lib/<TFM>/Kusto.Language.dll inside the .nupkg.

    Returns None if that exact path isn't present.
    """
    target = f"lib/{TFM}/{DLL_NAME}"
    with zipfile.ZipFile(io.BytesIO(nupkg_bytes)) as z:
        if target not in z.namelist():
            return None
        with z.open(target) as f:
            return sha256_of(f.read())


def _verify_offline() -> int:
    """Check the bundled DLL against the locally recorded pin only.

    Makes no network request -- it cannot detect drift from what
    nuget.org currently ships, only whether the bundled file matches
    its own pin.
    """
    bundled_path = BIN_DIR / DLL_NAME
    if not bundled_path.exists():
        raise ConfigError(f"{bundled_path} does not exist.")

    bundled_sha = sha256_of_file(bundled_path)
    print(f"Bundled  {DLL_NAME} : {bundled_sha}")

    pinned_version, pinned_sha = read_version_txt_sha()
    if not pinned_sha:
        raise ConfigError(
            "bin/VERSION.txt has no sha256 pin to verify against -- "
            "--offline has nothing to check against. Run without --offline, "
            "or run scripts/refresh_dll.py to record a pin."
        )

    if pinned_sha != bundled_sha:
        raise HashMismatchError(
            "bundled DLL hash does not match bin/VERSION.txt.\n"
            f"  bundled  : {bundled_sha}\n"
            f"  pin says : {pinned_sha}"
        )

    print(
        f"OK (offline): bundled {DLL_NAME} matches the recorded sha256 pin "
        f"for {PACKAGE} {pinned_version or '?'} in bin/VERSION.txt."
    )
    return 0


def _verify_online(version_override: str | None) -> int:
    version = version_override or read_pinned_version()
    if not version:
        raise ConfigError(
            "no version pin found. Set [tool.kustology] "
            "kusto_language_version in pyproject.toml or pass --version."
        )

    bundled_path = BIN_DIR / DLL_NAME
    if not bundled_path.exists():
        raise ConfigError(f"{bundled_path} does not exist.")

    bundled_sha = sha256_of_file(bundled_path)
    print(f"Bundled  {DLL_NAME} : {bundled_sha}")

    pinned_version, pinned_sha = read_version_txt_sha()
    if pinned_sha and pinned_sha != bundled_sha:
        raise HashMismatchError(
            "bundled DLL hash does not match bin/VERSION.txt.\n"
            f"  bundled  : {bundled_sha}\n"
            f"  pin says : {pinned_sha}"
        )
    if pinned_version and pinned_version != version:
        print(
            f"WARN: bin/VERSION.txt records version {pinned_version!r} "
            f"but verifying against {version!r}.",
            file=sys.stderr,
        )

    print(f"Fetching {PACKAGE} {version} from nuget.org...")
    nupkg = fetch_nupkg(version)

    nuget_sha = find_dll_hash(nupkg)
    if nuget_sha is None:
        raise ConfigError(
            f"no lib/{TFM}/{DLL_NAME} found inside {PACKAGE} {version}. "
            f"The package layout may have changed and the TFM pin (currently "
            f"{TFM!r}) needs updating."
        )
    print(f"  lib/{TFM}/{DLL_NAME}: {nuget_sha}")

    if bundled_sha == nuget_sha:
        print(
            f"\nOK: bundled {DLL_NAME} is byte-identical to lib/{TFM}/{DLL_NAME} "
            f"inside {PACKAGE} {version} on nuget.org."
        )
        return 0

    raise HashMismatchError(
        f"bundled {DLL_NAME} does NOT match lib/{TFM}/{DLL_NAME} in "
        f"{PACKAGE} {version} on nuget.org.\n"
        f"  bundled : {bundled_sha}\n"
        f"  nuget   : {nuget_sha}\n"
        "The bundled binary may have been tampered with or built from a "
        "different version. Re-run scripts/refresh_dll.py to refresh."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        help=f"Override the version of {PACKAGE} to verify against. "
             "Defaults to the pin in pyproject.toml. Ignored with --offline.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Verify only against the local pin in bin/VERSION.txt; makes "
             "no network request. Cannot detect drift from what nuget.org "
             "currently ships.",
    )
    args = parser.parse_args(argv)

    try:
        if args.offline:
            return _verify_offline()
        return _verify_online(args.version)
    except HashMismatchError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    except (ConfigError, NetworkError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
