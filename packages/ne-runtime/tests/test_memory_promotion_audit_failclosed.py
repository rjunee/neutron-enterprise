# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint-D boundary-audit #6 real-seam proof: a promoted/retrievable fact can
NEVER stand permanently unaudited.

Real stack, no mock past the frozen ``KnowledgeWrite`` seam: a REAL ne-brain
:class:`KnowledgeWriter` over :memory: Qdrant + a real sqlite quarantine lane + a
real :class:`PostgresAuditSink` (on sqlite) for the writer's OWN audit, wired
through the DOCUMENTED :func:`ne.runtime.create_memory_promoter` recipe. The
PROMOTER's audit sink is the injected failing/recovering double.

``ne-brain`` enters here only as a dev/test dependency (the src seam is
unchanged). Async is exercised via ``asyncio.run`` (pytest-asyncio is forbidden);
the autouse ``frozen_config_hash`` fixture in this package's conftest covers
``config_hash``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write import (
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    ShapeContract,
    SqliteWriteQuarantine,
)
from ne.events import ensure_outbox_schema
from ne.runtime.errors import MemoryPromotionAuditError
from ne.runtime.factory import create_memory_promoter
from ne.runtime.memory_promotion import MemoryPromoter
from ne.runtime.memory_store import (
    MemoryEventKind,
    SqlAlchemyEpisodicStore,
    ensure_memory_schema,
)

from .conftest import AppendCountFailingSink, FailOnceAuditSink, RecordingAuditSink

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

_CONTENT = "the plating temp is 180C"


class _RecipeStack:
    """The DOCUMENTED create_memory_promoter recipe over real seams."""

    def __init__(self, tmp_path, *, promoter_sink) -> None:
        client = AsyncQdrantClient(":memory:")
        self.store = QdrantChunkStore(
            client, f"kw_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
        )
        asyncio.run(self.store.ensure_collection())
        self.quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
        self.audit_engine = sa.create_engine("sqlite://")
        ensure_outbox_schema(self.audit_engine)
        ensure_audit_schema(self.audit_engine)
        self.writer = KnowledgeWriter(
            self.store,
            self.quarantine,
            {NOTE_CONTRACT.name: NOTE_CONTRACT},
            default_shape=NOTE_CONTRACT.name,
            actor="svc:memory-promotion",
            default_provenance_class=ProvenanceClass.LEARNED_BY_AGENT,
            audit_sink=PostgresAuditSink(self.audit_engine),
            violation_sink=OutboxShapeViolationSink(self.audit_engine),
        )
        self.memory_engine = sa.create_engine("sqlite://")
        ensure_memory_schema(self.memory_engine)
        self.episodic = SqlAlchemyEpisodicStore(self.memory_engine)
        self.promoter: MemoryPromoter = create_memory_promoter(
            writer=self.writer, sink=promoter_sink, engine=self.memory_engine
        )

    async def append_learned_fact(self) -> str:
        return await self.episodic.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content=_CONTENT,
            occurred_at=datetime.now(UTC),
            session_id="s1",
        )

    def writer_audit_actions(self, resource: str) -> list[str]:
        with self.audit_engine.begin() as conn:
            rows = conn.execute(sa.select(ne_audit_log)).mappings().all()
        payloads = [dict(r["payload"]) for r in rows]
        return [p["action"] for p in payloads if p.get("resource") == resource]


def test_failed_promotion_audit_leaves_no_retrievable_unaudited_chunk(tmp_path) -> None:
    stack = _RecipeStack(
        tmp_path, promoter_sink=AppendCountFailingSink("memory.promotion.promoted")
    )

    async def scenario() -> None:
        event_id = await stack.append_learned_fact()
        item_id = f"learned-{event_id}"
        proposal_id = await stack.promoter.propose(agent_id="agent-a", event_ids=[event_id])
        try:
            await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
            raise AssertionError("expected MemoryPromotionAuditError")
        except MemoryPromotionAuditError:
            pass
        # NO orphan retrievable chunk — the upserted chunk was unwound.
        assert await stack.store.get([item_id]) == []
        # The full payload is parked (never lost) via park-before-delete.
        assert stack.quarantine.get(item_id) is not None
        # No approval row — the promotion is cleanly retryable.
        approved = await stack.episodic.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert approved == []
        # The unwind itself is audited: both the upsert AND the quarantine row exist.
        actions = stack.writer_audit_actions(f"knowledge://{item_id}")
        assert "knowledge.upsert" in actions
        assert "knowledge.quarantine" in actions

    asyncio.run(scenario())


def test_retry_after_audit_recovery_promotes_and_audits(tmp_path) -> None:
    sink = FailOnceAuditSink("memory.promotion.promoted")
    stack = _RecipeStack(tmp_path, promoter_sink=sink)

    async def scenario() -> None:
        event_id = await stack.append_learned_fact()
        item_id = f"learned-{event_id}"
        proposal_id = await stack.promoter.propose(agent_id="agent-a", event_ids=[event_id])
        try:
            await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
            raise AssertionError("expected MemoryPromotionAuditError on first attempt")
        except MemoryPromotionAuditError:
            pass
        # After sink recovery the retry succeeds (guard not armed).
        await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        # End state: the chunk IS retrievable, backed by its audit record.
        assert await stack.store.get([item_id]) != []
        approved = await stack.episodic.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert len(approved) == 1
        assert len(sink.with_action("memory.promotion.promoted")) == 1
        # Re-upsert cleared the parked row — no chunk without a matching audit event.
        assert stack.quarantine.get(item_id) is None

    asyncio.run(scenario())


def test_documented_recipe_produces_audited_writer(tmp_path) -> None:
    sink = RecordingAuditSink()
    stack = _RecipeStack(tmp_path, promoter_sink=sink)

    async def scenario() -> None:
        event_id = await stack.append_learned_fact()
        item_id = f"learned-{event_id}"
        proposal_id = await stack.promoter.propose(agent_id="agent-a", event_ids=[event_id])
        receipt = await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert receipt.accepted == (item_id,)
        # The recipe writer audits its governed upsert.
        assert "knowledge.upsert" in stack.writer_audit_actions(f"knowledge://{item_id}")
        # Both promotion stages are audited through the promoter sink.
        assert sink.with_action("memory.promotion.proposed")
        assert sink.with_action("memory.promotion.promoted")

    asyncio.run(scenario())
