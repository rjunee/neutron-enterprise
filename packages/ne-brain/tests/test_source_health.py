# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Read-time source-health filtering for the sole T1 retrieval chokepoint."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ne.brain.pipeline import RetrievalPipeline
from ne.brain.retriever import HybridRetriever
from ne.brain.withholding import (
    SOURCE_HEALTH_WITHHOLDING,
    SourceHealthWithholding,
    observe_source_health_withholding,
)
from ne.contracts.governance import AuditVerification
from ne.contracts.types import Chunk, Principal, ScoredChunk, SparseVector


class _Embedder:
    model_id = "fixture"
    dimension = 1

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0] for _ in texts]


class _Sparse:
    model_id = "fixture"

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return [SparseVector(indices=(), values=()) for _ in texts]

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]:
        return await self.encode(texts)


class _Store:
    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}

    async def query(self, **_kwargs: Any) -> Sequence[ScoredChunk]:
        return tuple(ScoredChunk(chunk=chunk, score=1.0) for chunk in self._chunks.values())

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        return tuple(self._chunks[cid] for cid in chunk_ids if cid in self._chunks)

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        return await self.get(chunk_ids)

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        self._chunks.update((chunk.chunk_id, chunk) for chunk in chunks)

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        for chunk_id in chunk_ids:
            self._chunks.pop(chunk_id, None)


class _Health:
    def __init__(self, degraded: frozenset[str]) -> None:
        self.degraded = degraded
        self.calls = 0

    async def __call__(self) -> frozenset[str]:
        self.calls += 1
        return self.degraded


class _Reranker:
    model_id = "fixture"

    def __init__(self) -> None:
        self.seen: list[tuple[str, ...]] = []

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        self.seen.append(tuple(chunk.chunk_id for chunk in chunks))
        return tuple(ScoredChunk(chunk=chunk, score=1.0) for chunk in chunks[:top_k])


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> AuditVerification:
        raise NotImplementedError


def _chunk(chunk_id: str, connector: object = ...) -> Chunk:
    metadata: dict[str, object] = {
        "source_system": "fixture",
        "source_ref": f"ref-{chunk_id}",
    }
    if connector is not ...:
        metadata["connector"] = connector
    return Chunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        text=chunk_id,
        metadata=metadata,
        acl=("eng",),
    )


_PRINCIPAL = Principal(id="u1", groups=("eng",))


def test_degraded_chunk_is_removed_before_rerank_and_later_success_restores_it() -> None:
    chunks = (
        _chunk("degraded", "fixture"),
        _chunk("healthy", "salesforce"),
        _chunk("curated"),
        _chunk("malformed", ""),
    )
    health = _Health(frozenset({"fixture"}))
    reranker = _Reranker()
    pipeline = RetrievalPipeline(
        HybridRetriever(
            _Store(chunks),
            _Embedder(),
            _Sparse(),
            audit_sink=None,
            source_health=health,
        ),
        reranker,
    )

    async def scenario() -> None:
        degraded = await pipeline.search("q", principal=_PRINCIPAL, top_k=10)
        assert [row.chunk.chunk_id for row in degraded.chunks] == ["healthy", "curated"]
        # This is the pre-rerank boundary: neither degraded nor malformed content
        # reached the reranker edge.
        assert reranker.seen == [("healthy", "curated")]

        health.degraded = frozenset()
        recovered = await pipeline.search("q", principal=_PRINCIPAL, top_k=10)
        assert [row.chunk.chunk_id for row in recovered.chunks] == [
            "degraded",
            "healthy",
            "curated",
        ]
        assert reranker.seen[-1] == ("degraded", "healthy", "curated")
        assert health.calls == 2

    asyncio.run(scenario())


def test_retrieve_withholds_same_source_and_keeps_citations_aligned() -> None:
    health = _Health(frozenset({"fixture"}))
    retriever = HybridRetriever(
        _Store((_chunk("degraded", "fixture"), _chunk("curated"))),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=health,
    )

    async def scenario() -> None:
        result = await retriever.retrieve(["degraded", "curated"], principal=_PRINCIPAL)
        assert [row.chunk.chunk_id for row in result.chunks] == ["curated"]
        assert [citation.chunk_id for citation in result.citations] == ["curated"]

    asyncio.run(scenario())


def test_health_provider_failure_propagates_without_serving() -> None:
    async def failing_health() -> frozenset[str]:
        raise RuntimeError("health store unavailable")

    retriever = HybridRetriever(
        _Store((_chunk("connector", "fixture"),)),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=failing_health,
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="health store unavailable"):
            await retriever.search("q", principal=_PRINCIPAL)

    asyncio.run(scenario())


def test_actual_withholding_emits_counts_only_audit(
    frozen_config: str,
) -> None:
    sink = _Sink()
    retriever = HybridRetriever(
        _Store((_chunk("SECRET-ID", "fixture"), _chunk("curated"))),
        _Embedder(),
        _Sparse(),
        audit_sink=sink,
        source_health=_Health(frozenset({"fixture"})),
    )

    async def scenario() -> None:
        result = await retriever.retrieve(
            ["SECRET-ID", "curated"], principal=_PRINCIPAL, trace_id="trace-1"
        )
        assert [row.chunk.chunk_id for row in result.chunks] == ["curated"]
        assert [event.action for event in sink.events] == [
            "knowledge.withhold",
            "knowledge.retrieve",
        ]
        assert sink.events[0].detail == ("op=retrieve returned=1 source_health_trim_delta=1")
        assert sink.events[1].detail == (
            "candidates=2 returned=1 acl_trim_delta=0 source_health_trim_delta=1"
        )
        assert all(event.trace_id == "trace-1" for event in sink.events)
        assert all(event.config_hash == frozen_config for event in sink.events)
        assert "SECRET-ID" not in "".join(event.model_dump_json() for event in sink.events)

    asyncio.run(scenario())


