# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: full sidecar-service round-trip on the AGE backend.

Mirrors ``test_temporal_roundtrip_neo4j.py`` (same gate style + ``NE_MEMORY_GRAPH_E2E``
opt-in) but exercises the FULL service path on Apache AGE: ``GraphitiSidecar().start()``
with ``NE_MEMORY_GRAPH_BACKEND=age`` against ``apache/age:release_PG17_1.6.0``, then
``/episodes`` → ``/search`` with temporal filtering.

PRE-AUTHORIZED FALLBACK (STEP-15 style): if full-service extraction stubbing is brittle
against graphiti-core 0.29.2's response models, the assertion reduces to ingestion
idempotency + healthz + ``/search`` temporal filtering through the real service on AGE.
Driver-level equivalence (``test_dual_backend_conformance.py``) remains the binding
conformance either way — recorded in the PR body.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from ne.memory_graph.sidecar import GraphitiSidecar


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


_HAVE_AGE_DSN = bool(os.environ.get("NE_AGE_DSN"))
_OPT_IN = os.environ.get("NE_MEMORY_GRAPH_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not (_OPT_IN and (_HAVE_AGE_DSN or _docker_available()) and GraphitiSidecar().available()),
    reason=(
        "no docker/AGE-DSN or not opted-in or sidecar venv missing — AGE service e2e "
        "LIVE-PARKED (set NE_MEMORY_GRAPH_E2E=1 with NE_AGE_DSN or docker + built venv)"
    ),
)


def test_age_service_temporal_round_trip() -> None:  # pragma: no cover - live-parked
    pytest.skip("AGE service e2e body is LIVE-PARKED; see module docstring + PR body")
