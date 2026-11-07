# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ChunkStoreContractKit against a REAL Qdrant server (dev-stack v1.18.2).

THIS is the actual conformance claim against real Qdrant — local mode
(test_chunk_store_conformance_local.py) is supplementary always-on CI
coverage using the vendor's in-process implementation.

Live-parked unless the operator NAMES a Qdrant with ``NE_QDRANT_URL``. There is
no default: on a box running the demo stack the default WAS the deployed store,
and this module wrote 985 orphan collections into it before 2026-09-10. The
collections it makes now are dropped in fixture teardown
(``owned_qdrant_collections``), pass, fail or error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from ne.brain.qdrant import QdrantChunkStore
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.testing.kits.chunk_store import ChunkStoreContractKit
from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    server_reachable,
)
from ne.contracts.knowledge import ChunkStore
from ne.contracts.types import SparseVector

_URL = qdrant_server_url()

if not server_reachable(_URL):
    pytest.skip(
        "LIVE-PARKED: real-Qdrant conformance needs a Qdrant YOU own — name it in "
        "NE_QDRANT_URL (never the deployed stack's; it is not a scratch store)",
        allow_module_level=True,
    )


class TestQdrantChunkStoreServerMode(ChunkStoreContractKit):
    @pytest.fixture(autouse=True)
    def _live_store(
        self, bm25_encoder: FastEmbedBM25Encoder, owned_qdrant_collections: OwnedQdrantCollections
    ) -> None:
        self._session_bm25 = bm25_encoder
        self._owned = owned_qdrant_collections

    def create_store(self) -> ChunkStore:
        client = self._owned.client()
        embedder = HashEmbedder(32)
        store = QdrantChunkStore(
            client, self._owned.collection("kit"), embedder, self._session_bm25
        )
        asyncio.run(store.ensure_collection())
        self._embedder = embedder
        return store

    def embed_text(self, text: str) -> Sequence[float]:
        return asyncio.run(self._embedder.embed([text]))[0]

    def encode_query(self, text: str) -> SparseVector:
        return asyncio.run(self._session_bm25.encode_queries([text]))[0]
