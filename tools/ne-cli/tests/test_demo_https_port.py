# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-DEMO-HTTPS-PORT: `ne demo up --https-port` override.

Two things are proven here: (1) the DEFAULT 443 path is byte-identical to today —
the generated overlay equals the pre-change golden, no Keycloak client GET/PUT is
issued, `_console_base` omits the explicit ``:443``, and `_endpoint_summary`
matches the pre-change literal; and (2) a non-443 port (9443) propagates end to
end — the overlay's caddy `ports: !override` republishes 9443->443, the `ne up`
preflight probes 9443, the ne-console realm client's redirect/web-origin gains the
9443 origin, demo-credentials.json records the port, and config_hash is unaffected.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import unquote

import click
import httpx
import pytest
import yaml
from click.testing import CliRunner

from ne.cli import main
from ne.cli.demo import corpus, live

up_module = sys.modules["ne.cli.up"]
REPO_ROOT = Path(__file__).resolve().parents[3]


def _compose_child_env() -> dict[str, str]:
    child = dict(os.environ)
    child["NE_AGENT_REGISTRY_RAW_SHA256"] = hashlib.sha256(
        (REPO_ROOT / "deploy/agents/registry.toml").read_bytes()
    ).hexdigest()
    child["NE_SKILL_BUNDLE_RAW_SHA256"] = hashlib.sha256(
        (REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes()
    ).hexdigest()
    return child


def _assert_exact_registry_rollout_labels(doc: dict[str, object]) -> None:
    expected = hashlib.sha256((REPO_ROOT / "deploy/agents/registry.toml").read_bytes()).hexdigest()
    expected_skill = hashlib.sha256(
        (REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes()
    ).hexdigest()
    services = cast("dict[str, dict[str, object]]", doc["services"])
    for service in ("ne-api", "ne-worker"):
        labels = cast("dict[str, str]", services[service]["labels"])
        assert labels["neutron.dev/agent-registry-sha256"] == expected
        assert labels["neutron.dev/skill-bundle-sha256"] == expected_skill


def test_ne_up_overwrites_hostile_ambient_skill_bundle_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NE_SKILL_BUNDLE_RAW_SHA256", "0" * 64)
    child = up_module._scrubbed_child_env(REPO_ROOT)
    assert (
        child["NE_SKILL_BUNDLE_RAW_SHA256"]
        == hashlib.sha256(
            (REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes()
        ).hexdigest()
    )


# --- a local YAML loader that understands the compose `!override` tag ---------
class _OverlayLoader(yaml.SafeLoader):
    """SafeLoader subclass so the `!override` constructor never mutates the global
    yaml.SafeLoader (which other tests rely on)."""


def _construct_override(loader: yaml.Loader, node: yaml.Node) -> list[object]:
    return loader.construct_sequence(cast("yaml.SequenceNode", node))


_OverlayLoader.add_constructor("!override", _construct_override)


# --- the golden overlay template (embedded LITERAL to catch drift) -------------
# The natural-language grounding mode pins every gate setting explicitly. The
# marker-driven hallucination seam is explicitly pinned false so neither a
# lower live overlay nor hostile host environment can retain/reactivate it.
# NE-168: includes the delegation env — NE_API_DEMO_DELEGATION plus the
# 2-model NE_GATEWAY_MODELS (private-default/frontier-best) and
# version-specific Agent policy tasks plus the shared support-task menu that ``ne demo``
# ALWAYS emits for both ne-api and ne-worker (§6.2/Q5 parity). Byte-verified
# against the real generator: ``''.join(f'      {k}: "{v}"\n' for k, v in
# live._DEMO_OVERLAY_ENV) + live._gateway_lane_env(None)``.
# NE-DEMO-FIX1: adds the TEI embed/rerank timeout parity escape hatch
# (NE_TEI_EMBED_TIMEOUT_S / NE_TEI_RERANK_TIMEOUT_S, both live._DEMO_TEI_TIMEOUT_S)
# directly after NE_API_CHAT_AGENTS and before the gateway-lane env.
# NE-CTXB-8: adds NE_ONTOLOGY_VERIFIED_MAX_RESULT_TOKENS (8192 — turns token
# packing ON in the demo composition; default-off in code) directly after
# NE_ONTOLOGY_INCLUDE_ONTOLOGY_GRAPH, mirroring live._DEMO_OVERLAY_ENV order.
_ENV_LINES = (
    '      NE_ONTOLOGY_VERIFIED_ANSWERS_ENABLED: "true"\n'
    '      NE_ONTOLOGY_VERIFIED_TEMPLATES_DIR: "/seed/verified-templates"\n'
    '      NE_ONTOLOGY_GOVERNED_LOCAL_T3_ENABLED: "true"\n'
    '      NE_API_DEMO_MERIDIAN_FIXTURE_PATH: "/seed/meridian-v1"\n'
    '      NE_API_WORKFLOWS_ENABLED: "true"\n'
    '      NE_ONTOLOGY_INCLUDE_ONTOLOGY_GRAPH: "false"\n'
    '      NE_ONTOLOGY_VERIFIED_MAX_RESULT_TOKENS: "8192"\n'
    '      NE_API_DEMO_WRITEBACK_TOOL: "true"\n'
    '      NE_API_DEMO_DELEGATION: "true"\n'
    '      NE_API_DEMO_GROUNDED_HALLUCINATION: "false"\n'
    '      NE_ONTOLOGY_GROUNDED_AGENT_ENABLED: "true"\n'
    '      NE_ONTOLOGY_GROUNDED_AGENT_NAME: "grounded-analyst"\n'
    '      NE_ONTOLOGY_GROUNDING_STRICTNESS: "standard"\n'
    '      NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS: "strict"\n'
    '      NE_ONTOLOGY_GROUNDING_GATE_PROFILE: "ng2"\n'
    '      NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED: "false"\n'
    '      NE_ONTOLOGY_GROUNDED_RUN_AGENT_IDS: "agt_01k3groundedanalyst000000000"\n'
    '      NE_API_CHAT_AGENTS: \'["console-chat", "grounded-analyst"]\'\n'
    '      NE_TEI_EMBED_TIMEOUT_S: "180"\n'
    '      NE_TEI_RERANK_TIMEOUT_S: "180"\n'
    "      NE_GATEWAY_MODELS: '"
    '[{"name": "private-default", "litellm_model": "hosted_vllm/qwen3-0.6b", '
    '"provider": "hosted_vllm", "lane": "private-vllm", '
    '"api_base": "http://vllm:8000/v1", "context_window": 32768}, '
    '{"name": "frontier-best", "litellm_model": "anthropic/claude-fable-5", '
    '"provider": "anthropic", "lane": "frontier-api", "context_window": 1000000, '
    '"max_output_tokens": 12288}]'
    "'\n"
    "      NE_GATEWAY_TASKS: '"
    '{"agent": {"model": "private-default"}, '
    '"agent.meridian-analyst.v2": {"model": "private-default"}, '
    '"agent.meridian-hardship-review.v3": {"model": "private-default"}, '
    '"triage": {"model": "private-default"}, '
    '"reason": {"model": "private-default"}, '
    '"adjudicate": {"model": "frontier-best"}, '
    '"skill-authoring": {"model": "private-default"}, '
    '"grounding-assist": {"model": "private-default"}, '
    '"graph-extract": {"model": "private-default"}}'
    "'\n"
)

# NE-CONNECT-CSV: the polled-connector lane env, emitted after the gateway-lane
# env for BOTH app services. Default mode configures the csv source only (the
# JSON-array values are single-quoted per the NE-GA-G6 rule); --live-writeback
# adds salesforce/m365 (proven in test_demo_live_seams).
_CONNECT_ENV_LINES = (
    "      NE_CONNECT_SYNC_SOURCES: '[\"csv\"]'\n"
    '      NE_CONNECT_SYNC_CRON: "*/15 * * * *"\n'
    '      NE_CONNECT_SYNC_STATE_PATH: "/connect/state.db"\n'
    '      NE_CONNECT_SYNC_QUARANTINE_PATH: "/connect/quarantine.db"\n'
    '      NE_PARSER_VENV_DIR: "/connect/.parser-venv"\n'
    '      NE_CONNECT_SYNC_KPI_TAG: "ne-demo"\n'
    '      NE_CONNECT_CSV_PATH: "/connect/acme-renewals.csv"\n'
    '      NE_CONNECT_CSV_SCHEMA_PATH: "/connect/renewals.schema.json"\n'
    '      NE_CONNECT_CSV_OWNER: "acme-demo-fixture"\n'
    '      NE_CONNECT_CSV_ID_COLUMN: "renewal_id"\n'
    "      NE_CONNECT_CSV_DEFAULT_ACL: '[\"all-employees\"]'\n"
)


def _golden_overlay(seed_abs: Path, workdir: Path) -> str:
    connect_abs = (workdir / "connect").resolve()
    app = (
        f"    environment:\n{_ENV_LINES}{_CONNECT_ENV_LINES}"
        f'    volumes:\n      - "{seed_abs}:/seed:ro"\n'
        f'      - "{connect_abs}:/connect"\n'
        "    healthcheck:\n      start_period: 600s\n"
    )
    return (
        "# NE-DEMO-LIVE-SEAMS runtime overlay (the NE-084 pilot-override precedent).\n"
        "# Layered with `-f` over deploy/compose/pilot/compose.yaml — that file is\n"
        "# NEVER edited (the byte-match guardian). ne-api and ne-worker get the\n"
        "# IDENTICAL demo-seam env map (design §6.2/Q5 cross-process hash parity); opa's\n"
        "# /policies mount is replaced with the staged first-party ne.govern rego bundle.\n"
        "# NO secrets ride this overlay (C12).\n"
        "services:\n"
        "  ne-api:\n"
        f"{app}"
        "  ne-worker:\n"
        f"{app}"
        "  opa:\n"
        "    volumes:\n"
        f'      - "{seed_abs / "opa-policies"}:/policies:ro"\n'
        # TEI batch-token cap so warm-up fits the pinned 4 GiB mem limit (else 137 OOM).
        "  tei-embed:\n"
        "    environment:\n"
        '      MAX_BATCH_TOKENS: "2048"\n'
        "  tei-rerank:\n"
        "    environment:\n"
        '      MAX_BATCH_TOKENS: "2048"\n'
        # vllm-cpu aliased as `vllm` so ne-api's http://vllm:8000 lane resolves;
        # KV budget bumped to fit Qwen3-0.6B's default max-seq-len (else engine aborts).
        "  vllm-cpu:\n"
        "    networks:\n"
        "      default:\n"
        '        aliases: ["vllm"]\n'
        "    environment:\n"
        '      VLLM_CPU_KVCACHE_SPACE: "6"\n'
    )


def _caddy_block(https_port: int) -> str:
    return (
        "  caddy:\n"
        f"    # NE-DEMO-HTTPS-PORT: republish the API host port "
        f"({https_port} -> in-container 443).\n"
        "    # `!override` REPLACES the pinned compose's published list (a plain merge would\n"
        "    # APPEND and leave host 443 bound). Requires docker compose >= 2.24.4.\n"
        "    ports: !override\n"
        f'      - "{https_port}:443"\n'
        '      - "8443:8443"\n'
    )


def _caddy_block_external(https_port: int) -> str:
    # NE-DEMO-EXTERNAL-URL: same block as _caddy_block plus the loopback note and a
    # 127.0.0.1: bind prefix on both published ports (an external edge fronts them).
    return (
        "  caddy:\n"
        f"    # NE-DEMO-HTTPS-PORT: republish the API host port "
        f"({https_port} -> in-container 443).\n"
        "    # `!override` REPLACES the pinned compose's published list (a plain merge would\n"
        "    # APPEND and leave host 443 bound). Requires docker compose >= 2.24.4.\n"
        "    # NE-DEMO-EXTERNAL-URL: bind the published ports to 127.0.0.1 so an\n"
        "    # external edge/reverse-proxy (serving a real cert at the external URL)\n"
        "    # fronts the demo without a public-interface (0.0.0.0) port conflict.\n"
        "    ports: !override\n"
        f'      - "127.0.0.1:{https_port}:443"\n'
        '      - "127.0.0.1:8443:8443"\n'
    )


# ===========================================================================
# overlay generation
# ===========================================================================
def test_default_overlay_byte_identical_to_golden(tmp_path: Path) -> None:
    seed = live._stage_demo_seed(tmp_path)
    seed_abs = seed.resolve()
    golden = _golden_overlay(seed_abs, tmp_path)
    # Both the default-arg call and the explicit https_port=443 call must equal the
    # pre-change golden bytes (SPEC-row item 6 default byte-parity).
    assert live._write_demo_overlay(tmp_path, seed).read_text(encoding="utf-8") == golden
    assert (
        live._write_demo_overlay(tmp_path, seed, https_port=443).read_text(encoding="utf-8")
        == golden
    )


def test_overlay_9443_replaces_caddy_publish(tmp_path: Path) -> None:
    seed = live._stage_demo_seed(tmp_path)
    text = live._write_demo_overlay(tmp_path, seed, https_port=9443).read_text(encoding="utf-8")
    assert "ports: !override" in text
    doc = yaml.load(text, Loader=_OverlayLoader)
    assert doc["services"]["caddy"]["ports"] == ["9443:443", "8443:8443"]
    # The app + opa sections are unchanged vs the default.
    default_doc = yaml.load(_golden_overlay(seed.resolve(), tmp_path), Loader=_OverlayLoader)
    for svc in ("ne-api", "ne-worker", "opa"):
        assert doc["services"][svc] == default_doc["services"][svc]


def test_overlay_9443_is_default_plus_caddy_block_only(tmp_path: Path) -> None:
    seed = live._stage_demo_seed(tmp_path)
    default_text = live._write_demo_overlay(tmp_path, seed, https_port=443).read_text(
        encoding="utf-8"
    )
    port_text = live._write_demo_overlay(tmp_path, seed, https_port=9443).read_text(
        encoding="utf-8"
    )
    # The 9443 overlay is EXACTLY the default overlay + the appended caddy block.
    assert port_text.startswith(default_text)
    assert port_text[len(default_text) :] == _caddy_block(9443)


# ===========================================================================
# NE-DEMO-EXTERNAL-URL: caddy binds loopback when an external edge fronts the demo
# ===========================================================================
def test_overlay_external_url_binds_loopback(tmp_path: Path) -> None:
    seed = live._stage_demo_seed(tmp_path)
    text = live._write_demo_overlay(
        tmp_path, seed, https_port=9443, external_url="https://demo.example.host"
    ).read_text(encoding="utf-8")
    assert "ports: !override" in text
    doc = yaml.load(text, Loader=_OverlayLoader)
    # Both published ports bind 127.0.0.1 so an external reverse-proxy fronts them
    # without a public-interface (0.0.0.0) conflict.
    assert doc["services"]["caddy"]["ports"] == ["127.0.0.1:9443:443", "127.0.0.1:8443:8443"]
    # The app + opa sections are unchanged vs the default overlay.
    default_doc = yaml.load(_golden_overlay(seed.resolve(), tmp_path), Loader=_OverlayLoader)
    for svc in ("ne-api", "ne-worker", "opa"):
        assert doc["services"][svc] == default_doc["services"][svc]


def test_overlay_external_url_is_default_plus_loopback_caddy_block(tmp_path: Path) -> None:
    seed = live._stage_demo_seed(tmp_path)
    default_text = live._write_demo_overlay(tmp_path, seed, https_port=443).read_text(
        encoding="utf-8"
    )
    ext_text = live._write_demo_overlay(
        tmp_path, seed, https_port=9443, external_url="https://demo.example.host"
    ).read_text(encoding="utf-8")
    # The external overlay is EXACTLY the default overlay + the loopback caddy block.
    assert ext_text.startswith(default_text)
    assert ext_text[len(default_text) :] == _caddy_block_external(9443)


def test_overlay_external_url_binds_loopback_even_at_443(tmp_path: Path) -> None:
    # External edge with the demo published on host 443: still binds loopback (the
    # caddy block is emitted because external_url is set, not because the port != 443).
    seed = live._stage_demo_seed(tmp_path)
    text = live._write_demo_overlay(
        tmp_path, seed, https_port=443, external_url="https://demo.example.host"
    ).read_text(encoding="utf-8")
    doc = yaml.load(text, Loader=_OverlayLoader)
    assert doc["services"]["caddy"]["ports"] == ["127.0.0.1:443:443", "127.0.0.1:8443:8443"]


# ===========================================================================
# `ne up` port preflight + endpoint summary
# ===========================================================================
def test_check_ports_free_checks_chosen_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bind a real listener on an OS-assigned free port; the preflight for that port
    # must fail loud naming it.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("", 0))
    listener.listen(1)
    held = listener.getsockname()[1]

    # A second, genuinely-free port (obtained then released).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("", 0))
    other = probe.getsockname()[1]
    probe.close()

    try:
        with pytest.raises(up_module.click.ClickException, match=str(held)):
            up_module._check_ports_free((held, other))
    finally:
        listener.close()
    # With the listener closed, the same call passes.
    up_module._check_ports_free((held, other))

    # And the DEFAULT (no-arg) call still probes exactly (443, 8443).
    probed: list[int] = []
    monkeypatch.setattr(up_module, "_bind_probe", lambda port, published=(): probed.append(port))
    up_module._check_ports_free()
    assert probed == [443, 8443]


def test_check_ports_free_honors_exact_loopback_publish() -> None:
    public_side = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    public_side.bind(("127.0.0.2", 0))
    public_side.listen(1)
    held = public_side.getsockname()[1]
    try:
        # A listener on another local address does not collide with the generated
        # external-URL overlay's exact 127.0.0.1 publish.
        up_module._check_ports_free((held, held), bind_host="127.0.0.1")
    finally:
        public_side.close()

    loopback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    loopback.bind(("127.0.0.1", held))
    loopback.listen(1)
    try:
        with pytest.raises(up_module.click.ClickException, match=rf"127\.0\.0\.1:{held}"):
            up_module._check_ports_free((held, held), bind_host="127.0.0.1")
    finally:
        loopback.close()


def test_current_project_exact_publishes_admit_idempotent_reup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    results = iter(
        (
            types.SimpleNamespace(returncode=0, stdout="caddy-id\n", stderr=""),
            types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {
                                    "com.docker.compose.project": "ne-pilot",
                                    "com.docker.compose.service": "caddy",
                                }
                            },
                            "NetworkSettings": {
                                "Ports": {
                                    "443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9443"}],
                                    "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8443"}],
                                }
                            },
                        }
                    ]
                ),
                stderr="",
            ),
        )
    )

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return next(results)

    monkeypatch.setattr(up_module.subprocess, "run", fake_run)
    assert up_module._current_project_publishes_ports((9443, 8443), bind_host="127.0.0.1")
    assert calls[0][:2] == ["docker", "ps"]
    assert calls[1] == ["docker", "inspect", "caddy-id"]


