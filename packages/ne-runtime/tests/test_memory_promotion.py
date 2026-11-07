# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Curation-gated learned-fact → brain promotion (local frozen-Protocol doubles)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.contracts.knowledge import KnowledgeItem, UpsertReceipt
from ne.runtime.errors import (
    MemoryEventNotFoundError,
    MemoryPromotionAuditError,
    MemoryPromotionError,
)
from ne.runtime.memory_promotion import (
    LEARNED_BY_AGENT_PROVENANCE,
    MemoryPromoter,
)
from ne.runtime.memory_store import (
    MemoryEventKind,
    SqlAlchemyEpisodicStore,
    ensure_memory_schema,
)

from .conftest import (
    AppendCountFailingSink,
    FailingAuditSink,
    FailOnceAuditSink,
    RecordingAuditSink,
)

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


# --- local frozen-KnowledgeWrite Protocol doubles (ne-brain never imported) ---


class RecordingKnowledgeWrite:
    """Records upsert batches; accepts everything unless quarantine ids configured."""

    def __init__(self, quarantined: tuple[str, ...] = ()) -> None:
        self.batches: list[list[KnowledgeItem]] = []
        self.quarantines: list[tuple[str, str]] = []
        self._quarantined = quarantined

    async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt:
        batch = list(items)
        self.batches.append(batch)
        accepted = tuple(i.item_id for i in batch if i.item_id not in self._quarantined)
        return UpsertReceipt(accepted=accepted, quarantined=self._quarantined)

    async def curate(self, item_id: str, patch: object, *, provenance: str) -> None:
        raise AssertionError("promotion never curates")

    async def quarantine(self, item_id: str, reason: str) -> None:
        # quarantine is the sanctioned audit-failure UNWIND path (Checkpoint-D #6).
        self.quarantines.append((item_id, reason))


class FailingKnowledgeWrite:
    """An upsert that always fails — durability-ordering test double."""

    async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt:
        raise RuntimeError("brain is down")

    async def curate(self, item_id: str, patch: object, *, provenance: str) -> None:
        raise AssertionError("promotion must only use upsert")

    async def quarantine(self, item_id: str, reason: str) -> None:
        raise AssertionError("promotion must only use upsert")


@pytest.fixture
def store() -> Iterator[SqlAlchemyEpisodicStore]:
    engine = sa.create_engine("sqlite://")
    ensure_memory_schema(engine)
    try:
        yield SqlAlchemyEpisodicStore(engine)
    finally:
        engine.dispose()


async def _append_learned_fact(
    store: SqlAlchemyEpisodicStore, *, agent_id: str = "agent-a", content: str = "fact"
) -> str:
    return await store.append(
        agent_id=agent_id,
        kind=MemoryEventKind.LEARNED_FACT,
        content=content,
        occurred_at=T0,
        session_id="s1",
    )


def test_propose_requires_learned_fact_kind(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        promoter = MemoryPromoter(store, writer, sink=recording_sink)
        event_id = await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.DECISION,
            content="chose",
            occurred_at=T0,
        )
        with pytest.raises(MemoryPromotionError):
            await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        proposed = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_PROPOSED]
        )
        assert proposed == []
        assert recording_sink.with_action("memory.promotion.proposed") == []
        assert writer.batches == []

    asyncio.run(scenario())


def test_propose_rejects_foreign_and_unknown_events(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        promoter = MemoryPromoter(store, RecordingKnowledgeWrite(), sink=recording_sink)
        b_event = await _append_learned_fact(store, agent_id="agent-b")
        with pytest.raises(MemoryEventNotFoundError):
            await promoter.propose(agent_id="agent-a", event_ids=[b_event])
        with pytest.raises(MemoryEventNotFoundError):
            await promoter.propose(agent_id="agent-a", event_ids=["random-id"])

    asyncio.run(scenario())


def test_propose_never_touches_the_writer(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        promoter = MemoryPromoter(store, writer, sink=recording_sink)
        event_id = await _append_learned_fact(store)
        await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        assert writer.batches == []

    asyncio.run(scenario())


def test_propose_appends_event_and_audits_with_config_hash(
    store: SqlAlchemyEpisodicStore,
    recording_sink: RecordingAuditSink,
    frozen_config_hash: str,
) -> None:
    async def scenario() -> None:
        promoter = MemoryPromoter(store, RecordingKnowledgeWrite(), sink=recording_sink)
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        proposed = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_PROPOSED]
        )
        assert len(proposed) == 1
        payload = json.loads(proposed[0].content)
        assert payload["proposal_id"] == proposal_id
        assert payload["event_ids"] == [event_id]
        events = recording_sink.with_action("memory.promotion.proposed")
        assert len(events) == 1
        assert events[0].actor == "agent-a"
        assert events[0].config_hash == frozen_config_hash

    asyncio.run(scenario())


