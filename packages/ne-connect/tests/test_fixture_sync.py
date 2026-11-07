# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-INGEST (P1-4) — the fixture sync governed write-path proof (no live tenant).

Over the EXISTING writer_stack conftest fixture (REAL :memory: Qdrant + real audit
sink — no mock past the seam): run_fixture_sync writes the 3 recorded records via the
governed KnowledgeWriter, the chunks carry the fixture's source ACL principals (C7),
exactly ONE connector.sync AuditEvent is recorded, and a re-run is dedup-idempotent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.connect import ConnectorStateStore
from ne.connect.bridge import canonical_payload_json
from ne.connect.fixture_sync import FixtureSyncSettings, run_fixture_sync
from ne.contracts.source_authority import NO_SOURCE_STATUS, SOURCE_STATUS_METADATA_KEY

from .conftest import FIXTURE_SOURCE_PATH, WriterStack

_SOURCE = "acme-crm"
_OWNER = "data-eng@acme.test"


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


def _sync_events(stack: WriterStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        rows = [
            dict(r)
            for r in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]
    return [r for r in rows if r["action"] == "connector.sync"]


def _settings() -> FixtureSyncSettings:
    return FixtureSyncSettings(path=str(FIXTURE_SOURCE_PATH), owner=_OWNER)


def test_fixture_sync_writes_three_records_with_source_acls(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        outcome = await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
        )
        assert outcome.mode == "backfill"
        assert outcome.report.written == 3

        # C7: chunks stamped exactly as the fixture grants.
        contacts = (await stack.store.get([_item_id("acme-crm/contacts", "c1")]))[0]
        assert contacts.acl == ("oidc:sales",)
        accounts = (await stack.store.get([_item_id("acme-crm/accounts", "a1")]))[0]
        assert accounts.acl == ("oidc:finance",)

        # exactly one connector.sync event, config_hash-stamped, chain valid.
        events = _sync_events(stack)
        assert len(events) == 1
        assert events[0]["config_hash"] == frozen_config
        assert events[0]["outcome"] == "success"
        assert (await stack.sink.verify()).valid

    asyncio.run(scenario())


def test_fixture_sync_rerun_is_dedup_idempotent(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    stack = writer_stack
    state_store = ConnectorStateStore()

    async def scenario() -> None:
        first = await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
        )
        assert first.report.written == 3

        second = await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
        )
        assert second.report.written == 0
        assert second.report.skipped_unchanged == 3

    asyncio.run(scenario())


def test_fixture_sync_marks_the_source_free_lane_only_under_the_registry_grant(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """015A0-ENFORCEMENT: the server authors the class, the connector never does."""
    stack = writer_stack

    async def scenario() -> None:
        await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=ConnectorStateStore(),
            audit_sink=stack.sink,
            actor="connector:test",
            source_free=True,
        )
        marked = (await stack.store.get([_item_id("acme-crm/contacts", "c1")]))[0]
        assert marked.metadata[SOURCE_STATUS_METADATA_KEY] == NO_SOURCE_STATUS

        await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=ConnectorStateStore(),
            audit_sink=stack.sink,
            actor="connector:test",
        )
        unmarked = (await stack.store.get([_item_id("acme-crm/contacts", "c1")]))[0]
        assert SOURCE_STATUS_METADATA_KEY not in unmarked.metadata

    asyncio.run(scenario())


def test_the_mapper_revision_rewrites_a_corpus_ingested_before_the_class_existed(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    """The governed backfill: an ordinary sync rewrites, then goes idempotent.

    A ledger holding the PRE-revision content hash (what a corpus ingested
    before ``source_status`` existed leaves behind) must not report those records
    unchanged — otherwise the fix would only ever reach newly created records and
    the retained corpus would stay permanently unservable. No operator writes a
    row and no state file is deleted; the next scheduled sync does it.
    """
    stack = writer_stack
    state_store = ConnectorStateStore()
    fixture = json.loads(FIXTURE_SOURCE_PATH.read_text(encoding="utf-8"))
    for record in fixture["records"]:
        state_store.mark(
            _SOURCE,
            record["source_id"],
            record["record_id"],
            hashlib.sha256(canonical_payload_json(record["payload"]).encode()).hexdigest(),
        )

    async def scenario() -> None:
        rewritten = await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            source_free=True,
        )
        assert (rewritten.report.written, rewritten.report.skipped_unchanged) == (3, 0)
        stamped = (await stack.store.get([_item_id("acme-crm/contacts", "c1")]))[0]
        assert stamped.metadata[SOURCE_STATUS_METADATA_KEY] == NO_SOURCE_STATUS

        settled = await run_fixture_sync(
            _settings(),
            writer=stack.writer,
            state_store=state_store,
            audit_sink=stack.sink,
            actor="connector:test",
            source_free=True,
        )
        assert (settled.report.written, settled.report.skipped_unchanged) == (0, 3)

    asyncio.run(scenario())
