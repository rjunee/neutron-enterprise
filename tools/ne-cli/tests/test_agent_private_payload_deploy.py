# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-009A A3: deployment truth for the Agent Run private-payload key.

Two properties this file exists to hold:

* **API and worker receive the byte-identical secret reference.** Two roots with
  different keys is the failure this whole unit is built to prevent, and a
  per-service copy of the env block is exactly how that happens. Asserted as an
  equality between the two rendered deployments, not by reading both and hoping.
* **No key material is ever rendered.** Compose, Helm, the rotation Job and the
  Zarf package carry NAMES, references and key ids only.

Self-contained by convention -- `tools/ne-cli/tests/` has no conftest.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"
COMPOSE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"

KEY_ID_ENV = "NE_AGENT_PRIVATE_PAYLOAD_KEY_ID"
KEK_ENV = "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64"
_ENVS = (KEY_ID_ENV, KEK_ENV)

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*extra: str) -> list[dict[str, Any]]:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI), *extra]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _deployment_env(docs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == name:
            containers = doc["spec"]["template"]["spec"]["containers"]
            merged: list[dict[str, Any]] = []
            for container in containers:
                merged.extend(container.get("env", []))
            return merged
    raise AssertionError(f"no Deployment/{name}")


# --- Pilot Compose ------------------------------------------------------------


def test_compose_declares_both_keys_null_valued_in_the_shared_anchor() -> None:
    """Null-valued, so an unset key is OMITTED from the container entirely, and
    SHARED, so the two roots cannot drift apart."""
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    api = doc["services"]["ne-api"]["environment"]
    worker = doc["services"]["ne-worker"]["environment"]
    for env in _ENVS:
        assert env in api and api[env] is None, env
        assert env in worker and worker[env] is None, env
    # Identical reference, not merely both present.
    assert {k: api[k] for k in _ENVS} == {k: worker[k] for k in _ENVS}


def test_compose_carries_no_key_material_anywhere() -> None:
    """A literal here would also trip the pilot hygiene scanner's `_key` regex."""
    body = COMPOSE.read_text(encoding="utf-8")
    for env in _ENVS:
        for line in body.splitlines():
            if line.strip().startswith(f"{env}:"):
                assert line.strip() == f"{env}:", line


# --- Helm ---------------------------------------------------------------------


@_needs_helm
def test_api_and_worker_receive_the_identical_secret_reference() -> None:
    docs = _render()
    api = [item for item in _deployment_env(docs, "ne-api") if item["name"] in _ENVS]
    worker = [item for item in _deployment_env(docs, "ne-worker") if item["name"] in _ENVS]
    assert {item["name"] for item in api} == set(_ENVS)
    # THE assertion: byte-identical references, so the two roots cannot hold
    # different keys. Sorted because env ORDER may legitimately differ.
    assert sorted(api, key=lambda i: i["name"]) == sorted(worker, key=lambda i: i["name"])
    for item in api:
        ref = item["valueFrom"]["secretKeyRef"]
        assert ref["key"] == item["name"]
        assert ref["optional"] is True
        assert "value" not in item  # never an inline literal


@_needs_helm
def test_no_rendered_manifest_contains_key_material() -> None:
    """Deployment truth carries names, references and key ids -- never material."""
    docs = _render("--set", "agentPrivatePayloadRotation.enabled=true")
    body = json.dumps(docs)
    assert KEK_ENV in body  # the NAME is present...
    for doc in docs:
        if doc.get("kind") == "Secret":
            assert KEK_ENV not in json.dumps(doc.get("stringData", {}))
            assert KEK_ENV not in json.dumps(doc.get("data", {}))
    # ...and never as an inline value on any container env entry.
    for doc in docs:
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []):
            for item in container.get("env", []):
                if item.get("name") in _ENVS:
                    assert "value" not in item, item


@_needs_helm
def test_the_rotation_job_is_default_off() -> None:
    names = {doc.get("metadata", {}).get("name") for doc in _render()}
    assert "ne-private-payload-rotate" not in names


@_needs_helm
def test_rotation_is_a_one_shot_job_on_the_exact_first_party_image() -> None:
    """Not `kubectl exec` into a scaled-zero pod: a Job records its exact image
    digest and exit status, and there is no pod left to exec into anyway."""
    docs = _render("--set", "agentPrivatePayloadRotation.enabled=true")
    job = next(
        doc
        for doc in docs
        if doc.get("kind") == "Job" and doc["metadata"]["name"] == "ne-private-payload-rotate"
    )
    spec = job["spec"]
    assert spec["backoffLimit"] == 0  # one shot; never a silent retry
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    # EXACT image: the same digest-pinned first-party ref the roots run.
    api_image = next(
        c["image"]
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "ne-api"
        for c in doc["spec"]["template"]["spec"]["containers"]
        if c["name"] == "ne-api"
    )
    assert container["image"] == api_image
    assert "@sha256:" in container["image"]
    # It drives the A2 CLI, with the roots-stopped confirmation the CLI requires.
    assert container["command"][:3] == ["/app/.venv/bin/ne", "private-payload-keys", "rotate"]
    assert "--roots-stopped" in container["command"]
    # No key material in the Job either.
    for item in container.get("env", []):
        assert item.get("name") not in _ENVS or "value" not in item
