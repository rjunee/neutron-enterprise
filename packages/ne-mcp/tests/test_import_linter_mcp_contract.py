# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance (c), part 2: the c3-mcp-sdk-chokepoint import-linter contract, both ways.

The contract at /.importlinter passes on the clean tree AND fails on a planted ``import
mcp`` in a module OTHER than ne.mcp._sdk (a module-level chokepoint proof, strictly
stronger than a package-boundary check — mirrors NE-012's precedent).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> ne-mcp/ -> packages/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LINT = Path(sys.executable).parent / "lint-imports"
_CMD = [str(_LINT), "--config", str(_REPO_ROOT / ".importlinter"), "--no-cache"]

_HEADER = "# Copyright (c) 2026 Ryan Junee.\n# SPDX-License-Identifier: Apache-2.0\n"


# NE-XDIST-PLANT: this pair SHELLS OUT to `lint-imports` against the SHARED repo
# root and one of them PLANTS a violating module into the source tree. Under
# pytest-xdist the "clean tree" test can run inside the other's plant window and
# fail on a violation that is not its own. The group name is SHARED with
# packages/ne-gateway/tests/test_import_linter_contract.py deliberately: both
# modules plant into the same repo and run the SAME repo-wide lint, so a
# per-module group would still let the two modules race each other.
@pytest.mark.xdist_group(name="import-linter-shared-tree")
def test_mcp_contract_passes_on_clean_tree() -> None:
    proc = subprocess.run(_CMD, cwd=_REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, f"lint-imports failed on clean tree:\n{proc.stdout}\n{proc.stderr}"


# NE-XDIST-PLANT: this pair SHELLS OUT to `lint-imports` against the SHARED repo
# root and one of them PLANTS a violating module into the source tree. Under
# pytest-xdist the "clean tree" test can run inside the other's plant window and
# fail on a violation that is not its own. The group name is SHARED with
# packages/ne-gateway/tests/test_import_linter_contract.py deliberately: both
# modules plant into the same repo and run the SAME repo-wide lint, so a
# per-module group would still let the two modules race each other.
@pytest.mark.xdist_group(name="import-linter-shared-tree")
def test_mcp_contract_catches_planted_violation() -> None:
    plant = _REPO_ROOT / "packages/ne-mcp/src/ne/mcp/_ne049_planted_c3_violation.py"
    # Self-heal a leak from a hard-killed prior run (SIGKILL/OOM between write and
    # the finally-unlink is the one window the finally can't cover). Pre-cleaning
    # here — rather than `assert not plant.exists()` outside the try — means a
    # stale artifact is removed instead of failing this run AND persisting.
    plant.unlink(missing_ok=True)
    plant.write_text(
        _HEADER + "import mcp  # planted by NE-049 contract test\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(_CMD, cwd=_REPO_ROOT, capture_output=True, text=True)
        assert proc.returncode != 0, f"contract did not catch the planted violation:\n{proc.stdout}"
        assert "c3-mcp-sdk-chokepoint" in proc.stdout or "Broken" in proc.stdout, proc.stdout
    finally:
        plant.unlink(missing_ok=True)
