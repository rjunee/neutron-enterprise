# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The NE-071a Helm umbrella chart PARITY GUARDIAN.

Static tests (always run, no helm needed) assert the chart SOURCES byte-match
the pilot Compose tier (deploy/compose/pilot/compose.yaml is the executable
spec — C11 digests, bundled initdb SQL / OPA rego, C12 secret hygiene).

Render tests (per-test skipif shutil.which('helm') is None — mirrors
test_signing.py's cosign-skip precedent, but scoped to individual test
functions rather than a module-level `pytestmark` so the static guardians
above are NEVER skipped just because helm is absent from the environment)
run `helm template` against the CI fixture and assert the rendered manifests
carry the SAME hardening/env/image pins as the compose ne-api service, plus
the C11 fail-closed schema checks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
SERVE_COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "serve" / "compose.yaml"
PILOT_OTEL_CONFIG = (
    REPO_ROOT / "deploy" / "compose" / "pilot" / "otel" / "otel-collector-config.yaml"
)
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"
VALUES_SCHEMA = CHART_DIR / "values.schema.json"
VALUES_KIND_NOBRAIN = REPO_ROOT / "deploy" / "helm" / "tests" / "values-kind-nobrain.yaml"

_DIGEST_PIN = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")
_PILOT_POSTGRES_AGE_IMAGE = (
    "sha256:6ba66e01b43cbfde0f0a03699f609fed1e218612aab7b2119052e0fd5aeb4f26"
)
_PILOT_POSTGRES_AGE_SOURCE_REVISION = "7b7cede30d7af58ac3259b51cf59c76f1e95754c"

# NE-087 (O-4): the five NULL-valued NE_REBAC_* pass-through keys live ONLY on the
# compose x-ne-app-env anchor (the `ne up` --env-file mechanism). The Helm chart
# injects env via valueFrom secretKeyRef and does NOT carry the rebac wiring (helm
# ReBAC is a deferred follow-up — this unit is compose-only per the guardian
# decision), so they are excluded from the compose→chart env-key parity check.
_REBAC_COMPOSE_ONLY_KEYS = {
    "NE_REBAC_ENABLED",
    "NE_REBAC_API_URL",
    "NE_REBAC_STORE_ID",
    "NE_REBAC_AUTHORIZATION_MODEL_ID",
    "NE_REBAC_API_TOKEN",
}

# The persistent AGE substrate is currently a Pilot-only, first-party locally
# admitted artifact. Kubernetes still deploys the generic third-party Postgres
# image and must not receive graph configuration until registry promotion and
# the production graph composition unit land. Keep this exemption closed: any
# additional Compose-only key must fail both API and worker parity guardians.
_PILOT_ONLY_GRAPH_ENV = {
    "NE_BRAIN_GRAPH_DATABASE_URL": (
        "postgresql://ne:${NE_PILOT_POSTGRES_PASSWORD}@postgres:5432/neutron"
    ),
    "NE_BRAIN_GRAPH_LEDGER_URL": (
        "postgresql+psycopg://ne:${NE_PILOT_POSTGRES_PASSWORD}@postgres:5432/neutron"
    ),
    "NE_BRAIN_GRAPH_GRAPH_NAME": "ne_graph",
    # PRE-GRAPH-ON: the Tier-2 activation key, declared NULL-VALUED. Compose-only:
    # the chart has no graph substrate to address, so the Helm surface must not
    # render it at all — asserted by the rendered-key disjointness below.
    "NE_BRAIN_GRAPH_ENABLED": None,
}
# PRE-010G: the canonical default-Ask activation pin is Pilot-Compose-only. The
# chart gives BOTH deployments the transport generation but deliberately not this
# second half, so a Helm deployment composes ``default_ask_generation=""`` and
# build_platform holds the default Ask CLOSED. Dormant is the fail-closed state
# here, not an oversight: only the admitted Pilot manifest activates the path.
_DEFAULT_ASK_COMPOSE_ONLY_KEYS = {"NE_API_DEFAULT_ASK_GENERATION"}
_COMPOSE_ONLY_ENV_KEYS = (
    _REBAC_COMPOSE_ONLY_KEYS | set(_PILOT_ONLY_GRAPH_ENV) | _DEFAULT_ASK_COMPOSE_ONLY_KEYS
)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _compose_services() -> dict[str, dict]:
    return _load_yaml(COMPOSE_FILE)["services"]


def _chart_values() -> dict[str, Any]:
    return _load_yaml(CHART_DIR / "values.yaml")


def _tracked_helm_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "deploy/helm"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / rel for rel in out]


# ---------------------------------------------------------------------------
# Static tests — no helm binary required
# ---------------------------------------------------------------------------
def test_chart_layout_and_versions() -> None:
    umbrella = _load_yaml(CHART_DIR / "Chart.yaml")
    assert umbrella["apiVersion"] == "v2"
    assert umbrella["name"] == "neutron-enterprise"
    assert umbrella["kubeVersion"] == ">=1.31.0-0"

    dep_names = {dep["name"] for dep in umbrella["dependencies"]}
    assert dep_names == {
        "ne-api",
        "ne-worker",
        "postgres",
        "keycloak",
        "opa",
        "qdrant",
        "tei",
        "obs",
        "vllm",
    }
    # NE-071c: explicit per-chart condition map (core / brain / obs / serve).
    expected_conditions = {
        "ne-api": "profiles.core",
        "ne-worker": "profiles.core",
        "postgres": "profiles.core",
        "keycloak": "profiles.core",
        "opa": "profiles.core",
        "qdrant": "profiles.brain",
        "tei": "profiles.brain",
        "obs": "profiles.obs",
        "vllm": "profiles.serve.enabled",
    }
    for dep in umbrella["dependencies"]:
        assert dep["condition"] == expected_conditions[dep["name"]], dep["name"]
        chart_yaml = CHART_DIR / "charts" / dep["name"] / "Chart.yaml"
        assert chart_yaml.is_file(), f"missing subchart Chart.yaml for {dep['name']}"
        sub = _load_yaml(chart_yaml)
        assert sub["apiVersion"] == "v2"
        assert sub["name"] == dep["name"]


def test_agent_registry_chart_source_is_exact_repository_bytes() -> None:
    chart_bytes = (CHART_DIR / "files/agents/registry.toml").read_bytes()
    assert chart_bytes == (REPO_ROOT / "deploy/agents/registry.toml").read_bytes()
    for component in ("ne-api", "ne-worker"):
        assert (
            CHART_DIR / "charts" / component / "files/agents/registry.toml"
        ).read_bytes() == chart_bytes


def test_skill_bundle_chart_sources_are_exact_repository_bytes() -> None:
    source = REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md"
    chart_path = "files/skills/meridian-hardship-draft/SKILL.md"
    chart_bytes = (CHART_DIR / chart_path).read_bytes()
    assert chart_bytes == source.read_bytes()
    for component in ("ne-api", "ne-worker"):
        assert (CHART_DIR / "charts" / component / chart_path).read_bytes() == chart_bytes


def test_third_party_digests_byte_match_compose() -> None:
    services = _compose_services()
    values = _chart_values()
    # Pilot Postgres is deliberately no longer in this map: it is the
    # first-party locally admitted AGE artifact asserted by the next test.
    # Kubernetes retains the generic third-party Postgres pin until a separate
    # registry-promotion unit lands. Keycloak/OPA parity remains exact.
    pairs = {"keycloak": "keycloak", "opa": "opa"}
    for chart_key, compose_name in pairs.items():
        compose_image = services[compose_name]["image"]
        assert _DIGEST_PIN.match(compose_image), f"compose {compose_name} not digest-pinned"
        compose_repo_tag, compose_digest = compose_image.split("@", 1)
        image = values[chart_key]["image"]
        chart_image = f"{image['repository']}:{image['tag']}"
        assert chart_image == compose_repo_tag, (
            f"{chart_key}: chart image {chart_image!r} != compose {compose_repo_tag!r}"
        )
        assert image["digest"] == compose_digest, (
            f"{chart_key}: digest drift chart={image['digest']!r} compose={compose_digest!r}"
        )


