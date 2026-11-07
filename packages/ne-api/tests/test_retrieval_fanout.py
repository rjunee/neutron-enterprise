# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-RERANK-FANOUT — the two over-fetches compose ONCE, not multiplicatively.

``CurrentSourceAuthorizedKnowledgeRead`` over-fetches so per-chunk current-source
authorization can drop rows and still fill the page; ``RetrievalPipeline``
over-fetches so the reranker has a pool larger than its output. Both are real.
Stacked with the old constants they multiplied: a ``top_k=5`` Ask reranked
``5 * 8 * 4 = 160`` documents (measured on the demo box at ``rerank_ms=227271``,
98.9% of a 229 s search) while ``ne.brain.reranker``'s docstring said 20 and
the documented pool was 40.

These tests exercise the REAL composed pair — the production wrapper over the
production pipeline over a recording fake store — and pin the three things the
fix must hold: the candidate count, the page the caller receives, and the fact
that the reranker still selects from a pool bigger than what it emits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ne.api.platform import RERANK_CANDIDATE_CEILING
from ne.api.tools import CurrentSourceAuthorizedKnowledgeRead
from ne.brain.current_source_filter import SOURCE_AUTHORITY_STAMP_METADATA_KEY
from ne.brain.pipeline import RetrievalPipeline
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    NO_SOURCE_STATUS,
    SOURCE_STATUS_METADATA_KEY,
    GovernedLocalT3SourcePoint,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import Chunk, Principal, RetrievalResult, ScoredChunk
from ne.runtime.source_results import authenticated_request_scope

#: The deployed Ask shape (``ApiSettings.retrieval_top_k`` default) and the two
#: multipliers ``ne.api.platform`` composes around it.
_ASK_TOP_K = 5
_SCAN_MULTIPLIER = 2
_CANDIDATE_MULTIPLIER = 4

_PRINCIPAL = Principal(id="principal-1", groups=("analyst",))
_CONTEXT = AuthenticatedRequestContext(
    principal_id="principal-1",
    authorization_groups=("analyst",),
    issuer="https://issuer.test",
    expires_at=4102444800,
)


def _free_chunk(index: int) -> ScoredChunk:
    """A server-authored source-free chunk — authorization keeps it, no evaluator."""
    return ScoredChunk(
        chunk=Chunk(
            chunk_id=f"c{index:03d}",
            document_id=f"doc-{index:03d}",
            text=f"body {index}",
            metadata={SOURCE_STATUS_METADATA_KEY: NO_SOURCE_STATUS},
        ),
        score=float(1000 - index),
    )


def _stamp(revision: str) -> SourceAuthorityStamp:
    contract = SourceContractIdentity(
        source_kind="governed_local_t3",
        descriptor_schema_version=1,
        descriptor_hash="1" * 64,
    )
    source = SourceIdentity(
        deployment_id="deployment-1",
        source_kind="governed_local_t3",
        connector_instance_id="ontology-1",
        point=GovernedLocalT3SourcePoint(dataset_name="meridian", contributor_key="contributor-1"),
        generation=1,
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="2" * 64,
        content_snapshot_hash="3" * 64,
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=revision,
        acl_snapshot_hash="4" * 64,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id="receipt-1",
        authority_receipt_hash="5" * 64,
    )


def _stamped_chunk(index: int) -> ScoredChunk:
    """A source-bearing chunk whose stamp is UNIQUE — one evaluator decision each.

    ``_authorized`` memoizes decisions by stamp, so a shared stamp would make an
    alternating-denial evaluator decide once for the whole page.
    """
    return ScoredChunk(
        chunk=Chunk(
            chunk_id=f"c{index:03d}",
            document_id=f"doc-{index:03d}",
            text=f"body {index}",
            metadata={
                SOURCE_AUTHORITY_STAMP_METADATA_KEY: _stamp(f"revision-{index}").model_dump(
                    mode="json"
                )
            },
        ),
        score=float(1000 - index),
    )


class _RecordingStore:
    """A ranked fake store: returns the head of a fixed corpus, records every ask."""

    def __init__(self, corpus: Sequence[ScoredChunk]) -> None:
        self._corpus = tuple(corpus)
        self.search_top_k: list[int] = []

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        self.search_top_k.append(top_k)
        return RetrievalResult(chunks=self._corpus[:top_k], citations=())

    async def retrieve(self, chunk_ids: Sequence[str], *, principal: Principal) -> RetrievalResult:
        raise NotImplementedError


