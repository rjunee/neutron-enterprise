# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``RetrievalPipeline`` — the production T1 retrieval path (NE-022).

Composes the existing hybrid retriever with a rerank stage and a citation
rebuild into :class:`ne.contracts.knowledge.KnowledgeRead`'s ``search``/
``retrieve`` surface (SPEC §5 NE-022):

    query
      -> embed (dense TEI + sparse BM25)
      -> QdrantChunkStore server-side RRF fused retrieve (candidate_k)
      -> TEIReranker (top_k)
      -> citations with source-system IDs (C7)
      -> RetrievalResult

``search`` over-fetches ``candidate_k`` fused candidates from the underlying
retriever, then reranks down to ``top_k`` and rebuilds citations in **reranked
order** (1:1 aligned)::

    candidate_k = max(top_k, min(top_k * candidate_multiplier, max_candidate_k))

Raising ``top_k`` therefore multiplies BOTH the fused-candidate fetch AND the
number of candidates scored by the reranker per search (every fused candidate is
sent through ``Reranker.rerank`` in one call) — a caller who bumps a configured
``top_k`` (SPEC §5 NE-CTXB-6) is directly multiplying per-search rerank
calls/tokens and underlying retrieval load — but ``max_candidate_k`` bounds how
far that can run.

``max_candidate_k`` — the ceiling that stops two over-fetches compounding
------------------------------------------------------------------------
This pipeline is not the only stage that over-fetches. In the composed platform
it sits UNDER ``ne.api.tools.CurrentSourceAuthorizedKnowledgeRead``, which asks
for more than the caller's page so that per-chunk current-source authorization
can drop rows and still fill it. That wrapper's window arrives here as ``top_k``
— and until NE-RERANK-FANOUT this class multiplied it AGAIN, so an Ask for 5
chunks reranked ``5 * 8 * 4 = 160`` documents while ``ne.brain.reranker``'s own
docstring and the documented pool still described a pool of 20-40. Measured on the
demo box, that pool was ``rerank_ms=227271`` of a 229 s search: 98.9%, in 5
sequential HTTP calls (TEI refuses batches above 32).

The two over-fetches exist for two different, both-real reasons — the reranker
needs a pool bigger than what it emits, the authorization wrapper needs rows it
can afford to lose — so neither is removed. They are made to compose ONCE over
the final page instead of multiplying each other: the wrapper's multiplier is
now the authorization headroom only (``ne.api.tools``), and ``max_candidate_k``
caps what any caller, however deep its own over-fetch, can turn into rerank
work. ``max(top_k, ...)`` keeps the cap from ever shrinking the page the caller
asked for: a ceiling may flatten the reranker to a pure re-orderer for a very
large ``top_k``, and that is the deliberate trade — bounded cost over a wider
selection pool. ``None`` (the default) leaves the pipeline uncapped.

⚠️ The Checkpoint-C eval gates were measured with the pre-NE-RERANK-FANOUT pool
(P2-19). A smaller pool can change which
chunks surface; those numbers need re-measuring and nobody may quote them as
covering this pool size.

The rerank stage has NO fallback, deliberately
--------------------------------------------
``search`` awaits ``Reranker.rerank`` with no ``try``/``except``: no retry, no
circuit breaker, no second backend. A rerank backend that is slow, dead, or
refusing (a preempted GPU rig, an engaged air gap over an allowlisted external
host) therefore HARD-FAILS the whole search — after up to
``NE_TEI_RERANK_TIMEOUT_S`` (the demo overlay sets 180 s), surfacing as a 502.

This is a decision, not an oversight. A ``RetrievalPipeline`` holds exactly ONE
``Reranker``, so the only degradation available here is *skipping* the stage and
returning fused-retrieval order as though it had been scored — a silent quality
downgrade of the stage the Checkpoint-C eval gated, invisible to the caller and
to the audit record. A loud failure the operator can see and revert beats a quiet
wrong answer. The remedy is configuration: point ``NE_API_RERANK_BACKEND`` back
at ``tei`` (the sealed on-box CPU lane) and restart. A real automatic fallback
means composing a SECOND reranker plus a health policy, and owning eval evidence
that the fallback's results are acceptable to serve — a feature, with its own
gate, not an exception handler. ``ne.brain.reranker.VllmReranker`` carries the same warning.

