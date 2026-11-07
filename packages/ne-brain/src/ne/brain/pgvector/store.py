# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""pgvector ``ChunkStore`` — a drop-in alternate to :class:`~ne.brain.qdrant.QdrantChunkStore`.

Implements the frozen :class:`ne.contracts.knowledge.ChunkStore` Protocol plus the
structural ``FilteredChunkFetch`` (``get_filtered``) and ``PayloadUpdatable``
(``update_payload``) capabilities the NE-026 spine + NE-043 C7 seam consume —
CONFORMING to those contracts, never altering :class:`ne.brain.retriever.HybridRetriever`
or the ACL filter. It stores each chunk in a Postgres table with a pgvector
``vector`` column (HNSW cosine index) + a sparse inverted-index table, and answers
hybrid queries with the SAME retrieval contract as the Qdrant store.

SCHEMA (per ``table``, ``t``):

* ``{t}(chunk_id text PK, payload jsonb NOT NULL, dense vector(dim) NOT NULL)`` —
  HNSW index ``USING hnsw (dense vector_cosine_ops)``; GIN index
  ``USING gin (payload jsonb_path_ops)`` for the ``@>`` filter legs.
* ``{t}_sparse(chunk_id text -> {t} ON DELETE CASCADE, term_index bigint,
  weight real, PRIMARY KEY(chunk_id, term_index))`` — btree on ``term_index``.

RETRIEVAL PARITY (mirrors ``qdrant_client`` 1.18.x local mode exactly):

* DENSE leg — ``ORDER BY dense <=> qvec`` (pgvector cosine distance); score
  ``1 - (dense <=> qvec)`` = cosine similarity (Qdrant COSINE convention).
* SPARSE leg — Qdrant's server/local IDF, VERBATIM: ``idf(term) =
  ln((N - df + 0.5)/(df + 0.5) + 1)`` with ``N`` = TOTAL row count and ``df`` =
  GLOBAL document frequency (both UNFILTERED, exactly like
  ``local_collection._rescore_idf`` which uses ``count(count_filter=None)`` and the
  global ``idf_store``); score ``= sum(stored.weight * query.qweight * idf(term))``
  over shared terms. The filter restricts which CANDIDATES are scored, never the
  IDF statistics — again matching Qdrant's per-prefetch filter.
* HYBRID — each leg limited to ``2 * top_k`` (the Qdrant prefetch limit), then
  Reciprocal Rank Fusion in-adapter mirroring ``qdrant_client/hybrid/fusion.py``
  VERBATIM: ``score += 1/(pos0 + 2)`` (``ranking_constant_k = 2``, weight ``1.0``),
  accumulated over ``[dense_leg, sparse_leg]`` in that order, stable-sorted by
  score desc (insertion order breaks ties — identical to the reference), cut to
  ``top_k``.

C7 (LOAD-BEARING): the filter is compiled server-side into EVERY leg's ``WHERE``
BEFORE fusion and into ``get_filtered``, from the SAME
:func:`ne.brain.trimming.acl_match_labels` labels and the SAME grammar (incl.
NE-087 ``$any``) as Qdrant — a vector-store swap does NOT weaken permission-trimmed
reads. :meth:`permission_filter` emits the SAME mapping
:class:`ne.brain.trimming.PrincipalAclFilter` emits, compiled by the SAME
:func:`ne.brain.pgvector.filters.compile_filters` used by :meth:`query` — no drift
by construction.

C2: the adapter talks ONLY to the injected in-network Postgres DSN (no egress,
single-tenant). A single lazily-opened async connection + ``asyncio.Lock``
serializes store I/O (correctness over throughput; ``psycopg_pool`` deliberately
NOT adopted to keep zero new deps — a documented future knob, RISK 6).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

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
from ne.brain.pgvector.filters import compile_filters, validate_identifier
from ne.brain.trimming import GroupExpander, acl_match_labels
from ne.contracts.knowledge import Embedder, SparseEncoder
from ne.contracts.types import Chunk, Principal, ScoredChunk, SparseVector

_UPSERT_BATCH_SIZE = 64


