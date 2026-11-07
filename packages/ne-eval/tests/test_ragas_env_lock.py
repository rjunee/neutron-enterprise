# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKC13: the 4th env-builder copy now delegates to ne.envbuild.

``ragas_oracle.ensure_ragas_env_at`` was the FOURTH bespoke sidecar env-builder
copy — the same unlocked ``.requirements.sha256`` fast-path TOCTOU (an
``is_file() -> read_text()`` probe race against a non-atomic ``write_text``) that
NE-CKC8 fixed in the docling, presidio, and codegen builders, left out of that
unit's lane. It now delegates to :func:`ne.envbuild.ensure_sidecar_venv`, gaining
the cross-process/thread filelock serialisation, the atomic (tmp + os.replace)
marker write, and the authoritative under-lock re-check. This ports the
presidio/docling/codegen concurrency + fail-safe proofs to the ragas oracle and
additionally asserts the ragas-specific install shape: a full hash-pinned
closure install (``--require-hashes``, no ``--no-deps``) of
``ragas_env/requirements.txt``.

The ``_run`` subprocess is monkeypatched — this test NEVER runs a real ``uv venv``
/ ``uv pip install``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

import ne.eval.ragas_oracle as ragas_oracle_module
from ne.envbuild import env_lock_path
from ne.eval.ragas_oracle import ensure_ragas_env_at
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
            # The venv python must already exist on entry (the `uv venv` step ran)
            # and must NOT be yanked out from under us mid-install by a racer.
            assert python.is_file(), "venv python missing on install entry"
            assert "--require-hashes" in cmd, "install must be hash-pinned"
            assert "--no-deps" not in cmd, (
                "ragas installs its FULL hash-pinned closure, unlike presidio"
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
    env_dir = tmp_path / "ragas_env"
    venv_dir = tmp_path / "state" / ".ragas-venv"  # parent does not exist yet
    want = _write_requirements(env_dir, "ragas==0.4.3 --hash=sha256:deadbeef\n")

    runner = _FakeRunner(env_dir)
    monkeypatch.setattr(ragas_oracle_module, "_run", runner)

    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            ensure_ragas_env_at(env_dir, venv_dir)
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
    # And the in-lock recheck meant only the FIRST waiter actually rebuilt; the
    # other three hit the fast-path recheck and returned.
    assert runner.install_calls == 1
    # Correct marker written for the requirements content.
    marker = venv_dir / ".requirements.sha256"
    assert marker.read_text().strip() == want


def test_lock_file_lives_outside_venv_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "ragas_env"
    venv_dir = tmp_path / "state" / ".ragas-venv"
    lock_path = env_lock_path(venv_dir)

    # Structural guarantee: the lock file is NOT inside venv_dir, so the rmtree
    # of venv_dir during a rebuild cannot delete it.
    assert not lock_path.is_relative_to(venv_dir)
    assert not str(lock_path).startswith(str(venv_dir) + "/")

    monkeypatch.setattr(ragas_oracle_module, "_run", _FakeRunner(env_dir))

    # First build.
    _write_requirements(env_dir, "ragas==0.4.3 --hash=sha256:aaaa\n")
    ensure_ragas_env_at(env_dir, venv_dir)
    assert (venv_dir / "bin" / "python").is_file()
    assert lock_path.exists()

    # Change requirements -> marker mismatch forces a rebuild that rmtree's
    # venv_dir. The lock file (a sibling) must survive; the venv is recreated.
    _write_requirements(env_dir, "ragas==0.4.3 --hash=sha256:bbbb\n")
    ensure_ragas_env_at(env_dir, venv_dir)
    assert lock_path.exists()
    assert (venv_dir / "bin" / "python").is_file()


def test_stale_marker_rebuilds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "ragas_env"
    venv_dir = tmp_path / ".ragas-venv"
    want = _write_requirements(env_dir, "ragas==0.4.3 --hash=sha256:cccc\n")

    runner = _FakeRunner(env_dir)
    monkeypatch.setattr(ragas_oracle_module, "_run", runner)

    # Pre-seed a current venv with a STALE (truncated/partial) marker.
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("#!fake-python\n")
    (venv_dir / ".requirements.sha256").write_text(want[:16])  # partial -> must rebuild

    ensure_ragas_env_at(env_dir, venv_dir)

    # Fail-safe: any doubt about the marker rebuilds, never a false skip.
    assert runner.install_calls == 1
    assert (venv_dir / ".requirements.sha256").read_text().strip() == want


def test_ensure_ragas_env_at_delegates_to_ne_envbuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-CKC13: ensure_ragas_env_at forwards the ragas-specific shape to ne.envbuild.

    Ragas installs its FULL hash-pinned closure (no ``--no-deps``, unlike
    presidio) and (since NE-FIX-GOVERN-EVAL) passes the AIR_GAP fail-closed
    ``pre_lock_check`` hook — a callable that raises ``AirGapViolationError``
    under AIR_GAP and is a no-op otherwise.
    """
    env_dir = tmp_path / "ragas_env"
    venv_dir = tmp_path / ".ragas-venv"
    _write_requirements(env_dir, "ragas==0.4.3 --hash=sha256:aaaa\n")

    recorded: dict[str, object] = {}

    def _recorder(**kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(ragas_oracle_module, "ensure_sidecar_venv", _recorder)
    ensure_ragas_env_at(env_dir, venv_dir)

    assert recorded["env_dir"] == env_dir
    assert recorded["venv_dir"] == venv_dir
    assert recorded["install_args"] == ()
    hook = recorded["pre_lock_check"]
    assert callable(hook)
    monkeypatch.setenv("AIR_GAP", "1")
    with pytest.raises(AirGapViolationError):
        hook()
    monkeypatch.delenv("AIR_GAP", raising=False)
    assert hook() is None
    assert recorded["run"] is ragas_oracle_module._run
    assert recorded["lock_timeout_s"] == ragas_oracle_module._ENV_LOCK_TIMEOUT_S
