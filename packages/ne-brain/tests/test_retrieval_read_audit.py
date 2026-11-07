# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-164: read-path auditing on ``HybridRetriever.retrieve()``.

Mirrors the shipped write-path audit discipline (``ne.brain.write.writer``):
every completed ``retrieve()`` call, when an ``AuditSink`` is injected, emits
exactly ONE config_hash-stamped ``AuditEvent`` carrying the query SHAPE only
(candidate/returned counts + the ACL-trim delta) — never raw query content,
chunk ids, or ACL label values (C7).

Uses the REAL trimming seam (no mocking past it): a real ``PrincipalAclFilter``
over a static expander, and the REAL Qdrant ``:memory:`` server-side
``get_filtered`` fetch — the same fixture pattern as
``test_retriever_acl_seam.py``.
"""

from __future__ import annotations

import asyncio
import uuid
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.trimming import GroupExpander, PrincipalAclFilter
from ne.contracts import ConfigHashNotFrozenError
from ne.contracts.governance import AuditVerification
from ne.contracts.types import Chunk, Principal, SparseVector


class _StubSparseEncoder:
    """Deterministic, dependency-free sparse encoder (mirrors the ACL-seam suite)."""

    @property
    def model_id(self) -> str:
        return "stub-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [
            SparseVector(indices=(zlib.crc32(t.encode()) % 4096,), values=(1.0,)) for t in texts
        ]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return await self.encode(texts)


class _StaticExpander:
    """Real ``GroupExpander`` returning a fixed label set — the real trimming seam."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        return frozenset({"u1", "group-a"})