@dataclass(frozen=True)
class _PgvectorPointImage:
    payload: Any
    dense: str
    sparse: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class _PgvectorSettlementState:
    preimages: dict[str, _PgvectorPointImage]
    postimages: dict[str, _PgvectorPointImage]


def _vector_literal(values: Sequence[float]) -> str:
    """Serialize a dense vector as a pgvector text literal (no client lib needed)."""
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def _rrf_fuse(
    responses: Sequence[Sequence[tuple[str, Any, float]]], limit: int
) -> list[tuple[str, Any, float]]:
    """Reciprocal Rank Fusion — VERBATIM ``qdrant_client/hybrid/fusion.py`` (K=2).

    ``compute_score(pos) = 1/((pos + 1)/1.0 + 2 - 1) = 1/(pos0 + 2)``; scores
    accumulate per ``chunk_id`` across ``responses`` in order; the result is a
    stable sort by score desc (dict insertion order breaks ties — identical to the
    reference), cut to ``limit``.
    """
    scores: dict[str, float] = {}
    pile: dict[str, tuple[str, Any]] = {}
    for response in responses:
        for pos, (chunk_id, payload, _score) in enumerate(response):
            contribution = 1.0 / (pos + 2.0)
            if chunk_id in scores:
                scores[chunk_id] += contribution
            else:
                pile[chunk_id] = (chunk_id, payload)
                scores[chunk_id] = contribution
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    fused: list[tuple[str, Any, float]] = []
    for chunk_id, score in ordered[:limit]:
        _, payload = pile[chunk_id]
        fused.append((chunk_id, payload, score))
    return fused


