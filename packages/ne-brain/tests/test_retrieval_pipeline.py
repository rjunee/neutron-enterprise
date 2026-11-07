# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Orchestration unit tests for RetrievalPipeline + permission-seam artifacts.

The orchestration tests stub the pipeline's own collaborators (a recording
fake retriever and a recording stub reranker). The permission-seam tests run
against the REAL stack (:memory: Qdrant + HybridRetriever + TEIReranker over a
MockTransport) to prove the pipeline never surfaces an ACL-trimmed chunk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.pipeline import (
    CurrentAuthorizedRetrievalPipeline,
    RerankedRetrievalResult,
    RetrievalPipeline,
)
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.reranker import TEIReranker
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder
from ne.contracts.source_authority import (
    AuthorizedArtifactPageStatus,
    AuthorizedRetrievalEnvelope,
)
from ne.contracts.types import (
    Chunk,
    Principal,
    RetrievalResult,
    ScoredChunk,
    SparseVector,
)

_PRINCIPAL = Principal(id="u1", groups=("group-a",))


class _RecordingRetriever:
    """Fake KnowledgeRead: records call kwargs, returns a canned result."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.search_calls: list[dict[str, Any]] = []
        self.retrieve_calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        self.search_calls.append(
            {"query": query, "principal": principal, "top_k": top_k, "filters": filters}
        )
        return self._result

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        self.retrieve_calls.append({"chunk_ids": list(chunk_ids), "principal": principal})
        return self._result


class _RecordingReranker:
    """Stub Reranker: records calls, returns the chunks in a caller-chosen order."""

    def __init__(self, order: str = "identity") -> None:
        self._order = order
        self.calls: list[dict[str, Any]] = []

    @property
    def model_id(self) -> str:
        return "stub-reranker"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        self.calls.append({"query": query, "chunks": list(chunks), "top_k": top_k})
        ordered = list(chunks)
        if self._order == "reverse":
            ordered = list(reversed(ordered))
        return tuple(
            ScoredChunk(chunk=c, score=float(len(ordered) - i)) for i, c in enumerate(ordered)
        )[:top_k]


def _canned(chunks: Sequence[Chunk]) -> RetrievalResult:
    return RetrievalResult(
        chunks=tuple(ScoredChunk(chunk=c, score=1.0) for c in chunks),
        citations=(),
    )


def _chunk(cid: str, **meta: Any) -> Chunk:
    return Chunk(chunk_id=cid, document_id=f"doc-{cid}", text=f"text {cid}", metadata=meta)


# --- orchestration unit tests ----------------------------------------------


def test_search_requests_candidate_multiplier_times_top_k() -> None:
    retriever = _RecordingRetriever(_canned([_chunk("a")]))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(), candidate_multiplier=4)

    asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    assert retriever.search_calls[0]["top_k"] == 8


def test_search_passes_principal_and_filters_through_unchanged() -> None:
    retriever = _RecordingRetriever(_canned([_chunk("a")]))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker())
    filters = {"acl": ["group-a"]}

    asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=3, filters=filters))

    call = retriever.search_calls[0]
    assert call["principal"] is _PRINCIPAL
    assert call["filters"] == filters


def test_search_returns_reranked_order_not_fusion_order() -> None:
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    retriever = _RecordingRetriever(_canned(chunks))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(order="reverse"))

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=3))

    assert [sc.chunk.chunk_id for sc in result.chunks] == ["c", "b", "a"]


def test_search_citations_align_with_reranked_chunks() -> None:
    chunks = [
        _chunk("a", source_system="confluence", source_ref="PAGE-A"),
        _chunk("b", source_system="sharepoint", source_ref="DOC-B"),
    ]
    retriever = _RecordingRetriever(_canned(chunks))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(order="reverse"))

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    assert len(result.citations) == len(result.chunks)
    for scored, citation in zip(result.chunks, result.citations, strict=True):
        assert citation.chunk_id == scored.chunk.chunk_id
        assert citation.document_id == scored.chunk.document_id
        assert citation.source_system == scored.chunk.metadata.get("source_system", "")
        assert citation.source_ref == scored.chunk.metadata.get(
            "source_ref", scored.chunk.document_id
        )


def test_search_empty_candidates_skips_rerank() -> None:
    retriever = _RecordingRetriever(RetrievalResult(chunks=(), citations=()))
    reranker = _RecordingReranker()
    pipeline = RetrievalPipeline(retriever, reranker)

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=3))

    assert result.chunks == ()
    assert result.citations == ()
    assert reranker.calls == []


def test_retrieve_delegates_verbatim_without_rerank() -> None:
    chunks = [_chunk("a"), _chunk("b")]
    retriever = _RecordingRetriever(_canned(chunks))
    reranker = _RecordingReranker()
    pipeline = RetrievalPipeline(retriever, reranker)

    result = asyncio.run(pipeline.retrieve(["a", "b"], principal=_PRINCIPAL))

    assert retriever.retrieve_calls[0]["chunk_ids"] == ["a", "b"]
    assert reranker.calls == []
    assert [sc.chunk.chunk_id for sc in result.chunks] == ["a", "b"]


def test_candidate_multiplier_below_one_raises_value_error() -> None:
    with pytest.raises(ValueError):
        RetrievalPipeline(
            _RecordingRetriever(_canned([])), _RecordingReranker(), candidate_multiplier=0
        )


# --- NE-RERANK-FANOUT: the rerank pool has a ceiling ------------------------


class _RankedRetriever(_RecordingRetriever):
    """A store fake that HONOURS ``top_k`` — the ceiling only shows up if it does."""

    def __init__(self, corpus: Sequence[Chunk]) -> None:
        super().__init__(_canned(corpus))
        self._corpus = tuple(corpus)

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        await super().search(query, principal=principal, top_k=top_k, filters=filters)
        return _canned(self._corpus[:top_k])


def test_max_candidate_k_caps_the_rerank_pool() -> None:
    """The ceiling, not the multiplier, is what bounds rerank cost.

    ``RetrievalPipeline`` sits under ``CurrentSourceAuthorizedKnowledgeRead``,
    whose own authorization over-fetch arrives here as ``top_k``. Re-multiplying
    that window is how a ``top_k=5`` Ask came to rerank 160 documents.
    """
    retriever = _RecordingRetriever(_canned([_chunk("a")]))
    pipeline = RetrievalPipeline(
        retriever, _RecordingReranker(), candidate_multiplier=4, max_candidate_k=40
    )

    asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=20))

    # 20 * 4 = 80 uncapped.
    assert retriever.search_calls[0]["top_k"] == 40


def test_max_candidate_k_never_shrinks_the_page_the_caller_asked_for() -> None:
    """``max(top_k, ...)`` is load-bearing: a ceiling bounds COST, never the page."""
    retriever = _RecordingRetriever(_canned([_chunk("a")]))
    pipeline = RetrievalPipeline(
        retriever, _RecordingReranker(), candidate_multiplier=4, max_candidate_k=40
    )

    asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=100))

    assert retriever.search_calls[0]["top_k"] == 100


def test_max_candidate_k_defaults_to_uncapped() -> None:
    retriever = _RecordingRetriever(_canned([_chunk("a")]))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(), candidate_multiplier=4)

    asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=30))

    assert retriever.search_calls[0]["top_k"] == 120


def test_max_candidate_k_below_one_raises_value_error() -> None:
    with pytest.raises(ValueError):
        RetrievalPipeline(_RecordingRetriever(_canned([])), _RecordingReranker(), max_candidate_k=0)


def test_a_capped_pipeline_still_hands_the_reranker_more_than_it_returns() -> None:
    """The multiplier's PURPOSE — a pool wider than the output — survives the cap."""
    retriever = _RankedRetriever([_chunk(f"c{i:03d}") for i in range(200)])
    reranker = _RecordingReranker()
    pipeline = RetrievalPipeline(retriever, reranker, candidate_multiplier=4, max_candidate_k=40)

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=10))

    assert len(reranker.calls[0]["chunks"]) == 40
    assert len(result.chunks) == 10


