# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ChunkStore conformance kit — dense/sparse hybrid vector store contract.

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is not
a dependency and must not be added — same rule as the ObjectStore kit in
ne-contracts). ``embed_text``/``encode_query`` are SYNCHRONOUS factories (they
must not themselves call ``asyncio.run`` — they may be invoked either outside
or inside the per-test ``asyncio.run(scenario())`` call).

Kit-law decided semantics asserted here (PR-body-documented contract law):

* ``get()`` OMITS unknown ids rather than raising, preserving the caller's
  input order for the ids that do exist.
* ``query(dense=None, sparse=None)`` raises ``ValueError`` — at least one
  signal is required.
* Filter grammar: keys are used verbatim as payload field paths (e.g.
  ``"metadata.team"``, ``"acl"``); a scalar value means equality, a
  list/tuple value means "any of".
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from ne.contracts.knowledge import ChunkStore
from ne.contracts.types import Chunk, SparseVector


class ChunkStoreContractKit:
    """Subclass and override the factories to prove ChunkStore conformance."""

    def create_store(self) -> ChunkStore:
        """Return a fresh, empty ChunkStore adapter (collection already ensured)."""
        raise NotImplementedError

    def embed_text(self, text: str) -> Sequence[float]:
        """Return a dense vector for ``text``, consistent with the store's
        ingest-time embedding (so an exact-text query ranks that chunk first).

        Must be synchronous (no ``asyncio.run``) — this is called from both
        outside and inside a test's ``asyncio.run(scenario())`` context.
        """
        raise NotImplementedError

    def encode_query(self, text: str) -> SparseVector:
        """Return a sparse QUERY vector for ``text``, consistent with the
        store's ingest-time BM25 encoding (so a shared keyword matches).

        Must be synchronous (no ``asyncio.run``) — see :meth:`embed_text`.
        """
        raise NotImplementedError

    # -- roundtrip / CRUD ------------------------------------------------

    def test_upsert_get_roundtrip_preserves_fields(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            chunk = Chunk(
                chunk_id="c1",
                document_id="d1",
                text="hello hybrid retrieval world",
                metadata={"source_system": "confluence", "source_ref": "PAGE-1"},
                acl=("group-a",),
            )
            await store.upsert_chunks([chunk])
            got = await store.get(["c1"])
            assert len(got) == 1
            assert got[0].chunk_id == "c1"
            assert got[0].document_id == "d1"
            assert got[0].text == "hello hybrid retrieval world"
            assert got[0].metadata["source_system"] == "confluence"
            assert got[0].metadata["source_ref"] == "PAGE-1"
            assert got[0].acl == ("group-a",)

        asyncio.run(scenario())

    def test_get_missing_ids_omitted(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.upsert_chunks(
                [Chunk(chunk_id="known", document_id="d1", text="alpha beta")]
            )
            got = await store.get(["missing-1", "known", "missing-2"])
            assert [c.chunk_id for c in got] == ["known"]

        asyncio.run(scenario())

    def test_get_multiple_ids_preserves_input_order(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            chunks = [
                Chunk(chunk_id=f"c{i}", document_id="d1", text=f"ordered content {i}")
                for i in range(3)
            ]
            await store.upsert_chunks(chunks)
            # Request in a deliberately non-ingest order: the result must
            # follow the CALLER's id order, not insertion or storage order.
            got = await store.get(["c2", "c0", "c1"])
            assert [c.chunk_id for c in got] == ["c2", "c0", "c1"]

        asyncio.run(scenario())

    def test_upsert_same_chunk_id_overwrites(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            await store.upsert_chunks([Chunk(chunk_id="c1", document_id="d1", text="version one")])
            await store.upsert_chunks([Chunk(chunk_id="c1", document_id="d1", text="version two")])
            got = await store.get(["c1"])
            assert len(got) == 1
            assert got[0].text == "version two"

        asyncio.run(scenario())

    def test_delete_removes_from_get_and_query(self) -> None:
        store = self.create_store()
        dense = self.embed_text("ephemeral chunk text")

        async def scenario() -> None:
            chunk = Chunk(chunk_id="c1", document_id="d1", text="ephemeral chunk text")
            await store.upsert_chunks([chunk])
            assert await store.get(["c1"])

            await store.delete(["c1"])
            assert await store.get(["c1"]) == []

            results = await store.query(dense=dense, top_k=5)
            assert all(sc.chunk.chunk_id != "c1" for sc in results)

        asyncio.run(scenario())

    # -- query validation --------------------------------------------------

    def test_query_requires_dense_or_sparse(self) -> None:
        store = self.create_store()

        async def scenario() -> None:
            with pytest.raises(ValueError):
                await store.query(dense=None, sparse=None)

        asyncio.run(scenario())

    # -- relevance -----------------------------------------------------

    def test_dense_query_exact_text_ranks_first(self) -> None:
        store = self.create_store()
        dense = self.embed_text("quarterly revenue report")

        async def scenario() -> None:
            target = Chunk(chunk_id="target", document_id="d1", text="quarterly revenue report")
            others = [
                Chunk(chunk_id=f"noise-{i}", document_id="d1", text=f"unrelated filler text {i}")
                for i in range(4)
            ]
            await store.upsert_chunks([target, *others])

            results = await store.query(dense=dense, top_k=5)
            assert results
            assert results[0].chunk.chunk_id == "target"

        asyncio.run(scenario())

    def test_sparse_query_keyword_match_ranks_first(self) -> None:
        store = self.create_store()
        sparse = self.encode_query("zzyzxplorb")

        async def scenario() -> None:
            target = Chunk(
                chunk_id="target",
                document_id="d1",
                text="the zzyzxplorb subsystem handles reconciliation",
            )
            others = [
                Chunk(chunk_id=f"noise-{i}", document_id="d1", text=f"generic filler content {i}")
                for i in range(4)
            ]
            await store.upsert_chunks([target, *others])

            results = await store.query(sparse=sparse, top_k=5)
            assert results
            assert results[0].chunk.chunk_id == "target"

        asyncio.run(scenario())

    def test_hybrid_fusion_merges_both_signals(self) -> None:
        store = self.create_store()
        dense = self.embed_text("the exact dense query text")
        sparse = self.encode_query("flumbrastatin")

        async def scenario() -> None:
            dense_match = Chunk(
                chunk_id="dense-match", document_id="d1", text="the exact dense query text"
            )
            keyword_match = Chunk(
                chunk_id="keyword-match",
                document_id="d1",
                text="mentions the unique term flumbrastatin here",
            )
            others = [
                Chunk(chunk_id=f"noise-{i}", document_id="d1", text=f"generic filler content {i}")
                for i in range(4)
            ]
            await store.upsert_chunks([dense_match, keyword_match, *others])

            results = await store.query(dense=dense, sparse=sparse, top_k=5)

            ids = {sc.chunk.chunk_id for sc in results}
            assert "dense-match" in ids
            assert "keyword-match" in ids

        asyncio.run(scenario())

    def test_query_top_k_respected(self) -> None:
        store = self.create_store()
        dense = self.embed_text("distinct content block 0")

        async def scenario() -> None:
            chunks = [
                Chunk(chunk_id=f"c{i}", document_id="d1", text=f"distinct content block {i}")
                for i in range(6)
            ]
            await store.upsert_chunks(chunks)

            results = await store.query(dense=dense, top_k=2)
            assert len(results) == 2

        asyncio.run(scenario())

    def test_query_scores_descending(self) -> None:
        store = self.create_store()
        dense = self.embed_text("distinct content block 0")

        async def scenario() -> None:
            chunks = [
                Chunk(chunk_id=f"c{i}", document_id="d1", text=f"distinct content block {i}")
                for i in range(6)
            ]
            await store.upsert_chunks(chunks)

            results = await store.query(dense=dense, top_k=6)
            scores = [sc.score for sc in results]
            assert scores == sorted(scores, reverse=True)

        asyncio.run(scenario())

    # -- filters ---------------------------------------------------------

    def test_query_metadata_filter_restricts(self) -> None:
        store = self.create_store()
        query_text = "shared query phrasing"
        dense = self.embed_text(query_text)

        async def scenario() -> None:
            alpha = Chunk(
                chunk_id="alpha",
                document_id="d1",
                text="unrelated alpha content",
                metadata={"team": "alpha"},
            )
            beta = Chunk(
                chunk_id="beta",
                document_id="d1",
                text=query_text,
                metadata={"team": "beta"},
            )
            await store.upsert_chunks([alpha, beta])

            # Without the filter, beta (exact text match) would outrank alpha.
            unfiltered = await store.query(dense=dense, top_k=5)
            assert unfiltered[0].chunk.chunk_id == "beta"

            filtered = await store.query(dense=dense, top_k=5, filters={"metadata.team": "alpha"})
            assert [sc.chunk.chunk_id for sc in filtered] == ["alpha"]

        asyncio.run(scenario())

    def test_query_acl_filter_seam(self) -> None:
        store = self.create_store()
        dense = self.embed_text("shared content body")

        async def scenario() -> None:
            group_a = Chunk(
                chunk_id="ga", document_id="d1", text="shared content body", acl=("group-a",)
            )
            group_b = Chunk(
                chunk_id="gb", document_id="d1", text="shared content body", acl=("group-b",)
            )
            await store.upsert_chunks([group_a, group_b])

            results = await store.query(dense=dense, top_k=5, filters={"acl": ["group-a"]})
            assert [sc.chunk.chunk_id for sc in results] == ["ga"]

        asyncio.run(scenario())
