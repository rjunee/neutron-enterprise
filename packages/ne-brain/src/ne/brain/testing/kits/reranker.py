# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reranker conformance kit — query-scoped cross-encoder rerank.

Async contract methods are exercised via ``asyncio.run`` (pytest-asyncio is not
a dependency and must not be added). ``create_reranker`` is a SYNCHRONOUS
factory (mirrors :class:`KnowledgeReadContractKit`). See the kit-location note
in :mod:`ne.brain.testing.kits`.
"""

from __future__ import annotations

import asyncio

from ne.contracts.knowledge import Reranker
from ne.contracts.types import Chunk


class RerankerContractKit:
    """Subclass and override :meth:`create_reranker` to prove Reranker conformance."""

    def create_reranker(self) -> Reranker:
        raise NotImplementedError

    def test_model_id_is_nonempty_string(self) -> None:
        reranker = self.create_reranker()
        assert isinstance(reranker.model_id, str)
        assert reranker.model_id

    def test_rerank_returns_at_most_top_k(self) -> None:
        reranker = self.create_reranker()
        chunks = [
            Chunk(chunk_id=f"c{i}", document_id="d1", text=f"content block {i}") for i in range(5)
        ]

        async def scenario() -> None:
            result = await reranker.rerank("content block 0", chunks, top_k=3)
            assert len(result) <= 3
            input_ids = {c.chunk_id for c in chunks}
            for scored in result:
                assert scored.chunk.chunk_id in input_ids

        asyncio.run(scenario())

    def test_rerank_orders_by_score_descending(self) -> None:
        reranker = self.create_reranker()
        chunks = [
            Chunk(chunk_id=f"c{i}", document_id="d1", text=f"content block {i}") for i in range(5)
        ]

        async def scenario() -> None:
            result = await reranker.rerank("content block 0", chunks, top_k=5)
            scores = [sc.score for sc in result]
            assert scores == sorted(scores, reverse=True)

        asyncio.run(scenario())

    def test_rerank_relevant_chunk_ranks_first(self) -> None:
        reranker = self.create_reranker()
        chunks = [
            Chunk(chunk_id="target", document_id="d1", text="quarterly revenue report"),
            *(
                Chunk(chunk_id=f"noise-{i}", document_id="d1", text=f"unrelated filler {i}")
                for i in range(4)
            ),
        ]

        async def scenario() -> None:
            result = await reranker.rerank("quarterly revenue report", chunks, top_k=5)
            assert result
            assert result[0].chunk.chunk_id == "target"

        asyncio.run(scenario())

    def test_rerank_empty_chunks_returns_empty(self) -> None:
        reranker = self.create_reranker()

        async def scenario() -> None:
            result = await reranker.rerank("anything", [], top_k=5)
            assert list(result) == []

        asyncio.run(scenario())
