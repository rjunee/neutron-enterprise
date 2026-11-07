# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AIR_GAP fail-closed on the linkml codegen venv builder (C2, NE-CKC10 / P2-23).

Under AIR_GAP an absent/stale codegen venv must fail with a typed
``AirGapViolationError`` BEFORE any ``uv`` build/egress (spy-proven zero ``_run``
invocations); a CURRENT venv is a silent no-op; with AIR_GAP unset a stale venv
rebuilds (the guard only ever ADDS restriction). Mirrors
``packages/ne-memory-graph/tests/test_sidecar_air_gap.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ne.net.airgap import AirGapViolationError
from ne.schema import codegen

_CODEGEN_REL = Path("packages") / "ne-schema" / "codegen"
_VENV_REL = Path("packages") / "ne-schema" / ".codegen-venv"


def _write_requirements(root: Path) -> Path:
    codegen_dir = root / _CODEGEN_REL
    codegen_dir.mkdir(parents=True, exist_ok=True)
    (codegen_dir / "requirements.txt").write_text("linkml==1.8.0\n", encoding="utf-8")
    return codegen_dir


def _build_venv(root: Path, *, marker: str | None = None) -> None:
    venv_dir = root / _VENV_REL
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("", encoding="utf-8")
    want = marker
    if want is None:
        want = hashlib.sha256((root / _CODEGEN_REL / "requirements.txt").read_bytes()).hexdigest()
    (venv_dir / ".requirements.sha256").write_text(want, encoding="utf-8")


def test_air_gap_missing_venv_fails_closed_no_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_requirements(tmp_path)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(codegen, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(AirGapViolationError):
        codegen.ensure_env(tmp_path)

    assert calls == []


def test_air_gap_stale_venv_fails_closed_no_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_requirements(tmp_path)
    _build_venv(tmp_path, marker="stale-marker-does-not-match")
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(codegen, "_run", lambda cmd: calls.append(cmd))

    with pytest.raises(AirGapViolationError):
        codegen.ensure_env(tmp_path)

    assert calls == []


def test_air_gap_current_venv_is_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_requirements(tmp_path)
    _build_venv(tmp_path)
    monkeypatch.setenv("AIR_GAP", "1")
    calls: list[list[str]] = []
    monkeypatch.setattr(codegen, "_run", lambda cmd: calls.append(cmd))

    assert codegen.ensure_env(tmp_path) is None
    assert calls == []


def test_air_gap_unset_stale_venv_rebuilds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_requirements(tmp_path)
    _build_venv(tmp_path, marker="stale-marker-does-not-match")
    monkeypatch.delenv("AIR_GAP", raising=False)
    calls: list[list[str]] = []
    venv_dir = tmp_path / _VENV_REL

    def _fake_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        calls.append(cmd)
        # Stand in for `uv venv`: recreate the dir the rebuild rmtree'd so the
        # marker write at the end of the (mocked) build succeeds.
        venv_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(codegen, "_run", _fake_run)

    codegen.ensure_env(tmp_path)

    # The guard only ADDS restriction: with AIR_GAP unset the stale venv rebuilds.
    assert calls, "expected the venv builder to invoke _run when AIR_GAP is unset"
