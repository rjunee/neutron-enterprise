# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Embed+index integration for the NE-026 spine, OFFLINE in CI.

Proves the spine actually embeds chunks and indexes them into a real
``QdrantChunkStore`` (qdrant-client local ``:memory:`` mode) such that they are
retrievable afterwards — with NO network and NO skips: the dense embedder is the
deterministic ``HashEmbedder`` and the sparse encoder is the deterministic
``HashSparseEncoder`` (no fastembed HF asset download). The same happy path runs
against a REAL Qdrant server in ``test_ingest_spine_server.py`` (live-parked).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path

from qdrant_client import AsyncQdrantClient

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import ParsedDocument
from ne.brain.ingest.docling_parser import parsed_document_from_json
from ne.brain.ingest.ledger import SqliteIngestLedger
from ne.brain.ingest.spine import IngestionPipeline, IngestSource, IngestStatus
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder

_FIXTURES = Path(__file__).parent / "fixtures" / "ingest"


class FixtureParser:
    """Replays the committed sample_report fixture, truncatable via the source file.

    The source file's text selects how many leading blocks to keep (``"full"`` =
    all), so a "modified" document produces fewer chunks under the same
    document_id and its bytes hash differently (drives the supersede test) —
    without the docling runtime.
    """

    def __init__(self, fixture: Path) -> None:
        self._raw = fixture.read_bytes()

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        selector = Path(path).read_text().strip()
        base = parsed_document_from_json(self._raw)
        keep = len(base.blocks) if selector == "full" else int(selector)
        return ParsedDocument(
            document_id=document_id,
            source_name=base.source_name,
            source_format=base.source_format,
            blocks=base.blocks[:keep],
            metadata=base.metadata,
        )


class CountingHashEmbedder(HashEmbedder):
    """HashEmbedder that counts embed() calls (proves the no-re-embed cheap path)."""

    def __init__(self, dimension: int = 32) -> None:
        super().__init__(dimension)
        self.embed_calls = 0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.embed_calls += 1
        return await super().embed(texts)


def _make(
    tmp_path: Path, *, embedder: HashEmbedder | None = None
) -> tuple[IngestionPipeline, QdrantChunkStore, AsyncQdrantClient, str]:
    client = AsyncQdrantClient(":memory:")
    collection = f"spine_{uuid.uuid4().hex}"
    store = QdrantChunkStore(client, collection, embedder or HashEmbedder(32), HashSparseEncoder())
    asyncio.run(store.ensure_collection())
    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    pipeline = IngestionPipeline(
        FixtureParser(_FIXTURES / "sample_report.parsed.json"),
        StructuralChunker(),
        store,
        ledger,
    )
    return pipeline, store, client, collection


def _write(path: Path, selector: str) -> Path:
    path.write_text(selector)
    return path


def test_ingest_indexes_chunks_retrievable_after_ingest(tmp_path: Path) -> None:
    pipeline, store, _, _ = _make(tmp_path)
    src = IngestSource(path=_write(tmp_path / "doc.txt", "full"), document_id="report")

    outcome = asyncio.run(pipeline.ingest(src))
    assert outcome.status == IngestStatus.INDEXED
    assert outcome.chunk_count > 0

    state = pipeline._ledger.get_state("report")
    assert state is not None
    indexed_ids = list(state.indexed_chunk_ids)

    # Every ledgered chunk id is fetchable from the index.
    fetched = asyncio.run(store.get(indexed_ids))
    assert {c.chunk_id for c in fetched} == set(indexed_ids)

    # Dense query with an indexed chunk's EXACT text ranks that chunk first
    # (the established HashEmbedder relevance property).
    target = fetched[0]
    query_vec = asyncio.run(HashEmbedder(32).embed([target.text]))[0]
    results = asyncio.run(store.query(dense=query_vec, top_k=3))
    assert results
    assert results[0].chunk.chunk_id == target.chunk_id


