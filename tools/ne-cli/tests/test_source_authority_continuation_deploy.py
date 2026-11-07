# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE: deployment truth for the source-authority continuation key.

Two properties, and deliberately only two — the pilot-compose anchor and the Zarf
exclusion are owned by ``deploy/airgap/tests/test_static_harness.py`` and are not
restated here:

* **The exact required-secret contract.** Path ``pilot/source-authority-continuation``
  with fields ``hmac_key_b64`` and ``key_id``, resolved through the existing bundle
  SecretStore mechanism.
* **API and worker receive the byte-identical secret reference.** Two roots with
  different continuation keys is a silent failure: each refuses the other's
  outstanding tokens, which reads as flaky paging rather than as a split key.
  Asserted as an equality between the two RENDERED deployments.

Self-contained by convention -- `tools/ne-cli/tests/` has no conftest.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from ne.cli import main
from ne.cli.pilotcfg import REQUIRED_SECRETS, resolve_required_secrets
from ne.secrets.file_store import FileSecretStore

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"

SECRET_PATH = "pilot/source-authority-continuation"
KEY_ID_ENV = "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_ID"
KEY_B64_ENV = "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64"
_ENVS = (KEY_ID_ENV, KEY_B64_ENV)

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*extra: str) -> list[dict[str, Any]]:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI), *extra]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _deployment_env(docs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name") == name:
            merged: list[dict[str, Any]] = []
            for container in doc["spec"]["template"]["spec"]["containers"]:
                merged.extend(container.get("env", []))
            return merged
    raise AssertionError(f"no Deployment/{name}")


# --- the exact required-secret contract --------------------------------------


def test_the_continuation_key_is_a_required_secret_at_its_exact_path() -> None:
    assert REQUIRED_SECRETS[KEY_ID_ENV] == {"path": SECRET_PATH, "field": "key_id"}
    assert REQUIRED_SECRETS[KEY_B64_ENV] == {"path": SECRET_PATH, "field": "hmac_key_b64"}


def test_both_names_resolve_through_the_bundle_secret_store_mechanism(tmp_path: Path) -> None:
    """NAMES in the bundle, values only in the store — the C12 split, end to end."""
    sentinel_key = "S3NT1NEL-continuation-key-material"
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)

    async def _seed() -> None:
        await store.write("pilot/postgres", {"password": "pg"})
        await store.write("pilot/keycloak", {"admin_password": "kc"})
        await store.write("pilot/openfga", {"api_token": "of"})
        await store.write("pilot/agent-run-private-payload-kek", {"key_id": "k1", "kek": "kek"})
        await store.write(SECRET_PATH, {"key_id": "sa-k1", "hmac_key_b64": sentinel_key})

    asyncio.run(_seed())
    sec_toml = tmp_path / "ne-secrets.toml"
    sec_toml.write_text(f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8")
    bundle = tmp_path / "bundle"
    result = CliRunner().invoke(
        main,
        [
            "pilot",
            "promote",
            "--customer",
            "t",
            "--hostname",
            "authority.example.test",
            "--out",
            str(bundle),
            "--secrets-config",
            str(sec_toml),
        ],
    )
    assert result.exit_code == 0, result.output

    recorded = json.loads((bundle / "secrets" / "required.json").read_text(encoding="utf-8"))
    assert recorded[KEY_ID_ENV] == {"path": SECRET_PATH, "field": "key_id"}
    assert recorded[KEY_B64_ENV] == {"path": SECRET_PATH, "field": "hmac_key_b64"}
    for path in bundle.rglob("*"):
        if path.is_file():
            assert sentinel_key not in path.read_text(encoding="utf-8", errors="ignore"), path

    resolved = resolve_required_secrets(bundle)
    assert resolved[KEY_B64_ENV] == sentinel_key
    assert resolved[KEY_ID_ENV] == "sa-k1"


# --- Helm: one reference, two roots ------------------------------------------


@_needs_helm
def test_api_and_worker_receive_the_identical_continuation_secret_reference() -> None:
    docs = _render()
    api = [item for item in _deployment_env(docs, "ne-api") if item["name"] in _ENVS]
    worker = [item for item in _deployment_env(docs, "ne-worker") if item["name"] in _ENVS]
    assert {item["name"] for item in api} == set(_ENVS)
    # THE assertion: byte-identical references, so the two roots cannot honour
    # different continuation keys. Sorted because env ORDER may legitimately differ.
    assert sorted(api, key=lambda i: i["name"]) == sorted(worker, key=lambda i: i["name"])
    for item in api:
        ref = item["valueFrom"]["secretKeyRef"]
        assert ref["key"] == item["name"]
        assert ref["optional"] is True
        assert "value" not in item  # never an inline literal


@_needs_helm
def test_no_rendered_manifest_carries_continuation_key_material() -> None:
    docs = _render()
    assert KEY_B64_ENV in json.dumps(docs)  # the NAME is present...
    for doc in docs:
        if doc.get("kind") == "Secret":
            assert KEY_B64_ENV not in json.dumps(doc.get("stringData", {}))
            assert KEY_B64_ENV not in json.dumps(doc.get("data", {}))
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []):
            for item in container.get("env", []):
                if item.get("name") in _ENVS:
                    assert "value" not in item, item
