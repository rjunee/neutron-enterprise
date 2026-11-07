# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKC8 shared TOCTOU proof suite for ``ne.envbuild.sidecar``.

Proves the consolidated env-builder: atomic marker write (same-dir tmp +
``os.replace``; crash mid-write preserves the OLD complete marker; no tmp
litter), NO false skip on any degenerate marker (empty/short/partial/mismatched/
unreadable/missing -> always rebuild, never raise), a concurrent reader never
observes a partial marker, 4-thread ensure serialised to exactly one install
with the authoritative under-lock re-check, the unlocked fast-path is provably
lock-free, ``pre_lock_check`` ordering (stale-only, before lock/mkdir), and the
lock file is a rmtree-surviving sibling.

The ``uv`` subprocess is faked — this test NEVER runs a real ``uv venv`` / ``uv
pip install``.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from ne.envbuild import (
    ensure_sidecar_venv,
    env_lock_path,
    marker_path,
    read_marker,
    venv_python,
    write_marker_atomic,
)

_HEX64 = "a" * 64
_HEX64_B = "b" * 64


def _write_requirements(
    env_dir: Path, body: str = "docling==2.110.0 --hash=sha256:deadbeef\n"
) -> str:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "requirements.txt").write_text(body)
    return hashlib.sha256((env_dir / "requirements.txt").read_bytes()).hexdigest()


