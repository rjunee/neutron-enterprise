# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ChunkStoreContractKit against a REAL pgvector Postgres (dev-stack pg18/0.8.2).

THIS is the pgvector conformance claim (NE-091), gated byte-for-byte like
test_chunk_store_conformance_server.py. Skipped (LIVE-PARKED) when a probe
connect + ``CREATE EXTENSION IF NOT EXISTS vector`` against ``NE_PGVECTOR_DSN``
fails. To run for real:

    docker run --name ne-pgvector -e POSTGRES_PASSWORD=pgv -p 5457:5432 \
        -d pgvector/pgvector:pg18
    NE_PGVECTOR_DSN=postgresql://postgres:pgv@localhost:5457/postgres \
        uv run pytest packages/ne-brain/tests/test_pgvector_store_conformance_server.py -q

pgvector >= 0.8.2 is REQUIRED on the customer Postgres (CVE-2026-3172).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import psycopg
import pytest

from ne.brain.pgvector.store import PgvectorChunkStore
from ne.brain.sparse import FastEmbedBM25Encoder
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.testing.kits.chunk_store import ChunkStoreContractKit
from ne.contracts.knowledge import ChunkStore, Embedder
from ne.contracts.types import Chunk, Principal, SparseVector

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
    """Below-seam stub: u-ada -> {u-ada, g-eng}, everyone else -> {self}."""

    def expand(self, principal: Principal | str) -> frozenset[str]:
        pid = principal.id if isinstance(principal, Principal) else principal
        if pid == "u-ada":
            return frozenset({"u-ada", "g-eng"})
        return frozenset({pid})


class _CountingEmbedder:
    """Wraps HashEmbedder to count embed() calls (update_payload must not embed)."""

    def __init__(self, dimension: int = 32) -> None:
        self._inner = HashEmbedder(dimension)
        self.embed_calls = 0

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.embed_calls += 1
        return await self._inner.embed(texts)


class TestPgvectorChunkStoreServerMode(ChunkStoreContractKit):
    @pytest.fixture(autouse=True)
    def _inject_bm25(self, bm25_encoder: FastEmbedBM25Encoder) -> None:
        self._session_bm25 = bm25_encoder

    def create_store(self) -> ChunkStore:
        embedder = HashEmbedder(32)
        table = f"kit_{uuid.uuid4().hex[:12]}"
        store = PgvectorChunkStore(_DSN, table, embedder, self._session_bm25)
        self._embedder = embedder
        return store

    def embed_text(self, text: str) -> Sequence[float]:
        return asyncio.run(self._embedder.embed([text]))[0]

    def encode_query(self, text: str) -> SparseVector:
        return asyncio.run(self._session_bm25.encode_queries([text]))[0]


# --- pgvector-specific round-trips (beyond the shared kit) ------------------


def _new_store(embedder: Embedder, sparse: FastEmbedBM25Encoder, *, expander: object = None):
    table = f"pgx_{uuid.uuid4().hex[:12]}"
    return PgvectorChunkStore(
        _DSN,
        table,
        embedder,
        sparse,
        group_expander=expander,  # type: ignore[arg-type]
    )


def test_update_payload_changes_acl_without_re_embed(bm25_encoder: FastEmbedBM25Encoder) -> None:
    embedder = _CountingEmbedder(32)
    store = _new_store(embedder, bm25_encoder)

    async def scenario() -> None:
        original = Chunk(
            chunk_id="u1", document_id="d1", text="body text", acl=("g-old",), metadata={"k": "v"}
        )
        await store.upsert_chunks([original])
        calls_after_upsert = embedder.embed_calls
        assert calls_after_upsert >= 1

        updated = Chunk(
            chunk_id="u1", document_id="d1", text="body text", acl=("g-new",), metadata={"k": "v2"}
        )
        await store.update_payload([updated])
        # NO embed() call during update_payload (PayloadUpdatable).
        assert embedder.embed_calls == calls_after_upsert

        # The new acl is immediately filter-effective; the old one is gone.
        assert [
            c.chunk_id for c in await store.get_filtered(["u1"], filters={"acl": ["g-new"]})
        ] == ["u1"]
        assert await store.get_filtered(["u1"], filters={"acl": ["g-old"]}) == []
        # metadata was updated too.
        got = await store.get(["u1"])
        assert got[0].metadata["k"] == "v2"

    asyncio.run(scenario())


def test_get_filtered_honors_acl_and_any_and_omits(bm25_encoder: FastEmbedBM25Encoder) -> None:
    store = _new_store(HashEmbedder(32), bm25_encoder)

    async def scenario() -> None:
        shared = Chunk(chunk_id="c-shared", document_id="d-shared", text="body", acl=("u-owner",))
        labelled = Chunk(chunk_id="c-lab", document_id="d-lab", text="body", acl=("g-a",))
        hidden = Chunk(chunk_id="c-hidden", document_id="d-x", text="body", acl=("g-secret",))
        await store.upsert_chunks([shared, labelled, hidden])

        # $any OR-group: label g-a OR a share of d-shared -> both, hidden omitted.
        flt = {"$any": [{"acl": ["g-a"]}, {"document_id": ["d-shared"]}]}
        got = await store.get_filtered(["c-shared", "c-lab", "c-hidden"], filters=flt)
        assert {c.chunk_id for c in got} == {"c-shared", "c-lab"}

        # Plain acl filter omits everything the principal cannot see.
        only_a = await store.get_filtered(
            ["c-shared", "c-lab", "c-hidden"], filters={"acl": ["g-a"]}
        )
        assert {c.chunk_id for c in only_a} == {"c-lab"}

    asyncio.run(scenario())