class _RecordingReranker:
    """Order-preserving ``Reranker`` fake that records each pool it was handed.

    Scores are a function of the CHUNK, never of the pool size — a cross-encoder
    scores each query/chunk pair independently, so a differential across two pool
    sizes has to be able to compare the scores it returns.
    """

    def __init__(self) -> None:
        self.pool_sizes: list[int] = []
        self.out_sizes: list[int] = []

    @property
    def model_id(self) -> str:
        return "recording-reranker"

    async def rerank(
        self, query: str, chunks: Sequence[Chunk], *, top_k: int = 10
    ) -> Sequence[ScoredChunk]:
        self.pool_sizes.append(len(chunks))
        out = tuple(
            ScoredChunk(chunk=c, score=float(1000 - int(c.chunk_id[1:]))) for c in chunks[:top_k]
        )
        self.out_sizes.append(len(out))
        return out


class _AllowAll:
    async def evaluate(self, *_: Any, **__: Any) -> Any:
        class _Ok:
            authorized = True

        return _Ok()


class _DenyEveryOther:
    """Authorizes even-numbered revisions only — a 50% denial rate per page."""

    async def evaluate(self, stamp: Any, **__: Any) -> Any:
        index = int(str(stamp.selected_head_revision).rsplit("-", 1)[1])

        class _Decision:
            authorized = index % 2 == 0

        return _Decision()


def _compose(
    store: _RecordingStore,
    reranker: _RecordingReranker,
    *,
    evaluator: Any = None,
    scan_multiplier: int = _SCAN_MULTIPLIER,
    max_candidate_k: int | None = RERANK_CANDIDATE_CEILING,
) -> CurrentSourceAuthorizedKnowledgeRead:
    """The production composition: authorization wrapper OVER the rerank pipeline."""
    pipeline = RetrievalPipeline(
        store,  # type: ignore[arg-type]
        reranker,  # type: ignore[arg-type]
        candidate_multiplier=_CANDIDATE_MULTIPLIER,
        max_candidate_k=max_candidate_k,
    )
    return CurrentSourceAuthorizedKnowledgeRead(
        pipeline,
        _AllowAll() if evaluator is None else evaluator,  # type: ignore[arg-type]
        scan_multiplier=scan_multiplier,
    )


def _search(retriever: CurrentSourceAuthorizedKnowledgeRead, *, top_k: int) -> RetrievalResult:
    with authenticated_request_scope(_CONTEXT):
        return asyncio.run(retriever.search("q", principal=_PRINCIPAL, top_k=top_k))


# --- (a) the candidate count -------------------------------------------------


def test_the_deployed_ask_reranks_forty_candidates_not_one_hundred_sixty() -> None:
    """The whole point: ``top_k=5`` must not fan out to ``5 * 8 * 4``.

    40 is the documented pool. At the TEI batch size of 32 it is
    2 HTTP calls instead of 5, and rerank cost is LINEAR in this number for every
    backend, so this single assertion is the measured fix.
    """
    store = _RecordingStore([_free_chunk(i) for i in range(500)])
    reranker = _RecordingReranker()

    _search(_compose(store, reranker), top_k=_ASK_TOP_K)

    assert reranker.pool_sizes == [40]
    assert store.search_top_k == [40]
    # The defect, named so a regression reads as itself rather than as a number.
    assert reranker.pool_sizes != [_ASK_TOP_K * 8 * _CANDIDATE_MULTIPLIER]


def test_the_ceiling_bounds_the_largest_top_k_the_search_route_accepts() -> None:
    """``BrainSearchRequest.top_k`` allows 50; uncapped that was 50*8*4 = 1600."""
    store = _RecordingStore([_free_chunk(i) for i in range(4000)])
    reranker = _RecordingReranker()

    _search(_compose(store, reranker), top_k=50)

    # window = 50*2 = 100; the ceiling cannot shrink the page, so the pool is the
    # window itself rather than 100*4 = 400 — bounded, and 16x below the old 1600.
    assert reranker.pool_sizes == [100]
    assert reranker.pool_sizes[0] < 50 * 8 * _CANDIDATE_MULTIPLIER


