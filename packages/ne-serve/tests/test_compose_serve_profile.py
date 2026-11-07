# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static validation of deploy/compose/serve/compose.yaml (NE-013).

No live docker daemon is used: the compose YAML is parsed with PyYAML and
asserted structurally (pattern: tools/ne-cli/tests/test_dev_stack.py). The last
test proves the frozen base dev stack is untouched (no vllm service, no
gpu/cpu profile) — the additive-only guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVE_COMPOSE = REPO_ROOT / "deploy" / "compose" / "serve" / "compose.yaml"
DEV_COMPOSE = REPO_ROOT / "deploy" / "compose" / "dev" / "compose.yaml"

# Single source of truth for the pinned images (tag@digest) — the README table
# and the compose file must agree with these exact constants (C11).
GPU_IMAGE = (
    "vllm/vllm-openai:v0.24.0@"
    "sha256:251eba5cc7c12fed0b75da22a9240e582b1c9e39f6fbc064f86781b963bd814f"
)
CPU_IMAGE = (
    "vllm/vllm-openai-cpu:v0.24.0@"
    "sha256:b0e6f8604413a8bfcca8836bb4b57b47f1aa653957e07f8903ece531309981a1"
)

OFFLINE_ENV_KEYS = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "VLLM_NO_USAGE_STATS": "1",
    "DO_NOT_TRACK": "1",
}


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _services() -> dict[str, Any]:
    return _load(SERVE_COMPOSE)["services"]


def test_compose_parses_with_exactly_two_services() -> None:
    services = _services()
    assert set(services) == {"vllm", "vllm-cpu"}


def test_profiles_gate_the_two_services() -> None:
    services = _services()
    assert services["vllm"]["profiles"] == ["gpu"]
    assert services["vllm-cpu"]["profiles"] == ["cpu"]


def test_images_pinned_tag_and_digest() -> None:
    services = _services()
    assert services["vllm"]["image"] == GPU_IMAGE
    assert services["vllm-cpu"]["image"] == CPU_IMAGE
    for name in ("vllm", "vllm-cpu"):
        assert "@sha256:" in services[name]["image"], name  # C11


def test_gpu_device_reservation_present_on_gpu_only() -> None:
    services = _services()
    devices = services["vllm"]["deploy"]["resources"]["reservations"]["devices"]
    assert len(devices) == 1
    dev = devices[0]
    assert dev["driver"] == "nvidia"
    assert dev["capabilities"] == ["gpu"]
    # The CPU service must NOT reserve a GPU.
    assert "deploy" not in services["vllm-cpu"]


def test_offline_and_telemetry_env_on_both_services() -> None:
    services = _services()
    for name in ("vllm", "vllm-cpu"):
        env = services[name]["environment"]
        for key, value in OFFLINE_ENV_KEYS.items():
            assert env.get(key) == value, (name, key)


def test_weights_volume_mounted_read_only() -> None:
    services = _services()
    for name in ("vllm", "vllm-cpu"):
        volumes = services[name]["volumes"]
        assert any(v.endswith(":/models:ro") for v in volumes), name


def test_command_uses_current_structured_output_flag() -> None:
    services = _services()
    for name in ("vllm", "vllm-cpu"):
        command = services[name]["command"]
        assert "--structured-outputs-config.backend" in command, name
        idx = command.index("--structured-outputs-config.backend")
        assert command[idx + 1] == "xgrammar", name
        assert "--served-model-name" in command, name
        # The removed guided-decoding flag must NOT appear.
        assert "--guided-decoding-backend" not in command, name


def test_cpu_qwen_disables_in_band_thinking_by_server_default() -> None:
    """Prompted JSON must not be prefixed by Qwen's ``<think>`` transport.

    The CPU image does not run a reasoning parser, so Qwen3's default hybrid
    thinking lands in ``message.content`` and invalidates the structured answer.
    Keep the policy on the serving command rather than relying on every caller
    to remember a request-local override.  The GPU lane intentionally retains
    its separately parsed reasoning behavior.
    """
    services = _services()
    cpu_command = services["vllm-cpu"]["command"]
    flag = "--default-chat-template-kwargs"
    assert flag in cpu_command
    assert cpu_command[cpu_command.index(flag) + 1] == '{"enable_thinking":false}'
    assert flag not in services["vllm"]["command"]


def test_host_ports_bind_loopback_by_default() -> None:
    """Codex P2: never publish on all interfaces — default to 127.0.0.1.

    NE_VLLM_BIND_ADDR overrides for genuinely private/VPC interfaces.
    """
    services = _services()
    bind = "${NE_VLLM_BIND_ADDR:-127.0.0.1}"
    assert services["vllm"]["ports"] == [f"{bind}:8090:8000"]
    assert services["vllm-cpu"]["ports"] == [f"{bind}:8091:8000"]


def test_healthchecks_present() -> None:
    services = _services()
    for name in ("vllm", "vllm-cpu"):
        assert "healthcheck" in services[name], name
        assert "test" in services[name]["healthcheck"], name


def test_healthchecks_require_a_bounded_completion_not_front_process_health() -> None:
    """A green container must prove the engine generated one token.

    Regression/mutation lock: changing the request back to ``/health``, removing
    the one-token bound, or accepting a response without exactly one generated
    token must fail this test.
    """
    services = _services()
    expected_models = {"vllm": "${NE_VLLM_SERVED_NAME:-qwen3.6-35b-a3b}", "vllm-cpu": "qwen3-0.6b"}
    for name, expected_model in expected_models.items():
        service = services[name]
        probe = service["healthcheck"]["test"]
        assert probe[:3] == ["CMD", "python3", "-c"], name
        script = probe[3]
        assert "/v1/completions" in script, name
        assert "/health" not in script, name
        assert '"max_tokens": 1' in script, name
        assert 'get("completion_tokens") == 1' in script, name
        assert "timeout=90" in script, name
        assert service["healthcheck"]["timeout"] == "100s", name
        assert service["environment"]["NE_VLLM_HEALTH_MODEL"] == expected_model, name


def test_completion_healthcheck_is_resource_bounded() -> None:
    """The health signal stays current without generating on every poll."""
    services = _services()
    for name in ("vllm", "vllm-cpu"):
        health = services[name]["healthcheck"]
        script = health["test"][3]
        assert 'Path("/tmp/ne-vllm-completion-ready")' in script, name
        assert "st_mtime < 60" in script, name
        assert health["interval"] == "30s", name
        assert health["retries"] == 3, name


def test_dev_base_stack_untouched() -> None:
    """Additive guard: the frozen dev stack has no vllm service, no gpu/cpu profile."""
    dev = _load(DEV_COMPOSE)["services"]
    assert "vllm" not in dev
    assert "vllm-cpu" not in dev
    for name, cfg in dev.items():
        profiles = cfg.get("profiles", [])
        assert "gpu" not in profiles, name
        assert "cpu" not in profiles, name
