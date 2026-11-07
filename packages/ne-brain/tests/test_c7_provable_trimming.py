# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE D2 provable-trimming suite (SPEC §5 NE-043, Checkpoint-D D2).

No mock past any seam: real ``AsyncQdrantClient(':memory:')`` + real
``QdrantChunkStore`` + REAL ne-permissions ``PrincipalStore``/``PrincipalExpander``
over sqlite, composed EXACTLY like the production composition root
(``HybridRetriever(acl_filters=PrincipalAclFilter(expander))`` and
``QdrantChunkStore(group_expander=expander)``), plus a ``RetrievalPipeline`` on top.

The property (the literal D2 sentence): a caller holding a known ``chunk_id``
CANNOT ``retrieve()`` a chunk that ``search()`` hides from them. Proven across
transitive group expansion, fail-closed unknown principals, disjoint ACLs,
empty-ACL chunks, TENANT_ALL, caller-filter widening, and query-time revocation
— on BOTH principal-scoped read paths.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.brain.pipeline import RetrievalPipeline
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk
from ne.permissions.expand import PrincipalExpander
from ne.permissions.models import TENANT_ALL, PrincipalKind, SyncedPrincipal
from ne.permissions.store import PrincipalStore, ensure_permissions_schema

NOW = datetime(2026, 7, 6, tzinfo=UTC)

# All chunks share identical text so an UNTRIMMED hybrid search ranks every one
# at the top (HashEmbedder/HashSparseEncoder are deterministic) — any absence
# from a result is therefore provably an ACL trim, not a relevance miss.
_TEXT = "quarterly revenue report"
_CORPUS = [
    Chunk(chunk_id="c-eng", document_id="d", text=_TEXT, acl=("g-eng",)),
    Chunk(chunk_id="c-ada", document_id="d", text=_TEXT, acl=("u-ada",)),
    Chunk(chunk_id="c-secret", document_id="d", text=_TEXT, acl=("u-other",)),
    Chunk(chunk_id="c-tenant", document_id="d", text=_TEXT, acl=(TENANT_ALL,)),
    Chunk(chunk_id="c-empty", document_id="d", text=_TEXT, acl=()),
    Chunk(chunk_id="c-ghost", document_id="d", text=_TEXT, acl=("u-ghost",)),
]
_ALL_IDS = [c.chunk_id for c in _CORPUS]
_TOP_K = 20  # >= corpus size, so trimming is the only thing that removes a chunk

P_ADA = Principal(id="u-ada")  # transitive member g-child ⊂ g-eng, store-known
P_BOB = Principal(id="u-bob")  # store-known, groupless
P_GHOST = Principal(id="u-ghost")  # NEVER synced -> unknown/fail-closed


class _IdentityReranker:
    """Passthrough reranker — the reranker is NOT the seam under test."""

    @property
    def model_id(self) -> str:
        return "identity"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        return [ScoredChunk(chunk=c, score=1.0) for c in list(chunks)[:top_k]]


@dataclass
class _Env:
    retriever: HybridRetriever
    pipeline: RetrievalPipeline
    principals: PrincipalStore


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'principals.db'}")
    ensure_permissions_schema(engine)
    pstore = PrincipalStore(engine)
    # Seed principals: u-ada (transitive g-child ⊂ g-eng) and u-bob (groupless);
    # u-ghost is intentionally NEVER synced.
    pstore.upsert_principals(
        [
            SyncedPrincipal(principal_id="u-ada", kind=PrincipalKind.USER),
            SyncedPrincipal(principal_id="u-bob", kind=PrincipalKind.USER),
        ],
        source="test",
        now=NOW,
    )
    pstore.set_member_groups("u-ada", ["g-child"], source="test", now=NOW)
    pstore.set_member_groups("g-child", ["g-eng"], source="test", now=NOW)

    expander = PrincipalExpander(pstore)
    hook = PrincipalAclFilter(expander)

    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    collection = f"c7_{uuid.uuid4().hex}"
    store = QdrantChunkStore(
        client, collection, embedder, HashSparseEncoder(), group_expander=expander
    )

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks(_CORPUS)

    asyncio.run(setup())

    retriever = HybridRetriever(
        store, embedder, HashSparseEncoder(), acl_filters=hook, audit_sink=None
    )
    pipeline = RetrievalPipeline(retriever, _IdentityReranker())
    return _Env(retriever=retriever, pipeline=pipeline, principals=pstore)


async def _search_ids(reader: KnowledgeRead, principal: Principal) -> set[str]:
    result = await reader.search(_TEXT, principal=principal, top_k=_TOP_K)
    return {sc.chunk.chunk_id for sc in result.chunks}


async def _retrieve_ids(
    reader: KnowledgeRead, principal: Principal, chunk_ids: list[str]
) -> set[str]:
    result: RetrievalResult = await reader.retrieve(chunk_ids, principal=principal)
    return {sc.chunk.chunk_id for sc in result.chunks}


