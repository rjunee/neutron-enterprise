# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKD6 atomicity proof: a governed KnowledgeWrite is store+audit or NEITHER.

Closes the Checkpoint-D P1 at the writer seam itself (protecting ALL callers of
the frozen ``KnowledgeWrite`` Protocol, not just ``promote()``): when a governed
write's per-item audit append FAILS after the store mutation, the writer unwinds
its own store write via the governed park-before-delete discipline and raises the
typed :class:`KnowledgeAuditError`, so no caller can ever observe a durable+
retrievable chunk without a matching audit record under a single fault.

Real seams, no mock past the AuditSink: real :memory: Qdrant + real sqlite
quarantine + a real :class:`PostgresAuditSink` (on sqlite) as the DELEGATE — the
only double is the ``_FlakyAuditSink`` wrapper that injects the sink outage at the
frozen ``AuditSink`` seam. Async is exercised via ``asyncio.run`` (pytest-asyncio
is forbidden); every test requests the ``frozen_config`` fixture.
"""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.audit.store import ne_audit_log
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.write import (
    KnowledgeAuditError,
    KnowledgeWriter,
    OutboxShapeViolationSink,
    ProvenanceClass,
    SqliteWriteQuarantine,
)
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.contracts.knowledge import KnowledgeItem
from ne.events import ensure_outbox_schema

from .conftest import NOTE_CONTRACT


class _FlakyAuditSink:
    """Wraps a real ``AuditSink``; fails ``append`` for a chosen action on chosen
    matching-call indices, delegating everything else (and ``verify``).

    Mirrors the async signatures of the ne-runtime ``RecordingAuditSink`` double so
    pyright structurally accepts it as an ``AuditSink``.

    * ``fail_times`` — fail the FIRST N matching-action appends (``None`` = always,
      unless ``fail_only_call`` is set); count only matching-action calls.
    * ``fail_only_call`` — 1-based index of the ONE matching-action append to fail
      (lets a test pass item 1's audit and fail item 2's within one batch).
    """

    def __init__(
        self,
        delegate: PostgresAuditSink,
        *,
        fail_action: str,
        fail_times: int | None = None,
        fail_only_call: int | None = None,
    ) -> None:
        self._delegate = delegate
        self._fail_action = fail_action
        self._fail_times = fail_times
        self._fail_only_call = fail_only_call
        self._matched = 0

    def _should_fail(self, n: int) -> bool:
        if self._fail_only_call is not None:
            return n == self._fail_only_call
        if self._fail_times is None:
            return True
        return n <= self._fail_times

    async def append(self, event: AuditEventLike) -> None:
        if getattr(event, "action", "") == self._fail_action:
            self._matched += 1
            if self._should_fail(self._matched):
                raise RuntimeError(f"audit sink is down for {self._fail_action}")
        await self._delegate.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return await self._delegate.verify(start=start, end=end)


@dataclass
class _FlakyStack:
    writer: KnowledgeWriter
    store: QdrantChunkStore
    quarantine: SqliteWriteQuarantine
    engine: sa.Engine
    sink: _FlakyAuditSink


def _flaky_stack(
    tmp_path: Path,
    *,
    fail_action: str,
    fail_times: int | None = None,
    fail_only_call: int | None = None,
    default_provenance_class: ProvenanceClass = ProvenanceClass.CURATED,
) -> _FlakyStack:
    client = AsyncQdrantClient(":memory:")
    store = QdrantChunkStore(
        client, f"kw_{uuid.uuid4().hex}", HashEmbedder(32), HashSparseEncoder()
    )
    asyncio.run(store.ensure_collection())
    quarantine = SqliteWriteQuarantine(tmp_path / f"q_{uuid.uuid4().hex}.db")
    engine = sa.create_engine("sqlite://")
    ensure_outbox_schema(engine)
    ensure_audit_schema(engine)
    sink = _FlakyAuditSink(
        PostgresAuditSink(engine),
        fail_action=fail_action,
        fail_times=fail_times,
        fail_only_call=fail_only_call,
    )
    writer = KnowledgeWriter(
        store,
        quarantine,
        {NOTE_CONTRACT.name: NOTE_CONTRACT},
        default_shape=NOTE_CONTRACT.name,
        actor="user:curator@acme.com",
        default_provenance_class=default_provenance_class,
        audit_sink=sink,
        violation_sink=OutboxShapeViolationSink(engine),
    )
    return _FlakyStack(writer=writer, store=store, quarantine=quarantine, engine=engine, sink=sink)


def _audit_rows(engine: sa.Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        rows = conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings().all()
    return [dict(r["payload"]) for r in rows]


def _rows_for(engine: sa.Engine, resource: str) -> list[dict[str, object]]:
    return [r for r in _audit_rows(engine) if r.get("resource") == resource]


def test_upsert_audit_failure_unwinds_store_write_atomically(
    tmp_path: Path, frozen_config: str
) -> None:
    stack = _flaky_stack(tmp_path, fail_action="knowledge.upsert")

    async def scenario() -> None:
        with pytest.raises(KnowledgeAuditError) as exc_info:
            await stack.writer.upsert(
                [KnowledgeItem(item_id="a1", content="body", provenance="src")]
            )
        assert exc_info.value.action == "knowledge.upsert"
        # store+audit atomic: the durable store write was unwound.
        assert await stack.store.get(["a1"]) == []
        # The FULL payload is parked (never lost).
        parked = stack.quarantine.get("a1")
        assert parked is not None
        assert parked.payload["content"] == "body"
        assert parked.payload["item_id"] == "a1"
        # The unwind was audited (knowledge.quarantine), and NO success upsert row.
        rows = _rows_for(stack.engine, "knowledge://a1")
        assert any(r["action"] == "knowledge.quarantine" for r in rows)
        assert not any(
            r["action"] == "knowledge.upsert" and r["outcome"] == "success" for r in rows
        )

    asyncio.run(scenario())


def test_upsert_partial_audit_failure_unwinds_only_unaudited_items(
    tmp_path: Path, frozen_config: str
) -> None:
    # Fail exactly the 2nd knowledge.upsert audit append: item 1 stays; 2 and 3 unwind.
    stack = _flaky_stack(tmp_path, fail_action="knowledge.upsert", fail_only_call=2)

    async def scenario() -> None:
        items = [
            KnowledgeItem(item_id="p1", content="one", provenance="src"),
            KnowledgeItem(item_id="p2", content="two", provenance="src"),
            KnowledgeItem(item_id="p3", content="three", provenance="src"),
        ]
        with pytest.raises(KnowledgeAuditError) as exc_info:
            await stack.writer.upsert(items)
        assert exc_info.value.audited == ("p1",)
        assert exc_info.value.unwound == ("p2", "p3")
        # item 1 remains retrievable WITH its audit row.
        got = await stack.store.get(["p1"])
        assert len(got) == 1 and got[0].text == "one"
        assert any(
            r["action"] == "knowledge.upsert" and r["outcome"] == "success"
            for r in _rows_for(stack.engine, "knowledge://p1")
        )
        # items 2 and 3 are parked and gone from the store.
        assert await stack.store.get(["p2"]) == []
        assert await stack.store.get(["p3"]) == []
        assert stack.quarantine.get("p2") is not None
        assert stack.quarantine.get("p3") is not None

    asyncio.run(scenario())


def test_same_id_reembed_audit_failure_restores_exact_governed_point(
    tmp_path: Path, frozen_config: str
) -> None:
    stack = _flaky_stack(tmp_path, fail_action="knowledge.upsert", fail_only_call=2)

    async def scenario() -> None:
        await stack.writer.upsert(
            [KnowledgeItem(item_id="same", content="original", provenance="old")]
        )
        before = await stack.store._client.retrieve(  # type: ignore[attr-defined]
            collection_name=stack.store._collection,  # type: ignore[attr-defined]
            ids=[str(uuid.uuid5(uuid.NAMESPACE_URL, "ne-chunk:same"))],
            with_payload=True,
            with_vectors=True,
        )
        snapshot = deepcopy(before)

        with pytest.raises(KnowledgeAuditError):
            await stack.writer.upsert(
                [KnowledgeItem(item_id="same", content="replacement", provenance="new")]
            )

        after = await stack.store._client.retrieve(  # type: ignore[attr-defined]
            collection_name=stack.store._collection,  # type: ignore[attr-defined]
            ids=[str(uuid.uuid5(uuid.NAMESPACE_URL, "ne-chunk:same"))],
            with_payload=True,
            with_vectors=True,
        )
        assert after == snapshot
        assert (await stack.store.get(["same"]))[0].text == "original"

    asyncio.run(scenario())


def test_upsert_retry_after_sink_recovery_heals_idempotently(
    tmp_path: Path, frozen_config: str
) -> None:
    stack = _flaky_stack(tmp_path, fail_action="knowledge.upsert", fail_times=1)

    async def scenario() -> None:
        item = KnowledgeItem(item_id="r1", content="body", provenance="src")
        with pytest.raises(KnowledgeAuditError):
            await stack.writer.upsert([item])
        # Same upsert retried after the sink recovers.
        receipt = await stack.writer.upsert([item])
        assert receipt.accepted == ("r1",)
        got = await stack.store.get(["r1"])
        assert len(got) == 1 and got[0].text == "body"
        success_rows = [
            r
            for r in _rows_for(stack.engine, "knowledge://r1")
            if r["action"] == "knowledge.upsert" and r["outcome"] == "success"
        ]
        assert len(success_rows) == 1
        assert stack.quarantine.get("r1") is None

    asyncio.run(scenario())


def test_curate_audit_failure_restores_original_chunk_and_parks_patch(
    tmp_path: Path, frozen_config: str
) -> None:
    stack = _flaky_stack(tmp_path, fail_action="knowledge.curate")

    async def scenario() -> None:
        await stack.writer.upsert(
            [KnowledgeItem(item_id="c1", content="original", provenance="src")]
        )
        with pytest.raises(KnowledgeAuditError) as exc_info:
            await stack.writer.curate("c1", {"content": "edited"}, provenance="cur")
        assert exc_info.value.action == "knowledge.curate"
        assert exc_info.value.unwound == ("c1",)
        # The ORIGINAL content is restored (store never holds unaudited-modified content).
        got = await stack.store.get(["c1"])
        assert len(got) == 1 and got[0].text == "original"
        # The attempted patch is parked (curator work never lost).
        parked = stack.quarantine.get("c1")
        assert parked is not None
        assert parked.payload["content"] == "edited"

    asyncio.run(scenario())


def test_curate_retry_after_recovery_applies_patch_and_clears_lane(
    tmp_path: Path, frozen_config: str
) -> None:
    stack = _flaky_stack(tmp_path, fail_action="knowledge.curate", fail_times=1)

    async def scenario() -> None:
        await stack.writer.upsert(
            [KnowledgeItem(item_id="c2", content="original", provenance="src")]
        )
        with pytest.raises(KnowledgeAuditError):
            await stack.writer.curate("c2", {"content": "edited"}, provenance="cur")
        # Retry the same curate after the sink recovers.
        await stack.writer.curate("c2", {"content": "edited"}, provenance="cur")
        got = await stack.store.get(["c2"])
        assert len(got) == 1 and got[0].text == "edited"
        assert any(
            r["action"] == "knowledge.curate" and r["outcome"] == "success"
            for r in _rows_for(stack.engine, "knowledge://c2")
        )
        # The parked failed-curate row was cleared by the successful retry.
        assert stack.quarantine.get("c2") is None

    asyncio.run(scenario())


def test_quarantine_audit_failure_is_failsafe_parked_deleted_typed(
    tmp_path: Path, frozen_config: str
) -> None:
    stack = _flaky_stack(tmp_path, fail_action="knowledge.quarantine")

    async def scenario() -> None:
        await stack.writer.upsert([KnowledgeItem(item_id="q1", content="body", provenance="src")])
        with pytest.raises(KnowledgeAuditError) as exc_info:
            await stack.writer.quarantine("q1", "policy pull")
        assert exc_info.value.action == "knowledge.quarantine"
        # Fail-safe direction: park+delete PRECEDE the audit, so the item is
        # deleted from the store AND parked (deliberately NOT unwound).
        assert await stack.store.get(["q1"]) == []
        assert stack.quarantine.get("q1") is not None

    asyncio.run(scenario())
