# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""⭐ SWAP-PARITY: Qdrant (':memory:') vs LIVE pgvector — the NE-091 proof.

The SAME corpus + queries + encoders run through BOTH backends. Assertions (all
HARD failures — never lower a floor; if one fails honestly, STOP and flag the
semantic gap, the NE-092 discipline):

1. SINGLE-LEG EXACT PARITY — ordered top-10 chunk_id LISTS are IDENTICAL between
   backends for dense-only and sparse-only queries (dense = cosine both sides;
   sparse = the SAME IDF formula — divergence is a bug, not tolerance).
2. HYBRID TOLERANCE — per-query top-10 id-set overlap >= 0.8, mean >= 0.9 (RRF
   tie-order is the only legitimate divergence).
3. C7 ACL-TRIM IDENTITY — through the REAL HybridRetriever + REAL
   PrincipalAclFilter(_StubExpander) over EACH store: search() AND retrieve() id
   SETS are IDENTICAL across backends AND equal the acl oracle, for every
   principal.
4. HIDDEN-CHUNK NON-LEAK — the planted g-secret chunk and every empty-acl chunk
   leak in NEITHER backend, for NO principal.
5. $any PARITY — a ReBAC-shape ``$any`` filter returns IDENTICAL id sets in both;
   a caller-supplied ``$``-key raises ValueError in BOTH.

Qdrant runs in-process (always available); pgvector is live-gated on
NE_PGVECTOR_DSN (LIVE-PARKED skip otherwise — same probe as the conformance test).
"""

from __future__ import annotations

import asyncio
import statistics
import uuid

import psycopg
import pytest
from qdrant_client import AsyncQdrantClient

from ne.brain.pgvector.store import PgvectorChunkStore
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.retriever import HybridRetriever
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.trimming import PrincipalAclFilter
from ne.contracts.types import Chunk, Principal

_DEFAULT_DSN = "postgresql://postgres:pgv@localhost:5457/postgres"


def _pgvector_dsn() -> str:
    import os

    return os.environ.get("NE_PGVECTOR_DSN", _DEFAULT_DSN)


def _pgvector_ready(dsn: str) -> bool:
    try:
        with (
            psycopg.connect(dsn, connect_timeout=2, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return True
    except Exception:
        return False


_DSN = _pgvector_dsn()

if not _pgvector_ready(_DSN):
    pytest.skip(
        "LIVE-PARKED: no reachable pgvector Postgres at NE_PGVECTOR_DSN "
        f"(NE_PGVECTOR_DSN={_DSN}); start "
        "`docker run --name ne-pgvector -e POSTGRES_PASSWORD=pgv -p 5457:5432 "
        "-d pgvector/pgvector:pg18` and set "
        "NE_PGVECTOR_DSN=postgresql://postgres:pgv@localhost:5457/postgres",
        allow_module_level=True,
    )


class _StubExpander:
    """Below-seam stub: u-ada -> {u-ada, g-eng}, u-bob -> {u-bob}, else -> {self}."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        if pid == "u-ada":
            return frozenset({"u-ada", "g-eng"})
        return frozenset({pid})


# --- deterministic corpus: 24 chunks over 4 documents ----------------------
# Distinct, well-separated texts so single-leg scores never tie in the top-10.
_SUBJECTS = [
    "quarterly revenue report",
    "annual budget planning cycle",
    "the zzyzxplorb subsystem reconciliation",
    "machine learning embedding retrieval",
    "distributed consensus protocol design",
    "customer churn analysis dashboard",
    "security incident response runbook",
    "payroll processing schedule notes",
    "warehouse inventory turnover metrics",
    "marketing attribution funnel model",
    "kubernetes rollout canary strategy",
    "database index vacuum maintenance",
    "supplier contract renewal terms",
    "employee onboarding checklist flow",
    "network egress firewall policy",
    "encryption key rotation ledger",
    "graph traversal shortest path",
    "sparse vector inverted index",
    "cosine similarity ranking scores",
    "reciprocal rank fusion weights",
    "postgres logical replication slot",
    "vector extension hnsw parameters",
    "audit chain hash verification",
    "principal group expansion labels",
]
# One planted HIDDEN chunk whose text strongly matches every query family. Each
# keyword is repeated so the hidden chunk's per-term weight is distinctly > 1 and
# never ties (on an equal single-token score) with a subject chunk — keeping
# single-leg scores well-separated so exact-rank parity is a clean bug detector,
# while still ranking the hidden chunk HIGH (a strong non-leak proof once trimmed).
_HIDDEN_BASE = (
    "quarterly revenue machine learning consensus security payroll inventory "
    "kubernetes database vector cosine reciprocal postgres audit principal churn"
)
_HIDDEN_TEXT = " ".join([_HIDDEN_BASE] * 3)  # each keyword appears 3x (count weight 3)