@pytest.mark.parametrize(
    ("project", "service", "host_ip", "host_port"),
    [
        ("foreign", "caddy", "127.0.0.1", "9443"),
        ("ne-pilot", "foreign", "127.0.0.1", "9443"),
        ("ne-pilot", "caddy", "0.0.0.0", "9443"),
        ("ne-pilot", "caddy", "127.0.0.1", "443"),
    ],
)
def test_current_project_publish_reuse_refuses_every_identity_or_binding_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    project: str,
    service: str,
    host_ip: str,
    host_port: str,
) -> None:
    results = iter(
        (
            types.SimpleNamespace(returncode=0, stdout="candidate\n", stderr=""),
            types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {
                                    "com.docker.compose.project": project,
                                    "com.docker.compose.service": service,
                                }
                            },
                            "NetworkSettings": {
                                "Ports": {
                                    "443/tcp": [{"HostIp": host_ip, "HostPort": host_port}],
                                    "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8443"}],
                                }
                            },
                        }
                    ]
                ),
                stderr="",
            ),
        )
    )
    monkeypatch.setattr(up_module.subprocess, "run", lambda *_a, **_k: next(results))
    assert not up_module._current_project_publishes_ports((9443, 8443), bind_host="127.0.0.1")


def test_endpoint_summary_default_byte_identical() -> None:
    got = up_module._endpoint_summary("localhost", ["brain", "core", "obs"], 443)
    expected = "\n".join(
        [
            "Pilot stack is up (project ne-pilot). Endpoints:",
            "  console          https://localhost:443 (SPA + /api, same-origin via ne-api)",
            "  keycloak         https://localhost:8443",
            "  postgres         127.0.0.1:5432 (on-VM)",
            "  opa              127.0.0.1:8181 (on-VM)",
            "  ne-api           (in-network upstream behind Caddy 443)",
            "  qdrant           127.0.0.1:6333 (grpc 6334) (on-VM)",
            "  tei-embed        127.0.0.1:8085 (on-VM)",
            "  tei-rerank       127.0.0.1:8086 (on-VM)",
            "  otel-collector   127.0.0.1:4317 / 4318 (on-VM)",
            "Tear down with `ne down --bundle <dir>`. Off-box: only https://localhost:443/8443.",
        ]
    )
    assert got == expected


