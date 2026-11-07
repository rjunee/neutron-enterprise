# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``HybridRetriever`` — the retrieval side of ``KnowledgeRead`` over a ChunkStore.

Combines an :class:`~ne.contracts.knowledge.Embedder`, a sparse query encoder,
and a :class:`~ne.contracts.knowledge.ChunkStore` into
:class:`ne.contracts.knowledge.KnowledgeRead`'s ``search``/``retrieve`` surface,
with citations.

NE-043 seam (CLOSED contract): ``acl_filters`` is the C7 security-trimming hook.
In production it is wired to :class:`ne.brain.trimming.PrincipalAclFilter`, which
is fail-closed and NEVER returns ``None`` or ``{}`` — so the two escape branches
below are unreachable from production and remain only as defense in depth (and
the C7 guardian test enforces the seam). Its three return states have exact,
fail-closed meanings:

* ``acl_filters is None`` (hook absent) OR the hook returns ``None`` — trimming
  DISABLED. Neither ``search`` nor ``retrieve`` is principal-trimmed; every
  principal sees every chunk. This is an explicitly UN-TRIMMED composition for
  fixtures/eval only; production always injects ``PrincipalAclFilter``.
* the hook returns a NON-empty mapping — server-side filter, AND-combined with
  any caller filter (see :func:`and_combine_filters`).
* the hook returns a non-None EMPTY mapping ``{}`` — MATCH-NOTHING. A ``{}``
  means "this principal has ZERO accessible labels", which MUST yield an empty
  result on BOTH ``search()`` and ``retrieve()`` — no store call, no embedding,
  no fetch. It is NOT "trimming disabled": collapsing ``{}`` into the unscoped
  path would hand a groupless principal the FULL corpus (the closed
  fail-OPEN this contract exists to prevent).

Both ``search()`` and ``retrieve()`` route the principal through the SAME
``acl_filters`` hook. A store that cannot enforce a non-empty filter on the
fetch-by-id path fails CLOSED (``NotImplementedError``) rather than silently
falling back to an unscoped fetch. (The match-nothing ``{}`` short-circuit needs
no store capability — nothing is fetched, so nothing can leak — and therefore
runs BEFORE that capability check.)

Filter composition: when both a caller filter and an ACL filter are present,
``search`` AND-combines them via :func:`and_combine_filters` (intersection on
key collision, never overwrite) per the SPEC NE-043 filter-merge note, so
NE-043 inherits correct composition.

NE-164 read-path auditing: ``retrieve()`` optionally emits ONE config_hash-
stamped ``knowledge.retrieve`` event per call via an injected
:class:`~ne.contracts.governance.AuditSink` (mirrors the write-path pattern in
``ne.brain.write.writer``). An actual source-health trim emits one additional
counts-only ``knowledge.withhold`` event. See :meth:`HybridRetriever._emit_read_audit`
for the exact C7 discipline (counts + trim delta only — never ACL values, chunk
ids, chunk text, or query content).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from ne.brain.current_source_authority import SqlAlchemyAuthorizedContinuationStore
from ne.brain.current_source_filter import (
    CurrentAuthorizedArtifactSelectorImpl,
    CurrentSourceDisclosureEvaluator,
    ScoredChunkCandidateReader,
    authority_stamp_from_chunk,
)
from ne.brain.source_health import (
    DegradedSourcesProvider,
    chunk_connector_stamp,
    chunk_source_is_healthy,
    evaluate_source_health,
)
from ne.brain.trimming import ACL_ANY_KEY
from ne.brain.withholding import (
    record_source_health_stale_served,
    record_source_health_withholding,
)
from ne.contracts.governance import AuditSink
from ne.contracts.knowledge import ChunkStore, Embedder
from ne.contracts.source_authority import (
    AuthorizedArtifactPageStatus,
    AuthorizedRetrievalEnvelope,
    SourceAuthorityDecision,
)
from ne.contracts.types import (
    Chunk,
    Citation,
    Principal,
    RetrievalResult,
    ScoredChunk,
    SparseVector,
)
from ne.events import new_audit_event
from ne.schema import AuditOutcome


