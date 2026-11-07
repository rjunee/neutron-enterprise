# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED end-to-end: full governed write loop on real Qdrant + real Postgres.

Runs only when the operator NAMES both stores. Neither address has a default: on
a box running the demo stack the old Qdrant default WAS the deployed vector
store, which is how orphan collections got there. The collection this makes is
dropped in fixture teardown (``owned_qdrant_collections``), pass, fail or error.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.testing.qdrant_live import (
    OwnedQdrantCollections,
    qdrant_server_url,
    server_reachable,
)
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ShapeContract,
    SqliteWriteQuarantine,
)
from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.contracts.knowledge import KnowledgeItem
from ne.events import ack, ensure_outbox_schema, read_batch

_QDRANT = qdrant_server_url()
_POSTGRES = os.environ.get("NE_TEST_POSTGRES_URL")

if _POSTGRES is None or not server_reachable(_QDRANT):
    pytest.skip(
        "LIVE-PARKED: real Qdrant + real Postgres e2e — set NE_TEST_POSTGRES_URL, and "
        "name a Qdrant YOU own in NE_QDRANT_URL (never the deployed stack's)",
        allow_module_level=True,
    )

_CONTRACT = ShapeContract(
    name="ne.knowledge.note",
    version="1",
    json_schema={
        "type": "object",
        "required": ["content"],
        "properties": {"content": {"type": "string", "minLength": 1}},
    },
)


class _Settings(NeSettings):
    demo: int = 1


def test_live_upsert_violation_outbox_and_audit_loop(
    owned_qdrant_collections: OwnedQdrantCollections,
) -> None:
    assert _POSTGRES is not None  # guaranteed by the module-level skip
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_Settings())
    engine = sa.create_engine(_POSTGRES)
    try:
        ensure_outbox_schema(engine)
        ensure_audit_schema(engine)
        client_store = QdrantChunkStore(
            owned_qdrant_collections.client(),
            owned_qdrant_collections.collection("kw_e2e"),
            HashEmbedder(32),
            HashSparseEncoder(),
        )
        asyncio.run(client_store.ensure_collection())
        writer = KnowledgeWriter(
            client_store,
            SqliteWriteQuarantine(":memory:"),
            {_CONTRACT.name: _CONTRACT},
            default_shape=_CONTRACT.name,
            actor="e2e",
            audit_sink=PostgresAuditSink(engine),
            violation_sink=OutboxShapeViolationSink(engine),
        )

        async def scenario() -> None:
            receipt = await writer.upsert(
                [
                    KnowledgeItem(item_id="live-ok", content="quarterly report", provenance="src"),
                    KnowledgeItem(item_id="live-bad", content="", provenance="src"),
                ]
            )
            assert receipt.accepted == ("live-ok",)
            assert receipt.quarantined == ("live-bad",)

            dense = list((await HashEmbedder(32).embed(["quarterly report"]))[0])
            hits = await client_store.query(
                dense=dense, filters={"metadata.provenance.provenance_class": "curated"}
            )
            assert "live-ok" in {h.chunk.chunk_id for h in hits}

        asyncio.run(scenario())

        consumer = f"e2e-{uuid.uuid4().hex}"
        with engine.begin() as conn:
            records, high = read_batch(conn, consumer, event_types=["shape_violation"])
            ack(conn, consumer, high)
        assert any(r.payload["subject_ref"] == "live-bad" for r in records)

        with engine.begin() as conn:
            audit = conn.execute(sa.select(ne_audit_log)).mappings().all()
        assert any(r["config_hash"] == digest for r in audit)
    finally:
        reset_config_hash_for_testing()
        engine.dispose()