Permission-filter hook: this class adds NO second permission path. The seam is
the underlying ``HybridRetriever``'s ``acl_filters`` hook — REAL as of NE-043
(``ne.brain.trimming.PrincipalAclFilter`` in production). Both ``search`` and
``retrieve`` ride through the delegated retriever, so C7 ACL trimming happens
exactly once, below this pipeline.

Timing instrument (search only): every :meth:`RetrievalPipeline.search` call
emits ONE INFO line splitting its wall clock between the two awaits it makes —
the delegated retriever call and the rerank call — alongside the three counts
that drive them (``top_k``, the computed ``candidate_k``, and the number of
candidates actually handed to the reranker). It answers exactly one question:
of a slow search, how much is retrieval and how much is rerank. The line is
COUNTS AND DURATIONS ONLY — no query text, no chunk text, no ids, no hashes, no
principal — because this logger's stream is shared. It is an observer: it adds
no ``try``/``except``, so a rerank that raises still propagates untouched (and
simply logs nothing, the failure being the audible signal).

NE-164 trace threading: ``retrieve`` forwards an optional ``trace_id`` (data
only, per NE-166's "trace_id as data" philosophy — no ``ne.runtime`` import
here or anywhere in ne-brain) to the underlying ``HybridRetriever`` so its
read-path ``AuditEvent`` carries the caller's run-scoped correlation id. The
forward is via ``isinstance`` narrowing (the declared ``retriever:
KnowledgeRead`` constructor type stays a Protocol, so conformance kits and
other ``KnowledgeRead`` implementations are untouched): a non-``HybridRetriever``
delegate (a fake, or a future non-audited implementation) simply drops the
``trace_id`` — its own ``retrieve`` is called without it, and, if it emits no
read-path event at all, the trace id has nowhere to go anyway.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ne.brain.retriever import HybridRetriever, citation_for
from ne.contracts.knowledge import KnowledgeRead, Reranker
from ne.contracts.source_authority import AuthorizedRetrievalEnvelope
from ne.contracts.types import Principal, RetrievalResult

_LOG = logging.getLogger(__name__)


def _log_search_timing(
    *,
    top_k: int,
    candidate_k: int,
    candidate_count: int,
    retriever_ms: int,
    rerank_ms: int,
) -> None:
    """Emit the one per-search timing line. Counts and durations ONLY (C7-safe)."""
    _LOG.info(
        "retrieval pipeline search: top_k=%d candidate_k=%d candidates=%d "
        "retriever_ms=%d rerank_ms=%d",
        top_k,
        candidate_k,
        candidate_count,
        retriever_ms,
        rerank_ms,
    )


class RerankedRetrievalResult(RetrievalResult):
    """``RetrievalResult`` plus the pre-rerank candidate count (NE-CTXB-6).

    ``candidate_count`` is the number of fused, ACL-trimmed candidates the
    pipeline retrieved from the underlying ``KnowledgeRead`` BEFORE reranking
    down to ``top_k`` — i.e. ``len(candidates.chunks)`` in :meth:`search`,
    counted AFTER the one ``acl_filters`` trimming seam (C7: it can never
    reveal the existence of a trimmed chunk). This is an ADDITIVE subclass
    defined here in ``ne.brain`` (not ``ne.contracts``): the declared
    ``KnowledgeRead.search`` return type stays plain ``RetrievalResult`` post
    Checkpoint-A contract freeze, so no ADR or contract version bump is
    triggered — conformance is proven by ``isinstance(result,
    RetrievalResult)`` still holding for callers that only know the base
    Protocol shape.
    """

    candidate_count: int


