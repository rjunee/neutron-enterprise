# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KnowledgeRead conformance kit — principal-scoped search/retrieve + citations.

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is not
a dependency and must not be added). ``create_reader`` is a SYNCHRONOUS
factory (it must not itself call ``asyncio.run`` from within a running loop —
it is always invoked from a plain sync context, before the per-test
``asyncio.run(scenario())`` call).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ne.contracts.knowledge import KnowledgeRead
from ne.contracts.types import Chunk, Principal

_PRINCIPAL = Principal(id="u1", groups=("group-a",))


class KnowledgeReadContractKit:
    """Subclass and override :meth:`create_reader` to prove KnowledgeRead conformance."""

    def create_reader(self, chunks: Sequence[Chunk]) -> KnowledgeRead:
        """Ingest ``chunks`` into a fresh backing store and return a reader over it."""
        raise NotImplementedError

    def test_search_returns_scored_chunks_and_citations(self) -> None:
        chunks = [
            Chunk(
                chunk_id="c1",
                document_id="d1",
                text="quarterly revenue report details",
                metadata={"source_system": "confluence", "source_ref": "PAGE-1"},
            ),
            Chunk(
                chunk_id="c2",
                document_id="d2",
                text="unrelated filler content",
                metadata={"source_system": "sharepoint", "source_ref": "DOC-2"},
            ),
        ]
        reader = self.create_reader(chunks)

        async def scenario() -> None:
            result = await reader.search(
                "quarterly revenue report details", principal=_PRINCIPAL, top_k=5
            )
            assert result.chunks
            assert len(result.citations) == len(result.chunks)
            for scored, citation in zip(result.chunks, result.citations, strict=True):
                assert citation.chunk_id == scored.chunk.chunk_id
                assert citation.document_id == scored.chunk.document_id
                assert citation.source_system == scored.chunk.metadata.get("source_system", "")
                assert citation.source_ref == scored.chunk.metadata.get(
                    "source_ref", scored.chunk.document_id
                )

        asyncio.run(scenario())

    def test_search_top_k_respected(self) -> None:
        chunks = [
            Chunk(chunk_id=f"c{i}", document_id="d1", text=f"distinct content block {i}")
            for i in range(6)
        ]
        reader = self.create_reader(chunks)

        async def scenario() -> None:
            result = await reader.search("distinct content block 0", principal=_PRINCIPAL, top_k=2)
            assert len(result.chunks) == 2

        asyncio.run(scenario())

    def test_search_exact_text_ranks_first(self) -> None:
        chunks = [
            Chunk(chunk_id="target", document_id="d1", text="quarterly revenue report"),
            *(
                Chunk(chunk_id=f"noise-{i}", document_id="d1", text=f"unrelated filler {i}")
                for i in range(4)
            ),
        ]
        reader = self.create_reader(chunks)

        async def scenario() -> None:
            result = await reader.search("quarterly revenue report", principal=_PRINCIPAL, top_k=5)
            assert result.chunks
            assert result.chunks[0].chunk.chunk_id == "target"

        asyncio.run(scenario())

    def test_retrieve_returns_chunks_surfaced_by_search(self) -> None:
        chunks = [
            Chunk(chunk_id="target", document_id="d1", text="quarterly revenue report"),
            *(
                Chunk(chunk_id=f"noise-{i}", document_id="d1", text=f"unrelated filler {i}")
                for i in range(4)
            ),
        ]
        reader = self.create_reader(chunks)

        async def scenario() -> None:
            searched = await reader.search(
                "quarterly revenue report", principal=_PRINCIPAL, top_k=5
            )
            ids = [sc.chunk.chunk_id for sc in searched.chunks]

            retrieved = await reader.retrieve(ids, principal=_PRINCIPAL)
            assert {sc.chunk.chunk_id for sc in retrieved.chunks} == set(ids)

        asyncio.run(scenario())

    def test_retrieve_unknown_ids_returns_empty(self) -> None:
        chunks = [Chunk(chunk_id="c1", document_id="d1", text="known content")]
        reader = self.create_reader(chunks)

        async def scenario() -> None:
            retrieved = await reader.retrieve(["does-not-exist"], principal=_PRINCIPAL)
            assert retrieved.chunks == ()
            assert retrieved.citations == ()

        asyncio.run(scenario())