_ACL_CYCLE: list[tuple[str, ...]] = [
    ("u-ada",),
    ("g-eng",),
    ("u-bob",),
    ("g-eng", "u-bob"),
    (),
]


def _corpus() -> list[Chunk]:
    chunks: list[Chunk] = []
    for i, subject in enumerate(_SUBJECTS):
        chunks.append(
            Chunk(
                chunk_id=f"c{i:02d}",
                document_id=f"d{i % 4}",
                text=subject,
                acl=_ACL_CYCLE[i % len(_ACL_CYCLE)],
            )
        )
    chunks.append(
        Chunk(chunk_id="c-hidden", document_id="d0", text=_HIDDEN_TEXT, acl=("g-secret",))
    )
    return chunks


_CORPUS = _corpus()
_EMBEDDER = HashEmbedder(32)
_SPARSE = HashSparseEncoder()


async def _build_pg() -> PgvectorChunkStore:
    store = PgvectorChunkStore(
        _DSN, f"parity_{uuid.uuid4().hex[:12]}", _EMBEDDER, _SPARSE, group_expander=_StubExpander()
    )
    await store.upsert_chunks(_CORPUS)
    return store


async def _build_qdrant() -> QdrantChunkStore:
    store = QdrantChunkStore(
        AsyncQdrantClient(":memory:"),
        f"parity_{uuid.uuid4().hex}",
        _EMBEDDER,
        _SPARSE,
        group_expander=_StubExpander(),
    )
    await store.ensure_collection()
    await store.upsert_chunks(_CORPUS)
    return store


def _oracle(principal: Principal) -> set[str]:
    labels = set(_StubExpander().expand(principal)) | {principal.id}
    return {c.chunk_id for c in _CORPUS if set(c.acl) & labels}


# --- 1. single-leg exact rank parity ---------------------------------------


def test_single_leg_exact_rank_parity() -> None:
    dense_queries = _SUBJECTS[:6]
    sparse_queries = _SUBJECTS[6:12]

    async def scenario() -> None:
        pg = await _build_pg()
        qd = await _build_qdrant()
        for q in dense_queries:
            dv = (await _EMBEDDER.embed([q]))[0]
            pg_ids = [sc.chunk.chunk_id for sc in await pg.query(dense=dv, top_k=10)]
            qd_ids = [sc.chunk.chunk_id for sc in await qd.query(dense=dv, top_k=10)]
            assert pg_ids == qd_ids, f"DENSE divergence q={q!r}: pgvector={pg_ids} qdrant={qd_ids}"
        for q in sparse_queries:
            sv = (await _SPARSE.encode_queries([q]))[0]
            pg_ids = [sc.chunk.chunk_id for sc in await pg.query(sparse=sv, top_k=10)]
            qd_ids = [sc.chunk.chunk_id for sc in await qd.query(sparse=sv, top_k=10)]
            assert pg_ids == qd_ids, f"SPARSE divergence q={q!r}: pgvector={pg_ids} qdrant={qd_ids}"

    asyncio.run(scenario())


# --- 2. hybrid tolerance ----------------------------------------------------


def test_hybrid_top_k_overlap_within_tolerance() -> None:
    queries = _SUBJECTS[:8]

    async def scenario() -> None:
        pg = await _build_pg()
        qd = await _build_qdrant()
        overlaps: list[float] = []
        for q in queries:
            dv = (await _EMBEDDER.embed([q]))[0]
            sv = (await _SPARSE.encode_queries([q]))[0]
            pg_ids = {sc.chunk.chunk_id for sc in await pg.query(dense=dv, sparse=sv, top_k=10)}
            qd_ids = {sc.chunk.chunk_id for sc in await qd.query(dense=dv, sparse=sv, top_k=10)}
            overlap = len(pg_ids & qd_ids) / len(pg_ids | qd_ids)
            assert overlap >= 0.8, (
                f"HYBRID overlap {overlap:.2f} < 0.8 q={q!r} pg={pg_ids} qd={qd_ids}"
            )
            overlaps.append(overlap)
        assert statistics.mean(overlaps) >= 0.9, (
            f"mean hybrid overlap {statistics.mean(overlaps):.3f} < 0.9"
        )

    asyncio.run(scenario())


# --- 3 + 4. C7 ACL-trim identity + hidden-chunk non-leak --------------------


