# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full Jira pipeline over REAL components (NE-093) — no mock past the seam.

recorded REST/Agile crawl -> JiraConnector -> SyncRunner -> KnowledgeWriter ->
QdrantChunkStore(:memory:) + real sqlite audit. Proves ACL-stamped chunks,
exactly-one config_hash-stamped connector.sync event, the acl_quarantine denied
event naming both quarantined projects + quarantined items, per-project cursor
persist/advance-only-on-success, an incremental second run, and mid-run-failure
resumability with zero duplicates.
"""

from __future__ import annotations

import asyncio
import hashlib

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.errors import JiraSyncError
from ne.connect.jira import run_jira_sync

from .conftest import WriterStack
from .jira_transport import (
    ACME_PROJECT_ID,
    GROUP_ID,
    ROLE_ID,
    USER_ID,
    build_jira_transport,
    jira_settings,
)

_SOURCE = "jira"


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
        outcome = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(),
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 6
        assert outcome.acl_quarantined == 2
        assert outcome.items_quarantined == 2

        expected_acl = (
            f"jira-group:{GROUP_ID}",
            f"jira-role:{ACME_PROJECT_ID}:{ROLE_ID}",
            f"jira-user:{USER_ID}",
            f"jira-project:{ACME_PROJECT_ID}",
        )
        assert await _acl_of(stack, "issues/ACME", "ACME-1") == expected_acl
        assert await _acl_of(stack, "comments/ACME", "40001") == expected_acl

        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        assert (await stack.sink.verify()).valid

        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        detail = str(quarantine_events[0]["payload"]["detail"])  # type: ignore[index]
        assert "projects_quarantined=2" in detail
        assert "items_quarantined=2" in detail
        for token in ("SECRET", "FORGED", "ACME-2", "comment/40002"):
            assert token in detail

        assert state_store.load_state(_SOURCE) == {"updated:ACME": "2026-01-03 10:30"}
        assert dict(outcome.report.cursors) == state_store.load_state(_SOURCE)

    asyncio.run(scenario())


def test_second_run_is_incremental_and_writes_only_the_delta(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(),
        )
        assert first.report.written == 6

        second = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(incremental=True),
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # only ACME-4
        assert state_store.load_state(_SOURCE)["updated:ACME"] == "2026-01-10 12:00"
        chunk = (await stack.store.get([_item_id("issues/ACME", "ACME-4")]))[0]
        assert "onboarding" in chunk.text

    asyncio.run(scenario())


def test_mid_run_failure_leaves_cursor_intact_and_rerun_has_no_duplicates(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        # fail_after=1: the project GET succeeds, the permissionscheme GET 500s;
        # max_attempts=1 aborts immediately (no cursor commit).
        try:
            await run_jira_sync(
                jira_settings(max_attempts=1),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_jira_transport(fail_after=1),
            )
            raise AssertionError("expected a JiraSyncError abort")
        except JiraSyncError:
            pass
        assert state_store.load_state(_SOURCE) == {}

        retry = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(),
        )
        assert retry.report.written == 6
        assert state_store.load_state(_SOURCE) == {"updated:ACME": "2026-01-03 10:30"}

        # Second (incremental) run writes the ACME-4 delta.
        third = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(incremental=True),
        )
        assert third.report.written == 1

        # Rewind the cursor and re-run incremental: ACME-4 is now dedup-skipped.
        state_store.set_cursor(_SOURCE, "updated:ACME", "2026-01-03 10:30")
        fourth = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(incremental=True),
        )
        assert fourth.report.written == 0
        # The incremental recrawl re-emits ACME-4 + the (cursorless) project and
        # sprint records; every one is dedup-skipped — ZERO duplicate writes.
        assert fourth.report.skipped_unchanged == 4

        for source_id, record_id in (
            ("project/ACME", ACME_PROJECT_ID),
            ("issues/ACME", "ACME-1"),
            ("issues/ACME", "ACME-3"),
            ("issues/ACME", "ACME-4"),
            ("comments/ACME", "40001"),
            ("sprints/ACME", "501"),
            ("sprints/ACME", "502"),
        ):
            assert len(await stack.store.get([_item_id(source_id, record_id)])) == 1

    asyncio.run(scenario())


def test_parked_record_never_stalls_the_cursor_and_the_fixed_record_clears_its_park(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """A park is a HANDLED outcome, never a cursor stall — at the PRODUCTION
    composition: the quarantine lane is passed exactly as the worker cron
    (``ne.worker.ingest``) and ``ne connect sync`` pass it, unconditionally.

    A permanently-invalid record must not hold the ``updated:ACME`` cursor
    back — a stalled cursor replays the whole window every cron fire, forever.
    The record's data is durable in its park (payload included), and the fix
    path stays live without any replay: fixing the record in Jira bumps
    ``updated``, it re-enters the incremental stream past any cursor, and its
    accepted write clears the park (shared canonical item id).
    """
    stack = writer_stack
    state_store = ConnectorStateStore()
    acme5 = _item_id("issues/ACME", "ACME-5")

    async def scenario() -> None:
        # Run A: ordinary backfill establishes the baseline cursor.
        first = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(),
            quarantine=stack.quarantine,
        )
        assert first.report.written == 6
        assert state_store.load_state(_SOURCE) == {"updated:ACME": "2026-01-03 10:30"}

        # Run B: the incremental batch adds ACME-4 (accepted) + a blank ACME-5
        # the governed KnowledgeWriter shape-parks. The park is durable and
        # inspectable — and the cursor ADVANCES past it.
        second = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(
                incremental=True, incremental_fixture="search_ACME_incremental_blank.json"
            ),
            quarantine=stack.quarantine,
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # ACME-4
        assert second.report.quarantined == 1  # ACME-5 (blank summary/description)
        assert acme5 in {q.item_id for q in stack.quarantine.list_quarantined()}
        assert state_store.load_state(_SOURCE) == {"updated:ACME": "2026-01-11 09:00"}
        assert dict(second.report.cursors) == {"updated:ACME": "2026-01-11 09:00"}

        # Run C: the record is fixed at the source (its edit bumped
        # ``updated``, so it re-enters the stream past the advanced cursor):
        # accepted, and the accepted write clears the park.
        third = await run_jira_sync(
            jira_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_jira_transport(
                incremental=True, incremental_fixture="search_ACME_incremental_fixed.json"
            ),
            quarantine=stack.quarantine,
        )
        assert third.report.written == 1  # ACME-5 (ACME-4 dedup-skipped)
        assert third.report.quarantined == 0
        assert state_store.load_state(_SOURCE) == {"updated:ACME": "2026-01-11 09:00"}
        assert len(await stack.store.get([acme5])) == 1
        assert acme5 not in {q.item_id for q in stack.quarantine.list_quarantined()}

    asyncio.run(scenario())