def test_endpoint_summary_9443() -> None:
    got = up_module._endpoint_summary("localhost", ["core"], 9443)
    assert "https://localhost:9443 (SPA + /api" in got
    assert "Off-box: only https://localhost:9443/8443." in got
    # The in-container Caddy 443 line is unchanged.
    assert "(in-network upstream behind Caddy 443)" in got


def test_compose_version_gate_for_override_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    def _stdout(version: str):
        return lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=version, stderr="")

    monkeypatch.setattr(up_module.subprocess, "run", _stdout("2.24.0\n"))
    with pytest.raises(up_module.click.ClickException, match=r"2\.24\.4"):
        up_module._check_compose_version(ports_override=True)

    for ok in ("2.24.4\n", "2.36.2\n"):
        monkeypatch.setattr(up_module.subprocess, "run", _stdout(ok))
        up_module._check_compose_version(ports_override=True)  # no raise

    # ports_override=False keeps the default floor: 2.24.0 still passes.
    monkeypatch.setattr(up_module.subprocess, "run", _stdout("2.24.0\n"))
    up_module._check_compose_version(ports_override=False)


# --- `ne up --https-port` end-to-end (CliRunner, docker mocked) --------------
def _up_bundle(tmp_path: Path) -> Path:
    layout = live._prepare_workdir(tmp_path / "wd")
    toml = live._seed_secret_store(layout["store"])
    live._promote_bundle(layout["bundle"], toml, "localhost")
    return layout["bundle"]


