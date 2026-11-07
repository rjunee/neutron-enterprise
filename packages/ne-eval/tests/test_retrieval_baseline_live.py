# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED: the same baseline proof against the REAL Qdrant + TEI dev stack.

Module-skipped unless the operator NAMES a Qdrant in ``NE_QDRANT_URL`` and the TEI
embedder (``NE_TEI_EMBED_URL``) and reranker (``NE_TEI_RERANK_URL``) are reachable
— i.e. the NE-013/NE-070 dev-stack wave. The Qdrant address has NO default: this
module's ``rag_public_live_*`` collections were part of the 985 orphans that took
the live store to ``status: red`` on 2026-09-10 (see
``ne.brain.testing.qdrant_live``). When live, it ingests the
public corpus into a real Qdrant collection (TEI dense + FastEmbed BM25 sparse),
retrieves through a ``RetrievalPipeline`` with the real ``TEIReranker``, and
asserts ONLY the deterministic metric ``recall_at_10 >= 0.83`` (the WARN-band
floor).

The JUDGED metrics on the live stack stay LIVE-PARKED until the NE-013 vLLM
gateway judge exists (the deterministic LexicalJudge is a CI stand-in, not the
production judge), so this module never asserts faithfulness/precision/recall-
judged numbers against a live judge.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    server_reachable,
)
from ne.contracts.types import Principal
from ne.eval.baseline import KnowledgeReadEvalTarget
from ne.eval.golden import load_golden_set
from ne.eval.metrics import mean_score, recall_at_k

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_PUBLIC = Path(__file__).resolve().parent.parent / "goldensets" / "public"

QDRANT_URL = qdrant_server_url()
TEI_EMBED_URL = os.environ.get("NE_TEI_EMBED_URL", "http://localhost:8085")
TEI_RERANK_URL = os.environ.get("NE_TEI_RERANK_URL", "http://localhost:8086")


def _reachable(url: str, path: str) -> bool:
    try:
        return httpx.get(f"{url}{path}", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


_LIVE = (
    server_reachable(QDRANT_URL)
    and _reachable(TEI_EMBED_URL, "/health")
    and _reachable(TEI_RERANK_URL, "/health")
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "LIVE-PARKED: name a Qdrant YOU own in NE_QDRANT_URL (never the deployed "
        "stack's) and start the TEI pair (NE-013/NE-070 wave)"
    ),
)


def test_recall_on_live_stack(owned_qdrant_collections: OwnedQdrantCollections) -> None:
    from ne.brain.embedder import TEIEmbedder
    from ne.brain.ingest.chunker import StructuralChunker
    from ne.brain.ingest.contracts import BlockKind, DocBlock, ParsedDocument
    from ne.brain.pipeline import RetrievalPipeline
    from ne.brain.qdrant import QdrantChunkStore
    from ne.brain.reranker import TEIReranker
    from ne.brain.retriever import HybridRetriever
    from ne.brain.sparse import FastEmbedBM25Encoder

    corpus = [
        json.loads(line)
        for line in (_PUBLIC / "squad_corpus.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    golden = load_golden_set(_PUBLIC / "squad_golden.jsonl")

    chunker = StructuralChunker()
    all_chunks = []
    for row in corpus:
        doc = ParsedDocument(
            document_id=row["document_id"],
            source_name=row["title"],
            source_format="md",
            blocks=(
                DocBlock(kind=BlockKind.HEADING, text=row["title"], level=1),
                DocBlock(kind=BlockKind.PARAGRAPH, text=row["text"]),
            ),
        )
        all_chunks.extend(chunker.chunk(doc))

    embedder = TEIEmbedder(TEI_EMBED_URL)
    bm25 = FastEmbedBM25Encoder()
    # Registered for teardown BEFORE it is created: `owned_qdrant_collections`
    # drops it in a fixture `finally`, so a failure anywhere below — including in
    # `upsert_chunks`, which the old inline delete sat after — cannot leak it.
    collection = owned_qdrant_collections.collection("rag_public_live")
    store = QdrantChunkStore(owned_qdrant_collections.client(), collection, embedder, bm25)
    pipeline = RetrievalPipeline(
        HybridRetriever(store, embedder, bm25, audit_sink=None),
        TEIReranker(TEI_RERANK_URL),
        candidate_multiplier=4,
    )
    target = KnowledgeReadEvalTarget(pipeline, Principal(id="eval-live"), top_k=10)

    async def _run() -> float:
        await store.ensure_collection()
        await store.upsert_chunks(all_chunks)
        try:
            scores = []
            for item in golden.items:
                retrieved = await target.retrieve(item.question)
                scores.append(recall_at_k(item, retrieved, k=10))
            mean = mean_score(scores)
            assert mean is not None
            return mean
        finally:
            await embedder.aclose()

    recall = asyncio.run(_run())
    assert recall >= 0.83, f"live recall_at_10 {recall} below WARN-band floor"
