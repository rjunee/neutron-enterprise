# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FIX-SYNC-INVOKERS (P2-21) — the SIEM-export default-on parity guardian.

The drain is enabled BY DEFAULT and the sink is the IN-CLUSTER otel-collector, on
BOTH the pilot compose ne-worker service and the Helm ne-worker Deployment. A static
compose test always runs; the Helm render test is per-test _needs_helm skipped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"

_CONSUMER = "siem-otel"
_ENDPOINT = "http://otel-collector:4318"

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _compose_worker_env() -> dict[str, Any]:
    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    # SafeLoader resolves the `<<: *ne-app-env` merge key.
    return dict(doc["services"]["ne-worker"]["environment"])


def test_compose_worker_enables_drain_to_in_cluster_collector() -> None:
    env = _compose_worker_env()
    assert env["NE_WORKER_OUTBOX_CONSUMER"] == _CONSUMER
    assert env["NE_WORKER_OUTBOX_OTLP_ENDPOINT"] == _ENDPOINT
    # In-cluster service host only (C2) — no external SIEM host.
    assert _ENDPOINT.startswith("http://otel-collector:")


def _rendered_docs() -> list[dict[str, Any]]:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _worker_env(docs: list[dict[str, Any]]) -> dict[str, str]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == "ne-worker":
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {e["name"]: e.get("value") for e in container["env"]}
    raise AssertionError("no Deployment/ne-worker in rendered output")


@_needs_helm
def test_helm_worker_enables_drain_to_in_cluster_collector() -> None:
    env = _worker_env(_rendered_docs())
    assert env["NE_WORKER_OUTBOX_CONSUMER"] == _CONSUMER
    assert env["NE_WORKER_OUTBOX_OTLP_ENDPOINT"] == _ENDPOINT
    assert env["NE_WORKER_OUTBOX_OTLP_ENDPOINT"].startswith("http://otel-collector:")