def test_total_withholding_is_observable_and_distinct_from_an_empty_corpus() -> None:
    """A read that matched and disclosed nothing must not look like a read that matched nothing.

    This is the exact production shape it was written for: every candidate the
    store returned carried one connector stamp, that connector's latest sync fact
    was ``degraded``, and the caller received ``chunks == ()``. Without the
    observation the caller cannot tell that from a corpus that simply had no
    match, and renders "0 of 0" over a corpus it withheld in full.
    """
    chunks = (_chunk("a", "m365"), _chunk("b", "m365"), _chunk("c", "m365"))
    retriever = HybridRetriever(
        _Store(chunks),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=_Health(frozenset({"m365"})),
    )

    async def _run() -> tuple[int, SourceHealthWithholding]:
        with observe_source_health_withholding() as observed:
            result = await retriever.search("q", principal=_PRINCIPAL, top_k=10)
        return len(result.chunks), observed

    served, observed = asyncio.run(_run())
    assert served == 0
    assert observed.candidates == 3
    assert observed.withheld == 3
    assert observed.total_blackout is True


def test_a_genuinely_empty_corpus_reports_no_withholding() -> None:
    """Zero served with zero withheld is the honest empty case and must stay distinguishable."""
    retriever = HybridRetriever(
        _Store(()),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=_Health(frozenset({"m365"})),
    )

    async def _run() -> tuple[int, SourceHealthWithholding]:
        with observe_source_health_withholding() as observed:
            result = await retriever.search("q", principal=_PRINCIPAL, top_k=10)
        return len(result.chunks), observed

    served, observed = asyncio.run(_run())
    assert served == 0
    assert observed.candidates == 0
    assert observed.withheld == 0
    assert observed.total_blackout is False


def test_partial_withholding_is_not_a_blackout() -> None:
    """One degraded connector among several narrows the result; it does not empty it."""
    chunks = (_chunk("a", "m365"), _chunk("b", "salesforce"), _chunk("c", "m365"))
    retriever = HybridRetriever(
        _Store(chunks),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=_Health(frozenset({"m365"})),
    )

    async def _run() -> tuple[int, SourceHealthWithholding]:
        with observe_source_health_withholding() as observed:
            result = await retriever.search("q", principal=_PRINCIPAL, top_k=10)
        return len(result.chunks), observed

    served, observed = asyncio.run(_run())
    assert served == 1
    assert observed.withheld == 2
    assert observed.total_blackout is False


def test_recording_without_an_open_observation_is_a_no_op() -> None:
    """No caller asked, so nothing is carried — the pre-existing behaviour, byte-identical."""
    retriever = HybridRetriever(
        _Store((_chunk("a", "m365"),)),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=_Health(frozenset({"m365"})),
    )
    result = asyncio.run(retriever.search("q", principal=_PRINCIPAL, top_k=10))
    assert result.chunks == ()
    assert SOURCE_HEALTH_WITHHOLDING.get() is None


def test_impossible_counts_are_refused_rather_than_recorded() -> None:
    """The observation is evidence; a count it cannot justify must raise, never round off."""
    observation = SourceHealthWithholding()
    with pytest.raises(ValueError, match="impossible withholding counts"):
        observation.record(candidates=1, withheld=2)
    with pytest.raises(ValueError, match="impossible withholding counts"):
        observation.record(candidates=-1, withheld=0)


def test_stale_source_is_served_and_counted_not_withheld() -> None:
    """The stale leg: a source in the snapshot's ``stale`` set is SERVED.

    Its served-chunk count rides the ambient observation for the renderer's
    caveat; nothing is withheld, and the unavailable leg keeps withholding.
    """
    from ne.brain.source_health import SourceHealthSnapshot

    chunks = (_chunk("a", "m365"), _chunk("b", "salesforce"), _chunk("c", "m365"), _chunk("d"))
    retriever = HybridRetriever(
        _Store(chunks),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=lambda: SourceHealthSnapshot(
            unavailable=frozenset({"salesforce"}), stale=frozenset({"m365"})
        ),
    )

    async def _run() -> tuple[list[str], SourceHealthWithholding]:
        with observe_source_health_withholding() as observed:
            result = await retriever.search("q", principal=_PRINCIPAL, top_k=10)
        return [row.chunk.chunk_id for row in result.chunks], observed

    served, observed = asyncio.run(_run())
    assert served == ["a", "c", "d"]
    assert observed.withheld == 1
    assert observed.stale_served == 2


def test_bare_frozenset_provider_still_means_withhold() -> None:
    """The legacy provider shape (a bare set) remains the unavailable set."""
    retriever = HybridRetriever(
        _Store((_chunk("a", "m365"), _chunk("b"))),
        _Embedder(),
        _Sparse(),
        audit_sink=None,
        source_health=_Health(frozenset({"m365"})),
    )

    async def _run() -> tuple[list[str], SourceHealthWithholding]:
        with observe_source_health_withholding() as observed:
            result = await retriever.search("q", principal=_PRINCIPAL, top_k=10)
        return [row.chunk.chunk_id for row in result.chunks], observed

    served, observed = asyncio.run(_run())
    assert served == ["b"]
    assert observed.withheld == 1
    assert observed.stale_served == 0