_PRINCIPALS = [
    Principal(id="u-ada", groups=()),
    Principal(id="u-bob", groups=()),
    Principal(id="u-stranger", groups=()),
]


def _retriever(store: object) -> HybridRetriever:
    return HybridRetriever(
        store,  # type: ignore[arg-type]
        _EMBEDDER,
        _SPARSE,
        acl_filters=PrincipalAclFilter(_StubExpander()),
        audit_sink=None,
    )


def test_acl_trim_identity_and_hidden_chunk_non_leak() -> None:
    all_ids = [c.chunk_id for c in _CORPUS]
    k = len(_CORPUS)

    async def scenario() -> None:
        pg = await _build_pg()
        qd = await _build_qdrant()
        pg_r = _retriever(pg)
        qd_r = _retriever(qd)
        for principal in _PRINCIPALS:
            oracle = _oracle(principal)
            # search() — SET identity across backends AND equal the oracle.
            pg_search = {
                sc.chunk.chunk_id
                for sc in (await pg_r.search("revenue", principal=principal, top_k=k)).chunks
            }
            qd_search = {
                sc.chunk.chunk_id
                for sc in (await qd_r.search("revenue", principal=principal, top_k=k)).chunks
            }
            assert pg_search == qd_search, (
                f"search() backend divergence principal={principal.id}: "
                f"pgvector-only={pg_search - qd_search} qdrant-only={qd_search - pg_search}"
            )
            assert pg_search == oracle, (
                f"search() principal={principal.id} != oracle: "
                f"extra={pg_search - oracle} missing={oracle - pg_search}"
            )
            # retrieve() — likewise identical + oracle-equal.
            pg_ret = {
                sc.chunk.chunk_id
                for sc in (await pg_r.retrieve(all_ids, principal=principal)).chunks
            }
            qd_ret = {
                sc.chunk.chunk_id
                for sc in (await qd_r.retrieve(all_ids, principal=principal)).chunks
            }
            assert pg_ret == qd_ret, (
                f"retrieve() backend divergence principal={principal.id}: "
                f"pgvector-only={pg_ret - qd_ret} qdrant-only={qd_ret - pg_ret}"
            )
            assert pg_ret == oracle, (
                f"retrieve() principal={principal.id} != oracle: "
                f"extra={pg_ret - oracle} missing={oracle - pg_ret}"
            )
            # HIDDEN / empty-acl non-leak in BOTH backends.
            empty_ids = {c.chunk_id for c in _CORPUS if not c.acl}
            for leaked, where in [
                (pg_search, "pgvector search"),
                (qd_search, "qdrant search"),
                (pg_ret, "pgvector retrieve"),
                (qd_ret, "qdrant retrieve"),
            ]:
                assert "c-hidden" not in leaked, f"g-secret leaked in {where} for {principal.id}"
                assert not (leaked & empty_ids), f"empty-acl leaked in {where} for {principal.id}"

    asyncio.run(scenario())


# --- 5. $any ReBAC-shape parity + caller $-key rejection --------------------


def test_any_filter_parity_and_caller_dollar_key_rejected() -> None:
    async def scenario() -> None:
        pg = await _build_pg()
        qd = await _build_qdrant()
        dv = (await _EMBEDDER.embed(["revenue"]))[0]
        # ReBAC shape: chunks labelled g-eng OR belonging to document d1.
        flt = {"$any": [{"acl": ["g-eng"]}, {"document_id": ["d1"]}]}
        pg_ids = {
            sc.chunk.chunk_id for sc in await pg.query(dense=dv, top_k=len(_CORPUS), filters=flt)
        }
        qd_ids = {
            sc.chunk.chunk_id for sc in await qd.query(dense=dv, top_k=len(_CORPUS), filters=flt)
        }
        assert pg_ids == qd_ids, (
            f"$any parity divergence: pgvector-only={pg_ids - qd_ids} qdrant-only={qd_ids - pg_ids}"
        )
        oracle = {c.chunk_id for c in _CORPUS if ("g-eng" in c.acl) or (c.document_id == "d1")}
        assert pg_ids == oracle, (
            f"$any != oracle: extra={pg_ids - oracle} missing={oracle - pg_ids}"
        )

        # A caller-supplied reserved key is rejected by BOTH stores.
        bad = {"$foo": [{"acl": ["g-eng"]}]}
        with pytest.raises(ValueError):
            await pg.query(dense=dv, top_k=5, filters=bad)
        with pytest.raises(ValueError):
            await qd.query(dense=dv, top_k=5, filters=bad)

    asyncio.run(scenario())
