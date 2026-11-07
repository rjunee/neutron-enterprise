# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Store-level tests for the REAL ``QdrantChunkStore.permission_filter`` (NE-043).

Against real ``AsyncQdrantClient(':memory:')``: the stub returns a ``models.Filter``
for every principal (``None`` is gone), matches own-id + expanded groups exactly,
an empty-ACL chunk matches NO filter (the never-match-all direction), the
expander-less fallback = verified token claims only, and the store-level filter
is provably equivalent to the retriever-hook mapping (no-drift).
"""

from __future__ import annotations

import asyncio
import uuid

from qdrant_client import AsyncQdrantClient, models

from ne.brain.qdrant import QdrantChunkStore, _build_filter, point_id_for
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter, acl_match_labels
from ne.contracts.types import Chunk, Principal

_ADA = Chunk(chunk_id="c-ada", document_id="d", text="alpha", acl=("u-ada",))
_ENG = Chunk(chunk_id="c-eng", document_id="d", text="alpha", acl=("g-eng",))
_EMPTY = Chunk(chunk_id="c-empty", document_id="d", text="alpha", acl=())
_OTHER = Chunk(chunk_id="c-other", document_id="d", text="alpha", acl=("u-other",))
_CORPUS = [_ADA, _ENG, _EMPTY, _OTHER]


class _StubExpander:
    """Below-seam stub: maps u-ada -> {u-ada, g-eng}, everyone else -> {self}."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        if pid == "u-ada":
            return frozenset({"u-ada", "g-eng"})
        return frozenset({pid})


def _build(*, group_expander: object = None) -> QdrantChunkStore:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    collection = f"pf_{uuid.uuid4().hex}"
    store = QdrantChunkStore(
        client,
        collection,
        embedder,
        HashSparseEncoder(),
        group_expander=group_expander,  # type: ignore[arg-type]
    )

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks(_CORPUS)

    asyncio.run(setup())
    return store


async def _matching_ids(store: QdrantChunkStore, flt: models.Filter) -> set[str]:
    # `_client` is Optional by construction (it is built lazily from a factory),
    # so narrow before use rather than suppressing the type error. Pre-existing
    # on this lineage; fixed at assembly because CI's typecheck gate is required.
    assert store._client is not None
    records, _ = await store._client.scroll(
        collection_name=store._collection,
        scroll_filter=flt,
        limit=100,
        with_payload=True,
    )
    by_point = {point_id_for(c.chunk_id): c.chunk_id for c in _CORPUS}
    return {by_point[str(r.id)] for r in records}


def test_permission_filter_returns_filter_never_none() -> None:
    with_exp = _build(group_expander=_StubExpander())
    without_exp = _build()
    p = Principal(id="u-ada", groups=())
    assert isinstance(with_exp.permission_filter(p), models.Filter)
    assert isinstance(without_exp.permission_filter(p), models.Filter)


def test_permission_filter_matches_own_id_and_expanded_groups() -> None:
    store = _build(group_expander=_StubExpander())
    p = Principal(id="u-ada", groups=())

    async def scenario() -> None:
        matched = await _matching_ids(store, store.permission_filter(p))
        assert matched == {"c-ada", "c-eng"}

    asyncio.run(scenario())


def test_permission_filter_empty_acl_chunk_never_matches() -> None:
    store = _build(group_expander=_StubExpander())

    async def scenario() -> None:
        for pid, groups in [("u-ada", ()), ("u-other", ()), ("u-ghost", ("g-eng",))]:
            p = Principal(id=pid, groups=groups)
            matched = await _matching_ids(store, store.permission_filter(p))
            assert "c-empty" not in matched

    asyncio.run(scenario())


def test_permission_filter_without_expander_matches_token_claims_only() -> None:
    store = _build()  # no expander
    # A principal whose only verified label is its own id sees only its own chunk.
    p = Principal(id="u-ada", groups=())

    async def scenario() -> None:
        matched = await _matching_ids(store, store.permission_filter(p))
        assert matched == {"c-ada"}
        # A token-claim group widens (only) to that verified group.
        p2 = Principal(id="u-other", groups=("g-eng",))
        matched2 = await _matching_ids(store, store.permission_filter(p2))
        assert matched2 == {"c-other", "c-eng"}

    asyncio.run(scenario())


def test_permission_filter_agrees_with_hook_mapping() -> None:
    # No-drift: the store-level Filter equals the Filter built from the
    # PrincipalAclFilter hook mapping over the same labels.
    expander = _StubExpander()
    store = _build(group_expander=expander)
    hook = PrincipalAclFilter(expander)
    for pid in ["u-ada", "u-other", "u-ghost"]:
        p = Principal(id=pid, groups=())
        hook_mapping = hook(p)
        assert hook_mapping == {"acl": list(acl_match_labels(p, expander))}
        assert store.permission_filter(p) == _build_filter(dict(hook_mapping))
