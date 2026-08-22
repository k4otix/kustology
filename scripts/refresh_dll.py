#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan
"""
Refresh the bundled Kusto.Language.dll.

Resolves Microsoft.Azure.Kusto.Language from NuGet via the dotnet CLI, copies
the resulting Kusto.Language.dll into src/kustology/bin/, and writes
bin/VERSION.txt with the package version, sha256, and refresh timestamp.

Usage:
    python scripts/refresh_dll.py                       # uses pinned version
    python scripts/refresh_dll.py --version 12.3.2      # explicit override
    python scripts/refresh_dll.py --pin                 # also write the version
                                                        # back into pyproject.toml

The pinned version is read from pyproject.toml under
[tool.kustology] kusto_language_version.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
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

# Must match verify_dll.py's TFM pin -- that script only accepts
# lib/net6.0/Kusto.Language.dll as "the" DLL, so publishing against a
# different TargetFramework here would fetch a build verify_dll.py can never
# match, even when it is a legitimate copy from the same package version.
TFM = "net6.0"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` via a same-directory temp file + os.replace.

    A crash or kill mid-write leaves the temp file, never a truncated
    `path` -- `path` either has its old contents or its fully-written new
    ones, never something in between. os.replace is atomic on both POSIX
    and Windows as long as source and destination are on the same
    filesystem, which a same-directory temp file guarantees. The
    f.flush() + os.fsync() before the replace extends that guarantee to a
    power loss, not just a killed process -- without it, the new bytes
    could still be sitting in the OS page cache, never written to disk,
    at the moment os.replace renames the temp file over `path`.

    tempfile.mkstemp creates the temp file at mode 0600 (owner-only), and
    os.replace carries the *source* file's mode to the destination -- so
    without an explicit chmod, every atomically-written file would
    silently lose its group/world-readable bits relative to what
    shutil.copyfile/Path.write_text would have produced. We restore the
    target's existing mode when overwriting a file that already exists,
    or fall back to the umask-default 0644 for a brand new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    _atomic_write_bytes(path, text.encode(encoding))


def read_pinned_version() -> str | None:
    if not PYPROJECT.exists():
        return None
    data = tomllib.loads(PYPROJECT.read_text())
    return data.get("tool", {}).get("kustology", {}).get("kusto_language_version")


def write_pinned_version(version: str) -> None:
    """Insert or update [tool.kustology] kusto_language_version atomically."""
    text = PYPROJECT.read_text()
    marker = "[tool.kustology]"
    block = f"{marker}\nkusto_language_version = \"{version}\"\n"
    if marker in text:
        lines = text.splitlines(keepends=True)
        out = []
        in_block = False
        replaced = False
        for line in lines:
            if line.startswith(marker):
                in_block = True
                out.append(line)
                continue
            if in_block and line.lstrip().startswith("kusto_language_version"):
                out.append(f'kusto_language_version = "{version}"\n')
                replaced = True
                continue
            if in_block and line.startswith("[") and not line.startswith(marker):
                if not replaced:
                    out.append(f'kusto_language_version = "{version}"\n')
                    replaced = True
                in_block = False
            out.append(line)
        if in_block and not replaced:
            out.append(f'kusto_language_version = "{version}"\n')
        text = "".join(out)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + block
    _atomic_write_text(PYPROJECT, text)


def _csproj_text(version: str) -> str:
    """The throwaway project file dotnet publish resolves PACKAGE@version
    against. Split out from restore_and_publish so the TFM pin can be
    asserted on without invoking dotnet (which would need network access to
    restore the package)."""
    return textwrap.dedent(f"""\
        <Project Sdk="Microsoft.NET.Sdk">
          <PropertyGroup>
            <TargetFramework>{TFM}</TargetFramework>
            <CopyLocalLockFileAssemblies>true</CopyLocalLockFileAssemblies>
            <EnableDefaultItems>false</EnableDefaultItems>
          </PropertyGroup>
          <ItemGroup>
            <PackageReference Include="{PACKAGE}" Version="{version}" />
          </ItemGroup>
        </Project>
        """)


def restore_and_publish(version: str, work_dir: Path) -> Path:
    """Run `dotnet publish` to materialize the package and return the DLL path."""
    csproj = work_dir / "fetch.csproj"
    csproj.write_text(_csproj_text(version))

    out_dir = work_dir / "out"
    subprocess.run(
        ["dotnet", "publish", str(csproj), "-c", "Release", "-o", str(out_dir)],
        check=True,
        cwd=work_dir,
    )

    dll = out_dir / DLL_NAME
    if not dll.exists():
        candidates = list(out_dir.rglob(DLL_NAME))
        if not candidates:
            raise FileNotFoundError(f"{DLL_NAME} not found in {out_dir}")
        dll = candidates[0]
    return dll


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        help=f"Version of {PACKAGE} to fetch. "
             "Defaults to the pin in pyproject.toml.",
    )
    parser.add_argument(
        "--pin",
        action="store_true",
        help="Update [tool.kustology] kusto_language_version in pyproject.toml.",
    )
    args = parser.parse_args(argv)

    version = args.version or read_pinned_version()
    if not version:
        parser.error(
            "No version provided and no pin found in pyproject.toml. "
            "Pass --version, e.g. `--version 12.3.2`."
        )

    if shutil.which("dotnet") is None:
        parser.error(
            "dotnet CLI not found on PATH. Install .NET 8.0+ from "
            "https://dotnet.microsoft.com/download/dotnet/8.0"
        )

    BIN_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="kustology-dll-") as tmp:
        work_dir = Path(tmp)
        print(f"Fetching {PACKAGE} {version} via dotnet publish (TFM={TFM})...")
        dll = restore_and_publish(version, work_dir)
        sha = sha256_of(dll)
        target = BIN_DIR / DLL_NAME
        # Same rationale as the VERSION.txt / pyproject.toml writes below: a
        # reader (or a concurrent test run) must never observe a
        # partially-written DLL.
        _atomic_write_bytes(target, dll.read_bytes())

    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    _atomic_write_text(
        BIN_DIR / "VERSION.txt",
        f"package={PACKAGE}\n"
        f"version={version}\n"
        f"sha256={sha}\n"
        f"refreshed={timestamp}\n",
    )
    print(f"Wrote {target} ({sha[:16]}...)")
    print(f"Wrote {BIN_DIR / 'VERSION.txt'}")

    if args.pin:
        write_pinned_version(version)
        print(f"Pinned {PACKAGE}={version} in {PYPROJECT}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
