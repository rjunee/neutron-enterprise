# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-087 shared-document seam — the `$any` union proven end-to-end on Qdrant.

Real ``AsyncQdrantClient(':memory:')`` + real ``QdrantChunkStore`` +
``HybridRetriever`` composed exactly as production, with an INLINE fake
``SharedDocumentResolver`` (no dependency on ne-permissions-rebac; ne-brain must
not import it). Proves: fail-closed absence (T-1), exact per-object grant (T-2),
revocation with no cache (T-3), the union matrix on BOTH search() and retrieve()
(T-4), byte-identity of the no-resolver / zero-shares paths, async-hook plumbing,
and deny-by-erroring propagation (T-8).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.types import Chunk, Principal, RetrievalResult

_TEXT = "quarterly revenue report"


class _FakeExpander:
    """Structural :class:`ne.brain.trimming.GroupExpander` — id + fixed groups."""

    def __init__(self, groups: dict[str, frozenset[str]] | None = None) -> None:
        self._groups = groups or {}

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        return frozenset({pid, *self._groups.get(pid, frozenset())})


class _FakeResolver:
    """Structural :class:`ne.brain.trimming.SharedDocumentResolver` over a dict."""

    def __init__(self, mapping: dict[str, frozenset[str]]) -> None:
        self.mapping = mapping

    async def shared_document_ids(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        return self.mapping.get(pid, frozenset())


class _RaisingResolver:
    async def shared_document_ids(self, principal: Principal | str) -> frozenset[str]:
        raise RuntimeError("openfga backend down")


# labels/documents. jane sees D-src by label; D-shared/D-reb only via a share.
_CORPUS = [
    Chunk(chunk_id="c-src", document_id="D-src", text=_TEXT, acl=("u-jane",)),
    Chunk(chunk_id="c-shared", document_id="D-shared", text=_TEXT, acl=("u-owner",)),
    Chunk(chunk_id="c-other", document_id="D-other", text=_TEXT, acl=("u-owner",)),
    Chunk(chunk_id="c-both", document_id="D-both", text=_TEXT, acl=("u-jane",)),
    Chunk(chunk_id="c-reb", document_id="D-reb", text=_TEXT, acl=("u-owner",)),
    Chunk(chunk_id="c-none", document_id="D-none", text=_TEXT, acl=("u-owner",)),
]
_ALL_IDS = [c.chunk_id for c in _CORPUS]
_TOP_K = 50

P_JANE = Principal(id="u-jane")
P_COLLEAGUE = Principal(id="u-colleague")
P_NOBODY = Principal(id="u-nobody")


def _build(resolver: Any | None) -> HybridRetriever:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(client, f"seam_{uuid.uuid4().hex}", embedder, HashSparseEncoder())
    expander = _FakeExpander()
    hook = PrincipalAclFilter(expander, shared_docs=resolver) if resolver is not None else None

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks(_CORPUS)

    asyncio.run(setup())
    return HybridRetriever(store, embedder, HashSparseEncoder(), acl_filters=hook, audit_sink=None)


async def _search_ids(r: HybridRetriever, p: Principal) -> set[str]:
    res = await r.search(_TEXT, principal=p, top_k=_TOP_K)
    return {sc.chunk.chunk_id for sc in res.chunks}


async def _retrieve_ids(r: HybridRetriever, p: Principal, ids: list[str]) -> set[str]:
    res = await r.retrieve(ids, principal=p)
    return {sc.chunk.chunk_id for sc in res.chunks}


# --- T-1 fail-closed absence -----------------------------------------------


def test_no_source_and_no_share_sees_nothing_both_paths() -> None:
    retriever = _build(_FakeResolver({}))  # nobody has any share

    async def scenario() -> None:
        assert await _search_ids(retriever, P_NOBODY) == set()
        assert await _retrieve_ids(retriever, P_NOBODY, _ALL_IDS) == set()

    asyncio.run(scenario())


# --- T-2 exact per-object grant --------------------------------------------


def test_exact_grant_only_the_shared_subject_and_only_the_shared_doc() -> None:
    retriever = _build(_FakeResolver({"u-jane": frozenset({"D-shared"})}))

    async def scenario() -> None:
        # jane sees her label doc AND the shared doc, on BOTH paths.
        jane_search = await _search_ids(retriever, P_JANE)
        assert "c-src" in jane_search and "c-shared" in jane_search
        assert await _retrieve_ids(retriever, P_JANE, ["c-shared"]) == {"c-shared"}
        assert await _retrieve_ids(retriever, P_JANE, ["c-src"]) == {"c-src"}
        # jane cannot see any OTHER owner doc (grant is per-object, no widening).
        assert "c-other" not in jane_search
        assert await _retrieve_ids(retriever, P_JANE, ["c-other"]) == set()
        # a same-nothing colleague (no share) still cannot see the shared doc.
        assert "c-shared" not in await _search_ids(retriever, P_COLLEAGUE)
        assert await _retrieve_ids(retriever, P_COLLEAGUE, ["c-shared"]) == set()

    asyncio.run(scenario())


# --- T-3 revocation (no cache) ---------------------------------------------


def test_revocation_hides_doc_next_query_both_paths() -> None:
    resolver = _FakeResolver({"u-jane": frozenset({"D-shared"})})
    retriever = _build(resolver)

    async def scenario() -> None:
        assert "c-shared" in await _search_ids(retriever, P_JANE)  # baseline
        resolver.mapping["u-jane"] = frozenset()  # revoke the share
        assert "c-shared" not in await _search_ids(retriever, P_JANE)
        assert await _retrieve_ids(retriever, P_JANE, ["c-shared"]) == set()
        # jane still sees her label doc (targeted revoke).
        assert "c-src" in await _search_ids(retriever, P_JANE)

    asyncio.run(scenario())


# --- T-4 union matrix (both paths) -----------------------------------------


def test_union_matrix_visible_iff_either_leg_grants() -> None:
    # jane: source grants {c-src, c-both}; rebac grants {D-both, D-reb}.
    retriever = _build(_FakeResolver({"u-jane": frozenset({"D-both", "D-reb"})}))
    expected = {"c-src", "c-both", "c-reb"}  # NOT c-other / c-shared / c-none

    async def scenario() -> None:
        assert await _search_ids(retriever, P_JANE) == expected
        assert await _retrieve_ids(retriever, P_JANE, _ALL_IDS) == expected
        # rebac grant on an already-source-granted doc (D-both) changes nothing.
        assert "c-both" in expected

    asyncio.run(scenario())


# --- byte-identity: no-resolver and zero-shares paths ----------------------


def test_no_resolver_is_sync_and_zero_shares_awaits_to_same_mapping() -> None:
    expander = _FakeExpander()
    sync_hook = PrincipalAclFilter(expander)
    sync_out = sync_hook(P_JANE)
    assert not asyncio.iscoroutine(sync_out)
    assert isinstance(sync_out, Mapping)
    assert sync_out == {"acl": ["u-jane"]}

    async_hook = PrincipalAclFilter(expander, shared_docs=_FakeResolver({}))
    coro = async_hook(P_JANE)
    assert asyncio.iscoroutine(coro)
    resolved = asyncio.run(coro)
    # zero shares collapse to the SAME mapping (no $any wrapper).
    assert resolved == sync_out


def test_one_share_emits_any_group_mapping() -> None:
    resolver = _FakeResolver({"u-jane": frozenset({"D-shared"})})
    hook = PrincipalAclFilter(_FakeExpander(), shared_docs=resolver)
    resolved = asyncio.run(hook(P_JANE))  # type: ignore[arg-type]
    assert resolved == {"$any": [{"acl": ["u-jane"]}, {"document_id": ["D-shared"]}]}


# --- async-hook plumbing: sync hooks still work ----------------------------


def test_sync_hook_still_works_on_both_paths() -> None:
    # A plain sync callable (not PrincipalAclFilter) returning a mapping.
    retriever = HybridRetriever(
        _build(None)._store,  # reuse a populated store
        HashEmbedder(32),
        HashSparseEncoder(),
        acl_filters=lambda _p: {"acl": ["u-jane"]},
        audit_sink=None,
    )

    async def scenario() -> None:
        got = await _search_ids(retriever, P_JANE)
        assert got == {"c-src", "c-both"}  # only u-jane-labelled chunks
        assert await _retrieve_ids(retriever, P_JANE, ["c-src"]) == {"c-src"}

    asyncio.run(scenario())


# --- T-8 deny-by-erroring propagation --------------------------------------


def test_resolver_exception_propagates_on_both_paths() -> None:
    retriever = _build(_RaisingResolver())

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="backend down"):
            await retriever.search(_TEXT, principal=P_JANE, top_k=_TOP_K)
        with pytest.raises(RuntimeError, match="backend down"):
            await retriever.retrieve(["c-shared"], principal=P_JANE)

    asyncio.run(scenario())


def test_seam_result_type_is_retrieval_result() -> None:
    retriever = _build(_FakeResolver({}))
    res = asyncio.run(retriever.retrieve([], principal=P_JANE))
    assert isinstance(res, RetrievalResult)
