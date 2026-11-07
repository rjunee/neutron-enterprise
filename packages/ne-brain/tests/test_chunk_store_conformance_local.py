# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ChunkStoreContractKit against qdrant-client local (in-process) mode.

Always runs in CI — this is vendor in-process ("local mode") Qdrant, not a
real server. Supplementary to the real-server run in
test_chunk_store_conformance_server.py, which is the actual conformance
claim against Qdrant v1.18.2.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.qdrant import QdrantChunkStore
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.testing.kits.chunk_store import ChunkStoreContractKit
from ne.contracts.knowledge import ChunkStore
from ne.contracts.types import SparseVector


class TestQdrantChunkStoreLocalMode(ChunkStoreContractKit):
    @pytest.fixture(autouse=True)
    def _inject_bm25(self, bm25_encoder: FastEmbedBM25Encoder) -> None:
        self._session_bm25 = bm25_encoder

    def create_store(self) -> ChunkStore:
        client = AsyncQdrantClient(":memory:")
        embedder = HashEmbedder(32)
        collection = f"kit_{uuid.uuid4().hex}"
        store = QdrantChunkStore(client, collection, embedder, self._session_bm25)
        asyncio.run(store.ensure_collection())
        self._embedder = embedder
        return store

    def embed_text(self, text: str) -> Sequence[float]:
        return asyncio.run(self._embedder.embed([text]))[0]

    def encode_query(self, text: str) -> SparseVector:
        return asyncio.run(self._session_bm25.encode_queries([text]))[0]