def test_transitive_group_member_sees_group_chunk_on_both_paths(env: _Env) -> None:
    async def scenario() -> None:
        assert "c-eng" in await _search_ids(env.retriever, P_ADA)
        assert await _retrieve_ids(env.retriever, P_ADA, ["c-eng"]) == {"c-eng"}

    asyncio.run(scenario())


def test_non_member_cannot_retrieve_what_search_hides(env: _Env) -> None:
    # The literal D2 sentence: u-bob is not in g-eng, so c-eng is absent from
    # search AND retrieve([c-eng]) is empty even though bob knows the id.
    async def scenario() -> None:
        assert "c-eng" not in await _search_ids(env.retriever, P_BOB)
        result = await env.retriever.retrieve(["c-eng"], principal=P_BOB)
        assert result == RetrievalResult(chunks=(), citations=())

    asyncio.run(scenario())


def test_unknown_principal_fail_closed_matches_only_own_id(env: _Env) -> None:
    # u-ghost was never synced -> expands to exactly {u-ghost}; it sees ONLY
    # c-ghost and explicitly NOT c-tenant (TENANT_ALL resolves only for known
    # principals) — on both paths.
    async def scenario() -> None:
        searched = await _search_ids(env.retriever, P_GHOST)
        assert searched == {"c-ghost"}
        assert await _retrieve_ids(env.retriever, P_GHOST, _ALL_IDS) == {"c-ghost"}
        assert "c-tenant" not in searched

    asyncio.run(scenario())


def test_disjoint_acl_chunk_hidden_on_both_paths(env: _Env) -> None:
    async def scenario() -> None:
        assert "c-secret" not in await _search_ids(env.retriever, P_ADA)
        result = await env.retriever.retrieve(["c-secret"], principal=P_ADA)
        assert result.chunks == ()

    asyncio.run(scenario())


def test_empty_acl_chunk_invisible_to_everyone(env: _Env) -> None:
    # Empty ACL is match-NOTHING, never match-all — for every principal, both paths.
    async def scenario() -> None:
        for p in (P_ADA, P_BOB, P_GHOST):
            assert "c-empty" not in await _search_ids(env.retriever, p)
            assert await _retrieve_ids(env.retriever, p, ["c-empty"]) == set()

    asyncio.run(scenario())


def test_known_principal_sees_tenant_all_chunk(env: _Env) -> None:
    # Anti-over-restriction: a store-known groupless principal DOES get TENANT_ALL.
    async def scenario() -> None:
        assert "c-tenant" in await _search_ids(env.retriever, P_BOB)
        assert await _retrieve_ids(env.retriever, P_BOB, ["c-tenant"]) == {"c-tenant"}

    asyncio.run(scenario())


def test_caller_filter_cannot_widen_acl(env: _Env) -> None:
    # u-bob supplies filters={'acl': ['g-eng']}; the reused and_combine_filters
    # intersects caller ∩ acl, so bob still cannot reach c-eng (or anything else
    # outside his label set).
    async def scenario() -> None:
        result = await env.retriever.search(
            _TEXT, principal=P_BOB, top_k=_TOP_K, filters={"acl": ["g-eng"]}
        )
        ids = {sc.chunk.chunk_id for sc in result.chunks}
        assert "c-eng" not in ids
        assert ids <= set()  # disjoint intersection -> nothing surfaces

    asyncio.run(scenario())


def test_revocation_via_group_edge_removal_no_reindex(env: _Env) -> None:
    # Remove u-ada's membership edge; c-eng must vanish from ada's search AND
    # retrieve WITHOUT any chunk re-upsert (query-time revocation).
    async def scenario() -> None:
        assert "c-eng" in await _search_ids(env.retriever, P_ADA)  # baseline: visible
        env.principals.set_member_groups("u-ada", [], source="test", now=NOW)
        assert "c-eng" not in await _search_ids(env.retriever, P_ADA)
        assert await _retrieve_ids(env.retriever, P_ADA, ["c-eng"]) == set()
        # ada still sees her own-id chunk (proves it is a targeted revoke).
        assert "c-ada" in await _search_ids(env.retriever, P_ADA)

    asyncio.run(scenario())


def test_retrieval_pipeline_trims_identically_to_retriever(env: _Env) -> None:
    # Run the u-bob hidden-chunk assertions through the RetrievalPipeline surface.
    async def scenario() -> None:
        assert "c-eng" not in await _search_ids(env.pipeline, P_BOB)
        result = await env.pipeline.retrieve(["c-eng"], principal=P_BOB)
        assert result == RetrievalResult(chunks=(), citations=())
        # And it still surfaces what bob may see.
        assert "c-tenant" in await _search_ids(env.pipeline, P_BOB)

    asyncio.run(scenario())