def test_the_capped_candidate_count_is_the_one_the_timing_line_reports(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The instrument must report the pool actually reranked, not ``top_k * 4``.

    This line is the only in-process evidence of the fan-out, so a ceiling the
    log did not know about would leave the next latency investigation reading a
    number the reranker never saw.
    """
    retriever = _RankedRetriever([_chunk(f"c{i:03d}") for i in range(200)])
    pipeline = RetrievalPipeline(
        retriever, _RecordingReranker(), candidate_multiplier=4, max_candidate_k=40
    )

    with caplog.at_level(logging.INFO, logger="ne.brain.pipeline"):
        asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=10))

    top_k, candidate_k, candidate_count, _, _ = _timing_args(_only_timing_record(caplog))
    assert (top_k, candidate_k, candidate_count) == (10, 40, 40)


def test_search_propagates_a_rerank_failure_instead_of_degrading_silently() -> None:
    """A dead/slow rerank backend HARD-FAILS the search — no fused-order fallback.

    Pinned here because the NE-RERANK-CPU latency work makes the tempting "fix"
    for a slow reranker a ``try``/``except`` that returns fused order. The
    ``RetrievalPipeline`` docstring argues at length that a silent no-rerank
    degradation is worse than a loud failure; this is the assertion behind that
    argument. If ``search`` ever grows a fallback, this goes RED.
    """

    class _DeadReranker:
        @property
        def model_id(self) -> str:
            return "dead-reranker"

        async def rerank(
            self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
        ) -> Sequence[ScoredChunk]:
            raise httpx.ReadTimeout("rerank backend timed out")

    retriever = _RecordingRetriever(_canned([_chunk("a"), _chunk("b")]))
    pipeline = RetrievalPipeline(retriever, _DeadReranker(), candidate_multiplier=4)

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))
    # And the candidates were really fetched — the raise is the rerank stage's,
    # not an early return that never reached it.
    assert len(retriever.search_calls) == 1


def test_search_returns_full_chunk_text_from_the_reranker() -> None:
    """The other half of the pair: a SUCCESSFUL rerank returns whole chunks.

    ``scoring_char_limit`` lets an operator shrink the text a rerank SERVER
    scores. Whatever the adapter sent on the wire, the pipeline's result must
    carry the chunk the retriever produced, byte-for-byte, because that text is
    what the reader and the citation are built from.
    """
    long_chunks = [
        Chunk(chunk_id=cid, document_id=f"doc-{cid}", text=f"{cid} " + "lorem ipsum " * 200)
        for cid in ("a", "b")
    ]
    retriever = _RecordingRetriever(_canned(long_chunks))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(), candidate_multiplier=4)

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    by_id = {sc.chunk.chunk_id: sc.chunk for sc in result.chunks}
    for chunk in long_chunks:
        assert by_id[chunk.chunk_id].text == chunk.text
        assert len(by_id[chunk.chunk_id].text) > 2000


def test_search_result_carries_candidate_count() -> None:
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    retriever = _RecordingRetriever(_canned(chunks))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker())

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    assert isinstance(result, RerankedRetrievalResult)
    assert result.candidate_count == 3
    assert len(result.chunks) == 2


def test_search_result_is_still_a_retrieval_result() -> None:
    retriever = _RecordingRetriever(_canned([_chunk("a"), _chunk("b")]))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker())

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    assert isinstance(result, RetrievalResult)


def test_search_empty_candidates_returns_plain_base_result() -> None:
    retriever = _RecordingRetriever(RetrievalResult(chunks=(), citations=()))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker())

    result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=3))

    assert result == RetrievalResult(chunks=(), citations=())
    assert not isinstance(result, RerankedRetrievalResult)


class _CurrentRetriever:
    def __init__(self, envelope: AuthorizedRetrievalEnvelope) -> None:
        self.envelope = envelope
        self.search_calls: list[dict[str, Any]] = []

    async def search_current(
        self,
        query: str,
        *,
        principal: Principal,
        access_context_hash: str,
        request_hash: str,
        top_k: int = 10,
        scan_limit: int = 100,
        filters: Mapping[str, Any] | None = None,
        continuation_token: str | None = None,
    ) -> AuthorizedRetrievalEnvelope:
        self.search_calls.append(
            {
                "query": query,
                "principal": principal,
                "access_context_hash": access_context_hash,
                "request_hash": request_hash,
                "top_k": top_k,
                "scan_limit": scan_limit,
                "filters": filters,
                "continuation_token": continuation_token,
            }
        )
        return self.envelope

    async def retrieve_current(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> AuthorizedRetrievalEnvelope:
        return self.envelope


def test_current_pipeline_preserves_incomplete_empty_envelope_without_reranking() -> None:
    envelope = AuthorizedRetrievalEnvelope(
        result=RetrievalResult(chunks=(), citations=()),
        status=AuthorizedArtifactPageStatus.EXHAUSTED,
        scanned_count=2,
        returned_count=0,
        scan_limit=2,
        continuation_token="opaque",
    )
    retriever = _CurrentRetriever(envelope)
    reranker = _RecordingReranker()
    pipeline = CurrentAuthorizedRetrievalPipeline(retriever, reranker)

    result = asyncio.run(
        pipeline.search(
            "q",
            principal=_PRINCIPAL,
            access_context_hash="1" * 64,
            request_hash="2" * 64,
            scan_limit=2,
        )
    )

    assert result == envelope
    assert result.status is AuthorizedArtifactPageStatus.EXHAUSTED
    assert result.continuation_token == "opaque"
    assert reranker.calls == []


def test_current_pipeline_reranks_only_disclosed_items_and_preserves_page_truth() -> None:
    chunks = [_chunk("a"), _chunk("b")]
    envelope = AuthorizedRetrievalEnvelope(
        result=_canned(chunks),
        status=AuthorizedArtifactPageStatus.PARTIAL,
        scanned_count=4,
        returned_count=2,
        scan_limit=4,
        continuation_token="opaque",
    )
    pipeline = CurrentAuthorizedRetrievalPipeline(
        _CurrentRetriever(envelope), _RecordingReranker(order="reverse")
    )

    result = asyncio.run(
        pipeline.search(
            "q",
            principal=_PRINCIPAL,
            access_context_hash="1" * 64,
            request_hash="2" * 64,
            top_k=2,
            scan_limit=4,
        )
    )

    assert [item.chunk.chunk_id for item in result.result.chunks] == ["b", "a"]
    assert result.status is AuthorizedArtifactPageStatus.PARTIAL
    assert (result.scanned_count, result.returned_count, result.scan_limit) == (4, 2, 4)
    assert result.continuation_token == "opaque"


def test_current_pipeline_refuses_reranker_cardinality_change() -> None:
    envelope = AuthorizedRetrievalEnvelope(
        result=_canned([_chunk("a"), _chunk("b")]),
        status=AuthorizedArtifactPageStatus.COMPLETE,
        scanned_count=2,
        returned_count=2,
        scan_limit=2,
    )
    pipeline = CurrentAuthorizedRetrievalPipeline(_CurrentRetriever(envelope), _RecordingReranker())

    with pytest.raises(ValueError, match="authorized page cardinality"):
        asyncio.run(
            pipeline.search(
                "q",
                principal=_PRINCIPAL,
                access_context_hash="1" * 64,
                request_hash="2" * 64,
                top_k=1,
                scan_limit=2,
            )
        )


# --- permission-seam artifacts over the REAL stack -------------------------


class _StubSparseEncoder:
    @property
    def model_id(self) -> str:
        return "stub-sparse"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [
            SparseVector(indices=(zlib.crc32(t.encode()) % 4096,), values=(1.0,)) for t in texts
        ]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return await self.encode(texts)


_allowed = Chunk(chunk_id="open-1", document_id="d1", text="quarterly revenue", acl=("group-a",))
_denied = Chunk(chunk_id="secret-1", document_id="d2", text="secret target list", acl=("group-b",))
_seam_principal = Principal(id="u1", groups=("group-a",))


def _group_acl(principal: Principal) -> Mapping[str, Any] | None:
    return {"acl": list(principal.groups)}


def _rerank_client_scoring_denied_highest() -> httpx.AsyncClient:
    """Scores the denied chunk's text highest, so if the seam leaked it would rank #1."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        entries = [
            {"index": i, "score": 10.0 if "secret" in text else 0.0}
            for i, text in enumerate(body["texts"])
        ]
        return httpx.Response(200, json=entries)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _real_pipeline() -> RetrievalPipeline:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(client, f"seam_{uuid.uuid4().hex}", embedder, _StubSparseEncoder())

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks([_allowed, _denied])

    asyncio.run(setup())
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=_group_acl, audit_sink=None
    )
    reranker = TEIReranker("http://tei.local", client=_rerank_client_scoring_denied_highest())
    return RetrievalPipeline(retriever, reranker)