class PgvectorChunkStore:
    """Dense+sparse hybrid ``ChunkStore`` backed by a Postgres pgvector table."""

    def __init__(
        self,
        dsn: str,
        table: str,
        embedder: Embedder,
        sparse_encoder: SparseEncoder,
        *,
        group_expander: GroupExpander | None = None,
    ) -> None:
        self._dsn = dsn
        self._table = validate_identifier(table)
        self._sparse_table = validate_identifier(f"{table}_sparse")
        self._embedder = embedder
        self._sparse_encoder = sparse_encoder
        self._group_expander = group_expander
        self._artifact_table = validate_identifier(f"{table}_source_artifact_candidates")
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._ensured = False
        self._artifact_ensured = False
        self._settlement_owner = object()
        self._settlements: dict[str, _PgvectorSettlementState] = {}

    def vector_evidence_descriptor(self) -> VectorEvidenceDescriptor:
        """Describe the next write without disclosing table or connection data."""
        return vector_evidence_descriptor(
            backend="pgvector",
            logical_index=self._table,
            model_id=self._embedder.model_id,
            dimensions=self._embedder.dimension,
        )

    # -- connection / schema ------------------------------------------------

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[Any]:
        """Yield the single lazily-opened connection under the lock (schema ensured once)."""
        from ne.brain.pgvector import _pg

        async with self._lock:
            if self._conn is None:
                self._conn = await _pg.connect(self._dsn)
            if not self._ensured:
                await self._ensure(self._conn)
                self._ensured = True
            yield self._conn

    async def _ensure(self, conn: Any) -> None:
        from ne.brain.pgvector import _pg

        t, st = self._table, self._sparse_table
        dim = self._embedder.dimension
        await _pg.execute(conn, "CREATE EXTENSION IF NOT EXISTS vector")
        # A freshly-created extension registers the hnsw.* GUCs only now.
        await _pg.try_set_iterative_scan(conn)
        await _pg.execute(
            conn,
            f"CREATE TABLE IF NOT EXISTS {t} ("
            "chunk_id text PRIMARY KEY, "
            "payload jsonb NOT NULL, "
            f"dense vector({dim}) NOT NULL)",
        )
        await _pg.execute(
            conn,
            f"CREATE TABLE IF NOT EXISTS {st} ("
            f"chunk_id text NOT NULL REFERENCES {t}(chunk_id) ON DELETE CASCADE, "
            "term_index bigint NOT NULL, "
            "weight real NOT NULL, "
            "PRIMARY KEY (chunk_id, term_index))",
        )
        await _pg.execute(
            conn,
            f"CREATE INDEX IF NOT EXISTS {t}_dense_hnsw ON {t} "
            "USING hnsw (dense vector_cosine_ops)",
        )
        await _pg.execute(
            conn,
            f"CREATE INDEX IF NOT EXISTS {t}_payload_gin ON {t} USING gin (payload jsonb_path_ops)",
        )
        await _pg.execute(
            conn,
            f"CREATE INDEX IF NOT EXISTS {st}_term_idx ON {st} (term_index)",
        )

    # -- write --------------------------------------------------------------

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
        """Write chunks and return bounded submitted-embedding evidence after commit.

        The returned values are prefixes of the exact finite dense tuples passed
        to pgvector, not a claim about its stored byte representation.
        """
        from ne.brain.pgvector import _pg

        items = list(chunks)
        if not items:
            raise ValueError("pgvector settlement write requires at least one chunk")
        t, st = self._table, self._sparse_table
        descriptor = self.vector_evidence_descriptor()
        chunk_ids = tuple(chunk.chunk_id for chunk in items)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("pgvector upsert chunk ids must be distinct")

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
        receipts = validate_vector_evidence_receipts(
            tuple(
                submitted_embedding_receipt(chunk.chunk_id, descriptor, dense)
                for chunk, dense in zip(items, dense_vectors, strict=True)
            ),
            chunk_ids,
        )

        async with self._acquire() as conn, conn.transaction():
            preimages = await self._load_raw_points(conn, chunk_ids)
            for start in range(0, len(items), _UPSERT_BATCH_SIZE):
                batch = items[start : start + _UPSERT_BATCH_SIZE]
                batch_dense = dense_vectors[start : start + _UPSERT_BATCH_SIZE]
                batch_sparse = sparse_vectors[start : start + _UPSERT_BATCH_SIZE]
                main_rows = [
                    (
                        chunk.chunk_id,
                        json.dumps(chunk.model_dump(mode="json")),
                        _vector_literal(dense),
                    )
                    for chunk, dense in zip(batch, batch_dense, strict=True)
                ]
                sparse_rows = [
                    (chunk.chunk_id, int(index), float(weight))
                    for chunk, sparse in zip(batch, batch_sparse, strict=True)
                    for index, weight in zip(sparse.indices, sparse.values, strict=True)
                ]
                batch_ids = [chunk.chunk_id for chunk in batch]
                await _pg.executemany(
                    conn,
                    f"INSERT INTO {t} (chunk_id, payload, dense) "
                    "VALUES (%s, %s::jsonb, %s::vector) "
                    "ON CONFLICT (chunk_id) DO UPDATE SET "
                    "payload = EXCLUDED.payload, dense = EXCLUDED.dense",
                    main_rows,
                )
                await _pg.execute(conn, f"DELETE FROM {st} WHERE chunk_id = ANY(%s)", (batch_ids,))
                if sparse_rows:
                    await _pg.executemany(
                        conn,
                        f"INSERT INTO {st} (chunk_id, term_index, weight) VALUES (%s, %s, %s)",
                        sparse_rows,
                    )
            postimages = await self._load_raw_points(conn, chunk_ids)
            if set(postimages) != set(chunk_ids):
                raise RuntimeError(
                    "pgvector acknowledgement verification omitted written chunk ids"
                )
        token = uuid.uuid4().hex
        self._settlements[token] = _PgvectorSettlementState(
            preimages=preimages,
            postimages=postimages,
        )
        return VectorEvidenceWriteOutcome(
            receipts=receipts,
            settlement=VectorWriteSettlementHandle(
                owner=self._settlement_owner, token=token, chunk_ids=chunk_ids
            ),
        )

    async def _load_raw_points(
        self, conn: Any, chunk_ids: Sequence[str]
    ) -> dict[str, _PgvectorPointImage]:
        from ne.brain.pgvector import _pg

        if not chunk_ids:
            return {}
        main_rows = await _pg.execute(
            conn,
            f"SELECT chunk_id, payload, dense::text FROM {self._table} WHERE chunk_id = ANY(%s)",
            (list(chunk_ids),),
        )
        sparse_rows = await _pg.execute(
            conn,
            f"SELECT chunk_id, term_index, weight FROM {self._sparse_table} "
            "WHERE chunk_id = ANY(%s) ORDER BY chunk_id, term_index",
            (list(chunk_ids),),
        )
        sparse_by_id: dict[str, list[tuple[int, float]]] = {}
        for chunk_id, term_index, weight in sparse_rows or ():
            sparse_by_id.setdefault(str(chunk_id), []).append((int(term_index), float(weight)))
        result: dict[str, _PgvectorPointImage] = {}
        for chunk_id, payload, dense in main_rows or ():
            normalized = str(chunk_id)
            if normalized not in set(chunk_ids) or normalized in result:
                raise RuntimeError("pgvector raw image read returned ambiguous chunk ids")
            result[normalized] = _PgvectorPointImage(
                payload=payload,
                dense=str(dense),
                sparse=tuple(sparse_by_id.get(normalized, ())),
            )
        return result

    async def settle_vector_write(
        self,
        handle: VectorWriteSettlementHandle,
        *,
        keep_chunk_ids: Sequence[str],
        restore_chunk_ids: Sequence[str],
    ) -> None:
        """Settle a successful call without re-embedding any restored row.

        This compensates one follow-up failure within this store process. It is
        not a durable crash journal or cross-process concurrency fence.
        """
        from ne.brain.pgvector import _pg

        token = handle._claim(
            owner=self._settlement_owner,
            keep_chunk_ids=keep_chunk_ids,
            restore_chunk_ids=restore_chunk_ids,
        )
        state = self._settlements.pop(token, None)
        if state is None:
            raise RuntimeError("pgvector settlement state is unavailable")
        expected = dict(state.postimages)
        for chunk_id in restore_chunk_ids:
            if chunk_id in state.preimages:
                expected[chunk_id] = state.preimages[chunk_id]
            else:
                expected.pop(chunk_id, None)
        restore = list(restore_chunk_ids)
        if not restore:
            async with self._acquire() as conn:
                current = await self._load_raw_points(conn, tuple(state.postimages))
                if current != expected:
                    raise RuntimeError(
                        "pgvector settlement verification did not match expected state"
                    )
            return
        async with self._acquire() as conn, conn.transaction():
            if restore:
                await _pg.execute(
                    conn,
                    f"DELETE FROM {self._table} WHERE chunk_id = ANY(%s)",
                    (restore,),
                )
                main_rows = [
                    (
                        chunk_id,
                        json.dumps(state.preimages[chunk_id].payload),
                        state.preimages[chunk_id].dense,
                    )
                    for chunk_id in restore
                    if chunk_id in state.preimages
                ]
                if main_rows:
                    await _pg.executemany(
                        conn,
                        f"INSERT INTO {self._table} (chunk_id, payload, dense) "
                        "VALUES (%s, %s::jsonb, %s::vector)",
                        main_rows,
                    )
                sparse_rows = [
                    (chunk_id, term_index, weight)
                    for chunk_id in restore
                    if chunk_id in state.preimages
                    for term_index, weight in state.preimages[chunk_id].sparse
                ]
                if sparse_rows:
                    await _pg.executemany(
                        conn,
                        f"INSERT INTO {self._sparse_table} (chunk_id, term_index, weight) "
                        "VALUES (%s, %s, %s)",
                        sparse_rows,
                    )
            current = await self._load_raw_points(conn, tuple(state.postimages))
            if current != expected:
                raise RuntimeError("pgvector settlement verification did not match expected state")

    async def update_payload(self, chunks: Sequence[Chunk]) -> None:
        """``PayloadUpdatable`` (NE-026 P2) — overwrite ONLY acl/metadata, NO re-embed.

        UPDATE-only: unlike Qdrant ``set_payload`` (which CREATES a missing point),
        a chunk_id absent here updates 0 rows — a documented semantic gap that is
        C7-safe (the spine only passes chunks it just fetched from this store, so
        every id already exists). No ``embed()``/``encode()`` calls.
        """
        from ne.brain.pgvector import _pg

        t = self._table
        async with self._acquire() as conn:
            for chunk in chunks:
                dumped = chunk.model_dump(mode="json")
                patch = json.dumps({"acl": dumped["acl"], "metadata": dumped["metadata"]})
                await _pg.execute(
                    conn,
                    f"UPDATE {t} SET payload = payload || %s::jsonb WHERE chunk_id = %s",
                    (patch, chunk.chunk_id),
                )

    async def delete(self, chunk_ids: Sequence[str]) -> None:
        from ne.brain.pgvector import _pg

        ids = list(chunk_ids)
        if not ids:
            return
        async with self._acquire() as conn:
            # ON DELETE CASCADE clears the {t}_sparse rows.
            await _pg.execute(conn, f"DELETE FROM {self._table} WHERE chunk_id = ANY(%s)", (ids,))

    # -- read ---------------------------------------------------------------

    async def get(self, chunk_ids: Sequence[str]) -> Sequence[Chunk]:
        """Unscoped fetch-by-id. Unknown ids are OMITTED; caller id order preserved."""
        from ne.brain.pgvector import _pg

        ids = list(chunk_ids)
        if not ids:
            return []
        async with self._acquire() as conn:
            rows = await _pg.execute(
                conn,
                f"SELECT chunk_id, payload FROM {self._table} WHERE chunk_id = ANY(%s)",
                (ids,),
            )
        return self._reorder(chunk_ids, rows)

    async def get_filtered(
        self, chunk_ids: Sequence[str], *, filters: Mapping[str, Any] | None = None
    ) -> Sequence[Chunk]:
        """Payload-filtered fetch-by-id (server-side). ``filters=None`` ≡ :meth:`get`.

        The ACL-seam counterpart of :meth:`get` used by ``HybridRetriever.retrieve``
        so trimming mirrors :meth:`query`. Unknown ids and ids excluded by
        ``filters`` are OMITTED; input id order is preserved for survivors.
        """
        from ne.brain.pgvector import _pg

        ids = list(chunk_ids)
        if not ids:
            return []
        cond = compile_filters(filters)
        sql = f"SELECT chunk_id, payload FROM {self._table} WHERE chunk_id = ANY(%s)"
        params: list[Any] = [ids]
        if cond is not None:
            sql += " AND " + cond.sql
            params.extend(cond.params)
        async with self._acquire() as conn:
            rows = await _pg.execute(conn, sql, tuple(params))
        return self._reorder(chunk_ids, rows)

    async def scan_filtered(self, *, filters: Mapping[str, Any], limit: int) -> Sequence[Chunk]:
        """Filter-only scan: every chunk matching ``filters``, up to ``limit``.

        The pgvector mirror of ``QdrantChunkStore.scan_filtered`` (NE-S5-INTERNAL-CRM)
        — same frozen grammar, same fail-closed refusal of an empty filter, same
        truncation contract (exactly ``limit`` rows back means the caller must
        treat the scan as possibly incomplete). Ordered by ``chunk_id`` so the
        result is deterministic across runs.
        """
        from ne.brain.pgvector import _pg

        if not filters:
            raise ValueError("scan_filtered requires a non-empty filter")
        if limit < 1:
            raise ValueError("scan_filtered requires limit >= 1")
        cond = compile_filters(filters)
        assert cond is not None  # non-empty filters always compile to a fragment
        sql = (
            f"SELECT chunk_id, payload FROM {self._table} WHERE "
            + cond.sql
            + " ORDER BY chunk_id LIMIT %s"
        )
        params: list[Any] = [*cond.params, limit]
        async with self._acquire() as conn:
            rows = await _pg.execute(conn, sql, tuple(params))
        return [Chunk.model_validate(row[1]) for row in rows]

    @staticmethod
    def _reorder(chunk_ids: Sequence[str], rows: Sequence[tuple[Any, ...]]) -> list[Chunk]:
        by_id = {row[0]: row[1] for row in rows}
        result: list[Chunk] = []
        for cid in chunk_ids:
            payload = by_id.get(cid)
            if payload is not None:
                result.append(Chunk.model_validate(payload))
        return result

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
        cond = compile_filters(filters)
        async with self._acquire() as conn:
            if dense is not None and sparse is not None:
                dense_rows = await self._dense_leg(conn, dense, 2 * top_k, cond)
                sparse_rows = await self._sparse_leg(conn, sparse, 2 * top_k, cond)
                fused = _rrf_fuse([dense_rows, sparse_rows], top_k)
                return [
                    ScoredChunk(chunk=Chunk.model_validate(payload), score=score)
                    for _cid, payload, score in fused
                ]
            if dense is not None:
                rows = await self._dense_leg(conn, dense, top_k, cond)
            else:
                assert sparse is not None
                rows = await self._sparse_leg(conn, sparse, top_k, cond)
        return [
            ScoredChunk(chunk=Chunk.model_validate(payload), score=float(score))
            for _cid, payload, score in rows
        ]

    async def _dense_leg(
        self, conn: Any, dense: Sequence[float], limit: int, cond: Any
    ) -> list[tuple[str, Any, float]]:
        from ne.brain.pgvector import _pg

        vec = _vector_literal(dense)
        sql = f"SELECT chunk_id, payload, 1 - (dense <=> %s::vector) AS score FROM {self._table}"
        params: list[Any] = [vec]
        if cond is not None:
            sql += " WHERE " + cond.sql
            params.extend(cond.params)
        sql += " ORDER BY dense <=> %s::vector, chunk_id LIMIT %s"
        params.extend([vec, limit])
        rows = await _pg.execute(conn, sql, tuple(params))
        return [(row[0], row[1], row[2]) for row in rows]

    async def _sparse_leg(
        self, conn: Any, sparse: SparseVector, limit: int, cond: Any
    ) -> list[tuple[str, Any, float]]:
        from ne.brain.pgvector import _pg

        indices = [int(i) for i in sparse.indices]
        weights = [float(w) for w in sparse.values]
        if not indices:
            return []
        t, st = self._table, self._sparse_table
        where = ""
        params: list[Any] = [indices, weights]
        if cond is not None:
            where = "WHERE " + cond.sql
            params.extend(cond.params)
        params.append(limit)
        # N (row count) and df (per-term document frequency) are GLOBAL/UNFILTERED
        # — identical to Qdrant's ``count(count_filter=None)`` + global idf_store.
        sql = (
            "WITH q(term_index, qweight) AS "
            "(SELECT * FROM unnest(%s::bigint[], %s::real[])), "
            f"n AS (SELECT count(*)::double precision AS total FROM {t}), "
            "df AS (SELECT sp.term_index, count(*)::double precision AS df "
            f"FROM {st} sp JOIN q ON q.term_index = sp.term_index GROUP BY sp.term_index) "
            "SELECT c.chunk_id, c.payload, "
            "sum(sp.weight * q.qweight * "
            "ln((n.total - df.df + 0.5) / (df.df + 0.5) + 1)) AS score "
            f"FROM {st} sp "
            "JOIN q ON q.term_index = sp.term_index "
            "JOIN df ON df.term_index = sp.term_index "
            "CROSS JOIN n "
            f"JOIN {t} c ON c.chunk_id = sp.chunk_id "
            f"{where} "
            "GROUP BY c.chunk_id, c.payload "
            "ORDER BY score DESC, c.chunk_id LIMIT %s"
        )
        rows = await _pg.execute(conn, sql, tuple(params))
        return [(row[0], row[1], row[2]) for row in rows]

    # -- C7 permission seam -------------------------------------------------

    def permission_filter(self, principal: Principal) -> dict[str, Any]:
        """NE-043 C7 seam — the SAME mapping :class:`PrincipalAclFilter` emits.

        Returns ``{"acl": [labels]}`` where ``labels`` come from the SAME
        :func:`ne.brain.trimming.acl_match_labels` over the injected
        ``group_expander`` — compiled by the SAME :func:`compile_filters` as
        :meth:`query`/:meth:`get_filtered`, so the store-level filter and the
        retriever-hook mapping can never drift. Fail-closed guarantees (mirroring
        ``QdrantChunkStore.permission_filter``):

        * the label set ALWAYS contains ``principal.id`` — never empty, never
          ``None``, never match-all;
        * a chunk with an EMPTY/MISSING ``acl`` matches NOTHING (``@>`` over an
          empty/absent array never matches) — the never-match-all direction;
        * without an injected expander the labels fall back to the verified token
          identity (id + ``Principal.groups``); an expander exception propagates.
        """
        return {"acl": list(acl_match_labels(principal, self._group_expander))}

    # -- 015A0-AUTHORITY-CORE immutable-artifact candidates -------------------
    #
    # The Postgres pole of ``ImmutableArtifactVectorSeam``: the same two methods
    # the Qdrant adapter exposes and no others -- write one candidate, list an
    # artifact's candidates. No delete and no update, so the immutable-artifact
    # store cannot reach an unconditional write through this adapter either.
    #
    # READ THIS BEFORE CHANGING THE COLUMN TYPE. A candidate's dense values are a
    # ``double precision[]``, NOT a pgvector ``vector(dim)`` column, and
    # :meth:`_acquire_artifact_candidates` deliberately does NOT go through
    # :meth:`_ensure` -- no ``CREATE EXTENSION vector``, no HNSW, no GIN. A
    # candidate is inert staging: selection is decided by the CAS on the
    # immutable-artifact ADDRESS row in ``ne_source_immutable_artifacts``, and
    # nothing similarity-searches a candidate, so an HNSW-indexed ``vector``
    # column here would index rows no query reaches and would couple an inert
    # table to an extension it has no use for. The sparse leg's
    # ``real[]``/``bigint[]`` columns are the same shape already used here.
    #
    # Scope of what this pole claims, stated so nobody reads more into it: the
    # exact candidate payload, its enumeration, and the absence of any overwrite
    # or delete capability. It does NOT claim to exercise pgvector similarity
    # search -- the ``vector``-typed chunk path keeps its own conformance suite --
    # and it is NOT part of the promoted live gate, which stays standard
    # PostgreSQL plus the exact pinned Qdrant.

    async def _ensure_artifact_candidates(self, conn: Any) -> None:
        from ne.brain.pgvector import _pg

        await _pg.execute(
            conn,
            f"CREATE TABLE IF NOT EXISTS {self._artifact_table} ("
            "candidate_id text PRIMARY KEY, "
            "artifact_id text NOT NULL, "
            "dense double precision[] NOT NULL)",
        )
        await _pg.execute(
            conn,
            f"CREATE INDEX IF NOT EXISTS {self._artifact_table}_artifact_idx "
            f"ON {self._artifact_table} (artifact_id)",
        )

    @asynccontextmanager
    async def _acquire_artifact_candidates(self) -> AsyncIterator[Any]:
        """Yield the shared connection with ONLY the candidate table ensured.

        Deliberately not :meth:`_acquire`: that path runs ``CREATE EXTENSION
        vector`` and builds the HNSW/GIN indexes, none of which the
        immutable-artifact seam uses, and the first of which the gate's Postgres
        image cannot satisfy.
        """
        from ne.brain.pgvector import _pg

        async with self._lock:
            if self._conn is None:
                self._conn = await _pg.connect(self._dsn)
            if not self._artifact_ensured:
                await self._ensure_artifact_candidates(self._conn)
                self._artifact_ensured = True
            yield self._conn

    async def put_immutable_artifact_candidate(
        self, *, candidate_id: str, artifact_id: str, dense: Sequence[float]
    ) -> None:
        """Write one candidate row for an immutable authority artifact."""
        from ne.brain.pgvector import _pg

        vector = validated_dense_embedding(dense, dimensions=self._embedder.dimension)
        async with self._acquire_artifact_candidates() as conn:
            await _pg.execute(
                conn,
                f"INSERT INTO {self._artifact_table} (candidate_id, artifact_id, dense) "
                "VALUES (%s, %s, %s) ON CONFLICT (candidate_id) DO NOTHING",
                (candidate_id, artifact_id, list(vector)),
            )

    async def immutable_artifact_candidate_ids(self, artifact_id: str) -> tuple[str, ...]:
        """Every candidate written for ``artifact_id``, selected winner included."""
        from ne.brain.pgvector import _pg

        async with self._acquire_artifact_candidates() as conn:
            rows = await _pg.execute(
                conn,
                f"SELECT candidate_id FROM {self._artifact_table} WHERE artifact_id = %s",
                (artifact_id,),
            )
        return tuple(sorted(str(row[0]) for row in rows))

    async def close_artifact_connection(self) -> None:
        """Close the lazily-opened connection (tests and short-lived workers)."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
                self._ensured = False
                self._artifact_ensured = False


__all__ = ["PgvectorChunkStore"]
