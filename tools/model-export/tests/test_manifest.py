# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Export-manifest build/round-trip/tamper tests over a tiny synthetic onnx
dir — no real model/network anywhere in this module."""

from __future__ import annotations

from pathlib import Path

import pytest

from ne.model_export import manifest as m


def _write_onnx_pair(tmp_path: Path) -> list[Path]:
    onnx_dir = tmp_path / "onnx"
    onnx_dir.mkdir()
    model = onnx_dir / "model.onnx"
    data = onnx_dir / "model.onnx_data"
    model.write_bytes(b"fake-onnx-graph-bytes")
    data.write_bytes(b"fake-external-data-bytes" * 10)
    return [model, data]


def test_build_manifest_from_synthetic_onnx_dir(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    manifest = m.build_manifest(
        model_key="qwen3-embedding-0.6b",
        source_repo="Qwen/Qwen3-Embedding-0.6B",
        source_revision="a" * 40,
        dtype="fp32",
        toolchain={"torch": "2.13.0", "onnxruntime": "1.27.0"},
        files=files,
        golden_count=16,
        min_cosine=0.99995,
        threshold=0.9999,
    )
    assert manifest["manifest_version"] == m.MANIFEST_VERSION
    assert manifest["model_key"] == "qwen3-embedding-0.6b"
    assert manifest["source_revision"] == "a" * 40
    assert manifest["dtype"] == "fp32"
    assert manifest["toolchain"] == {"torch": "2.13.0", "onnxruntime": "1.27.0"}
    names = {f["name"] for f in manifest["files"]}
    assert names == {"model.onnx", "model.onnx_data"}
    by_name = {f["name"]: f for f in manifest["files"]}
    assert by_name["model.onnx"]["size"] == len(b"fake-onnx-graph-bytes")
    assert by_name["model.onnx"]["sha256"] == m.sha256_file(files[0])
    assert manifest["parity"] == {"golden_count": 16, "min_cosine": 0.99995, "threshold": 0.9999}
    assert "created_at" in manifest
    assert "artifact" not in manifest  # core manifest has no artifact section yet


def test_with_artifact_adds_artifact_section_without_mutating_input(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    core = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="b" * 40,
        dtype="fp32",
        toolchain={},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    artifact_path = tmp_path / "artifact.tar.gz"
    artifact_path.write_bytes(b"tarball-bytes")

    full = m.with_artifact(core, filename="artifact.tar.gz", path=artifact_path)
    assert "artifact" not in core  # original untouched
    # NE-TEI-GEN: the artifact section is exactly {filename, sha256, size} — no
    # hosted-asset tag (the hosted-release channel was vetoed).
    assert full["artifact"] == {
        "filename": "artifact.tar.gz",
        "sha256": m.sha256_file(artifact_path),
        "size": len(b"tarball-bytes"),
    }


def test_save_and_load_manifest_round_trip(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    manifest = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="c" * 40,
        dtype="fp32",
        toolchain={"torch": "2.13.0"},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    path = tmp_path / "manifest.json"
    m.save_manifest(manifest, path)
    loaded = m.load_manifest(path)
    assert loaded == manifest


def test_load_manifest_missing_file_raises() -> None:
    with pytest.raises(m.ManifestIntegrityError):
        m.load_manifest(Path("/nonexistent/manifest.json"))


def test_load_manifest_unparseable_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(m.ManifestIntegrityError):
        m.load_manifest(path)


def test_verify_files_against_manifest_accepts_clean_staging(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    manifest = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="d" * 40,
        dtype="fp32",
        toolchain={},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    m.verify_files_against_manifest(manifest, tmp_path / "onnx")


def test_verify_files_against_manifest_rejects_tamper(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    manifest = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="e" * 40,
        dtype="fp32",
        toolchain={},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    files[0].write_bytes(b"TAMPERED")
    with pytest.raises(m.ManifestIntegrityError, match="sha256 mismatch"):
        m.verify_files_against_manifest(manifest, tmp_path / "onnx")


def test_verify_files_against_manifest_rejects_missing_file(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    manifest = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="f" * 40,
        dtype="fp32",
        toolchain={},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    files[1].unlink()
    with pytest.raises(m.ManifestIntegrityError, match="missing file"):
        m.verify_files_against_manifest(manifest, tmp_path / "onnx")


def test_verify_files_against_manifest_rejects_size_mismatch(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    manifest = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="1" * 40,
        dtype="fp32",
        toolchain={},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    files[0].write_bytes(b"short")
    with pytest.raises(m.ManifestIntegrityError):
        m.verify_files_against_manifest(manifest, tmp_path / "onnx")


def test_verify_artifact_accepts_clean_and_rejects_tampered(tmp_path: Path) -> None:
    files = _write_onnx_pair(tmp_path)
    core = m.build_manifest(
        model_key="k",
        source_repo="r",
        source_revision="2" * 40,
        dtype="fp32",
        toolchain={},
        files=files,
        golden_count=1,
        min_cosine=1.0,
        threshold=0.9999,
    )
    artifact_path = tmp_path / "artifact.tar.gz"
    artifact_path.write_bytes(b"tarball-bytes")
    full = m.with_artifact(core, filename="artifact.tar.gz", path=artifact_path)
    m.verify_artifact(full, artifact_path)

    artifact_path.write_bytes(b"TAMPERED-TARBALL")
    with pytest.raises(m.ManifestIntegrityError):
        m.verify_artifact(full, artifact_path)


def test_verify_artifact_requires_artifact_section() -> None:
    with pytest.raises(m.ManifestIntegrityError, match="no 'artifact' section"):
        m.verify_artifact({"files": []}, Path("/nonexistent"))
