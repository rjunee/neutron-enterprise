# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Adapter-semantics parity between TEIReranker and VllmReranker.

Why this file exists
====================
"The same model on both servers gives an identical top-10, with scores agreeing
to ~4 decimals" was written into eight places — SPEC, two adapter docstrings, a
settings docstring, the ne-brain README, the runbook, the CLI ``--help``, the
rig script — and pinned by zero tests. Repetition is not evidence; it is how a
claim stops being read as a claim. The sentence rests on ONE side-by-side
measurement taken on the demo rig on 2026-08-04.

That measurement cannot be a unit test: it is a statement about what two
*servers* compute, and neither server is present in CI. What CAN be pinned — and
what the platform actually depends on when it swaps the backend under a live
demo — is the half that lives in THIS repository:

    given equivalent backend responses, the two adapters must produce identical
    ordering, identical tie-breaking, and identical ``top_k`` semantics.

If that ever diverged, "server swap, never a model swap" would be false no matter
how well the two servers agreed on scores, because the same scores would come
back in a different order.

Derived, not transliterated
===========================
The point of the exercise is defeated by writing the expected ordering out twice.
So nothing here restates a rank: one score vector is authored per case, rendered
into each backend's OWN wire shape by :func:`_tei_body` / :func:`_vllm_body`, and
the two adapters' outputs are asserted EQUAL TO EACH OTHER. A change to either
adapter's ordering, tie-break or truncation breaks the equality; a change applied
to both in the same way is caught by
:func:`test_parity_holds_against_an_independently_derived_expected_order`, which
compares against an ordering derived from the score vector by a different route
(``sorted`` on the authored pairs) rather than from either adapter.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ne.brain.reranker import TEIReranker, VllmReranker
from ne.contracts.types import Chunk, ScoredChunk

#: ``(name, scores)`` — ``scores[i]`` is the relevance the backend reports for
#: input chunk ``i``. Authored ONCE per case; every expectation below is derived
#: from it, never restated.
_SCORE_VECTORS: list[tuple[str, list[float]]] = [
    ("strictly-descending", [0.9, 0.5, 0.1]),
    ("strictly-ascending", [0.1, 0.5, 0.9]),
    ("all-tied", [0.5, 0.5, 0.5]),
    ("partial-tie-at-the-top", [0.8, 0.8, 0.3, 0.8]),
    ("partial-tie-at-the-cut", [0.9, 0.4, 0.4, 0.4]),
    ("negative-and-zero", [-1.5, 0.0, -0.25, 2.0]),
    ("single-document", [0.42]),
    ("forty-documents", [i / 40 for i in range(40)]),
]


def _chunks(n: int) -> list[Chunk]:
    return [Chunk(chunk_id=f"c{i}", document_id="d1", text=f"text {i}") for i in range(n)]


def _score_of(text: str, scores: list[float]) -> float:
    """The score the score vector assigns to the chunk whose text this is.

    Both fake servers look their answer up BY THE DOCUMENT THEY WERE SENT, exactly
    as a real reranker does. That is what makes the two responses "equivalent"
    rather than merely "the same literal": the TEI adapter batches client-side, so
    its second request must be answered with batch-relative indices, and a fixture
    that returned one fixed body would be testing the fixture.
    """
    return scores[int(text.removeprefix("text "))]


def _entries(texts: list[str], scores: list[float]) -> list[tuple[int, float]]:
    """``(index-within-this-request, score)`` in an order NO adapter may trust.

    Emitted back-to-front so a client that returned the server's order verbatim
    would disagree with one that re-sorts — the two adapters would then differ on
    every non-symmetric case here rather than agreeing by accident.
    """
    return [(i, _score_of(text, scores)) for i, text in reversed(list(enumerate(texts)))]


