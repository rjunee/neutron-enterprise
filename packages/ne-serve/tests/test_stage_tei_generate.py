# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-TEI-GEN generate-on-demand staging: the `generate=True` auto mode, the
explicit-artifact override precedence, the AIR_GAP fail-closed surfacing as a
typed WeightIntegrityError (before any snapshot_download), and the
`ensure_generated_onnx` subprocess-seam contract. All fixture-based (injected
seams/runner + tiny synthetic onnx bytes) — NO real subprocess/network."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import types
from pathlib import Path
from typing import Any

import pytest

from ne.serve import __main__ as serve_main
from ne.serve.errors import WeightIntegrityError
from ne.serve.stage_tei import (
    GENERATABLE_REPOS,
    StageResult,
    ensure_generated_onnx,
    stage_tei,
    stage_tei_verify,
)

_SHA = "a" * 40
_REPO_ID = "Qwen/Qwen3-Embedding-0.6B"
_MODEL_KEY = "qwen3-embedding-0.6b"
_OFFICIAL_FILES = {
    "config.json": b"{}",
    "tokenizer.json": b'{"tok": true}',
    "model.safetensors": b"safetensors-bytes-here",
}
_ONNX_BYTES = b"fake-onnx-graph-bytes"
_ONNX_DATA_BYTES = b"fake-external-data-bytes" * 4


def _fake_repo_info(*, siblings: list[str] | None = None, sha: str | None = _SHA):
    names = siblings if siblings is not None else list(_OFFICIAL_FILES)

    def repo_info(**_kwargs: Any) -> Any:
        return types.SimpleNamespace(
            sha=sha,
            siblings=[types.SimpleNamespace(rfilename=n) for n in names],
        )

    return repo_info


class _FakeSnapshot:
    def __init__(self, *, content: dict[str, bytes] | None = None) -> None:
        self.content = content if content is not None else dict(_OFFICIAL_FILES)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        dest = Path(kwargs["local_dir"])
        for name, data in self.content.items():
            (dest / name).write_bytes(data)


