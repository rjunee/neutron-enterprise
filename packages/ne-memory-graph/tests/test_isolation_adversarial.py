# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Adversarial cross-group isolation (R5): hostile sidecar hits are ALL dropped."""

from __future__ import annotations

import asyncio

from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.memory_store import SqlAlchemyEpisodicStore

from .conftest import T0, FakeSidecar


def test_cross_group_and_foreign_provenance_all_dropped(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        # A real event for the BOUND agent and one for ANOTHER agent.
        own = await store.append(
            agent_id="agent-a", kind="learned-fact", content="own fact", occurred_at=T0
        )
        other = await store.append(
            agent_id="agent-b", kind="learned-fact", content="other fact", occurred_at=T0
        )
        valid_at = T0.isoformat()
        hostile_hits = [
            # (1) wrong group_id — dropped even though it cites a real own event_id.
            {
                "content": "leak via group",
                "valid_at": valid_at,
                "created_at": valid_at,
                "group_id": "agent-b",
                "event_ids": [own],
                "edge_uuid": "edge-x",
            },
            # (2) unknown event_id — provenance unresolvable → dropped.
            {
                "content": "leak via phantom",
                "valid_at": valid_at,
                "created_at": valid_at,
                "group_id": "agent-a",
                "event_ids": ["does-not-exist"],
                "edge_uuid": "edge-y",
            },
            # (3) cross-agent event_id — store.get(agent-a, other) raises → dropped
            #     (existence of agent-b's event never leaks).
            {
                "content": "leak via cross-agent id",
                "valid_at": valid_at,
                "created_at": valid_at,
                "group_id": "agent-a",
                "event_ids": [other],
                "edge_uuid": "edge-z",
            },
            # (4) EMPTY event_ids — a fact with no backing episode has no
            #     provenance at all → dropped (never vacuously "resolvable").
            {
                "content": "hallucinated fact with no provenance",
                "valid_at": valid_at,
                "created_at": valid_at,
                "group_id": "agent-a",
                "event_ids": [],
                "edge_uuid": "edge-w",
            },
        ]
        sidecar = FakeSidecar(hits=hostile_hits)
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        results = await facade.recall("own")
        # Only the bound agent's own floor event survives; every hostile hit dropped.
        assert [e.content for e in results] == ["own fact"]
        assert all(not e.provenance.startswith("memory-graph:") for e in results)
        # Every search carried the bound-agent partition, never a wider scope.
        assert sidecar.search_bodies
        for body in sidecar.search_bodies:
            assert body["group_ids"] == ["agent-a"]

    asyncio.run(scenario())


def test_empty_event_ids_hit_is_dropped(store: SqlAlchemyEpisodicStore) -> None:
    """FIX-2 (provenance invariant): a graph hit whose ``event_ids`` is EMPTY —
    a fact with no backing NE-037 truth-log event — is DROPPED from recall.

    Isolated single-purpose regression so the ratchet is unambiguous: a sidecar
    bug that surfaced a provenance-free (potentially hallucinated) fact must
    never project into recall. Only the real floor event survives.
    """

    async def scenario() -> None:
        floor = await store.append(
            agent_id="agent-a", kind="learned-fact", content="real floor fact", occurred_at=T0
        )
        valid_at = T0.isoformat()
        sidecar = FakeSidecar(
            hits=[
                {
                    "content": "hallucinated fact with no provenance",
                    "valid_at": valid_at,
                    "created_at": valid_at,
                    "group_id": "agent-a",
                    "event_ids": [],
                    "edge_uuid": "edge-empty",
                }
            ]
        )
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        results = await facade.recall("fact")
        # Exactly the floor event, nothing graph-projected.
        assert [e.content for e in results] == ["real floor fact"]
        assert all(not e.provenance.startswith("memory-graph:") for e in results)
        assert floor  # the floor event id exists (sanity)

    asyncio.run(scenario())
