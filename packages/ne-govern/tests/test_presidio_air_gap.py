# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AIR_GAP fail-closed on the presidio venv builder + C12 redaction path (NE-FIX-GOVERN-EVAL).

The isolated presidio venv builder provisions itself with ``uv pip install``
(pypi.org / files.pythonhosted.org egress) on a cold/stale env. Under AIR_GAP
that runtime egress is forbidden (C2), so ``ensure_env_at`` passes an
``_air_gap_check`` closure as ``ensure_sidecar_venv``'s ``pre_lock_check`` hook:

* a missing/stale venv under AIR_GAP fails with a typed ``AirGapViolationError``
  BEFORE any ``uv`` build/egress (spy-proven ZERO ``_run`` invocations);
* a CURRENT venv is a silent no-op (the unlocked fast-path precedes the hook);
* the SPEC-mandated C12 proof: ``PresidioRedactor.redact(...)`` under AIR_GAP
  with an absent venv fails typed with zero build I/O — the raise precedes the
  ``redact_text.py`` subprocess.

Reverting the ``pre_lock_check`` hook to ``None`` makes these tests fail.
"""

from __future__ import annotations

import hashlib

import pytest

import ne.govern.redaction.presidio_backend as presidio_backend_module
from ne.govern.redaction.presidio_backend import PresidioRedactor, ensure_env_at
from ne.net.airgap import AirGapViolationError


def _write_requirements(env_dir) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text("some-pkg==1.2.3\n", encoding="utf-8")


def _build_current_venv(env_dir, venv_dir) -> None:
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
    want = hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want, encoding="utf-8")


def test_ensure_env_air_gap_missing_venv_fails_closed_no_build_attempt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "presidio_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(presidio_backend_module, "_run", calls.append)

    with pytest.raises(AirGapViolationError):
        ensure_env_at(env_dir, tmp_path / "venv-absent")

    assert calls == []  # no `uv venv` / `uv pip install` attempted


def test_ensure_env_air_gap_current_venv_is_silent_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "presidio_env"
    _write_requirements(env_dir)
    venv_dir = tmp_path / "venv"
    _build_current_venv(env_dir, venv_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(presidio_backend_module, "_run", calls.append)

    assert ensure_env_at(env_dir, venv_dir) is None
    assert calls == []


def test_redact_under_air_gap_fails_closed_typed_no_build_io(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "presidio_env"
    _write_requirements(env_dir)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(presidio_backend_module, "_run", calls.append)

    redactor = PresidioRedactor(presidio_env_dir=env_dir, venv_dir=tmp_path / "venv-absent")
    with pytest.raises(AirGapViolationError):
        redactor.redact("John Smith called")

    assert calls == []  # zero build I/O; the raise precedes the redact_text.py subprocess
