# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Replay-based backfill / catch-up over the UNTOUCHED NE-037 episodic log.

The log IS the queue (feasibility memo §4): backfill replays each agent's events
in ``occurred_at`` order through the sidecar as Graphiti episodes named by
``event_id`` — idempotent by name ⇒ resumable. A per-agent watermark
(``ne_memory_graph_watermarks``) records the last-ingested ``recorded_at`` so
``catch_up`` replays only the gap. The watermark table is DERIVED, rebuildable
state: dropping it is safe (a fresh backfill re-derives it; the graph re-ingest is
idempotent).

Watermark rule (NE-083b-FOLLOWUP): replay order is ``occurred_at`` ASC, but
``recorded_at`` is NOT monotonic in that order — a BACKDATED event (occurred
earlier, recorded later than a sibling) can be replayed after an event with a
lower ``recorded_at``. The watermark therefore advances only over the
CONTIGUOUS ``recorded_at`` PREFIX of successfully-ingested pending events: a
success is buffered until every pending event with a lower-or-equal
``recorded_at`` has also succeeded, so a failed/backdated event stranded mid-run
is retried by the next ``catch_up`` instead of being skipped past permanently.

Extraction cost (R1/C12): every replayed event is an LLM extraction call, so a
``throttle_s`` sleep (injectable for tests) paces the spend.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.contracts.governance import AuditSink
from ne.events import new_audit_event
from ne.memory_graph.client import MemoryGraphClient
from ne.runtime.memory_store import (
    SqlAlchemyEpisodicStore,
    ne_agent_memory_events,
)
from ne.schema import AuditOutcome

__all__ = [
    "AgentBackfillReport",
    "BackfillReport",
    "backfill",
    "catch_up",
    "ensure_watermark_schema",
    "watermark_metadata",
]

watermark_metadata = sa.MetaData()

