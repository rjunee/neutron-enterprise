# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKD6 — the exact masked Checkpoint-D path: ONE AuditSink instance shared by
BOTH the ``KnowledgeWriter`` and the ``MemoryPromoter`` (the documented
``create_memory_promoter`` recipe), failing on the ``knowledge.upsert`` action.

Before NE-CKD6 a single shared-audit-sink outage surfaced INSIDE the writer:
``KnowledgeWriter.upsert`` committed to Qdrant and THEN its per-item audit append
FAILED, propagating out of ``MemoryPromoter.promote()`` OUTSIDE its step-2 unwind
— leaving chunks durable+retrievable with ZERO audit records. This proves the
writer is now ATOMIC (store+audit or neither): the failure leaves no
retrievable-but-unaudited chunk, and a retry after recovery heals idempotently.

Real stack, no mock past the sink seam: real :memory: Qdrant + real sqlite
quarantine + a real :class:`PostgresAuditSink` (on sqlite) as the DELEGATE for the
shared sink; the ONLY double is the ``_SharedFlakySink`` wrapper injecting the
outage. Async via ``asyncio.run``; the autouse ``frozen_config_hash`` fixture
covers ``config_hash``.
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
    KnowledgeAuditError,
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    ShapeContract,
    SqliteWriteQuarantine,
)
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.events import ensure_outbox_schema
from ne.runtime.factory import create_memory_promoter
from ne.runtime.memory_promotion import MemoryPromoter
from ne.runtime.memory_store import (
    MemoryEventKind,
    SqlAlchemyEpisodicStore,
    ensure_memory_schema,
)

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


class _SharedFlakySink:
    """Wraps a real ``PostgresAuditSink``; fails the FIRST ``fail_times`` appends
    whose ``event.action == fail_action`` and delegates everything else (and
    ``verify``). ONE instance is shared by the writer AND the promoter.

    Mirrors ``RecordingAuditSink``'s async signatures so pyright accepts it as an
    ``AuditSink``.
    """

    def __init__(
        self,
        delegate: PostgresAuditSink,
        *,
        fail_action: str = "knowledge.upsert",
        fail_times: int | None = None,
        fail_matches: frozenset[int] | None = None,
    ) -> None:
        self._delegate = delegate
        self._fail_action = fail_action
        self._fail_times = fail_times
        # 1-based match indices to fail EXACTLY (e.g. {2} fails only the 2nd
        # matching append — the partial-batch case where an EARLIER item already
        # audited). When set it takes precedence over the first-N ``fail_times``.
        self._fail_matches = fail_matches
        self._matched = 0

    def _should_fail(self, matched: int) -> bool:
        if self._fail_matches is not None:
            return matched in self._fail_matches
        if self._fail_times is None:
            return True
        return matched <= self._fail_times

    async def append(self, event: AuditEventLike) -> None:
        if getattr(event, "action", "") == self._fail_action:
            self._matched += 1
            if self._should_fail(self._matched):
                raise RuntimeError(f"audit sink is down for {self._fail_action}")
        await self._delegate.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return await self._delegate.verify(start=start, end=end)


class _SharedSinkStack:
    """The documented create_memory_promoter recipe with ONE sink shared by the
    writer AND the promoter."""

    def __init__(
        self,
        tmp_path,
        *,
        fail_times: int | None = None,
        fail_matches: frozenset[int] | None = None,
    ) -> None:
        client = AsyncQdrantClient(":memory:")
        self.store = QdrantChunkStore(
            client, f"kw_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
        )
        asyncio.run(self.store.ensure_collection())
        self.quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
        self.audit_engine = sa.create_engine("sqlite://")
        ensure_outbox_schema(self.audit_engine)
        ensure_audit_schema(self.audit_engine)
        # ONE sink instance, shared by writer AND promoter.
        self.sink = _SharedFlakySink(
            PostgresAuditSink(self.audit_engine),
            fail_action="knowledge.upsert",
            fail_times=fail_times,
            fail_matches=fail_matches,
        )
        self.writer = KnowledgeWriter(
            self.store,
            self.quarantine,
            {NOTE_CONTRACT.name: NOTE_CONTRACT},
            default_shape=NOTE_CONTRACT.name,
            actor="svc:memory-promotion",
            default_provenance_class=ProvenanceClass.LEARNED_BY_AGENT,
            audit_sink=self.sink,
            violation_sink=OutboxShapeViolationSink(self.audit_engine),
        )
        self.memory_engine = sa.create_engine("sqlite://")
        ensure_memory_schema(self.memory_engine)
        self.episodic = SqlAlchemyEpisodicStore(self.memory_engine)
        self.promoter: MemoryPromoter = create_memory_promoter(
            writer=self.writer, sink=self.sink, engine=self.memory_engine
        )

    async def append_learned_fact(self, content: str = _CONTENT) -> str:
        return await self.episodic.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content=content,
            occurred_at=datetime.now(UTC),
            session_id="s1",
        )

    def audit_rows(self) -> list[dict[str, object]]:
        with self.audit_engine.begin() as conn:
            rows = conn.execute(sa.select(ne_audit_log)).mappings().all()
        return [dict(r["payload"]) for r in rows]

    def rows_for(self, resource: str) -> list[dict[str, object]]:
        return [r for r in self.audit_rows() if r.get("resource") == resource]