def test_up_cli_https_port_threads_preflight_and_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = Path.cwd()
    import os

    os.chdir(REPO_ROOT)
    try:
        for name in (
            "_check_docker",
            "_check_daemon",
            "_check_compose_version",
            "_check_app_image",
            "_check_age_image",
            "_check_disk",
            "_check_bundle",
            "_check_gpu",
        ):
            monkeypatch.setattr(up_module, name, lambda *a, **k: None)
        monkeypatch.setattr(up_module, "_write_runtime_env", lambda b, **k: tmp_path / "pilot.env")

        recorded: dict[str, object] = {}

        def _rec_ports(ports: tuple[int, int] = (443, 8443)) -> None:
            recorded["ports"] = ports

        monkeypatch.setattr(up_module, "_check_ports_free", _rec_ports)
        monkeypatch.setattr(
            up_module.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(returncode=0),
        )
        bundle = _up_bundle(tmp_path)

        result = CliRunner().invoke(
            main, ["up", "--profile", "pilot", "--bundle", str(bundle), "--https-port", "9443"]
        )
        assert result.exit_code == 0, result.output
        assert recorded["ports"] == (9443, 8443)
        assert "https://localhost:9443" in result.output
        assert "Off-box: only https://localhost:9443/8443." in result.output
    finally:
        os.chdir(cwd)


def test_https_port_8443_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = _up_bundle(tmp_path)
    demo_res = CliRunner().invoke(
        main, ["demo", "--workdir", str(tmp_path / "d"), "up", "--https-port", "8443"]
    )
    assert demo_res.exit_code != 0
    assert "8443" in demo_res.output

    up_res = CliRunner().invoke(
        main, ["up", "--profile", "core", "--bundle", str(bundle), "--https-port", "8443"]
    )
    assert up_res.exit_code != 0
    assert "8443" in up_res.output


def test_demo_graph_option_does_not_require_live_writeback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(live, "demo_up", lambda *args, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(main, ["demo", "--workdir", str(tmp_path / "wd"), "up", "--graph"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["graph"] is True
    assert calls[0]["live_writeback"] is False


def test_demo_graph_option_threads_only_at_explicit_live_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(live, "demo_up", lambda *args, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        main,
        [
            "demo",
            "--workdir",
            str(tmp_path / "wd"),
            "up",
            "--live-writeback",
            "--graph",
            "--seed-manifest-dir",
            str(tmp_path / "manifests"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "hostname": "localhost",
            "seed": None,
            "skip_image_build": False,
            "rebuild_image": False,
            "https_port": 443,
            "external_url": None,
            "gpu_vllm": None,
            "gpu_rerank": None,
            "tei_onnx_artifact": None,
            "live_writeback": True,
            "graph": True,
            "m365_draft_mailbox": None,
            "seed_manifest_dir": tmp_path / "manifests",
            "frontier": None,
        }
    ]


# ===========================================================================
# Keycloak ne-console client update (OIDC correctness — hazard c)
# ===========================================================================
_CONSOLE_CLIENT = {
    "id": "abc123",
    "clientId": "ne-console",
    "redirectUris": [
        "https://localhost/auth/callback",
        "https://localhost:443/auth/callback",
    ],
    "webOrigins": ["+"],
}


def _mock_httpx(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict],
    *,
    fail_frontline_reset: bool = False,
    fail_frontline_reconciliation: bool = False,
):
    real_client = httpx.Client
    memberships = {
        "ops-id": {"all-employees", "ops"},
        "approver-id": {"approver", "stale-approver"},
        "frontline-id": {"ops", "restricted"},
        "steward-id": {"steward", "stale-steward"},
    }
    authority_memberships = {"group-department:servicing": {"ops-id"}}
    realm: dict[str, object] = {"realm": "ne-pilot", "enabled": True}

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # The token POST is form-encoded (data=); the admin group/user/client calls
        # are JSON. Only decode a JSON body, leave the form POST's body as None.
        body = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        records.append(
            {
                "method": request.method,
                "url": url,
                "body": body,
                "host": request.headers.get("host"),
            }
        )
        if request.method == "POST" and "openid-connect/token" in url:
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/admin/realms/ne-pilot":
            if request.method == "GET":
                return httpx.Response(200, json=realm)
            if request.method == "PUT":
                assert isinstance(body, dict)
                realm.clear()
                realm.update(body)
                return httpx.Response(204)
        if request.method == "POST" and url.endswith("/groups"):
            return httpx.Response(201, json={})
        if request.method == "POST" and url.endswith("/users"):
            assert body is not None
            return httpx.Response(409 if body["username"] == "meridian-frontline" else 201)
        if request.method == "GET" and url.endswith("/users?username=ops&exact=true"):
            return httpx.Response(200, json=[{"id": "ops-id", "username": "ops"}])
        if request.method == "GET" and url.endswith("/users?username=approver&exact=true"):
            return httpx.Response(200, json=[{"id": "approver-id", "username": "approver"}])
        if request.method == "GET" and url.endswith("/users?username=steward&exact=true"):
            return httpx.Response(200, json=[{"id": "steward-id", "username": "steward"}])
        if request.method == "GET" and url.endswith(
            "/users?username=meridian-frontline&exact=true"
        ):
            return httpx.Response(
                200, json=[{"id": "frontline-id", "username": "meridian-frontline"}]
            )
        if request.method == "GET" and "/groups?" in url:
            group = request.url.params["search"]
            return httpx.Response(200, json=[{"id": f"group-{group}", "name": group}])
        if request.method == "GET" and "/groups/" in url and request.url.path.endswith("/members"):
            group_id = unquote(url.rsplit("/groups/", 1)[-1].split("/members", 1)[0])
            if group_id == "group-bank.servicing.read":
                member_ids = sorted(
                    user_id
                    for user_id, groups in memberships.items()
                    if "bank.servicing.read" in groups
                )
            else:
                member_ids = sorted(authority_memberships.get(group_id, set()))
            first = int(request.url.params["first"])
            maximum = int(request.url.params["max"])
            return httpx.Response(
                200,
                json=[{"id": member_id} for member_id in member_ids[first : first + maximum]],
            )
        if request.method == "GET" and url.endswith("/groups") and "/users/" in url:
            user_id = url.rsplit("/users/", 1)[-1].split("/", 1)[0]
            if fail_frontline_reconciliation and user_id == "frontline-id":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "group-ops", "name": "ops"},
                        {"id": "group-restricted", "name": "restricted"},
                    ],
                )
            return httpx.Response(
                200,
                json=[{"id": f"group-{name}", "name": name} for name in memberships[user_id]],
            )
        if request.method in {"PUT", "DELETE"} and "/groups/group-" in url:
            user_id = url.rsplit("/users/", 1)[-1].split("/", 1)[0]
            name = url.rsplit("/groups/group-", 1)[-1]
            if request.method == "PUT":
                memberships[user_id].add(name)
            else:
                memberships[user_id].discard(name)
            return httpx.Response(204)
        if (
            fail_frontline_reset
            and request.method == "PUT"
            and "/users/frontline-id/reset-password" in url
        ):
            return httpx.Response(500, json={})
        if request.method == "PUT" and "/users/" in url:
            return httpx.Response(204)
        if request.method == "GET" and "/admin/realms/ne-pilot/clients" in url:
            return httpx.Response(200, json=[dict(_CONSOLE_CLIENT)])
        if request.method == "PUT" and "/clients/abc123" in url:
            return httpx.Response(204)
        return httpx.Response(500, json={})

    def _factory(*args, **kwargs) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(_handler))

    monkeypatch.setattr(httpx, "Client", _factory)


