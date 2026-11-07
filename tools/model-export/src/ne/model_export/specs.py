# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The model-export registry: which models this tool knows how to export,
pinned to an exact source revision + parity threshold + committed artifact
paths. One entry today (NE-TEI-ONNX); a future model earns its own entry, not
a parameter override of this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class ModelSpec:
    """A pinned model-export target: source, parity threshold, and every path
    this tool reads/writes (all repo-root-relative — callers resolve against
    the discovered repo root, never against ``Path.cwd()``)."""

    key: str
    repo_id: str
    revision: str
    dim: int
    threshold: float
    artifact_filename: str
    manifest_path: Path
    golden_vectors_path: Path
    golden_texts_path: Path


QWEN3_EMBEDDING_0_6B: Final[ModelSpec] = ModelSpec(
    key="qwen3-embedding-0.6b",
    repo_id="Qwen/Qwen3-Embedding-0.6B",
    revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
    dim=1024,
    threshold=0.9999,
    artifact_filename="qwen3-embedding-0.6b-onnx-fp32-97b0c61.tar.gz",
    manifest_path=Path("tools/model-export/manifests/qwen3-embedding-0.6b-onnx-fp32.json"),
    golden_vectors_path=Path("tools/model-export/golden/qwen3-embedding-0.6b.json"),
    golden_texts_path=Path("tools/model-export/golden/qwen3-embedding-0.6b.texts.json"),
)

MODEL_SPECS: Final[dict[str, ModelSpec]] = {
    QWEN3_EMBEDDING_0_6B.key: QWEN3_EMBEDDING_0_6B,
}

__all__ = ["MODEL_SPECS", "QWEN3_EMBEDDING_0_6B", "ModelSpec"]
