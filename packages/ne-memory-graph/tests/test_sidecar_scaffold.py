# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GraphitiSidecar env-builder shape (fake runner) + loopback guard at construction.

The env-builder shape is asserted with a monkeypatched ``run`` — this test NEVER
runs a real ``uv venv`` / ``uv pip install`` (the splink precedent). The loopback
guard rejects a non-loopback ``service_url`` at MemoryGraphClient construction with
ZERO requests issued (C2 fail-closed).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ne.memory_graph.client import MemoryGraphClient
from ne.memory_graph.config import MemoryGraphSettings
from ne.memory_graph.sidecar import GraphitiSidecar, default_graphiti_env_dir


class _FakeRunner:
    def __init__(self, env_dir: Path) -> None:
        self._env_dir = env_dir
        self.install_calls = 0

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        if cmd[:2] == ["uv", "venv"]:
            venv_dir = Path(cmd[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!fake-python\n")
            return
        if cmd[:3] == ["uv", "pip", "install"]:
            python = Path(cmd[4])
            assert python.is_file(), "venv python must exist on install entry"
            assert "--require-hashes" in cmd, "the Graphiti sidecar install must be hash-pinned"
            assert "--no-deps" not in cmd
            assert cmd[-2:] == ["-r", str(self._env_dir / "requirements.txt")], cmd
            self.install_calls += 1
            return
        raise AssertionError(f"unexpected command: {cmd!r}")


def _write_requirements(env_dir: Path, body: str) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text(body)
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


def test_default_env_dir_points_at_in_repo_graphiti_env() -> None:
    env_dir = default_graphiti_env_dir()
    assert env_dir.name == "graphiti_env"
    assert (env_dir / "requirements.in").is_file()
    assert (env_dir / "service.py").is_file()
    assert (env_dir / "requirements.txt").is_file()


def test_ensure_builds_hash_pinned_venv(tmp_path: Path) -> None:
    env_dir = tmp_path / "graphiti_env"
    venv_dir = tmp_path / ".graphiti-venv"
    want = _write_requirements(env_dir, "graphiti-core==0.29.2 --hash=sha256:deadbeef\n")
    runner = _FakeRunner(env_dir)
    sidecar = GraphitiSidecar(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert sidecar.available() is False
    sidecar.ensure()
    assert runner.install_calls == 1
    assert sidecar.available() is True
    assert (venv_dir / ".requirements.sha256").read_text() == want

    sidecar.ensure()  # marker matches → no-op
    assert runner.install_calls == 1


def test_ensure_rebuilds_when_requirements_change(tmp_path: Path) -> None:
    env_dir = tmp_path / "graphiti_env"
    venv_dir = tmp_path / ".graphiti-venv"
    _write_requirements(env_dir, "graphiti-core==0.29.2 --hash=sha256:aaa\n")
    runner = _FakeRunner(env_dir)
    sidecar = GraphitiSidecar(env_dir=env_dir, venv_dir=venv_dir, run=runner)
    sidecar.ensure()
    assert runner.install_calls == 1
    _write_requirements(env_dir, "graphiti-core==0.29.2 --hash=sha256:bbb\n")
    sidecar.ensure()
    assert runner.install_calls == 2


def test_loopback_guard_rejects_external_service_url_with_zero_requests() -> None:
    settings = MemoryGraphSettings(service_url="http://evil.example.com:8093")
    with pytest.raises(ValueError, match="external"):
        MemoryGraphClient(settings)


def test_loopback_guard_rejects_embedded_credentials() -> None:
    settings = MemoryGraphSettings(service_url="http://user:pass@127.0.0.1:8093")
    with pytest.raises(ValueError, match="credentials"):
        MemoryGraphClient(settings)


def test_loopback_guard_allows_allowlisted_host() -> None:
    settings = MemoryGraphSettings(
        service_url="http://memgraph.internal-svc:8093",
        allowed_service_hosts=("memgraph.internal-svc",),
    )
    client = MemoryGraphClient(settings)
    assert client.base_url == "http://memgraph.internal-svc:8093"


def test_loopback_guard_allows_ipv6_loopback() -> None:
    settings = MemoryGraphSettings(service_url="http://[::1]:8093")
    client = MemoryGraphClient(settings)
    assert client.base_url == "http://[::1]:8093"


def test_service_env_contract() -> None:
    """Structural: service.py honours the documented env contract.

    (a) sidecar bearer auth reads NE_MEMORY_GRAPH_SERVICE_TOKEN — the SAME var
    MemoryGraphSettings.service_token reads and the README documents; the
    never-documented NE_MEMORY_GRAPH_TOKEN spelling must not survive;
    (b) the LLM api_key falls back to the service token (frontier-lane llm_facade
    bearer) so ONE secret wires the lane;
    (c) C1: GRAPHITI_TELEMETRY_ENABLED is forced 'false' before any graphiti import.
    """
    source = (default_graphiti_env_dir() / "service.py").read_text(encoding="utf-8")
    assert '_env("NE_MEMORY_GRAPH_SERVICE_TOKEN")' in source
    assert '"NE_MEMORY_GRAPH_TOKEN"' not in source
    assert 'or _env("NE_MEMORY_GRAPH_SERVICE_TOKEN")' in source  # LLM api_key fallback
    assert "api_key=_resolve_llm_api_key()" in source  # FIX-3 refactor pinned
    telemetry_off = source.index('os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"')
    first_graphiti_import = source.index("from graphiti_core")
    assert telemetry_off < first_graphiti_import


def test_venv_dir_honors_sidecar_venv_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NE-SVENV: venv_dir resolves under NE_SIDECAR_VENV_ROOT; explicit kwarg wins."""
    root = tmp_path / "writable"
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root))
    sidecar = GraphitiSidecar(env_dir=tmp_path / "graphiti_env")
    assert sidecar.venv_dir == root / ".graphiti-venv"

    explicit = tmp_path / "explicit" / ".graphiti-venv"
    override = GraphitiSidecar(env_dir=tmp_path / "graphiti_env", venv_dir=explicit)
    assert override.venv_dir == explicit
