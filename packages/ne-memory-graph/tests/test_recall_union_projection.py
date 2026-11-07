# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Graph-hit → MemoryEvent projection, union+dedupe with the keyword floor."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from ne.contracts.runtime import MemoryEvent
from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.memory_store import MemoryEventKind, SqlAlchemyEpisodicStore

from .conftest import T0, FakeSidecar


async def _seed(store: SqlAlchemyEpisodicStore, *, content: str, agent: str = "agent-a") -> str:
    return await store.append(agent_id=agent, kind="learned-fact", content=content, occurred_at=T0)


def _hit(event_id: str, *, content: str, valid_at: str, uuid: str, group: str = "agent-a") -> dict:
    return {
        "content": content,
        "valid_at": valid_at,
        "created_at": valid_at,
        "group_id": group,
        "event_ids": [event_id],
        "edge_uuid": uuid,
    }


def test_fact_projection_shape(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        event_id = await _seed(store, content="source episode")
        valid_at = (T0 + timedelta(hours=1)).isoformat()
        sidecar = FakeSidecar(
            hits=[_hit(event_id, content="ACME pays on time", valid_at=valid_at, uuid="edge-1")]
        )
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        results = await facade.recall("nonmatching-floor-query")
        facts = [e for e in results if e.provenance.startswith("memory-graph:")]
        assert len(facts) == 1
        fact = facts[0]
        assert fact.kind == MemoryEventKind.LEARNED_FACT
        assert fact.content == "ACME pays on time"
        assert fact.occurred_at.isoformat() == valid_at
        assert fact.provenance == f"memory-graph:edge=edge-1;episodes={event_id}"

    asyncio.run(scenario())


def test_union_with_floor_and_dedupe_by_edge_uuid(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        event_id = await _seed(store, content="alpha fact")
        valid_at = (T0 + timedelta(hours=2)).isoformat()
        # Two hits share edge_uuid → deduped to one fact.
        hits = [
            _hit(event_id, content="graph fact", valid_at=valid_at, uuid="edge-9"),
            _hit(event_id, content="graph fact dup", valid_at=valid_at, uuid="edge-9"),
        ]
        sidecar = FakeSidecar(hits=hits)
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        results = await facade.recall("alpha")  # floor matches the seeded event
        floor = [e for e in results if e.provenance == ""]
        facts = [e for e in results if e.provenance.startswith("memory-graph:")]
        assert [e.content for e in floor] == ["alpha fact"]  # floor present
        assert len(facts) == 1  # deduped

    asyncio.run(scenario())


def test_since_until_forwarded_and_newest_first(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        e1 = await _seed(store, content="s1")
        older = _hit(
            e1, content="older fact", valid_at=(T0 + timedelta(hours=1)).isoformat(), uuid="e-old"
        )
        newer = _hit(
            e1, content="newer fact", valid_at=(T0 + timedelta(hours=5)).isoformat(), uuid="e-new"
        )
        sidecar = FakeSidecar(hits=[older, newer])
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        since = T0 + timedelta(minutes=30)
        until = T0 + timedelta(hours=10)
        results = await facade.recall("zzz-no-floor", since=since, until=until)
        # since/until forwarded to the sidecar search body.
        body = sidecar.search_bodies[0]
        assert "since" in body and "until" in body
        facts = [e.content for e in results if e.provenance.startswith("memory-graph:")]
        assert facts == ["newer fact", "older fact"]  # newest-first

    asyncio.run(scenario())


def test_limit_truncates_but_never_drops_floor(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        # Five floor events matching the query.
        for i in range(5):
            await store.append(
                agent_id="agent-a",
                kind="learned-fact",
                content=f"match {i}",
                occurred_at=T0 + timedelta(minutes=i),
            )
        e_first = (await store.recall(agent_id="agent-a"))[0].event_id
        # A graph fact that is NEWER than every floor event.
        hit = _hit(
            e_first,
            content="newest graph fact",
            valid_at=(T0 + timedelta(hours=9)).isoformat(),
            uuid="edge-top",
        )
        sidecar = FakeSidecar(hits=[hit])
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        results = await facade.recall("match", limit=5)
        assert len(results) == 5
        # All five floor events are retained even though a newer graph fact exists.
        floor = [e for e in results if e.provenance == ""]
        assert len(floor) == 5
        # No room left for the graph fact (floor filled the limit).
        assert all(not e.provenance.startswith("memory-graph:") for e in results)

    asyncio.run(scenario())


def test_floor_only_when_sidecar_empty(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await _seed(store, content="lonely fact")
        sidecar = FakeSidecar(hits=[])
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        results = await facade.recall("lonely")
        assert [e.content for e in results] == ["lonely fact"]
        assert all(isinstance(e, MemoryEvent) for e in results)

    asyncio.run(scenario())