def test_permission_filter_end_to_end(bm25_encoder: FastEmbedBM25Encoder) -> None:
    store = _new_store(HashEmbedder(32), bm25_encoder, expander=_StubExpander())

    async def scenario() -> None:
        ada = Chunk(chunk_id="c-ada", document_id="d", text="alpha", acl=("u-ada",))
        eng = Chunk(chunk_id="c-eng", document_id="d", text="alpha", acl=("g-eng",))
        empty = Chunk(chunk_id="c-empty", document_id="d", text="alpha", acl=())
        other = Chunk(chunk_id="c-other", document_id="d", text="alpha", acl=("u-other",))
        all_ids = ["c-ada", "c-eng", "c-empty", "c-other"]
        await store.upsert_chunks([ada, eng, empty, other])

        # u-ada -> {u-ada, g-eng}: sees own-id + expanded-group chunks only.
        flt_ada = store.permission_filter(Principal(id="u-ada", groups=()))
        seen = await store.get_filtered(all_ids, filters=flt_ada)
        assert {c.chunk_id for c in seen} == {"c-ada", "c-eng"}

        # empty-ACL chunk is invisible to everyone.
        for pid, groups in [("u-ada", ()), ("u-other", ()), ("u-ghost", ("g-eng",))]:
            flt = store.permission_filter(Principal(id=pid, groups=groups))
            visible = {c.chunk_id for c in await store.get_filtered(all_ids, filters=flt)}
            assert "c-empty" not in visible

        # u-other isolation: sees only its own chunk.
        flt_other = store.permission_filter(Principal(id="u-other", groups=()))
        assert {c.chunk_id for c in await store.get_filtered(all_ids, filters=flt_other)} == {
            "c-other"
        }

    asyncio.run(scenario())


def test_permission_filter_without_expander_token_identity(
    bm25_encoder: FastEmbedBM25Encoder,
) -> None:
    store = _new_store(HashEmbedder(32), bm25_encoder)  # no expander

    async def scenario() -> None:
        ada = Chunk(chunk_id="c-ada", document_id="d", text="alpha", acl=("u-ada",))
        eng = Chunk(chunk_id="c-eng", document_id="d", text="alpha", acl=("g-eng",))
        await store.upsert_chunks([ada, eng])
        # Token identity only: u-ada with no groups sees only its own chunk.
        flt = store.permission_filter(Principal(id="u-ada", groups=()))
        assert {c.chunk_id for c in await store.get_filtered(["c-ada", "c-eng"], filters=flt)} == {
            "c-ada"
        }
        # A verified token group widens to that group.
        flt2 = store.permission_filter(Principal(id="u-x", groups=("g-eng",)))
        assert {c.chunk_id for c in await store.get_filtered(["c-ada", "c-eng"], filters=flt2)} == {
            "c-eng"
        }

    asyncio.run(scenario())


def test_scan_filtered_against_a_real_pgvector(bm25_encoder: FastEmbedBM25Encoder) -> None:
    """NE-S5-INTERNAL-CRM: the filter-only scan the internal CRM lookup resolves on.

    The always-on twin
    (``packages/ne-brain/tests/test_scan_filtered_real_stores.py``) executes this
    exact method with only the psycopg wire intercepted — it pins the SQL text,
    the parameter order and the bound ``LIMIT``. This one runs that SQL against a
    real pgvector, so the ``jsonb @>`` semantics, the ``ORDER BY chunk_id``
    determinism and the bounded truncation contract are proven end to end.
    """
    store = _new_store(HashEmbedder(32), bm25_encoder)

    async def scenario() -> None:
        rows = [
            Chunk(
                chunk_id=f"sf-{i}",
                document_id=f"d-{i}",
                text=f"Person {i}",
                metadata={"connector": "salesforce", "object": "Contact"},
                acl=("ops",) if i % 2 == 0 else ("legal",),
            )
            for i in range(5)
        ]
        rows.append(
            Chunk(
                chunk_id="foreign",
                document_id="d-foreign",
                text="Person 0",
                # Same sObject shape, DIFFERENT connector: the pin must exclude it.
                metadata={"connector": "m365", "object": "Contact"},
                acl=("ops",),
            )
        )
        await store.upsert_chunks(rows)

        pinned = {"metadata.connector": "salesforce", "metadata.object": "Contact"}
        got = await store.scan_filtered(filters=pinned, limit=64)
        assert [c.chunk_id for c in got] == [f"sf-{i}" for i in range(5)]  # ORDER BY chunk_id

        # Bounded: exactly `limit` back is the caller's truncation signal.
        assert len(await store.scan_filtered(filters=pinned, limit=3)) == 3

        # An array-valued payload field matches by CONTAINS, not whole-value.
        acl_scoped = await store.scan_filtered(filters={**pinned, "acl": ["ops"]}, limit=64)
        assert {c.chunk_id for c in acl_scoped} == {"sf-0", "sf-2", "sf-4"}

        # Omitting the ACL term applies NO trim (deliberate: the internal CRM
        # lookup mirrors a single-integration-identity client) — but the trim is
        # available, so a caller that omits it is choosing the exposure.
        assert len(got) > len(acl_scoped)

        # Fail closed.
        with pytest.raises(ValueError, match="non-empty filter"):
            await store.scan_filtered(filters={}, limit=8)
        with pytest.raises(ValueError, match="limit >= 1"):
            await store.scan_filtered(filters=pinned, limit=0)

    asyncio.run(scenario())
