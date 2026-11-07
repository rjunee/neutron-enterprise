# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeReadContractKit over HybridRetriever, qdrant-client local mode.

Always runs in CI (vendor in-process Qdrant). Supplementary to the real-server
run in test_knowledge_read_conformance_server.py.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.testing.kits.knowledge_read import KnowledgeReadContractKit
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Chunk


class TestHybridRetrieverLocalMode(KnowledgeReadContractKit):
    @pytest.fixture(autouse=True)
    def _inject_bm25(self, bm25_encoder: FastEmbedBM25Encoder) -> None:
        self._session_bm25 = bm25_encoder

    def create_reader(self, chunks: Sequence[Chunk]) -> KnowledgeRead:
        client = AsyncQdrantClient(":memory:")
        embedder = HashEmbedder(32)
        collection = f"kit_{uuid.uuid4().hex}"
        store = QdrantChunkStore(client, collection, embedder, self._session_bm25)

        async def setup() -> None:
            await store.ensure_collection()
            await store.upsert_chunks(list(chunks))

        asyncio.run(setup())
        return HybridRetriever(store, embedder, self._session_bm25, audit_sink=None)
