# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shadow-read no-loss (migration-correctness criterion b) vs. the NE-037 log."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from ne.memory_graph.facade import TemporalAgentMemory
from ne.memory_graph.shadow import shadow_read
from ne.runtime.memory_store import AgentMemory, SqlAlchemyEpisodicStore

from .conftest import T0, FakeSidecar


async def _seed_corpus(store: SqlAlchemyEpisodicStore) -> str:
    last = ""
    for i in range(6):
        last = await store.append(
            agent_id="agent-a",
            kind="learned-fact",
            content=f"vendor note {i}",
            occurred_at=T0 + timedelta(minutes=i),
        )
    return last


def _check_no_loss(store: SqlAlchemyEpisodicStore, sidecar: FakeSidecar) -> None:
    async def scenario() -> None:
        await _seed_corpus(store)
        v1 = AgentMemory(store, "agent-a")
        facade = TemporalAgentMemory(store, "agent-a", client=sidecar.client())
        report = await shadow_read(facade, v1, "vendor")
        assert report.lost == ()
        assert report.no_loss
        assert report.v1_count == report.graph_path_count - len(report.added)

    asyncio.run(scenario())


def test_no_loss_sidecar_empty(store: SqlAlchemyEpisodicStore) -> None:
    _check_no_loss(store, FakeSidecar(hits=[]))


def test_no_loss_sidecar_down(store: SqlAlchemyEpisodicStore) -> None:
    _check_no_loss(store, FakeSidecar(raise_connect=True))


def test_no_loss_sidecar_junk_unresolvable(store: SqlAlchemyEpisodicStore) -> None:
    junk = [
        {
            "content": "phantom fact",
            "valid_at": T0.isoformat(),
            "created_at": T0.isoformat(),
            "group_id": "agent-a",
            "event_ids": ["phantom-id"],
            "edge_uuid": "edge-junk",
        }
    ]
    _check_no_loss(store, FakeSidecar(hits=junk))
