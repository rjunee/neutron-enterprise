# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Backfill: replay order, watermark resume, throttle, audit, report counts + CLI."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.memory_graph import cli
from ne.memory_graph.backfill import backfill, catch_up, ne_memory_graph_watermarks
from ne.runtime.memory_store import SqlAlchemyEpisodicStore, ne_agent_memory_events

from .conftest import T0, FakeSidecar


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEventLike] = []

    async def append(self, event: AuditEventLike) -> None:
        self.events.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return AuditVerification(valid=True, checked=len(self.events))

    def actions(self) -> list[str]:
        return [getattr(e, "action", "") for e in self.events]


def test_replay_order_is_occurred_at_ascending(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        # Append OUT of occurred_at order.
        for minute in (5, 1, 3, 2, 4):
            await store.append(
                agent_id="agent-a",
                kind="outcome",
                content=f"m{minute}",
                occurred_at=T0 + timedelta(minutes=minute),
            )
        sidecar = FakeSidecar()
        report = await backfill(store, sidecar.client(), agent_ids=["agent-a"])
        assert report.require("agent-a").replayed == 5
        assert [p["content"] for p in sidecar.posted] == ["m1", "m2", "m3", "m4", "m5"]

    asyncio.run(scenario())


def test_injectable_sleep_throttle(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        for i in range(3):
            await store.append(agent_id="agent-a", kind="outcome", content=f"o{i}", occurred_at=T0)
        calls: list[float] = []

        async def fake_sleep(s: float) -> None:
            calls.append(s)

        await backfill(
            store, FakeSidecar().client(), agent_ids=["agent-a"], throttle_s=0.25, sleep=fake_sleep
        )
        assert calls == [0.25, 0.25, 0.25]

    asyncio.run(scenario())


def test_audit_events_emitted_on_sink(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await store.append(agent_id="agent-a", kind="outcome", content="o", occurred_at=T0)
        sink = _RecordingSink()
        await backfill(store, FakeSidecar().client(), agent_ids=["agent-a"], sink=sink)
        actions = sink.actions()
        assert "memory.graph.backfill.started" in actions
        assert "memory.graph.backfill.finished" in actions

    asyncio.run(scenario())


def test_per_agent_counts_and_all_agents_discovery(store: SqlAlchemyEpisodicStore) -> None:
    async def scenario() -> None:
        await store.append(agent_id="agent-a", kind="outcome", content="a0", occurred_at=T0)
        await store.append(agent_id="agent-b", kind="outcome", content="b0", occurred_at=T0)
        await store.append(agent_id="agent-b", kind="outcome", content="b1", occurred_at=T0)
        report = await backfill(store, FakeSidecar().client())  # agent_ids=None ⇒ discover
        assert report.require("agent-a").replayed == 1
        assert report.require("agent-b").replayed == 2

    asyncio.run(scenario())


def test_failure_stops_agent_and_is_watermark_resumable(
    store: SqlAlchemyEpisodicStore,
) -> None:
    async def scenario() -> None:
        for i in range(4):
            await store.append(
                agent_id="agent-a",
                kind="outcome",
                content=f"o{i}",
                occurred_at=T0 + timedelta(minutes=i),
            )
        # Sidecar fails from the 2nd episode onward → only o0 posts, agent stops.
        failing = FakeSidecar(fail_episodes_after=1)
        r1 = await backfill(store, failing.client(), agent_ids=["agent-a"])
        assert r1.require("agent-a").replayed == 1
        assert r1.require("agent-a").failed == 1
        # A healthy sidecar catch-up resumes exactly the gap past the watermark.
        healthy = FakeSidecar()
        r2 = await catch_up(store, healthy.client(), store.engine, agent_ids=["agent-a"])
        assert r2.require("agent-a").replayed == 3
        assert [p["content"] for p in healthy.posted] == ["o1", "o2", "o3"]

    asyncio.run(scenario())


def test_backdated_event_not_permanently_skipped_after_partial_failure(
    store: SqlAlchemyEpisodicStore,
) -> None:
    """A backdated event (occurred first, recorded last) stranded by a partial
    failure must NOT be permanently skipped by a later ``catch_up`` — the
    watermark may only advance over the contiguous ``recorded_at`` prefix of
    successfully-ingested events (NE-083b-FOLLOWUP).
    """

    async def scenario() -> None:
        e1 = await store.append(
            agent_id="agent-a",
            kind="outcome",
            content="early-occurred",
            occurred_at=T0 + timedelta(minutes=1),
        )
        e2 = await store.append(
            agent_id="agent-a",
            kind="outcome",
            content="later-occurred",
            occurred_at=T0 + timedelta(minutes=2),
        )
        # Force non-monotonic recorded_at: e1 (occurred FIRST) is recorded LAST.
        with store.engine.begin() as conn:
            conn.execute(
                ne_agent_memory_events.update()
                .where(ne_agent_memory_events.c.event_id == e1)
                .values(recorded_at=T0 + timedelta(minutes=10))
            )
            conn.execute(
                ne_agent_memory_events.update()
                .where(ne_agent_memory_events.c.event_id == e2)
                .values(recorded_at=T0 + timedelta(minutes=5))
            )

        # Replay order is occurred_at ASC: e1 posts (succeeds), then e2 fails.
        failing = FakeSidecar(fail_episodes_after=1)
        r1 = await backfill(store, failing.client(), agent_ids=["agent-a"])
        assert r1.require("agent-a").replayed == 1
        assert r1.require("agent-a").failed == 1

        # Under the OLD per-row watermark write, this would now be T0+10 —
        # stranding e2 (recorded_at T0+5) below the watermark forever. The
        # frontier algorithm must NOT have written any watermark row at all,
        # since e2 (a lower recorded_at pending event) never succeeded.
        with store.engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_memory_graph_watermarks.c.last_recorded_at).where(
                    ne_memory_graph_watermarks.c.agent_id == "agent-a"
                )
            ).first()
        assert row is None

        healthy = FakeSidecar()
        r2 = await catch_up(store, healthy.client(), store.engine, agent_ids=["agent-a"])
        assert r2.require("agent-a").replayed == 2
        assert {"early-occurred", "later-occurred"} <= {p["content"] for p in healthy.posted}

        with store.engine.connect() as conn:
            final_row = conn.execute(
                sa.select(ne_memory_graph_watermarks.c.last_recorded_at).where(
                    ne_memory_graph_watermarks.c.agent_id == "agent-a"
                )
            ).first()
        assert final_row is not None
        final_watermark = final_row[0]
        if final_watermark.tzinfo is None:
            final_watermark = final_watermark.replace(tzinfo=UTC)
        assert final_watermark == T0 + timedelta(minutes=10)

    asyncio.run(scenario())


def test_duplicate_recorded_at_sibling_not_skipped_after_partial_failure(
    store: SqlAlchemyEpisodicStore,
) -> None:
    """Two pending events sharing ONE ``recorded_at`` (same-instant appends /
    DB timestamp truncation): a success at that timestamp must NOT advance the
    watermark while the same-timestamp sibling is still un-ingested, or the
    sibling would be stranded below the ``recorded_at > watermark`` catch_up
    filter permanently (Codex P2 on NE-083b-FOLLOWUP).
    """

    async def scenario() -> None:
        e1 = await store.append(
            agent_id="agent-a",
            kind="outcome",
            content="twin-one",
            occurred_at=T0 + timedelta(minutes=1),
        )
        e2 = await store.append(
            agent_id="agent-a",
            kind="outcome",
            content="twin-two",
            occurred_at=T0 + timedelta(minutes=2),
        )
        shared = T0 + timedelta(minutes=7)
        with store.engine.begin() as conn:
            conn.execute(
                ne_agent_memory_events.update()
                .where(ne_agent_memory_events.c.event_id.in_([e1, e2]))
                .values(recorded_at=shared)
            )

        # e1 posts (succeeds), then e2 fails — both share `shared` recorded_at.
        failing = FakeSidecar(fail_episodes_after=1)
        r1 = await backfill(store, failing.client(), agent_ids=["agent-a"])
        assert r1.require("agent-a").replayed == 1
        assert r1.require("agent-a").failed == 1

        # The shared timestamp is only PARTIALLY consumed, so NO watermark may
        # exist — writing `shared` here would permanently strand e2.
        with store.engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_memory_graph_watermarks.c.last_recorded_at).where(
                    ne_memory_graph_watermarks.c.agent_id == "agent-a"
                )
            ).first()
        assert row is None

        healthy = FakeSidecar()
        r2 = await catch_up(store, healthy.client(), store.engine, agent_ids=["agent-a"])
        assert r2.require("agent-a").replayed == 2
        assert {"twin-one", "twin-two"} <= {p["content"] for p in healthy.posted}

        with store.engine.connect() as conn:
            final_row = conn.execute(
                sa.select(ne_memory_graph_watermarks.c.last_recorded_at).where(
                    ne_memory_graph_watermarks.c.agent_id == "agent-a"
                )
            ).first()
        assert final_row is not None
        final_watermark = final_row[0]
        if final_watermark.tzinfo is None:
            final_watermark = final_watermark.replace(tzinfo=UTC)
        assert final_watermark == shared

    asyncio.run(scenario())


def test_cli_backfill_all_drives_the_hooks(
    store: SqlAlchemyEpisodicStore, monkeypatch, capsys
) -> None:
    async def seed() -> None:
        await store.append(agent_id="agent-a", kind="outcome", content="a0", occurred_at=T0)

    asyncio.run(seed())
    sidecar = FakeSidecar()
    monkeypatch.setattr(cli, "build_engine", lambda settings: store.engine)
    monkeypatch.setattr(cli, "build_store", lambda engine: store)
    monkeypatch.setattr(cli, "build_client", lambda settings: sidecar.client())
    monkeypatch.setattr(cli, "load_runtime_settings", lambda: object())
    monkeypatch.setattr(cli, "load_memory_settings", lambda: object())

    rc = cli.main(["backfill", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "agent=agent-a replayed=1" in out