ne_memory_graph_watermarks = sa.Table(
    "ne_memory_graph_watermarks",
    watermark_metadata,
    sa.Column("agent_id", sa.String, primary_key=True),
    sa.Column("last_recorded_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_watermark_schema(engine: sa.Engine) -> None:
    """Create the (rebuildable) watermark table. Idempotent."""
    watermark_metadata.create_all(engine)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _read_watermark(engine: sa.Engine, agent_id: str) -> datetime | None:
    stmt = sa.select(ne_memory_graph_watermarks.c.last_recorded_at).where(
        ne_memory_graph_watermarks.c.agent_id == agent_id
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return _as_utc(row[0]) if row is not None else None


def _write_watermark(engine: sa.Engine, agent_id: str, value: datetime) -> None:
    value = _as_utc(value)
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(ne_memory_graph_watermarks.c.agent_id).where(
                ne_memory_graph_watermarks.c.agent_id == agent_id
            )
        ).first()
        if existing is None:
            conn.execute(
                ne_memory_graph_watermarks.insert().values(
                    agent_id=agent_id, last_recorded_at=value
                )
            )
        else:
            conn.execute(
                ne_memory_graph_watermarks.update()
                .where(ne_memory_graph_watermarks.c.agent_id == agent_id)
                .values(last_recorded_at=value)
            )


def _all_agent_ids(engine: sa.Engine) -> list[str]:
    stmt = (
        sa.select(ne_agent_memory_events.c.agent_id)
        .distinct()
        .order_by(ne_agent_memory_events.c.agent_id)
    )
    with engine.connect() as conn:
        return [str(row[0]) for row in conn.execute(stmt).all()]


class AgentBackfillReport(BaseModel):
    """Per-agent replay counts."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    replayed: int
    skipped: int
    failed: int


class BackfillReport(BaseModel):
    """Frozen report over one backfill / catch-up pass."""

    model_config = ConfigDict(frozen=True)

    agents: tuple[AgentBackfillReport, ...]

    def get(self, agent_id: str) -> AgentBackfillReport | None:
        for agent in self.agents:
            if agent.agent_id == agent_id:
                return agent
        return None

    def require(self, agent_id: str) -> AgentBackfillReport:
        """Like :meth:`get` but raises ``KeyError`` when the agent is absent."""
        agent = self.get(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        return agent


async def _emit(sink: AuditSink | None, *, action: str, actor: str, detail: str) -> None:
    if sink is None:
        return
    await sink.append(
        new_audit_event(
            action=action,
            resource=f"memory-graph-backfill:{actor}",
            outcome=AuditOutcome.success,
            actor=actor,
            detail=detail,
            provenance={"source": "ne-memory-graph", "component": "backfill"},
        )
    )


async def _replay_agent(
    store: SqlAlchemyEpisodicStore,
    client: MemoryGraphClient,
    agent_id: str,
    *,
    since_recorded_at: datetime | None,
    throttle_s: float,
    sleep: Callable[[float], Awaitable[None]],
) -> AgentBackfillReport:
    # Newest-first from the store; reverse to occurred_at ASC (stable, deterministic).
    rows = list(reversed(await store.recall(agent_id=agent_id, limit=None)))
    pending = [
        row
        for row in rows
        if since_recorded_at is None or _as_utc(row.recorded_at) > since_recorded_at
    ]
    skipped = len(rows) - len(pending)
    replayed = failed = 0
    engine = store.engine

    # Contiguous-recorded_at-prefix frontier (NE-083b-FOLLOWUP): a success only
    # advances the watermark once every pending event at-or-below its
    # recorded_at has also succeeded, so a backdated/failed event never gets
    # skipped past permanently.
    frontier = sorted(_as_utc(row.recorded_at) for row in pending)
    done: Counter[datetime] = Counter()
    idx = 0

    for row in pending:
        try:
            ok = await client.post_episode(
                group_id=agent_id,
                name=row.event_id,
                content=row.content,
                kind=row.kind,
                occurred_at=row.occurred_at,
                provenance=row.provenance,
            )
        except Exception:
            failed += 1
            break
        if not ok:
            failed += 1
            break
        replayed += 1
        done[_as_utc(row.recorded_at)] += 1
        advanced: datetime | None = None
        while idx < len(frontier) and done[frontier[idx]] > 0:
            done[frontier[idx]] -= 1
            consumed = frontier[idx]
            idx += 1
            # Only a FULLY-consumed timestamp may become the watermark: with
            # duplicate recorded_at values (same-instant appends / DB timestamp
            # truncation), advancing while a same-timestamp sibling is still
            # un-ingested would strand that sibling below the
            # ``recorded_at > watermark`` catch_up filter permanently.
            if idx == len(frontier) or frontier[idx] != consumed:
                advanced = consumed
        if advanced is not None:
            _write_watermark(engine, agent_id, advanced)
        if throttle_s:
            await sleep(throttle_s)
    return AgentBackfillReport(agent_id=agent_id, replayed=replayed, skipped=skipped, failed=failed)


async def backfill(
    store: SqlAlchemyEpisodicStore,
    client: MemoryGraphClient,
    *,
    agent_ids: Sequence[str] | None = None,
    throttle_s: float = 0.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    sink: AuditSink | None = None,
) -> BackfillReport:
    """Replay every (or the named) agent's full episodic log into the graph.

    Idempotent by episode name ⇒ resumable; a per-event ``sleep(throttle_s)`` paces
    LLM-extraction spend. A failure stops THAT agent and continues the others.
    """
    ensure_watermark_schema(store.engine)
    agents = list(agent_ids) if agent_ids is not None else _all_agent_ids(store.engine)
    reports: list[AgentBackfillReport] = []
    for agent_id in agents:
        await _emit(sink, action="memory.graph.backfill.started", actor=agent_id, detail="{}")
        report = await _replay_agent(
            store,
            client,
            agent_id,
            since_recorded_at=None,
            throttle_s=throttle_s,
            sleep=sleep,
        )
        reports.append(report)
        await _emit(
            sink,
            action="memory.graph.backfill.finished",
            actor=agent_id,
            detail=report.model_dump_json(),
        )
    return BackfillReport(agents=tuple(reports))


async def catch_up(
    store: SqlAlchemyEpisodicStore,
    client: MemoryGraphClient,
    engine: sa.Engine,
    *,
    agent_ids: Sequence[str] | None = None,
    throttle_s: float = 0.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    sink: AuditSink | None = None,
) -> BackfillReport:
    """Replay only the per-agent gap past the watermark (``recorded_at`` > mark)."""
    ensure_watermark_schema(engine)
    agents = list(agent_ids) if agent_ids is not None else _all_agent_ids(engine)
    reports: list[AgentBackfillReport] = []
    for agent_id in agents:
        mark = _read_watermark(engine, agent_id)
        report = await _replay_agent(
            store,
            client,
            agent_id,
            since_recorded_at=mark,
            throttle_s=throttle_s,
            sleep=sleep,
        )
        reports.append(report)
        await _emit(
            sink,
            action="memory.graph.catch_up.finished",
            actor=agent_id,
            detail=report.model_dump_json(),
        )
    return BackfillReport(agents=tuple(reports))
