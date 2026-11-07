# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TEI hub-cache pre-staging (NE-TEI-ONNX): layout, refs/main byte-exactness,
onnx injection + sha256 verification against the export manifest,
skip-safetensors, idempotent re-run, stage-tei-verify tamper detection, and
the CLI wiring. All fixture-based (injected callables + tmp_path + tiny
synthetic 'onnx' bytes) — NO real downloads/network anywhere.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import types
from pathlib import Path
from typing import Any

import pytest

from ne.serve.__main__ import main
from ne.serve.errors import WeightIntegrityError
from ne.serve.stage_tei import stage_tei, stage_tei_verify

_SHA = "a" * 40
_REPO_ID = "Qwen/Qwen3-Embedding-0.6B"
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
    """Callable snapshot_download fake that records calls and stages files
    into ``local_dir`` (mirrors ne-serve's existing download-tooling fixture)."""

    def __init__(self, *, content: dict[str, bytes] | None = None) -> None:
        self.content = content if content is not None else dict(_OFFICIAL_FILES)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        dest = Path(kwargs["local_dir"])
        for name, data in self.content.items():
            (dest / name).write_bytes(data)


def _make_onnx_artifact(
    tmp_path: Path,
    *,
    onnx_bytes: bytes = _ONNX_BYTES,
    data_bytes: bytes = _ONNX_DATA_BYTES,
    filename: str = "artifact.tar.gz",
) -> Path:
    """Build a synthetic .tar.gz shaped exactly like a model-export artifact
    (onnx/model.onnx + onnx/model.onnx_data + export-manifest.json), stdlib
    tarfile only — no dependency on tools/model-export."""
    path = tmp_path / filename
    with tarfile.open(path, "w:gz") as tar:
        for arcname, data in (
            ("onnx/model.onnx", onnx_bytes),
            ("onnx/model.onnx_data", data_bytes),
            ("export-manifest.json", b'{"model_key": "qwen3-embedding-0.6b"}'),
        ):
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _make_export_manifest(
    tmp_path: Path,
    *,
    source_revision: str = _SHA,
    onnx_bytes: bytes = _ONNX_BYTES,
    data_bytes: bytes = _ONNX_DATA_BYTES,
    filename: str = "export-manifest.json",
) -> Path:
    manifest = {
        "manifest_version": 1,
        "model_key": "qwen3-embedding-0.6b",
        "source_repo": _REPO_ID,
        "source_revision": source_revision,
        "files": [
            {
                "name": "model.onnx",
                "size": len(onnx_bytes),
                "sha256": hashlib.sha256(onnx_bytes).hexdigest(),
            },
            {
                "name": "model.onnx_data",
                "size": len(data_bytes),
                "sha256": hashlib.sha256(data_bytes).hexdigest(),
            },
        ],
    }
    path = tmp_path / filename
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _stage(tmp_path: Path, **kwargs: Any):
    snap = kwargs.pop("snapshot_download", None) or _FakeSnapshot()
    info = kwargs.pop("repo_info", None) or _fake_repo_info()
    result = stage_tei(
        _REPO_ID,
        tmp_path,
        snapshot_download=snap,
        repo_info=info,
        **kwargs,
    )
    return result, snap


def test_stage_tei_builds_hub_cache_layout(tmp_path: Path) -> None:
    result, snap = _stage(tmp_path)

    repo_dir = tmp_path / "models--Qwen--Qwen3-Embedding-0.6B"
    assert result.path == repo_dir
    assert result.resolved_revision == _SHA
    snapshot_dir = repo_dir / "snapshots" / _SHA
    for name in _OFFICIAL_FILES:
        assert (snapshot_dir / name).is_file()
    assert (repo_dir / "refs" / "main").is_file()
    assert result.manifest_path == repo_dir / "ne-stage-manifest.json"
    assert result.manifest_path.is_file()

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["repo_id"] == _REPO_ID
    assert manifest["resolved_revision"] == _SHA
    names = {f["name"] for f in manifest["files"]}
    assert names == {
        "refs/main",
        f"snapshots/{_SHA}/config.json",
        f"snapshots/{_SHA}/tokenizer.json",
        f"snapshots/{_SHA}/model.safetensors",
    }

    # snapshot_download was PINNED to the resolved sha (never the mutable ref).
    assert snap.calls[0]["revision"] == _SHA