def test_promote_requires_distinct_named_curator(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        promoter = MemoryPromoter(store, writer, sink=recording_sink)
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        # "agent-a " and " agent-a" must NOT bypass the self-approval gate via
        # surrounding whitespace — the approver is canonicalized before comparison.
        for approver in ("agent-a", "", "  ", "agent-a ", " agent-a", "\tagent-a\n"):
            with pytest.raises(MemoryPromotionError):
                await promoter.promote(proposal_id, agent_id="agent-a", approved_by=approver)
        assert writer.batches == []

    asyncio.run(scenario())


def test_promote_upserts_learned_by_agent_items(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        promoter = MemoryPromoter(store, writer, sink=recording_sink)
        event_id = await _append_learned_fact(store, content="durable learning")
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        receipt = await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert len(writer.batches) == 1
        (item,) = writer.batches[0]
        assert item.provenance == LEARNED_BY_AGENT_PROVENANCE
        assert item.item_id == f"learned-{event_id}"
        assert ":" not in item.item_id
        assert item.metadata["agent_id"] == "agent-a"
        assert item.metadata["source_event_id"] == event_id
        assert item.metadata["proposal_id"] == proposal_id
        assert item.metadata["approved_by"] == "carol"
        assert "occurred_at" in item.metadata
        assert receipt.accepted == (item.item_id,)

    asyncio.run(scenario())


def test_promote_appends_approved_event_and_audits(
    store: SqlAlchemyEpisodicStore,
    recording_sink: RecordingAuditSink,
    frozen_config_hash: str,
) -> None:
    async def scenario() -> None:
        promoter = MemoryPromoter(store, RecordingKnowledgeWrite(), sink=recording_sink)
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        approved = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert len(approved) == 1
        payload = json.loads(approved[0].content)
        assert payload["accepted"] == [f"learned-{event_id}"]
        events = recording_sink.with_action("memory.promotion.promoted")
        assert len(events) == 1
        assert events[0].actor == "carol"
        assert events[0].config_hash == frozen_config_hash

    asyncio.run(scenario())


def test_promote_same_proposal_twice_raises(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        promoter = MemoryPromoter(store, writer, sink=recording_sink)
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        with pytest.raises(MemoryPromotionError, match="already promoted"):
            await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert len(writer.batches) == 1

    asyncio.run(scenario())


def test_promote_unknown_proposal_raises(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        promoter = MemoryPromoter(store, RecordingKnowledgeWrite(), sink=recording_sink)
        with pytest.raises(MemoryEventNotFoundError):
            await promoter.promote("no-such", agent_id="agent-a", approved_by="carol")

    asyncio.run(scenario())


def test_writer_failure_leaves_no_approved_record(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        promoter = MemoryPromoter(store, FailingKnowledgeWrite(), sink=recording_sink)
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        with pytest.raises(RuntimeError, match="brain is down"):
            await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        approved = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert approved == []
        assert recording_sink.with_action("memory.promotion.promoted") == []

    asyncio.run(scenario())


def test_propose_audit_failure_leaves_no_proposal_row(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        promoter = MemoryPromoter(store, RecordingKnowledgeWrite(), sink=FailingAuditSink())
        event_id = await _append_learned_fact(store)
        with pytest.raises(RuntimeError, match="audit sink is down"):
            await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        # Audit-first: the failed audit means NO proposal row was ever written.
        proposed = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_PROPOSED]
        )
        assert proposed == []

    asyncio.run(scenario())


def test_promote_audit_failure_unwinds_and_leaves_no_approved_row(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        # propose's audit event succeeds; only the promoted-audit append fails.
        promoter = MemoryPromoter(
            store, writer, sink=AppendCountFailingSink("memory.promotion.promoted")
        )
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        with pytest.raises(MemoryPromotionAuditError):
            await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        # Every accepted chunk was unwound through the governed quarantine seam.
        assert len(writer.quarantines) == 1
        unwound_id, reason = writer.quarantines[0]
        assert unwound_id == f"learned-{event_id}"
        assert proposal_id in reason
        # No approval row, no promoted audit event — the promotion is retryable.
        approved = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert approved == []

    asyncio.run(scenario())


def test_promote_retry_after_audit_outage_reemits_and_succeeds(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        writer = RecordingKnowledgeWrite()
        sink = FailOnceAuditSink("memory.promotion.promoted")
        promoter = MemoryPromoter(store, writer, sink=sink)
        event_id = await _append_learned_fact(store)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        # First promote: the promoted-audit append fails once, unwinds, raises.
        with pytest.raises(MemoryPromotionAuditError):
            await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        # The _already_promoted guard is NOT armed (approval row written last) — the
        # SECOND promote of the same proposal succeeds and re-emits the audit.
        await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert len(writer.batches) == 2
        approved = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        assert len(approved) == 1
        assert len(sink.with_action("memory.promotion.promoted")) == 1

    asyncio.run(scenario())


def test_quarantined_receipt_recorded_not_hidden(
    store: SqlAlchemyEpisodicStore, recording_sink: RecordingAuditSink
) -> None:
    async def scenario() -> None:
        event_id = await _append_learned_fact(store)
        writer = RecordingKnowledgeWrite(quarantined=(f"learned-{event_id}",))
        promoter = MemoryPromoter(store, writer, sink=recording_sink)
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        receipt = await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert receipt.quarantined == (f"learned-{event_id}",)
        approved = await store.recall(
            agent_id="agent-a", kinds=[MemoryEventKind.PROMOTION_APPROVED]
        )
        payload = json.loads(approved[0].content)
        assert payload["quarantined"] == [f"learned-{event_id}"]
        promoted = [
            e
            for e in recording_sink.events
            if getattr(e, "action", "") == "memory.promotion.promoted"
        ]
        detail = json.loads(promoted[0].detail)
        assert detail["quarantined"] == [f"learned-{event_id}"]

    asyncio.run(scenario())


def test_factories_wire_end_to_end(recording_sink: RecordingAuditSink) -> None:
    from ne.runtime.config import RuntimeSettings
    from ne.runtime.factory import create_agent_memory, create_memory_promoter

    async def scenario() -> None:
        engine = sa.create_engine("sqlite://")
        ensure_memory_schema(engine)
        memory = create_agent_memory("agent-a", engine=engine, session_id="s1")
        writer = RecordingKnowledgeWrite()
        promoter = create_memory_promoter(writer=writer, sink=recording_sink, engine=engine)
        from ne.contracts.runtime import MemoryEvent

        await memory.append(MemoryEvent(kind="learned-fact", content="e2e fact", occurred_at=T0))
        # find the appended event id via the shared store
        store = SqlAlchemyEpisodicStore(engine)
        rows = await store.recall(agent_id="agent-a", kinds=[MemoryEventKind.LEARNED_FACT])
        event_id = rows[0].event_id
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        receipt = await promoter.promote(proposal_id, agent_id="agent-a", approved_by="carol")
        assert receipt.accepted == (f"learned-{event_id}",)
        engine.dispose()

    asyncio.run(scenario())

    # factories fail loud on missing engine + empty URL
    empty = RuntimeSettings(session_database_url="")
    with pytest.raises(ValueError, match="create_agent_memory needs an engine"):
        create_agent_memory("agent-a", settings=empty)
    with pytest.raises(ValueError, match="create_memory_promoter needs an engine"):
        create_memory_promoter(
            writer=RecordingKnowledgeWrite(), sink=recording_sink, settings=empty
        )
