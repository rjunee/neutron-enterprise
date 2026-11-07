# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pyright-checked structural conformance of ne-brain adapters to ne.contracts.

Typed assignments below only type-check if the adapter's shape truly satisfies
the frozen Protocol; `uv run pyright` enforces this statically. The runtime
assert is a trivial belt-and-braces smoke check.
"""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient

from ne.brain.embedder import TEIEmbedder
from ne.brain.pipeline import RetrievalPipeline
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.reranker import TEIReranker
from ne.brain.retriever import HybridRetriever
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.contracts.knowledge import ChunkStore, Embedder, KnowledgeRead, Reranker, SparseEncoder


def test_adapters_satisfy_frozen_protocols() -> None:
    hash_embedder = HashEmbedder()
    encoder = FastEmbedBM25Encoder()
    store: ChunkStore = QdrantChunkStore(
        AsyncQdrantClient(":memory:"), "conformance", hash_embedder, encoder
    )
    enc: SparseEncoder = encoder
    emb: Embedder = TEIEmbedder("http://tei.local")
    emb2: Embedder = hash_embedder
    kr: KnowledgeRead = HybridRetriever(store, hash_embedder, encoder, audit_sink=None)
    rr: Reranker = TEIReranker("http://tei.local")
    kr_pipeline: KnowledgeRead = RetrievalPipeline(
        HybridRetriever(store, hash_embedder, encoder, audit_sink=None),
        TEIReranker("http://tei.local"),
    )

    assert store is not None
    assert enc is not None
    assert emb is not None
    assert emb2 is not None
    assert kr is not None
    assert rr is not None
    assert kr_pipeline is not None
