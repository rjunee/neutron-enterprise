# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static guardian for the Flowise optional add-on overlay (NE-088).

No docker daemon needed: the compose YAML is parsed with PyYAML and asserted
against the NE-088 add-on contract (OFF-BY-DEFAULT, C11 digest pin >= the
CVE-2025-59528 fix floor, C4 EE-exclusion, C2 loopback/in-network, C12 secret
interpolations, hardening, telemetry-off, customMCP disabled).

This file duplicates the off-by-default proof (pilot ships no flowise service)
locally so the EXISTING pilot exact-match guardian (test_pilot_stack.py) stays
UNTOUCHED and green — this unit adds a service in a SEPARATE compose project and
never edits the pilot compose or its guardian.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_COMPOSE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
FLOWISE_COMPOSE = REPO_ROOT / "deploy" / "compose" / "flowise" / "compose.yaml"
FLOWISE_ENV_EXAMPLE = REPO_ROOT / "deploy" / "compose" / "flowise" / ".env.example"
HELM_CHART = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
HELM_VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"

# The commercial enterprise-edition activation env vars (verified against
# docker/.env.example at tag flowise@3.1.3). Setting ANY of them activates the
# EE surface — this profile must set NONE (C4).
_EE_ACTIVATION_VARS = {"FLOWISE_EE_LICENSE_KEY", "LICENSE_URL", "OFFLINE"}

# CVE-2025-59528 fix floor (the patched version >= 3.0.6).
_FIX_FLOOR = (3, 0, 6)

_IMAGE_PIN = re.compile(r"^flowiseai/flowise:3\.[0-9.]+@sha256:[0-9a-f]{64}$")

# The pilot cred regex (reused verbatim). Two Flowise env keys match it but name
# WHERE/HOW the encryption key is stored (a storage-type enum + a filesystem path),
# NOT secret material — the actual key material is FLOWISE_SECRETKEY_OVERWRITE,
# which IS an interpolation. Those two config pointers are allow-listed below.
_CRED = re.compile(r"(?i)(password|secret|token|_key)")
_NON_SECRET_CONFIG_KEYS = {"SECRETKEY_STORAGE_TYPE", "SECRETKEY_PATH"}


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _pilot() -> dict:
    return _load(PILOT_COMPOSE)


def _flowise() -> dict:
    return _load(FLOWISE_COMPOSE)


def _flowise_service() -> dict:
    return _flowise()["services"]["flowise"]


def _flowise_env() -> dict:
    return _flowise_service().get("environment", {})


# --- ⭐ off-by-default proof (duplicated so the pilot guardian stays untouched) --


def test_default_pilot_compose_has_no_flowise() -> None:
    data = _pilot()
    services = data["services"]
    assert not any("flowise" in name for name in services), (
        "the default pilot compose must ship NO flowise service (off-by-default)"
    )
    includes = data["include"]
    paths = [entry["path"] if isinstance(entry, dict) else entry for entry in includes]
    assert paths == ["../serve/compose.yaml"], (
        "the pilot include list must be exactly the serve tier (no flowise overlay wiring)"
    )


# --- the overlay contract ---------------------------------------------------


def test_flowise_overlay_defines_only_flowise() -> None:
    data = _flowise()
    assert set(data["services"]) == {"flowise"}
    assert data["name"] == "ne-flowise"


def test_flowise_service_is_profile_gated() -> None:
    assert _flowise_service()["profiles"] == ["flowise"]


def test_flowise_image_digest_pinned() -> None:
    image = _flowise_service()["image"]
    assert _IMAGE_PIN.match(image), f"flowise image not tag@sha256 pinned: {image}"
    tag = image.split("@")[0].split(":")[1]
    version = tuple(int(part) for part in tag.split("."))
    assert version >= _FIX_FLOOR, (
        f"flowise {tag} is below the CVE-2025-59528 fix floor {_FIX_FLOOR}"
    )


# GHSA-8gj2-2cvc-6xx7 fix floor — unauthenticated /api/v1/text-to-speech/generate
# TTS credential abuse; patched 3.1.4 (no CVE id). NOT covered by H1
# DISABLED_NODES (whitelisted REST controller, not a flow node), so the version
# floor is the only real fix — a pin revert below 3.1.4 reintroduces it.
_TTS_CRED_ABUSE_FIX_FLOOR = (3, 1, 4)


