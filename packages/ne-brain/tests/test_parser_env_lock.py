# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-020 P2a regression: the parser-env venv rebuild is lock-serialised.

Two concurrent ``parse()`` calls could both enter the venv (re)build path — one
removing/recreating ``venv_dir`` while the other installs into it — an
intermittent corruption. ``ensure_env_at`` now serialises the rebuild behind a
cross-process/thread :class:`filelock.FileLock` whose lock file is a SIBLING of
``venv_dir`` (so it survives the ``rmtree`` of ``venv_dir``), plus an in-lock
re-check of the marker fast-path so only the first waiter actually rebuilds.

The ``_run`` subprocess is monkeypatched (per the
``test_docling_parser_env_resolution.py`` pattern) — this test NEVER runs a real
``uv venv`` / ``uv pip install``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

import ne.brain.ingest.docling_parser as docling_parser_module
from ne.brain.ingest.docling_parser import _env_lock_path_at, ensure_env_at


def _write_requirements(env_dir: Path, body: str) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text(body)
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


class _FakeRunner:
    """Stand-in for ``_run`` that fakes ``uv venv`` / ``uv pip install`` shapes."""

    def __init__(self) -> None:
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
    env_dir = tmp_path / "parser_env"
    venv_dir = tmp_path / "state" / ".parser-venv"  # parent does not exist yet
    want = _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:deadbeef\n")

    runner = _FakeRunner()
    monkeypatch.setattr(docling_parser_module, "_run", runner)

    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            ensure_env_at(env_dir, venv_dir)
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
    env_dir = tmp_path / "parser_env"
    venv_dir = tmp_path / "state" / ".parser-venv"
    lock_path = _env_lock_path_at(venv_dir)

    # Structural guarantee: the lock file is NOT inside venv_dir, so the rmtree
    # of venv_dir during a rebuild cannot delete it.
    assert not lock_path.is_relative_to(venv_dir)
    assert not str(lock_path).startswith(str(venv_dir) + "/")

    monkeypatch.setattr(docling_parser_module, "_run", _FakeRunner())

    # First build.
    _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:aaaa\n")
    ensure_env_at(env_dir, venv_dir)
    assert (venv_dir / "bin" / "python").is_file()
    assert lock_path.exists()

    # Change requirements -> marker mismatch forces a rebuild that rmtree's
    # venv_dir. The lock file (a sibling) must survive; the venv is recreated.
    _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:bbbb\n")
    ensure_env_at(env_dir, venv_dir)
    assert lock_path.exists()
    assert (venv_dir / "bin" / "python").is_file()


def test_ensure_env_at_delegates_to_ne_envbuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-CKC8: ensure_env_at forwards the docling-specific shape to ne.envbuild."""
    env_dir = tmp_path / "parser_env"
    venv_dir = tmp_path / ".parser-venv"
    _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:aaaa\n")

    recorded: dict[str, object] = {}

    def _recorder(**kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(docling_parser_module, "ensure_sidecar_venv", _recorder)
    ensure_env_at(env_dir, venv_dir)

    assert recorded["env_dir"] == env_dir
    assert recorded["venv_dir"] == venv_dir
    assert recorded["install_args"] == ()
    assert recorded["pre_lock_check"] is not None
    assert recorded["run"] is docling_parser_module._run
    assert recorded["lock_timeout_s"] == docling_parser_module._ENV_LOCK_TIMEOUT_S


def test_parser_env_build_exports_exec_capable_uv_python_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV-EXEC: the parser venv (re)build must run `uv venv` with
    UV_PYTHON_INSTALL_DIR pointing at the venv's exec-capable sibling — not
    uv's $HOME default (a noexec tmpfs in the live connect container, where
    this failed every ~2 minutes for two days). FAILS ON MAIN."""
    import os

    from ne.envbuild import UV_PYTHON_DIR_LEAF, UV_PYTHON_INSTALL_DIR_VAR

    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    env_dir = tmp_path / "parser_env"
    venv_dir = tmp_path / "connect" / ".parser-venv"
    _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:deadbeef\n")

    seen: list[str | None] = []
    inner = _FakeRunner()

    def _recording_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        if cmd[:2] == ["uv", "venv"]:
            # Record what the subprocess would inherit: the explicit child env
            # when one is passed, else the ambient process environment.
            seen.append((env if env is not None else os.environ).get(UV_PYTHON_INSTALL_DIR_VAR))
        inner(cmd)

    monkeypatch.setattr(docling_parser_module, "_run", _recording_run)
    ensure_env_at(env_dir, venv_dir)

    assert seen == [str(venv_dir.parent / UV_PYTHON_DIR_LEAF)]
    assert UV_PYTHON_INSTALL_DIR_VAR not in os.environ
