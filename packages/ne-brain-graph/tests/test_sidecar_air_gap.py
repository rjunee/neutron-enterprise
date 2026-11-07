# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AIR_GAP fail-closed on the LightRAG + Splink sidecar venv builders (C2).

Under AIR_GAP an absent/stale sidecar venv must fail with a typed
``AirGapViolationError`` BEFORE any ``uv`` build/egress (spy-proven zero ``run``
invocations, and the raised error is the typed one — NOT a raw uv/subprocess
error). A CURRENT venv is a silent no-op. Reverting the ``pre_lock_check`` guard
makes these fail: the spy would be called (or a raw error raised) instead.
"""

from __future__ import annotations

import hashlib

import pytest

from ne.brain_graph.er._splink import SplinkSidecar
from ne.brain_graph.lightrag._lightrag import LightRagSidecar
from ne.net.airgap import AirGapViolationError


def _write_requirements(env_dir) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("some-pkg==1.2.3\n", encoding="utf-8")


def _build_current_venv(env_dir, venv_dir, *, marker: str | None = None) -> None:
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
    want = marker
    if want is None:
        want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want, encoding="utf-8")


def test_lightrag_ensure_air_gap_missing_venv_fails_closed_no_build_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "lightrag_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    sidecar = LightRagSidecar(
        env_dir=env_dir,
        venv_dir=tmp_path / "venv-absent",
        run=lambda cmd, env=None: calls.append(cmd),
    )

    with pytest.raises(AirGapViolationError):
        sidecar.ensure()

    assert calls == []  # no `uv venv` / `uv pip install` attempted


def test_lightrag_ensure_air_gap_current_venv_is_silent_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "lightrag_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    sidecar = LightRagSidecar(
        env_dir=env_dir, venv_dir=venv_dir, run=lambda cmd, env=None: calls.append(cmd)
    )

    assert sidecar.ensure() is None
    assert calls == []


def test_splink_ensure_air_gap_missing_venv_fails_closed_no_build_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "splink_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    sidecar = SplinkSidecar(
        env_dir=env_dir,
        venv_dir=tmp_path / "venv-absent",
        run=lambda cmd, env=None: calls.append(cmd),
    )

    with pytest.raises(AirGapViolationError):
        sidecar.ensure()

    assert calls == []


def test_splink_ensure_air_gap_current_venv_is_silent_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "splink_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    sidecar = SplinkSidecar(
        env_dir=env_dir, venv_dir=venv_dir, run=lambda cmd, env=None: calls.append(cmd)
    )

    assert sidecar.ensure() is None
    assert calls == []


def test_splink_air_gap_fires_at_writable_root_resolved_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV C2: AIR_GAP fail-closed fires at the NE_SIDECAR_VENV_ROOT-resolved
    path — the error names the RESOLVED venv, no build is attempted, and nothing is
    written under the writable root."""
    env_dir = tmp_path / "splink_env"
    _write_requirements(env_dir)
    root = tmp_path / "writable"
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root))
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    # No venv_dir kwarg: it must resolve to $NE_SIDECAR_VENV_ROOT/.splink-venv.
    sidecar = SplinkSidecar(env_dir=env_dir, run=lambda cmd, env=None: calls.append(cmd))
    resolved = root / ".splink-venv"
    assert sidecar.venv_dir == resolved

    with pytest.raises(AirGapViolationError) as exc_info:
        sidecar.ensure()

    assert str(resolved) in str(exc_info.value)  # error names the RESOLVED path
    assert calls == []  # no `uv venv` / `uv pip install` attempted
    assert not root.exists()  # nothing created under the writable root
