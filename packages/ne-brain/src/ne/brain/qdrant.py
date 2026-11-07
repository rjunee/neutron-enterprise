# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Qdrant ``ChunkStore`` adapter — dense + sparse named vectors, server-side fusion.

Implements :class:`ne.contracts.knowledge.ChunkStore`. Each chunk is stored as
one Qdrant point with TWO named vectors in the same collection:

* ``dense`` — produced by an :class:`~ne.contracts.knowledge.Embedder` (e.g.
  :class:`ne.brain.embedder.TEIEmbedder`), cosine distance.
* ``sparse`` — BM25 term weights produced client-side by a
  :class:`~ne.contracts.knowledge.SparseEncoder` (e.g.
  :class:`ne.brain.sparse.FastEmbedBM25Encoder`), with Qdrant's ``Modifier.IDF``
  applying the IDF term server-side.

Hybrid queries (``query(dense=..., sparse=...)``) issue **one** Qdrant
``query_points`` call: a ``dense`` prefetch, a ``sparse`` prefetch, and a
``FusionQuery(fusion=Fusion.RRF)`` outer query — fusion happens entirely inside
Qdrant, never client-side.

The chunk payload carries provenance (via ``Chunk.metadata``) plus the frozen
``Chunk.acl`` field, which is keyword-indexed so filters can restrict by group.
:meth:`QdrantChunkStore.update_payload` is the ``PayloadUpdatable`` capability
used by the NE-026 spine's stale-ACL fix: it overwrites only the acl/metadata of
already-indexed points (no re-embed).

C7 security trimming (NE-043) is REAL: :meth:`QdrantChunkStore.permission_filter`
builds a fail-closed Qdrant ``Filter`` matching a point IFF its ``acl`` payload
array contains the principal id OR any of its expanded group labels. The concrete
group expander (``ne.permissions.expand.PrincipalExpander``) is injected at the
COMPOSITION ROOT via the keyword-only ``group_expander`` seam (a package-local
:class:`ne.brain.trimming.GroupExpander` Protocol — NOT a ne-brain -> ne-permissions
import edge). Without an injected expander the filter falls back to the verified
token identity (id + ``Principal.groups``); it never returns ``None`` and never
matches all.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
import warnings
import weakref
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from qdrant_client import AsyncQdrantClient, models

from ne.brain.evidence import (
    VectorEvidenceDescriptor,
    VectorEvidenceWriteOutcome,
    VectorEvidenceWriteReceipt,
    VectorWriteSettlementHandle,
    submitted_embedding_receipt,
    validate_vector_evidence_receipts,
    validated_dense_embedding,
    vector_evidence_descriptor,
)
from ne.brain.trimming import ACL_ANY_KEY, GroupExpander, acl_match_labels
from ne.contracts.knowledge import Embedder, SparseEncoder
from ne.contracts.types import Chunk, Principal, ScoredChunk, SparseVector

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"

_UPSERT_BATCH_SIZE = 64
_ACL_FIELD = "acl"


class QdrantWriteCompensationError(RuntimeError):
    """A failed write could not be restored and verified to its pre-call state."""


@dataclass(frozen=True)
class _PointPreimage:
    point_id: models.ExtendedPointId
    payload: dict[str, Any] | None
    vector: Any


@dataclass(frozen=True)
class _QdrantSettlementState:
    chunk_to_point: dict[str, models.ExtendedPointId]
    preimages: dict[models.ExtendedPointId, _PointPreimage]
    postimages: dict[models.ExtendedPointId, _PointPreimage]


