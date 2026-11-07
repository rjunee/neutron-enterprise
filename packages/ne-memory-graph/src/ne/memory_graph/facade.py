# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``TemporalAgentMemory`` — the frozen ``MemoryProvider`` over episodic + graph.

Implements ``ne.contracts.runtime.MemoryProvider`` byte-unchanged. ``append`` is
the unchanged NE-037 episodic INSERT (the transaction, the TRUTH) plus a
best-effort queued Graphiti episode ingestion — a sidecar outage NEVER fails an
append. ``recall`` UNIONs graph fact hits over the retained v1 keyword floor and
projects everything to the frozen ``MemoryEvent`` shape.

Degradation is total: if the sidecar is unreachable, ``append`` still writes the
episodic row and ``recall`` returns exactly the v1 keyword floor.

INTERVAL → INSTANT projection caveat (spike memo Q3): the frozen
``MemoryEvent.occurred_at`` is an INSTANT, but graph facts carry a valid-time
INTERVAL. A projected fact's ``occurred_at`` is its ``valid_at`` (interval start;
falling back to ``created_at``) — the full interval and bi-temporal fields are
visible only via the store-level :meth:`TemporalAgentMemory.recall_as_of` extra,
never through the frozen seam.

Governance: this facade (and this whole package) holds NO knowledge-writer /
knowledge-write-seam reference. Promotion of a graph-synthesized fact still routes
append → propose →
promote through the UNCHANGED ``MemoryPromoter``, which reads the LOG, never the
graph. Reads are bound to one ``agent_id``; every search carries
``group_ids=[agent_id]`` plus a defense-in-depth provenance post-filter so
existence never leaks cross-agent (the NE-037 bar).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from ne.contracts.runtime import MemoryEvent
from ne.memory_graph.client import GraphHit, MemoryGraphClient
from ne.memory_graph.config import MemoryGraphSettings
from ne.runtime.errors import MemoryEventNotFoundError
from ne.runtime.memory_store import MemoryEventKind, SqlAlchemyEpisodicStore

__all__ = ["TemporalAgentMemory", "create_temporal_agent_memory"]

_LOGGER = logging.getLogger("ne.memory_graph.facade")


