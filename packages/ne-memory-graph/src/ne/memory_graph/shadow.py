# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Shadow-read burn-in comparator (migration-correctness criterion b).

Runs the v1 keyword path and the temporal-graph path side-by-side and reports the
no-loss property: ``report.lost`` — v1 results absent from the graph path — MUST
be empty by construction, because ``TemporalAgentMemory.recall`` UNIONs the
keyword floor and never drops a floor event. ``report.added`` is the enrichment
(graph facts the keyword path could not surface).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ne.contracts.runtime import MemoryEvent
from ne.memory_graph.facade import TemporalAgentMemory
from ne.runtime.memory_store import AgentMemory

__all__ = ["ShadowReadReport", "shadow_read"]


class ShadowReadReport(BaseModel):
    """Side-by-side comparison of the v1 keyword path and the graph path."""

    model_config = ConfigDict(frozen=True)

    v1_count: int
    graph_path_count: int
    lost: tuple[MemoryEvent, ...]
    added: tuple[MemoryEvent, ...]

    @property
    def no_loss(self) -> bool:
        """True iff the graph path lost nothing the keyword path surfaced."""
        return self.lost == ()


async def shadow_read(
    facade: TemporalAgentMemory,
    agent_memory_v1: AgentMemory,
    query: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
) -> ShadowReadReport:
    """Compare the graph path against the v1 keyword path; report no-loss/divergence."""
    v1_results = list(await agent_memory_v1.recall(query, since=since, until=until, limit=limit))
    graph_results = list(await facade.recall(query, since=since, until=until, limit=limit))
    graph_set = list(graph_results)
    lost = tuple(event for event in v1_results if event not in graph_set)
    v1_set = list(v1_results)
    added = tuple(event for event in graph_results if event not in v1_set)
    return ShadowReadReport(
        v1_count=len(v1_results),
        graph_path_count=len(graph_results),
        lost=lost,
        added=added,
    )
