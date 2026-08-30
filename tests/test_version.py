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


def test_build_info_ir_tags_track_the_extra():
    info = kustology.build_info()
    try:
        from kustology import ir
    except ImportError:
        assert (info.ir_schema_version, info.semantic_hash_scheme) == (None, None)
    else:
        assert (info.ir_schema_version, info.semantic_hash_scheme) == (ir.IR_SCHEMA_VERSION, ir.SEMANTIC_HASH_SCHEME)
