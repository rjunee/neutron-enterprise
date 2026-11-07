# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""append = episodic INSERT + queued episode; sidecar failure never fails append."""

from __future__ import annotations

import asyncio

import pytest

from ne.contracts.runtime import MemoryEvent
from ne.memory_graph.backfill import backfill, catch_up
from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.memory_store import SqlAlchemyEpisodicStore

from .conftest import T0, FakeSidecar


def test_append_writes_row_and_posts_episode_named_event_id(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        sidecar = FakeSidecar()
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        await facade.append(MemoryEvent(kind="learned-fact", content="fact one", occurred_at=T0))
        rows = await store.recall(agent_id="agent-a")
        assert len(rows) == 1
        assert len(sidecar.posted) == 1
        assert sidecar.posted[0]["name"] == rows[0].event_id
        assert sidecar.posted[0]["group_id"] == "agent-a"
        assert sidecar.posted[0]["content"] == "fact one"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "sidecar",
    [
        FakeSidecar(episode_status=500),
        FakeSidecar(raise_timeout=True),
        FakeSidecar(raise_connect=True),
    ],
)
def test_append_survives_sidecar_failure(
    store: SqlAlchemyEpisodicStore, sidecar: FakeSidecar
) -> None:
    async def scenario() -> None:
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        await facade.append(MemoryEvent(kind="decision", content="durable", occurred_at=T0))
        rows = await store.recall(agent_id="agent-a")
        assert [r.content for r in rows] == ["durable"]

    asyncio.run(scenario())


def test_catch_up_replays_only_the_watermark_gap(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        sidecar = FakeSidecar()
        client = sidecar.client()
        # Two pre-existing episodic events (written directly to the log).
        for i in range(2):
            await store.append(
                agent_id="agent-a",
                kind="outcome",
                content=f"o{i}",
                occurred_at=T0,
            )
        first = await catch_up(store, client, store.engine, agent_ids=["agent-a"])
        assert first.require("agent-a").replayed == 2
        # No new events → the next catch-up replays nothing (watermark holds).
        again = await catch_up(store, client, store.engine, agent_ids=["agent-a"])
        assert again.require("agent-a").replayed == 0
        assert again.require("agent-a").skipped == 2
        # One more event → only the gap is replayed.
        await store.append(agent_id="agent-a", kind="outcome", content="o2", occurred_at=T0)
        third = await catch_up(store, client, store.engine, agent_ids=["agent-a"])
        assert third.require("agent-a").replayed == 1

    asyncio.run(scenario())


def test_backfill_double_post_is_idempotent_tolerated(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        sidecar = FakeSidecar()
        client = sidecar.client()
        await store.append(agent_id="agent-a", kind="outcome", content="o0", occurred_at=T0)
        r1 = await backfill(store, client, agent_ids=["agent-a"])
        r2 = await backfill(store, client, agent_ids=["agent-a"])
        assert r1.require("agent-a").replayed == 1
        # Re-running backfill re-posts (idempotent by episode name; tolerated).
        assert r2.require("agent-a").replayed == 1
        assert len(sidecar.posted) == 2

    asyncio.run(scenario())