class TemporalAgentMemory:
    """Per-agent temporal-graph memory facade (frozen ``MemoryProvider``)."""

    def __init__(
        self,
        store: SqlAlchemyEpisodicStore,
        agent_id: str,
        *,
        session_id: str | None = None,
        settings: MemoryGraphSettings | None = None,
        client: MemoryGraphClient | None = None,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")
        self._store = store
        self._agent_id = agent_id
        self._session_id = session_id
        self._settings = settings or MemoryGraphSettings()
        # Construction-time loopback guard fires here (via the client ctor) unless a
        # pre-built client is injected.
        self._client = client if client is not None else MemoryGraphClient(self._settings)

    async def append(self, event: MemoryEvent) -> None:
        """Append via the frozen Protocol: episodic INSERT (the transaction) then
        a best-effort queued graph episode. The INSERT is the ONLY thing that can
        fail an append; ALL ingestion exceptions are swallowed (logged)."""
        event_id = await self._store.append(
            agent_id=self._agent_id,
            kind=event.kind,
            content=event.content,
            occurred_at=event.occurred_at,
            provenance=event.provenance,
            session_id=self._session_id,
        )
        try:
            await self._client.post_episode(
                group_id=self._agent_id,
                name=event_id,
                content=event.content,
                kind=event.kind,
                occurred_at=event.occurred_at,
                provenance=event.provenance,
            )
        except Exception:
            _LOGGER.warning(
                "graph episode ingestion failed for event %s (agent %s); "
                "the episodic row is durable and will be replayed by catch-up",
                event_id,
                self._agent_id,
                exc_info=True,
            )

    async def _keyword_floor(
        self,
        query: str,
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[MemoryEvent]:
        """The retained v1 keyword floor — identical semantics to AgentMemory.recall."""
        rows = await self._store.recall(
            agent_id=self._agent_id, since=since, until=until, limit=None
        )
        q = query.strip().lower()
        matched: list[MemoryEvent] = []
        for row in rows:
            if not q or q in row.content.lower() or q == row.kind.lower():
                matched.append(row.as_memory_event())
            if len(matched) >= limit:
                break
        return matched

    async def _surviving_hits(self, hits: Sequence[GraphHit]) -> list[GraphHit]:
        """Defense-in-depth post-filter: bound-agent group + resolvable provenance.

        Drops any hit whose ``group_id`` is not the bound agent, whose
        ``event_ids`` is EMPTY (a fact with no backing episode has no provenance
        at all — never surfaced), OR any of whose ``event_ids`` does not resolve
        to one of the bound agent's own events — existence never leaks
        cross-agent and provenance is always resolvable. Also dedupes graph hits
        among themselves by ``edge_uuid``.
        """
        seen: set[str] = set()
        surviving: list[GraphHit] = []
        for hit in hits:
            if hit.group_id != self._agent_id:
                continue
            if not hit.event_ids:
                continue
            if hit.edge_uuid in seen:
                continue
            resolvable = True
            for event_id in hit.event_ids:
                try:
                    await self._store.get(agent_id=self._agent_id, event_id=event_id)
                except MemoryEventNotFoundError:
                    resolvable = False
                    break
            if not resolvable:
                continue
            seen.add(hit.edge_uuid)
            surviving.append(hit)
        return surviving

    @staticmethod
    def _project(hit: GraphHit) -> MemoryEvent:
        """Project a surviving graph hit to the frozen ``MemoryEvent`` shape."""
        episodes = ",".join(hit.event_ids)
        return MemoryEvent(
            kind=MemoryEventKind.LEARNED_FACT,
            content=hit.content,
            occurred_at=hit.valid_at or hit.created_at,
            provenance=f"memory-graph:edge={hit.edge_uuid};episodes={episodes}",
        )

    async def _graph_facts(
        self,
        query: str,
        *,
        since: datetime | None,
        until: datetime | None,
        as_of: datetime | None,
        limit: int,
    ) -> list[MemoryEvent]:
        """Graph fact hits, post-filtered + projected. Sidecar errors ⇒ empty."""
        try:
            hits = await self._client.search(
                query,
                group_ids=[self._agent_id],
                since=since,
                until=until,
                as_of=as_of,
                limit=limit,
            )
        except Exception:
            _LOGGER.warning(
                "graph search failed for agent %s; degrading to keyword floor",
                self._agent_id,
                exc_info=True,
            )
            return []
        surviving = await self._surviving_hits(hits)
        return [self._project(hit) for hit in surviving]

    async def recall(
        self,
        query: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
    ) -> Sequence[MemoryEvent]:
        """Hybrid recall: v1 keyword floor UNIONed with projected graph facts.

        Fixes the v1 ``recall(limit=0)`` off-by-one (``[]``, not "all") — this is
        the ONE deliberate behavioural fix vs. AgentMemory; the floor is otherwise
        byte-identical. Floor events are NEVER dropped; graph facts fill remaining
        slots, newest-first by ``occurred_at``, truncated to ``limit``.
        """
        if limit <= 0:
            return []
        floor = await self._keyword_floor(query, since=since, until=until, limit=limit)
        remaining = limit - len(floor)
        if remaining <= 0:
            return sorted(floor, key=lambda e: e.occurred_at, reverse=True)[:limit]
        facts = await self._graph_facts(query, since=since, until=until, as_of=None, limit=limit)
        facts_newest = sorted(facts, key=lambda e: e.occurred_at, reverse=True)[:remaining]
        combined = [*floor, *facts_newest]
        return sorted(combined, key=lambda e: e.occurred_at, reverse=True)

    async def recall_as_of(
        self,
        query: str,
        *,
        as_of: datetime,
        limit: int = 20,
    ) -> Sequence[MemoryEvent]:
        """Point-in-time current-belief recall (NOT on the frozen Protocol).

        Returns the facts the agent believed AS OF ``as_of`` (bi-temporal
        current-belief filtering happens server-side), post-filtered + projected.
        The ``recall_shared`` / ``get`` precedent: richer temporal ops live on the
        concrete class, never on the frozen seam.
        """
        if limit <= 0:
            return []
        facts = await self._graph_facts(query, since=None, until=None, as_of=as_of, limit=limit)
        return sorted(facts, key=lambda e: e.occurred_at, reverse=True)[:limit]


def create_temporal_agent_memory(
    store: SqlAlchemyEpisodicStore,
    agent_id: str,
    *,
    session_id: str | None = None,
    settings: MemoryGraphSettings | None = None,
    client: MemoryGraphClient | None = None,
) -> TemporalAgentMemory:
    """Entry-point factory (``ne.plugins.memory_provider:temporal-graph``)."""
    return TemporalAgentMemory(
        store,
        agent_id,
        session_id=session_id,
        settings=settings,
        client=client,
    )
