# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Exit-code tests for the scripts/*.py maintenance tooling.

A pre-release audit found several of these scripts exit 0 on failure -- an
empty corpus, a network error, a directory that isn't the repo it claims to
be all read as success. Each test here drives one script through argv/main()
and asserts the process-level exit code a caller (a human, or CI) actually
sees, not just that the function runs without raising.

scripts/ has no __init__.py -- it isn't an installed package -- so each
module is loaded fresh by file path with importlib rather than imported by
name. A fresh load per test also means one test's monkeypatched module
globals (BIN_DIR, PYPROJECT, NUGET_FLATCONTAINER) can never leak into
another test's copy of the same module.

verify_dll.py / refresh_dll.py touch the bundled Kusto.Language.dll and its
nuget.org provenance. Nothing here downloads the real package or writes to
the repo's actual src/kustology/bin/ -- every fixture uses a tmp_path bin/
dir, and the one network-path test points at a reserved, guaranteed-
unroutable host (RFC 2606's .invalid TLD) so it exercises the NetworkError
path without ever reaching nuget.org.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    """Load scripts/<name>.py as a fresh module object."""
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_scripts_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# verify_dll.py
# ---------------------------------------------------------------------------

def _dll_bin_dir(tmp_path: Path, *, bundled: bytes,
                  pinned_sha: str | None, pinned_version: str = "12.3.2") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "Kusto.Language.dll").write_bytes(bundled)
    if pinned_sha is not None:
        (bin_dir / "VERSION.txt").write_text(
            "package=Microsoft.Azure.Kusto.Language\n"
            f"version={pinned_version}\n"
            f"sha256={pinned_sha}\n"
            "refreshed=2026-01-01T00:00:00+00:00\n"
        )
    return bin_dir


def test_verify_dll_offline_matching_pin_exits_0(tmp_path):
    mod = _load("verify_dll")
    data = b"fake dll bytes for a matching-pin fixture"
    mod.BIN_DIR = _dll_bin_dir(tmp_path, bundled=data,
                                pinned_sha=hashlib.sha256(data).hexdigest())
    assert mod.main(["--offline"]) == 0


def test_verify_dll_offline_hash_mismatch_exits_1(tmp_path):
    """A genuine hash mismatch is the ONE case that returns 1 -- everything
    else (missing files, missing pin, network trouble) is 2."""
    mod = _load("verify_dll")
    mod.BIN_DIR = _dll_bin_dir(tmp_path, bundled=b"actual bundled bytes",
                                pinned_sha="0" * 64)
    assert mod.main(["--offline"]) == 1


def test_verify_dll_offline_no_sha_pin_exits_2(tmp_path):
    mod = _load("verify_dll")
    mod.BIN_DIR = _dll_bin_dir(tmp_path, bundled=b"actual bundled bytes",
                                pinned_sha=None)
    assert mod.main(["--offline"]) == 2


def test_verify_dll_offline_missing_dll_exits_2(tmp_path):
    mod = _load("verify_dll")
    mod.BIN_DIR = tmp_path / "no-such-bin"
    assert mod.main(["--offline"]) == 2


def test_verify_dll_no_version_pin_exits_2(tmp_path):
    mod = _load("verify_dll")
    mod.PYPROJECT = tmp_path / "pyproject.toml"
    mod.PYPROJECT.write_text('[project]\nname = "x"\n')
    assert mod.main([]) == 2


def test_verify_dll_fake_url_network_error_exits_2(tmp_path, capsys):
    """A real fetch attempt against a host that cannot exist (RFC 2606
    .invalid) hits NetworkError, not HashMismatchError: exit 2, and the
    stderr names the failure as a network problem, not a hash mismatch."""
    mod = _load("verify_dll")
    mod.BIN_DIR = _dll_bin_dir(tmp_path, bundled=b"whatever bytes",
                                pinned_sha=None)
    mod.NUGET_FLATCONTAINER = "https://kustology-test-fixture.invalid/v3-flatcontainer"
    rc = mod.main(["--version", "12.3.2"])
    assert rc == 2
    assert "FAIL" in capsys.readouterr().err


def test_verify_dll_tfm_pin_is_net6_0():
    mod = _load("verify_dll")
    assert mod.TFM == "net6.0"


# ---------------------------------------------------------------------------
# refresh_dll.py
# ---------------------------------------------------------------------------

def test_refresh_dll_atomic_write_text_writes_content_and_no_leftover_tmp(tmp_path):
    mod = _load("refresh_dll")
    target = tmp_path / "sub" / "VERSION.txt"
    mod._atomic_write_text(target, "package=x\nversion=1.2.3\n")
    assert target.read_text() == "package=x\nversion=1.2.3\n"
    assert list(target.parent.glob(".VERSION.txt.*.tmp")) == []