class QuerySparseEncoder(Protocol):
    """Structural protocol for a sparse encoder usable at query time.

    Deliberately NOT imported from ``ne.contracts`` — ne-contracts is FROZEN
    and its ``SparseEncoder`` Protocol does not (yet) declare
    ``encode_queries``. Any encoder satisfying this shape (e.g.
    :class:`ne.brain.sparse.FastEmbedBM25Encoder`) works here.
    """

    @property
    def model_id(self) -> str: ...

    async def encode(self, texts: Sequence[str]) -> Sequence[SparseVector]: ...

    async def encode_queries(self, texts: Sequence[str]) -> Sequence[SparseVector]: ...


@runtime_checkable
class FilteredChunkFetch(Protocol):
    """Structural protocol for a ChunkStore supporting payload-filtered fetch-by-id.

    Deliberately NOT part of the FROZEN ne-contracts ``ChunkStore`` Protocol
    (same pattern as ``QuerySparseEncoder`` above); ``QdrantChunkStore.get_filtered``
    satisfies it.
    """

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]: ...


def citation_for(chunk: Chunk) -> Citation:
    """Build a source-system Citation (C7 provenance) for ``chunk``.

    Shared by ``HybridRetriever`` and ``RetrievalPipeline`` so every retrieved
    chunk maps 1:1 to a citation pointing back to its source of record:
    ``source_system``/``source_ref`` are pulled from the chunk's metadata
    (``source_ref`` falls back to ``document_id``).
    """
    return Citation(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        source_system=str(chunk.metadata.get("source_system", "")),
        source_ref=str(chunk.metadata.get("source_ref", chunk.document_id)),
    )


def and_combine_filters(
    caller: Mapping[str, Any] | None, acl: Mapping[str, Any] | None
) -> dict[str, Any]:
    """AND-combine a caller filter with an ACL filter (SPEC NE-043 row).

    The NE-043 filter-merge note requires composing caller filters + ACL
    filters by **intersection** on key collision, NEVER overwrite — so that
    NE-043 inherits correct composition when it makes the ``acl_filters`` hook
    real. This resolves the deferred bug where ``merged |= acl`` let an ACL
    filter be silently replaced by (or replace) a colliding caller filter.

    Grammar mirrors :func:`ne.brain.qdrant._build_filter`: a ``str``/``int``/
    ``bool`` value is equality, a ``list``/``tuple`` is any-of. On a KEY
    COLLISION the result is the intersection in that grammar:

    * scalar ∧ equal-scalar   -> the scalar
    * scalar ∧ unequal-scalar -> ``[]`` (match-nothing)
    * scalar ∧ list           -> the scalar if it is in the list, else ``[]``
    * list ∧ list             -> caller-ordered intersection (``[]`` if disjoint)

    An empty list ``[]`` deliberately encodes match-nothing: ``_build_filter``
    turns it into ``MatchAny(any=[])`` which matches ZERO points on both the
    query and scroll paths. Any other value type raises ``ValueError`` (same as
    the store's filter grammar).

    NE-087 reserved-namespace hardening (the Checkpoint-D anti-smuggling lesson,
    applied to the new ``$any`` grammar). ``$``-prefixed keys are RESERVED to the
    ACL trimming hook:

    * ANY ``$``-prefixed key in the CALLER position raises ``ValueError`` — a
      caller-supplied OR-group could otherwise widen the AND-composition into
      match-MORE (the "unreserved token smuggles tenant-all" bug class).
    * in the ACL position, :data:`ne.brain.trimming.ACL_ANY_KEY` (``"$any"``) is
      passed through VERBATIM after shape validation (a non-empty list/tuple of
      non-empty mappings, none of whose keys is ``$``-prefixed). Because the
      caller position just rejected every ``$`` key, an ``$any`` collision is
      impossible, so it never intersects — it is a pure OR-group ANDed with the
      caller's plain (narrowing-only) keys.
    * any OTHER ``$``-prefixed ACL key raises ``ValueError`` (an unknown reserved
      token fails closed).
    """
    caller_map = caller or {}
    for key in caller_map:
        if isinstance(key, str) and key.startswith("$"):
            raise ValueError(
                f"reserved filter key {key!r}: '$'-prefixed keys are reserved for the "
                "ACL trimming hook and are not permitted in a caller filter"
            )
    merged: dict[str, Any] = dict(caller_map)
    for key, acl_value in (acl or {}).items():
        if key == ACL_ANY_KEY:
            _check_any_group_value(key, acl_value)
            # No collision possible (caller '$' keys were rejected above), so the
            # OR-group is placed verbatim — never intersected, never widening.
            merged[key] = acl_value
            continue
        if isinstance(key, str) and key.startswith("$"):
            raise ValueError(
                f"unknown reserved filter key {key!r}: only {ACL_ANY_KEY!r} is a "
                "recognised '$'-prefixed ACL grammar token"
            )
        _check_filter_value(key, acl_value)
        if key not in merged:
            merged[key] = acl_value
            continue
        merged[key] = _intersect_filter_values(key, merged[key], acl_value)
    return merged


