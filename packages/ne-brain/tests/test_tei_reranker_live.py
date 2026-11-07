# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TEIReranker LIVE acceptance test against a real TEI reranker server.

Skipped (LIVE-PARKED) when NE_TEI_RERANK_URL is unreachable. To run for real:

    ne dev up
    NE_TEI_RERANK_URL=http://localhost:8086 uv run pytest packages/ne-brain -q
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

import httpx
import pytest

from ne.brain.reranker import TEIReranker
from ne.contracts.types import Chunk, ScoredChunk


def _tei_rerank_url() -> str:
    return os.environ.get("NE_TEI_RERANK_URL", "http://localhost:8086")


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


_URL = _tei_rerank_url()

if not _reachable(_URL):
    pytest.skip(
        "LIVE-PARKED: real-TEI rerank pending — start `ne dev up` and re-run "
        "`NE_TEI_RERANK_URL=http://localhost:8086 uv run pytest packages/ne-brain -q` "
        f"(NE_TEI_RERANK_URL={_URL} unreachable)",
        allow_module_level=True,
    )


def _chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id="relevant",
            document_id="d1",
            text="Deep learning is a subfield of machine learning using neural networks.",
        ),
        Chunk(
            chunk_id="unrelated",
            document_id="d2",
            text="The quarterly cafeteria menu features seasonal vegetables.",
        ),
    ]


def test_rerank_ranks_relevant_text_first() -> None:
    reranker = TEIReranker(_URL)

    async def scenario() -> Sequence[ScoredChunk]:
        result = await reranker.rerank("What is Deep Learning?", _chunks(), top_k=2)
        await reranker.aclose()
        return result

    result = asyncio.run(scenario())
    assert result
    assert result[0].chunk.chunk_id == "relevant"


def test_rerank_scores_descending_and_top_k_respected() -> None:
    reranker = TEIReranker(_URL)

    async def scenario() -> Sequence[ScoredChunk]:
        result = await reranker.rerank("What is Deep Learning?", _chunks(), top_k=1)
        await reranker.aclose()
        return result

    result = asyncio.run(scenario())
    assert len(result) == 1
    scores = [sc.score for sc in result]
    assert scores == sorted(scores, reverse=True)
