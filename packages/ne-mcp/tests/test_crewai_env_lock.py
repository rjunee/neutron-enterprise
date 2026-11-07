# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CREWAI-DROPIN — the crewai sidecar-venv builder delegates to ne.envbuild.

Mirrors ``packages/ne-mcp/tests/test_langgraph_env_lock.py``: ``ensure_crewai_env_at``
forwards the ragas/langgraph-shaped install closure to
:func:`ne.envbuild.ensure_sidecar_venv` — a FULL hash-pinned install
(``--require-hashes``, NO ``--no-deps``) of ``crewai_env/requirements.txt``, serialised
behind the cross-process filelock, with the AIR_GAP fail-closed ``pre_lock_check`` hook
(C2).

The ``_run`` subprocess is monkeypatched — this test NEVER runs a real ``uv venv`` /
``uv pip install``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

import ne.mcp.crewai_demo as crewai_demo_module
from ne.envbuild import env_lock_path
from ne.mcp.crewai_demo import ensure_crewai_env_at
from ne.net.airgap import AirGapViolationError


def _write_requirements(env_dir: Path, body: str) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text(body)
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


class _FakeRunner:
    """Stand-in for ``_run`` that fakes ``uv venv`` / ``uv pip install`` shapes."""

    def __init__(self, env_dir: Path | None = None) -> None:
        self._env_dir = env_dir
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.install_calls = 0

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        if cmd[:2] == ["uv", "venv"]:
            venv_dir = Path(cmd[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!fake-python\n")
            return
        if cmd[:3] == ["uv", "pip", "install"]:
            python = Path(cmd[4])
            assert python.is_file(), "venv python missing on install entry"
            assert "--require-hashes" in cmd, "install must be hash-pinned"
            assert "--no-deps" not in cmd, (
                "the crewai demo installs its FULL hash-pinned closure (the ragas shape)"
            )
            if self._env_dir is not None:
                assert cmd[-2:] == ["-r", str(self._env_dir / "requirements.txt")], cmd
            with self._lock:
                self.in_flight += 1
                self.install_calls += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            time.sleep(0.2)
            assert python.is_file(), "venv python vanished during install"
            with self._lock:
                self.in_flight -= 1
            return
        raise AssertionError(f"unexpected command: {cmd!r}")


def test_concurrent_ensure_env_rebuild_serialized_single_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "crewai_env"
    venv_dir = tmp_path / "state" / ".crewai-venv"  # parent does not exist yet
    want = _write_requirements(env_dir, "crewai==1.15.5 --hash=sha256:deadbeef\n")

    runner = _FakeRunner(env_dir)
    monkeypatch.setattr(crewai_demo_module, "_run", runner)

    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            ensure_crewai_env_at(env_dir, venv_dir)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    # The lock serialised the rebuild: never two installs at once.
    assert runner.max_in_flight == 1
    # The in-lock recheck meant only the FIRST waiter actually rebuilt.
    assert runner.install_calls == 1
    marker = venv_dir / ".requirements.sha256"
    assert marker.read_text().strip() == want


def test_air_gap_engaged_refuses_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "crewai_env"
    venv_dir = tmp_path / ".crewai-venv"  # absent -> stale
    _write_requirements(env_dir, "crewai==1.15.5 --hash=sha256:aaaa\n")

    runner = _FakeRunner(env_dir)
    monkeypatch.setattr(crewai_demo_module, "_run", runner)
    monkeypatch.setenv("AIR_GAP", "1")

    with pytest.raises(AirGapViolationError):
        ensure_crewai_env_at(env_dir, venv_dir)

    # Fail-closed BEFORE any subprocess: the runner was never invoked.
    assert runner.install_calls == 0
    assert not (venv_dir / "bin" / "python").exists()


def test_air_gap_up_to_date_is_a_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "crewai_env"
    venv_dir = tmp_path / ".crewai-venv"
    _write_requirements(env_dir, "crewai==1.15.5 --hash=sha256:bbbb\n")

    runner = _FakeRunner(env_dir)
    monkeypatch.setattr(crewai_demo_module, "_run", runner)

    # Build once (no air gap), then engage AIR_GAP: an up-to-date venv is a no-op.
    ensure_crewai_env_at(env_dir, venv_dir)
    assert runner.install_calls == 1

    monkeypatch.setenv("AIR_GAP", "1")
    ensure_crewai_env_at(env_dir, venv_dir)  # must NOT raise, must NOT rebuild
    assert runner.install_calls == 1


def test_lock_file_lives_outside_venv_dir(tmp_path: Path) -> None:
    venv_dir = tmp_path / "state" / ".crewai-venv"
    lock_path = env_lock_path(venv_dir)
    # The lock file is a SIBLING of venv_dir, so a rebuild rmtree cannot delete it.
    assert not lock_path.is_relative_to(venv_dir)
    assert not str(lock_path).startswith(str(venv_dir) + "/")