def test_pipeline_search_never_surfaces_acl_trimmed_chunk() -> None:
    pipeline = _real_pipeline()

    async def scenario() -> None:
        result = await pipeline.search("secret target list", principal=_seam_principal, top_k=10)
        ids = {sc.chunk.chunk_id for sc in result.chunks}
        assert "secret-1" not in ids

    asyncio.run(scenario())


def test_pipeline_retrieve_trims_denied_chunk_id() -> None:
    pipeline = _real_pipeline()

    async def scenario() -> None:
        result = await pipeline.retrieve(["secret-1"], principal=_seam_principal)
        assert result.chunks == ()
        assert result.citations == ()

    asyncio.run(scenario())


# -- NE-164: trace_id-as-data forwarding to the T1 read-path audit event ----


class _RecordingAuditSink:
    """Test-double ``AuditSink`` recording every appended event verbatim."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> Any:
        raise NotImplementedError


def test_pipeline_retrieve_forwards_trace_id_to_t1_event(frozen_config: str) -> None:
    client = AsyncQdrantClient(":memory:")
    embedder = HashEmbedder(32)
    store = QdrantChunkStore(client, f"trace_{uuid.uuid4().hex}", embedder, _StubSparseEncoder())

    async def setup() -> None:
        await store.ensure_collection()
        await store.upsert_chunks([_allowed])

    asyncio.run(setup())
    sink = _RecordingAuditSink()
    retriever = HybridRetriever(
        store, embedder, _StubSparseEncoder(), acl_filters=_group_acl, audit_sink=sink
    )
    pipeline = RetrievalPipeline(retriever, _RecordingReranker())
    trace_id = "t" * 32

    async def scenario() -> None:
        await pipeline.retrieve(["open-1"], principal=_seam_principal, trace_id=trace_id)
        assert len(sink.events) == 1
        assert sink.events[0].trace_id == trace_id

    asyncio.run(scenario())


# -- per-search timing instrument: where does a slow search actually go? ----


class _SlowRetriever(_RecordingRetriever):
    """``_RecordingRetriever`` that burns a known amount of wall clock."""

    def __init__(self, result: RetrievalResult, delay_s: float) -> None:
        super().__init__(result)
        self._delay_s = delay_s

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        await asyncio.sleep(self._delay_s)
        return await super().search(query, principal=principal, top_k=top_k, filters=filters)


class _SlowReranker(_RecordingReranker):
    """``_RecordingReranker`` that burns a known amount of wall clock."""

    def __init__(self, delay_s: float, order: str = "identity") -> None:
        super().__init__(order=order)
        self._delay_s = delay_s

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        await asyncio.sleep(self._delay_s)
        return await super().rerank(query, chunks, top_k=top_k)


def _timing_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "ne.brain.pipeline"]


def _only_timing_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = _timing_records(caplog)
    assert len(records) == 1, f"expected exactly one timing line, got {len(records)}"
    return records[0]


def _timing_args(record: logging.LogRecord) -> tuple[int, int, int, int, int]:
    """The five values the timing line interpolates, in order.

    Doubles as the content-free floor: every arg must be a plain ``int``, so
    nothing text-shaped can ride out on this record.
    """
    args = record.args
    assert isinstance(args, tuple), "the timing line must use lazy %-style args"
    values = [arg for arg in args if isinstance(arg, int)]
    assert len(values) == len(args) == 5, f"expected five integer args, got {args!r}"
    return values[0], values[1], values[2], values[3], values[4]


def test_search_logs_one_timing_line_with_top_k_candidate_k_and_candidate_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [_chunk(str(i)) for i in range(7)]
    retriever = _RecordingRetriever(_canned(chunks))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(), candidate_multiplier=4)

    with caplog.at_level(logging.INFO, logger="ne.brain.pipeline"):
        result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=3))

    record = _only_timing_record(caplog)
    assert record.levelno == logging.INFO
    top_k, candidate_k, candidate_count, retriever_ms, rerank_ms = _timing_args(record)
    # top_k as requested; candidate_k as computed; candidates as actually reranked.
    assert (top_k, candidate_k, candidate_count) == (3, 12, 7)
    # Lazy %-style args: the record holds them, unrendered, until a handler asks.
    assert "top_k=%d" in record.msg
    assert record.getMessage() == (
        "retrieval pipeline search: top_k=3 candidate_k=12 candidates=7 "
        f"retriever_ms={retriever_ms} rerank_ms={rerank_ms}"
    )
    # Behaviour unchanged by the instrument.
    assert isinstance(result, RerankedRetrievalResult)
    assert result.candidate_count == 7
    assert [sc.chunk.chunk_id for sc in result.chunks] == ["0", "1", "2"]


def test_search_attributes_a_slow_retriever_to_retriever_ms_not_rerank_ms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point: a slow RETRIEVER must not be charged to the rerank span."""
    retriever = _SlowRetriever(_canned([_chunk("a"), _chunk("b")]), delay_s=0.20)
    pipeline = RetrievalPipeline(retriever, _RecordingReranker(), candidate_multiplier=4)

    with caplog.at_level(logging.INFO, logger="ne.brain.pipeline"):
        asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    _, _, _, retriever_ms, rerank_ms = _timing_args(_only_timing_record(caplog))
    assert retriever_ms >= 150
    assert rerank_ms < 100


