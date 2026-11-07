# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Full SQL connector pipeline over REAL components (NE-046) — no mock past the seam.

fixture SQLite -> SqlConnector -> SyncRunner -> KnowledgeWriter ->
QdrantChunkStore(:memory:) + real sqlite audit. Proves ACL-stamped chunks per
mode, exactly-one config_hash-stamped ``connector.sync`` event, the
``acl_quarantine`` denied event, deterministic document shaping, cursor
commit-only-on-success, an incremental second run, and mid-run-write-failure
resumability with zero duplicates.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.sql import run_sql_sync
from ne.connect.sql.contract import sql_data_contract
from ne.contracts.knowledge import KnowledgeWrite, UpsertReceipt

from .conftest import WriterStack
from .sql_fixture import build_sql_fixture_db, sql_settings

_SOURCE = "sql-acme"


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.db"
    build_sql_fixture_db(path)
    return path


def _engine(db_path: Path) -> sa.Engine:
    return sa.create_engine(f"sqlite:///{db_path}")


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


class _FailAfterNBatches:
    """Wraps a real ``KnowledgeWrite`` and raises on the Nth+1 ``upsert`` call."""

    def __init__(self, inner: KnowledgeWrite, fail_after: int) -> None:
        self._inner = inner
        self._fail_after = fail_after
        self._calls = 0

    async def upsert(self, items: Sequence[Any]) -> UpsertReceipt:
        self._calls += 1
        if self._calls > self._fail_after:
            raise RuntimeError("simulated write-lane fault")
        return await self._inner.upsert(items)

    async def curate(self, item_id: str, patch: Mapping[str, Any], *, provenance: str) -> None:
        await self._inner.curate(item_id, patch, provenance=provenance)

    async def quarantine(self, item_id: str, reason: str) -> None:
        await self._inner.quarantine(item_id, reason)


