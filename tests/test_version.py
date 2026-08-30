# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

import pathlib
import re

import kustology


def _source_version():
    src = pathlib.Path(kustology.__file__).with_name("_version.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"$', src, re.MULTILINE).group(1)


def test_version_describes_the_imported_code():
    assert kustology.__version__ == _source_version()


def test_build_info_matches_the_bundled_pin():
    info = kustology.build_info()
    pin = pathlib.Path(kustology.__file__).with_name("bin").joinpath("VERSION.txt").read_text()
    assert f"version={info.kusto_language_version}" in pin
    assert f"sha256={info.kusto_language_sha256}" in pin
    assert info.version == kustology.__version__


def test_build_info_reports_the_ir_tags_without_the_extra():
    """The tags describe the installed source, so they do not depend on pydantic.

    The assertion reads ``kustology._ir_tags``, which imports on a base
    install where ``kustology.ir`` would raise. The check therefore runs in
    every CI cell, and a ``build_info()`` that reached into the IR fails it.
    """
    from kustology import _ir_tags

    info = kustology.build_info()
    assert (info.ir_schema_version, info.semantic_hash_scheme) == (
        _ir_tags.IR_SCHEMA_VERSION,
        _ir_tags.SEMANTIC_HASH_SCHEME,
    )


def test_build_info_does_not_import_the_ir():
    """A Tier 1 host reading its DLL pin must not pay for pydantic and the IR graph."""
    import subprocess
    import sys

    probe = (
        "import kustology, sys; kustology.build_info(); "
        "print(int('pydantic' in sys.modules or 'kustology.ir' in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0", "build_info() pulled the IR into sys.modules"