class RetrievalPipeline:
    """Rerank-and-cite pipeline over a ``KnowledgeRead``. Implements ``KnowledgeRead``."""

    def __init__(
        self,
        retriever: KnowledgeRead,
        reranker: Reranker,
        *,
        candidate_multiplier: int = 4,
        max_candidate_k: int | None = None,
    ) -> None:
        if candidate_multiplier < 1:
            raise ValueError(f"candidate_multiplier must be >= 1, got {candidate_multiplier}")
        if max_candidate_k is not None and max_candidate_k < 1:
            raise ValueError(f"max_candidate_k must be >= 1, got {max_candidate_k}")
        self._retriever = retriever
        self._reranker = reranker
        self._candidate_multiplier = candidate_multiplier
        self._max_candidate_k = max_candidate_k

    def _candidate_k(self, top_k: int) -> int:
        """The rerank pool for one search — over-fetched, but never unbounded.

        ``max(top_k, ...)`` is load-bearing: the ceiling bounds rerank COST, it
        must never shrink the page the caller asked for.
        """
        candidate_k = top_k * self._candidate_multiplier
        if self._max_candidate_k is not None:
            candidate_k = min(candidate_k, self._max_candidate_k)
        return max(top_k, candidate_k)

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        candidate_k = self._candidate_k(top_k)
        started = time.monotonic()
        candidates = await self._retriever.search(
            query, principal=principal, top_k=candidate_k, filters=filters
        )
        retriever_ms = int((time.monotonic() - started) * 1000)
        candidate_count = len(candidates.chunks)
        if not candidates.chunks:
            # The early return still logs: a search that spent 200 s in the
            # retriever and never reached the reranker is exactly the shape this
            # instrument exists to distinguish, and dropping the line here would
            # make that case look like no search happened at all. rerank_ms=0 is
            # literal — the stage did not run.
            _log_search_timing(
                top_k=top_k,
                candidate_k=candidate_k,
                candidate_count=0,
                retriever_ms=retriever_ms,
                rerank_ms=0,
            )
            return RetrievalResult(chunks=(), citations=())
        started = time.monotonic()
        reranked = await self._reranker.rerank(
            query, [sc.chunk for sc in candidates.chunks], top_k=top_k
        )
        rerank_ms = int((time.monotonic() - started) * 1000)
        _log_search_timing(
            top_k=top_k,
            candidate_k=candidate_k,
            candidate_count=candidate_count,
            retriever_ms=retriever_ms,
            rerank_ms=rerank_ms,
        )
        return RerankedRetrievalResult(
            chunks=tuple(reranked),
            citations=tuple(citation_for(sc.chunk) for sc in reranked),
            candidate_count=len(candidates.chunks),
        )

    async def retrieve(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        trace_id: str | None = None,
    ) -> RetrievalResult:
        """Delegate fetch-by-id verbatim — not ranked, so no rerank stage.

        ACL trimming rides through the delegated retriever's seam, identical
        to :meth:`search`. ``trace_id`` is forwarded to a ``HybridRetriever``
        delegate (NE-164) via ``isinstance`` narrowing; any other
        ``KnowledgeRead`` delegate drops it (documented caller gap).
        """
        if isinstance(self._retriever, HybridRetriever):
            return await self._retriever.retrieve(chunk_ids, principal=principal, trace_id=trace_id)
        return await self._retriever.retrieve(chunk_ids, principal=principal)


class CurrentAuthorizedRead(Protocol):
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
    ) -> AuthorizedRetrievalEnvelope: ...

    async def retrieve_current(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> AuthorizedRetrievalEnvelope: ...


class CurrentAuthorizedRetrievalPipeline:
    """Rerank without discarding bounded current-authorization truth."""

    def __init__(self, retriever: CurrentAuthorizedRead, reranker: Reranker) -> None:
        self._retriever = retriever
        self._reranker = reranker

    async def search(
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
        envelope = await self._retriever.search_current(
            query,
            principal=principal,
            access_context_hash=access_context_hash,
            request_hash=request_hash,
            top_k=top_k,
            scan_limit=scan_limit,
            filters=filters,
            continuation_token=continuation_token,
        )
        if not envelope.result.chunks:
            return envelope
        reranked = tuple(
            await self._reranker.rerank(
                query,
                [item.chunk for item in envelope.result.chunks],
                top_k=top_k,
            )
        )
        if len(reranked) != envelope.returned_count:
            raise ValueError("reranker changed the authorized page cardinality")
        result = RetrievalResult(
            chunks=reranked,
            citations=tuple(citation_for(item.chunk) for item in reranked),
        )
        return AuthorizedRetrievalEnvelope(
            result=result,
            status=envelope.status,
            scanned_count=envelope.scanned_count,
            returned_count=envelope.returned_count,
            scan_limit=envelope.scan_limit,
            continuation_token=envelope.continuation_token,
        )

    async def retrieve(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> AuthorizedRetrievalEnvelope:
        return await self._retriever.retrieve_current(
            chunk_ids,
            principal=principal,
            access_context_hash=access_context_hash,
        )