def test_reingest_same_content_no_duplicate_points(tmp_path: Path) -> None:
    pipeline, _, client, collection = _make(tmp_path)
    src = IngestSource(path=_write(tmp_path / "doc.txt", "full"), document_id="report")

    asyncio.run(pipeline.ingest(src))
    count_before = asyncio.run(client.count(collection)).count

    second = asyncio.run(pipeline.ingest(src))
    assert second.status == IngestStatus.UNCHANGED
    count_after = asyncio.run(client.count(collection)).count
    # Idempotent: identical bytes add zero points.
    assert count_after == count_before


def test_changed_doc_supersedes_old_version_in_index(tmp_path: Path) -> None:
    pipeline, store, _, _ = _make(tmp_path)
    path = tmp_path / "doc.txt"
    src = IngestSource(path=_write(path, "full"), document_id="report")

    asyncio.run(pipeline.ingest(src))
    ids_v1 = set(pipeline._ledger.get_state("report").indexed_chunk_ids)  # type: ignore[union-attr]

    # A shorter version (fewer leading blocks) under the same document_id.
    _write(path, "5")
    outcome = asyncio.run(pipeline.ingest(src))
    assert outcome.status == IngestStatus.INDEXED
    ids_v2 = set(pipeline._ledger.get_state("report").indexed_chunk_ids)  # type: ignore[union-attr]

    stale = ids_v1 - ids_v2
    assert stale, "expected the shorter version to leave at least one stale chunk id"
    # Stale points are gone from the real (local-mode) index; live ids remain.
    assert asyncio.run(store.get(sorted(stale))) == []
    assert {c.chunk_id for c in asyncio.run(store.get(sorted(ids_v2)))} == ids_v2


def test_reingest_same_bytes_changed_acl_updates_payload_without_reembedding(
    tmp_path: Path,
) -> None:
    embedder = CountingHashEmbedder(32)
    pipeline, store, _, _ = _make(tmp_path, embedder=embedder)
    path = _write(tmp_path / "doc.txt", "full")

    first = asyncio.run(
        pipeline.ingest(IngestSource(path=path, document_id="report", acl=("group-a",)))
    )
    assert first.status == IngestStatus.INDEXED
    calls_after_first = embedder.embed_calls
    assert calls_after_first > 0

    # SAME bytes, narrowed acl -> cheap payload-only correction against the real
    # (local-mode) QdrantChunkStore.set_payload, with ZERO additional embed calls.
    second = asyncio.run(pipeline.ingest(IngestSource(path=path, document_id="report", acl=())))
    assert second.status == IngestStatus.INDEXED
    assert second.content_hash == first.content_hash
    assert embedder.embed_calls == calls_after_first

    state = pipeline._ledger.get_state("report")
    assert state is not None
    fetched = asyncio.run(store.get(list(state.indexed_chunk_ids)))
    assert fetched
    assert all(c.acl == () for c in fetched)


def test_source_metadata_usable_by_retrieval_filter(tmp_path: Path) -> None:
    pipeline, store, _, _ = _make(tmp_path)
    src = IngestSource(
        path=_write(tmp_path / "doc.txt", "full"),
        document_id="report",
        metadata={"department": "eng"},
    )
    asyncio.run(pipeline.ingest(src))

    state = pipeline._ledger.get_state("report")
    assert state is not None
    target = asyncio.run(store.get(list(state.indexed_chunk_ids)))[0]
    query_vec = asyncio.run(HashEmbedder(32).embed([target.text]))[0]

    hits = asyncio.run(
        store.query(dense=query_vec, top_k=5, filters={"metadata.source.department": "eng"})
    )
    assert hits
    assert all(sc.chunk.metadata["source"]["department"] == "eng" for sc in hits)

    misses = asyncio.run(
        store.query(dense=query_vec, top_k=5, filters={"metadata.source.department": "legal"})
    )
    assert not misses
