# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AIR_GAP fail-closed on the Graphiti sidecar venv builder (C2).

Under AIR_GAP an absent/stale Graphiti sidecar venv must fail with a typed
``AirGapViolationError`` BEFORE any ``uv`` build/egress (spy-proven zero ``run``
invocations); a CURRENT venv is a silent no-op. Reverting the ``pre_lock_check``
guard makes these fail: the spy would be invoked / a raw error raised.
"""

from __future__ import annotations

import hashlib

import pytest

from ne.memory_graph.sidecar import GraphitiSidecar
from ne.net.airgap import AirGapViolationError


def _write_requirements(env_dir) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("graphiti-core==0.29.2\n", encoding="utf-8")


def _build_current_venv(env_dir, venv_dir, *, marker: str | None = None) -> None:
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
    want = marker
    if want is None:
        want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want, encoding="utf-8")


def test_graphiti_ensure_air_gap_missing_venv_fails_closed_no_build_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "graphiti_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    sidecar = GraphitiSidecar(
        env_dir=env_dir,
        venv_dir=tmp_path / "venv-absent",
        run=lambda cmd, env=None: calls.append(cmd),
    )

    with pytest.raises(AirGapViolationError):
        sidecar.ensure()

    assert calls == []


def test_graphiti_ensure_air_gap_current_venv_is_silent_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "graphiti_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    sidecar = GraphitiSidecar(
        env_dir=env_dir, venv_dir=venv_dir, run=lambda cmd, env=None: calls.append(cmd)
    )

    assert sidecar.ensure() is None
    assert calls == []