def test_shared_sink_single_fault_leaves_no_retrievable_unaudited_chunk(tmp_path) -> None:
    stack = _SharedSinkStack(tmp_path, fail_times=1)

    async def scenario() -> None:
        event_id = await stack.append_learned_fact()
        item_id = f"learned-{event_id}"
        proposal_id = await stack.promoter.propose(agent_id="agent-a", event_ids=[event_id])
        # The writer-internal knowledge.upsert audit fails; the typed error
        # propagates OUT of promote() (the promoter did not swallow it).
        try:
            await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
            raise AssertionError("expected KnowledgeAuditError to propagate out of promote()")
        except KnowledgeAuditError:
            pass
        # NO durable+retrievable chunk without an audit record — the writer unwound.
        assert await stack.store.get([item_id]) == []
        # The full payload is parked (never lost).
        assert stack.quarantine.get(item_id) is not None
        # No approval row — the promotion is cleanly retryable.
        approved = await stack.episodic.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert approved == []
        rows = stack.rows_for(f"knowledge://{item_id}")
        # No success knowledge.upsert row, but the unwind's knowledge.quarantine row IS present.
        assert not any(
            r["action"] == "knowledge.upsert" and r["outcome"] == "success" for r in rows
        )
        assert any(r["action"] == "knowledge.quarantine" for r in rows)
        # No promoted row (the promoter never reached its own audit).
        assert not any(r["action"] == "memory.promotion.promoted" for r in stack.audit_rows())

    asyncio.run(scenario())


def test_shared_sink_recovery_retry_heals_idempotently(tmp_path) -> None:
    stack = _SharedSinkStack(tmp_path, fail_times=1)

    async def scenario() -> None:
        event_id = await stack.append_learned_fact()
        item_id = f"learned-{event_id}"
        proposal_id = await stack.promoter.propose(agent_id="agent-a", event_ids=[event_id])
        try:
            await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
            raise AssertionError("expected KnowledgeAuditError on the first attempt")
        except KnowledgeAuditError:
            pass
        # Retry after the shared sink recovers (fail budget spent).
        receipt = await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert receipt.accepted == (item_id,)
        got = await stack.store.get([item_id])
        assert len(got) == 1
        approved = await stack.episodic.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert len(approved) == 1
        success_upsert = [
            r
            for r in stack.rows_for(f"knowledge://{item_id}")
            if r["action"] == "knowledge.upsert" and r["outcome"] == "success"
        ]
        assert len(success_upsert) == 1
        assert any(r["action"] == "memory.promotion.promoted" for r in stack.audit_rows())
        # Re-upsert cleared the parked row — exactly one final audited chunk.
        assert stack.quarantine.get(item_id) is None

    asyncio.run(scenario())


def test_shared_sink_multi_event_partial_fault_leaves_no_retrievable_unaudited_chunk(
    tmp_path,
) -> None:
    """Codex P1: a MULTI-event promotion where a LATER item's knowledge.upsert audit
    fails while an EARLIER item is already store+audit complete (per-item writer
    atomicity). Without the promoter-side sweep the earlier chunk would stay
    retrievable with only its knowledge.upsert record — no memory.promotion.promoted
    audit, no approval row — violating the promotion integrity invariant (#6).
    fail_matches={2} fails EXACTLY the 2nd knowledge.upsert append.
    """
    stack = _SharedSinkStack(tmp_path, fail_matches=frozenset({2}))

    async def scenario() -> None:
        e1 = await stack.append_learned_fact("first learned fact: mise en place")
        e2 = await stack.append_learned_fact("second learned fact: rest the meat")
        id1, id2 = f"learned-{e1}", f"learned-{e2}"
        proposal_id = await stack.promoter.propose(agent_id="agent-a", event_ids=[e1, e2])

        try:
            await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
            raise AssertionError("expected KnowledgeAuditError to propagate out of promote()")
        except KnowledgeAuditError:
            pass

        # THE FIX: NEITHER item survives retrievable — the writer unwound id2 and the
        # promoter swept the already-audited survivor id1 back through quarantine.
        assert await stack.store.get([id1]) == []
        assert await stack.store.get([id2]) == []
        # Both payloads are parked (never lost).
        assert stack.quarantine.get(id1) is not None
        assert stack.quarantine.get(id2) is not None
        # No approval row — cleanly retryable.
        approved = await stack.episodic.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert approved == []
        # The promoter never reached its own promoted audit.
        assert not any(r["action"] == "memory.promotion.promoted" for r in stack.audit_rows())
        # id1's survivor was unwound via the governed quarantine seam (audited).
        assert any(
            r["action"] == "knowledge.quarantine" for r in stack.rows_for(f"knowledge://{id1}")
        )

        # Retry after recovery heals idempotently — both facts durable, promotion recorded.
        receipt = await stack.promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert set(receipt.accepted) == {id1, id2}
        assert len(await stack.store.get([id1])) == 1
        assert len(await stack.store.get([id2])) == 1
        approved = await stack.episodic.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert len(approved) == 1
        assert any(r["action"] == "memory.promotion.promoted" for r in stack.audit_rows())
        assert stack.quarantine.get(id1) is None
        assert stack.quarantine.get(id2) is None

    asyncio.run(scenario())
