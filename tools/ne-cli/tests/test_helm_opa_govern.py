# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Helm-render guardians for the NE-PILOT-OPA-BUNDLE opa.governBundle overlay.

Per-test ``skipif`` (mirrors test_helm_chart.py's precedent): the static
byte-parity guardians in test_helm_chart.py (e.g.
``test_bundled_files_byte_match_pilot``) stay untouched and green regardless
of helm's presence — these tests are ADDITIONAL coverage for the new
values-gated opa-govern-policies ConfigMap + per-file subPath mounts, which
only render when ``ne k8s render`` populates ``opa.governBundle.files``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ne.cli.opa_bundle import govern_bundle_files

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"
GOVERN_POLICIES = REPO_ROOT / "packages" / "ne-govern" / "policies"

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _govern_overlay_file(tmp_path: Path) -> Path:
    """The `ne k8s render`-shaped overlay, written as JSON (a YAML subset —
    mirrors k8s.py's own `helm -f` convention: no PyYAML runtime dependency)."""
    files = govern_bundle_files(root=REPO_ROOT)
    overlay = {"opa": {"governBundle": {"files": files}}}
    path = tmp_path / "govern-overlay.json"
    path.write_text(json.dumps(overlay), encoding="utf-8")
    return path


def _render(*extra_values_files: Path) -> list[dict[str, Any]]:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI)]
    for values_file in extra_values_files:
        cmd += ["-f", str(values_file)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _by_kind_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


@_needs_helm
def test_default_render_has_no_govern_configmap() -> None:
    docs = _render()
    assert _by_kind_name(docs, "ConfigMap", "opa-govern-policies") is None
    opa = _by_kind_name(docs, "Deployment", "opa")
    assert opa is not None
    container = opa["spec"]["template"]["spec"]["containers"][0]
    mount_names = {m["name"] for m in container["volumeMounts"]}
    assert "govern-policies" not in mount_names
    volume_names = {v["name"] for v in opa["spec"]["template"]["spec"]["volumes"]}
    assert "govern-policies" not in volume_names
    # The placeholder mount is untouched.
    placeholder = next(m for m in container["volumeMounts"] if m.get("subPath") == "ne_system.rego")
    assert placeholder["mountPath"] == "/policies/ne_system.rego"
    assert placeholder["readOnly"] is True


@_needs_helm
def test_rendered_configmap_byte_matches_policies(tmp_path: Path) -> None:
    overlay = _govern_overlay_file(tmp_path)
    docs = _render(overlay)
    cm = _by_kind_name(docs, "ConfigMap", "opa-govern-policies")
    assert cm is not None

    files = govern_bundle_files(root=REPO_ROOT)
    assert set(cm["data"]) == set(files)
    for key, spec in files.items():
        assert cm["data"][key] == spec["content"], key


@_needs_helm
def test_deployment_mounts_every_policy_file(tmp_path: Path) -> None:
    overlay = _govern_overlay_file(tmp_path)
    docs = _render(overlay)
    opa = _by_kind_name(docs, "Deployment", "opa")
    assert opa is not None
    container = opa["spec"]["template"]["spec"]["containers"][0]

    files = govern_bundle_files(root=REPO_ROOT)
    govern_mounts = {
        m["subPath"]: m for m in container["volumeMounts"] if m["name"] == "govern-policies"
    }
    assert set(govern_mounts) == set(files)
    for key, spec in files.items():
        mount = govern_mounts[key]
        assert mount["mountPath"] == f"/policies/{spec['path']}"
        assert mount["readOnly"] is True

    volume = next(
        v for v in opa["spec"]["template"]["spec"]["volumes"] if v["name"] == "govern-policies"
    )
    assert volume["configMap"]["name"] == "opa-govern-policies"

    # The placeholder mount + telemetry args stay byte-identical.
    placeholder = next(m for m in container["volumeMounts"] if m.get("subPath") == "ne_system.rego")
    assert placeholder["mountPath"] == "/policies/ne_system.rego"
    assert "--disable-telemetry" in container["args"]


@_needs_helm
@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
def test_govern_render_kubeconform_valid(tmp_path: Path) -> None:
    overlay = _govern_overlay_file(tmp_path)
    template_cmd = [
        "helm",
        "template",
        "ne",
        str(CHART_DIR),
        "-f",
        str(VALUES_CI),
        "-f",
        str(overlay),
    ]
    template = subprocess.run(template_cmd, capture_output=True, text=True, check=True)
    kubeconform_cmd = ["kubeconform", "-strict", "-kubernetes-version", "1.31.0", "-summary"]
    result = subprocess.run(
        kubeconform_cmd, input=template.stdout, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
