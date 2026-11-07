# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Unit + end-to-end tests for the NE-043 AND-combine filter fix.

``and_combine_filters`` intersects a caller filter with an ACL filter on key
collision (never overwrite). The two :memory:-Qdrant integration tests prove
the fix end-to-end; ``test_search_disjoint_collision_returns_nothing`` FAILS
against the old ``merged |= acl`` overwrite, pinning the new semantics.
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
from ne.brain.retriever import HybridRetriever, and_combine_filters
from ne.brain.testing.embedding import HashEmbedder
from ne.contracts.types import Chunk, Principal, SparseVector

# --- unit tests over and_combine_filters -----------------------------------


def test_non_colliding_keys_pass_through_both() -> None:
    result = and_combine_filters({"team": "sales"}, {"acl": ["group-a"]})
    assert result == {"team": "sales", "acl": ["group-a"]}


def test_list_list_collision_intersects_preserving_caller_order() -> None:
    result = and_combine_filters(
        {"acl": ["group-b", "group-a", "group-c"]}, {"acl": ["group-a", "group-b"]}
    )
    assert result == {"acl": ["group-b", "group-a"]}


def test_scalar_in_acl_list_keeps_scalar() -> None:
    result = and_combine_filters({"acl": "group-a"}, {"acl": ["group-a", "group-b"]})
    assert result == {"acl": "group-a"}


def test_scalar_scalar_equal_keeps_scalar() -> None:
    result = and_combine_filters({"acl": "group-a"}, {"acl": "group-a"})
    assert result == {"acl": "group-a"}


def test_scalar_scalar_unequal_yields_match_nothing() -> None:
    result = and_combine_filters({"acl": "group-a"}, {"acl": "group-b"})
    assert result == {"acl": []}


def test_disjoint_lists_yield_match_nothing() -> None:
    result = and_combine_filters({"acl": ["group-a"]}, {"acl": ["group-b"]})
    assert result == {"acl": []}


def test_unsupported_value_type_raises_value_error() -> None:
    with pytest.raises(ValueError):
        and_combine_filters({"acl": {"nested": "map"}}, {"acl": ["group-a"]})
    with pytest.raises(ValueError):
        and_combine_filters({"acl": ["group-a"]}, {"acl": {"nested": "map"}})


def test_none_or_empty_acl_returns_caller_unchanged() -> None:
    assert and_combine_filters({"acl": ["group-a"]}, None) == {"acl": ["group-a"]}
    assert and_combine_filters({"acl": ["group-a"]}, {}) == {"acl": ["group-a"]}
    assert and_combine_filters(None, None) == {}


# --- NE-087 reserved-namespace ($any) grammar (T-5 / T-7) -------------------

_ANY_GROUP = {"$any": [{"acl": ["group-a"]}, {"document_id": ["d1", "d2"]}]}


def test_caller_dollar_any_key_rejected() -> None:
    # A caller OR-group could widen the AND-composition -> rejected fail-closed.
    with pytest.raises(ValueError, match="reserved filter key"):
        and_combine_filters({"$any": [{"acl": ["x"]}]}, {"acl": ["group-a"]})


def test_caller_any_dollar_prefixed_key_rejected() -> None:
    for bad in ("$or", "$foo", "$document_id"):
        with pytest.raises(ValueError, match="reserved filter key"):
            and_combine_filters({bad: ["x"]}, {"acl": ["group-a"]})


def test_acl_any_group_passes_through_verbatim() -> None:
    # In the acl position $any is a valid, verbatim OR-group.
    result = and_combine_filters(None, _ANY_GROUP)
    assert result == _ANY_GROUP


def test_acl_any_group_coexists_with_caller_plain_key_intersecting_outside() -> None:
    # T-5: a caller plain key AND-combines OUTSIDE the group (narrowing only); the
    # $any group is untouched and never intersected.
    result = and_combine_filters({"metadata.team": "sales"}, _ANY_GROUP)
    assert result == {"metadata.team": "sales", **_ANY_GROUP}


def test_acl_any_caller_collision_impossible_document_id_still_narrows() -> None:
    # A caller may name a plain `document_id`/`acl` key: it AND-combines OUTSIDE
    # the group (intersection), so it can only narrow, never widen either leg.
    result = and_combine_filters({"document_id": ["d1", "d9"]}, _ANY_GROUP)
    assert result["document_id"] == ["d1", "d9"]
    assert result["$any"] == _ANY_GROUP["$any"]


def test_unknown_reserved_acl_key_rejected() -> None:
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        and_combine_filters(None, {"$other": [{"acl": ["x"]}]})


def test_malformed_any_group_values_raise() -> None:
    for bad in (
        {"$any": []},  # empty OR-group
        {"$any": "notalist"},  # not a list
        {"$any": [{}]},  # empty sub-mapping
        {"$any": ["notamap"]},  # non-mapping element
        {"$any": [{"$any": [{"acl": ["x"]}]}]},  # nested reserved key
    ):
        with pytest.raises(ValueError):
            and_combine_filters(None, bad)


# --- end-to-end integration against real :memory: Qdrant -------------------


class _StubSparseEncoder:
    @property
    def model_id(self) -> str:
        return "stub-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [
            SparseVector(indices=(zlib.crc32(t.encode()) % 4096,), values=(1.0,)) for t in texts
        ]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return await self.encode(texts)


_a = Chunk(chunk_id="a-only", document_id="d1", text="alpha content", acl=("group-a",))
_ab = Chunk(chunk_id="a-and-b", document_id="d1", text="beta content", acl=("group-a", "group-b"))
_b = Chunk(chunk_id="b-only", document_id="d1", text="gamma content", acl=("group-b",))

_principal = Principal(id="u1", groups=("group-a",))


def _acl_hook(mapping: Mapping[str, Any]) -> Any:
    return lambda _principal: mapping


def _build(acl_filters: Any) -> HybridRetriever:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(client, f"fac_{uuid.uuid4().hex}", embedder, _StubSparseEncoder())

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks([_a, _ab, _b])

    asyncio.run(setup())
    return HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=acl_filters, audit_sink=None
    )


def test_search_key_collision_intersects_caller_and_acl() -> None:
    retriever = _build(_acl_hook({"acl": ["group-a"]}))

    async def scenario() -> None:
        result = await retriever.search(
            "content", principal=_principal, top_k=10, filters={"acl": ["group-a", "group-b"]}
        )
        ids = {sc.chunk.chunk_id for sc in result.chunks}
        # caller {a,b} ∩ acl {a} = {a}; only group-a-visible chunks survive.
        assert ids == {"a-only", "a-and-b"}

    asyncio.run(scenario())


def test_search_disjoint_collision_returns_nothing() -> None:
    retriever = _build(_acl_hook({"acl": ["group-a"]}))

    async def scenario() -> None:
        result = await retriever.search(
            "content", principal=_principal, top_k=10, filters={"acl": ["group-b"]}
        )
        # caller {b} ∩ acl {a} = {} -> match-nothing. Under the old
        # `merged |= acl` overwrite the acl {a} would have replaced {b} and
        # returned the group-a chunks, so this test pins the fix.
        assert result.chunks == ()

    asyncio.run(scenario())
