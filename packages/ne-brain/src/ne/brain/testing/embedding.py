# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, model-free embedding test doubles.

* :class:`HashEmbedder` — a DENSE embedder used by the ChunkStore/KnowledgeRead
  conformance kits so their dense-relevance assertions (querying with a chunk's
  exact text must rank that chunk first under cosine similarity) do not depend
  on any real ML model: identical text always produces an identical (and
  near-orthogonal-for-distinct-text) unit vector, deterministically, with no
  network or GPU/CPU model load.
* :class:`HashSparseEncoder` — a SPARSE encoder counterpart, so the NE-026 spine
  embed+index CI tests run fully OFFLINE (no fastembed HF asset download, no
  live-parked skip). It structurally satisfies BOTH
  ``ne.contracts.knowledge.SparseEncoder`` and ``ne.brain.retriever.QuerySparseEncoder``.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from ne.contracts.types import SparseVector

_MODEL_ID = "ne-test-hash-v1"
_SPARSE_MODEL_ID = "ne-test-hash-sparse-v1"


def _hash_vector(text: str, dimension: int) -> list[float]:
    values: list[float] = []
    block = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{text}:{block}".encode()).digest()
        values.extend((b - 127.5) / 127.5 for b in digest)
        block += 1
    values = values[:dimension]
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return values
    return [v / norm for v in values]


class HashEmbedder:
    """Deterministic ``Embedder`` for kit/CI use — no real model required."""

    def __init__(self, dimension: int = 32) -> None:
        self._dimension = dimension

    @property
    def model_id(self) -> str:
        return _MODEL_ID

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [_hash_vector(text, self._dimension) for text in texts]


def _token_index(token: str) -> int:
    return int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")


def _sparse_vector(text: str) -> SparseVector:
    counts: dict[int, float] = {}
    for token in text.lower().split():
        counts[_token_index(token)] = counts.get(_token_index(token), 0.0) + 1.0
    ordered = sorted(counts.items())
    return SparseVector(
        indices=tuple(index for index, _ in ordered),
        values=tuple(value for _, value in ordered),
    )


class HashSparseEncoder:
    """Deterministic sparse encoder for CI — no fastembed/HF assets required.

    Tokenises on whitespace (lower-cased), maps each token to a stable 32-bit
    index via SHA-256, and weights it by term count. Structurally satisfies both
    ``ne.contracts.knowledge.SparseEncoder`` (``model_id`` + ``encode``) and
    ``ne.brain.retriever.QuerySparseEncoder`` (also ``encode_queries``).
    """

    @property
    def model_id(self) -> str:
        return _SPARSE_MODEL_ID

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [_sparse_vector(text) for text in texts]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [_sparse_vector(text) for text in texts]
