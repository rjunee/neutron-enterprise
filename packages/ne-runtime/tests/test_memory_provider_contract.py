# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AgentMemory conforms to the FROZEN ne.contracts.runtime.MemoryProvider Protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.contracts.runtime import MemoryEvent, MemoryProvider
from ne.runtime.memory_store import (
    AgentMemory,
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


def _as_provider(p: MemoryProvider) -> MemoryProvider:
    """Typed identity — pyright enforces structural conformance at the call site."""
    return p


def test_agent_memory_satisfies_frozen_memory_provider(
    store: SqlAlchemyEpisodicStore,
) -> None:
    provider = _as_provider(AgentMemory(store, "agent-a"))
    assert isinstance(provider, AgentMemory)


def test_protocol_append_stamps_bound_agent_and_session(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        facade = AgentMemory(store, "agent-a", session_id="s7")
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
        assert isinstance(event, MemoryEvent)
        assert event.kind == "learned-fact"
        assert event.content == "the sky is blue"
        assert event.occurred_at == T0
        assert event.provenance == "obs"

    asyncio.run(scenario())


def test_protocol_recall_query_semantics(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        facade = AgentMemory(store, "agent-a")
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
        # case-insensitive content substring
        alpha = await facade.recall("plan alpha")
        assert [e.content for e in alpha] == ["Chose Plan Alpha"]
        # query equal to a kind matches
        facts = await facade.recall("learned-fact")
        assert [e.content for e in facts] == ["Beta is faster"]
        # empty query returns all (newest-first)
        all_events = await facade.recall("")
        assert [e.content for e in all_events] == ["Beta is faster", "Chose Plan Alpha"]
        # non-matching query returns nothing
        assert await facade.recall("nonexistent") == []

    asyncio.run(scenario())


def test_protocol_recall_window_and_limit(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        facade = AgentMemory(store, "agent-a")
        for i in range(25):
            await facade.append(
                MemoryEvent(
                    kind="outcome",
                    content=f"o{i}",
                    occurred_at=T0 + timedelta(minutes=i),
                )
            )
        # default limit=20 enforced when 25 match
        default = await facade.recall("")
        assert len(default) == 20
        assert all(isinstance(e, MemoryEvent) for e in default)
        # newest-first
        assert default[0].content == "o24"
        # window honored
        windowed = await facade.recall(
            "", since=T0 + timedelta(minutes=23), until=T0 + timedelta(minutes=24)
        )
        assert {e.content for e in windowed} == {"o23", "o24"}

    asyncio.run(scenario())