def test_pilot_postgres_age_is_exact_first_party_local_artifact() -> None:
    document = _load_yaml(COMPOSE_FILE)
    services = document["services"]
    assert document["x-ne-postgres-age-image"] == _PILOT_POSTGRES_AGE_IMAGE
    assert document["x-ne-postgres-age-source-revision"] == _PILOT_POSTGRES_AGE_SOURCE_REVISION

    postgres = services["postgres"]
    admission = services["age-admission"]
    assert postgres["image"] == admission["image"] == _PILOT_POSTGRES_AGE_IMAGE
    assert postgres["platform"] == admission["platform"] == "linux/amd64"
    assert postgres["pull_policy"] == admission["pull_policy"] == "never"
    assert admission["environment"] == {
        "NE_PILOT_POSTGRES_PASSWORD": (
            "${NE_PILOT_POSTGRES_PASSWORD:?set via the config bundle (ne up --bundle)}"
        ),
        "NE_POSTGRES_AGE_IMAGE_DIGEST": _PILOT_POSTGRES_AGE_IMAGE,
        "NE_POSTGRES_AGE_SOURCE_REVISION": _PILOT_POSTGRES_AGE_SOURCE_REVISION,
    }
    assert "age-admission-data:/var/lib/neutron-age-admission" in admission["volumes"]

    # A bare local image ID is intentionally not a third-party registry pin.
    assert _DIGEST_PIN.fullmatch(postgres["image"]) is None
    chart_postgres = _chart_values()["postgres"]["image"]
    assert _DIGEST_PIN.fullmatch(
        f"{chart_postgres['repository']}:{chart_postgres['tag']}@{chart_postgres['digest']}"
    )
    assert "age" not in f"{chart_postgres['repository']}:{chart_postgres['tag']}".lower()


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_pilot_only_graph_env_exemption_is_closed(rendered: list[dict[str, Any]]) -> None:
    services = _compose_services()
    for service_name in ("ne-api", "ne-worker"):
        environment = services[service_name]["environment"]
        assert {
            key: environment[key] for key in environment if key.startswith("NE_BRAIN_GRAPH_")
        } == _PILOT_ONLY_GRAPH_ENV
        # Declared, and declared EMPTY. A literal here would activate Tier-2 for
        # every Compose deployment and move both roots' frozen config_hash.
        assert environment["NE_BRAIN_GRAPH_ENABLED"] is None

    for deployment_name in ("ne-api", "ne-worker"):
        deployment = _by_kind_name(rendered, "Deployment", deployment_name)
        rendered_keys = {entry["name"] for entry in _all_containers(deployment)[0]["env"]}
        assert rendered_keys.isdisjoint(_PILOT_ONLY_GRAPH_ENV)


def test_first_party_tag_single_source() -> None:
    values = _chart_values()
    compose_image = _compose_services()["ne-api"]["image"]
    chart_image = f"{values['global']['image']['repository']}:{values['global']['image']['tag']}"
    assert chart_image == compose_image == "ne-app:0.1.0"


def test_bundled_files_byte_match_pilot() -> None:
    pg_pilot = REPO_ROOT / "deploy" / "compose" / "pilot" / "initdb" / "01-databases.sql"
    pg_chart = CHART_DIR / "charts" / "postgres" / "files" / "01-databases.sql"
    assert pg_chart.read_bytes() == pg_pilot.read_bytes()

    opa_pilot = REPO_ROOT / "deploy" / "compose" / "pilot" / "opa" / "policies" / "ne_system.rego"
    opa_chart = CHART_DIR / "charts" / "opa" / "files" / "ne_system.rego"
    assert opa_chart.read_bytes() == opa_pilot.read_bytes()


# SAME-LINE only ([ \t]*, not \s*, around the ':'/'=') — unlike the compose
# hygiene scanner's single-line env files, rendered K8s YAML nests
# `valueFrom:\n  secretKeyRef:\n    name: ...` across lines; a `\s*` here
# would cross the newline after `secretKeyRef:` and false-flag the NEXT
# line's `name:` as this key's literal value.
_CRED_ASSIGNMENT = re.compile(
    r"(?im)^[^#\n]*?\b(?P<key>[A-Za-z0-9_]*(?:password|secret|token|credential|_key)"
    r"[A-Za-z0-9_]*)"
    r"[ \t]*[:=][ \t]*(?P<val>[^\s#]+)"
)

# Typed Agent budgets are numeric capacity limits, not authentication material.
# Keep this exact: a suffix/prefix/substring exemption would mask real token keys.
_NON_CREDENTIAL_TOKEN_BUDGET_KEYS = frozenset({"max_input_tokens", "max_output_tokens"})


def _literal_credential_assignments(text: str) -> list[tuple[str, str]]:
    """Mirrors test_pilot_secret_hygiene.py's scanner: a ${...}, a $(...), or a
    Helm template expression `{{ ... }}` (e.g. a chart-source `secretName:
    {{ .Values.global.ingress.tls.secretName }}` reference) is NOT a literal.

    A key ending in `...Name` (e.g. `secretName`, `existingSecretName`) is a
    reference to a Secret OBJECT's name, not the credential payload itself —
    this is the standard K8s convention (`spec.tls[].secretName`,
    `.Values.global.secrets.secretName`) and is names-only, matching this
    chart's own C12 posture (required.json stays names-only too). It is
    excluded the same way ${...}/$(...)/{{...}} are: it cannot leak a value.
    """
    offenders: list[tuple[str, str]] = []
    for match in _CRED_ASSIGNMENT.finditer(text):
        key = match.group("key")
        if key.lower().endswith("name"):
            continue
        if key.lower() in _NON_CREDENTIAL_TOKEN_BUDGET_KEYS:
            continue
        val = match.group("val").strip().strip("\"'")
        if val.startswith("${") or val.startswith("$(") or val.startswith("{{"):
            continue
        offenders.append((key, val))
    return offenders


def test_scanner_catches_credentials_but_classifies_only_exact_token_budgets_safe() -> None:
    planted = """password: literal-password
api_token = "literal-api-token"
token: literal-token
client_secret: literal-secret
service_credential: literal-credential
session_tokens: literal-session-tokens
max_input_token: literal-near-miss
"""
    expected = [
        ("password", "literal-password"),
        ("api_token", "literal-api-token"),
        ("token", "literal-token"),
        ("client_secret", "literal-secret"),
        ("service_credential", "literal-credential"),
        ("session_tokens", "literal-session-tokens"),
        ("max_input_token", "literal-near-miss"),
    ]
    assert _literal_credential_assignments(planted) == expected
    assert (
        _literal_credential_assignments("max_input_tokens = 24000\nmax_output_tokens: 4000\n") == []
    )
    # Secret-object names remain references, never credential payloads.
    assert _literal_credential_assignments("secretName: ne-platform-secrets\n") == []
    assert _literal_credential_assignments('secretName: ""\n') == []


def test_no_secret_literals_in_chart_sources() -> None:
    for path in _tracked_helm_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders = _literal_credential_assignments(text)
        assert offenders == [], f"literal secret assignment(s) in {path}: {offenders}"
        # valueFrom-refs are the ONLY sanctioned secret plumbing in the chart —
        # no bare secret value should ever appear even outside a KEY: VALUE line.
        assert "BEGIN PRIVATE KEY" not in text, path


# ---------------------------------------------------------------------------
# Render tests — require a local `helm` binary. Per-test (NOT module-level)
# skipif: a module-level `pytestmark` assignment marks EVERY test in the
# file, including the static byte-match/C11/C12 guardians above, which must
# run helm-independently in every environment (that's the whole point of
# splitting them out). Mirrors test_signing.py's cosign skip, scoped down.
# CI's helm-smoke.yml `render` job installs a pinned helm and asserts these
# tests EXECUTED (must-run-not-skip gate).
# ---------------------------------------------------------------------------
_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _rendered_docs() -> list[dict[str, Any]]:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _render_with(*extra: str) -> list[dict[str, Any]]:
    """Render the chart against the CI fixture plus extra `helm template` args
    (e.g. --set networkPolicy.enabled=true) — the flag/overlay render helper for
    the NE-071b prod-hardening tests."""
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI), *extra]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    return _rendered_docs()


def _by_kind_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"no {kind}/{name} in rendered output")


def _all_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return list(spec.get("containers", []))


def _all_pod_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return list(spec.get("initContainers", [])) + list(spec.get("containers", []))


