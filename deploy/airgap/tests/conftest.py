# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the NE-073 AIR_GAP integration test harness.

The phase modules (test_airgap_phase_a.py / _b1.py / _b2.py / _c.py) only run
for real inside the sealed `ne-airgap` network namespace, driven by
deploy/airgap/scripts/run-airgap-pytest.sh (which sets ``NE_AIRGAP=1``). Run
anywhere else (a developer's laptop, `uv run pytest deploy/airgap/tests -q`
outside the harness, or a non-airgap CI job), each phase module skips at
collection time — the only always-on module is test_static_harness.py, which
needs no live namespace at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

AIRGAP_ACTIVE = os.environ.get("NE_AIRGAP") == "1"

REPO_ROOT = Path(__file__).resolve().parents[3]


def docker(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run `docker <args>` honoring the inner-dockerd DOCKER_HOST from the
    environment (set by run-airgap-pytest.sh / the no-egress workflow)."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )
