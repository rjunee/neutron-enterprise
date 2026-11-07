# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""FastEmbed BM25 sparse encoder (Apache-2.0).

Implements :class:`ne.contracts.knowledge.SparseEncoder`. BM25 term weights are
produced **client-side** by FastEmbed at both ingest (:meth:`encode`, document
mode) and query time (:meth:`encode_queries`, query mode). Query-mode vectors
carry a flat weight of ``1`` per token; the actual BM25 IDF term is applied
**server-side** by Qdrant via ``Modifier.IDF`` on the collection's sparse
vector params (see :mod:`ne.brain.qdrant`), matching the documented Qdrant/
FastEmbed hybrid-search recipe.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Sequence
from typing import Any

from ne.brain.sovereignty import AirGapViolationError, air_gap_engaged
from ne.contracts.types import SparseVector

_DEFAULT_MODEL = "Qdrant/bm25"


def _to_sparse_vector(embedding: Any) -> SparseVector:
    return SparseVector(
        indices=tuple(int(i) for i in embedding.indices),
        values=tuple(float(v) for v in embedding.values),
    )


class FastEmbedBM25Encoder:
    """BM25 sparse encoder backed by ``fastembed.SparseTextEmbedding``.

    The underlying model is loaded lazily (and cached) on first use, off the
    event loop via ``asyncio.to_thread``, since model construction touches disk
    (and, on a cold cache, the network) and is not itself async.

    C1/C2 (no phone-home / AIR_GAP): ``HF_HUB_DISABLE_TELEMETRY=1`` is set
    unconditionally at the load seam; under AIR_GAP, ``HF_HUB_OFFLINE=1`` is
    FORCED into the environment BEFORE the ``fastembed`` import (which pulls in
    ``huggingface_hub``, which snapshots those flags into module constants at
    import time) so ``huggingface_hub`` resolves the model from the local cache
    ONLY and a cold/incomplete cache raises with NO network attempt — that raise
    is wrapped as
    :class:`~ne.brain.sovereignty.AirGapViolationError` (the sanctioned path is a
    pre-staged ``FASTEMBED_CACHE_PATH``). Residual risk (documented for NE-073's
    no-egress CI proof): a fastembed GCS fallback over a partially-warm cache
    could in principle bypass ``HF_HUB_OFFLINE``; the primary gate here is the
    forced-offline env plus the sanctioned pre-staged cache.
    """

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model: Any | None = None
        # A THREADING lock, deliberately not asyncio.Lock: this encoder is
        # shared by ne-worker's DBOS turn loop (query encode) and its persistent
        # cron-tick loop (connector-sync ingest encode), and an asyncio.Lock
        # binds to whichever loop first awaits it — a first-load race across the
        # two loops then raises "is bound to a different event loop" (the
        # 2026-08-28 cron-offload incident class). The blocking acquire runs
        # inside ``asyncio.to_thread``, never on an event loop.
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self._model_name

    def _load_model_locked(self) -> Any:
        """Blocking, loop-agnostic double-checked model load (worker thread only)."""
        with self._lock:
            if self._model is None:
                # C1: telemetry off unconditionally. C2 AIR_GAP: force offline
                # BEFORE construction (forced, not setdefault — AIR_GAP only ADDS
                # restriction) so the loader resolves from local cache only and a
                # cold cache raises with no network attempt. These writes MUST
                # precede the `fastembed` import: importing fastembed imports
                # huggingface_hub, which snapshots HF_HUB_OFFLINE /
                # HF_HUB_DISABLE_TELEMETRY into module-level constants at import
                # time — setting them afterwards would be read too late in a
                # fresh process with a cold cache.
                os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
                if air_gap_engaged():
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

                from fastembed import SparseTextEmbedding

                try:
                    self._model = SparseTextEmbedding(self._model_name)
                except Exception as exc:
                    if air_gap_engaged():
                        raise AirGapViolationError(
                            f"BM25 model {self._model_name!r} could not be loaded offline "
                            "under AIR_GAP (cold or incomplete fastembed cache). Pre-stage "
                            "the model cache (FASTEMBED_CACHE_PATH) — runtime HuggingFace "
                            "downloads are forbidden in an air-gapped deployment."
                        ) from exc
                    raise
            return self._model

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        return await asyncio.to_thread(self._load_model_locked)

    async def encode(self, texts: Sequence[str]) -> tuple[SparseVector, ...]:
        """Document-mode BM25 encoding for ingest: term-frequency-weighted vectors."""
        model = await self._get_model()
        embeddings = await asyncio.to_thread(lambda: list(model.embed(list(texts))))
        return tuple(_to_sparse_vector(e) for e in embeddings)

    async def encode_queries(self, texts: Sequence[str]) -> tuple[SparseVector, ...]:
        """Query-mode BM25 encoding: weight-1 per token; IDF applied server-side."""
        model = await self._get_model()
        embeddings = await asyncio.to_thread(lambda: list(model.query_embed(list(texts))))
        return tuple(_to_sparse_vector(e) for e in embeddings)
