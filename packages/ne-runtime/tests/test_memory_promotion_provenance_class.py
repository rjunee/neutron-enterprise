# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closes NE-037 Codex P2 at the store-filter seam (NE-CKC12).

Real stack, no mocking past the frozen ``KnowledgeWrite`` seam: a REAL ne-brain
:class:`KnowledgeWriter` over :memory: Qdrant, wired through
:func:`ne.runtime.create_memory_promoter`, proves a promoted learned-fact is
class-queryable via ``metadata.provenance.provenance_class == "learned-by-agent"``
— but ONLY when the composition root constructs the writer with
``default_provenance_class=ProvenanceClass.LEARNED_BY_AGENT``. The sibling
negative test pins that a CURATED-default writer reproduces the P2 (the
``learned-by-agent`` SOURCE string alone does NOT set the queryable CLASS), which
is why the create_memory_promoter construction requirement is normative.

``ne-brain`` enters here only as a dev/test dependency (the src seam is unchanged).
Async is exercised via ``asyncio.run`` (pytest-asyncio is forbidden in this repo).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    ShapeContract,
    SqliteWriteQuarantine,
)
from ne.brain.write.contracts import provenance_from_chunk
from ne.events import ensure_outbox_schema
from ne.runtime.factory import create_memory_promoter
from ne.runtime.memory_store import (
    MemoryEventKind,
    SqlAlchemyEpisodicStore,
    ensure_memory_schema,
)

from .conftest import RecordingAuditSink

NOTE_CONTRACT = ShapeContract(
    name="ne.knowledge.note",
    version="1",
    json_schema={
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "metadata": {"type": "object"},
        },
    },
)

_OCCURRED_AT = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
_CONTENT = "the plating temp is 180C"


def _stack(tmp_path, *, provenance_class: ProvenanceClass):
    """Build a REAL (writer, store) over :memory: Qdrant + a sqlite quarantine lane."""
    client = AsyncQdrantClient(":memory:")
    store = QdrantChunkStore(
        client, f"kw_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
    )
    asyncio.run(store.ensure_collection())
    quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)
    writer = KnowledgeWriter(
        store,
        quarantine,
        {NOTE_CONTRACT.name: NOTE_CONTRACT},
        default_shape=NOTE_CONTRACT.name,
        actor="svc:memory-promotion",
        default_provenance_class=provenance_class,
        audit_sink=PostgresAuditSink(engine),
        violation_sink=OutboxShapeViolationSink(engine),
    )
    return writer, store


def _promote(writer, *, sink: RecordingAuditSink) -> str:
    """Append a learned fact, propose it, promote it; return the source event id."""
    engine = sa.create_engine("sqlite://")
    ensure_memory_schema(engine)
    episodic = SqlAlchemyEpisodicStore(engine)

    async def scenario() -> str:
        event_id = await episodic.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content=_CONTENT,
            occurred_at=_OCCURRED_AT,
            session_id="s1",
        )
        promoter = create_memory_promoter(writer=writer, sink=sink, engine=engine)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        receipt = await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert receipt.accepted == (f"learned-{event_id}",)
        return event_id

    return asyncio.run(scenario())


def _class_filter_hits(store: QdrantChunkStore) -> set[str]:
    async def _query() -> set[str]:
        dense = list((await HashEmbedder(32).embed([_CONTENT]))[0])
        hits = await store.query(
            dense=dense,
            filters={"metadata.provenance.provenance_class": "learned-by-agent"},
        )
        return {h.chunk.chunk_id for h in hits}

    return asyncio.run(_query())


def test_promoted_item_is_class_queryable_as_learned_by_agent(
    tmp_path, recording_sink: RecordingAuditSink
) -> None:
    writer, store = _stack(tmp_path, provenance_class=ProvenanceClass.LEARNED_BY_AGENT)
    event_id = _promote(writer, sink=recording_sink)
    item_id = f"learned-{event_id}"

    stored = asyncio.run(store.get([item_id]))
    stamp = provenance_from_chunk(stored[0])
    assert stamp is not None
    # Stamp read-back: the queryable CLASS is learned-by-agent, and the SOURCE
    # string is the provenance the promoter passed.
    assert stamp.provenance_class == ProvenanceClass.LEARNED_BY_AGENT
    assert stamp.source == "learned-by-agent"
    # Store-filter seam: the promoted item is retrievable by the learned-by-agent
    # provenance-class filter (closes the NE-037 Codex P2 pendingLiveVerification).
    assert item_id in _class_filter_hits(store)


def test_curated_default_writer_reproduces_the_p2(
    tmp_path, recording_sink: RecordingAuditSink
) -> None:
    # Identical stack, but the writer is left at the CURATED default.
    writer, store = _stack(tmp_path, provenance_class=ProvenanceClass.CURATED)
    event_id = _promote(writer, sink=recording_sink)
    item_id = f"learned-{event_id}"

    stored = asyncio.run(store.get([item_id]))
    stamp = provenance_from_chunk(stored[0])
    assert stamp is not None
    # The SOURCE string is still learned-by-agent, but the queryable CLASS stays
    # curated — so the learned-by-agent class filter does NOT return it (the P2).
    assert stamp.source == "learned-by-agent"
    assert stamp.provenance_class == ProvenanceClass.CURATED
    assert item_id not in _class_filter_hits(store)