def test_search_attributes_a_slow_reranker_to_rerank_ms_not_retriever_ms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mirror image: a slow RERANKER must not be charged to the retriever span."""
    retriever = _RecordingRetriever(_canned([_chunk("a"), _chunk("b")]))
    pipeline = RetrievalPipeline(retriever, _SlowReranker(delay_s=0.20), candidate_multiplier=4)

    with caplog.at_level(logging.INFO, logger="ne.brain.pipeline"):
        asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=2))

    _, _, _, retriever_ms, rerank_ms = _timing_args(_only_timing_record(caplog))
    assert rerank_ms >= 150
    assert retriever_ms < 100


def test_search_logs_the_empty_candidate_path_with_rerank_ms_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The early return still logs — rerank_ms=0 because the stage never ran."""
    retriever = _SlowRetriever(RetrievalResult(chunks=(), citations=()), delay_s=0.20)
    reranker = _RecordingReranker()
    pipeline = RetrievalPipeline(retriever, reranker, candidate_multiplier=4)

    with caplog.at_level(logging.INFO, logger="ne.brain.pipeline"):
        result = asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=3))

    top_k, candidate_k, candidate_count, retriever_ms, rerank_ms = _timing_args(
        _only_timing_record(caplog)
    )
    assert (top_k, candidate_k, candidate_count, rerank_ms) == (3, 12, 0, 0)
    assert retriever_ms >= 150
    # And the early return is byte-identical to what it was before the instrument.
    assert result == RetrievalResult(chunks=(), citations=())
    assert not isinstance(result, RerankedRetrievalResult)
    assert reranker.calls == []