@_needs_helm
def test_render_valid_and_kinds(rendered: list[dict[str, Any]]) -> None:
    kinds_names = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in rendered}
    assert ("Deployment", "ne-api") in kinds_names
    # NE-071b: the durable ne-worker rides the core profile (default render).
    assert ("Deployment", "ne-worker") in kinds_names
    # ...but exposes NO port, so it has no Service (run_admin_server=False).
    assert ("Service", "ne-worker") not in kinds_names
    assert ("Deployment", "opa") in kinds_names
    assert ("StatefulSet", "postgres") in kinds_names
    assert ("StatefulSet", "keycloak") in kinds_names
    assert ("Service", "ne-api") in kinds_names
    assert ("Service", "postgres") in kinds_names
    assert ("Service", "keycloak") in kinds_names
    assert ("Service", "opa") in kinds_names
    assert ("ConfigMap", "ne-platform-config") in kinds_names
    assert ("ConfigMap", "ne-agent-registry") in kinds_names
    assert ("ConfigMap", "ne-skill-bundle") in kinds_names
    assert ("Ingress", "neutron-enterprise") in kinds_names
    # C12 — installer-created only, Helm never renders a Secret.
    assert all(d.get("kind") != "Secret" for d in rendered)


@_needs_helm
def test_agent_registry_render_mount_env_and_bytes_match_both_roots(
    rendered: list[dict[str, Any]],
) -> None:
    registry = _by_kind_name(rendered, "ConfigMap", "ne-agent-registry")
    rendered_bytes = base64.b64decode(registry["binaryData"]["registry.toml"], validate=True)
    assert rendered_bytes == (REPO_ROOT / "deploy/agents/registry.toml").read_bytes()
    expected_hash = hashlib.sha256(rendered_bytes).hexdigest()
    for name in ("ne-api", "ne-worker"):
        deployment = _by_kind_name(rendered, "Deployment", name)
        assert (
            deployment["spec"]["template"]["metadata"]["annotations"][
                "neutron.dev/agent-registry-sha256"
            ]
            == expected_hash
        )
        container = _all_containers(deployment)[0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["NE_API_AGENT_MANIFEST_PATH"] == "/etc/neutron/agents/registry.toml"
        mount = next(item for item in container["volumeMounts"] if item["name"] == "agent-registry")
        assert mount == {
            "name": "agent-registry",
            "mountPath": "/etc/neutron/agents/registry.toml",
            "subPath": "registry.toml",
            "readOnly": True,
        }


@_needs_helm
def test_skill_bundle_render_mount_env_bytes_hash_and_api_readiness(
    rendered: list[dict[str, Any]],
) -> None:
    bundle = _by_kind_name(rendered, "ConfigMap", "ne-skill-bundle")
    rendered_bytes = base64.b64decode(
        bundle["binaryData"]["meridian-hardship-draft.SKILL.md"], validate=True
    )
    assert (
        rendered_bytes
        == (REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes()
    )
    expected_hash = hashlib.sha256(rendered_bytes).hexdigest()
    for name in ("ne-api", "ne-worker"):
        deployment = _by_kind_name(rendered, "Deployment", name)
        assert (
            deployment["spec"]["template"]["metadata"]["annotations"][
                "neutron.dev/skill-bundle-sha256"
            ]
            == expected_hash
        )
        container = _all_containers(deployment)[0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["NE_SKILLS_BUNDLE_DIR"] == "/etc/neutron/skills"
        assert env["NE_SKILL_BUNDLE_RAW_SHA256"] == expected_hash
        mount = next(item for item in container["volumeMounts"] if item["name"] == "skill-bundle")
        assert mount == {
            "name": "skill-bundle",
            "mountPath": "/etc/neutron/skills",
            "readOnly": True,
        }
    api = _by_kind_name(rendered, "Deployment", "ne-api")
    readiness = api["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert readiness == {
        "httpGet": {"path": "/healthz", "port": 8000},
        "periodSeconds": 10,
        "timeoutSeconds": 5,
        "failureThreshold": 3,
    }
    init = api["spec"]["template"]["spec"]["initContainers"]
    assert len(init) == 1
    assert init[0]["name"] == "wait-for-skill-delivery"
    assert init[0]["command"] == ["python", "-m", "ne.api.skills_delivery"]
    init_env = {item["name"]: item.get("value") for item in init[0]["env"]}
    assert init_env["NE_SKILL_BUNDLE_RAW_SHA256"] == expected_hash
    assert init_env["NE_FLYWHEEL_SIGNAL_DATABASE_URL"].endswith("@postgres:5432/neutron")


@_needs_helm
def test_agent_registry_raw_hash_moves_both_pod_templates(tmp_path: Path) -> None:
    changed_chart = tmp_path / "chart"
    shutil.copytree(CHART_DIR, changed_chart)
    changed_bytes = (CHART_DIR / "files/agents/registry.toml").read_bytes() + b" "
    for path in (
        changed_chart / "files/agents/registry.toml",
        changed_chart / "charts/ne-api/files/agents/registry.toml",
        changed_chart / "charts/ne-worker/files/agents/registry.toml",
    ):
        path.write_bytes(changed_bytes)
    result = subprocess.run(
        ["helm", "template", "ne", str(changed_chart), "-f", str(VALUES_CI)],
        capture_output=True,
        text=True,
        check=True,
    )
    changed = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    changed_hash = hashlib.sha256(changed_bytes).hexdigest()
    for name in ("ne-api", "ne-worker"):
        deployment = _by_kind_name(changed, "Deployment", name)
        assert (
            deployment["spec"]["template"]["metadata"]["annotations"][
                "neutron.dev/agent-registry-sha256"
            ]
            == changed_hash
        )


@_needs_helm
def test_agent_registry_hash_cannot_be_forged_by_values_override() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "ne",
            str(CHART_DIR),
            "-f",
            str(VALUES_CI),
            "--set",
            f"global.agentRegistry.rawSha256={'0' * 64}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "rawsha256" in result.stderr.lower()


@_needs_helm
def test_ne_api_env_keys_exactly_compose(rendered: list[dict[str, Any]]) -> None:
    compose_env = set(_compose_services()["ne-api"]["environment"]) - _COMPOSE_ONLY_ENV_KEYS
    expected = compose_env | {"NE_PILOT_POSTGRES_PASSWORD"}

    ne_api = _by_kind_name(rendered, "Deployment", "ne-api")
    container = _all_containers(ne_api)[0]
    rendered_keys = {e["name"] for e in container["env"]}
    assert rendered_keys == expected, (
        f"missing={expected - rendered_keys} extra={rendered_keys - expected}"
    )
    for entry in container["env"]:
        if entry["name"].endswith("_DATABASE_URL"):
            assert "$(NE_PILOT_POSTGRES_PASSWORD)" in entry["value"]
            assert "${" not in entry["value"]


@_needs_helm
def test_ne_api_hardening_parity(rendered: list[dict[str, Any]]) -> None:
    ne_api = _by_kind_name(rendered, "Deployment", "ne-api")
    pod_spec = ne_api["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert pod_spec["securityContext"]["runAsUser"] == 10001
    assert pod_spec["securityContext"]["runAsGroup"] == 10001

    container = _all_containers(ne_api)[0]
    assert container["image"] == (
        "ne-app:0.1.0@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    )
    sc = container["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"

    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert "/tmp" in mounts

    limits = container["resources"]["limits"]
    assert limits["memory"] == "2Gi"
    assert str(limits["cpu"]) == "2"

    assert container["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": 8000}
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"

    annotation = ne_api["spec"]["template"]["metadata"]["annotations"]["neutron.dev/config-hash"]
    assert annotation == "0" * 64


@_needs_helm
def test_every_container_hardened(rendered: list[dict[str, Any]]) -> None:
    workload_kinds = {"Deployment", "StatefulSet"}
    checked = 0
    for doc in rendered:
        if doc.get("kind") not in workload_kinds:
            continue
        pod_spec = doc["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsNonRoot"] is True, doc["metadata"]["name"]
        for container in pod_spec.get("containers", []):
            checked += 1
            sc = container["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, container["name"]
            assert sc["capabilities"]["drop"] == ["ALL"], container["name"]
            assert sc["seccompProfile"]["type"] == "RuntimeDefault", container["name"]
    assert checked == 8, (
        "expected 8 containers (ne-api/ne-worker/postgres/keycloak/opa + the "
        f"NE-071c brain qdrant/tei-embed/tei-rerank), got {checked}"
    )


@_needs_helm
def test_opa_telemetry_disabled_and_args(rendered: list[dict[str, Any]]) -> None:
    compose_args = _compose_services()["opa"]["command"]
    opa = _by_kind_name(rendered, "Deployment", "opa")
    container = _all_containers(opa)[0]
    assert container["args"] == compose_args
    assert "--disable-telemetry" in container["args"]


@_needs_helm
def test_no_secret_literals_in_rendered_output(rendered: list[dict[str, Any]]) -> None:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    offenders = _literal_credential_assignments(result.stdout)
    assert offenders == [], f"literal secret(s) in rendered output: {offenders}"


@_needs_helm
@pytest.mark.parametrize("protected", [False, True])
def test_no_embedded_url_credentials_in_any_rendered_container(protected: bool) -> None:
    overrides: tuple[str, ...] = ()
    if protected:
        overrides = (
            "--set-string",
            "global.agentTransport.generation=agent-transport-v1",
            "--set",
            "global.agentTransport.emptyBootstrap=true",
        )
    for doc in _render_with(*overrides):
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        for container in _all_pod_containers(doc):
            for env in container.get("env", []):
                value = env.get("value")
                if not isinstance(value, str):
                    continue
                embedded = re.search(r"//[^/\s]*:([^/@\s]+)@", value)
                assert embedded is None or embedded.group(1).startswith(("$(", "${")), (
                    doc["metadata"]["name"],
                    container["name"],
                    env["name"],
                )


@_needs_helm
def test_render_without_digest_fails_closed(tmp_path: Path) -> None:
    doc = _load_yaml(VALUES_CI)
    doc["global"]["image"]["digest"] = ""
    fixture = tmp_path / "no-digest.yaml"
    fixture.write_text(yaml.safe_dump(doc), encoding="utf-8")
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(fixture)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "digest" in result.stderr.lower()


@_needs_helm
def test_render_without_confighash_fails_closed(tmp_path: Path) -> None:
    doc = _load_yaml(VALUES_CI)
    doc["global"]["configHash"] = ""
    fixture = tmp_path / "no-confighash.yaml"
    fixture.write_text(yaml.safe_dump(doc), encoding="utf-8")
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(fixture)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "confighash" in result.stderr.lower() or "config_hash" in result.stderr.lower()


@_needs_helm
@pytest.mark.parametrize("mount_path", ["", "/tmp/registry.toml"])
def test_agent_registry_mount_path_schema_is_exact(tmp_path: Path, mount_path: str) -> None:
    doc = _load_yaml(VALUES_CI)
    doc.setdefault("global", {}).setdefault("agentRegistry", {})["mountPath"] = mount_path
    fixture = tmp_path / "invalid-agent-registry-path.yaml"
    fixture.write_text(yaml.safe_dump(doc), encoding="utf-8")
    result = subprocess.run(
        ["helm", "template", "ne", str(CHART_DIR), "-f", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "mountpath" in result.stderr.lower()


@_needs_helm
@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
def test_kubeconform_valid(rendered: list[dict[str, Any]]) -> None:
    template_cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI)]
    template = subprocess.run(template_cmd, capture_output=True, text=True, check=True)
    kubeconform_cmd = ["kubeconform", "-strict", "-kubernetes-version", "1.31.0", "-summary"]
    result = subprocess.run(
        kubeconform_cmd, input=template.stdout, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# NE-071b — ne-worker Deployment parity + prod-hardening (NetworkPolicy /
# OpenShift Route / backup CronJob) render guardians. The ne-worker env-parity
# tests are the SPEC-mandated extension of the ne-api env guardian.
# ---------------------------------------------------------------------------
def _worker_container(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _all_containers(_by_kind_name(docs, "Deployment", "ne-worker"))[0]


@_needs_helm
def test_ne_worker_env_keys_exactly_compose(rendered: list[dict[str, Any]]) -> None:
    # PyYAML SafeLoader resolves the `<<: *ne-app-env` merge key, so the compose
    # ne-worker environment dict already carries the full merged key-set.
    compose_env = set(_compose_services()["ne-worker"]["environment"]) - _COMPOSE_ONLY_ENV_KEYS
    expected = compose_env | {"NE_PILOT_POSTGRES_PASSWORD"}

    container = _worker_container(rendered)
    rendered_keys = {e["name"] for e in container["env"]}
    assert rendered_keys == expected, (
        f"missing={expected - rendered_keys} extra={rendered_keys - expected}"
    )
    for entry in container["env"]:
        if entry["name"].endswith("_DATABASE_URL"):
            assert "$(NE_PILOT_POSTGRES_PASSWORD)" in entry["value"]
            assert "${" not in entry["value"]


@_needs_helm
def test_ne_worker_shared_env_byte_matches_ne_api(rendered: list[dict[str, Any]]) -> None:
    # The anti-drift guardian that REPLACES a shared-template refactor: every
    # shared env entry must be byte-identical between the two deliberately-
    # duplicated templates (config_hash parity), so the only permitted
    # difference is the worker-only NE_WORKER_DBOS_DATABASE_URL tail entry.
    ne_api = _all_containers(_by_kind_name(rendered, "Deployment", "ne-api"))[0]
    api_env = {e["name"]: e for e in ne_api["env"]}
    worker_env = {e["name"]: e for e in _worker_container(rendered)["env"]}
    # The only permitted differences are the worker-only placement keys, which are
    # exactly the NE_WORKER_ prefix (NE_WORKER_DBOS_DATABASE_URL + the P2-21
    # NE_WORKER_OUTBOX_* SIEM-export knobs).
    shared = {k: v for k, v in worker_env.items() if not k.startswith("NE_WORKER_")}
    assert shared == api_env


@_needs_helm
def test_ne_worker_dbos_url_aligned(rendered: list[dict[str, Any]]) -> None:
    ne_api = _all_containers(_by_kind_name(rendered, "Deployment", "ne-api"))[0]
    api_env = {e["name"]: e["value"] for e in ne_api["env"] if "value" in e}
    worker_env = {e["name"]: e["value"] for e in _worker_container(rendered)["env"] if "value" in e}
    worker_dbos = worker_env["NE_WORKER_DBOS_DATABASE_URL"]
    assert worker_dbos == api_env["NE_API_DBOS_SYSTEM_DATABASE_URL"]
    assert worker_dbos.endswith("/ne_dbos")


@_needs_helm
def test_protected_empty_bootstrap_uses_external_root_databases_and_keeps_keycloak_db() -> None:
    docs = _render_with(
        "--set-string",
        "global.agentTransport.generation=agent-transport-v1",
        "--set",
        "global.agentTransport.emptyBootstrap=true",
    )
    kinds_names = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}
    assert ("StatefulSet", "postgres") in kinds_names
    assert ("Service", "postgres") in kinds_names

    api_containers = _all_pod_containers(_by_kind_name(docs, "Deployment", "ne-api"))
    api = next(container for container in api_containers if container["name"] == "ne-api")
    worker = _worker_container(docs)
    api_env = {item["name"]: item for item in api["env"]}
    worker_env = {item["name"]: item for item in worker["env"]}
    assert api_env["NE_AGENT_TRANSPORT_GENERATION"] == {
        "name": "NE_AGENT_TRANSPORT_GENERATION",
        "value": "agent-transport-v1",
    }
    assert worker_env["NE_AGENT_TRANSPORT_GENERATION"] == api_env["NE_AGENT_TRANSPORT_GENERATION"]
    for name in ("NE_RUNTIME_SESSION_DATABASE_URL", "NE_API_DBOS_SYSTEM_DATABASE_URL"):
        assert api_env[name]["valueFrom"] == worker_env[name]["valueFrom"]
        assert api_env[name]["valueFrom"]["secretKeyRef"] == {
            "name": "ne-platform-secrets",
            "key": name,
        }
        assert "value" not in api_env[name]
    assert worker_env["NE_WORKER_DBOS_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "ne-platform-secrets",
        "key": "NE_WORKER_DBOS_DATABASE_URL",
    }
    init = next(
        container for container in api_containers if container["name"] == "wait-for-skill-delivery"
    )
    init_env = {item["name"]: item for item in init["env"]}
    for name in ("NE_RUNTIME_SESSION_DATABASE_URL", "NE_API_DBOS_SYSTEM_DATABASE_URL"):
        assert init_env[name]["valueFrom"]["secretKeyRef"] == {
            "name": "ne-platform-secrets",
            "key": name,
        }


@_needs_helm
def test_default_ask_activation_is_live_overlay_only_and_helm_stays_dormant(
    rendered: list[dict[str, Any]],
) -> None:
    """Per-surface reconciliation of the PRE-010G pin, asserted in both directions.

    The Pilot base and Helm chart pin neither root, so both stay dormant until the
    generated live-writeback demo overlay supplies the canonical generation to the
    shared Compose service block. Helm retains the transport-generation positive
    control, proving the missing default-Ask key is a per-surface decision.
    """
    services = _compose_services()
    for service_name in ("ne-api", "ne-worker"):
        environment = services[service_name]["environment"]
        assert environment["NE_API_DEFAULT_ASK_GENERATION"] is None

    for deployment_name in ("ne-api", "ne-worker"):
        deployment = _by_kind_name(rendered, "Deployment", deployment_name)
        for container in _all_pod_containers(deployment):
            names = {entry["name"] for entry in container.get("env", [])}
            assert "NE_API_DEFAULT_ASK_GENERATION" not in names, deployment_name
        workload = next(
            container
            for container in _all_pod_containers(deployment)
            if container["name"] == deployment_name
        )
        workload_names = {entry["name"] for entry in workload["env"]}
        # Positive control: the chart DOES carry the OTHER half of the same readiness
        # gate, so the absence above is a per-surface decision rather than an empty
        # or unrendered env block.
        assert "NE_AGENT_TRANSPORT_GENERATION" in workload_names


@_needs_helm
def test_generation_env_tracks_every_schema_declared_value() -> None:
    schema = json.loads(VALUES_SCHEMA.read_text(encoding="utf-8"))
    declared_values = schema["properties"]["global"]["properties"]["agentTransport"]["properties"][
        "generation"
    ]["enum"]
    for declared in declared_values:
        docs = _render_with("--set-string", f"global.agentTransport.generation={declared}")
        for workload in ("ne-api", "ne-worker"):
            deployment = _by_kind_name(docs, "Deployment", workload)
            for container in _all_pod_containers(deployment):
                env = {item["name"]: item.get("value") for item in container.get("env", [])}
                if "NE_AGENT_TRANSPORT_GENERATION" in env:
                    assert env["NE_AGENT_TRANSPORT_GENERATION"] == declared


@_needs_helm
def test_agent_transport_bootstrap_schema_rejects_non_boolean(tmp_path: Path) -> None:
    doc = _load_yaml(VALUES_CI)
    doc.setdefault("global", {})["agentTransport"] = {
        "emptyBootstrap": "true",
    }
    fixture = tmp_path / "invalid-agent-transport.yaml"
    fixture.write_text(yaml.safe_dump(doc), encoding="utf-8")
    result = subprocess.run(
        ["helm", "template", "ne", str(CHART_DIR), "-f", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "emptybootstrap" in result.stderr.lower()


@_needs_helm
def test_every_rendered_in_cluster_postgres_reference_has_its_service() -> None:
    for docs in (_rendered_docs(), _render_with("--set", "profiles.core=false")):
        rendered = yaml.safe_dump_all(docs)
        internal_references = re.findall(r"(?:@|//|:)postgres:5432", rendered)
        service_names = {
            doc.get("metadata", {}).get("name") for doc in docs if doc.get("kind") == "Service"
        }
        assert bool(internal_references) == ("postgres" in service_names)


@_needs_helm
def test_ne_worker_hardening_and_probes(rendered: list[dict[str, Any]]) -> None:
    worker = _by_kind_name(rendered, "Deployment", "ne-worker")
    pod_spec = worker["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsUser"] == 10001
    assert pod_spec["securityContext"]["runAsGroup"] == 10001

    container = _all_containers(worker)[0]
    assert container["image"] == (
        "ne-app:0.1.0@sha256:0000000000000000000000000000000000000000000000000000000000000000"
    )
    assert container["command"] == ["python", "-m", "ne.worker"]
    assert "ports" not in container
    assert "readinessProbe" not in container

    startup = container["startupProbe"]
    assert startup["exec"]["command"] == ["python", "-m", "ne.worker", "--health"]
    assert startup["failureThreshold"] == 60
    assert "httpGet" not in startup
    liveness = container["livenessProbe"]
    assert liveness["exec"]["command"] == ["python", "-m", "ne.worker", "--health"]
    assert "httpGet" not in liveness

    limits = container["resources"]["limits"]
    assert limits["memory"] == "2Gi"
    assert str(limits["cpu"]) == "2"

    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert "/tmp" in mounts

    annotation = worker["spec"]["template"]["metadata"]["annotations"]["neutron.dev/config-hash"]
    assert annotation == "0" * 64
    assert worker["spec"]["replicas"] == 1


@_needs_helm
def test_optional_objects_absent_by_default(rendered: list[dict[str, Any]]) -> None:
    # NE-071d: "Schedule" (the Velero velero.io/v1 CR) joins the optional-kinds
    # set — the default render ships zero Velero objects (backup.velero.enabled
    # defaults false).
    optional = {"NetworkPolicy", "CronJob", "PersistentVolumeClaim", "Route", "Schedule"}
    present = {d.get("kind") for d in rendered}
    assert optional.isdisjoint(present), f"unexpected optional objects: {optional & present}"


@_needs_helm
def test_networkpolicy_render_behind_flag() -> None:
    docs = _render_with("--set", "networkPolicy.enabled=true")

    deny = _by_kind_name(docs, "NetworkPolicy", "neutron-default-deny")
    assert deny["spec"]["podSelector"] == {}
    assert sorted(deny["spec"]["policyTypes"]) == ["Egress", "Ingress"]

    allow_dns = _by_kind_name(docs, "NetworkPolicy", "allow-dns")
    assert allow_dns is not None

    ne_api = _by_kind_name(docs, "NetworkPolicy", "allow-ne-api")
    api_ing = ne_api["spec"]["ingress"][0]
    assert api_ing["ports"] == [{"port": 8000, "protocol": "TCP"}]
    ns_labels = api_ing["from"][0]["namespaceSelector"]["matchLabels"]
    assert ns_labels == {"kubernetes.io/metadata.name": "ingress-nginx"}

    worker = _by_kind_name(docs, "NetworkPolicy", "allow-ne-worker")
    assert "ingress" not in worker["spec"]
    worker_targets = {
        (e["to"][0]["podSelector"]["matchLabels"]["app"], e["ports"][0]["port"])
        for e in worker["spec"]["egress"]
    }
    assert ("postgres", 5432) in worker_targets
    assert ("opa", 8181) in worker_targets

    pg = _by_kind_name(docs, "NetworkPolicy", "allow-postgres")
    pg_from = pg["spec"]["ingress"][0]["from"][0]["podSelector"]["matchExpressions"][0]
    assert "ne-worker" in pg_from["values"]


@_needs_helm
def test_route_render_behind_flag() -> None:
    docs = _render_with("--set", "route.enabled=true")
    routes = [d for d in docs if d.get("kind") == "Route"]
    assert len(routes) == 2
    for r in routes:
        assert r["apiVersion"] == "route.openshift.io/v1"
        assert r["spec"]["tls"]["termination"] == "edge"
    by_host = {r["spec"]["host"]: r for r in routes}
    console = by_host["ci.local"]
    assert console["spec"]["to"]["name"] == "ne-api"
    assert console["spec"]["port"]["targetPort"] == 8000
    auth = by_host["auth.ci.local"]
    assert auth["spec"]["to"]["name"] == "keycloak"
    assert auth["spec"]["port"]["targetPort"] == 8080
    # Route SUPPRESSES the Ingress (mutually-exclusive front door).
    assert all(d.get("kind") != "Ingress" for d in docs)

    # ...and the DEFAULT render is the inverse: Ingress present, zero Routes.
    default = _rendered_docs()
    assert any(d.get("kind") == "Ingress" for d in default)
    assert all(d.get("kind") != "Route" for d in default)


@_needs_helm
def test_openshift_render_omits_fixed_uids() -> None:
    docs = _render_with("--set", "global.openshift=true")
    workloads = [d for d in docs if d.get("kind") in {"Deployment", "StatefulSet"}]
    # NE-071c: default render adds qdrant + tei-embed + tei-rerank (5 → 8).
    assert len(workloads) == 8
    for w in workloads:
        sc = w["spec"]["template"]["spec"]["securityContext"]
        assert sc["runAsNonRoot"] is True, w["metadata"]["name"]
        assert "runAsUser" not in sc, w["metadata"]["name"]
        assert "runAsGroup" not in sc, w["metadata"]["name"]
        assert "fsGroup" not in sc, w["metadata"]["name"]


@_needs_helm
def test_backup_cronjob_render_behind_flag() -> None:
    docs = _render_with("--set", "backup.enabled=true")
    cj = _by_kind_name(docs, "CronJob", "ne-backup")
    assert cj["spec"]["schedule"] == "0 2 * * *"

    pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 70

    container = pod["containers"][0]
    image = _chart_values()["postgres"]["image"]
    expected_image = f"{image['repository']}:{image['tag']}@{image['digest']}"
    assert container["image"] == expected_image

    env0 = container["env"][0]
    assert env0["name"] == "PGPASSWORD"
    assert env0["valueFrom"]["secretKeyRef"]["name"] == "ne-platform-secrets"
    assert "value" not in env0

    sc = container["securityContext"]
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"

    assert "pg_dumpall" in container["args"][0]

    pvc = _by_kind_name(docs, "PersistentVolumeClaim", "ne-backup-storage")
    assert pvc["spec"]["resources"]["requests"]["storage"] == "8Gi"


@_needs_helm
def test_route_and_backup_require_core_profile() -> None:
    # Codex NE-071b review (P2 x2): with profiles.core=false the ne-api/keycloak
    # Services and the postgres StatefulSet do not exist, so the Routes and the
    # pg_dumpall CronJob must NOT render (they would target nothing / fail
    # forever). Same gate pattern as templates/ingress.yaml and
    # templates/networkpolicy-allow.yaml. The backup PVC deliberately DOES still
    # render (data retention across a profile toggle — see backup-pvc.yaml).
    docs = _render_with(
        "--set",
        "profiles.core=false",
        "--set",
        "route.enabled=true",
        "--set",
        "backup.enabled=true",
    )
    kinds = {d.get("kind") for d in docs}
    assert "Route" not in kinds
    assert "CronJob" not in kinds
    assert "Ingress" not in kinds
    pvc = _by_kind_name(docs, "PersistentVolumeClaim", "ne-backup-storage")
    assert pvc is not None


@_needs_helm
@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
def test_flagged_render_kubeconform_valid() -> None:
    # Deliberately WITHOUT route.enabled — the Route CRD schema is absent from
    # the vanilla kubeconform schema set (route.openshift.io/v1 is an OpenShift
    # CRD), so -strict would false-fail on it.
    template_cmd = [
        "helm",
        "template",
        "ne",
        str(CHART_DIR),
        "-f",
        str(VALUES_CI),
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "backup.enabled=true",
        # NE-071c: the obs + serve optional profiles must ALSO be kubeconform-valid.
        "--set",
        "profiles.obs=true",
        "--set",
        "profiles.serve.enabled=true",
    ]
    template = subprocess.run(template_cmd, capture_output=True, text=True, check=True)
    kubeconform_cmd = ["kubeconform", "-strict", "-kubernetes-version", "1.31.0", "-summary"]
    result = subprocess.run(
        kubeconform_cmd, input=template.stdout, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# NE-071c — brain (qdrant + tei, default deploy) + obs (otel-collector) + serve
# (vllm) full-parity guardians. Static byte-match tests run helm-independently;
# the render tests are per-test _needs_helm scoped.
# ---------------------------------------------------------------------------
_VAR_DEFAULT = re.compile(r"\$\{[^:}]+:-([^}]*)\}")


def _serve_services() -> dict[str, dict]:
    return _load_yaml(SERVE_COMPOSE_FILE)["services"]


def _materialize(token: str) -> str:
    """Collapse a compose ${VAR:-default} token to its default (the value the
    Helm chart hard-codes from values)."""
    return _VAR_DEFAULT.sub(r"\1", token)


def _composed_pin(image_block: dict[str, str]) -> str:
    return f"{image_block['repository']}:{image_block['tag']}@{image_block['digest']}"


def test_new_subchart_digests_byte_match_compose() -> None:
    """C11: every NE-071c image pin byte-matches its compose source."""
    values = _chart_values()
    pilot = _compose_services()
    serve = _serve_services()

    # qdrant ← pilot compose.
    assert _composed_pin(values["qdrant"]["image"]) == pilot["qdrant"]["image"]

    # tei (shared pin) ← BOTH pilot tei-embed and tei-rerank.
    tei_pin = _composed_pin(values["tei"]["image"])
    assert tei_pin == pilot["tei-embed"]["image"]
    assert tei_pin == pilot["tei-rerank"]["image"]

    # obs ← pilot otel-collector.
    assert _composed_pin(values["obs"]["image"]) == pilot["otel-collector"]["image"]

    # vllm gpu/cpu ← serve compose vllm / vllm-cpu.
    assert _composed_pin(values["vllm"]["gpu"]["image"]) == serve["vllm"]["image"]
    assert _composed_pin(values["vllm"]["cpu"]["image"]) == serve["vllm-cpu"]["image"]


def test_obs_otel_config_byte_matches_pilot() -> None:
    chart_otel = CHART_DIR / "charts" / "obs" / "files" / "otel-collector-config.yaml"
    assert chart_otel.read_bytes() == PILOT_OTEL_CONFIG.read_bytes()


@_needs_helm
def test_default_render_includes_brain_and_excludes_optional(
    rendered: list[dict[str, Any]],
) -> None:
    kinds_names = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in rendered}
    for name in ("qdrant", "tei-embed", "tei-rerank"):
        assert ("StatefulSet", name) in kinds_names, name
        assert ("Service", name) in kinds_names, name
    # obs / serve are default-OFF — no doc named otel-collector or vllm.
    names = {d.get("metadata", {}).get("name") for d in rendered}
    assert "otel-collector" not in names
    assert "vllm" not in names


@_needs_helm
def test_qdrant_parity(rendered: list[dict[str, Any]]) -> None:
    values = _chart_values()
    sts = _by_kind_name(rendered, "StatefulSet", "qdrant")
    pod_spec = sts["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsUser"] == 1000
    assert pod_spec["securityContext"]["runAsGroup"] == 1000
    assert pod_spec["securityContext"]["fsGroup"] == 1000

    container = _all_containers(sts)[0]
    assert container["image"] == _composed_pin(values["qdrant"]["image"])
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["QDRANT__TELEMETRY_DISABLED"] == "true"
    # NE-071c-FU perms fix — init file on a writable emptyDir, upstream qdrant-helm pattern.
    assert env["QDRANT_INIT_FILE_PATH"] == "/qdrant/init/.qdrant-initialized"

    sc = container["securityContext"]
    assert sc["readOnlyRootFilesystem"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"

    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["tcpSocket"]["port"] == 6333, probe

    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert {"/qdrant/storage", "/qdrant/snapshots", "/qdrant/init"} <= mounts

    volumes = {v["name"]: v for v in pod_spec["volumes"]}
    assert "emptyDir" in volumes["qdrant-init"]

    vct_names = {v["metadata"]["name"] for v in sts["spec"]["volumeClaimTemplates"]}
    assert "qdrant-data" in vct_names

    limits = container["resources"]["limits"]
    assert limits["memory"] == "2Gi"
    assert str(limits["cpu"]) == "2"


@_needs_helm
def test_tei_parity(rendered: list[dict[str, Any]]) -> None:
    values = _chart_values()
    pilot = _compose_services()
    tei_pin = _composed_pin(values["tei"]["image"])
    cases = {
        "tei-embed": pilot["tei-embed"]["command"],
        "tei-rerank": pilot["tei-rerank"]["command"],
    }
    for name, compose_cmd in cases.items():
        sts = _by_kind_name(rendered, "StatefulSet", name)
        pod_spec = sts["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsUser"] == 1000
        assert pod_spec["securityContext"]["runAsGroup"] == 1000
        assert pod_spec["securityContext"]["fsGroup"] == 1000

        container = _all_containers(sts)[0]
        assert container["image"] == tei_pin, name
        # args byte-equal the compose --model-id command.
        assert container["args"] == compose_cmd, name

        env = {e["name"]: e["value"] for e in container["env"]}
        expected_env = {"HF_HUB_DISABLE_TELEMETRY", "HF_HUB_OFFLINE", "PORT"}
        # NE-RERANK-CPU: the reranker also pins RAYON_NUM_THREADS, because the TEI
        # image bakes in 8 and a raised cpu limit alone cannot lift it. The embedder
        # does not — it is not the search-path bottleneck and keeps the image default.
        if name == "tei-rerank":
            expected_env.add("RAYON_NUM_THREADS")
        assert set(env) == expected_env, name
        assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
        assert env["HF_HUB_OFFLINE"] == "0"
        assert env["PORT"] == "8080"

        sc = container["securityContext"]
        assert sc["readOnlyRootFilesystem"] is False

        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        assert {"/data", "/tmp"} <= mounts
        vct_names = {v["metadata"]["name"] for v in sts["spec"]["volumeClaimTemplates"]}
        assert "cache" in vct_names

        startup = container["startupProbe"]
        assert startup["tcpSocket"]["port"] == 8080
        assert startup["failureThreshold"] == 90

        limits = container["resources"]["limits"]
        assert limits["memory"] == "4Gi"
        # C11: the cpu limit is READ from compose, not a second hardcoded copy of
        # it — the chart claims a 1:1 reproduction and this is what makes that true
        # when the compose limit moves (NE-RERANK-CPU took tei-rerank 4 -> 12).
        compose_cpus = pilot[name]["deploy"]["resources"]["limits"]["cpus"]
        assert str(limits["cpu"]) == str(compose_cpus), name
        if name == "tei-rerank":
            # The rayon pool must match the quota, or the extra cores are unusable.
            assert env["RAYON_NUM_THREADS"] == str(compose_cpus)


@_needs_helm
def test_ne_api_brain_urls_resolve_in_cluster(rendered: list[dict[str, Any]]) -> None:
    ne_api = _all_containers(_by_kind_name(rendered, "Deployment", "ne-api"))[0]
    env = {e["name"]: e.get("value") for e in ne_api["env"]}
    assert env["NE_API_QDRANT_URL"] == "http://qdrant:6333"
    assert env["NE_API_TEI_URL"] == "http://tei-embed:80"

    qdrant_svc = _by_kind_name(rendered, "Service", "qdrant")
    qdrant_ports = {p["port"] for p in qdrant_svc["spec"]["ports"]}
    assert 6333 in qdrant_ports

    tei_svc = _by_kind_name(rendered, "Service", "tei-embed")
    tei_port = next(p for p in tei_svc["spec"]["ports"] if p["port"] == 80)
    assert tei_port["targetPort"] == 8080


@_needs_helm
def test_obs_render_behind_flag() -> None:
    values = _chart_values()
    docs = _render_with("--set", "profiles.obs=true")
    dep = _by_kind_name(docs, "Deployment", "otel-collector")
    pod_spec = dep["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsUser"] == 10001
    assert pod_spec["securityContext"]["runAsGroup"] == 10001

    container = _all_containers(dep)[0]
    assert container["image"] == _composed_pin(values["obs"]["image"])
    assert container["args"] == ["--config=/etc/otelcol/config.yaml"]

    sc = container["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    # the shared hardening trio (distroless still gets the container context).
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"

    # NO probes — documented distroless exemption.
    assert "startupProbe" not in container
    assert "readinessProbe" not in container
    assert "livenessProbe" not in container

    mount = next(
        m for m in container["volumeMounts"] if m["mountPath"] == "/etc/otelcol/config.yaml"
    )
    assert mount["subPath"] == "config.yaml"

    cm = _by_kind_name(docs, "ConfigMap", "otel-collector-config")
    assert cm["data"]["config.yaml"] == PILOT_OTEL_CONFIG.read_text(encoding="utf-8")

    svc = _by_kind_name(docs, "Service", "otel-collector")
    assert {p["port"] for p in svc["spec"]["ports"]} == {4317, 4318}


@_needs_helm
def test_serve_render_behind_flag() -> None:
    values = _chart_values()
    serve = _serve_services()
    docs = _render_with("--set", "profiles.serve.enabled=true")
    dep = _by_kind_name(docs, "Deployment", "vllm")
    container = _all_containers(dep)[0]
    assert container["image"] == _composed_pin(values["vllm"]["gpu"]["image"])

    expected_args = [_materialize(tok) for tok in serve["vllm"]["command"]]
    assert container["args"] == expected_args

    env = {e["name"]: e["value"] for e in container["env"]}
    for key in (
        "HF_HUB_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "VLLM_NO_USAGE_STATS",
        "DO_NOT_TRACK",
    ):
        assert env[key] == "1", key
    assert env["HOME"] == "/tmp"

    limits = container["resources"]["limits"]
    assert limits["nvidia.com/gpu"] == 1

    volumes = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    shm = volumes["shm"]["emptyDir"]
    assert shm["medium"] == "Memory"
    assert shm["sizeLimit"] == "16Gi"

    svc = _by_kind_name(docs, "Service", "vllm")
    assert {p["port"] for p in svc["spec"]["ports"]} == {8000}
    # the Service name 'vllm' matches the host in ne-api's gateway menu api_base.
    ne_api = _all_containers(_by_kind_name(docs, "Deployment", "ne-api"))[0]
    gateway = next(e["value"] for e in ne_api["env"] if e["name"] == "NE_GATEWAY_MODELS")
    assert "http://vllm:8000/v1" in gateway


@_needs_helm
def test_serve_cpu_mode_and_fail_closed() -> None:
    values = _chart_values()
    serve = _serve_services()
    docs = _render_with("--set", "profiles.serve.enabled=true", "--set", "vllm.mode=cpu")
    dep = _by_kind_name(docs, "Deployment", "vllm")
    container = _all_containers(dep)[0]
    assert container["image"] == _composed_pin(values["vllm"]["cpu"]["image"])

    expected_args = [_materialize(tok) for tok in serve["vllm-cpu"]["command"]]
    assert container["args"] == expected_args
    assert "/models/Qwen/Qwen3-0.6B" in container["args"]
    assert "qwen3-0.6b" in container["args"]

    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["VLLM_CPU_KVCACHE_SPACE"] == "4"

    limits = container["resources"].get("limits", {})
    assert "nvidia.com/gpu" not in limits

    # fail-closed on a bogus mode.
    cmd = [
        "helm",
        "template",
        "ne",
        str(CHART_DIR),
        "-f",
        str(VALUES_CI),
        "--set",
        "profiles.serve.enabled=true",
        "--set",
        "vllm.mode=bogus",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "mode" in result.stderr.lower()


@_needs_helm
def test_brain_toggle_off_removes_brain_objects() -> None:
    docs = _render_with("--set", "profiles.brain=false")
    names = {d.get("metadata", {}).get("name") for d in docs}
    for name in ("qdrant", "tei-embed", "tei-rerank"):
        assert name not in names, name
    # the sealed-install off-switch — ne-api still renders.
    assert ("Deployment", "ne-api") in {
        (d.get("kind"), d.get("metadata", {}).get("name")) for d in docs
    }


def test_kind_nobrain_overlay_is_one_knob() -> None:
    """Zarf-safety: the kind-smoke nobrain overlay must stay a single-knob file.

    It is passed ONLY in helm-smoke.yml and must never grow settings that could
    drift the zarf/compose brain posture.
    """
    assert _load_yaml(VALUES_KIND_NOBRAIN) == {"profiles": {"brain": False}}


@_needs_helm
def test_kind_nobrain_overlay_removes_brain_objects() -> None:
    docs = _render_with("-f", str(VALUES_KIND_NOBRAIN))
    names = {d.get("metadata", {}).get("name") for d in docs}
    for name in ("qdrant", "tei-embed", "tei-rerank"):
        assert name not in names, name
    assert ("Deployment", "ne-api") in {
        (d.get("kind"), d.get("metadata", {}).get("name")) for d in docs
    }


@_needs_helm
def test_networkpolicy_covers_new_services() -> None:
    docs = _render_with("--set", "networkPolicy.enabled=true")

    qdrant = _by_kind_name(docs, "NetworkPolicy", "allow-qdrant")
    assert qdrant["spec"]["ingress"][0]["ports"] == [{"port": 6333, "protocol": "TCP"}]

    for name in ("allow-tei-embed", "allow-tei-rerank"):
        pol = _by_kind_name(docs, "NetworkPolicy", name)
        assert pol["spec"]["ingress"][0]["ports"] == [{"port": 8080, "protocol": "TCP"}], name

    # the existing ne-api egress rules to tei now target pod port 8080, NOT 80.
    ne_api = _by_kind_name(docs, "NetworkPolicy", "allow-ne-api")
    tei_ports: set[int] = set()
    for rule in ne_api["spec"]["egress"]:
        app = rule["to"][0]["podSelector"]["matchLabels"].get("app")
        if app in ("tei-embed", "tei-rerank"):
            tei_ports |= {p["port"] for p in rule["ports"]}
    assert tei_ports == {8080}, tei_ports

    # obs + serve allows appear only under their gates.
    flagged = _render_with(
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "profiles.obs=true",
        "--set",
        "profiles.serve.enabled=true",
    )
    otel = _by_kind_name(flagged, "NetworkPolicy", "allow-otel-collector")
    otel_ports = {p["port"] for p in otel["spec"]["ingress"][0]["ports"]}
    assert otel_ports == {4317, 4318}
    vllm = _by_kind_name(flagged, "NetworkPolicy", "allow-vllm")
    assert vllm["spec"]["ingress"][0]["ports"] == [{"port": 8000, "protocol": "TCP"}]


# ---------------------------------------------------------------------------
# NE-071d — Velero Schedule templating (per-profile RPO/RTO)
#
# Closes the literal SPEC NE-071 wording ("Velero + DB-native backups with
# RPO/RTO per profile"): templates/velero-schedule.yaml adds a velero.io/v1
# Schedule CR (default-OFF via backup.velero.enabled) that references the
# cluster-level Velero already provisioned by deploy/terraform-azure/velero.tf
# — it never installs/re-provisions Velero itself. The pg_dumpall CronJob
# (templates/backup-cronjob.yaml) remains the DB-native half and is untouched.
#
# EXCLUDED from both kubeconform tests (test_kubeconform_valid and
# test_flagged_render_kubeconform_valid): velero.io/v1 is a CRD absent from
# the vanilla kubeconform schema set — same reasoning as the Route exclusion
# above (route.openshift.io/v1). Neither kubeconform test sets
# backup.velero.enabled.
# ---------------------------------------------------------------------------


@_needs_helm
def test_velero_schedule_render_behind_flag() -> None:
    docs = _render_with("--set", "backup.velero.enabled=true")

    sched = _by_kind_name(docs, "Schedule", "ne-velero-default")
    assert sched["apiVersion"] == "velero.io/v1"
    assert sched["metadata"]["namespace"] == "velero"
    # production is the default profile.
    assert sched["spec"]["schedule"] == "0 */6 * * *"

    tmpl = sched["spec"]["template"]
    assert tmpl["ttl"] == "336h0m0s"
    assert tmpl["includedNamespaces"] == ["default"]
    assert tmpl["storageLocation"] == "default"

    hook = tmpl["hooks"]["resources"][0]
    assert hook["labelSelector"]["matchLabels"] == {"app": "postgres"}
    ex = hook["pre"][0]["exec"]
    assert ex["container"] == "postgres"
    assert ex["command"][0] == "psql"
    assert "CHECKPOINT" in ex["command"]
    assert ex["onError"] == "Continue"

    # The two backup halves are independent: backup.enabled stays false here,
    # so no CronJob/PVC co-render alongside the Schedule.
    kinds = {d.get("kind") for d in docs}
    assert "CronJob" not in kinds
    assert "PersistentVolumeClaim" not in kinds

    # ...and the DEFAULT render (no --set at all) has no Schedule.
    assert all(d.get("kind") != "Schedule" for d in _rendered_docs())


@_needs_helm
def test_velero_pilot_profile_expresses_stated_rpo() -> None:
    docs = _render_with(
        "--set", "backup.velero.enabled=true", "--set", "backup.velero.profile=pilot"
    )
    sched = _by_kind_name(docs, "Schedule", "ne-velero-default")
    assert sched["spec"]["schedule"] == "0 3 * * *"
    assert sched["spec"]["template"]["ttl"] == "168h0m0s"


@_needs_helm
def test_velero_unknown_profile_fails_closed() -> None:
    cmd = [
        "helm",
        "template",
        "ne",
        str(CHART_DIR),
        "-f",
        str(VALUES_CI),
        "--set",
        "backup.velero.enabled=true",
        "--set",
        "backup.velero.profile=hourly",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "backup.velero.profile" in result.stderr


@_needs_helm
def test_velero_schedule_independent_of_core_profile() -> None:
    # Namespace DR is content-agnostic — the Schedule renders even with
    # profiles.core=false (the postgres CHECKPOINT hook simply no-ops via
    # onError: Continue when there's no postgres pod to hit).
    docs = _render_with("--set", "backup.velero.enabled=true", "--set", "profiles.core=false")
    sched = _by_kind_name(docs, "Schedule", "ne-velero-default")
    assert sched is not None


# ---------------------------------------------------------------------------
# NE-DR1 — snapshotVolumes contract (default-false + fail-closed DB exclusion)
#
# The Schedule now renders snapshotVolumes EXPLICITLY (default false — kills
# Velero's dangerous nil=true default). Opting into crash-consistent NON-DB PV
# snapshots while in-cluster postgres (profiles.core) is enabled FAILS CLOSED
# until the operator labels the DB PVC out of snapshots and attests via
# backup.velero.dbSnapshotExclusionConfirmed=true (SPEC §11 DR posture). The
# VSL itself (aws EBS / azure disk) lives in the infra terraform, not the chart.
# ---------------------------------------------------------------------------


@_needs_helm
def test_velero_snapshot_volumes_default_false() -> None:
    docs = _render_with("--set", "backup.velero.enabled=true")
    sched = _by_kind_name(docs, "Schedule", "ne-velero-default")
    tmpl = sched["spec"]["template"]
    assert tmpl["snapshotVolumes"] is False
    assert "volumeSnapshotLocations" not in tmpl


@_needs_helm
def test_velero_snapshot_optin_fails_closed_with_incluster_pg() -> None:
    # profiles.core defaults true (in-cluster postgres); opting into snapshots
    # without the exclusion attestation must abort the render.
    cmd = [
        "helm",
        "template",
        "ne",
        str(CHART_DIR),
        "-f",
        str(VALUES_CI),
        "--set",
        "backup.velero.enabled=true",
        "--set",
        "backup.velero.snapshotVolumes=true",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "dbSnapshotExclusionConfirmed" in result.stderr


@_needs_helm
def test_velero_snapshot_optin_renders_with_confirmation() -> None:
    docs = _render_with(
        "--set",
        "backup.velero.enabled=true",
        "--set",
        "backup.velero.snapshotVolumes=true",
        "--set",
        "backup.velero.dbSnapshotExclusionConfirmed=true",
    )
    sched = _by_kind_name(docs, "Schedule", "ne-velero-default")
    tmpl = sched["spec"]["template"]
    assert tmpl["snapshotVolumes"] is True
    assert tmpl["volumeSnapshotLocations"] == ["default"]


@_needs_helm
def test_velero_snapshot_optin_no_core_needs_no_confirmation() -> None:
    # No in-cluster DB (profiles.core=false) ⇒ no live DB volume to protect, so
    # opting into snapshots needs no attestation.
    docs = _render_with(
        "--set",
        "profiles.core=false",
        "--set",
        "backup.velero.enabled=true",
        "--set",
        "backup.velero.snapshotVolumes=true",
    )
    sched = _by_kind_name(docs, "Schedule", "ne-velero-default")
    tmpl = sched["spec"]["template"]
    assert tmpl["snapshotVolumes"] is True
    assert tmpl["volumeSnapshotLocations"] == ["default"]
