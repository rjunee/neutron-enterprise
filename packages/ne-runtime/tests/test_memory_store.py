# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemyEpisodicStore — append-only episodic memory, SQLite/Postgres parity."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import MemoryEventNotFoundError
from ne.runtime.memory_store import (
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


def test_append_recall_roundtrip(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        event_id = await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.DECISION,
            content="chose plan A",
            occurred_at=T0,
            provenance="obs",
            session_id="s1",
        )
        rows = await store.recall(agent_id="agent-a")
        assert len(rows) == 1
        row = rows[0]
        assert row.event_id == event_id
        assert row.agent_id == "agent-a"
        assert row.session_id == "s1"
        assert row.kind == "decision"
        assert row.content == "chose plan A"
        assert row.provenance == "obs"

    asyncio.run(scenario())


def test_recall_orders_newest_first_and_limits(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        for i in range(3):
            await store.append(
                agent_id="agent-a",
                kind=MemoryEventKind.OUTCOME,
                content=f"e{i}",
                occurred_at=T0 + timedelta(minutes=i),
            )
        newest2 = await store.recall(agent_id="agent-a", limit=2)
        assert [r.content for r in newest2] == ["e2", "e1"]
        all_rows = await store.recall(agent_id="agent-a", limit=None)
        assert [r.content for r in all_rows] == ["e2", "e1", "e0"]

    asyncio.run(scenario())


def test_time_window_bounds_are_inclusive(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        t1, t2, t3 = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)
        for t, c in ((t1, "c1"), (t2, "c2"), (t3, "c3")):
            await store.append(
                agent_id="agent-a", kind=MemoryEventKind.OUTCOME, content=c, occurred_at=t
            )
        since_t2 = {r.content for r in await store.recall(agent_id="agent-a", since=t2)}
        assert since_t2 == {"c2", "c3"}
        until_t2 = {r.content for r in await store.recall(agent_id="agent-a", until=t2)}
        assert until_t2 == {"c1", "c2"}
        exact = {r.content for r in await store.recall(agent_id="agent-a", since=t2, until=t2)}
        assert exact == {"c2"}

    asyncio.run(scenario())


def test_kind_filter(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await store.append(
            agent_id="agent-a", kind=MemoryEventKind.DECISION, content="d", occurred_at=T0
        )
        await store.append(
            agent_id="agent-a", kind=MemoryEventKind.OUTCOME, content="o", occurred_at=T0
        )
        await store.append(
            agent_id="agent-a",
            kind=MemoryEventKind.LEARNED_FACT,
            content="l",
            occurred_at=T0,
        )
        one = await store.recall(agent_id="agent-a", kinds=[MemoryEventKind.DECISION])
        assert {r.content for r in one} == {"d"}
        two = await store.recall(
            agent_id="agent-a",
            kinds=[MemoryEventKind.DECISION, MemoryEventKind.LEARNED_FACT],
        )
        assert {r.content for r in two} == {"d", "l"}

    asyncio.run(scenario())


def test_recalled_timestamps_are_utc_aware(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await store.append(
            agent_id="agent-a", kind=MemoryEventKind.DECISION, content="x", occurred_at=T0
        )
        row = (await store.recall(agent_id="agent-a"))[0]
        assert row.occurred_at.tzinfo is not None
        assert row.recorded_at.tzinfo is not None
        assert row.occurred_at == T0

    asyncio.run(scenario())


def test_rejects_naive_datetimes(store: SqlAlchemyEpisodicStore) -> None:
    naive = datetime(2026, 7, 6, 12, 0)  # intentionally tz-naive

    async def scenario() -> None:
        with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
            await store.append(
                agent_id="agent-a",
                kind=MemoryEventKind.DECISION,
                content="x",
                occurred_at=naive,
            )
        await store.append(
            agent_id="agent-a", kind=MemoryEventKind.DECISION, content="x", occurred_at=T0
        )
        with pytest.raises(ValueError, match="since must be timezone-aware"):
            await store.recall(agent_id="agent-a", since=naive)
        with pytest.raises(ValueError, match="until must be timezone-aware"):
            await store.recall(agent_id="agent-a", until=naive)

    asyncio.run(scenario())


def test_rejects_empty_required_fields(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="agent_id"):
            await store.append(agent_id="  ", kind="decision", content="x", occurred_at=T0)
        with pytest.raises(ValueError, match="kind"):
            await store.append(agent_id="agent-a", kind="", content="x", occurred_at=T0)
        with pytest.raises(ValueError, match="content"):
            await store.append(agent_id="agent-a", kind="decision", content="   ", occurred_at=T0)

    asyncio.run(scenario())


def test_store_surface_is_append_only(store: SqlAlchemyEpisodicStore) -> None:
    for name in ("update", "delete", "remove", "purge", "edit", "clear"):
        assert not hasattr(store, name)

    async def scenario() -> None:
        await store.append(
            agent_id="agent-a", kind=MemoryEventKind.DECISION, content="x", occurred_at=T0
        )
        first = await store.recall(agent_id="agent-a")
        second = await store.recall(agent_id="agent-a")
        assert [r.model_dump() for r in first] == [r.model_dump() for r in second]

    asyncio.run(scenario())


def test_get_own_event_and_missing_raises(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        event_id = await store.append(
            agent_id="agent-a", kind=MemoryEventKind.DECISION, content="x", occurred_at=T0
        )
        got = await store.get(agent_id="agent-a", event_id=event_id)
        assert got.event_id == event_id
        with pytest.raises(MemoryEventNotFoundError):
            await store.get(agent_id="agent-a", event_id="does-not-exist")

    asyncio.run(scenario())


def test_from_settings_requires_url() -> None:
    with pytest.raises(ValueError, match="session_database_url is empty"):
        SqlAlchemyEpisodicStore.from_settings(RuntimeSettings(session_database_url=""))


def test_ensure_memory_schema_idempotent() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_memory_schema(engine)
    ensure_memory_schema(engine)  # second call must not raise
    engine.dispose()
