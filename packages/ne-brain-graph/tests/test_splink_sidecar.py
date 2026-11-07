# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Splink sidecar: env-builder shape (fake runner) + LIVE resolve (parked).

The env-builder shape is asserted with a monkeypatched ``_run`` — this test NEVER
runs a real ``uv venv`` / ``uv pip install`` (the ``test_ragas_env_lock``
precedent): a full hash-pinned install (``--require-hashes``, no ``--no-deps``) of
``splink_env/requirements.txt`` into a ``.splink-venv``. The LIVE subprocess
``resolve()`` is skipped unless the venv is actually built (the ragas live-parked
precedent).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ne.brain_graph.er._splink import SplinkSidecar, default_splink_env_dir


class _FakeRunner:
    """Fakes ``uv venv`` / ``uv pip install`` shapes; asserts the install contract."""

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
            assert "--require-hashes" in cmd, "the Splink sidecar install must be hash-pinned"
            assert "--no-deps" not in cmd, "Splink installs its FULL hash-pinned closure"
            assert cmd[-2:] == ["-r", str(self._env_dir / "requirements.txt")], cmd
            self.install_calls += 1
            return
        raise AssertionError(f"unexpected command: {cmd!r}")


def _write_requirements(env_dir: Path, body: str) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text(body)
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


def test_default_env_dir_points_at_in_repo_splink_env() -> None:
    env_dir = default_splink_env_dir()
    assert env_dir.name == "splink_env"
    assert (env_dir / "requirements.in").is_file()
    assert (env_dir / "resolve.py").is_file()


def test_ensure_builds_hash_pinned_venv(tmp_path: Path) -> None:
    env_dir = tmp_path / "splink_env"
    venv_dir = tmp_path / ".splink-venv"
    want = _write_requirements(env_dir, "splink==4.0.16 --hash=sha256:deadbeef\n")
    runner = _FakeRunner(env_dir)
    sidecar = SplinkSidecar(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert sidecar.available() is False  # not built yet
    sidecar.ensure()
    assert runner.install_calls == 1
    # The marker records the committed requirements sha256; available() now True.
    assert sidecar.available() is True
    assert (venv_dir / ".requirements.sha256").read_text() == want

    # A second ensure is a no-op (marker still matches).
    sidecar.ensure()
    assert runner.install_calls == 1


def test_ensure_rebuilds_when_requirements_change(tmp_path: Path) -> None:
    env_dir = tmp_path / "splink_env"
    venv_dir = tmp_path / ".splink-venv"
    _write_requirements(env_dir, "splink==4.0.16 --hash=sha256:aaa\n")
    runner = _FakeRunner(env_dir)
    sidecar = SplinkSidecar(env_dir=env_dir, venv_dir=venv_dir, run=runner)
    sidecar.ensure()
    assert runner.install_calls == 1

    _write_requirements(env_dir, "splink==4.0.16 --hash=sha256:bbb\n")  # changed
    assert sidecar.available() is False  # marker no longer matches
    sidecar.ensure()
    assert runner.install_calls == 2


def test_venv_dir_honors_sidecar_venv_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NE-SVENV: with NE_SIDECAR_VENV_ROOT set, venv_dir resolves under it."""
    root = tmp_path / "writable"
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(root))
    sidecar = SplinkSidecar(env_dir=tmp_path / "splink_env")
    assert sidecar.venv_dir == root / ".splink-venv"


def test_explicit_venv_dir_beats_sidecar_venv_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV: an explicit venv_dir= kwarg wins over NE_SIDECAR_VENV_ROOT."""
    monkeypatch.setenv("NE_SIDECAR_VENV_ROOT", str(tmp_path / "writable"))
    explicit = tmp_path / "explicit" / ".splink-venv"
    sidecar = SplinkSidecar(env_dir=tmp_path / "splink_env", venv_dir=explicit)
    assert sidecar.venv_dir == explicit
