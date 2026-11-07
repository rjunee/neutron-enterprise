# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""P2-17: DockerRunner.run_stream plumbs stdin/stdout to OPEN FILE HANDLES (never
buffered PIPEs / input=), keeping dump/restore memory O(pipe buffer), while
preserving run()'s non-zero-exit + stderr and docker-missing error handling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ne.backup.errors import NeBackupError
from ne.backup.runner import DockerRunner


class _FakeProc:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


def test_run_stream_passes_file_handles_not_pipes(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr("ne.backup.runner.subprocess.run", fake_run)

    (tmp_path / "in").write_bytes(b"payload")
    with (tmp_path / "out").open("wb") as out, (tmp_path / "in").open("rb") as inp:
        DockerRunner().run_stream(
            ["exec", "-i", "c", "pg_restore"], stdin_file=inp, stdout_file=out
        )
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["stdout"] is out
        assert kwargs["stdin"] is inp
        assert kwargs["stderr"] is subprocess.PIPE
        assert "input" not in kwargs


def test_run_stream_raises_on_nonzero_with_stderr_message(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        return _FakeProc(returncode=3, stderr=b"pg_dump: error: boom")

    monkeypatch.setattr("ne.backup.runner.subprocess.run", fake_run)
    with pytest.raises(NeBackupError) as exc:
        DockerRunner().run_stream(["exec", "c", "pg_dump", "-Fc", "neutron"])
    message = str(exc.value)
    assert "exit 3" in message
    assert "boom" in message


def test_run_stream_docker_missing_maps_to_nebackuperror(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("ne.backup.runner.subprocess.run", fake_run)
    with pytest.raises(NeBackupError, match="Docker is not installed"):
        DockerRunner().run_stream(["exec", "c", "pg_dump"])