def test_search_timing_line_carries_counts_and_durations_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C7: the log stream is shared, so the line may not carry retrieved content."""
    secret_query = "zzz-query-needle"
    chunks = [
        Chunk(
            chunk_id="zzz-chunk-needle",
            document_id="zzz-doc-needle",
            text="zzz-text-needle",
            metadata={"source_ref": "zzz-ref-needle"},
        )
    ]
    principal = Principal(id="zzz-principal-needle", groups=("zzz-group-needle",))
    retriever = _RecordingRetriever(_canned(chunks))
    pipeline = RetrievalPipeline(retriever, _RecordingReranker())

    with caplog.at_level(logging.INFO, logger="ne.brain.pipeline"):
        asyncio.run(pipeline.search(secret_query, principal=principal, top_k=1))

    record = _only_timing_record(caplog)
    rendered = record.getMessage()
    assert "needle" not in rendered
    # Every interpolated value is a plain integer — nothing can smuggle text in.
    _timing_args(record)
    # Nor is content hiding on the record itself (exc_info/stack/extra).
    assert record.exc_info is None
    assert record.stack_info is None


def test_search_emits_no_timing_line_when_the_rerank_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The instrument is an observer, not a handler: it swallows nothing.

    Paired with ``test_search_propagates_a_rerank_failure_instead_of_degrading_silently``:
    that test proves the exception still escapes, this one proves the reason is
    that no ``try``/``except`` was added to log a partial line on the way out.
    """

    class _DeadReranker:
        @property
        def model_id(self) -> str:
            return "dead-reranker"

        async def rerank(
            self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
        ) -> Sequence[ScoredChunk]:
            raise httpx.ReadTimeout("rerank backend timed out")

    retriever = _RecordingRetriever(_canned([_chunk("a")]))
    pipeline = RetrievalPipeline(retriever, _DeadReranker(), candidate_multiplier=4)

    with (
        caplog.at_level(logging.INFO, logger="ne.brain.pipeline"),
        pytest.raises(httpx.ReadTimeout),
    ):
        asyncio.run(pipeline.search("q", principal=_PRINCIPAL, top_k=1))

    assert _timing_records(caplog) == []
