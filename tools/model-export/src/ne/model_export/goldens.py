# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Golden-vector file schema + pure-stdlib cosine — the vector-parity truth
gate's data model (design §5). Deliberately dependency-free (no numpy): both
the build-time gate's platform-side orchestrator (this dep-less package) and
the serving-time B1 smoke (httpx + stdlib only, ``deploy/airgap/tests``) need
cosine math without a heavy numeric dependency.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


class GoldenValidationError(Exception):
    """A golden texts/vectors file failed its schema or content invariants."""


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors (pure stdlib)."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("cannot compute cosine similarity of a zero-norm vector")
    return dot / (norm_a * norm_b)


def load_golden_texts(path: Path) -> list[str]:
    """Load + validate the committed golden TEXTS file: a JSON list (or
    ``{"texts": [...]}``) of unique, non-empty strings."""
    if not path.is_file():
        raise GoldenValidationError(f"no golden texts file at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    texts = data["texts"] if isinstance(data, dict) else data
    if not isinstance(texts, list) or not texts:
        raise GoldenValidationError(f"{path} must contain a non-empty list of texts")
    texts = [str(t) for t in texts]
    if any(not t.strip() for t in texts):
        raise GoldenValidationError(f"{path} contains an empty/whitespace-only text")
    if len(texts) != len(set(texts)):
        raise GoldenValidationError(f"{path} contains duplicate texts")
    return texts


def build_golden_vectors(
    *,
    texts: list[str],
    vectors: list[list[float]],
    dim: int,
    source_revision: str,
) -> dict[str, Any]:
    """Build the golden VECTORS payload (texts + reference embeddings + dim +
    the source revision they were embedded against)."""
    if len(texts) != len(vectors):
        raise GoldenValidationError(
            f"texts/vectors length mismatch: {len(texts)} texts, {len(vectors)} vectors"
        )
    for i, vec in enumerate(vectors):
        if len(vec) != dim:
            raise GoldenValidationError(f"vector {i} has dim {len(vec)}, expected {dim}")
    return {
        "texts": list(texts),
        "vectors": [list(v) for v in vectors],
        "dim": dim,
        "source_revision": source_revision,
    }


def save_golden_vectors(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_golden_vectors(path: Path) -> dict[str, Any]:
    """Load + validate the committed golden VECTORS file."""
    if not path.is_file():
        raise GoldenValidationError(f"no golden vectors file at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"texts", "vectors", "dim", "source_revision"}
    missing = required - set(data)
    if missing:
        raise GoldenValidationError(f"{path} missing required keys: {sorted(missing)}")
    if len(data["texts"]) != len(data["vectors"]):
        raise GoldenValidationError(f"{path}: texts/vectors length mismatch")
    dim = data["dim"]
    for i, vec in enumerate(data["vectors"]):
        if len(vec) != dim:
            raise GoldenValidationError(f"{path}: vector {i} has dim {len(vec)}, expected {dim}")
    return data


__all__ = [
    "GoldenValidationError",
    "build_golden_vectors",
    "cosine",
    "load_golden_texts",
    "load_golden_vectors",
    "save_golden_vectors",
]
