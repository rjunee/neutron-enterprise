# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-seam unit tests for QdrantChunkStore helpers (no server round trip)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from qdrant_client import AsyncQdrantClient, models

from ne.brain.qdrant import QdrantChunkStore, _build_filter, point_id_for
from ne.brain.testing.embedding import HashEmbedder


def test_point_id_for_is_deterministic() -> None:
    assert point_id_for("chunk-1") == point_id_for("chunk-1")


def test_point_id_for_is_a_valid_uuid() -> None:
    result = point_id_for("chunk-1")
    parsed = uuid.UUID(result)
    assert str(parsed) == result


def test_point_id_for_differs_across_chunk_ids() -> None:
    assert point_id_for("chunk-1") != point_id_for("chunk-2")


def test_build_filter_none_when_empty() -> None:
    assert _build_filter(None) is None
    assert _build_filter({}) is None


def test_build_filter_str_becomes_match_value() -> None:
    result = _build_filter({"metadata.team": "alpha"})
    assert result is not None
    assert isinstance(result.must, list)
    condition = result.must[0]
    assert isinstance(condition, models.FieldCondition)
    assert condition.key == "metadata.team"
    assert isinstance(condition.match, models.MatchValue)
    assert condition.match.value == "alpha"


def test_build_filter_list_becomes_match_any() -> None:
    result = _build_filter({"acl": ["group-a", "group-b"]})
    assert result is not None
    assert isinstance(result.must, list)
    condition = result.must[0]
    assert isinstance(condition, models.FieldCondition)
    assert isinstance(condition.match, models.MatchAny)
    assert condition.match.any == ["group-a", "group-b"]


def test_build_filter_unsupported_type_raises() -> None:
    with pytest.raises(ValueError):
        _build_filter({"bad": {"nested": "dict"}})


class _NullSparseEncoder:
    @property
    def model_id(self) -> str:
        return "null"

    async def encode(self, texts):  # type: ignore[no-untyped-def]
        return []


def test_query_raises_when_both_signals_none() -> None:
    async def scenario() -> None:
        client = AsyncQdrantClient(":memory:")
        store = QdrantChunkStore(client, "unit-test", HashEmbedder(8), _NullSparseEncoder())
        await store.ensure_collection()
        with pytest.raises(ValueError):
            await store.query(dense=None, sparse=None)

    asyncio.run(scenario())


class _FakeAsyncQdrantClient:
    """Minimal stand-in; only identity matters for the per-loop client law."""


def _store_with(client: object) -> QdrantChunkStore:
    from ne.brain.sparse import FastEmbedBM25Encoder

    return QdrantChunkStore(
        client,  # type: ignore[arg-type]
        "collection",
        HashEmbedder(),
        FastEmbedBM25Encoder(),
    )


def test_factory_mode_builds_one_client_per_event_loop() -> None:
    """2026-08-28 cron-offload incident: the AsyncQdrantClient is loop-affine.

    In factory mode the store must hand each event loop its OWN client (the
    worker's DBOS turn loop and its persistent cron-tick loop may never share
    one), and must reuse that client on the same loop.
    """
    built: list[_FakeAsyncQdrantClient] = []

    def factory() -> _FakeAsyncQdrantClient:
        client = _FakeAsyncQdrantClient()
        built.append(client)
        return client

    store = _store_with(factory)

    async def use_twice() -> tuple[object, object]:
        return store._c(), store._c()

    first_a, first_b = asyncio.run(use_twice())
    second_a, second_b = asyncio.run(use_twice())

    assert first_a is first_b, "same loop must reuse its client"
    assert second_a is second_b
    assert first_a is not second_a, "a different loop must get its own client"
    assert len(built) == 2


def test_instance_mode_keeps_the_single_injected_client() -> None:
    injected = _FakeAsyncQdrantClient()
    store = _store_with(injected)

    async def use() -> object:
        return store._c()

    assert asyncio.run(use()) is injected
    assert asyncio.run(use()) is injected