def test_backfill_stamps_acls_shapes_docs_and_emits_single_sync_event(
    writer_stack: WriterStack, frozen_config: str, db_path: Path
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        outcome = await run_sql_sync(
            sql_settings(db_path),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            engine=_engine(db_path),
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 7
        assert outcome.acl_quarantined == 2

        # (a) ACL stamping per mode.
        assert await _acl_of(stack, "docs:1", "1") == ("g-eng",)
        assert await _acl_of(stack, "notes:1", "1") == ("u-bob",)
        assert await _acl_of(stack, "notes:10", "10") == ("u-ada", "g-eng")
        assert await _acl_of(stack, "announcements:1", "1") == ("acl:tenant-all",)

        # (b) quarantined rows produced NO chunk.
        assert await stack.store.get([_item_id("notes:2", "2")]) == []
        assert await stack.store.get([_item_id("notes:9", "9")]) == []

        # (c) deterministic document shaping.
        chunk = (await stack.store.get([_item_id("docs:1", "1")]))[0]
        assert chunk.text == "T1\n\nB1"
        assert chunk.metadata["updated_at"] == "2026-01-01T00:00:00"
        assert chunk.metadata["table"] == "docs"
        # The SQL document-shaping mapper bypasses default_record_mapper, but
        # the shared SyncRunner boundary keeps its citation identity aligned
        # with graph construction's exact source_id:record_id grammar.
        assert chunk.metadata["source_system"] == "sql-acme"
        assert chunk.metadata["source_ref"] == "docs:1:1"
        assert chunk.metadata["source_ref"] != chunk.document_id

        # (d) exactly one connector.sync event, config_hash-stamped.
        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        assert sync_events[0]["outcome"] == "success"
        verification = await stack.sink.verify()
        assert verification.valid

        # (e) one acl_quarantine denied event naming both quarantined ids.
        quarantine_events = _events(stack, "connector.acl_quarantine")
        assert len(quarantine_events) == 1
        assert quarantine_events[0]["outcome"] == "denied"
        payload = quarantine_events[0]["payload"]
        assert isinstance(payload, dict)
        detail = str(payload["detail"])
        assert "quarantined=2" in detail
        assert "notes:2" in detail
        assert "notes:9" in detail

        # (f) cursors committed to the ledger.
        state = state_store.load_state(_SOURCE)
        assert "cursor:docs" in state
        assert "cursor:notes" in state
        assert state["cursor:docs"] == "2026-01-03T00:00:00"
        assert state["cursor:notes"] == "10"
        assert dict(outcome.report.cursors) == state

    asyncio.run(scenario())


def test_second_run_is_incremental_and_writes_only_the_delta(
    writer_stack: WriterStack, frozen_config: str, db_path: Path
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_sql_sync(
            sql_settings(db_path),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            engine=_engine(db_path),
        )
        assert first.report.written == 7

        engine = _engine(db_path)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE docs SET body = 'B2-revised', updated_at = '2026-01-05T00:00:00' "
                    "WHERE id = 2"
                )
            )
            conn.execute(
                sa.text("INSERT INTO notes (id, note, owner_principal) VALUES (11, 'n11', 'u-bob')")
            )

        second = await run_sql_sync(
            sql_settings(db_path),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            engine=_engine(db_path),
        )
        assert second.mode == "incremental"
        # docs id=2's updated_at moved past the persisted docs cursor (2026-01-03),
        # so it re-enters the incremental crawl (alongside the boundary-refetched
        # doc:3); notes id=11 is new (alongside the boundary-refetched note:10);
        # announcements have no cursor and are always fully re-scanned. Only the
        # genuinely changed/new rows are WRITTEN — the boundary re-fetches are
        # dedup-skipped, proving "no duplicates" (point count moves by exactly +1).
        assert second.report.written == 2
        assert second.report.skipped_unchanged == 4  # doc:3, note:10, ann:1, ann:2

        chunk = (await stack.store.get([_item_id("docs:2", "2")]))[0]
        assert "revised" in chunk.text
        assert (await stack.store.get([_item_id("notes:11", "11")]))[0].acl == ("u-bob",)

        # exactly the original 7 + the 1 new row exist, no duplicates.
        all_ids = [
            _item_id("docs:1", "1"),
            _item_id("docs:2", "2"),
            _item_id("docs:3", "3"),
            _item_id("notes:1", "1"),
            _item_id("notes:10", "10"),
            _item_id("notes:11", "11"),
            _item_id("announcements:1", "1"),
            _item_id("announcements:2", "2"),
        ]
        chunks = await stack.store.get(all_ids)
        assert len(chunks) == 8
        assert len({c.chunk_id for c in chunks}) == 8

        state = state_store.load_state(_SOURCE)
        assert state["cursor:docs"] == "2026-01-05T00:00:00"
        assert state["cursor:notes"] == "11"

    asyncio.run(scenario())


def test_third_run_with_no_changes_is_idempotent(
    writer_stack: WriterStack, frozen_config: str, db_path: Path
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        await run_sql_sync(
            sql_settings(db_path),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            engine=_engine(db_path),
        )
        third = await run_sql_sync(
            sql_settings(db_path),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            engine=_engine(db_path),
        )
        assert third.mode == "incremental"
        assert third.report.written == 0
        assert third.report.skipped_unchanged > 0

        sync_events = _events(stack, "connector.sync")
        assert len(sync_events) == 2  # exactly one per run

    asyncio.run(scenario())


def test_mid_run_write_failure_commits_no_cursor_and_rerun_is_exactly_once(
    writer_stack: WriterStack, frozen_config: str, db_path: Path
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        failing_writer = _FailAfterNBatches(stack.writer, fail_after=1)
        with pytest.raises(RuntimeError, match="simulated write-lane fault"):
            await run_sql_sync(
                sql_settings(db_path),
                writer=failing_writer,
                state_store=state_store,
                audit_sink=stack.sink,
                actor="connector:test",
                engine=_engine(db_path),
                batch_size=3,
            )
        state = state_store.load_state(_SOURCE)
        assert not any(key.startswith("cursor:") for key in state)

        retry = await run_sql_sync(
            sql_settings(db_path),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            engine=_engine(db_path),
            batch_size=3,
        )
        assert retry.mode == "backfill"  # no cursor was ever committed
        # the first (already-flushed, dedup-marked) batch is skipped; the rest
        # are written — exactly once, no duplicates.
        assert retry.report.written + retry.report.skipped_unchanged == 7

        for source_id, record_id in (
            ("docs", "1"),
            ("docs", "2"),
            ("docs", "3"),
            ("notes", "1"),
            ("notes", "10"),
            ("announcements", "1"),
            ("announcements", "2"),
        ):
            chunks = await stack.store.get([_item_id(f"{source_id}:{record_id}", record_id)])
            assert len(chunks) == 1

    asyncio.run(scenario())


def test_sql_data_contract_source_matches_settings(db_path: Path) -> None:
    settings = sql_settings(db_path)
    assert sql_data_contract(settings).source == _SOURCE