def _promoted_bundle(tmp_path: Path) -> Path:
    layout = live._prepare_workdir(tmp_path / "wd")
    toml = live._seed_secret_store(layout["store"])
    live._promote_bundle(layout["bundle"], toml, "localhost")
    return layout["bundle"]


def test_provision_keycloak_9443_updates_console_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    # Build the scenario before monkeypatching httpx.Client. The retired graph
    # snapshot staging no longer imports ne-brain-graph eagerly, and that package's
    # dependency closure evaluates httpx.Client annotations during first import.
    scenario = corpus.build_scenario()
    _mock_httpx(monkeypatch, records)

    live._provision_keycloak(bundle, "localhost", scenario, https_port=9443)

    gets = [r for r in records if r["method"] == "GET" and "/clients" in r["url"]]
    puts = [r for r in records if r["method"] == "PUT" and "/clients/abc123" in r["url"]]
    assert len(gets) == 1
    assert len(puts) == 1
    # The admin base URL is ALWAYS :8443 (the issuer port never changes).
    assert all(r["url"].startswith("https://localhost:8443") for r in records)

    body = puts[0]["body"]
    assert "https://localhost:9443/auth/callback" in body["redirectUris"]
    # The two originally-committed redirect URIs are preserved.
    assert "https://localhost/auth/callback" in body["redirectUris"]
    assert "https://localhost:443/auth/callback" in body["redirectUris"]
    assert "https://localhost:9443" in body["webOrigins"]
    assert "+" in body["webOrigins"]


def test_provision_keycloak_external_url_uses_external_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    scenario = corpus.build_scenario()
    _mock_httpx(monkeypatch, records)

    # The operator's real invocation: --hostname demo.example.host --https-port 9443
    # --external-url https://demo.example.host.
    live._provision_keycloak(
        bundle,
        "demo.example.host",
        scenario,
        https_port=9443,
        external_url="https://demo.example.host",
    )

    gets = [r for r in records if r["method"] == "GET" and "/clients" in r["url"]]
    puts = [r for r in records if r["method"] == "PUT" and "/clients/abc123" in r["url"]]
    assert len(gets) == 1
    assert len(puts) == 1
    # NE-DEMO-EXTERNAL-URL hairpin fix: every ON-BOX admin call dials loopback (the
    # public hostname resolves to the edge and hairpin-NATs / fails from the box) …
    assert all(r["url"].startswith("https://127.0.0.1:8443") for r in records)
    # … carrying the public Host so Caddy routes to the {hostname} vhost and Keycloak
    # (KC_HOSTNAME hostname-strict) still issues on https://demo.example.host:8443.
    assert all(r["host"] == "demo.example.host:8443" for r in records)

    body = puts[0]["body"]
    # The allowlist gains the EXTERNAL URL (no port), NOT the on-box :9443 origin.
    assert "https://demo.example.host/auth/callback" in body["redirectUris"]
    assert "https://demo.example.host" in body["webOrigins"]
    assert "https://demo.example.host:9443/auth/callback" not in body["redirectUris"]
    assert "https://demo.example.host:9443" not in body["webOrigins"]


def test_provision_keycloak_default_leaves_client_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert live._MERIDIAN_RESTRICTED_GROUPS == (
        "all-employees",
        "meridian-frontline",
        "bank.customer.read",
        "bank.account-summary.read",
        "sharepoint.frontline.read",
    )
    assert live._MERIDIAN_APPROVER_GROUPS == (
        "approver",
        "meridian-approver",
        "bank.customer.read",
        "bank.servicing.read",
        "bank.dispute.read",
        "bank.approval.read",
        "sharepoint.restricted.read",
    )
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    scenario = corpus.build_scenario()
    _mock_httpx(monkeypatch, records)

    creds_path = live._provision_keycloak(bundle, "localhost", scenario)

    assert not [r for r in records if "/clients" in r["url"]]  # no client GET/PUT at default 443
    owner_memberships = [
        r["url"].rsplit("/groups/group-", 1)[-1]
        for r in records
        if r["method"] == "PUT" and "/users/ops-id/groups/group-" in r["url"]
    ]
    assert set(owner_memberships) == {
        "meridian-ops",
        "bank.customer.read",
        "bank.servicing.read",
        "bank.dispute.read",
        "sharepoint.operations.read",
    }
    authority_writes = [
        r
        for r in records
        if r["method"] in {"PUT", "DELETE"}
        and unquote(r["url"]).endswith("/groups/group-department:servicing")
    ]
    # Approver receives the same derived Skill authority projection as any
    # bank.servicing.read member. This is not an ops route/persona grant.
    assert len(authority_writes) == 1
    assert "/users/approver-id/" in authority_writes[0]["url"]
    restricted_puts = {
        r["url"].rsplit("/groups/group-", 1)[-1]
        for r in records
        if r["method"] == "PUT" and "/users/frontline-id/groups/group-" in r["url"]
    }
    assert restricted_puts == {
        "all-employees",
        "meridian-frontline",
        "bank.customer.read",
        "bank.account-summary.read",
        "sharepoint.frontline.read",
    }
    assert not restricted_puts & {
        "ops",
        "approver",
        "steward",
        "restricted",
        "bank.servicing.read",
        "bank.dispute.read",
        "sharepoint.operations.read",
    }
    restricted_deletes = {
        r["url"].rsplit("/groups/group-", 1)[-1]
        for r in records
        if r["method"] == "DELETE" and "/users/frontline-id/groups/group-" in r["url"]
    }
    assert restricted_deletes == {"ops", "restricted"}
    approver_puts = {
        r["url"].rsplit("/groups/group-", 1)[-1]
        for r in records
        if r["method"] == "PUT" and "/users/approver-id/groups/group-" in r["url"]
    }
    assert approver_puts == {
        "meridian-approver",
        "bank.customer.read",
        "bank.servicing.read",
        "bank.dispute.read",
        "bank.approval.read",
        "sharepoint.restricted.read",
        "department%3Aservicing",
    }
    assert not approver_puts & {
        "all-employees",
        "ops",
        "meridian-ops",
        "steward",
        "sharepoint.operations.read",
    }
    approver_deletes = {
        r["url"].rsplit("/groups/group-", 1)[-1]
        for r in records
        if r["method"] == "DELETE" and "/users/approver-id/groups/group-" in r["url"]
    }
    assert approver_deletes == {"stale-approver"}
    steward_deletes = {
        r["url"].rsplit("/groups/group-", 1)[-1]
        for r in records
        if r["method"] == "DELETE" and "/users/steward-id/groups/group-" in r["url"]
    }
    assert steward_deletes == {"stale-steward"}
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    assert creds["https_port"] == 443
    assert creds["principals"]["meridian-frontline"]["username"] == "meridian-frontline"
    assert creds["principals"]["meridian-frontline"]["password"] == "meridian-frontline-demo"
    assert creds["principals"]["ops"]["username"] == "ops"
    assert creds["principals"]["ops"]["password"] == "ops-demo"
    assert creds["principals"][scenario.approver_id] == {
        "username": "approver",
        "password": "approver-demo",
    }
    assert creds["principals"]["steward"] == {
        "username": "steward",
        "password": "steward-demo",
    }
    assert creds["principals"][scenario.reader_id] == {
        "username": scenario.reader_id,
        "password": f"{scenario.reader_id}-demo",
    }
    assert creds["principals"][scenario.outsider_id] == {
        "username": scenario.outsider_id,
        "password": f"{scenario.outsider_id}-demo",
    }
    reset_bodies = {
        r["url"].rsplit("/users/", 1)[-1].split("/", 1)[0]: r["body"]
        for r in records
        if r["method"] == "PUT" and r["url"].endswith("/reset-password")
    }
    assert reset_bodies["frontline-id"] == {
        "type": "password",
        "value": creds["principals"]["meridian-frontline"]["password"],
        "temporary": False,
    }
    assert reset_bodies["ops-id"] == {
        "type": "password",
        "value": creds["principals"]["ops"]["password"],
        "temporary": False,
    }
    assert reset_bodies["approver-id"] == {
        "type": "password",
        "value": "approver-demo",
        "temporary": False,
    }
    assert reset_bodies["steward-id"] == {
        "type": "password",
        "value": "steward-demo",
        "temporary": False,
    }
    assert creds_path.stat().st_mode & 0o777 == 0o600


