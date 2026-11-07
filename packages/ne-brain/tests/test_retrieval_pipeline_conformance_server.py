# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeReadContractKit over the full RetrievalPipeline against a REAL
Qdrant plus a REAL TEI reranker.

Live-parked unless the operator NAMES a Qdrant with ``NE_QDRANT_URL``. There is
no default: on a box running the demo stack the default WAS the deployed store,
and this module wrote 985 orphan collections into it before 2026-09-10. The
collections it makes now are dropped in fixture teardown
(``owned_qdrant_collections``), pass, fail or error.

The reranker is read-only, so it keeps its reachability gate; the Qdrant is
written to, so it does not.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

import httpx
import pytest

from ne.brain.pipeline import RetrievalPipeline
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.reranker import TEIReranker
from ne.brain.retriever import HybridRetriever
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.testing.kits.knowledge_read import KnowledgeReadContractKit
from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    server_reachable,
)
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Chunk


def _tei_rerank_url() -> str:
    return os.environ.get("NE_TEI_RERANK_URL", "http://localhost:8086")


def _rerank_reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


_QDRANT_URL = qdrant_server_url()
_RERANK_URL = _tei_rerank_url()

if not (server_reachable(_QDRANT_URL) and _rerank_reachable(_RERANK_URL)):
    pytest.skip(
        "LIVE-PARKED: real-stack pipeline conformance needs a Qdrant YOU own, named "
        "in NE_QDRANT_URL (never the deployed stack's), plus a reachable TEI reranker",
        allow_module_level=True,
    )


class TestRetrievalPipelineServerMode(KnowledgeReadContractKit):
    @pytest.fixture(autouse=True)
    def _live_store(
        self, bm25_encoder: FastEmbedBM25Encoder, owned_qdrant_collections: OwnedQdrantCollections
    ) -> None:
        self._session_bm25 = bm25_encoder
        self._owned = owned_qdrant_collections

    def create_reader(self, chunks: Sequence[Chunk]) -> KnowledgeRead:
        client = self._owned.client()
        embedder = HashEmbedder(32)
        store = QdrantChunkStore(
            client, self._owned.collection("kit"), embedder, self._session_bm25
        )

        async def setup() -> None:
            await store.ensure_collection()
            await store.upsert_chunks(list(chunks))

        asyncio.run(setup())
        retriever = HybridRetriever(store, embedder, self._session_bm25, audit_sink=None)
        reranker = TEIReranker(_RERANK_URL)
        return RetrievalPipeline(retriever, reranker)
