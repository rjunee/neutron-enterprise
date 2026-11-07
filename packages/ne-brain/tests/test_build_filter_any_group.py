# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-087 ``$any`` OR-group compilation in ``ne.brain.qdrant._build_filter``.

Structural assertions on the built ``models.Filter`` objects — no server. The
``$any`` grammar compiles to an outer ``must`` carrying a nested
``Filter(should=[Filter(must=[...]), ...])`` OR-group, plain keys sit alongside
as sibling ``must`` conditions, and every reserved/malformed shape fails closed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient, models

from ne.brain.qdrant import QdrantChunkStore, _build_filter, point_id_for
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.contracts.types import Chunk


def _field_matchany(cond: models.Condition) -> tuple[str, list[Any]]:
    assert isinstance(cond, models.FieldCondition)
    assert isinstance(cond.match, models.MatchAny)
    return cond.key, list(cond.match.any)


def test_any_group_compiles_to_nested_should_of_musts() -> None:
    flt = _build_filter({"$any": [{"acl": ["g-a"]}, {"document_id": ["d1", "d2"]}]})
    assert flt is not None
    assert isinstance(flt.must, list) and len(flt.must) == 1
    group = flt.must[0]
    assert isinstance(group, models.Filter)
    should = group.should
    assert isinstance(should, list) and len(should) == 2
    # Each OR-branch is a Filter(must=[<one FieldCondition>]).
    branches = []
    for branch in should:
        assert isinstance(branch, models.Filter)
        assert isinstance(branch.must, list) and len(branch.must) == 1
        branches.append(_field_matchany(branch.must[0]))
    assert ("acl", ["g-a"]) in branches
    assert ("document_id", ["d1", "d2"]) in branches


def test_plain_keys_are_sibling_must_conditions_alongside_any() -> None:
    flt = _build_filter(
        {"metadata.team": "sales", "$any": [{"acl": ["g-a"]}, {"document_id": ["d1"]}]}
    )
    assert flt is not None
    assert isinstance(flt.must, list) and len(flt.must) == 2
    kinds = [type(c) for c in flt.must]
    assert models.FieldCondition in kinds  # the plain key
    assert models.Filter in kinds  # the $any OR-group


def test_unknown_dollar_key_raises() -> None:
    with pytest.raises(ValueError, match="unknown reserved filter key"):
        _build_filter({"$foo": [{"acl": ["g-a"]}]})


def test_nested_any_inside_sub_mapping_raises() -> None:
    with pytest.raises(ValueError):
        _build_filter({"$any": [{"$any": [{"acl": ["g-a"]}]}]})


def test_empty_any_list_raises() -> None:
    with pytest.raises(ValueError):
        _build_filter({"$any": []})


def test_non_mapping_any_element_raises() -> None:
    with pytest.raises(ValueError):
        _build_filter({"$any": ["notamap"]})


# --- get_filtered carries the group through base.must (scroll path) ---------


class _StubSparse:
    @property
    def model_id(self) -> str:
        return "stub"

    async def encode(self, texts: Sequence[str]) -> Sequence[Any]:
        return await HashSparseEncoder().encode(texts)

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[Any]:
        return await HashSparseEncoder().encode_queries(texts)


def test_get_filtered_carries_any_group_over_scroll() -> None:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(client, f"any_{uuid.uuid4().hex}", embedder, HashSparseEncoder())
    shared = Chunk(chunk_id="c-shared", document_id="d-shared", text="body", acl=("u-owner",))
    labelled = Chunk(chunk_id="c-lab", document_id="d-lab", text="body", acl=("g-a",))

    async def scenario() -> None:
        await store.ensure_collection()
        await store.upsert_chunks([shared, labelled])
        # A principal with label g-a plus a share of d-shared: the $any OR-group
        # returns BOTH ids on the fetch-by-id (scroll) path.
        flt = {"$any": [{"acl": ["g-a"]}, {"document_id": ["d-shared"]}]}
        got = await store.get_filtered(["c-shared", "c-lab"], filters=flt)
        assert {c.chunk_id for c in got} == {"c-shared", "c-lab"}
        # Without the share, only the labelled chunk survives.
        only_label = await store.get_filtered(
            ["c-shared", "c-lab"], filters={"$any": [{"acl": ["g-a"]}, {"document_id": ["nope"]}]}
        )
        assert {c.chunk_id for c in only_label} == {"c-lab"}

    asyncio.run(scenario())
    # sanity: point ids are deterministic (nothing above depended on ordering)
    assert point_id_for("c-shared") != point_id_for("c-lab")