def test_provision_keycloak_fails_closed_when_frontline_reset_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    scenario = corpus.build_scenario()
    records: list[dict] = []
    _mock_httpx(monkeypatch, records, fail_frontline_reset=True)

    with pytest.raises(httpx.HTTPStatusError):
        live._provision_keycloak(bundle, "localhost", scenario)

    assert not (bundle.parent / "demo-credentials.json").exists()


def test_provision_keycloak_writes_no_credentials_on_group_reconciliation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    _mock_httpx(monkeypatch, records, fail_frontline_reconciliation=True)

    with pytest.raises(click.ClickException, match="group reconciliation failed"):
        live._provision_keycloak(bundle, "localhost", corpus.build_scenario())

    assert not (bundle.parent / "demo-credentials.json").exists()


def test_provision_keycloak_tightens_existing_credentials_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    creds_path = bundle.parent / "demo-credentials.json"
    creds_path.write_text("stale", encoding="utf-8")
    creds_path.chmod(0o644)
    records: list[dict] = []
    scenario = corpus.build_scenario()
    _mock_httpx(monkeypatch, records)
    real_dump = json.dump

    def guarded_dump(payload, fh, *args, **kwargs):
        assert os.fstat(fh.fileno()).st_mode & 0o777 == 0o600
        return real_dump(payload, fh, *args, **kwargs)

    monkeypatch.setattr(json, "dump", guarded_dump)

    live._provision_keycloak(bundle, "localhost", scenario)

    assert creds_path.stat().st_mode & 0o777 == 0o600


def test_credentials_file_records_port_and_console_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    _mock_httpx(monkeypatch, records)

    creds_path = live._provision_keycloak(
        bundle, "localhost", corpus.build_scenario(), https_port=9443
    )
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    assert creds["https_port"] == 9443
    assert creds["hostname"] == "localhost"

    assert live._console_base("localhost", 443) == "https://localhost"
    assert ":443" not in live._console_base("localhost", 443)
    assert live._console_base("localhost", 9443) == "https://localhost:9443"


# ===========================================================================
# NE-DEMO-EXTERNAL-URL: on-box loopback dial (hairpin fix)
# ===========================================================================
def test_onbox_dial_default_is_byte_identical() -> None:
    # No external URL → dial the hostname directly, NO extra headers (byte-identical
    # to today; 443 omits the explicit port exactly as _console_base spells it).
    assert live._onbox_dial("localhost", 443, None) == ("https://localhost", {})
    assert live._onbox_dial("localhost", 8443, None) == ("https://localhost:8443", {})
    assert live._onbox_dial("localhost", 9443, None) == ("https://localhost:9443", {})
    # The default 443 dial equals the console origin (parity with the arc base today).
    assert live._onbox_dial("localhost", 443, None)[0] == live._console_base("localhost", 443)


def test_onbox_dial_external_url_targets_loopback_with_public_host() -> None:
    # An edge fronts the demo → dial loopback (always reachable on-box, never hairpins)
    # and carry the public Host so Caddy routes to the {hostname} vhost.
    ext = "https://demo.example.host"
    assert live._onbox_dial("demo.example.host", 443, ext) == (
        "https://127.0.0.1:443",
        {"Host": "demo.example.host"},
    )
    # Keycloak's :8443 issuer port keeps the port in the Host (browser-faithful).
    assert live._onbox_dial("demo.example.host", 8443, ext) == (
        "https://127.0.0.1:8443",
        {"Host": "demo.example.host:8443"},
    )


def test_provision_keycloak_default_sends_no_host_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Default path (no external URL): the on-box admin calls dial the hostname and send
    # NO explicit Host header — httpx derives it from the URL (byte-identical requests).
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    _mock_httpx(monkeypatch, records)

    live._provision_keycloak(bundle, "localhost", corpus.build_scenario())

    assert all(r["url"].startswith("https://localhost:8443") for r in records)
    assert all(r["host"] == "localhost:8443" for r in records)


def test_credentials_file_records_external_url_only_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _promoted_bundle(tmp_path)
    records: list[dict] = []
    _mock_httpx(monkeypatch, records)

    # external_url set → recorded so `ne demo run` dials loopback for its on-box calls.
    ext_path = live._provision_keycloak(
        bundle,
        "demo.example.host",
        corpus.build_scenario(),
        https_port=443,
        external_url="https://demo.example.host",
    )
    assert json.loads(ext_path.read_text(encoding="utf-8"))["external_url"] == (
        "https://demo.example.host"
    )

    # Default path → NO external_url key (creds file stays byte-identical to pre-change).
    def_path = live._provision_keycloak(bundle, "localhost", corpus.build_scenario())
    assert "external_url" not in json.loads(def_path.read_text(encoding="utf-8"))


# ===========================================================================
# NE-DEMO-EXTERNAL-URL: TLS SNI on the loopback dial
# ===========================================================================
# The Host header from _onbox_dial routes the HTTP request, but the TLS handshake
# happens FIRST: a dial to 127.0.0.1 sends no SNI (an IP literal is never a
# server_name) and the pilot Caddy serves ONLY named vhosts, so it rejects the
# no-SNI handshake with `tlsv1 alert internal error` before any HTTP exchange
# (observed live on the demo host, 2026-08-04). _onbox_transport injects the public
# hostname as the httpcore `sni_hostname` request extension.