def test_flowise_pin_at_or_above_ghsa_8gj2_tts_fix() -> None:
    """GHSA-8gj2-2cvc-6xx7: the pinned tag must be >= 3.1.4.

    H1 does not cover this whitelisted-REST-controller bug, so the version floor
    IS the fix.
    """
    image = _flowise_service()["image"]
    tag = image.split("@")[0].split(":")[1]
    version = tuple(int(part) for part in tag.split("."))
    assert version >= _TTS_CRED_ABUSE_FIX_FLOOR, (
        f"flowise {tag} is below the GHSA-8gj2-2cvc-6xx7 TTS credential-abuse "
        f"fix floor {_TTS_CRED_ABUSE_FIX_FLOOR}"
    )


def test_flowise_loopback_only() -> None:
    for port in _flowise_service().get("ports", []):
        spec = port if isinstance(port, str) else str(port)
        assert spec.startswith("127.0.0.1:"), f"flowise must publish loopback-only, got {spec!r}"


def test_flowise_hardened() -> None:
    cfg = _flowise_service()
    assert cfg.get("security_opt") == ["no-new-privileges:true"]
    assert cfg.get("cap_drop") == ["ALL"]
    assert "cap_add" not in cfg
    assert cfg.get("restart") == "unless-stopped"
    limits = cfg.get("deploy", {}).get("resources", {}).get("limits", {})
    assert "memory" in limits and "cpus" in limits, "flowise missing resource limits"
    assert "healthcheck" in cfg, "flowise missing healthcheck"


def test_flowise_ee_surface_excluded() -> None:
    env = _flowise_env()
    assert _EE_ACTIVATION_VARS.isdisjoint(env), (
        f"EE activation env vars present: {_EE_ACTIVATION_VARS & set(env)}"
    )
    # Belt-and-braces: no 'FLOWISE_EE' surface text in any NON-COMMENT line (the
    # license-boundary comment names it deliberately; live config must not).
    live = "\n".join(
        line
        for line in FLOWISE_COMPOSE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )
    assert "FLOWISE_EE" not in live, "FLOWISE_EE surface referenced outside comments"


def test_flowise_telemetry_off() -> None:
    env = _flowise_env()
    assert env.get("DISABLE_FLOWISE_TELEMETRY") == "true"
    assert "POSTHOG_PUBLIC_API_KEY" not in env


# (H1) The FULL dynamic-code-execution / arbitrary-eval node class — every
# `this.name` verified file-by-file at tag flowise@3.1.3 (do NOT retype; a mistyped
# identifier silently no-ops its disable, note the literal capital-C ChatflowTool).
# A future sibling-node CVE in this class is pre-disabled; any accidental list
# shrink fails CI.
_CODE_EXEC_NODES: frozenset[str] = frozenset(
    {
        # tools/MCP/{CustomMCP,CustomMcpServerTool} — arbitrary MCP server config /
        # stdio spawn (CVE-2025-59528 / 2026-40933 / 2026-58057).
        "customMCP",
        "customMcpServerTool",
        # tools/CustomTool — user-authored JS tool.
        "customTool",
        # utilities/{CustomFunction,IfElseFunction} — user JS.
        "customFunction",
        "ifElseFunction",
        # agentflow/CustomFunction — user JS (Agentflow v1 custom function).
        "customFunctionAgentflow",
        # documentloaders/CustomDocumentLoader — user JS.
        "customDocumentLoader",
        # tools/CodeInterpreterE2B — remote code exec via external E2B SaaS
        # (also a C2 egress-to-external-SaaS vector).
        "codeInterpreterE2B",
        # prompts/ChatPromptTemplate — optional messageHistoryCode user JS (plain
        # promptTemplate remains available to flow authors).
        "chatPromptTemplate",
        # tools/OpenAPIToolkit — customCode user JS.
        "openAPIToolkit",
        # tools/AgentAsTool + tools/ChatflowTool — interpolate chat input into
        # sandbox-executed JS fetch templates (CVE-2026-41264-style injection shape).
        "agentAsTool",
        "ChatflowTool",
        # The ENTIRE deprecated sequentialagents category: seqAgent/seqLLMNode/
        # seqToolNode/seqState/seqCondition/seqConditionAgent/seqCustomFunction/
        # seqExecuteFlow call executeJavaScriptCode via commonUtils; seqStart/seqEnd/
        # seqLoop are useless without them — categorical disable (future-sibling-CVE).
        "seqAgent",
        "seqCondition",
        "seqConditionAgent",
        "seqCustomFunction",
        "seqEnd",
        "seqExecuteFlow",
        "seqLLMNode",
        "seqLoop",
        "seqStart",
        "seqState",
        "seqToolNode",
    }
)


