# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-026 spine embed+index happy path against a REAL Qdrant server (dev-stack).

THIS is the real-server counterpart to the always-on local-mode coverage in
test_ingest_spine_qdrant.py.

Live-parked unless the operator NAMES a Qdrant with ``NE_QDRANT_URL``. There is
no default: on a box running the demo stack the default WAS the deployed store,
and this module wrote orphan ``spine_*`` collections into it before 2026-09-10.
The collections it makes now are dropped in fixture teardown
(``owned_qdrant_collections``), pass, fail or error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ne.brain.ingest.chunker import StructuralChunker
from ne.brain.ingest.contracts import ParsedDocument
from ne.brain.ingest.docling_parser import parsed_document_from_json
from ne.brain.ingest.ledger import SqliteIngestLedger
from ne.brain.ingest.spine import IngestionPipeline, IngestSource, IngestStatus
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    server_reachable,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
_URL = qdrant_server_url()

if not server_reachable(_URL):
    pytest.skip(
        "LIVE-PARKED: real-Qdrant spine integration needs a Qdrant YOU own — name it "
        "in NE_QDRANT_URL (never the deployed stack's; it is not a scratch store)",
        allow_module_level=True,
    )


class _FixtureParser:
    def __init__(self, fixture: Path) -> None:
        self._raw = fixture.read_bytes()

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        base = parsed_document_from_json(self._raw)
        return ParsedDocument(
            document_id=document_id,
            source_name=base.source_name,
            source_format=base.source_format,
            blocks=base.blocks,
            metadata=base.metadata,
        )


def test_ingest_indexes_chunks_retrievable_against_real_qdrant(
    tmp_path: Path, owned_qdrant_collections: OwnedQdrantCollections
) -> None:
    client = owned_qdrant_collections.client()
    collection = owned_qdrant_collections.collection("spine")
    store = QdrantChunkStore(client, collection, HashEmbedder(32), HashSparseEncoder())
    asyncio.run(store.ensure_collection())

    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    pipeline = IngestionPipeline(
        _FixtureParser(_FIXTURES / "sample_report.parsed.json"),
        StructuralChunker(),
        store,
        ledger,
    )
    source_file = tmp_path / "doc.txt"
    source_file.write_text("full")
    src = IngestSource(path=source_file, document_id="report")

    outcome = asyncio.run(pipeline.ingest(src))
    assert outcome.status == IngestStatus.INDEXED
    assert outcome.chunk_count > 0

    state = ledger.get_state("report")
    assert state is not None
    fetched = asyncio.run(store.get(list(state.indexed_chunk_ids)))
    assert {c.chunk_id for c in fetched} == set(state.indexed_chunk_ids)

    # Re-ingest identical bytes is a no-op (idempotent) on the real server too.
    second = asyncio.run(pipeline.ingest(src))
    assert second.status == IngestStatus.UNCHANGED


def test_reingest_narrowed_acl_updates_payload_against_real_qdrant(
    tmp_path: Path, owned_qdrant_collections: OwnedQdrantCollections
) -> None:
    client = owned_qdrant_collections.client()
    collection = owned_qdrant_collections.collection("spine")
    store = QdrantChunkStore(client, collection, HashEmbedder(32), HashSparseEncoder())
    asyncio.run(store.ensure_collection())

    ledger = SqliteIngestLedger(tmp_path / "ledger.db")
    pipeline = IngestionPipeline(
        _FixtureParser(_FIXTURES / "sample_report.parsed.json"),
        StructuralChunker(),
        store,
        ledger,
    )
    source_file = tmp_path / "doc.txt"
    source_file.write_text("full")
    src = IngestSource(path=source_file, document_id="report", acl=("group-a", "group-b"))

    first = asyncio.run(pipeline.ingest(src))
    assert first.status == IngestStatus.INDEXED

    # Same bytes, narrowed acl -> payload-only correction against the real server.
    narrowed = asyncio.run(
        pipeline.ingest(IngestSource(path=source_file, document_id="report", acl=("group-a",)))
    )
    assert narrowed.status == IngestStatus.INDEXED
    assert narrowed.content_hash == first.content_hash

    state = ledger.get_state("report")
    assert state is not None
    fetched = asyncio.run(store.get(list(state.indexed_chunk_ids)))
    assert fetched
    assert all(c.acl == ("group-a",) for c in fetched)
