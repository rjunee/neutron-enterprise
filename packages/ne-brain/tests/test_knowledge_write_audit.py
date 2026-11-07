# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-023 auditability: every curate/write emits a config_hash-stamped AuditEvent.

Uses the REAL ``PostgresAuditSink`` on a sqlite engine (the established
ne-gateway/ne-audit test pattern — no mock past the seam).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write import (
    CuratedKnowledgeItem,
    KnowledgeWriteError,
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ShapeViolationSink,
    SinkRequiredError,
    SqliteWriteQuarantine,
)
from ne.contracts import ConfigHashNotFrozenError, reset_config_hash_for_testing
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import KnowledgeItem
from ne.events import ensure_outbox_schema

from .conftest import NOTE_CONTRACT, WriteStack


def _audit_rows(stack: WriteStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        rows = conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings().all()
    return [dict(r["payload"]) for r in rows]


def test_upsert_emits_audit_event_with_config_hash(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="a1", content="body", provenance="src")])
        rows = _audit_rows(stack)
        assert len(rows) == 1
        assert rows[0]["action"] == "knowledge.upsert"
        assert rows[0]["resource"] == "knowledge://a1"
        assert rows[0]["outcome"] == "success"
        assert rows[0]["config_hash"] == frozen_config

    asyncio.run(scenario())


def test_quarantined_write_emits_denied_audit_event(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="d1", content="", provenance="src")])
        rows = _audit_rows(stack)
        assert len(rows) == 1
        assert rows[0]["action"] == "knowledge.upsert"
        assert rows[0]["outcome"] == "denied"

    asyncio.run(scenario())


def test_curate_emits_audit_event(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="c1", content="body", provenance="src")])
        await stack.writer.curate("c1", {"content": "edited"}, provenance="cur")
        actions = [r["action"] for r in _audit_rows(stack)]
        assert actions == ["knowledge.upsert", "knowledge.curate"]

    asyncio.run(scenario())


def test_manual_quarantine_parks_item_and_audits(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    stack = write_stack()

    async def scenario() -> None:
        await stack.writer.upsert(
            [CuratedKnowledgeItem(item_id="m1", content="body", provenance="src")]
        )
        await stack.writer.quarantine("m1", "policy pull")
        # Park-before-delete: parked AND gone from the store.
        assert stack.quarantine.get("m1") is not None
        assert await stack.store.get(["m1"]) == []
        rows = _audit_rows(stack)
        assert rows[-1]["action"] == "knowledge.quarantine"
        assert rows[-1]["outcome"] == "success"

    asyncio.run(scenario())


def _bare_write_components(
    tmp_path: Path,
) -> tuple[QdrantChunkStore, SqliteWriteQuarantine, sa.Engine]:
    """Build the real store/quarantine/engine seams a KnowledgeWriter needs.

    No config freeze is needed — construction emits no events.
    """
    client = AsyncQdrantClient(":memory:")
    store = QdrantChunkStore(
        client, f"kw_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
    )
    asyncio.run(store.ensure_collection())
    quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)
    return store, quarantine, engine


def test_construction_without_audit_sink_raises(tmp_path: Path) -> None:
    store, quarantine, engine = _bare_write_components(tmp_path)
    with pytest.raises(SinkRequiredError):
        KnowledgeWriter(
            store,
            quarantine,
            {NOTE_CONTRACT.name: NOTE_CONTRACT},
            default_shape=NOTE_CONTRACT.name,
            actor="test",
            audit_sink=cast(AuditSink, None),
            violation_sink=OutboxShapeViolationSink(engine),
        )


def test_construction_without_violation_sink_raises(tmp_path: Path) -> None:
    store, quarantine, engine = _bare_write_components(tmp_path)
    assert issubclass(SinkRequiredError, KnowledgeWriteError)
    with pytest.raises(SinkRequiredError):
        KnowledgeWriter(
            store,
            quarantine,
            {NOTE_CONTRACT.name: NOTE_CONTRACT},
            default_shape=NOTE_CONTRACT.name,
            actor="test",
            audit_sink=PostgresAuditSink(engine),
            violation_sink=cast(ShapeViolationSink, None),
        )


def test_unfrozen_config_hash_fails_loud(write_stack: Callable[..., WriteStack]) -> None:
    reset_config_hash_for_testing()
    stack = write_stack()

    async def scenario() -> None:
        with pytest.raises(ConfigHashNotFrozenError):
            await stack.writer.upsert(
                [KnowledgeItem(item_id="z1", content="body", provenance="src")]
            )
        # NE-CKD6: the unwind runs for the config-hash failure mode too — the store
        # write is rolled back and the payload parked — while the error type stays
        # the distinct, loud ConfigHashNotFrozenError (NOT wrapped as KnowledgeAuditError).
        assert await stack.store.get(["z1"]) == []
        assert stack.quarantine.get("z1") is not None

    asyncio.run(scenario())