def point_id_for(chunk_id: str) -> str:
    """Deterministic Qdrant point id for a chunk id (UUIDv5, stable across runs)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ne-chunk:{chunk_id}"))


def _field_condition(key: str, value: Any) -> models.FieldCondition:
    """Compile ONE ``key -> value`` pair into a Qdrant ``FieldCondition``.

    * ``list`` / ``tuple`` -> "any of" (``MatchAny``); EVERY element must itself
      be a supported scalar, so a nested container inside a list fails closed
      instead of being handed to ``MatchAny`` un-vetted. This is the same
      element-level check ``ne.brain.pgvector.filters._field_condition`` applies
      (it compiles each element through ``_pair`` -> ``_scalar_or_raise``), with
      the same message — the two compilers of the one frozen grammar must not
      disagree about what they refuse.
    * ``str`` / ``int`` / ``bool`` -> equality (``MatchValue``).
    * anything else -> ``ValueError`` (unsupported filter value).
    """
    if isinstance(value, list | tuple):
        for item in value:
            if not isinstance(item, str | int | bool):
                raise ValueError(f"unsupported filter value for {key!r}: {item!r}")
        return models.FieldCondition(key=key, match=models.MatchAny(any=list(value)))
    if isinstance(value, str | int | bool):
        return models.FieldCondition(key=key, match=models.MatchValue(value=value))
    raise ValueError(f"unsupported filter value for {key!r}: {value!r}")


def _build_any_group(key: str, value: Any) -> models.Filter:
    """Compile the NE-087 :data:`ACL_ANY_KEY` OR-group into a nested ``Filter``.

    ``value`` must be a non-empty list/tuple of non-empty mappings, none of whose
    keys is ``$``-prefixed (no nested ``$any`` — a single flat OR-group only).
    Each sub-mapping compiles (with the existing grammar) into a
    ``Filter(must=[...])``; the group is their OR: ``Filter(should=[...])``. A
    Qdrant ``Filter`` is itself a valid ``Condition``, so the returned nested
    Filter appends to the outer ``must`` list — OR within the group, AND with
    everything else. Fail-closed: any shape violation raises ``ValueError``.
    """
    if not isinstance(value, list | tuple) or not value:
        raise ValueError(f"{key!r} must be a non-empty list of sub-filters, got {value!r}")
    subs: list[models.Condition] = []
    for sub in value:
        if not isinstance(sub, Mapping) or not sub:
            raise ValueError(f"{key!r} sub-filter must be a non-empty mapping, got {sub!r}")
        sub_conditions: list[models.Condition] = []
        for sub_key, sub_value in sub.items():
            if isinstance(sub_key, str) and sub_key.startswith("$"):
                raise ValueError(
                    f"{key!r} sub-filter may not contain a nested reserved key {sub_key!r}"
                )
            sub_conditions.append(_field_condition(sub_key, sub_value))
        subs.append(models.Filter(must=sub_conditions))
    return models.Filter(should=subs)


def _build_filter(filters: Mapping[str, Any] | None) -> models.Filter | None:
    """Translate the ChunkStore filter grammar into a Qdrant ``Filter``.

    Keys are used **verbatim** as the payload field path (e.g. ``"acl"``,
    ``"metadata.team"`` for a nested field); values compile per
    :func:`_field_condition`.

    NE-087 reserved grammar: :data:`ACL_ANY_KEY` (``"$any"``) is handled FIRST and
    compiles to a nested OR-group (:func:`_build_any_group`) appended to the outer
    ``must`` list. Any OTHER ``$``-prefixed key raises ``ValueError`` (an unknown
    reserved token fails closed — the anti-smuggling discipline).
    """
    if not filters:
        return None
    conditions: list[models.Condition] = []
    for key, value in filters.items():
        if key == ACL_ANY_KEY:
            conditions.append(_build_any_group(key, value))
        elif isinstance(key, str) and key.startswith("$"):
            raise ValueError(
                f"unknown reserved filter key {key!r}: only {ACL_ANY_KEY!r} is a "
                "recognised '$'-prefixed filter grammar token"
            )
        else:
            conditions.append(_field_condition(key, value))
    return models.Filter(must=conditions)


class QdrantChunkStore:
    """Dense+sparse hybrid ``ChunkStore`` backed by a Qdrant collection.

    ``client`` accepts EITHER one :class:`AsyncQdrantClient` instance OR a
    zero-argument factory of them. The factory form exists because the
    ``AsyncQdrantClient`` is EVENT-LOOP-AFFINE (its transport binds asyncio
    primitives to the first loop that uses it), while the composed store is
    shared by ne-worker's DBOS loop (turn tools: ``read.knowledge.search``) AND
    its persistent cron-tick loop (connector-sync ingest). Observed live
    (ne-pilot-ne-worker-1, 2026-08-28): a cron tick binding the ONE shared
    client to a tick loop made turn reads die with "bound to a different event
    loop" / "Event loop is closed". With a factory, each event loop lazily gets
    its OWN client (cached per loop, weakly, so a departed loop's client is
    dropped); a plain client instance keeps today's single-loop behavior for
    every existing composition and test.
    """

    def __init__(
        self,
        client: AsyncQdrantClient | Callable[[], AsyncQdrantClient],
        collection: str,
        embedder: Embedder,
        sparse_encoder: SparseEncoder,
        *,
        group_expander: GroupExpander | None = None,
    ) -> None:
        if callable(client):
            self._client_factory: Callable[[], AsyncQdrantClient] | None = client
            self._client: AsyncQdrantClient | None = None
        else:
            self._client_factory = None
            self._client = client
        self._clients_by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, AsyncQdrantClient
        ] = weakref.WeakKeyDictionary()
        self._clients_lock = threading.Lock()
        self._collection = collection
        self._embedder = embedder
        self._sparse_encoder = sparse_encoder
        self._group_expander = group_expander
        self._settlement_owner = object()
        self._settlements: dict[str, _QdrantSettlementState] = {}

    def _c(self) -> AsyncQdrantClient:
        """The client for the CURRENT running event loop.

        Injected-instance mode returns the one client unchanged (single-loop
        compositions and tests). Factory mode lazily builds and caches one
        client per running loop, so no ``AsyncQdrantClient`` is ever awaited
        from two different loops (the loop-affinity law this store enforces
        after the 2026-08-28 cron-offload incident).
        """
        if self._client is not None:
            return self._client
        factory = self._client_factory
        if factory is None:  # pragma: no cover - init guarantees one of the two
            raise RuntimeError("QdrantChunkStore has neither a client nor a factory")
        loop = asyncio.get_running_loop()
        with self._clients_lock:
            client = self._clients_by_loop.get(loop)
            if client is None:
                client = factory()
                self._clients_by_loop[loop] = client
            return client

    def vector_evidence_descriptor(self) -> VectorEvidenceDescriptor:
        """Describe the next write without disclosing collection or endpoint data."""
        return vector_evidence_descriptor(
            backend="qdrant",
            logical_index=self._collection,
            model_id=self._embedder.model_id,
            dimensions=self._embedder.dimension,
        )

    async def ensure_collection(self) -> None:
        """Create the collection (named dense+sparse vectors) if it does not exist."""
        if await self._c().collection_exists(self._collection):
            return
        await self._c().create_collection(
            collection_name=self._collection,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=self._embedder.dimension, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        # Local (in-process) mode warns that payload indexes are a no-op there;
        # harmless — the real server honours the index. Suppress at this call
        # site only.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            await self._c().create_payload_index(
                collection_name=self._collection,
                field_name=_ACL_FIELD,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        await self.upsert_chunks_with_evidence(chunks)

    async def upsert_chunks_with_evidence(
        self, chunks: Sequence[Chunk]
    ) -> tuple[VectorEvidenceWriteReceipt, ...]:
        items = list(chunks)
        if not items:
            return ()
        outcome = await self.upsert_chunks_with_settlement(items)
        await self.settle_vector_write(
            outcome.settlement,
            keep_chunk_ids=[receipt.chunk_id for receipt in outcome.receipts],
            restore_chunk_ids=(),
        )
        return outcome.receipts

    async def upsert_chunks_with_settlement(
        self, chunks: Sequence[Chunk]
    ) -> VectorEvidenceWriteOutcome:
        """Write chunks and return bounded submitted-embedding evidence after ack.

        The full dense vectors still go only to Qdrant.  The returned descriptors
        carry at most the first eight finite components. Prepared receipt objects
        remain unpublished until every strongly ordered batch acknowledges.
        """
        items = list(chunks)
        if not items:
            raise ValueError("qdrant settlement write requires at least one chunk")
        descriptor = self.vector_evidence_descriptor()
        chunk_ids = tuple(chunk.chunk_id for chunk in items)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("qdrant upsert chunk ids must be distinct")

        embedded = tuple(await self._embedder.embed([chunk.text for chunk in items]))
        if len(embedded) != len(items):
            raise ValueError("dense embedder result count does not match chunk count")
        dense_vectors = tuple(
            validated_dense_embedding(vector, dimensions=descriptor.dimensions)
            for vector in embedded
        )
        sparse_vectors = tuple(await self._sparse_encoder.encode([chunk.text for chunk in items]))
        if len(sparse_vectors) != len(items):
            raise ValueError("sparse encoder result count does not match chunk count")
        points = tuple(
            models.PointStruct(
                id=point_id_for(chunk.chunk_id),
                vector={
                    DENSE_VECTOR_NAME: list(dense),
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(sparse.indices), values=list(sparse.values)
                    ),
                },
                payload=chunk.model_dump(mode="json"),
            )
            for chunk, dense, sparse in zip(items, dense_vectors, sparse_vectors, strict=True)
        )
        receipts = validate_vector_evidence_receipts(
            tuple(
                submitted_embedding_receipt(chunk.chunk_id, descriptor, dense)
                for chunk, dense in zip(items, dense_vectors, strict=True)
            ),
            chunk_ids,
        )
        point_ids = tuple(point.id for point in points)
        preimages = await self._load_point_preimages(point_ids)

        write_started = False
        try:
            for start in range(0, len(points), _UPSERT_BATCH_SIZE):
                batch = points[start : start + _UPSERT_BATCH_SIZE]
                write_started = True
                await self._c().upsert(
                    collection_name=self._collection,
                    points=list(batch),
                    wait=True,
                    ordering=models.WriteOrdering.STRONG,
                )
        except Exception:
            if write_started:
                try:
                    await self._restore_and_verify_preimages(point_ids, preimages)
                except Exception as compensation_error:
                    raise QdrantWriteCompensationError(
                        "qdrant write failed and exact pre-call state could not be restored"
                    ) from compensation_error
            raise
        try:
            postimages = await self._load_point_preimages(point_ids)
            if set(postimages) != set(point_ids):
                raise RuntimeError("qdrant acknowledgement verification omitted written point ids")
        except Exception as verification_error:
            try:
                await self._restore_and_verify_preimages(point_ids, preimages)
            except Exception as compensation_error:
                raise QdrantWriteCompensationError(
                    "qdrant write acknowledgement failed and exact pre-call state "
                    "could not be restored"
                ) from compensation_error
            raise RuntimeError(
                "qdrant write acknowledgement could not be verified"
            ) from verification_error
        token = uuid.uuid4().hex
        chunk_to_point = dict(zip(chunk_ids, point_ids, strict=True))
        self._settlements[token] = _QdrantSettlementState(
            chunk_to_point=chunk_to_point,
            preimages=preimages,
            postimages=postimages,
        )
        return VectorEvidenceWriteOutcome(
            receipts=receipts,
            settlement=VectorWriteSettlementHandle(
                owner=self._settlement_owner, token=token, chunk_ids=chunk_ids
            ),
        )

    async def settle_vector_write(
        self,
        handle: VectorWriteSettlementHandle,
        *,
        keep_chunk_ids: Sequence[str],
        restore_chunk_ids: Sequence[str],
    ) -> None:
        """Resolve one acknowledged call using exact raw point images.

        This follow-up compensation is process-local and single-fault bounded;
        it is not crash-safe, double-fault ACID, or safe against concurrent
        cross-process writes to the same ids.
        """
        token = handle._claim(
            owner=self._settlement_owner,
            keep_chunk_ids=keep_chunk_ids,
            restore_chunk_ids=restore_chunk_ids,
        )
        state = self._settlements.pop(token, None)
        if state is None:
            raise QdrantWriteCompensationError("qdrant settlement state is unavailable")
        restore_points = tuple(state.chunk_to_point[chunk_id] for chunk_id in restore_chunk_ids)
        expected = dict(state.postimages)
        for point_id in restore_points:
            if point_id in state.preimages:
                expected[point_id] = state.preimages[point_id]
            else:
                expected.pop(point_id, None)
        try:
            restore_preimages = {
                point_id: state.preimages[point_id]
                for point_id in restore_points
                if point_id in state.preimages
            }
            await self._restore_and_verify_preimages(restore_points, restore_preimages)
            current = await self._load_point_preimages(tuple(state.chunk_to_point.values()))
            if current != expected:
                raise RuntimeError("qdrant settlement verification did not match expected state")
        except Exception as exc:
            raise QdrantWriteCompensationError(
                "qdrant acknowledged write settlement could not be restored and verified"
            ) from exc

    async def _load_point_preimages(
        self, point_ids: Sequence[models.ExtendedPointId]
    ) -> dict[models.ExtendedPointId, _PointPreimage]:
        records = await self._c().retrieve(
            collection_name=self._collection,
            ids=point_ids,
            with_payload=True,
            with_vectors=True,
            consistency=models.ReadConsistencyType.ALL,
        )
        requested = set(point_ids)
        result: dict[models.ExtendedPointId, _PointPreimage] = {}
        for record in records:
            if record.id not in requested or record.id in result:
                raise RuntimeError("qdrant preimage read returned ambiguous point ids")
            result[record.id] = _PointPreimage(
                point_id=record.id,
                payload=deepcopy(record.payload),
                vector=deepcopy(record.vector),
            )
        return result

    async def _restore_and_verify_preimages(
        self,
        point_ids: Sequence[models.ExtendedPointId],
        preimages: Mapping[models.ExtendedPointId, _PointPreimage],
    ) -> None:
        new_ids = [point_id for point_id in point_ids if point_id not in preimages]
        if new_ids:
            await self._c().delete(
                collection_name=self._collection,
                points_selector=models.PointIdsList(points=new_ids),
                wait=True,
                ordering=models.WriteOrdering.STRONG,
            )
        if preimages:
            restore_points = [
                models.PointStruct(
                    id=preimage.point_id,
                    vector=cast(Any, deepcopy(preimage.vector)),
                    payload=deepcopy(preimage.payload),
                )
                for preimage in preimages.values()
            ]
            await self._c().upsert(
                collection_name=self._collection,
                points=restore_points,
                wait=True,
                ordering=models.WriteOrdering.STRONG,
            )
        restored = await self._load_point_preimages(point_ids)
        if restored != dict(preimages):
            raise RuntimeError("qdrant compensation verification did not match pre-call state")

    async def update_payload(self, chunks: Sequence[Chunk]) -> None:
        """The ``PayloadUpdatable`` capability (NE-026 P2) — a cheap acl/metadata fix.

        Overwrites ONLY the ``acl``/``metadata`` payload fields of each chunk's
        already-indexed point via Qdrant's ``set_payload``, with NO
        ``embed()``/``encode()`` calls; vectors and all other point data are left
        untouched. Structurally satisfies ``ne.brain.ingest.spine.PayloadUpdatable``
        (duck-typed, not imported — same pattern as :meth:`get_filtered` for
        ``retriever.FilteredChunkFetch``). Points that do not exist are created by
        ``set_payload``, but the spine only passes chunks it just fetched from this
        store, so in practice every id already exists.
        """
        for chunk in chunks:
            dumped = chunk.model_dump(mode="json")
            await self._c().set_payload(
                collection_name=self._collection,
                payload={"acl": dumped["acl"], "metadata": dumped["metadata"]},
                points=[point_id_for(chunk.chunk_id)],
            )

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        """Unscoped fetch-by-id. Unknown ids are OMITTED (never raise); the
        returned sequence preserves the input id order for the ids that exist."""
        ids = [point_id_for(cid) for cid in chunk_ids]
        if not ids:
            return []
        records = await self._c().retrieve(
            collection_name=self._collection, ids=ids, with_payload=True
        )
        by_id = {record.id: record.payload for record in records}
        result: list[Chunk] = []
        for cid in chunk_ids:
            payload = by_id.get(point_id_for(cid))
            if payload is not None:
                result.append(Chunk.model_validate(payload))
        return result

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        """Payload-filtered fetch-by-id, filtering server-side inside Qdrant.

        The ACL-seam counterpart of :meth:`get`, used by principal-scoped
        callers (``HybridRetriever.retrieve``) so trimming mirrors
        ``query()``'s server-side filtering. Unknown ids and ids excluded by
        ``filters`` are OMITTED (never raise); input id order is preserved
        for surviving ids. With ``filters=None``/empty this is semantically
        identical to :meth:`get`.
        """
        ids: list[models.ExtendedPointId] = [point_id_for(cid) for cid in chunk_ids]
        if not ids:
            return []
        conditions: list[models.Condition] = []
        conditions.append(models.HasIdCondition(has_id=ids))
        base = _build_filter(filters)
        if base is not None and isinstance(base.must, list):
            conditions.extend(base.must)
        records, _ = await self._c().scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(must=conditions),
            limit=len(ids),
            with_payload=True,
        )
        by_id = {record.id: record.payload for record in records}
        result: list[Chunk] = []
        for cid in chunk_ids:
            payload = by_id.get(point_id_for(cid))
            if payload is not None:
                result.append(Chunk.model_validate(payload))
        return result

    async def scan_filtered(self, *, filters: Mapping[str, Any], limit: int) -> Sequence[Chunk]:
        """Filter-only scroll: every chunk matching ``filters``, up to ``limit``.

        NE-S5-INTERNAL-CRM: the exact-record read the internal CRM lookup needs
        — no vector, no ranking, deterministic. ``filters`` uses the SAME frozen
        grammar as :meth:`query`/:meth:`get_filtered` and must be non-empty (an
        unfiltered whole-collection scan is never what a governed read means —
        fail closed rather than over-return). Pagination is chased server-side
        until ``limit`` chunks or the collection is exhausted; a caller that
        receives exactly ``limit`` chunks cannot know the scan was complete and
        must treat it as truncated.
        """
        if not filters:
            raise ValueError("scan_filtered requires a non-empty filter")
        if limit < 1:
            raise ValueError("scan_filtered requires limit >= 1")
        scroll_filter = _build_filter(filters)
        chunks: list[Chunk] = []
        offset: Any = None
        while len(chunks) < limit:
            records, offset = await self._c().scroll(
                collection_name=self._collection,
                scroll_filter=scroll_filter,
                limit=min(256, limit - len(chunks)),
                offset=offset,
                with_payload=True,
            )
            for record in records:
                if record.payload is not None:
                    chunks.append(Chunk.model_validate(record.payload))
            if offset is None:
                break
        return chunks[:limit]

    async def query(
        self,
        *,
        dense: Sequence[float] | None = None,
        sparse: SparseVector | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ScoredChunk]:
        if dense is None and sparse is None:
            raise ValueError("query requires at least one of dense/sparse")

        qfilter = _build_filter(filters)

        if dense is not None and sparse is not None:
            sparse_query = models.SparseVector(
                indices=list(sparse.indices), values=list(sparse.values)
            )
            response = await self._c().query_points(
                collection_name=self._collection,
                prefetch=[
                    models.Prefetch(
                        query=list(dense),
                        using=DENSE_VECTOR_NAME,
                        limit=2 * top_k,
                        filter=qfilter,
                    ),
                    models.Prefetch(
                        query=sparse_query,
                        using=SPARSE_VECTOR_NAME,
                        limit=2 * top_k,
                        filter=qfilter,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=top_k,
                with_payload=True,
            )
        elif dense is not None:
            response = await self._c().query_points(
                collection_name=self._collection,
                query=list(dense),
                using=DENSE_VECTOR_NAME,
                query_filter=qfilter,
                limit=top_k,
                with_payload=True,
            )
        else:
            assert sparse is not None
            response = await self._c().query_points(
                collection_name=self._collection,
                query=models.SparseVector(indices=list(sparse.indices), values=list(sparse.values)),
                using=SPARSE_VECTOR_NAME,
                query_filter=qfilter,
                limit=top_k,
                with_payload=True,
            )

        return [
            ScoredChunk(chunk=Chunk.model_validate(point.payload), score=point.score)
            for point in response.points
        ]

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        ids: list[models.ExtendedPointId] = [point_id_for(cid) for cid in chunk_ids]
        if not ids:
            return
        await self._c().delete(
            collection_name=self._collection,
            points_selector=models.PointIdsList(points=ids),
        )

    def permission_filter(self, principal: Principal) -> models.Filter:
        """NE-043 C7 security-trimming seam — REAL, fail-closed (never ``None``).

        Builds a Qdrant ``Filter`` matching a point IFF its ``acl`` payload array
        contains the principal id OR any of its expanded group labels
        (:func:`ne.brain.trimming.acl_match_labels` over the injected
        ``group_expander``). Fail-closed guarantees:

        * the label set ALWAYS contains the principal id, so the filter can never
          be empty and the return type is narrowed to ``models.Filter`` — ``None``
          (which the retriever reads as "trimming disabled") is gone forever;
        * a chunk with an EMPTY or MISSING ``acl`` payload matches NOTHING —
          ``MatchAny`` over an empty/absent array field never matches — which IS
          the never-match-all direction;
        * without an injected expander the labels fall back to the verified token
          identity (id + ``Principal.groups``); never widened, never match-all.

        Built via the shared :func:`_build_filter` so this store-level filter and
        the ``PrincipalAclFilter`` retriever-hook mapping can never drift.
        """
        labels = acl_match_labels(principal, self._group_expander)
        flt = _build_filter({_ACL_FIELD: list(labels)})
        assert flt is not None  # labels are always non-empty (contains principal.id)
        return flt

    # -- 015A0-AUTHORITY-CORE immutable-artifact candidates -------------------
    #
    # These are the whole ``ImmutableArtifactVectorSeam``: write one candidate
    # point, and list the candidates written for an artifact. There is
    # deliberately no delete and no update here — the immutable-artifact store
    # must not be able to reach an unconditional write through this adapter, and
    # a loser's inert candidate is retained rather than reclaimed pre-demo.

    async def put_immutable_artifact_candidate(
        self, *, candidate_id: str, artifact_id: str, dense: Sequence[float]
    ) -> None:
        """Write one candidate vector point for an immutable authority artifact."""
        from ne.brain.write.immutable_artifacts import (
            AUTHORITY_ARTIFACT_CANDIDATE_KEY,
            AUTHORITY_ARTIFACT_PAYLOAD_KEY,
        )

        vector = validated_dense_embedding(dense, dimensions=self._embedder.dimension)
        await self._c().upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=point_id_for(candidate_id),
                    vector={DENSE_VECTOR_NAME: list(vector)},
                    payload={
                        AUTHORITY_ARTIFACT_PAYLOAD_KEY: artifact_id,
                        AUTHORITY_ARTIFACT_CANDIDATE_KEY: candidate_id,
                    },
                )
            ],
            wait=True,
        )

    async def immutable_artifact_candidate_ids(self, artifact_id: str) -> tuple[str, ...]:
        """Every candidate written for ``artifact_id``, selected winner included."""
        from ne.brain.write.immutable_artifacts import (
            AUTHORITY_ARTIFACT_CANDIDATE_KEY,
            AUTHORITY_ARTIFACT_PAYLOAD_KEY,
        )

        found: list[str] = []
        offset: Any = None
        while True:
            records, offset = await self._c().scroll(
                collection_name=self._collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key=AUTHORITY_ARTIFACT_PAYLOAD_KEY,
                            match=models.MatchValue(value=artifact_id),
                        )
                    ]
                ),
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            for record in records:
                payload = record.payload or {}
                candidate = payload.get(AUTHORITY_ARTIFACT_CANDIDATE_KEY)
                if isinstance(candidate, str):
                    found.append(candidate)
            if offset is None:
                break
        return tuple(sorted(found))


__all__ = [
    "DENSE_VECTOR_NAME",
    "SPARSE_VECTOR_NAME",
    "QdrantChunkStore",
    "point_id_for",
]