def test_refs_main_is_bytes_exact_no_newline(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    raw = (result.path / "refs" / "main").read_bytes()
    assert raw == _SHA.encode("ascii")
    assert not raw.endswith(b"\n")
    assert not raw.endswith(b"\r")
    assert len(raw) == 40


def test_stage_tei_injects_onnx_from_artifact(tmp_path: Path) -> None:
    artifact = _make_onnx_artifact(tmp_path)
    export_manifest = _make_export_manifest(tmp_path)

    result, _ = _stage(tmp_path, onnx_artifact=artifact, export_manifest=export_manifest)

    onnx_dir = result.path / "snapshots" / _SHA / "onnx"
    assert (onnx_dir / "model.onnx").read_bytes() == _ONNX_BYTES
    assert (onnx_dir / "model.onnx_data").read_bytes() == _ONNX_DATA_BYTES

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    names = {f["name"] for f in manifest["files"]}
    assert f"snapshots/{_SHA}/onnx/model.onnx" in names
    assert f"snapshots/{_SHA}/onnx/model.onnx_data" in names
    by_name = {f["name"]: f for f in manifest["files"]}
    assert (
        by_name[f"snapshots/{_SHA}/onnx/model.onnx"]["sha256"]
        == hashlib.sha256(_ONNX_BYTES).hexdigest()
    )


def test_stage_tei_rejects_artifact_hash_mismatch(tmp_path: Path) -> None:
    artifact = _make_onnx_artifact(tmp_path, onnx_bytes=b"WRONG-BYTES-ENTIRELY")
    export_manifest = _make_export_manifest(tmp_path)  # expects the original _ONNX_BYTES hash

    with pytest.raises(WeightIntegrityError, match="sha256 verification"):
        _stage(tmp_path, onnx_artifact=artifact, export_manifest=export_manifest)

    # Nothing staged: hash verification happens BEFORE moving into the snapshot dir.
    snapshot_dir = tmp_path / "models--Qwen--Qwen3-Embedding-0.6B" / "snapshots" / _SHA
    assert not (snapshot_dir / "onnx").exists()


def test_stage_tei_requires_export_manifest_with_artifact(tmp_path: Path) -> None:
    artifact = _make_onnx_artifact(tmp_path)
    with pytest.raises(WeightIntegrityError, match="export_manifest is required"):
        _stage(tmp_path, onnx_artifact=artifact)


def test_stage_tei_rejects_revision_mismatch(tmp_path: Path) -> None:
    artifact = _make_onnx_artifact(tmp_path)
    # export manifest pinned to a DIFFERENT resolved revision than what repo_info resolves.
    export_manifest = _make_export_manifest(tmp_path, source_revision="b" * 40)

    with pytest.raises(WeightIntegrityError, match="source_revision"):
        _stage(tmp_path, onnx_artifact=artifact, export_manifest=export_manifest)


def test_stage_tei_skip_safetensors(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path, skip_safetensors=True)
    snapshot_dir = result.path / "snapshots" / _SHA
    assert not (snapshot_dir / "model.safetensors").exists()
    assert (snapshot_dir / "config.json").is_file()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    names = {f["name"] for f in manifest["files"]}
    assert f"snapshots/{_SHA}/model.safetensors" not in names


def test_stage_tei_idempotent_skip(tmp_path: Path) -> None:
    result1, snap1 = _stage(tmp_path)
    assert len(snap1.calls) == 1

    snap2 = _FakeSnapshot()
    result2, _ = _stage(tmp_path, snapshot_download=snap2)

    assert snap2.calls == []  # download callable NOT re-invoked
    assert result2.path == result1.path
    assert result2.resolved_revision == result1.resolved_revision


def test_stage_tei_idempotent_skip_re_stages_when_onnx_newly_requested(tmp_path: Path) -> None:
    # Plain snapshot first (no onnx requested) — idempotent skip must not
    # mask a LATER onnx request against the same revision.
    _stage(tmp_path)

    artifact = _make_onnx_artifact(tmp_path)
    export_manifest = _make_export_manifest(tmp_path)
    snap2 = _FakeSnapshot()
    result, _ = _stage(
        tmp_path,
        snapshot_download=snap2,
        onnx_artifact=artifact,
        export_manifest=export_manifest,
    )
    assert (result.path / "snapshots" / _SHA / "onnx" / "model.onnx").is_file()


def test_stage_tei_verify_passes_on_pristine(tmp_path: Path) -> None:
    artifact = _make_onnx_artifact(tmp_path)
    export_manifest = _make_export_manifest(tmp_path)
    _stage(tmp_path, onnx_artifact=artifact, export_manifest=export_manifest)
    results = stage_tei_verify(tmp_path)
    assert "models--Qwen--Qwen3-Embedding-0.6B" in results


def test_stage_tei_verify_detects_tamper(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    (result.path / "snapshots" / _SHA / "config.json").write_bytes(b"TAMPERED")
    with pytest.raises(WeightIntegrityError, match="sha256 mismatch"):
        stage_tei_verify(tmp_path)


def test_stage_tei_verify_detects_missing(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    (result.path / "snapshots" / _SHA / "config.json").unlink()
    with pytest.raises(WeightIntegrityError, match="missing file"):
        stage_tei_verify(tmp_path)


def test_stage_tei_verify_detects_extra_file(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    (result.path / "snapshots" / _SHA / "evil.bin").write_bytes(b"exfil")
    with pytest.raises(WeightIntegrityError, match="unexpected file"):
        stage_tei_verify(tmp_path)


def test_stage_tei_verify_detects_refs_trailing_newline(tmp_path: Path) -> None:
    result, _ = _stage(tmp_path)
    refs_path = result.path / "refs" / "main"
    refs_path.write_bytes(_SHA.encode("ascii") + b"\n")
    with pytest.raises(WeightIntegrityError, match="refs/main"):
        stage_tei_verify(tmp_path)


def test_stage_tei_verify_fails_without_any_staged_repo(tmp_path: Path) -> None:
    with pytest.raises(WeightIntegrityError, match="no staged"):
        stage_tei_verify(tmp_path)


def test_cli_stage_tei_wiring(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The CLI drives the REAL huggingface_hub import path only when
    # snapshot_download/repo_info aren't injectable at the CLI layer — so
    # this exercises argument parsing + dispatch (unresolvable revision path,
    # a fast, network-free failure mode) rather than a full live stage.
    with pytest.raises(SystemExit) as excinfo:
        main(["stage-tei", "--help"])
    assert excinfo.value.code == 0

    with pytest.raises(SystemExit) as excinfo:
        main(["stage-tei-verify", "--help"])
    assert excinfo.value.code == 0

    # --onnx-artifact without --export-manifest is rejected before any network call.
    rc = main(
        [
            "stage-tei",
            _REPO_ID,
            "--dest",
            str(tmp_path),
            "--onnx-artifact",
            str(tmp_path / "nonexistent.tar.gz"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "--export-manifest" in err


def test_cli_stage_tei_verify_reports_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["stage-tei-verify", "--dest", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
