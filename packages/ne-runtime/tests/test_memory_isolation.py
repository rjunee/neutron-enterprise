# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Per-agent memory isolation (NE-034-aligned) + explicit shared_with sharing."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.runtime.errors import MemoryEventNotFoundError
from ne.runtime.memory_store import (
    AgentMemory,
    MemoryEventKind,
    SqlAlchemyEpisodicStore,
    ensure_memory_schema,
)

T0 = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[SqlAlchemyEpisodicStore]:
    engine = sa.create_engine("sqlite://")
    ensure_memory_schema(engine)
    try:
        yield SqlAlchemyEpisodicStore(engine)
    finally:
        engine.dispose()


def test_agent_cannot_recall_another_agents_events(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        for i in range(3):
            await store.append(
                agent_id="agent-a",
                kind=MemoryEventKind.DECISION,
                content=f"secret-{i}",
                occurred_at=T0 + timedelta(minutes=i),
            )
        assert await store.recall(agent_id="agent-b") == []

    asyncio.run(scenario())


def test_get_cross_agent_raises_not_found(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        event_id = await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.DECISION,
            content="secret",
            occurred_at=T0,
        )
        with pytest.raises(MemoryEventNotFoundError):
            await store.get(agent_id="agent-b", event_id=event_id)

    asyncio.run(scenario())


def test_explicit_share_grants_recall_shared(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content="shared fact",
            occurred_at=T0,
            shared_with=("agent-b",),
        )
        got = await store.recall_shared(owner_agent_id="agent-a", requesting_agent_id="agent-b")
        assert [r.content for r in got] == ["shared fact"]
        none = await store.recall_shared(owner_agent_id="agent-a", requesting_agent_id="agent-c")
        assert none == []

    asyncio.run(scenario())


def test_unshared_events_never_appear_in_recall_shared(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content="private",
            occurred_at=T0,
        )
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content="public-to-b",
            occurred_at=T0 + timedelta(minutes=1),
            shared_with=("agent-b",),
        )
        got = await store.recall_shared(owner_agent_id="agent-a", requesting_agent_id="agent-b")
        assert [r.content for r in got] == ["public-to-b"]

    asyncio.run(scenario())


def test_recall_shared_respects_window_kind_and_limit(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        t1, t2, t3 = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.DECISION,
            content="d1",
            occurred_at=t1,
            shared_with=("agent-b",),
        )
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content="l2",
            occurred_at=t2,
            shared_with=("agent-b",),
        )
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content="l3",
            occurred_at=t3,
            shared_with=("agent-b",),
        )
        by_kind = await store.recall_shared(
            owner_agent_id="agent-a",
            requesting_agent_id="agent-b",
            kinds=[MemoryEventKind.LEARNED_FACT],
        )
        assert {r.content for r in by_kind} == {"l2", "l3"}
        windowed = await store.recall_shared(
            owner_agent_id="agent-a", requesting_agent_id="agent-b", since=t2
        )
        assert {r.content for r in windowed} == {"l2", "l3"}
        limited = await store.recall_shared(
            owner_agent_id="agent-a", requesting_agent_id="agent-b", limit=1
        )
        assert len(limited) == 1
        assert limited[0].content == "l3"  # newest first

    asyncio.run(scenario())


def test_facade_bound_to_its_agent(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await store.append(
            agent_id="agent-b",
            kind=MemoryEventKind.DECISION,
            content="b-secret",
            occurred_at=T0,
        )
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.DECISION,
            content="a-owned",
            occurred_at=T0,
        )
        facade = AgentMemory(store, "agent-a")
        events = await facade.recall("")
        contents = {e.content for e in events}
        assert "a-owned" in contents
        assert "b-secret" not in contents

    asyncio.run(scenario())
