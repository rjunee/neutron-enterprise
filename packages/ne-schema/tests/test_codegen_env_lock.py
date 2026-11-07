# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKC8: codegen's ensure_env is now lock-serialised (it never was before).

Before NE-CKC8 ``ne.schema.codegen.ensure_env`` had NO lock at all — two
concurrent runs (e.g. ``generate`` and ``check_freshness`` in parallel CI shards)
raced the ``rmtree``/install directly. It now delegates to
``ne.envbuild.ensure_sidecar_venv``, gaining the cross-process/thread filelock
serialisation + atomic marker. This ports the presidio/docling concurrency proof
to codegen and additionally asserts the ``--overrides`` install shape.

The ``_run`` subprocess is monkeypatched — this test NEVER runs real ``uv``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

import ne.schema.codegen as codegen_module
from ne.schema.codegen import _marker_path, ensure_env


def _lay_out_codegen(root: Path, req_body: str) -> str:
    codegen_dir = root / "packages" / "ne-schema" / "codegen"
    codegen_dir.mkdir(parents=True, exist_ok=True)
    (codegen_dir / "requirements.txt").write_text(req_body)
    (codegen_dir / "overrides.txt").write_text("jsonschema[format-nongpl]\n")
    return hashlib.sha256((codegen_dir / "requirements.txt").read_bytes()).hexdigest()


class _FakeRunner:
    """Stand-in for ``_run`` asserting the codegen install shape."""

    def __init__(self, root: Path) -> None:
        self._root = root
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
            expected = self._root / "packages" / "ne-schema" / ".codegen-venv" / "bin" / "python"
            assert cmd[4] == str(expected), cmd[4]
            assert "--overrides" in cmd, "codegen install must pass --overrides"
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
    want = _lay_out_codegen(tmp_path, "linkml==1.11.1 --hash=sha256:deadbeef\n")
    runner = _FakeRunner(tmp_path)
    monkeypatch.setattr(codegen_module, "_run", runner)

    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            ensure_env(tmp_path)
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
    assert _marker_path(tmp_path).read_text().strip() == want


def test_stale_marker_rebuilds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    want = _lay_out_codegen(tmp_path, "linkml==1.11.1 --hash=sha256:aaaa\n")
    runner = _FakeRunner(tmp_path)
    monkeypatch.setattr(codegen_module, "_run", runner)

    # Pre-seed a current venv with a STALE (partial/wrong) marker.
    venv_dir = tmp_path / "packages" / "ne-schema" / ".codegen-venv"
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    (venv_dir / "bin" / "python").write_text("#!fake-python\n")
    _marker_path(tmp_path).write_text(want[:16])  # short/partial -> must rebuild

    ensure_env(tmp_path)

    assert runner.install_calls == 1
    assert _marker_path(tmp_path).read_text().strip() == want


def test_codegen_env_build_exports_exec_capable_uv_python_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-SVENV-EXEC: the linkml codegen venv (re)build shares the docling
    parser's defect — a managed 3.12 download lands in uv's $HOME default,
    which is a noexec tmpfs in hardened containers. The shared builder must
    export UV_PYTHON_INSTALL_DIR at the venv's sibling. FAILS ON MAIN."""
    import os

    from ne.envbuild import UV_PYTHON_DIR_LEAF, UV_PYTHON_INSTALL_DIR_VAR

    monkeypatch.delenv(UV_PYTHON_INSTALL_DIR_VAR, raising=False)
    _lay_out_codegen(tmp_path, "linkml==1.11.1 --hash=sha256:deadbeef\n")
    venv_dir = tmp_path / "packages" / "ne-schema" / ".codegen-venv"

    seen: list[str | None] = []
    inner = _FakeRunner(tmp_path)

    def _recording_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        if cmd[:2] == ["uv", "venv"]:
            # Record what the subprocess would inherit: the explicit child env
            # when one is passed, else the ambient process environment.
            seen.append((env if env is not None else os.environ).get(UV_PYTHON_INSTALL_DIR_VAR))
        inner(cmd)

    monkeypatch.setattr(codegen_module, "_run", _recording_run)
    ensure_env(tmp_path)

    assert seen == [str(venv_dir.parent / UV_PYTHON_DIR_LEAF)]
    assert UV_PYTHON_INSTALL_DIR_VAR not in os.environ
