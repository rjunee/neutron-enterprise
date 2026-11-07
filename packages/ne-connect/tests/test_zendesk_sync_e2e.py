# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full Zendesk pipeline over REAL components (NE-093) — no mock past the seam.

recorded cursor/incremental exports -> ZendeskConnector -> SyncRunner ->
KnowledgeWriter -> QdrantChunkStore(:memory:) + real sqlite audit. Proves
ACL-stamped chunks, exactly-one config_hash-stamped connector.sync event, the
acl_quarantine denied event naming the quarantined ticket/article/comment,
cursor persist/advance-only-on-success, an incremental second run, and
mid-run-failure resumability with zero duplicates.
"""

from __future__ import annotations

import asyncio
import hashlib

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.errors import ZendeskSyncError
from ne.connect.zendesk import run_zendesk_sync

from .conftest import WriterStack
from .zendesk_transport import build_zendesk_transport, zendesk_settings

_SOURCE = "zendesk"


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
        outcome = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(),
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 6
        assert outcome.acl_quarantined == 2
        assert outcome.items_quarantined == 1

        assert set(await _acl_of(stack, "ticket/1001", "1001")) == {
            "zd-user:5001",
            "zd-agent:6001",
            "zd-group:7001",
            "zd-org:8001",
        }
        assert set(await _acl_of(stack, "ticket/1001/internal", "comment-9002")) == {
            "zd-agent:6001",
            "zd-group:7001",
        }
        assert await _acl_of(stack, "article/2001", "2001") == ("acl:tenant-all",)

        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        assert (await stack.sink.verify()).valid

        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        detail = str(quarantine_events[0]["payload"]["detail"])  # type: ignore[index]
        for token in ("ticket/1003", "article/2003", "comment/9101"):
            assert token in detail

        assert state_store.load_state(_SOURCE) == {
            "cursor:tickets": "curs3",
            "cursor:articles": "1704067200",
        }
        assert dict(outcome.report.cursors) == state_store.load_state(_SOURCE)

    asyncio.run(scenario())


def test_second_run_is_incremental_and_writes_only_the_delta(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(),
        )
        assert first.report.written == 6

        second = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(incremental=True),
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # only the new ticket 1005
        chunk = (await stack.store.get([_item_id("ticket/1005", "1005")]))[0]
        assert "freshly created" in chunk.text

    asyncio.run(scenario())


def test_mid_run_failure_leaves_cursor_intact_and_rerun_has_no_duplicates(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        try:
            await run_zendesk_sync(
                zendesk_settings(max_attempts=1),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_zendesk_transport(fail_after=1),
            )
            raise AssertionError("expected a ZendeskSyncError abort")
        except ZendeskSyncError:
            pass
        assert state_store.load_state(_SOURCE) == {}

        retry = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(),
        )
        assert retry.report.written == 6

        third = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(incremental=True),
        )
        assert third.report.written == 1
        assert third.report.skipped_unchanged == 0

        for source_id, record_id in (
            ("ticket/1001", "1001"),
            ("ticket/1001", "comment-9001"),
            ("ticket/1001/internal", "comment-9002"),
            ("ticket/1002", "1002"),
            ("ticket/1005", "1005"),
            ("article/2001", "2001"),
            ("article/2002", "2002"),
        ):
            assert len(await stack.store.get([_item_id(source_id, record_id)])) == 1

    asyncio.run(scenario())


def test_parked_record_never_stalls_the_cursors_and_the_fixed_record_clears_its_park(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """A park is a HANDLED outcome, never a cursor stall — at the PRODUCTION
    composition: the quarantine lane is passed exactly as the worker cron
    (``ne.worker.ingest``) and ``ne connect sync`` pass it, unconditionally.

    A permanently-invalid ticket must not hold the stream cursors back — a
    stalled cursor replays the whole export window every cron fire, forever.
    The record's data is durable in its park, and the fix path stays live
    without any replay: fixing the ticket bumps ``updated_at``, it re-enters
    the incremental stream past any cursor, and its accepted write clears the
    park (shared canonical item id).
    """
    stack = writer_stack
    state_store = ConnectorStateStore()
    blank_ticket = _item_id("ticket/1006", "1006")

    async def scenario() -> None:
        first = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(),
            quarantine=stack.quarantine,
        )
        assert first.report.written == 6
        snapshot = state_store.load_state(_SOURCE)
        assert snapshot == {"cursor:tickets": "curs3", "cursor:articles": "1704067200"}

        second = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(
                incremental=True,
                incremental_fixture="tickets_cursor_incremental_blank.json",
            ),
            quarantine=stack.quarantine,
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # ticket 1005
        assert second.report.quarantined == 1  # blank ticket 1006 — parked, durable
        assert blank_ticket in {q.item_id for q in stack.quarantine.list_quarantined()}
        # The tickets cursor ADVANCES past the parked record.
        assert state_store.load_state(_SOURCE)["cursor:tickets"] == "curs-blank"

        third = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(
                incremental=True,
                incremental_fixture="tickets_cursor_incremental_fixed.json",
            ),
            quarantine=stack.quarantine,
        )
        assert third.report.written == 1  # ticket 1006 (1005 dedup-skipped)
        assert third.report.quarantined == 0
        final_state = state_store.load_state(_SOURCE)
        assert final_state["cursor:tickets"] == "curs-fixed"
        assert blank_ticket not in {q.item_id for q in stack.quarantine.list_quarantined()}
        assert final_state["cursor:articles"] == "1900000000"
        assert len(await stack.store.get([blank_ticket])) == 1

    asyncio.run(scenario())


def test_mid_export_duplicate_ticket_parks_and_never_stalls_the_run_or_cursor(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """ORDINARY Zendesk pagination — a ticket updated mid-export appears on two
    pages of one incremental run — must cost exactly one inspectable park:
    the run completes (pre-NE-CONNECT-CSV the duplicate key aborted the whole
    run in the writer), the earliest occurrence is written, the later one is
    parked fail-closed with the honest duplicate-identity reason, and the
    stream cursor STILL advances (a park is a handled outcome, not a stall).
    Production composition: the quarantine lane is passed as the worker cron
    and CLI pass it.
    """
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(),
            quarantine=stack.quarantine,
        )
        assert first.report.written == 6

        second = await run_zendesk_sync(
            zendesk_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_zendesk_transport(
                incremental=True,
                incremental_fixture="tickets_cursor_incremental_duplicate.json",
            ),
            quarantine=stack.quarantine,
        )
        # Earliest occurrence written; the mid-export re-appearance parked.
        assert second.report.written == 1  # ticket 1005 (first occurrence)
        assert second.report.invalid == 1  # the duplicate occurrence
        assert second.report.quarantined == 1
        parked = {q.item_id: q for q in stack.quarantine.list_quarantined()}
        dup_id = hashlib.sha256(f"{_SOURCE}\nticket/1005\n1005\nduplicate".encode()).hexdigest()
        assert parked[dup_id].reason == (
            "duplicate-identity: record_id '1005' from source_id 'ticket/1005' "
            "appeared more than once in one sync run; the earliest occurrence was "
            "processed on its own merits and every later occurrence is rejected "
            "fail-closed into this single park (payload = the last occurrence seen)"
        )
        # The park holds the LAST occurrence's payload — exactly as the reason says.
        assert parked[dup_id].payload["subject"] == "Brand new ticket (updated mid-export)"
        # The written chunk is the FIRST occurrence, untouched by the duplicate.
        chunk = (await stack.store.get([_item_id("ticket/1005", "1005")]))[0]
        assert chunk.text == "Brand new ticket\n\nA freshly created ticket."
        # And the cursor still advances — no replay of the window next fire.
        assert state_store.load_state(_SOURCE)["cursor:tickets"] == "curs-dup"

    asyncio.run(scenario())