def test_refresh_dll_atomic_write_replaces_existing_file_content(tmp_path):
    mod = _load("refresh_dll")
    target = tmp_path / "VERSION.txt"
    target.write_text("stale content\n")
    mod._atomic_write_bytes(target, b"fresh content\n")
    assert target.read_bytes() == b"fresh content\n"


def test_refresh_dll_atomic_write_cleans_up_temp_file_on_failure(tmp_path, monkeypatch):
    """A write that fails mid-flight must not leave the target corrupted or
    a stray temp file behind."""
    mod = _load("refresh_dll")
    target = tmp_path / "VERSION.txt"
    target.write_text("original\n")

    real_replace = mod.os.replace

    def _boom(*args, **kwargs):
        raise OSError("simulated failure before replace")

    monkeypatch.setattr(mod.os, "replace", _boom)
    with pytest.raises(OSError):
        mod._atomic_write_text(target, "should not land\n")
    monkeypatch.setattr(mod.os, "replace", real_replace)

    assert target.read_text() == "original\n"
    assert list(tmp_path.glob(".VERSION.txt.*.tmp")) == []


def test_refresh_dll_write_pinned_version_updates_existing_pin(tmp_path):
    mod = _load("refresh_dll")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\n\n'
        "[tool.kustology]\n"
        'kusto_language_version = "1.0.0"\n'
    )
    mod.PYPROJECT = pyproject
    mod.write_pinned_version("9.9.9")
    text = pyproject.read_text()
    assert 'kusto_language_version = "9.9.9"' in text
    assert "1.0.0" not in text


def test_refresh_dll_write_pinned_version_inserts_new_block(tmp_path):
    mod = _load("refresh_dll")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n')
    mod.PYPROJECT = pyproject
    mod.write_pinned_version("5.5.5")
    text = pyproject.read_text()
    assert "[tool.kustology]" in text
    assert 'kusto_language_version = "5.5.5"' in text


def test_refresh_dll_tfm_matches_verify_dll_pin():
    """refresh_dll and verify_dll must pin the same TFM -- a mismatch means
    refresh_dll fetches a DLL verify_dll can never match, even from the
    correct package version."""
    refresh = _load("refresh_dll")
    verify = _load("verify_dll")
    assert refresh.TFM == verify.TFM == "net6.0"
    assert f"<TargetFramework>{refresh.TFM}</TargetFramework>" in refresh._csproj_text("12.3.2")


# ---------------------------------------------------------------------------
# verify_corpus.py (needs pydantic -- the [ir] extra)
# ---------------------------------------------------------------------------

def test_verify_corpus_empty_corpus_exits_1(tmp_path):
    pytest.importorskip("pydantic")
    mod = _load("verify_corpus")
    empty_dir = tmp_path / "empty-corpus"
    empty_dir.mkdir()
    rc = mod.main(["--corpus", str(empty_dir),
                   "--schemas", str(tmp_path / "no-schemas.json"),
                   "--output", str(tmp_path / "verdict.json")])
    assert rc == 1


def test_verify_corpus_missing_corpus_dir_exits_1(tmp_path):
    """rglob on a nonexistent directory silently yields nothing rather than
    raising -- the missing-dir case and the empty-dir case must both be
    caught by the same empty-corpus check."""
    pytest.importorskip("pydantic")
    mod = _load("verify_corpus")
    missing_dir = tmp_path / "does-not-exist"
    rc = mod.main(["--corpus", str(missing_dir),
                   "--schemas", str(tmp_path / "no-schemas.json"),
                   "--output", str(tmp_path / "verdict.json")])
    assert rc == 1


def test_verify_corpus_empty_corpus_soft_exits_0(tmp_path):
    pytest.importorskip("pydantic")
    mod = _load("verify_corpus")
    empty_dir = tmp_path / "empty-corpus"
    empty_dir.mkdir()
    rc = mod.main(["--corpus", str(empty_dir), "--soft",
                   "--schemas", str(tmp_path / "no-schemas.json"),
                   "--output", str(tmp_path / "verdict.json")])
    assert rc == 0