def _check_filter_value(key: str, value: Any) -> None:
    if not isinstance(value, list | tuple | str | int | bool):
        raise ValueError(f"unsupported filter value for {key!r}: {value!r}")


def _check_any_group_value(key: str, value: Any) -> None:
    """Validate the shape of an :data:`ACL_ANY_KEY` OR-group value, fail-closed.

    Must be a non-empty ``list``/``tuple`` whose every element is a non-empty
    ``Mapping`` containing NO ``$``-prefixed keys (no nested ``$any`` — a single
    flat OR-group only). Anything else raises ``ValueError``.
    """
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{key!r} must be a non-empty list of sub-filters, got {value!r}")
    for sub in value:
        if not isinstance(sub, Mapping) or not sub:
            raise ValueError(f"{key!r} sub-filter must be a non-empty mapping, got {sub!r}")
        for sub_key in sub:
            if isinstance(sub_key, str) and sub_key.startswith("$"):
                raise ValueError(
                    f"{key!r} sub-filter may not contain a nested reserved key {sub_key!r}"
                )


def _as_list(value: Any) -> list[Any] | None:
    return list(value) if isinstance(value, list | tuple) else None


def _intersect_filter_values(key: str, caller_value: Any, acl_value: Any) -> Any:
    _check_filter_value(key, caller_value)
    caller_list = _as_list(caller_value)
    acl_list = _as_list(acl_value)
    if caller_list is not None and acl_list is not None:
        return [v for v in caller_list if v in acl_list]
    if caller_list is not None:  # caller list ∧ acl scalar
        return acl_value if acl_value in caller_list else []
    if acl_list is not None:  # caller scalar ∧ acl list
        return caller_value if caller_value in acl_list else []
    return caller_value if caller_value == acl_value else []


