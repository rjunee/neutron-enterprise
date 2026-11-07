# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TEI-GEN demo wiring: the REAL `_stage_tei_cache` body — default path
generates (no --no-generate, no --onnx-artifact, no candle-fallback warning),
and the --tei-onnx-artifact override resolves its export manifest sibling-first
then committed, else refuses. subprocess.run + _find_repo_root are seam-mocked;
no docker/network anywhere."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from ne.cli.demo import live


def _mock_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def _mock_repo_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    # _stage_tei_cache does `from ne.cli.checks import _find_repo_root` inside the
    # function, so patch it on that module.
    import ne.cli.checks as checks

    monkeypatch.setattr(checks, "_find_repo_root", lambda _cwd: root)


def _stage_cmd(calls: list[list[str]]) -> list[str]:
    for cmd in calls:
        if "stage-tei" in cmd:
            return cmd
    raise AssertionError(f"no stage-tei command found in {calls!r}")


def test_default_generates_no_flags_no_candle_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _mock_subprocess(monkeypatch)
    live._stage_tei_cache(tmp_path / "workdir", None)

    cmd = _stage_cmd(calls)
    assert "--no-generate" not in cmd
    assert "--onnx-artifact" not in cmd
    out = capsys.readouterr().out
    # the old candle-fallback warning is gone; the new message sets the
    # generate-on-demand expectation.
    assert "stays on the slow candle CPU fallback" not in out
    assert "generate-on-demand cache" in out


def test_artifact_override_uses_sibling_manifest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _mock_subprocess(monkeypatch)
    repo_root = tmp_path / "repo"
    _mock_repo_root(monkeypatch, repo_root)

    artifact_dir = tmp_path / "bundle"
    artifact_dir.mkdir()
    artifact = artifact_dir / "qwen3.tar.gz"
    artifact.write_bytes(b"x")
    sibling = artifact_dir / "export-manifest.json"
    sibling.write_text("{}", encoding="utf-8")

    live._stage_tei_cache(tmp_path / "workdir", artifact)

    cmd = _stage_cmd(calls)
    assert "--onnx-artifact" in cmd
    assert str(artifact) in cmd
    mi = cmd.index("--export-manifest")
    assert cmd[mi + 1] == str(sibling)


def test_artifact_override_falls_back_to_committed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _mock_subprocess(monkeypatch)
    repo_root = tmp_path / "repo"
    committed = repo_root / live._TEI_EXPORT_MANIFEST_RELPATH
    committed.parent.mkdir(parents=True)
    committed.write_text("{}", encoding="utf-8")
    _mock_repo_root(monkeypatch, repo_root)

    artifact_dir = tmp_path / "bundle"
    artifact_dir.mkdir()
    artifact = artifact_dir / "qwen3.tar.gz"
    artifact.write_bytes(b"x")
    # no sibling export-manifest.json next to the artifact

    live._stage_tei_cache(tmp_path / "workdir", artifact)

    cmd = _stage_cmd(calls)
    mi = cmd.index("--export-manifest")
    assert cmd[mi + 1] == str(committed)


def test_artifact_override_no_manifest_anywhere_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_subprocess(monkeypatch)
    _mock_repo_root(monkeypatch, tmp_path / "repo")  # committed manifest absent

    artifact_dir = tmp_path / "bundle"
    artifact_dir.mkdir()
    artifact = artifact_dir / "qwen3.tar.gz"
    artifact.write_bytes(b"x")

    with pytest.raises(click.ClickException, match="no export manifest"):
        live._stage_tei_cache(tmp_path / "workdir", artifact)
