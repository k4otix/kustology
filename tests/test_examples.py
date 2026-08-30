# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Smoke-test every example in ``examples/``.

The examples are the first thing a new user copies, and drift in them
(signature changes, removed re-exports, dependency leaks) stays invisible
until something runs them. Each example imports as a module and its ``main()``
runs with stdout captured. Only a clean return is asserted; the output shape
stays unpinned because examples are demos.

IR-dependent examples skip cleanly when pydantic isn't installed.

Every example renders through ``examples/_display.py``, which uses Rich when
it is installed and plain text when it is not. Both paths run here: the
``forced-plain`` parameter sets ``KUSTOLOGY_EXAMPLES_PLAIN=1`` to cover the
fallback on a base install, and skips itself where Rich is absent and the
default path is already the fallback.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

# Examples that touch ``kustology.ir.*`` need the [ir] extra.
IR_EXAMPLES = {
    "analyzer_demo", "find_all_demo", "linter", "llm_view",
    "lookback_window", "query_similarity", "semantic_hash_demo", "walk_ir",
}

_HAS_RICH = importlib.util.find_spec("rich") is not None


def _example_modules() -> list[Path]:
    return sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


@pytest.mark.parametrize("forced_plain", [False, True], ids=["default", "forced-plain"])
@pytest.mark.parametrize(
    "example_path", _example_modules(), ids=lambda p: p.stem,
)
def test_example_runs_cleanly(example_path: Path, forced_plain: bool, monkeypatch):
    stem = example_path.stem
    if stem in IR_EXAMPLES:
        pytest.importorskip("pydantic")
    if forced_plain and not _HAS_RICH:
        pytest.skip("rich isn't installed; the default path is already plain text")

    if forced_plain:
        monkeypatch.setenv("KUSTOLOGY_EXAMPLES_PLAIN", "1")
    else:
        monkeypatch.delenv("KUSTOLOGY_EXAMPLES_PLAIN", raising=False)

    # Running an example directly puts examples/ on sys.path, which is how its
    # ``from _display import ...`` resolves; loading by file path does not.
    monkeypatch.syspath_prepend(str(EXAMPLES_DIR))
    # _display reads the environment once, at import.
    sys.modules.pop("_display", None)

    # Load by file path so the example needs no install, namespaced under
    # "examples." so the module name cannot collide with kustology's.
    spec = importlib.util.spec_from_file_location(f"examples.{stem}", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        # Capture the import too, so a stray module-level ``print`` stays out
        # of pytest's output.
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
            assert hasattr(mod, "main"), f"{stem} has no main() function"
            # The convention is a zero-argument ``main()``.
            mod.main()
    finally:
        sys.modules.pop(spec.name, None)
        sys.modules.pop("_display", None)
