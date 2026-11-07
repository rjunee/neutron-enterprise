# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-seam unit test for TEIReranker: HTTP shape via httpx.MockTransport.

This is a below-seam unit test (no real TEI server). Live acceptance against a
real TEI reranker is a separate test: test_tei_reranker_live.py.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.brain.reranker import TEIReranker
from ne.contracts.types import Chunk


def _payload(request: httpx.Request) -> dict:
    return json.loads(request.read())


def _chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", document_id="d1", text=f"text {i}") for i in range(n)]


def _overlap_client(recorded: list[httpx.Request]) -> httpx.AsyncClient:
    """Scores each text by token-overlap with the query; returns TEI /rerank shape."""

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        body = _payload(request)
        qtokens = set(body["query"].lower().split())
        entries = [
            {"index": i, "score": len(qtokens & set(text.lower().split())) / max(len(qtokens), 1)}
            for i, text in enumerate(body["texts"])
        ]
        return httpx.Response(200, json=entries)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_rerank_posts_expected_request_shape() -> None:
    recorded: list[httpx.Request] = []
    reranker = TEIReranker("http://tei.local", client=_overlap_client(recorded))

    asyncio.run(reranker.rerank("text 0", _chunks(2), top_k=2))

    assert len(recorded) == 1
    request = recorded[0]
    assert request.url.path == "/rerank"
    payload = _payload(request)
    assert payload["query"] == "text 0"
    assert payload["texts"] == ["text 0", "text 1"]
    assert payload["raw_scores"] is False


def test_rerank_maps_indices_and_sorts_descending() -> None:
    # Handler returns entries OUT OF ORDER with an inverted score ranking.
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"index": 1, "score": 0.9},
                {"index": 0, "score": 0.1},
                {"index": 2, "score": 0.5},
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = TEIReranker("http://tei.local", client=client)

    result = asyncio.run(reranker.rerank("q", _chunks(3), top_k=3))

    assert [sc.chunk.chunk_id for sc in result] == ["c1", "c2", "c0"]
    assert [sc.score for sc in result] == [0.9, 0.5, 0.1]


def test_rerank_respects_top_k() -> None:
    recorded: list[httpx.Request] = []
    reranker = TEIReranker("http://tei.local", client=_overlap_client(recorded))

    result = asyncio.run(reranker.rerank("text 0", _chunks(5), top_k=2))

    assert len(result) == 2


def test_rerank_batches_at_batch_size() -> None:
    recorded: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        body = _payload(request)
        # Give the highest score to any text ending in "9" (the last chunk,
        # which lives in the LAST batch) to prove cross-batch merge.
        entries = [
            {"index": i, "score": 10.0 if text.endswith("9") else float(i)}
            for i, text in enumerate(body["texts"])
        ]
        return httpx.Response(200, json=entries)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = TEIReranker("http://tei.local", client=client, batch_size=2)
    chunks = [Chunk(chunk_id=f"c{i}", document_id="d1", text=f"text {i}") for i in (1, 2, 3, 4, 9)]

    result = asyncio.run(reranker.rerank("q", chunks, top_k=3))

    assert len(recorded) == 3  # batches of 2, 2, 1
    # The chunk in the LAST batch (text "9") won the global top slot.
    assert result[0].chunk.chunk_id == "c9"
    assert result[0].score == 10.0


def test_rerank_empty_chunks_makes_no_http_call() -> None:
    recorded: list[httpx.Request] = []
    reranker = TEIReranker("http://tei.local", client=_overlap_client(recorded))

    result = asyncio.run(reranker.rerank("q", [], top_k=5))

    assert list(result) == []
    assert recorded == []


def test_rerank_malformed_response_raises_value_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 99, "score": 0.5}])  # index out of range

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reranker = TEIReranker("http://tei.local", client=client)

    try:
        asyncio.run(reranker.rerank("q", _chunks(2), top_k=2))
    except ValueError as exc:
        assert "malformed TEI /rerank response" in str(exc)
    else:
        raise AssertionError("expected ValueError for out-of-range index")


def test_model_id_property_default_is_bge_reranker_v2_m3() -> None:
    reranker = TEIReranker("http://tei.local")
    assert reranker.model_id == "BAAI/bge-reranker-v2-m3"


# --- NE-RERANK-CPU: scoring_char_limit -------------------------------------
#
# The pairing this has to hold: a truncated SCORING view <-> a full chunk still
# returned. Either half alone is a bug — a shortened view that also shortened
# the reader's text would be a silent corpus mutation, and a "limit" that did
# not reach the wire would be a setting that quietly does nothing.

_LONG = "alpha beta gamma delta epsilon zeta eta theta " * 40  # ~1760 chars


def _long_chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", document_id="d1", text=f"{i} {_LONG}") for i in range(n)]


def test_scoring_char_limit_defaults_to_off_and_posts_the_chunk_verbatim() -> None:
    recorded: list[httpx.Request] = []
    reranker = TEIReranker("http://tei.local", client=_overlap_client(recorded))
    chunks = _long_chunks(2)

    asyncio.run(reranker.rerank("alpha", chunks, top_k=2))

    assert _payload(recorded[0])["texts"] == [c.text for c in chunks]


def test_scoring_char_limit_truncates_only_the_text_sent_to_the_server() -> None:
    recorded: list[httpx.Request] = []
    reranker = TEIReranker(
        "http://tei.local", client=_overlap_client(recorded), scoring_char_limit=600
    )
    chunks = _long_chunks(2)

    result = asyncio.run(reranker.rerank("alpha", chunks, top_k=2))

    posted = _payload(recorded[0])["texts"]
    assert [len(t) for t in posted] == [600, 600]
    assert posted == [c.text[:600] for c in chunks]
    # ... and the FULL chunk still comes back: same object, untruncated text.
    assert {sc.chunk.chunk_id for sc in result} == {"c0", "c1"}
    by_id = {sc.chunk.chunk_id: sc.chunk for sc in result}
    for chunk in chunks:
        assert by_id[chunk.chunk_id] is chunk
        assert by_id[chunk.chunk_id].text == chunk.text
        assert len(by_id[chunk.chunk_id].text) > 600


def test_scoring_char_limit_shorter_than_a_chunk_is_a_no_op_for_that_chunk() -> None:
    recorded: list[httpx.Request] = []
    reranker = TEIReranker(
        "http://tei.local", client=_overlap_client(recorded), scoring_char_limit=10_000
    )
    chunks = _long_chunks(2)

    asyncio.run(reranker.rerank("alpha", chunks, top_k=2))

    assert _payload(recorded[0])["texts"] == [c.text for c in chunks]


def test_scoring_char_limit_rejects_a_non_positive_limit_at_construction() -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError, match="scoring_char_limit"):
            TEIReranker("http://tei.local", scoring_char_limit=bad)
