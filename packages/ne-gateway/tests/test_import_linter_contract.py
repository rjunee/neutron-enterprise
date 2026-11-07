# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C3 import-linter contract proof in both directions (NE-012): the contract at
/.importlinter passes on the clean tree and fails on a planted litellm import in
a module OTHER than ne.gateway._litellm (proving the module-level chokepoint,
strictly stronger than a package-boundary check).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# tests/ -> ne-gateway/ -> packages/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LINT = Path(sys.executable).parent / "lint-imports"
_CMD = [str(_LINT), "--config", str(_REPO_ROOT / ".importlinter"), "--no-cache"]

_HEADER = "# Copyright (c) 2026 Ryan Junee.\n# SPDX-License-Identifier: Apache-2.0\n"


# NE-XDIST-PLANT: these two tests SHELL OUT to `lint-imports` against the SHARED
# repo root, and one of them PLANTS a violating module into the source tree for
# the duration of its run. Under pytest-xdist they land on different workers, so
# the "clean tree" test can execute inside the other's plant window and see a
# violation that is not its own — observed in CI as
#   C3: provider SDKs / litellm only via ne.gateway._litellm BROKEN
#     ne.gateway._ne012_planted_c3_violation -> litellm (l.3)
# on a PR whose diff touched a single shell script. Serially they cannot overlap,
# which is why this passed for as long as the suite ran serially.
#
# Same shared-external-state reason the live-OPA tests carry an xdist_group. The
# group name is SHARED ACROSS BOTH import-linter modules on purpose: ne-mcp plants
# its own violation into ITS source tree and runs the SAME repo-wide lint, so a
# per-module group would still let the two modules race each other.
@pytest.mark.xdist_group(name="import-linter-shared-tree")
def test_c3_contract_passes_on_clean_tree() -> None:
    proc = subprocess.run(_CMD, cwd=_REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, f"lint-imports failed on clean tree:\n{proc.stdout}\n{proc.stderr}"


# NE-XDIST-PLANT: these two tests SHELL OUT to `lint-imports` against the SHARED
# repo root, and one of them PLANTS a violating module into the source tree for
# the duration of its run. Under pytest-xdist they land on different workers, so
# the "clean tree" test can execute inside the other's plant window and see a
# violation that is not its own — observed in CI as
#   C3: provider SDKs / litellm only via ne.gateway._litellm BROKEN
#     ne.gateway._ne012_planted_c3_violation -> litellm (l.3)
# on a PR whose diff touched a single shell script. Serially they cannot overlap,
# which is why this passed for as long as the suite ran serially.
#
# Same shared-external-state reason the live-OPA tests carry an xdist_group. The
# group name is SHARED ACROSS BOTH import-linter modules on purpose: ne-mcp plants
# its own violation into ITS source tree and runs the SAME repo-wide lint, so a
# per-module group would still let the two modules race each other.
@pytest.mark.xdist_group(name="import-linter-shared-tree")
def test_c3_contract_catches_planted_violation() -> None:
    plant = _REPO_ROOT / "packages/ne-gateway/src/ne/gateway/_ne012_planted_c3_violation.py"
    assert not plant.exists()
    plant.write_text(
        _HEADER + "import litellm  # planted by NE-012 contract test\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(_CMD, cwd=_REPO_ROOT, capture_output=True, text=True)
        assert proc.returncode != 0, f"contract did not catch the planted violation:\n{proc.stdout}"
        assert "c3-provider-sdk-chokepoint" in proc.stdout or "Broken" in proc.stdout, proc.stdout
    finally:
        plant.unlink(missing_ok=True)