class _FakeRunner:
    """Stand-in for ``run`` that fakes ``uv venv`` / ``uv pip install`` shapes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.install_calls = 0
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        with self._lock:
            self.commands.append(cmd)
        if cmd[:2] == ["uv", "venv"]:
            venv_dir = Path(cmd[2])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("#!fake-python\n")
            return
        if cmd[:3] == ["uv", "pip", "install"]:
            python = Path(cmd[4])
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


def _seed_current_venv(env_dir: Path, venv_dir: Path, marker_value: str) -> None:
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("#!fake-python\n")
    marker_path(venv_dir).write_text(marker_value)


def test_fresh_build_writes_marker_atomically(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".venv"  # parent does not exist yet
    want = _write_requirements(env_dir)
    runner = _FakeRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert runner.install_calls == 1
    assert read_marker(marker_path(venv_dir)) == want
    # No leftover temp files from the atomic write.
    assert list(venv_dir.glob("*.tmp")) == []


def test_up_to_date_fast_path_touches_nothing(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    want = _write_requirements(env_dir)
    _seed_current_venv(env_dir, venv_dir, want)
    runner = _FakeRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    assert runner.install_calls == 0
    assert runner.commands == []
    # The fast path is lock-free: no lock file was ever created.
    assert not env_lock_path(venv_dir).exists()


@pytest.mark.parametrize("precontent_kind", ["empty", "prefix16", "prefix63", "wrong64", "garbage"])
def test_stale_or_partial_marker_forces_rebuild(tmp_path: Path, precontent_kind: str) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    want = _write_requirements(env_dir)
    precontents = {
        "empty": "",
        "prefix16": want[:16],
        "prefix63": want[:63],
        "wrong64": "0" * 64,
        "garbage": want + "garbage",
    }
    _seed_current_venv(env_dir, venv_dir, precontents[precontent_kind])
    runner = _FakeRunner()

    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    # A partial/short/mismatched marker must NEVER skip — exactly one rebuild,
    # marker repaired to the true want.
    assert runner.install_calls == 1
    assert read_marker(marker_path(venv_dir)) == want


def test_unreadable_marker_is_fail_safe(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    want = _write_requirements(env_dir)
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("#!fake-python\n")
    # Marker path is a DIRECTORY -> read_text raises OSError -> read_marker None.
    marker_path(venv_dir).mkdir()
    runner = _FakeRunner()

    # Must not raise; rebuilds (rmtree clears the offending directory).
    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)
    assert runner.install_calls == 1
    assert read_marker(marker_path(venv_dir)) == want

    # And a missing marker path reads as None (fail-safe).
    assert read_marker(tmp_path / "does-not-exist") is None


def test_marker_write_uses_same_dir_tmp_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / ".venv"
    _write_requirements(env_dir)
    runner = _FakeRunner()

    real_replace = os.replace
    recorded: list[tuple[Path, Path]] = []

    def _recording_replace(src, dst):  # type: ignore[no-untyped-def]
        recorded.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _recording_replace)
    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)

    marker = marker_path(venv_dir)
    assert len(recorded) == 1
    src, dst = recorded[0]
    assert src.parent == marker.parent, "temp source must be in the marker's dir"
    assert src.name.startswith(marker.name + "."), src.name
    assert dst == marker


def test_concurrent_reader_never_observes_partial_marker(tmp_path: Path) -> None:
    marker = tmp_path / "m.sha256"
    observed: list[str] = []
    stop = threading.Event()

    def _writer() -> None:
        for i in range(200):
            write_marker_atomic(marker, _HEX64 if i % 2 == 0 else _HEX64_B)
        stop.set()

    def _reader() -> None:
        while not stop.is_set():
            try:
                value = marker.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                continue
            observed.append(value)

    w = threading.Thread(target=_writer)
    r = threading.Thread(target=_reader)
    r.start()
    w.start()
    w.join()
    r.join()

    assert observed, "reader saw nothing"
    for value in observed:
        assert value in (_HEX64, _HEX64_B), f"partial/torn marker observed: {value!r}"


def test_concurrent_ensure_serialized_single_build(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".venv"
    want = _write_requirements(env_dir)
    runner = _FakeRunner()
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, lock_timeout_s=60, run=runner)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert runner.max_in_flight == 1
    assert runner.install_calls == 1
    assert read_marker(marker_path(venv_dir)) == want


def test_crash_during_marker_write_preserves_old_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "m.sha256"
    marker.write_text(_HEX64)  # a complete OLD marker

    def _boom(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        write_marker_atomic(marker, _HEX64_B)

    # The old complete marker survived (a naive write_text would have truncated),
    assert marker.read_text() == _HEX64
    # and the temp file was cleaned up.
    assert list(tmp_path.glob("*.tmp")) == []


def test_pre_lock_check_only_on_stale_path(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    want = _write_requirements(env_dir)

    class _Sentinel(Exception):
        pass

    def _raiser() -> None:
        raise _Sentinel()

    # Current env: fast-path returns BEFORE pre_lock_check, no raise, no calls.
    current_venv = tmp_path / ".venv-current"
    _seed_current_venv(env_dir, current_venv, want)
    runner_a = _FakeRunner()
    ensure_sidecar_venv(
        env_dir=env_dir, venv_dir=current_venv, pre_lock_check=_raiser, run=runner_a
    )
    assert runner_a.commands == []

    # Stale (absent) env: pre_lock_check fires BEFORE any lock/mkdir/build.
    stale_venv = tmp_path / "state" / ".venv-stale"
    runner_b = _FakeRunner()
    with pytest.raises(_Sentinel):
        ensure_sidecar_venv(
            env_dir=env_dir, venv_dir=stale_venv, pre_lock_check=_raiser, run=runner_b
        )
    assert runner_b.commands == []
    assert not env_lock_path(stale_venv).exists()


def test_lock_file_is_sibling_of_venv_dir(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    venv_dir = tmp_path / "state" / ".venv"
    lock_path = env_lock_path(venv_dir)

    # Structural: the lock file is NOT inside venv_dir, so a rebuild rmtree can't
    # delete it.
    assert not lock_path.is_relative_to(venv_dir)
    assert not str(lock_path).startswith(str(venv_dir) + "/")

    runner = _FakeRunner()
    _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:aaaa\n")
    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)
    assert venv_python(venv_dir).is_file()
    assert lock_path.exists()

    # Requirements change -> marker mismatch -> rebuild rmtree's venv_dir; the
    # sibling lock survives and the venv is recreated.
    _write_requirements(env_dir, "docling==2.110.0 --hash=sha256:bbbb\n")
    ensure_sidecar_venv(env_dir=env_dir, venv_dir=venv_dir, run=runner)
    assert lock_path.exists()
    assert venv_python(venv_dir).is_file()