def _tei_client(scores: list[float], sink: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """A fake TEI ``/rerank``: request ``{"texts": [...]}`` → a bare list of
    ``{index, score}``, indices relative to THIS request (TEI batches)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if sink is not None:
            sink.append(request)
        texts = json.loads(request.read())["texts"]
        return httpx.Response(
            200, json=[{"index": i, "score": s} for i, s in _entries(texts, scores)]
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _vllm_client(scores: list[float], sink: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    """A fake vLLM (Jina-compatible) ``/rerank``: request ``{"documents": [...]}``
    → ``{"results": [{index, relevance_score, document}]}``, including the echoed
    ``document`` a real vLLM returns."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if sink is not None:
            sink.append(request)
        documents = json.loads(request.read())["documents"]
        return httpx.Response(
            200,
            json={
                "id": "rerank-1",
                "results": [
                    {"index": i, "relevance_score": s, "document": {"text": documents[i]}}
                    for i, s in _entries(documents, scores)
                ],
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(reranker: TEIReranker | VllmReranker, n: int, top_k: int) -> list[ScoredChunk]:
    return list(asyncio.run(reranker.rerank("q", _chunks(n), top_k=top_k)))


def _both(scores: list[float], top_k: int) -> tuple[list[ScoredChunk], list[ScoredChunk]]:
    """Run BOTH adapters over responses that carry the SAME scores for the SAME
    inputs, each in its own backend's wire shape."""
    n = len(scores)
    tei = TEIReranker("http://tei-rerank:80", client=_tei_client(scores))
    vllm = VllmReranker("http://rerank-gpu:80", client=_vllm_client(scores))
    return _run(tei, n, top_k), _run(vllm, n, top_k)


@pytest.mark.parametrize(("name", "scores"), _SCORE_VECTORS, ids=[v[0] for v in _SCORE_VECTORS])
@pytest.mark.parametrize("top_k", [1, 3, 10, 1000])
def test_both_adapters_produce_identical_order_ties_and_top_k(
    name: str, scores: list[float], top_k: int
) -> None:
    """THE pinnable half of the equivalence claim.

    Ordering, tie-breaking and ``top_k`` truncation are compared adapter-to-adapter
    over the same score vector — including ties spanning the ``top_k`` cut, where a
    difference in tie-break rule would change which chunk is dropped.
    """
    tei_result, vllm_result = _both(scores, top_k)

    assert [sc.chunk.chunk_id for sc in tei_result] == [sc.chunk.chunk_id for sc in vllm_result]
    assert [sc.score for sc in tei_result] == [sc.score for sc in vllm_result]
    assert len(tei_result) == min(top_k, len(scores))


@pytest.mark.parametrize(("name", "scores"), _SCORE_VECTORS, ids=[v[0] for v in _SCORE_VECTORS])
def test_parity_holds_against_an_independently_derived_expected_order(
    name: str, scores: list[float]
) -> None:
    """The control that keeps the parity assertion from passing vacuously.

    Adapter-to-adapter equality would survive a change applied identically to
    both. So the shared behaviour is ALSO checked against an ordering derived
    from the score vector by a different route: descending score, ties broken by
    ascending original index. Both adapters must match it.
    """
    top_k = 3
    expected = [
        f"c{index}"
        for index, _ in sorted(enumerate(scores), key=lambda pair: (-pair[1], pair[0]))[:top_k]
    ]

    tei_result, vllm_result = _both(scores, top_k)

    assert [sc.chunk.chunk_id for sc in tei_result] == expected
    assert [sc.chunk.chunk_id for sc in vllm_result] == expected


@pytest.mark.parametrize(("name", "scores"), _SCORE_VECTORS, ids=[v[0] for v in _SCORE_VECTORS])
def test_both_adapters_send_every_candidate_exactly_once(name: str, scores: list[float]) -> None:
    """Parity of the REQUEST, without which parity of the response means nothing:
    both adapters must submit the same documents, in input order, with no
    omissions or duplicates. (They differ in HOW: TEI batches client-side, vLLM
    sends one request — the shape difference this whole adapter pair exists for.)"""
    n = len(scores)
    tei_seen: list[httpx.Request] = []
    vllm_seen: list[httpx.Request] = []

    _run(TEIReranker("http://tei-rerank:80", client=_tei_client(scores, tei_seen)), n, n)
    _run(VllmReranker("http://rerank-gpu:80", client=_vllm_client(scores, vllm_seen)), n, n)

    tei_docs = [text for r in tei_seen for text in json.loads(r.read())["texts"]]
    vllm_docs = [text for r in vllm_seen for text in json.loads(r.read())["documents"]]

    assert tei_docs == vllm_docs == [f"text {i}" for i in range(n)]


def test_both_adapters_return_nothing_for_no_candidates() -> None:
    """Parity at the boundary the pipeline actually hits when ACL trimming empties
    the candidate set: neither adapter may call out, and both return empty."""
    calls: list[httpx.Request] = []

    def _recorder() -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=[])

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    tei = asyncio.run(TEIReranker("http://tei-rerank:80", client=_recorder()).rerank("q", []))
    vllm = asyncio.run(VllmReranker("http://rerank-gpu:80", client=_recorder()).rerank("q", []))

    assert list(tei) == list(vllm) == []
    assert calls == []


def test_the_two_adapters_are_not_the_same_object() -> None:
    """Guards the parity suite against the degenerate way it could pass: if both
    names ever resolved to one class, every assertion above would be a tautology."""
    assert TEIReranker is not VllmReranker
    assert not issubclass(VllmReranker, TEIReranker)