def test_verify_corpus_findings_exit_1_and_soft_exits_0(tmp_path):
    """A query the IR builder cannot build at all is a `builder_exception`
    finding -- the corpus is non-empty, but not clean, so the default run
    must fail while --soft still reports and returns 0."""
    pytest.importorskip("pydantic")
    mod = _load("verify_corpus")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "broken.kql").write_text("| | | not valid kql at all | | |")
    output = tmp_path / "verdict.json"

    rc = mod.main(["--corpus", str(corpus),
                   "--schemas", str(tmp_path / "no-schemas.json"),
                   "--output", str(output)])
    assert rc == 1
    assert output.exists()

    rc_soft = mod.main(["--corpus", str(corpus), "--soft",
                        "--schemas", str(tmp_path / "no-schemas.json"),
                        "--output", str(output)])
    assert rc_soft == 0


def test_verify_corpus_clean_corpus_exits_0(tmp_path):
    pytest.importorskip("pydantic")
    mod = _load("verify_corpus")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "simple.kql").write_text("T | take 1")
    rc = mod.main(["--corpus", str(corpus),
                   "--schemas", str(tmp_path / "no-schemas.json"),
                   "--output", str(tmp_path / "verdict.json")])
    assert rc == 0


def test_verify_corpus_reads_invalid_utf8_without_crashing(tmp_path):
    """errors="replace" on the corpus reads: a file with a stray invalid
    byte must not raise UnicodeDecodeError and take the whole run down."""
    pytest.importorskip("pydantic")
    mod = _load("verify_corpus")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    bad = corpus / "bad_encoding.kql"
    bad.write_bytes(b"T | where Name == 'a\xffb'")
    rc = mod.main(["--corpus", str(corpus),
                   "--schemas", str(tmp_path / "no-schemas.json"),
                   "--output", str(tmp_path / "verdict.json")])
    # Must complete (0 or 1, either is a real verdict) rather than raise.
    assert rc in (0, 1)


# ---------------------------------------------------------------------------
# extract_sentinel_schemas.py
# ---------------------------------------------------------------------------

def test_extract_sentinel_schemas_writes_output_only_on_success(tmp_path):
    mod = _load("extract_sentinel_schemas")
    reference = tmp_path / "reference.md"
    reference.write_text("# Not a table reference at all\nJust prose.\n")
    output = tmp_path / "schemas.json"

    rc = mod.main(["--reference-md", str(reference), "--output", str(output)])

    assert rc == 1
    assert not output.exists()


def test_extract_sentinel_schemas_writes_output_on_success(tmp_path):
    mod = _load("extract_sentinel_schemas")
    reference = tmp_path / "reference.md"
    reference.write_text(
        "### `MyTable`\n"
        "**Key Columns:**\n"
        "| Column | Type | Description |\n"
        "| `Foo` | string | a column |\n"
    )
    output = tmp_path / "schemas.json"

    rc = mod.main(["--reference-md", str(reference), "--output", str(output)])

    assert rc == 0
    assert output.exists()
    assert '"MyTable"' in output.read_text()


def test_extract_sentinel_schemas_missing_reference_exits_1(tmp_path):
    mod = _load("extract_sentinel_schemas")
    output = tmp_path / "schemas.json"
    rc = mod.main(["--reference-md", str(tmp_path / "does-not-exist.md"),
                   "--output", str(output)])
    assert rc == 1
    assert not output.exists()


# ---------------------------------------------------------------------------
# sample_sentinel_corpus.py
# ---------------------------------------------------------------------------

def test_sample_sentinel_corpus_missing_dir_exits_nonzero(tmp_path):
    pytest.importorskip("yaml")
    mod = _load("sample_sentinel_corpus")
    rc = mod.main(["--sentinel-root", str(tmp_path / "does-not-exist"),
                   "--dry-run"])
    assert rc != 0


def test_sample_sentinel_corpus_non_repo_dir_exits_nonzero(tmp_path):
    """A directory that exists but was never `git init`ed (a stray scratch
    dir, a wrong path, a clone that failed partway) used to sample zero
    queries from empty stratum folders and exit 0 with an empty manifest --
    indistinguishable from a real, successful, empty result."""
    pytest.importorskip("yaml")
    mod = _load("sample_sentinel_corpus")
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    (not_a_repo / "Detections").mkdir()
    rc = mod.main(["--sentinel-root", str(not_a_repo), "--dry-run"])
    assert rc != 0


def test_sample_sentinel_corpus_repo_with_no_strata_exits_nonzero(tmp_path):
    """A real git repo that just doesn't happen to be Azure-Sentinel (none
    of the expected stratum folders) must not report 0 sampled queries as
    success either."""
    pytest.importorskip("yaml")
    mod = _load("sample_sentinel_corpus")
    repo = tmp_path / "some-other-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("not a sentinel clone\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    rc = mod.main(["--sentinel-root", str(repo), "--dry-run"])
    assert rc != 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