def test_onbox_transport_default_is_none() -> None:
    # No edge → None, so httpx builds its stock transport (byte-identical default
    # path, mirroring _onbox_dial's no-header default).
    assert live._onbox_transport("localhost", None) is None
    assert live._onbox_transport("localhost", None, use_async=True) is None


def _capture_sni(monkeypatch: pytest.MonkeyPatch) -> list:
    """Patch both stock transports' terminal handlers to record each request's
    sni_hostname extension instead of touching the network."""
    seen: list = []

    def _sync_handle(self, request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("sni_hostname"))
        return httpx.Response(200)

    async def _async_handle(self, request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("sni_hostname"))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _sync_handle)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _async_handle)
    return seen


def test_onbox_transport_injects_public_hostname_as_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_sni(monkeypatch)

    with httpx.Client(
        transport=live._onbox_transport("demo.example.host", "https://demo.example.host")
    ) as client:
        assert client.get("https://127.0.0.1:8443/x").status_code == 200
    assert seen == ["demo.example.host"]

    # CONTROL (both directions): the stock transport on the same dial carries NO
    # sni_hostname — the extension exists only because _onbox_transport injects it.
    seen.clear()
    with httpx.Client(transport=httpx.HTTPTransport(verify=False)) as client:
        assert client.get("https://127.0.0.1:8443/x").status_code == 200
    assert seen == [None]


def test_onbox_transport_async_injects_public_hostname_as_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    seen = _capture_sni(monkeypatch)

    async def _drive() -> None:
        async with httpx.AsyncClient(
            transport=live._onbox_transport(
                "demo.example.host", "https://demo.example.host", use_async=True
            )
        ) as client:
            assert (await client.get("https://127.0.0.1:9443/x")).status_code == 200

    asyncio.run(_drive())
    assert seen == ["demo.example.host"]


def test_provision_keycloak_external_url_mounts_sni_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Lock the call-site wiring: under --external-url the admin Client is built
    # WITH the SNI transport (a Host header alone never survives the handshake);
    # the default path passes transport=None (stock behaviour).
    bundle = _promoted_bundle(tmp_path)
    orig_client = httpx.Client  # the real class, before _mock_httpx patches it
    records: list[dict] = []
    transports: list = []
    _mock_httpx(monkeypatch, records)
    mock_factory = httpx.Client

    def _factory(*args, **kwargs) -> httpx.Client:
        transports.append(kwargs.get("transport"))
        return mock_factory(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _factory)

    live._provision_keycloak(
        bundle,
        "demo.example.host",
        corpus.build_scenario(),
        https_port=9443,
        external_url="https://demo.example.host",
    )
    assert len(transports) == 1
    assert isinstance(transports[0], httpx.HTTPTransport)

    seen = _capture_sni(monkeypatch)
    with orig_client(transport=transports[0]) as client:
        client.get("https://127.0.0.1:8443/x")
    assert seen == ["demo.example.host"]

    transports.clear()
    live._provision_keycloak(bundle, "localhost", corpus.build_scenario())
    assert transports == [None]


