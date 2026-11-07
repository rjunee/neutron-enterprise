# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AIR_GAP fail-closed on the ragas venv builder + public entry (NE-FIX-GOVERN-EVAL).

The isolated ragas venv builder provisions itself with ``uv pip install``
(pypi.org / files.pythonhosted.org egress) on a cold/stale env. Under AIR_GAP
that runtime egress is forbidden (C2), so ``ensure_ragas_env_at`` passes an
``_air_gap_check`` closure as ``ensure_sidecar_venv``'s ``pre_lock_check`` hook:

* a missing/stale venv under AIR_GAP fails with a typed ``AirGapViolationError``
  BEFORE any ``uv`` build/egress (spy-proven ZERO ``_run`` invocations);
* a CURRENT venv is a silent no-op (the unlocked fast-path precedes the hook);
* the public-entry proof: ``run_cross_check(...)`` under AIR_GAP with an absent
  venv (env-var-overridden dirs) fails typed with zero build I/O.

Reverting the ``pre_lock_check`` hook to ``None`` makes these tests fail.
"""

from __future__ import annotations

import hashlib

import pytest

import ne.eval.ragas_oracle as ragas_oracle_module
from ne.eval.ragas_oracle import ensure_ragas_env_at, run_cross_check
from ne.net.airgap import AirGapViolationError


def _write_requirements(env_dir) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("some-pkg==1.2.3\n", encoding="utf-8")


def _build_current_venv(env_dir, venv_dir) -> None:
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
    want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want, encoding="utf-8")


def test_ensure_ragas_env_air_gap_missing_venv_fails_closed_no_build_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "ragas_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(ragas_oracle_module, "_run", calls.append)

    with pytest.raises(AirGapViolationError):
        ensure_ragas_env_at(env_dir, tmp_path / "venv-absent")

    assert calls == []  # no `uv venv` / `uv pip install` attempted


def test_ensure_ragas_env_air_gap_current_venv_is_silent_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "ragas_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(ragas_oracle_module, "_run", calls.append)

    assert ensure_ragas_env_at(env_dir, venv_dir) is None
    assert calls == []


def test_run_cross_check_under_air_gap_fails_closed_typed_no_build_io(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "ragas_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("NE_RAGAS_ENV_DIR", str(env_dir))
    monkeypatch.setenv("NE_RAGAS_VENV_DIR", str(tmp_path / "venv-absent"))
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(ragas_oracle_module, "_run", calls.append)

    with pytest.raises(AirGapViolationError):
        run_cross_check({"verdicts": []})

    assert calls == []  # zero build I/O; the raise precedes the cross_check.py subprocess
