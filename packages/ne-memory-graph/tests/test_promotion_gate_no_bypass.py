# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Promotion-through-curation-gate: NO KnowledgeWriter reference; log-only flow."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from ne.contracts.events import AuditEventLike
from ne.contracts.knowledge import KnowledgeItem, UpsertReceipt
from ne.contracts.runtime import MemoryEvent
from ne.memory_graph import facade as facade_module
from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.memory_promotion import MemoryPromoter
from ne.runtime.memory_store import SqlAlchemyEpisodicStore

from .conftest import T0, FakeSidecar

_FORBIDDEN_TOKENS = ("KnowledgeWrite", "KnowledgeWriter", "ne.brain.write")


def _package_sources() -> list[Path]:
    src_root = Path(facade_module.__file__).resolve().parent
    graphiti_env = src_root.parents[2] / "graphiti_env"
    files = list(src_root.rglob("*.py"))
    files += list(graphiti_env.rglob("*.py"))
    return files


def test_no_knowledge_writer_reference_anywhere_in_package() -> None:
    offenders: list[str] = []
    for path in _package_sources():
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path}: {token}")
    assert offenders == [], offenders


class _RecordingWriter:
    """Local frozen-KnowledgeWrite double (ne-brain never imported)."""

    def __init__(self) -> None:
        self.batches: list[list[KnowledgeItem]] = []

    async def upsert(self, items: Sequence[KnowledgeItem]) -> UpsertReceipt:
        batch = list(items)
        self.batches.append(batch)
        return UpsertReceipt(accepted=tuple(i.item_id for i in batch), quarantined=())

    async def curate(self, item_id: str, patch: object, *, provenance: str) -> None:
        raise AssertionError("promotion never curates")

    async def quarantine(self, item_id: str, reason: str) -> None:
        raise AssertionError("promotion never quarantines here")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEventLike] = []

    async def append(self, event: AuditEventLike) -> None:
        self.events.append(event)

    async def verify(self, *, start: datetime | None = None, end: datetime | None = None) -> object:
        raise NotImplementedError


def test_synthesized_fact_promotes_only_through_unchanged_promoter(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        # A graph-synthesized fact enters the SAME audited pipeline: it is first
        # appended as a learned-fact episodic event via the facade, then promoted
        # through the UNCHANGED MemoryPromoter propose→promote. Promotion reads the
        # LOG (store.get), never the graph.
        facade = TemporalAgentMemory(store, "agent-a", client=FakeSidecar().client())
        await facade.append(
            MemoryEvent(
                kind="learned-fact",
                content="synthesized: ACME approves invoices under $2k",
                occurred_at=T0,
                provenance="memory-graph:edge=e1;episodes=src",
            )
        )
        event_id = (await store.recall(agent_id="agent-a"))[0].event_id

        writer = _RecordingWriter()
        sink = _RecordingSink()
        promoter = MemoryPromoter(store, writer, sink=sink)  # type: ignore[arg-type]
        proposal_id = await promoter.propose(agent_id="agent-a", event_ids=[event_id])
        await promoter.promote(proposal_id, agent_id="agent-a", approved_by="curator-1")

        assert len(writer.batches) == 1
        assert writer.batches[0][0].content == "synthesized: ACME approves invoices under $2k"

    asyncio.run(scenario())
