# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C5 swap suite: TemporalAgentMemory conforms to the FROZEN MemoryProvider.

Replicates the four scenarios of ne-runtime's frozen-Protocol contract test
against ``TemporalAgentMemory`` over a sqlite store with an UNREACHABLE sidecar —
degradation collapses to the keyword floor, i.e. identical v1 semantics — plus the
``_as_provider`` structural check and the ``recall(limit=0) == []`` bug-fix pin.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from ne.contracts.runtime import MemoryEvent, MemoryProvider
from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.memory_store import SqlAlchemyEpisodicStore

from .conftest import T0, FakeSidecar


def _facade(store: SqlAlchemyEpisodicStore, agent_id: str = "agent-a") -> TemporalAgentMemory:
    # Unreachable sidecar: every ingest/search raises and is swallowed → keyword floor.
    client = FakeSidecar(raise_connect=True).client()
    return TemporalAgentMemory(store, agent_id, client=client)


def _as_provider(p: MemoryProvider) -> MemoryProvider:
    return p


def test_temporal_memory_satisfies_frozen_memory_provider(
    store: SqlAlchemyEpisodicStore,
) -> None:
    provider = _as_provider(_facade(store))
    assert isinstance(provider, TemporalAgentMemory)


def test_protocol_append_stamps_bound_agent_and_session(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        client = FakeSidecar(raise_connect=True).client()
        facade = TemporalAgentMemory(store, "agent-a", session_id="s7", client=client)
        await facade.append(
            MemoryEvent(
                kind="learned-fact",
                content="the sky is blue",
                occurred_at=T0,
                provenance="obs",
            )
        )
        rows = await store.recall(agent_id="agent-a")
        assert len(rows) == 1
        assert rows[0].session_id == "s7"
        event = rows[0].as_memory_event()
        assert event.kind == "learned-fact"
        assert event.content == "the sky is blue"
        assert event.occurred_at == T0
        assert event.provenance == "obs"

    asyncio.run(scenario())


def test_protocol_recall_query_semantics(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        facade = _facade(store)
        await facade.append(
            MemoryEvent(kind="decision", content="Chose Plan Alpha", occurred_at=T0)
        )
        await facade.append(
            MemoryEvent(
                kind="learned-fact",
                content="Beta is faster",
                occurred_at=T0 + timedelta(minutes=1),
            )
        )
        alpha = await facade.recall("plan alpha")
        assert [e.content for e in alpha] == ["Chose Plan Alpha"]
        facts = await facade.recall("learned-fact")
        assert [e.content for e in facts] == ["Beta is faster"]
        all_events = await facade.recall("")
        assert [e.content for e in all_events] == ["Beta is faster", "Chose Plan Alpha"]
        assert await facade.recall("nonexistent") == []

    asyncio.run(scenario())


def test_protocol_recall_window_and_limit(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        facade = _facade(store)
        for i in range(25):
            await facade.append(
                MemoryEvent(
                    kind="outcome",
                    content=f"o{i}",
                    occurred_at=T0 + timedelta(minutes=i),
                )
            )
        default = await facade.recall("")
        assert len(default) == 20
        assert default[0].content == "o24"
        windowed = await facade.recall(
            "", since=T0 + timedelta(minutes=23), until=T0 + timedelta(minutes=24)
        )
        assert {e.content for e in windowed} == {"o23", "o24"}

    asyncio.run(scenario())


def test_recall_limit_zero_returns_empty(store: SqlAlchemyEpisodicStore) -> None:
    """The deliberate v1 off-by-one FIX: recall(limit=0) == [] (not "all")."""

    async def scenario() -> None:
        facade = _facade(store)
        await facade.append(MemoryEvent(kind="outcome", content="only", occurred_at=T0))
        assert await facade.recall("", limit=0) == []

    asyncio.run(scenario())
