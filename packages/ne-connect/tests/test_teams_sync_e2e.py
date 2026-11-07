# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full Teams pipeline over REAL components (NE-080) — no mock past the seam.

recorded channel/messages delta -> TeamsConnector -> SyncRunner ->
KnowledgeWriter -> QdrantChunkStore(:memory:) + real sqlite audit. Proves
ACL-stamped chunks, exactly-one config_hash-stamped connector.sync event, the
acl_quarantine denied event naming the quarantined channels, cursor
commit-only-on-success, and an incremental second run writing only the delta.
"""

from __future__ import annotations

import asyncio
import hashlib

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.errors import GraphSyncError
from ne.connect.teams import run_teams_sync

from .conftest import WriterStack
from .teams_transport import (
    DELTA_LINK_PRIV,
    DELTA_LINK_STD,
    DELTA_LINK_STD_REBASED,
    build_teams_transport,
    teams_settings,
)

_SOURCE = "teams"


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


def _audit_rows(stack: WriterStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]


def _events(stack: WriterStack, action: str) -> list[dict[str, object]]:
    return [r for r in _audit_rows(stack) if r["action"] == action]


async def _acl_of(stack: WriterStack, source_id: str, record_id: str) -> tuple[str, ...]:
    chunks = await stack.store.get([_item_id(source_id, record_id)])
    assert len(chunks) == 1
    return chunks[0].acl


def test_backfill_stamps_acls_and_emits_single_sync_event(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        outcome = await run_teams_sync(
            teams_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_teams_transport(),
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 5
        assert outcome.acl_quarantined == 3

        assert await _acl_of(stack, "team/T0001/channel/C0STD", "m1") == ("teams-team:T0001",)
        assert await _acl_of(stack, "team/T0001/channel/C0PRIV", "mp1") == (
            "teams-channel:T0001/C0PRIV",
        )

        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        assert (await stack.sink.verify()).valid

        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        detail = str(quarantine_events[0]["payload"]["detail"])  # type: ignore[index]
        for token in (
            "team/T0001/channel/C0SHARED",
            "team/T0001/channel/C0WEIRD",
            "team/T0001/channel/role:forged",
        ):
            assert token in detail

        assert state_store.load_state(_SOURCE) == {
            "cursor:delta:T0001/C0STD": (
                "https://graph.microsoft.com/v1.0/teams/T0001/channels/"
                "C0STD/messages/delta?$deltatoken=STD1"
            ),
            "cursor:delta:T0001/C0PRIV": (
                "https://graph.microsoft.com/v1.0/teams/T0001/channels/"
                "C0PRIV/messages/delta?$deltatoken=PRIV1"
            ),
        }
        assert dict(outcome.report.cursors) == state_store.load_state(_SOURCE)

    asyncio.run(scenario())


def test_second_run_is_incremental_and_writes_only_the_delta(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_teams_sync(
            teams_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_teams_transport(),
        )
        assert first.report.written == 5

        second = await run_teams_sync(
            teams_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_teams_transport(incremental=True),
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # only the new message m-new
        chunk = (await stack.store.get([_item_id("team/T0001/channel/C0STD", "m-new")]))[0]
        assert "freshly created" in chunk.text

    asyncio.run(scenario())


def test_expired_delta_cursor_rebaselines_and_persists_fresh_cursor(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """FIX-1 at the run_teams_sync level: a Graph HTTP 410 on the stored deltaLink
    self-heals — the run COMPLETES (no DeltaTokenExpiredError abort), the re-baselined
    full message set is written through the fail-closed KnowledgeWriter with correct
    ACL stamps, and the fresh per-channel cursors are PERSISTED with a value provably
    distinct from the expired one only after success.
    """
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        # Seed the STALE cursor exactly as sync.py persists it (a prior successful run).
        state_store.set_cursor(_SOURCE, "cursor:delta:T0001/C0STD", DELTA_LINK_STD)
        assert DELTA_LINK_STD_REBASED != DELTA_LINK_STD  # the value-swap proof depends on this

        # The transport 410s the stored C0STD deltaLink — an un-fixed connector would
        # abort the whole run with DeltaTokenExpiredError here.
        outcome = await run_teams_sync(
            teams_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_teams_transport(expire_std_delta=True),
        )
        assert outcome.mode == "incremental"
        # The re-baseline re-emits the channel's full current set through the SAME
        # fail-closed writer path: m1, reply-r1, m2, m3, mp1.
        assert outcome.report.written == 5

        # C7: fail-closed ACL stamping preserved on the re-baselined records.
        assert await _acl_of(stack, "team/T0001/channel/C0STD", "m1") == ("teams-team:T0001",)

        # Durability contract: the REFRESHED cursor is committed ONLY after the run
        # COMPLETED. The persisted C0STD cursor is DELTA_LINK_STD_REBASED — textually
        # distinct from the seeded stale DELTA_LINK_STD — proving the value-swap (not
        # merely that some cursor was committed after success); this would fail if the
        # stale cursor had lingered uncommitted.
        assert state_store.load_state(_SOURCE) == {
            "cursor:delta:T0001/C0STD": DELTA_LINK_STD_REBASED,
            "cursor:delta:T0001/C0PRIV": DELTA_LINK_PRIV,
        }

        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["outcome"] == "success"
        assert sync_events[0]["config_hash"] == frozen_config

    asyncio.run(scenario())


def test_mid_run_failure_leaves_cursor_intact(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        try:
            await run_teams_sync(
                teams_settings(max_attempts=1),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_teams_transport(fail_after=1),
            )
            raise AssertionError("expected a GraphSyncError abort")
        except GraphSyncError:
            pass
        assert state_store.load_state(_SOURCE) == {}

        retry = await run_teams_sync(
            teams_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_teams_transport(),
        )
        assert retry.report.written == 5

    asyncio.run(scenario())
