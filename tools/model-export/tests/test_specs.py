# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pin the model-export registry: exact source revision, dim, threshold, and
every committed path — these are C11 pins; a silent drift here would change
what gets exported/served without review."""

from __future__ import annotations

from pathlib import Path

from ne.model_export.specs import MODEL_SPECS, QWEN3_EMBEDDING_0_6B


def test_registry_has_exactly_the_one_pinned_spec() -> None:
    assert set(MODEL_SPECS) == {"qwen3-embedding-0.6b"}
    assert MODEL_SPECS["qwen3-embedding-0.6b"] is QWEN3_EMBEDDING_0_6B


def test_qwen3_embedding_pins() -> None:
    spec = QWEN3_EMBEDDING_0_6B
    assert spec.key == "qwen3-embedding-0.6b"
    assert spec.repo_id == "Qwen/Qwen3-Embedding-0.6B"
    assert spec.revision == "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    assert len(spec.revision) == 40
    assert all(c in "0123456789abcdef" for c in spec.revision)
    assert spec.dim == 1024
    assert spec.threshold == 0.9999


def test_qwen3_embedding_artifact_pins() -> None:
    spec = QWEN3_EMBEDDING_0_6B
    assert spec.artifact_filename == "qwen3-embedding-0.6b-onnx-fp32-97b0c61.tar.gz"
    assert spec.artifact_filename.endswith(".tar.gz")
    assert not spec.artifact_filename.endswith(".tar.zst")


def test_modelspec_fields_have_no_hosted_release_channel() -> None:
    # NE-TEI-GEN: the GitHub-release channel was vetoed; the pin is the committed
    # manifest, not a hosted asset — the ModelSpec fields are exactly these, with
    # no hosted-asset tag among them.
    assert set(QWEN3_EMBEDDING_0_6B.__dataclass_fields__) == {
        "key",
        "repo_id",
        "revision",
        "dim",
        "threshold",
        "artifact_filename",
        "manifest_path",
        "golden_vectors_path",
        "golden_texts_path",
    }


def test_qwen3_embedding_committed_paths() -> None:
    spec = QWEN3_EMBEDDING_0_6B
    assert spec.manifest_path == Path(
        "tools/model-export/manifests/qwen3-embedding-0.6b-onnx-fp32.json"
    )
    assert spec.golden_vectors_path == Path("tools/model-export/golden/qwen3-embedding-0.6b.json")
    assert spec.golden_texts_path == Path(
        "tools/model-export/golden/qwen3-embedding-0.6b.texts.json"
    )