def _make_onnx_artifact(tmp_path: Path, *, filename: str = "artifact.tar.gz") -> Path:
    path = tmp_path / filename
    with tarfile.open(path, "w:gz") as tar:
        for arcname, data in (
            ("onnx/model.onnx", _ONNX_BYTES),
            ("onnx/model.onnx_data", _ONNX_DATA_BYTES),
            ("export-manifest.json", b'{"model_key": "qwen3-embedding-0.6b"}'),
        ):
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _make_export_manifest(tmp_path: Path, *, filename: str = "export-manifest.json") -> Path:
    manifest = {
        "manifest_version": 1,
        "model_key": _MODEL_KEY,
        "source_repo": _REPO_ID,
        "source_revision": _SHA,
        "files": [
            {
                "name": "model.onnx",
                "size": len(_ONNX_BYTES),
                "sha256": hashlib.sha256(_ONNX_BYTES).hexdigest(),
            },
            {
                "name": "model.onnx_data",
                "size": len(_ONNX_DATA_BYTES),
                "sha256": hashlib.sha256(_ONNX_DATA_BYTES).hexdigest(),
            },
        ],
    }
    path = tmp_path / filename
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _completed(cmd: list[str], *, returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# (a) generate=True + ensure_onnx seam -> generate + stage + verify
# --------------------------------------------------------------------------- #
def test_generate_true_seam_stages_and_verifies(tmp_path: Path) -> None:
    dest = tmp_path / "tei-cache"
    artifact = _make_onnx_artifact(tmp_path)
    manifest = _make_export_manifest(tmp_path)
    seam_calls: list[str] = []

    def _ensure(repo_id: str) -> tuple[Path, Path]:
        seam_calls.append(repo_id)
        return artifact, manifest

    snap = _FakeSnapshot()
    result = stage_tei(
        _REPO_ID,
        dest,
        generate=True,
        ensure_onnx=_ensure,
        snapshot_download=snap,
        repo_info=_fake_repo_info(),
    )
    assert seam_calls == [_REPO_ID]  # seam invoked exactly once
    onnx_dir = result.path / "snapshots" / _SHA / "onnx"
    assert (onnx_dir / "model.onnx").read_bytes() == _ONNX_BYTES
    assert (onnx_dir / "model.onnx_data").read_bytes() == _ONNX_DATA_BYTES
    # the staged tree re-verifies clean at the air-gap boundary
    assert "models--Qwen--Qwen3-Embedding-0.6B" in stage_tei_verify(dest)


# --------------------------------------------------------------------------- #
# idempotent restage: an already-staged tree short-circuits BEFORE the resolver
# (a sealed AIR_GAP redeploy must not fail-closed on a cache miss when nothing
# needs (re)generating)
# --------------------------------------------------------------------------- #
def test_generate_idempotent_restage_skips_resolver(tmp_path: Path) -> None:
    dest = tmp_path / "tei-cache"
    artifact = _make_onnx_artifact(tmp_path)
    manifest = _make_export_manifest(tmp_path)

    # First: stage from an explicit bundle (simulating an air-gap bundle stage).
    stage_tei(
        _REPO_ID,
        dest,
        onnx_artifact=artifact,
        export_manifest=manifest,
        snapshot_download=_FakeSnapshot(),
        repo_info=_fake_repo_info(),
    )

    # Rerun with the DEFAULT generate=True auto mode: the tree is already valid
    # with its onnx pair, so the generation resolver must NOT run (this is what
    # lets a sealed AIR_GAP redeploy restage idempotently — the resolver would
    # otherwise fail-closed on a cache miss).
    def _ensure(repo_id: str) -> tuple[Path, Path]:
        raise AssertionError("resolver must not run when the tree is already staged")

    snap = _FakeSnapshot()
    result = stage_tei(
        _REPO_ID,
        dest,
        generate=True,
        ensure_onnx=_ensure,
        snapshot_download=snap,
        repo_info=_fake_repo_info(),
    )
    assert result.resolved_revision == _SHA
    assert (result.path / "snapshots" / _SHA / "onnx" / "model.onnx").is_file()
    assert snap.calls == []  # no re-download either


# --------------------------------------------------------------------------- #
# (c) explicit onnx_artifact override wins — seam NOT called
# --------------------------------------------------------------------------- #
def test_explicit_artifact_override_beats_generate(tmp_path: Path) -> None:
    dest = tmp_path / "tei-cache"
    artifact = _make_onnx_artifact(tmp_path)
    manifest = _make_export_manifest(tmp_path)

    def _ensure(repo_id: str) -> tuple[Path, Path]:
        raise AssertionError("ensure_onnx seam must NOT be called when --onnx-artifact is given")

    result = stage_tei(
        _REPO_ID,
        dest,
        generate=True,
        onnx_artifact=artifact,
        export_manifest=manifest,
        ensure_onnx=_ensure,
        snapshot_download=_FakeSnapshot(),
        repo_info=_fake_repo_info(),
    )
    assert (result.path / "snapshots" / _SHA / "onnx" / "model.onnx").read_bytes() == _ONNX_BYTES


# --------------------------------------------------------------------------- #
# (d) AIR_GAP subprocess failure -> typed error, no download
# --------------------------------------------------------------------------- #
def test_air_gap_generate_failure_refuses_before_download(tmp_path: Path) -> None:
    dest = tmp_path / "tei-cache"
    snap = _FakeSnapshot()

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return _completed(
            cmd,
            returncode=1,
            stderr="error: AIR_GAP=1 and no cached ONNX — supply the bundle artifact",
        )

    def _ensure(repo_id: str) -> tuple[Path, Path]:
        return ensure_generated_onnx(repo_id, runner=_runner)

    with pytest.raises(WeightIntegrityError, match="AIR_GAP"):
        stage_tei(
            _REPO_ID,
            dest,
            generate=True,
            ensure_onnx=_ensure,
            snapshot_download=snap,
            repo_info=_fake_repo_info(),
        )
    assert snap.calls == []  # staging refused before ANY snapshot download


# --------------------------------------------------------------------------- #
# ensure_generated_onnx contract: cmd shape + last-line JSON parse
# --------------------------------------------------------------------------- #
def test_ensure_generated_onnx_parses_last_line_json(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        stdout = (
            "probing cache...\n"
            "generating...\n"
            + json.dumps({"artifact": "/c/art.tar.gz", "manifest": "/c/export-manifest.json"})
            + "\n"
        )
        return _completed(cmd, returncode=0, stdout=stdout)

    artifact, manifest = ensure_generated_onnx(_REPO_ID, runner=_runner)
    assert artifact == Path("/c/art.tar.gz")
    assert manifest == Path("/c/export-manifest.json")
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "ne.model_export.cli",
        "ensure-cached",
        _MODEL_KEY,
    ]


def test_ensure_generated_onnx_unknown_repo_raises() -> None:
    with pytest.raises(WeightIntegrityError, match="not a generatable repo"):
        ensure_generated_onnx("some/unknown-repo", runner=lambda cmd: _completed(cmd, returncode=0))


def test_ensure_generated_onnx_nonzero_exit_carries_stderr_tail() -> None:
    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return _completed(cmd, returncode=1, stderr="boom: something failed")

    with pytest.raises(WeightIntegrityError, match="boom: something failed"):
        ensure_generated_onnx(_REPO_ID, runner=_runner)


# --------------------------------------------------------------------------- #
# generate=True + unknown repo -> snapshot-only, seam not consulted
# --------------------------------------------------------------------------- #
def test_generate_true_unknown_repo_stages_snapshot_only(tmp_path: Path) -> None:
    unknown = "Some/Other-Model"
    dest = tmp_path / "tei-cache"
    snap = _FakeSnapshot()
    result = stage_tei(
        unknown,
        dest,
        generate=True,
        snapshot_download=snap,
        repo_info=_fake_repo_info(),
    )
    # snapshot staged, no onnx dir (generation never attempted for an unknown repo)
    assert snap.calls  # official snapshot WAS downloaded
    assert not (result.path / "snapshots" / _SHA / "onnx").exists()


def test_generatable_repos_mirrors_registry() -> None:
    assert GENERATABLE_REPOS == {"Qwen/Qwen3-Embedding-0.6B": "qwen3-embedding-0.6b"}


# --------------------------------------------------------------------------- #
# CLI wiring: --no-generate -> generate=False; default -> generate=True
# --------------------------------------------------------------------------- #
def _capture_stage_tei(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake_stage_tei(repo_id: str, dest: Any, **kwargs: Any) -> StageResult:
        captured["repo_id"] = repo_id
        captured["kwargs"] = kwargs
        return StageResult(
            path=Path(dest) / "models--x",
            manifest_path=Path(dest) / "m.json",
            resolved_revision=_SHA,
        )

    monkeypatch.setattr(serve_main, "stage_tei", _fake_stage_tei)
    return captured


def test_cli_default_passes_generate_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = _capture_stage_tei(monkeypatch)
    rc = serve_main.main(["stage-tei", _REPO_ID, "--dest", str(tmp_path)])
    assert rc == 0
    assert captured["kwargs"]["generate"] is True


def test_cli_no_generate_passes_generate_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = _capture_stage_tei(monkeypatch)
    rc = serve_main.main(["stage-tei", _REPO_ID, "--dest", str(tmp_path), "--no-generate"])
    assert rc == 0
    assert captured["kwargs"]["generate"] is False
