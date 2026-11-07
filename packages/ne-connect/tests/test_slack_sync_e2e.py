# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full Slack pipeline over REAL components (NE-045) — no mock past the seam.

recorded Slack backfill+ACL -> SlackConnector -> SyncRunner -> KnowledgeWriter ->
QdrantChunkStore(:memory:) + real sqlite audit. Proves ACL-stamped chunks,
exactly-one config_hash-stamped connector.sync event, the acl_quarantine denied
event, ts-cursor persist/advance-only-on-success, an incremental second run, and
mid-run-failure resumability with zero duplicates (throttling never loses
progress — the post-2026-03 operational requirement). Also proves the
NE-CLEAN3 item 2 durability contract: a per-channel rate-limit skip does NOT
abort the run — the OTHER channels' cursors still commit.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.errors import SlackSyncError
from ne.connect.slack import TENANT_ALL, run_slack_sync

from .conftest import WriterStack
from .slack_transport import (
    C0000EXT,
    C0000FORGED,
    C0000PRIV,
    C0000PUB,
    C0000QMEM,
    C0000QTYPE,
    G0000MPIM,
    build_slack_transport,
    slack_settings,
)

_SOURCE = "slack"


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
    transport = build_slack_transport()

    async def scenario() -> None:
        outcome = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=transport,
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 7
        assert outcome.acl_quarantined == 4

        # (a) chunks stamped exactly as the fixtures grant.
        assert await _acl_of(stack, C0000PUB, "1720000000.000100") == (TENANT_ALL,)
        assert await _acl_of(stack, C0000PRIV, "1720000100.000100") == (
            "U00000ADA",
            "U00000BOB",
        )
        assert await _acl_of(stack, G0000MPIM, "1720000200.000100") == (
            "U00000ADA",
            "U00000EVE",
        )

        # (b) quarantined channels produced ZERO chunks.
        for cid, rid in (
            (C0000QTYPE, "n/a"),
            (C0000QMEM, "n/a"),
            (C0000FORGED, "n/a"),
            (C0000EXT, "n/a"),
        ):
            assert await stack.store.get([_item_id(cid, rid)]) == []

        # (c) exactly one connector.sync event, config_hash-stamped, chain valid.
        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        verification = await stack.sink.verify()
        assert verification.valid

        # (d) one acl_quarantine denied event naming all 4 quarantined ids.
        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        payload = quarantine_events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert "quarantined=4" in detail
        for cid in (C0000QTYPE, C0000QMEM, C0000FORGED, C0000EXT):
            assert cid in detail

        # (e) ts cursors persisted for the 3 live channels.
        assert state_store.load_state(_SOURCE) == {
            f"ts:{C0000PUB}": "1720000020.000300",
            f"ts:{C0000PRIV}": "1720000110.000200",
            f"ts:{G0000MPIM}": "1720000200.000100",
        }
        assert dict(outcome.report.cursors) == state_store.load_state(_SOURCE)

    asyncio.run(scenario())


def test_second_run_is_incremental_and_writes_only_the_new_message(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_slack_transport(),
        )
        assert first.report.written == 7

        second = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_slack_transport(incremental=True),
        )
        assert second.mode == "incremental"
        assert second.report.written == 1  # only the new C0000PUB message
        assert state_store.load_state(_SOURCE)[f"ts:{C0000PUB}"] == "1720009999.000900"
        chunk = (await stack.store.get([_item_id(C0000PUB, "1720009999.000900")]))[0]
        assert "brand new" in chunk.text

    asyncio.run(scenario())


def test_mid_run_failure_does_not_advance_cursor_and_rerun_has_no_duplicates(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        # Run 1: a Slack fault mid-crawl aborts the run BEFORE any cursor
        # commits — nothing written and no cursor advanced.
        with pytest.raises(SlackSyncError):
            await run_slack_sync(
                slack_settings(),
                writer=stack.writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                transport=build_slack_transport(fail_after=2),
            )
        assert state_store.load_state(_SOURCE) == {}  # cursor NOT advanced

        # Run 2 (retry, clean): completes the backfill and writes every item once.
        retry = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_slack_transport(),
        )
        assert retry.report.written == 7
        assert state_store.load_state(_SOURCE) == {
            f"ts:{C0000PUB}": "1720000020.000300",
            f"ts:{C0000PRIV}": "1720000110.000200",
            f"ts:{G0000MPIM}": "1720000200.000100",
        }

        # Run 3 (incremental) writes the one new message.
        third = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_slack_transport(incremental=True),
        )
        assert third.report.written == 1

        # Rewind the ledger to before the incremental message and re-run: the
        # same content is now dedup-skipped — ZERO duplicate chunks written.
        state_store.set_cursor(_SOURCE, f"ts:{C0000PUB}", "1720000020.000300")
        fourth = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=build_slack_transport(incremental=True),
        )
        # The re-fetched overlap is IDENTICAL content already in the dedup
        # ledger from run 3 — skipped, not re-written. Zero duplicate writes.
        assert fourth.report.written == 0
        assert fourth.report.skipped_unchanged == 1

        # The 7 backfilled + 1 incremental messages exist exactly once each.
        for source_id, record_id in (
            (C0000PUB, "1720000000.000100"),
            (C0000PUB, "1720000005.000150"),
            (C0000PUB, "1720000010.000200"),
            (C0000PUB, "1720000020.000300"),
            (C0000PUB, "1720009999.000900"),
            (C0000PRIV, "1720000100.000100"),
            (C0000PRIV, "1720000110.000200"),
            (G0000MPIM, "1720000200.000100"),
        ):
            assert len(await stack.store.get([_item_id(source_id, record_id)])) == 1

    asyncio.run(scenario())


def test_rate_limited_channel_skips_but_run_commits_other_cursors(
    writer_stack: WriterStack, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NE-CLEAN3 item 2 durability contract: a channel that exhausts Slack's
    rate limit does NOT abort the run — ``run_slack_sync`` returns (never
    raises), ``outcome.rate_limited == 1``, the OTHER channels' ``ts:``
    cursors are committed, the rate-limited channel's cursor is absent (it is
    retried in full next run), and it is never an ``acl_quarantine`` audit
    target."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    stack = writer_stack
    state_store = ConnectorStateStore()
    transport = build_slack_transport(member_ok_false_ratelimited=C0000PRIV)

    async def scenario() -> None:
        outcome = await run_slack_sync(
            slack_settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            transport=transport,
        )
        assert outcome.rate_limited == 1

        cursors = state_store.load_state(_SOURCE)
        assert f"ts:{C0000PRIV}" not in cursors
        assert cursors[f"ts:{C0000PUB}"] == "1720000020.000300"
        assert cursors[f"ts:{G0000MPIM}"] == "1720000200.000100"

        # No C0000PRIV record/chunk was written this run.
        assert await stack.store.get([_item_id(C0000PRIV, "1720000100.000100")]) == []

        # The acl_quarantine audit event (baseline QTYPE/QMEM/FORGED/EXT) never
        # names C0000PRIV — rate-limit skip is a distinct, non-quarantine
        # category.
        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        payload = quarantine_events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert C0000PRIV not in detail

    asyncio.run(scenario())
