# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eddie Allan

"""Smoke-test every example in ``examples/``.

The examples are the first thing a new user copies. Silent drift —
signature changes, removed re-exports, dependency leaks — is invisible
without exercising them in CI. This test imports each example as a
module and invokes its ``main()`` with stdout captured, asserting only
that it returns cleanly. The shape of the output is intentionally not
pinned: examples are demos, not contracts.

IR-dependent examples skip cleanly when pydantic isn't installed.
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
    "semantic_hash_demo", "walk_ir",
}


def _example_modules() -> list[Path]:
    return sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


@pytest.mark.parametrize(
    "example_path", _example_modules(), ids=lambda p: p.stem,
)
def test_example_runs_cleanly(example_path: Path):
    stem = example_path.stem
    if stem in IR_EXAMPLES:
        pytest.importorskip("pydantic")

    # Load by file path so the example doesn't need to be on sys.path or
    # installed as a package. mod_name is namespaced under "examples." to
    # avoid colliding with kustology module names.
    spec = importlib.util.spec_from_file_location(f"examples.{stem}", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        # Importing the module shouldn't print to stdout, but capture
        # defensively so a stray ``print`` at module load doesn't pollute
        # pytest's output.
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
            assert hasattr(mod, "main"), f"{stem} has no main() function"
            # Some examples take parameters; the convention here is zero-arg.
            mod.main()
    finally:
        sys.modules.pop(spec.name, None)