def test_flowise_code_exec_node_class_fully_disabled() -> None:
    """(H1) DISABLED_NODES must cover the FULL code-exec node class, not just
    customMCP — so a future sibling-node CVE in this class is pre-disabled."""
    raw = _flowise_env().get("DISABLED_NODES", "")
    disabled = raw.split(",")
    # No empty / whitespace-only entries (a stray comma would silently no-op).
    assert all(item and item == item.strip() for item in disabled), (
        f"DISABLED_NODES has empty/whitespace items: {disabled!r}"
    )
    disabled_set = set(disabled)
    assert disabled_set >= _CODE_EXEC_NODES, (
        f"DISABLED_NODES is missing code-exec nodes: {_CODE_EXEC_NODES - disabled_set}"
    )
    # CVE-2025-59528 anchor — the original KEV RCE node must still be present.
    assert "customMCP" in disabled_set


def test_flowise_secrets_are_interpolations() -> None:
    for key, value in _flowise_env().items():
        if _CRED.search(key) and key not in _NON_SECRET_CONFIG_KEYS:
            assert isinstance(value, str) and value.startswith("${"), (
                f"flowise.{key} must be a ${{...}} interpolation (C12), got {value!r}"
            )


def test_flowise_rides_pilot_network() -> None:
    networks = _flowise()["networks"]
    assert networks["default"] == {"name": "ne-pilot_default", "external": True}


def test_flowise_data_on_named_volume() -> None:
    cfg = _flowise_service()
    assert "flowise-data:/data" in cfg.get("volumes", [])
    assert _flowise()["volumes"].get("flowise-data") in ({}, None)
    assert _flowise_env().get("DATABASE_PATH", "").startswith("/data")


# --- ⭐ H2: network isolation (tightest ENFORCEABLE compose posture) ---------


def test_flowise_publishes_no_ports_beyond_loopback() -> None:
    """(H2) EXACTLY one published port and it is loopback-bound — no off-box
    surface, nothing published beyond 127.0.0.1."""
    ports = _flowise_service().get("ports", [])
    assert len(ports) == 1, f"flowise must publish exactly one port, got {ports!r}"
    spec = ports[0] if isinstance(ports[0], str) else str(ports[0])
    assert spec.startswith("127.0.0.1:"), f"flowise must publish loopback-only, got {spec!r}"


def test_flowise_no_egress_grants() -> None:
    """(H2) The overlay grants no extra egress lanes: no extra_hosts / dns /
    network_mode, and the ONLY network is the external pilot network."""
    cfg = _flowise_service()
    for key in ("extra_hosts", "dns", "dns_search", "network_mode"):
        assert key not in cfg, f"flowise must not set {key} (unaudited egress lane)"
    # If the service pins `networks:` it may only reference the project default.
    svc_networks = cfg.get("networks")
    assert svc_networks in (None, ["default"], {"default": None}), (
        f"flowise must not join extra networks, got {svc_networks!r}"
    )
    assert _flowise()["networks"] == {"default": {"name": "ne-pilot_default", "external": True}}, (
        "the overlay's only network must be the external ne-pilot_default"
    )


# Sealed-stack files that must carry NO flowise reverse-proxy / egress / allowlist
# entry — the pilot stack must be provably unaware of the add-on.
def _sealed_stack_files() -> list[Path]:
    files: list[Path] = [
        REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml",
        REPO_ROOT / "deploy" / "compose" / "serve" / "compose.yaml",
    ]
    caddy_dir = REPO_ROOT / "deploy" / "compose" / "pilot" / "caddy"
    files.extend(sorted(p for p in caddy_dir.glob("Caddyfile*") if p.is_file()))
    for sub in ("opa", "otel"):
        base = REPO_ROOT / "deploy" / "compose" / "pilot" / sub
        files.extend(sorted(p for p in base.rglob("*") if p.is_file()))
    return files


