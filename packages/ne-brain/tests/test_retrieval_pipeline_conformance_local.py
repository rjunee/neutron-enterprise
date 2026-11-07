# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeReadContractKit over the FULL RetrievalPipeline, local Qdrant mode.

Proves the production retrieval path (embed -> RRF fusion -> rerank ->
citations) satisfies the frozen KnowledgeRead contract with the ONLY stub at
the TEI /rerank HTTP boundary — real :memory: Qdrant + real FastEmbed BM25, no
mocking past the seam. Supplementary to the real-server run in
test_retrieval_pipeline_conformance_server.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.pipeline import RetrievalPipeline
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.reranker import TEIReranker
from ne.brain.retriever import HybridRetriever
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.testing.kits.knowledge_read import KnowledgeReadContractKit
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Chunk


async def _rerank_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.read())
    qtokens = set(body["query"].lower().split())
    entries = [
        {"index": i, "score": len(qtokens & set(text.lower().split())) / max(len(qtokens), 1)}
        for i, text in enumerate(body["texts"])
    ]
    return httpx.Response(200, json=entries)


class TestRetrievalPipelineLocalMode(KnowledgeReadContractKit):
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
        retriever = HybridRetriever(store, embedder, self._session_bm25, audit_sink=None)
        reranker = TEIReranker(
            "http://tei.local",
            client=httpx.AsyncClient(transport=httpx.MockTransport(_rerank_handler)),
        )
        return RetrievalPipeline(retriever, reranker)
