# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Proves ``HybridRetriever.retrieve()`` honors the ACL seam identically to
``search()`` — the NE-021 Codex P2 regression suite.

Before this fix, ``retrieve()`` always used the unscoped ``ChunkStore.get()``
and ignored ``acl_filters`` entirely, so a caller who knew a denied chunk's id
could always fetch it via ``retrieve()`` even though ``search()`` would never
surface it — a latent authorization bypass once NE-043 makes the
``acl_filters`` hook real.

CLOSED ACL-hook contract (NE-CKC2, seeds the NE-043 C7 guardian): the
``acl_filters`` hook has three fail-closed states — ``None`` (hook absent / hook
returns None) = trimming disabled; a non-None empty mapping ``{}`` = MATCH-NOTHING
(empty result, no store call, on BOTH ``search`` and ``retrieve``); a non-empty
mapping = server-side filter. The previously-green
``test_retrieve_empty_acl_mapping_falls_back_to_unscoped_get`` enshrined the
fail-OPEN (a groupless principal got the full corpus); it is INVERTED here.

Uses a deterministic in-test stub sparse encoder (NOT the ``bm25_encoder``
fixture) so this security suite ALWAYS runs in CI with no LIVE-PARK skip.
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
from ne.contracts.types import Chunk, Principal, SparseVector


class _StubSparseEncoder:
    """Deterministic, dependency-free sparse encoder for the ACL-seam suite."""

    @property
    def model_id(self) -> str:
        return "stub-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [
            SparseVector(indices=(zlib.crc32(t.encode()) % 4096,), values=(1.0,)) for t in texts
        ]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return await self.encode(texts)


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


def _group_acl(principal: Principal) -> Mapping[str, Any] | None:
    return {"acl": list(principal.groups)}


def _build(
    acl_filters: Any = None,
) -> tuple[QdrantChunkStore, HybridRetriever]:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    collection = f"acl_{uuid.uuid4().hex}"
    store = QdrantChunkStore(client, collection, embedder, _StubSparseEncoder())

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks([allowed1, allowed2, denied])

    asyncio.run(setup())
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=acl_filters, audit_sink=None
    )
    return store, retriever


def test_get_filtered_none_filters_matches_get() -> None:
    store, _ = _build()

    async def scenario() -> None:
        ids = ["open-1", "open-2", "secret-1"]
        expected = await store.get(ids)
        actual = await store.get_filtered(ids, filters=None)
        assert [c.chunk_id for c in actual] == [c.chunk_id for c in expected]

    asyncio.run(scenario())


def test_get_filtered_applies_filter_and_preserves_input_order() -> None:
    store, _ = _build()

    async def scenario() -> None:
        result = await store.get_filtered(
            ["open-2", "secret-1", "open-1", "does-not-exist"],
            filters={"acl": ["group-a"]},
        )
        assert [c.chunk_id for c in result] == ["open-2", "open-1"]

    asyncio.run(scenario())


def test_get_filtered_empty_ids_returns_empty() -> None:
    store, _ = _build()

    async def scenario() -> None:
        result = await store.get_filtered([], filters={"acl": ["group-a"]})
        assert result == []

    asyncio.run(scenario())


def test_retrieve_trims_chunk_excluded_by_acl() -> None:
    _, retriever = _build(acl_filters=_group_acl)

    async def scenario() -> None:
        result = await retriever.retrieve(["secret-1"], principal=principal)
        assert result.chunks == ()
        assert result.citations == ()

    asyncio.run(scenario())


def test_retrieve_returns_only_visible_subset_in_input_order() -> None:
    _, retriever = _build(acl_filters=_group_acl)

    async def scenario() -> None:
        result = await retriever.retrieve(["open-2", "secret-1", "open-1"], principal=principal)
        assert [sc.chunk.chunk_id for sc in result.chunks] == ["open-2", "open-1"]
        assert len(result.citations) == len(result.chunks)
        assert [c.chunk_id for c in result.citations] == ["open-2", "open-1"]

    asyncio.run(scenario())


def test_retrieve_and_search_trim_identically() -> None:
    _, retriever = _build(acl_filters=_group_acl)

    async def scenario() -> None:
        search_result = await retriever.search(
            "secret acquisition target list", principal=principal, top_k=10
        )
        assert "secret-1" not in [sc.chunk.chunk_id for sc in search_result.chunks]

        retrieve_result = await retriever.retrieve(["secret-1"], principal=principal)
        assert retrieve_result.chunks == ()

    asyncio.run(scenario())


def test_retrieve_untrimmed_when_acl_filters_none() -> None:
    _, retriever = _build(acl_filters=None)

    async def scenario() -> None:
        result = await retriever.retrieve(["open-1", "secret-1"], principal=principal)
        assert {sc.chunk.chunk_id for sc in result.chunks} == {"open-1", "secret-1"}

    asyncio.run(scenario())


def test_retrieve_empty_acl_mapping_matches_nothing() -> None:
    # CLOSED contract: a non-None {} means the principal has ZERO accessible
    # labels -> match-nothing on retrieve(), NOT an unscoped full-corpus fetch.
    _, retriever = _build(acl_filters=lambda p: {})

    async def scenario() -> None:
        result = await retriever.retrieve(["open-1", "secret-1"], principal=principal)
        assert result.chunks == ()
        assert result.citations == ()

    asyncio.run(scenario())


def test_search_empty_acl_mapping_matches_nothing() -> None:
    # The search() path had the identical fail-open — {} must match nothing there
    # too (empty result, no store query, no embedding).
    _, retriever = _build(acl_filters=lambda p: {})

    async def scenario() -> None:
        result = await retriever.search("quarterly revenue report", principal=principal)
        assert result.chunks == ()
        assert result.citations == ()

    asyncio.run(scenario())


def test_retrieve_empty_acl_mapping_matches_nothing_without_get_filtered() -> None:
    # Match-nothing short-circuits BEFORE the get_filtered capability check, so a
    # store lacking get_filtered returns EMPTY rather than raising NotImplementedError.
    store = _NoFilterChunkStore()
    embedder = HashEmbedder(32)
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=lambda p: {}, audit_sink=None
    )

    async def scenario() -> None:
        await store.upsert_chunks([allowed1])
        result = await retriever.retrieve(["open-1"], principal=principal)
        assert result.chunks == ()
        assert result.citations == ()

    asyncio.run(scenario())


class _NoFilterChunkStore:
    """Minimal in-test ChunkStore stub with NO ``get_filtered`` — proves fail-closed."""

    def __init__(self) -> None:
        self._chunks: dict[str, Chunk] = {}

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return [self._chunks[cid] for cid in chunk_ids if cid in self._chunks]

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[Any]:
        return []

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for cid in chunk_ids:
            self._chunks.pop(cid, None)


def test_retrieve_fails_closed_when_store_lacks_get_filtered() -> None:
    store = _NoFilterChunkStore()
    embedder = HashEmbedder(32)
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=_group_acl, audit_sink=None
    )

    async def scenario() -> None:
        await store.upsert_chunks([allowed1])
        with pytest.raises(NotImplementedError):
            await retriever.retrieve(["open-1"], principal=principal)

    asyncio.run(scenario())