def test_sealed_stack_has_no_flowise_egress_lane() -> None:
    """(H2) No non-comment 'flowise' reference anywhere in the sealed pilot/serve
    compose, Caddyfile(s), opa/, or otel/ configs — the sealed stack carries no
    flowise proxy / egress / allowlist entry."""
    offenders: list[str] = []
    for path in _sealed_stack_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "flowise" in stripped.lower():
                offenders.append(f"{path}:{lineno}: {line}")
    assert not offenders, "sealed stack references flowise:\n" + "\n".join(offenders)


# --- ⭐ H3: explicit accept-the-risk enable-gate ------------------------------


def test_flowise_accept_risk_gate_required_interpolation() -> None:
    """(H3) NE_FLOWISE_ACCEPT_CVE_RISK is a REQUIRED ${...:?} interpolation, so
    `docker compose --profile flowise config` refuses to even render without it."""
    value = _flowise_env().get("NE_FLOWISE_ACCEPT_CVE_RISK", "")
    assert re.match(r"^\$\{NE_FLOWISE_ACCEPT_CVE_RISK:\?", value), (
        f"the ack must be a required :? interpolation, got {value!r}"
    )


def test_flowise_entrypoint_refuses_without_ack() -> None:
    """(H3) The /bin/sh -c entrypoint wrapper compares the RUNTIME env to the exact
    string 'true', exits nonzero with a loud message, and exec's flowise start."""
    entrypoint = _flowise_service().get("entrypoint")
    assert isinstance(entrypoint, list) and entrypoint[0:2] == ["/bin/sh", "-c"], (
        f"expected a /bin/sh -c entrypoint wrapper, got {entrypoint!r}"
    )
    script = entrypoint[2]
    # YAML does NOT interpret `$$`, so the parsed script keeps the compose-escaped
    # `$$NE_FLOWISE_ACCEPT_CVE_RISK` verbatim (compose collapses it to a single `$`
    # at deploy time so the SHELL reads the container env at runtime).
    assert "$$NE_FLOWISE_ACCEPT_CVE_RISK" in script, (
        "entrypoint must read the runtime env via the compose-escaped $$ form"
    )
    assert '!= "true"' in script, "entrypoint must require the exact string true"
    assert "exit" in script, "entrypoint must exit nonzero when the ack is absent"
    assert "exec flowise start" in script, (
        "entrypoint must exec the upstream `flowise start` on success (PID 1)"
    )


def test_flowise_env_example_documents_ack() -> None:
    """(H3) .env.example ships the ack line and documents the KEV CVE."""
    text = FLOWISE_ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"(?m)^NE_FLOWISE_ACCEPT_CVE_RISK=", text), (
        ".env.example must ship a NE_FLOWISE_ACCEPT_CVE_RISK= line"
    )
    assert "CVE-2025-59528" in text, ".env.example must name the KEV CVE"


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return probe.returncode == 0


_needs_compose = pytest.mark.skipif(
    not _docker_compose_available(),
    reason="docker compose CLI not available",
)

# The five Flowise secret interpolations that also gate `config` — supply dummies
# so ONLY the ack-gate governs the pass/fail of the H3 live check.
_DUMMY_SECRETS = {
    "NE_FLOWISE_SECRETKEY": "x",
    "NE_FLOWISE_JWT_AUTH_SECRET": "x",
    "NE_FLOWISE_JWT_REFRESH_SECRET": "x",
    "NE_FLOWISE_SESSION_SECRET": "x",
    "NE_FLOWISE_TOKEN_HASH_SECRET": "x",
}