class HybridRetriever:
    """Dense+sparse hybrid retriever implementing ``KnowledgeRead``."""

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        sparse_encoder: QuerySparseEncoder,
        acl_filters: (
            Callable[
                [Principal],
                Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
            ]
            | None
        ) = None,
        *,
        audit_sink: AuditSink | None,
        source_health: DegradedSourcesProvider | None = None,
    ) -> None:
        """Construct the ONE ACL-trimmed retriever.

        NE-VG-2 (V6 shape ratchet, design §3-V6, ExtractionJob precedent at
        ``ne.brain_graph.extraction.job.ExtractionRuntime``): ``audit_sink`` is
        now REQUIRED keyword-only — still typed ``AuditSink | None`` because a
        genuinely sink-less composition remains legitimate (the byte-identical
        "emits nothing" contract pinned by
        ``test_retrieval_read_audit.py::test_no_audit_sink_composition_emits_nothing_and_behaves_identically``)
        — but the OMISSION of the keyword is now a pyright ``reportCallIssue`` /
        ``TypeError`` at construction, never a silent default. This is a
        RATCHET-HARDENING of an already-deletion-guarded binding (the platform
        composition root's ``audit_sink=sink`` at ``platform.py:1003`` is
        covered by ``test_brain_api.py::test_retrieve_route_emits_read_audit_event``
        end-to-end) — not a live-defect closure. Every legitimate sink-less
        caller now passes ``audit_sink=None`` EXPLICITLY.
        """
        self._store = store
        self._embedder = embedder
        self._sparse_encoder = sparse_encoder
        self._acl_filters = acl_filters
        self._audit_sink = audit_sink
        self._source_health = source_health

    async def _eval_acl(self, principal: Principal) -> Mapping[str, Any] | None:
        """Evaluate the ``acl_filters`` hook, awaiting an awaitable result.

        NE-087 makes the hook async-CAPABLE (additive): a sync hook (every
        existing composition, e.g. ``PrincipalAclFilter`` without a resolver)
        returns a plain mapping and is used as-is; a hook returning an awaitable
        (``PrincipalAclFilter`` with a ReBAC resolver) is awaited here. A resolver
        exception propagates (deny-by-erroring). Both ``search()`` and
        ``retrieve()`` route through this ONE evaluator so the two principal-
        scoped read paths can never diverge.
        """
        if self._acl_filters is None:
            return None
        acl_raw = self._acl_filters(principal)
        return await acl_raw if inspect.isawaitable(acl_raw) else acl_raw

    async def search(
        self,
        query: str,
        *,
        principal: Principal,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> RetrievalResult:
        # Evaluate the ACL hook FIRST (before any embed/encode/store work). A
        # non-None empty mapping means the principal has ZERO accessible labels
        # -> match nothing, with NO store call and NO embedding. (The production
        # PrincipalAclFilter never returns {}; this branch is defense in depth.)
        # Ordering note: this moves the acl_filters call ahead of embedding; no
        # current caller relies on an acl-hook exception ordering.
        acl = await self._eval_acl(principal)
        if acl is not None and not acl:
            return RetrievalResult(chunks=(), citations=())

        dense = (await self._embedder.embed([query]))[0]
        sparse = (await self._sparse_encoder.encode_queries([query]))[0]

        merged: dict[str, Any] = dict(filters or {})
        if acl is not None:
            # AND-combine (intersect on key collision), never overwrite — the
            # NE-043 filter-merge fix so caller ∩ acl composes correctly.
            merged = and_combine_filters(merged, acl)

        scored: Sequence[ScoredChunk] = await self._store.query(
            dense=dense, sparse=sparse, top_k=top_k, filters=merged or None
        )
        scored, health_trimmed = await self._filter_source_health(scored)
        await self._emit_source_health_audit(
            principal=principal,
            op="search",
            returned=len(scored),
            health_trimmed=health_trimmed,
            trace_id=None,
        )
        return RetrievalResult(
            chunks=tuple(scored), citations=tuple(citation_for(sc.chunk) for sc in scored)
        )

    async def retrieve(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        trace_id: str | None = None,
    ) -> RetrievalResult:
        """Fetch-by-id, ACL-consistent with :meth:`search` (CLOSED contract).

        The ``acl_filters`` hook's three states (see the module docstring):

        * ``None`` (hook absent, or hook returns ``None``) — trimming disabled;
          the unscoped :meth:`ChunkStore.get` fetch is used, no trimming occurs.
        * non-None EMPTY mapping ``{}`` — MATCH-NOTHING: return an empty result
          with NO store call. A groupless principal is fetched nothing; this
          needs no ``get_filtered`` capability and runs BEFORE the capability
          check, so it can never leak via a store lacking the filtered fetch.
        * non-empty mapping — server-side filter via ``get_filtered`` so a chunk
          the principal cannot see is never returned (symmetric with
          :meth:`search`). A store that cannot filter fetch-by-id fails CLOSED
          (``NotImplementedError``) rather than leaking.

        NE-164: when an ``audit_sink`` is injected, every COMPLETED call below
        emits exactly one read-path ``AuditEvent`` (see
        :meth:`_emit_read_audit`) BEFORE returning. The fail-closed
        ``NotImplementedError`` branch emits nothing — the exception propagates
        before any audit decision is made, matching the security refusal
        happening first.
        """
        requested = len(chunk_ids)
        acl = await self._eval_acl(principal)
        if acl is not None and not acl:
            # Match-nothing: no store capability needed, nothing fetched. The
            # ENTIRE candidate set counts as ACL-trimmed.
            await self._emit_read_audit(
                principal=principal,
                requested=requested,
                returned=0,
                trimmed=requested,
                trace_id=trace_id,
            )
            return RetrievalResult(chunks=(), citations=())
        if acl is not None:
            if not isinstance(self._store, FilteredChunkFetch):
                raise NotImplementedError(
                    "acl_filters is set but this ChunkStore has no get_filtered(); "
                    "refusing to fall back to the unscoped get() for a "
                    "principal-scoped retrieve()"
                )
            chunks = await self._store.get_filtered(chunk_ids, filters=acl)
            trimmed = requested - len(chunks)
        else:
            chunks = await self._store.get(chunk_ids)
            # Trimming is disabled entirely — a missing id here is NOT an ACL
            # trim (it may simply not exist); the delta is always 0.
            trimmed = 0
        scored, health_trimmed = await self._filter_source_health(
            tuple(ScoredChunk(chunk=c, score=1.0) for c in chunks)
        )
        await self._emit_source_health_audit(
            principal=principal,
            op="retrieve",
            returned=len(scored),
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        await self._emit_read_audit(
            principal=principal,
            requested=requested,
            returned=len(scored),
            trimmed=trimmed,
            health_trimmed=health_trimmed,
            trace_id=trace_id,
        )
        return RetrievalResult(
            chunks=scored,
            citations=tuple(citation_for(scored_chunk.chunk) for scored_chunk in scored),
        )

    async def _filter_source_health(
        self, scored: Sequence[ScoredChunk]
    ) -> tuple[tuple[ScoredChunk, ...], int]:
        """Apply one fresh health snapshot before results reach a reranker/caller.

        The trim delta is recorded into the ambient
        ``ne.brain.withholding`` observation as well as returned, so the caller
        RENDERING this read can tell "nothing matched" from "every match was
        withheld" — two outcomes that are both ``chunks == ()`` and were, until
        that seam existed, indistinguishable to everyone downstream including the
        audit reader. The recording is counts-only and is a byte-identical no-op
        when no caller opened an observation scope.
        """
        if self._source_health is None or not scored:
            return tuple(scored), 0
        health = await evaluate_source_health(self._source_health)
        kept = tuple(
            candidate
            for candidate in scored
            if chunk_source_is_healthy(candidate.chunk.metadata, health.unavailable)
        )
        withheld = len(scored) - len(kept)
        record_source_health_withholding(candidates=len(scored), withheld=withheld)
        if health.stale:
            # The SERVED leg of the split: these chunks' source failed its
            # latest sync attempt but remains readable (a short plain-error run
            # since its last success). Counts only — the renderer states the
            # freshness caveat; nothing about which chunks are served changes.
            stale_served = sum(
                1
                for candidate in kept
                if chunk_connector_stamp(candidate.chunk.metadata) in health.stale
            )
            record_source_health_stale_served(stale_served)
        return kept, withheld

    async def _emit_source_health_audit(
        self,
        *,
        principal: Principal,
        op: str,
        returned: int,
        health_trimmed: int,
        trace_id: str | None,
    ) -> None:
        """Audit only actual withholding, using counts and rule identity alone."""
        if health_trimmed == 0 or self._audit_sink is None:
            return
        await self._audit_sink.append(
            new_audit_event(
                action="knowledge.withhold",
                resource="knowledge://chunks",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail=(f"op={op} returned={returned} source_health_trim_delta={health_trimmed}"),
                trace_id=trace_id,
            )
        )

    async def _emit_read_audit(
        self,
        *,
        principal: Principal,
        requested: int,
        returned: int,
        trimmed: int,
        health_trimmed: int = 0,
        trace_id: str | None,
    ) -> None:
        """Emit ONE ``knowledge.retrieve`` event for a completed ``retrieve()`` call.

        The event carries query shape only: candidate/returned counts and ACL/
        health trim deltas. It never carries ACL values, chunk ids, chunk text,
        or raw query content. Sink failure propagates so the read fails closed.
        """
        if self._audit_sink is None:
            return
        health_detail = f" source_health_trim_delta={health_trimmed}" if health_trimmed > 0 else ""
        await self._audit_sink.append(
            new_audit_event(
                action="knowledge.retrieve",
                resource="knowledge://chunks",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail=(
                    f"candidates={requested} returned={returned} "
                    f"acl_trim_delta={trimmed}{health_detail}"
                ),
                trace_id=trace_id,
            )
        )


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CurrentAuthorizedRetriever:
    """Progressively select only exact-current, healthy, ACL-authorized artifacts.

    The ordinary ``HybridRetriever`` remains the legacy historical-ACL surface.
    This separate envelope-returning surface is deliberately impossible to
    mistake for a complete ``RetrievalResult`` when its bounded scan stopped
    before either satisfying ``top_k`` or proving backend exhaustion.
    """

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        sparse_encoder: QuerySparseEncoder,
        evaluator: CurrentSourceDisclosureEvaluator,
        continuations: SqlAlchemyAuthorizedContinuationStore,
        *,
        config_hash: str,
        contract_head_hash: str,
        candidate_bound: int = 1000,
    ) -> None:
        if not _SHA256.fullmatch(config_hash) or not _SHA256.fullmatch(contract_head_hash):
            raise ValueError("retriever config/head hashes must be sha256 digests")
        if not 1 <= candidate_bound <= 10_000:
            raise ValueError("candidate_bound must be between 1 and 10000")
        self._store = store
        self._embedder = embedder
        self._sparse_encoder = sparse_encoder
        self._evaluator = evaluator
        self._continuations = continuations
        self._config_hash = config_hash
        self._contract_head_hash = contract_head_hash
        self._candidate_bound = candidate_bound
        self._selectors: dict[
            tuple[str, str, str, int, int], CurrentAuthorizedArtifactSelectorImpl
        ] = {}

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
        """Return a truthful bounded envelope; never collapse incomplete to empty."""

        if scan_limit > self._candidate_bound:
            raise ValueError("scan_limit exceeds the configured candidate bound")

        dense = (await self._embedder.embed([query]))[0]
        sparse = (await self._sparse_encoder.encode_queries([query]))[0]
        scored = tuple(
            await self._store.query(
                dense=dense,
                sparse=sparse,
                top_k=self._candidate_bound,
                filters=filters,
            )
        )
        reader = ScoredChunkCandidateReader(
            scored,
            end_reached=len(scored) < self._candidate_bound,
        )
        key = (principal.id, access_context_hash, request_hash, top_k, scan_limit)
        selector = self._selectors.get(key)
        if selector is None:
            selector = CurrentAuthorizedArtifactSelectorImpl(
                reader,
                self._evaluator,
                self._continuations,
                principal=principal,
                config_hash=self._config_hash,
                contract_head_hash=self._contract_head_hash,
            )
            self._selectors[key] = selector
        page = await selector.select(
            principal_id=principal.id,
            access_context_hash=access_context_hash,
            request_hash=request_hash,
            top_k=top_k,
            scan_limit=scan_limit,
            continuation_token=continuation_token,
        )
        material = reader.materialize(page.items)
        result = RetrievalResult(
            chunks=material,
            citations=tuple(citation_for(item.chunk) for item in material),
        )
        return AuthorizedRetrievalEnvelope(
            result=result,
            status=page.status,
            scanned_count=page.scanned_count,
            returned_count=page.returned_count,
            scan_limit=page.scan_limit,
            continuation_token=page.continuation_token,
        )

    async def retrieve_current(
        self,
        chunk_ids: Sequence[str],
        *,
        principal: Principal,
        access_context_hash: str,
    ) -> AuthorizedRetrievalEnvelope:
        """Recheck current authority and ACL for every direct retrieval id."""

        chunks = tuple(await self._store.get(chunk_ids))
        disclosed: list[ScoredChunk] = []
        for chunk in chunks:
            stamp = authority_stamp_from_chunk(chunk)
            if stamp is None:
                continue
            decision = await self._evaluator.evaluate(
                stamp,
                principal=principal,
                access_context_hash=access_context_hash,
            )
            if decision.decision is SourceAuthorityDecision.AUTHORIZED:
                disclosed.append(ScoredChunk(chunk=chunk, score=1.0))
        result = RetrievalResult(
            chunks=tuple(disclosed),
            citations=tuple(citation_for(item.chunk) for item in disclosed),
        )
        return AuthorizedRetrievalEnvelope(
            result=result,
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=len(chunks),
            returned_count=len(disclosed),
            scan_limit=max(1, len(chunk_ids)),
        )