def test_a_mid_sized_top_k_is_held_at_the_ceiling_by_the_pipeline() -> None:
    """``top_k=10`` on /v1/brain/search: window 20, uncapped pool 80, ceiling 40."""
    store = _RecordingStore([_free_chunk(i) for i in range(500)])
    reranker = _RecordingReranker()

    _search(_compose(store, reranker), top_k=10)

    assert reranker.pool_sizes == [RERANK_CANDIDATE_CEILING]


# --- (b) the authorized page is unchanged ------------------------------------


def test_the_authorized_page_size_and_order_survive_the_smaller_pool() -> None:
    """A differential against the OLD constants over the same ranked corpus.

    Old shape: 160 fetched -> reranked to 40 -> authorized to 5.
    New shape:  40 fetched -> reranked to 10 -> authorized to 5.
    With a ranked store and an order-preserving reranker the served page — its
    size, its members and their order — must be byte-identical.
    """
    corpus = [_free_chunk(i) for i in range(500)]

    old = _search(
        _compose(
            _RecordingStore(corpus), _RecordingReranker(), scan_multiplier=8, max_candidate_k=None
        ),
        top_k=_ASK_TOP_K,
    )
    new = _search(_compose(_RecordingStore(corpus), _RecordingReranker()), top_k=_ASK_TOP_K)

    assert len(new.chunks) == _ASK_TOP_K
    assert [sc.chunk.chunk_id for sc in new.chunks] == [sc.chunk.chunk_id for sc in old.chunks]
    assert [sc.chunk.chunk_id for sc in new.chunks] == ["c000", "c001", "c002", "c003", "c004"]
    assert [sc.score for sc in new.chunks] == [sc.score for sc in old.chunks]
    assert [c.chunk_id for c in new.citations] == [c.chunk_id for c in old.citations]


def test_the_page_never_exceeds_top_k_even_though_the_pool_is_wide() -> None:
    store = _RecordingStore([_free_chunk(i) for i in range(500)])

    result = _search(_compose(store, _RecordingReranker()), top_k=_ASK_TOP_K)

    assert len(result.chunks) == _ASK_TOP_K


def test_the_remaining_headroom_still_refills_a_half_denied_page() -> None:
    """The wrapper's over-fetch KEPT ITS PURPOSE, not just its name.

    ``scan_multiplier=2`` is the authorization headroom on its own now. This pins
    that it is real headroom: an evaluator denying every other chunk still yields
    a full ``top_k`` page. It is also the honest limit of the fix — a denial rate
    above 50% now returns a short page where an 8x window would have refilled it.
    """
    store = _RecordingStore([_stamped_chunk(i) for i in range(500)])

    result = _search(
        _compose(store, _RecordingReranker(), evaluator=_DenyEveryOther()), top_k=_ASK_TOP_K
    )

    assert len(result.chunks) == _ASK_TOP_K
    assert [sc.chunk.chunk_id for sc in result.chunks] == [
        "c000",
        "c002",
        "c004",
        "c006",
        "c008",
    ]


def test_this_path_issues_no_continuation_token_so_no_cursor_is_disturbed() -> None:
    """The score-keyed cursor lives in the OTHER lane, and this one never mints one.

    ``current_source_filter``'s continuation state binds ``requested_top_k`` and
    ``scan_limit`` and is reached only through ``search_current`` /
    ``AuthorizedRetrievalEnvelope``. This wrapper returns a bare
    ``RetrievalResult``, so changing its window cannot invalidate an outstanding
    token. Pinned structurally rather than argued in a comment.
    """
    store = _RecordingStore([_free_chunk(i) for i in range(50)])

    result = _search(_compose(store, _RecordingReranker()), top_k=_ASK_TOP_K)

    assert type(result) is RetrievalResult
    assert not hasattr(result, "continuation_token")


# --- (c) the reranker still selects ------------------------------------------


def test_the_reranker_pool_stays_strictly_larger_than_what_it_emits() -> None:
    """Neither over-fetch was neutered: 40 scored -> 10 emitted -> 5 served."""
    store = _RecordingStore([_free_chunk(i) for i in range(500)])
    reranker = _RecordingReranker()

    result = _search(_compose(store, reranker), top_k=_ASK_TOP_K)

    (pool,) = reranker.pool_sizes
    (emitted,) = reranker.out_sizes
    assert pool > emitted > len(result.chunks)
    assert (pool, emitted, len(result.chunks)) == (40, 10, 5)