class RecordingAuditSink:
    """Test-double ``AuditSink`` that records every appended event verbatim.

    Typed ``list[Any]`` (not ``AuditEventLike``, the placeholder structural
    Protocol that only declares the base ``EventLike`` fields): tests assert
    on ``AuditEvent``-specific fields (``action``/``resource``/``outcome``/
    ``detail``) that ``new_audit_event`` always populates on the concrete
    object actually appended here.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


class _FailingAuditSink:
    """An ``AuditSink`` whose ``append`` always raises (fail-closed test double)."""

    async def append(self, event: Any) -> None:
        raise RuntimeError("audit store unreachable")

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


class _ExplodingStore:
    """Every async method raises — proves the store is never reached (guardian pattern)."""

    _MSG = "store reached despite match-nothing ACL"

    async def query(self, **kwargs: Any) -> Sequence[Any]:
        raise AssertionError(self._MSG)

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        raise AssertionError(self._MSG)

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        raise AssertionError(self._MSG)

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        raise AssertionError(self._MSG)

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        raise AssertionError(self._MSG)


allowed1 = Chunk(
    chunk_id="open-1",
    document_id="d1",
    text="quarterly revenue report",
    acl=("group-a",),
)
allowed2 = Chunk(
    chunk_id="open-2",
    document_id="d1",
    text="annual budget summary",
    acl=("group-a", "group-b"),
)
denied = Chunk(
    chunk_id="secret-1",
    document_id="d2",
    text="secret acquisition target list",
    acl=("group-b",),
)

principal = Principal(id="u1", groups=("group-a",))


def _build(
    *,
    audit_sink: Any = None,
    acl_filters: Any = "real",
) -> tuple[QdrantChunkStore, HybridRetriever]:
    """Build a real Qdrant(:memory:) store + HybridRetriever.

    ``acl_filters="real"`` (the default) wires the REAL ``PrincipalAclFilter``
    over a real (static) ``GroupExpander`` — the trimming seam is never mocked.
    Pass an explicit callable/None to override for the disabled/match-nothing
    scenarios.
    """
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    collection = f"read_audit_{uuid.uuid4().hex}"
    store = QdrantChunkStore(client, collection, embedder, _StubSparseEncoder())

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks([allowed1, allowed2, denied])

    asyncio.run(setup())
    resolved_acl: Any = (
        PrincipalAclFilter(_StaticExpander()) if acl_filters == "real" else acl_filters
    )
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=resolved_acl, audit_sink=audit_sink
    )
    return store, retriever


def test_grounded_retrieve_emits_read_audit_with_counts_delta_and_trace_id(
    frozen_config: str,
) -> None:
    sink = RecordingAuditSink()
    _, retriever = _build(audit_sink=sink)
    trace_id = "a" * 32

    async def scenario() -> None:
        result = await retriever.retrieve(
            ["open-1", "open-2", "secret-1"], principal=principal, trace_id=trace_id
        )
        assert {sc.chunk.chunk_id for sc in result.chunks} == {"open-1", "open-2"}
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.event_type == "audit"
        assert event.action == "knowledge.retrieve"
        assert event.resource == "knowledge://chunks"
        assert event.outcome == "success"
        assert event.actor == "u1"
        assert event.detail == "candidates=3 returned=2 acl_trim_delta=1"
        assert event.trace_id == trace_id
        assert event.config_hash == frozen_config

    asyncio.run(scenario())


def test_invisible_chunk_increments_trim_delta_and_leaks_no_label(frozen_config: str) -> None:
    sink = RecordingAuditSink()
    _, retriever = _build(audit_sink=sink)

    async def scenario() -> None:
        # All-visible retrieval of the principal's own two chunks: delta 0.
        await retriever.retrieve(["open-1", "open-2"], principal=principal)
        visible_event = sink.events[-1]
        assert visible_event.detail == "candidates=2 returned=2 acl_trim_delta=0"

        # Adding the denied chunk: the doc is absent from the result and the
        # delta is exactly 1 higher.
        result = await retriever.retrieve(["open-1", "open-2", "secret-1"], principal=principal)
        assert "secret-1" not in {sc.chunk.chunk_id for sc in result.chunks}
        trimmed_event = sink.events[-1]
        assert trimmed_event.detail == "candidates=3 returned=2 acl_trim_delta=1"

        for event in sink.events:
            payload = event.model_dump_json()
            assert "group-a" not in payload
            assert "group-b" not in payload
            assert "secret" not in payload
            assert "revenue" not in payload

    asyncio.run(scenario())


def test_event_payload_carries_no_chunk_ids_or_query_content(frozen_config: str) -> None:
    sink = RecordingAuditSink()
    _, retriever = _build(audit_sink=sink)

    async def scenario() -> None:
        await retriever.retrieve(["open-1", "open-2", "secret-1"], principal=principal)
        payload = sink.events[-1].model_dump_json()
        for leaked in ("open-1", "open-2", "secret-1"):
            assert leaked not in payload

    asyncio.run(scenario())


def test_match_nothing_principal_audits_full_trim_with_no_store_call(frozen_config: str) -> None:
    sink = RecordingAuditSink()
    store = _ExplodingStore()
    embedder = HashEmbedder(32)
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=lambda p: {}, audit_sink=sink
    )

    async def scenario() -> None:
        result = await retriever.retrieve(["c1", "c2"], principal=principal)
        assert result.chunks == ()
        assert len(sink.events) == 1
        assert sink.events[0].detail == "candidates=2 returned=0 acl_trim_delta=2"

    asyncio.run(scenario())


def test_trimming_disabled_reports_zero_acl_trim_delta(frozen_config: str) -> None:
    sink = RecordingAuditSink()
    _, retriever = _build(audit_sink=sink, acl_filters=None)

    async def scenario() -> None:
        result = await retriever.retrieve(["open-1", "does-not-exist"], principal=principal)
        assert [sc.chunk.chunk_id for sc in result.chunks] == ["open-1"]
        assert len(sink.events) == 1
        # A missing id with trimming DISABLED is not an ACL trim: delta stays 0.
        assert sink.events[0].detail == "candidates=2 returned=1 acl_trim_delta=0"

    asyncio.run(scenario())


def test_no_audit_sink_composition_emits_nothing_and_behaves_identically() -> None:
    # Back-compat: explicit audit_sink=None construction (NE-VG-2: audit_sink is
    # now a REQUIRED keyword-only parameter on HybridRetriever — there is no
    # more "default construction"; every sink-less caller passes None
    # explicitly) is untouched — no config_hash freeze needed either, since no
    # event machinery is touched.
    _, retriever = _build(audit_sink=None)

    async def scenario() -> None:
        result = await retriever.retrieve(["open-1", "secret-1"], principal=principal)
        assert {sc.chunk.chunk_id for sc in result.chunks} == {"open-1"}

    asyncio.run(scenario())


def test_audit_append_failure_fails_closed_no_result(frozen_config: str) -> None:
    _, retriever = _build(audit_sink=_FailingAuditSink())

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await retriever.retrieve(["open-1"], principal=principal)

    asyncio.run(scenario())


def test_unfrozen_config_hash_fails_loud() -> None:
    # No frozen_config fixture used here — config_hash is deliberately NOT frozen.
    sink = RecordingAuditSink()
    _, retriever = _build(audit_sink=sink)

    async def scenario() -> None:
        with pytest.raises(ConfigHashNotFrozenError):
            await retriever.retrieve(["open-1"], principal=principal)
        assert sink.events == []

    asyncio.run(scenario())


def test_group_expander_protocol_satisfied_by_static_expander() -> None:
    # Sanity: _StaticExpander is a real, structural GroupExpander (no mocking
    # past the trimming seam) — proven by isinstance against the runtime_checkable
    # Protocol used in ne.brain.trimming.
    assert isinstance(_StaticExpander(), GroupExpander)
