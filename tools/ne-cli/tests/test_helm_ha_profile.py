# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-089 HA production-profile render guardian.

Proves the HA surface (`ne-api.podDisruptionBudget`/`.affinity`/
`.topologySpreadConstraints`) is truly ADDITIVE: the default render (CI
fixture alone) carries NONE of it, and the example overlay
(`deploy/helm/examples/values-production-ha.yaml`) renders it correctly on
top WITHOUT disturbing the existing hardening parity
(`tools/ne-cli/tests/test_helm_chart.py`, left unmodified, is the guardian for
that — this file only adds the HA-specific assertions).
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
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"
HA_OVERLAY = REPO_ROOT / "deploy" / "helm" / "examples" / "values-production-ha.yaml"

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _render(*value_files: Path) -> list[dict[str, Any]]:
    cmd = ["helm", "template", "ne", str(CHART_DIR)]
    for vf in value_files:
        cmd += ["-f", str(vf)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _by_kind_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


@pytest.fixture(scope="module")
def default_rendered() -> list[dict[str, Any]]:
    return _render(VALUES_CI)


@pytest.fixture(scope="module")
def ha_rendered() -> list[dict[str, Any]]:
    return _render(VALUES_CI, HA_OVERLAY)


def test_overlay_file_exists_and_is_valid_yaml() -> None:
    doc = _load_yaml(HA_OVERLAY)
    assert doc["ne-api"]["podDisruptionBudget"]["enabled"] is True
    assert doc["ne-api"]["podDisruptionBudget"]["minAvailable"] == 2
    assert doc["ne-api"]["replicas"] == 3
    # NE-071b: the ne-worker HA additions live in the SAME overlay.
    assert doc["ne-worker"]["replicas"] == 2
    assert doc["ne-worker"]["podDisruptionBudget"]["enabled"] is True
    assert doc["ne-worker"]["podDisruptionBudget"]["minAvailable"] == 1


@_needs_helm
def test_default_render_has_no_pdb_or_affinity(default_rendered: list[dict[str, Any]]) -> None:
    kinds_names = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in default_rendered}
    assert ("PodDisruptionBudget", "ne-api") not in kinds_names
    assert ("PodDisruptionBudget", "ne-worker") not in kinds_names

    ne_api = _by_kind_name(default_rendered, "Deployment", "ne-api")
    assert ne_api is not None
    pod_spec = ne_api["spec"]["template"]["spec"]
    assert "affinity" not in pod_spec
    assert "topologySpreadConstraints" not in pod_spec
    assert ne_api["spec"]["replicas"] == 2

    ne_worker = _by_kind_name(default_rendered, "Deployment", "ne-worker")
    assert ne_worker is not None
    worker_spec = ne_worker["spec"]["template"]["spec"]
    assert "affinity" not in worker_spec
    assert "topologySpreadConstraints" not in worker_spec
    assert ne_worker["spec"]["replicas"] == 1


@_needs_helm
def test_ha_overlay_renders(ha_rendered: list[dict[str, Any]]) -> None:
    pdb = _by_kind_name(ha_rendered, "PodDisruptionBudget", "ne-api")
    assert pdb is not None
    assert pdb["apiVersion"] == "policy/v1"
    assert pdb["spec"]["minAvailable"] == 2
    assert pdb["spec"]["selector"]["matchLabels"] == {"app": "ne-api"}

    ne_api = _by_kind_name(ha_rendered, "Deployment", "ne-api")
    assert ne_api is not None
    assert ne_api["spec"]["replicas"] == 3
    pod_spec = ne_api["spec"]["template"]["spec"]
    assert "podAntiAffinity" in pod_spec["affinity"]
    assert pod_spec["topologySpreadConstraints"][0]["maxSkew"] == 1

    opa = _by_kind_name(ha_rendered, "Deployment", "opa")
    assert opa is not None
    assert opa["spec"]["replicas"] == 3

    # NE-071b: the ne-worker HA equivalents render on the SAME overlay.
    worker_pdb = _by_kind_name(ha_rendered, "PodDisruptionBudget", "ne-worker")
    assert worker_pdb is not None
    assert worker_pdb["apiVersion"] == "policy/v1"
    assert worker_pdb["spec"]["minAvailable"] == 1
    assert worker_pdb["spec"]["selector"]["matchLabels"] == {"app": "ne-worker"}

    ne_worker = _by_kind_name(ha_rendered, "Deployment", "ne-worker")
    assert ne_worker is not None
    assert ne_worker["spec"]["replicas"] == 2
    worker_spec = ne_worker["spec"]["template"]["spec"]
    assert "podAntiAffinity" in worker_spec["affinity"]
    assert worker_spec["topologySpreadConstraints"][0]["maxSkew"] == 1

    # Hardening parity STILL holds under the overlay (the whole point of
    # additive-not-conflicting values).
    container = pod_spec["containers"][0]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    sc = container["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]

    # ...and the ne-worker container is still hardened under the overlay too.
    worker_container = worker_spec["containers"][0]
    worker_sc = worker_container["securityContext"]
    assert worker_spec["securityContext"]["runAsNonRoot"] is True
    assert worker_sc["allowPrivilegeEscalation"] is False
    assert worker_sc["capabilities"]["drop"] == ["ALL"]
    assert worker_sc["readOnlyRootFilesystem"] is True