@_needs_compose
def test_flowise_profile_without_ack_does_not_start() -> None:
    """(H3) THE proof: `docker compose --profile flowise config` with the ack
    ABSENT exits nonzero naming the variable; with the ack + dummy secrets it
    renders — the profile without the ack provably cannot come up (config-time,
    no daemon). The env dict is built from scratch (NOT os.environ) so a
    developer's exported NE_FLOWISE_* vars can't make it flaky."""
    base_env = {"PATH": os.environ.get("PATH", "")}
    cmd = [
        "docker",
        "compose",
        "-f",
        str(FLOWISE_COMPOSE),
        "--profile",
        "flowise",
        "config",
    ]

    # (a) ack ABSENT (dummy secrets present) → nonzero, names the variable.
    denied = subprocess.run(
        cmd, capture_output=True, text=True, check=False, env={**base_env, **_DUMMY_SECRETS}
    )
    assert denied.returncode != 0, "config must FAIL when NE_FLOWISE_ACCEPT_CVE_RISK is unset"
    assert "NE_FLOWISE_ACCEPT_CVE_RISK" in denied.stderr, (
        f"the failure must name the ack variable, got stderr: {denied.stderr!r}"
    )

    # (b) ack=true + dummy secrets → renders (exit 0).
    allowed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env={**base_env, **_DUMMY_SECRETS, "NE_FLOWISE_ACCEPT_CVE_RISK": "true"},
    )
    assert allowed.returncode == 0, (
        f"config must render with ack+secrets, got stderr: {allowed.stderr!r}"
    )


# --- ⭐ H2: the Helm flowise-egress-clamp NetworkPolicy -----------------------

_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _helm_render(*extra: str) -> list[dict]:
    cmd = ["helm", "template", "ne", str(HELM_CHART), "-f", str(HELM_VALUES_CI), *extra]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _find(docs: list[dict], kind: str, name: str) -> dict | None:
    for doc in docs:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    return None


@_needs_helm
def test_flowise_helm_networkpolicy_clamps_egress_to_ne_api() -> None:
    """(H2) Rendered flowise-egress-clamp: podSelector app=flowise, Ingress+Egress
    declared, ZERO ingress rules, egress EXACTLY ne-api:8000/TCP."""
    docs = _helm_render("--set", "networkPolicy.enabled=true", "--set", "profiles.flowise=true")
    policy = _find(docs, "NetworkPolicy", "flowise-egress-clamp")
    assert policy is not None, "flowise-egress-clamp must render when both gates are on"
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "flowise"}}
    assert sorted(spec["policyTypes"]) == ["Egress", "Ingress"]
    assert "ingress" not in spec, "flowise-egress-clamp must declare NO ingress rules"
    assert spec["egress"] == [
        {
            "to": [{"podSelector": {"matchLabels": {"app": "ne-api"}}}],
            "ports": [{"port": 8000, "protocol": "TCP"}],
        }
    ]


@_needs_helm
def test_flowise_helm_networkpolicy_completes_ne_api_ingress_lane() -> None:
    """(H2) The egress clamp alone cannot establish the flowise→ne-api lane: K8s
    needs BOTH the source's egress AND the destination's ingress, and the chart's
    allow-ne-api admits ingress only from the ingress-controller namespace. The
    additive allow-ne-api-from-flowise policy must open ne-api ingress from
    app=flowise on 8000 (Ingress-only, so ne-api egress is unaffected)."""
    docs = _helm_render("--set", "networkPolicy.enabled=true", "--set", "profiles.flowise=true")
    policy = _find(docs, "NetworkPolicy", "allow-ne-api-from-flowise")
    assert policy is not None, (
        "allow-ne-api-from-flowise must render when both gates are on — without it "
        "the clamp's sole allowed destination (ne-api) is unreachable"
    )
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app": "ne-api"}}
    assert spec["policyTypes"] == ["Ingress"], (
        "Ingress-only: this peer must not touch ne-api's egress posture"
    )
    assert spec["ingress"] == [
        {
            "from": [{"podSelector": {"matchLabels": {"app": "flowise"}}}],
            "ports": [{"port": 8000, "protocol": "TCP"}],
        }
    ]


@_needs_helm
def test_flowise_helm_networkpolicy_requires_both_gates() -> None:
    """(H2) Both flowise policies (the egress clamp AND its ne-api ingress peer)
    are absent by default, absent with networkPolicy.enabled alone, and absent
    with profiles.flowise alone — keeping every NE-071 helm guardian byte-green."""
    for name in ("flowise-egress-clamp", "allow-ne-api-from-flowise"):
        assert _find(_helm_render(), "NetworkPolicy", name) is None
        assert (
            _find(
                _helm_render("--set", "networkPolicy.enabled=true"),
                "NetworkPolicy",
                name,
            )
            is None
        )
        assert (
            _find(
                _helm_render("--set", "profiles.flowise=true"),
                "NetworkPolicy",
                name,
            )
            is None
        )