# ===========================================================================
# demo_up seam threading + config_hash independence
# ===========================================================================
def _seam_mock_demo_up(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    monkeypatch.setattr(live, "_preflight_docker", lambda: None)
    monkeypatch.setattr(live, "_image_up_to_date", lambda root: True)
    monkeypatch.setattr(live, "_download_model", lambda models_dir: None)
    monkeypatch.setattr(
        live, "_stage_tei_cache", lambda workdir, onnx_artifact: workdir / "tei-cache"
    )
    monkeypatch.setattr(live, "_stage_demo_seed", lambda workdir: workdir / "seed")

    def _overlay(
        workdir: Path,
        seed_dir: Path,
        https_port: int = 443,
        external_url: str | None = None,
        gpu_vllm: str | None = None,
        tei_cache_dir: Path | None = None,
    ) -> Path:
        captured["overlay"] = https_port
        captured["overlay_external_url"] = external_url
        captured["overlay_gpu_vllm"] = gpu_vllm
        return workdir / "overlay.yaml"

    def _bring(
        bundle: Path,
        models_dir: Path,
        overlay_path: Path,
        https_port: int = 443,
        *,
        external_url: str | None = None,
    ) -> None:
        captured["bring_up"] = https_port
        captured["bring_up_external_url"] = external_url

    def _provision(
        bundle: Path,
        hostname: str,
        scenario: object,
        https_port: int = 443,
        external_url: str | None = None,
    ) -> Path:
        captured["provision"] = https_port
        captured["provision_external_url"] = external_url
        return bundle

    monkeypatch.setattr(live, "_write_demo_overlay", _overlay)
    monkeypatch.setattr(live, "_bring_up_stack", _bring)
    monkeypatch.setattr(live, "_provision_keycloak", _provision)
    monkeypatch.setattr(live, "_seed_corpus", lambda workdir, bundle, corpus_dir: None)


def test_demo_up_threads_https_port_through_seams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    _seam_mock_demo_up(monkeypatch, captured)
    live.demo_up(
        tmp_path / "wd", hostname="localhost", seed=None, skip_image_build=False, https_port=9443
    )
    assert captured["overlay"] == 9443
    assert captured["bring_up"] == 9443
    assert captured["provision"] == 9443


def test_demo_up_threads_external_url_to_bind_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    _seam_mock_demo_up(monkeypatch, captured)
    live.demo_up(
        tmp_path / "wd",
        hostname="demo.example.host",
        seed=None,
        skip_image_build=False,
        https_port=9443,
        external_url="https://demo.example.host",
    )
    assert captured["bring_up_external_url"] == "https://demo.example.host"


def test_config_hash_unaffected_by_https_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    _seam_mock_demo_up(monkeypatch, captured)

    live.demo_up(
        tmp_path / "a", hostname="localhost", seed=None, skip_image_build=False, https_port=443
    )
    live.demo_up(
        tmp_path / "b", hostname="localhost", seed=None, skip_image_build=False, https_port=9443
    )
    hash_a = json.loads((tmp_path / "a" / "bundle" / "manifest.json").read_text())["config_hash"]
    hash_b = json.loads((tmp_path / "b" / "bundle" / "manifest.json").read_text())["config_hash"]
    assert hash_a == hash_b


# ===========================================================================
# LIVE-PARKABLE: the real compose merge over the pinned compose (docker CLI only)
# ===========================================================================
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI required")
def test_compose_merge_publishes_9443(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = live._stage_demo_seed(tmp_path)
    overlay = live._write_demo_overlay(tmp_path, seed, https_port=9443)
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(
        "NE_PILOT_HOSTNAME=x\nNE_PILOT_KEYCLOAK_ADMIN_PASSWORD=x\n"
        "NE_PILOT_OPENFGA_API_TOKEN=x\nNE_PILOT_POSTGRES_PASSWORD=x\n",
        encoding="utf-8",
    )
    compose = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
    monkeypatch.setenv("NE_AGENT_REGISTRY_RAW_SHA256", "0" * 64)
    monkeypatch.setenv("NE_SKILL_BUNDLE_RAW_SHA256", "0" * 64)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "ne-demo-porttest",
            "-f",
            str(compose),
            "-f",
            str(overlay),
            "--env-file",
            str(env_file),
            "--profile",
            "core",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_compose_child_env(),
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    _assert_exact_registry_rollout_labels(doc)
    published = {p["published"] for p in doc["services"]["caddy"]["ports"]}
    assert published == {"9443", "8443"}, published
    assert "443" not in published


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI required")
def test_compose_merge_disables_inherited_grounding_marker_and_pins_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later generated overlay must neutralize the old live demo layer.

    This executes Compose's real mapping merge over the pinned pilot file, a
    production-shaped lower layer carrying every stale grounding value, and
    the newly generated overlay. Parsing the generated YAML alone cannot prove
    that an inherited environment key is gone from the resolved container.
    """
    seed = live._stage_demo_seed(tmp_path)
    overlay = live._write_demo_overlay(tmp_path, seed)
    lower = tmp_path / "legacy-grounding.yaml"
    lower.write_text(
        "services:\n"
        "  ne-api:\n"
        "    environment:\n"
        '      NE_API_DEMO_GROUNDED_HALLUCINATION: "true"\n'
        "      NE_API_CHAT_AGENTS: '[\"console-chat\"]'\n"
        '      NE_ONTOLOGY_GROUNDED_AGENT_ENABLED: "false"\n'
        '      NE_ONTOLOGY_GROUNDED_AGENT_NAME: "legacy-grounded"\n'
        '      NE_ONTOLOGY_GROUNDING_STRICTNESS: "strict"\n'
        '      NE_ONTOLOGY_GROUNDING_GATE_PROFILE: "ng1-frozen"\n'
        '      NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED: "true"\n'
        "  ne-worker:\n"
        "    environment:\n"
        '      NE_API_DEMO_GROUNDED_HALLUCINATION: "true"\n'
        "      NE_API_CHAT_AGENTS: '[\"console-chat\"]'\n"
        '      NE_ONTOLOGY_GROUNDED_AGENT_ENABLED: "false"\n'
        '      NE_ONTOLOGY_GROUNDED_AGENT_NAME: "legacy-grounded"\n'
        '      NE_ONTOLOGY_GROUNDING_STRICTNESS: "strict"\n'
        '      NE_ONTOLOGY_GROUNDING_GATE_PROFILE: "ng1-frozen"\n'
        '      NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED: "true"\n',
        encoding="utf-8",
    )
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(
        "NE_PILOT_HOSTNAME=x\nNE_PILOT_KEYCLOAK_ADMIN_PASSWORD=x\n"
        "NE_PILOT_OPENFGA_API_TOKEN=x\nNE_PILOT_POSTGRES_PASSWORD=x\n",
        encoding="utf-8",
    )
    compose = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
    monkeypatch.setenv("NE_AGENT_REGISTRY_RAW_SHA256", "0" * 64)
    monkeypatch.setenv("NE_SKILL_BUNDLE_RAW_SHA256", "0" * 64)
    child_env = _compose_child_env()
    assert (
        child_env["NE_SKILL_BUNDLE_RAW_SHA256"]
        == hashlib.sha256(
            (REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes()
        ).hexdigest()
    )
    child_env["NE_API_DEMO_GROUNDED_HALLUCINATION"] = "true"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "ne-demo-grounding-merge-test",
            "-f",
            str(compose),
            "-f",
            str(lower),
            "-f",
            str(overlay),
            "--env-file",
            str(env_file),
            "--profile",
            "core",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    _assert_exact_registry_rollout_labels(doc)
    expected = {
        "NE_API_CHAT_AGENTS": '["console-chat", "grounded-analyst"]',
        "NE_ONTOLOGY_GROUNDED_AGENT_ENABLED": "true",
        "NE_ONTOLOGY_GROUNDED_AGENT_NAME": "grounded-analyst",
        "NE_ONTOLOGY_GROUNDING_STRICTNESS": "standard",
        "NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS": "strict",
        "NE_ONTOLOGY_GROUNDING_GATE_PROFILE": "ng2",
        "NE_ONTOLOGY_GROUNDED_ASSIST_ENABLED": "false",
    }
    resolved: dict[str, dict[str, str | None]] = {}
    for service in ("ne-api", "ne-worker"):
        service_env = doc["services"][service]["environment"]
        assert service_env.get("NE_API_DEMO_GROUNDED_HALLUCINATION") == "false"
        assert {key: service_env.get(key) for key in expected} == expected
        resolved[service] = service_env
    assert {key: resolved["ne-api"].get(key) for key in expected} == {
        key: resolved["ne-worker"].get(key) for key in expected
    }

    from ne.api.settings import ApiSettings

    for service in ("ne-api", "ne-worker"):
        service_env = {
            key: str(value) for key, value in resolved[service].items() if value is not None
        }
        with patch.dict(os.environ, service_env, clear=True):
            settings = ApiSettings(
                qdrant_url="http://qdrant.test",
                qdrant_collection="demo",
                tei_url="http://tei.test",
            )
        assert settings.demo_grounded_hallucination is False
        assert settings.chat_agents == ("console-chat", "grounded-analyst")


# ===========================================================================
# NE-DEMO-GPU-LANE: --gpu-vllm points the private-vllm lane at an external GPU
# ===========================================================================
def test_gpu_vllm_lane_and_egress_allow(tmp_path: Path) -> None:
    seed = live._stage_demo_seed(tmp_path)
    text = live._write_demo_overlay(
        tmp_path, seed, gpu_vllm="https://pod-8000.proxy.runpod.net/v1"
    ).read_text(encoding="utf-8")
    doc = yaml.load(text, Loader=_OverlayLoader)
    for svc in ("ne-api", "ne-worker"):
        env = doc["services"][svc]["environment"]
        menu = json.loads(env["NE_GATEWAY_MODELS"])
        assert menu[0]["lane"] == "private-vllm"
        assert menu[0]["litellm_model"] == "hosted_vllm/qwen3.6-35b-a3b"
        assert menu[0]["context_window"] == 262144
        assert menu[0]["api_base"] == "https://pod-8000.proxy.runpod.net/v1"
        # the external host is allowlisted on the C2 egress guard
        assert json.loads(env["NE_EGRESS_ALLOWED_PRIVATE_VLLM_HOSTS"]) == [
            "pod-8000.proxy.runpod.net"
        ]


def test_no_gpu_vllm_leaves_gateway_untouched(tmp_path: Path) -> None:
    # NE-168: the gateway menu is now ALWAYS emitted (3 models / 6 tasks — the
    # NE-SKILL-1b skill-authoring task and the NE-T2X-4 graph-extract route
    # included) even
    # without --gpu-vllm, so "untouched" no longer means the keys are absent — it
    # means every lane stays IN-NETWORK (http://vllm:8000/v1) and the C2 egress
    # allowlist line never rides the default overlay (no off-box relaxation).
    seed = live._stage_demo_seed(tmp_path)
    text = live._write_demo_overlay(tmp_path, seed).read_text(encoding="utf-8")
    assert "NE_EGRESS_ALLOWED_PRIVATE_VLLM_HOSTS" not in text
    doc = yaml.load(text, Loader=_OverlayLoader)
    for svc in ("ne-api", "ne-worker"):
        env = doc["services"][svc]["environment"]
        by_name = {m["name"]: m for m in json.loads(env["NE_GATEWAY_MODELS"])}
        assert set(by_name) == {"private-default", "frontier-best"}
        assert by_name["private-default"]["api_base"] == "http://vllm:8000/v1"
        assert "api_base" not in by_name["frontier-best"]
