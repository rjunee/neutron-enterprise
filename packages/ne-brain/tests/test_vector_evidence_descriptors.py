# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Safe vector-store evidence descriptor proofs."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from math import nan
from typing import Any, cast

import pytest
from qdrant_client import models

from ne.brain.evidence import (
    VectorEvidenceWriteReceipt,
    VectorEvidenceWriteSource,
    validate_vector_evidence_receipts,
)
from ne.brain.pgvector import _pg
from ne.brain.pgvector.store import PgvectorChunkStore
from ne.brain.qdrant import QdrantChunkStore, QdrantWriteCompensationError, point_id_for
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.contracts.types import Chunk


class _FixedEmbedder:
    model_id = "test/fixed-v1"

    def __init__(self, vectors: list[list[float]], *, dimension: int = 10) -> None:
        self.dimension = dimension
        self._vectors = vectors

    async def embed(self, texts):  # type: ignore[no-untyped-def]
        assert len(texts) == len(self._vectors)
        return self._vectors


class _QdrantClient:
    def __init__(
        self,
        *,
        fail_on_upsert_call: int | None = None,
        apply_before_failure: bool = False,
        fail_delete: bool = False,
        corrupt_verification_read: bool = False,
    ) -> None:
        self.fail_on_upsert_call = fail_on_upsert_call
        self.apply_before_failure = apply_before_failure
        self.fail_delete = fail_delete
        self.corrupt_verification_read = corrupt_verification_read
        self.acknowledged = False
        self.upsert_calls = 0
        self.points: dict[object, models.PointStruct] = {}

    async def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[object] | tuple[object, ...],
        with_payload: bool,
        with_vectors: bool,
        consistency: models.ReadConsistencyType,
    ) -> list[models.Record]:
        assert collection_name == "private-customer-collection"
        assert with_payload is True
        assert with_vectors is True
        assert consistency is models.ReadConsistencyType.ALL
        records = [
            models.Record(
                id=cast(Any, self.points[point_id].id),
                payload=deepcopy(self.points[point_id].payload),
                vector=cast(Any, deepcopy(self.points[point_id].vector)),
            )
            for point_id in ids
            if point_id in self.points
        ]
        if self.corrupt_verification_read and self.upsert_calls >= 2 and records:
            records[0].payload = {"corrupted": True}
        return records

    async def upsert(
        self,
        *,
        collection_name: str,
        points: list[models.PointStruct] | tuple[models.PointStruct, ...],
        wait: bool,
        ordering: models.WriteOrdering,
    ) -> None:
        assert collection_name == "private-customer-collection"
        assert wait is True
        assert ordering is models.WriteOrdering.STRONG
        self.upsert_calls += 1
        should_fail = self.fail_on_upsert_call == self.upsert_calls
        if should_fail and not self.apply_before_failure:
            raise RuntimeError("qdrant unavailable")
        for point in points:
            self.points[point.id] = deepcopy(point)
        if should_fail:
            raise RuntimeError("qdrant unavailable")
        self.acknowledged = True

    async def delete(
        self,
        *,
        collection_name: str,
        points_selector: models.PointIdsList,
        wait: bool,
        ordering: models.WriteOrdering,
    ) -> None:
        assert collection_name == "private-customer-collection"
        assert wait is True
        assert ordering is models.WriteOrdering.STRONG
        if self.fail_delete:
            raise RuntimeError("qdrant compensation delete unavailable")
        for point_id in points_selector.points:
            self.points.pop(point_id, None)


class _Transaction:
    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        self.committed = exc_type is None


class _PgConnection:
    def __init__(self) -> None:
        self.transactions: list[_Transaction] = []

    def transaction(self) -> _Transaction:
        transaction = _Transaction()
        self.transactions.append(transaction)
        return transaction


def _chunks() -> list[Chunk]:
    return [
        Chunk(chunk_id="chunk-a", document_id="doc-a", text="alpha", acl=("ops",)),
        Chunk(chunk_id="chunk-b", document_id="doc-b", text="bravo", acl=("ops",)),
    ]


def _vectors() -> list[list[float]]:
    return [
        [index + 0.125 for index in range(10)],
        [-(index + 0.25) for index in range(10)],
    ]


def _point_snapshot(point: models.PointStruct) -> tuple[object, object]:
    return deepcopy(point.payload), deepcopy(point.vector)


def test_real_store_descriptors_are_stable_opaque_and_adapter_specific() -> None:
    embedder = HashEmbedder(19)
    sparse = HashSparseEncoder()
    qdrant = QdrantChunkStore(
        cast(Any, object()),
        "private-customer-collection",
        embedder,
        sparse,
    )
    pgvector = PgvectorChunkStore(
        "postgresql://secret.example/runtime",
        "private_customer_table",
        embedder,
        sparse,
    )

    first = qdrant.vector_evidence_descriptor()
    repeated = qdrant.vector_evidence_descriptor()
    alternate = pgvector.vector_evidence_descriptor()

    assert first == repeated
    assert first.model_id == "ne-test-hash-v1"
    assert first.dimensions == 19
    assert first.index_reference == f"sha256:{first.index_fingerprint}"
    assert first.index_fingerprint != alternate.index_fingerprint
    projected = repr((first, alternate))
    for forbidden in (
        "private-customer-collection",
        "private_customer_table",
        "postgresql://",
        "secret.example",
    ):
        assert forbidden not in projected


def test_descriptor_fingerprint_changes_with_logical_index_or_dimension() -> None:
    sparse = HashSparseEncoder()
    first = QdrantChunkStore(
        cast(Any, object()), "index-a", HashEmbedder(8), sparse
    ).vector_evidence_descriptor()
    other_index = QdrantChunkStore(
        cast(Any, object()), "index-b", HashEmbedder(8), sparse
    ).vector_evidence_descriptor()
    other_dimension = QdrantChunkStore(
        cast(Any, object()), "index-a", HashEmbedder(16), sparse
    ).vector_evidence_descriptor()

    assert (
        len(
            {
                first.index_fingerprint,
                other_index.index_fingerprint,
                other_dimension.index_fingerprint,
            }
        )
        == 3
    )


def test_real_stores_capture_the_exact_bounded_dense_prefix_after_acknowledged_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        vectors = _vectors()
        sparse = HashSparseEncoder()

        qdrant_client = _QdrantClient()
        qdrant = QdrantChunkStore(
            cast(Any, qdrant_client),
            "private-customer-collection",
            _FixedEmbedder(vectors),
            sparse,
        )
        assert isinstance(qdrant, VectorEvidenceWriteSource)
        qdrant_evidence = await qdrant.upsert_chunks_with_evidence(_chunks())
        assert qdrant_client.acknowledged is True
        first_point = qdrant_client.points[point_id_for("chunk-a")]
        assert cast(Any, first_point.vector)["dense"] == vectors[0]

        pgvector = PgvectorChunkStore(
            "postgresql://secret.example/runtime",
            "private_customer_table",
            _FixedEmbedder(vectors),
            sparse,
        )
        connection = _PgConnection()

        @asynccontextmanager
        async def acquire():
            yield connection

        pgvector._acquire = acquire  # type: ignore[method-assign]

        pg_rows: dict[str, tuple[str, str]] = {}

        async def execute(conn: object, query: str, params: object = None):  # type: ignore[no-untyped-def]
            if "SELECT chunk_id, payload, dense::text" in query:
                ids = cast(tuple[list[str]], params)[0]
                return [
                    (chunk_id, json.loads(pg_rows[chunk_id][0]), pg_rows[chunk_id][1])
                    for chunk_id in ids
                    if chunk_id in pg_rows
                ]
            if "SELECT chunk_id, term_index, weight" in query:
                return []
            return None

        async def executemany(conn: object, query: str, rows: list[tuple[Any, ...]]) -> None:
            if "INSERT INTO private_customer_table (" in query:
                for chunk_id, payload, dense in rows:
                    pg_rows[chunk_id] = (payload, dense)

        monkeypatch.setattr(_pg, "execute", execute)
        monkeypatch.setattr(_pg, "executemany", executemany)
        assert isinstance(pgvector, VectorEvidenceWriteSource)
        pgvector_evidence = await pgvector.upsert_chunks_with_evidence(_chunks())
        assert connection.transactions[-1].committed is True

        expected = (tuple(vectors[0][:8]), tuple(vectors[1][:8]))
        assert tuple(item.chunk_id for item in qdrant_evidence) == ("chunk-a", "chunk-b")
        assert tuple(item.chunk_id for item in pgvector_evidence) == ("chunk-a", "chunk-b")
        assert tuple(item.descriptor.numeric_preview for item in qdrant_evidence) == expected
        assert tuple(item.descriptor.numeric_preview for item in pgvector_evidence) == expected
        expected_by_id: dict[str, list[float]] = dict(
            zip(("chunk-a", "chunk-b"), vectors, strict=True)
        )
        for receipt in qdrant_evidence:
            point = qdrant_client.points[point_id_for(receipt.chunk_id)]
            assert cast(Any, point.vector)["dense"] == expected_by_id[receipt.chunk_id]
            assert receipt.descriptor.numeric_preview == tuple(expected_by_id[receipt.chunk_id][:8])
        assert all(item.descriptor.dimensions == 10 for item in qdrant_evidence + pgvector_evidence)
        assert all(len(item.descriptor.numeric_preview or ()) <= 8 for item in qdrant_evidence)
        assert len(connection.transactions) == 1

        short_client = _QdrantClient()
        short = QdrantChunkStore(
            cast(Any, short_client),
            "private-customer-collection",
            _FixedEmbedder([[1.0, 2.0, 3.0]], dimension=3),
            sparse,
        )
        short_evidence = await short.upsert_chunks_with_evidence(_chunks()[:1])
        assert short_evidence[0].chunk_id == "chunk-a"
        assert short_evidence[0].descriptor.numeric_preview == (1.0, 2.0, 3.0)

    asyncio.run(scenario())


def test_frozen_chunk_store_entrypoint_delegates_to_optional_evidence_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        stores = (
            QdrantChunkStore(
                cast(Any, object()),
                "private-customer-collection",
                HashEmbedder(8),
                HashSparseEncoder(),
            ),
            PgvectorChunkStore(
                "postgresql://secret.example/runtime",
                "private_customer_table",
                HashEmbedder(8),
                HashSparseEncoder(),
            ),
        )
        delegated: list[tuple[str, ...]] = []

        for store in stores:

            async def capture(chunks: list[Chunk]) -> tuple[()]:
                delegated.append(tuple(chunk.chunk_id for chunk in chunks))
                return ()

            monkeypatch.setattr(store, "upsert_chunks_with_evidence", capture)
            assert await store.upsert_chunks(_chunks()) is None

        assert delegated == [("chunk-a", "chunk-b"), ("chunk-a", "chunk-b")]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "vectors, message",
    [
        ([[0.0] * 9, [0.0] * 10], "dimension"),
        ([[nan] + [0.0] * 9, [0.0] * 10], "finite"),
        ([[True] + [0.0] * 9, [0.0] * 10], "finite"),
        ([["0.0"] + [0.0] * 9, [0.0] * 10], "finite"),
    ],
)
def test_qdrant_rejects_malformed_dense_output_before_any_upsert(
    vectors: object, message: str
) -> None:
    async def scenario() -> None:
        client = _QdrantClient()
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(cast(Any, vectors)),
            HashSparseEncoder(),
        )
        with pytest.raises(ValueError, match=message):
            await store.upsert_chunks_with_evidence(_chunks())
        assert client.acknowledged is False
        assert client.points == {}

    asyncio.run(scenario())


def test_failed_upsert_never_returns_submitted_embedding_evidence() -> None:
    async def scenario() -> None:
        client = _QdrantClient(fail_on_upsert_call=1)
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )
        with pytest.raises(RuntimeError, match="qdrant unavailable"):
            await store.upsert_chunks_with_evidence(_chunks())
        assert client.acknowledged is False

    asyncio.run(scenario())


def test_receipt_validation_is_identity_bound_not_position_bound() -> None:
    descriptor = QdrantChunkStore(
        cast(Any, object()),
        "private-customer-collection",
        HashEmbedder(8),
        HashSparseEncoder(),
    ).vector_evidence_descriptor()
    first = VectorEvidenceWriteReceipt(chunk_id="chunk-a", descriptor=descriptor)
    second = VectorEvidenceWriteReceipt(chunk_id="chunk-b", descriptor=descriptor)

    assert validate_vector_evidence_receipts((second, first), ("chunk-a", "chunk-b")) == (
        second,
        first,
    )
    with pytest.raises(ValueError, match="duplicate"):
        validate_vector_evidence_receipts((first, first), ("chunk-a", "chunk-b"))
    with pytest.raises(ValueError, match="do not match"):
        validate_vector_evidence_receipts((first,), ("chunk-a", "chunk-b"))
    with pytest.raises(ValueError, match="distinct"):
        validate_vector_evidence_receipts((first, second), ("chunk-a", "chunk-a"))


def test_pgvector_uses_one_transaction_and_binds_each_sql_vector_to_its_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        vectors = _vectors()
        store = PgvectorChunkStore(
            "postgresql://secret.example/runtime",
            "private_customer_table",
            _FixedEmbedder(vectors),
            HashSparseEncoder(),
        )
        connection = _PgConnection()

        @asynccontextmanager
        async def acquire():
            yield connection

        store._acquire = acquire  # type: ignore[method-assign]
        main_rows: list[tuple[str, str, str]] = []
        pg_rows: dict[str, tuple[str, str]] = {}

        async def execute(conn: object, query: str, params: object = None):  # type: ignore[no-untyped-def]
            if "SELECT chunk_id, payload, dense::text" in query:
                ids = cast(tuple[list[str]], params)[0]
                return [
                    (chunk_id, json.loads(pg_rows[chunk_id][0]), pg_rows[chunk_id][1])
                    for chunk_id in ids
                    if chunk_id in pg_rows
                ]
            if "SELECT chunk_id, term_index, weight" in query:
                return []
            return None

        async def executemany(conn: object, query: str, rows: list[tuple[Any, ...]]) -> None:
            if "INSERT INTO private_customer_table (" in query:
                main_rows.extend(cast(Any, rows))
                for chunk_id, payload, dense in rows:
                    pg_rows[chunk_id] = (payload, dense)

        monkeypatch.setattr(_pg, "execute", execute)
        monkeypatch.setattr(_pg, "executemany", executemany)

        receipts = await store.upsert_chunks_with_evidence(_chunks())

        assert len(connection.transactions) == 1
        assert connection.transactions[0].committed is True
        row_by_id = {row[0]: row for row in main_rows}
        assert set(row_by_id) == {"chunk-a", "chunk-b"}
        expected_by_id: dict[str, list[float]] = dict(
            zip(("chunk-a", "chunk-b"), vectors, strict=True)
        )
        for receipt in receipts:
            vector = expected_by_id[receipt.chunk_id]
            assert row_by_id[receipt.chunk_id][2] == "[" + ",".join(map(str, vector)) + "]"
            assert receipt.descriptor.numeric_preview == tuple(vector[:8])

    asyncio.run(scenario())


def test_pgvector_failure_in_a_later_batch_rolls_back_the_one_outer_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        chunks = [
            Chunk(
                chunk_id=f"chunk-{index}",
                document_id="doc",
                text=f"text-{index}",
                acl=("ops",),
            )
            for index in range(65)
        ]
        vectors = [[float(index)] * 10 for index in range(65)]
        store = PgvectorChunkStore(
            "postgresql://secret.example/runtime",
            "private_customer_table",
            _FixedEmbedder(vectors),
            HashSparseEncoder(),
        )
        connection = _PgConnection()

        @asynccontextmanager
        async def acquire():
            yield connection

        store._acquire = acquire  # type: ignore[method-assign]
        main_batch = 0

        async def execute(*args: object, **kwargs: object) -> None:
            return None

        async def executemany(conn: object, query: str, rows: list[tuple[Any, ...]]) -> None:
            nonlocal main_batch
            if "INSERT INTO private_customer_table (" in query:
                main_batch += 1
                if main_batch == 2:
                    raise RuntimeError("second batch failed")

        monkeypatch.setattr(_pg, "execute", execute)
        monkeypatch.setattr(_pg, "executemany", executemany)

        with pytest.raises(RuntimeError, match="second batch failed"):
            await store.upsert_chunks_with_evidence(chunks)
        assert len(connection.transactions) == 1
        assert connection.transactions[0].committed is False

    asyncio.run(scenario())


def test_qdrant_ambiguous_batch_failure_restores_raw_overwrite_and_removes_new_point() -> None:
    async def scenario() -> None:
        client = _QdrantClient(fail_on_upsert_call=1, apply_before_failure=True)
        old_point = models.PointStruct(
            id=point_id_for("chunk-a"),
            payload={"raw": {"nested": ["preimage"]}},
            vector={
                "dense": [91.25, -7.5],
                "sparse": models.SparseVector(indices=[9, 2], values=[0.25, 4.5]),
            },
        )
        client.points[old_point.id] = deepcopy(old_point)
        expected = _point_snapshot(old_point)
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )

        with pytest.raises(RuntimeError, match="qdrant unavailable"):
            await store.upsert_chunks_with_evidence(_chunks())

        assert set(client.points) == {point_id_for("chunk-a")}
        assert _point_snapshot(client.points[point_id_for("chunk-a")]) == expected
        assert client.upsert_calls == 2  # ambiguous write plus exact preimage restore

    asyncio.run(scenario())


def test_qdrant_compensation_failure_is_reported_as_stronger_uncertain_failure() -> None:
    async def scenario() -> None:
        client = _QdrantClient(
            fail_on_upsert_call=1,
            apply_before_failure=True,
            fail_delete=True,
        )
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )
        with pytest.raises(QdrantWriteCompensationError, match="could not be restored"):
            await store.upsert_chunks_with_evidence(_chunks())

    asyncio.run(scenario())


def test_qdrant_compensation_verification_mismatch_is_stronger_uncertain_failure() -> None:
    async def scenario() -> None:
        client = _QdrantClient(
            fail_on_upsert_call=1,
            apply_before_failure=True,
            corrupt_verification_read=True,
        )
        old_point = models.PointStruct(
            id=point_id_for("chunk-a"),
            payload={"old": True},
            vector={
                "dense": [1.0, 2.0],
                "sparse": models.SparseVector(indices=[7], values=[0.5]),
            },
        )
        client.points[old_point.id] = old_point
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )
        with pytest.raises(QdrantWriteCompensationError, match="could not be restored"):
            await store.upsert_chunks_with_evidence(_chunks())

    asyncio.run(scenario())


def test_qdrant_settlement_keeps_audited_and_restores_exact_overwrite_and_new_ids() -> None:
    async def scenario() -> None:
        client = _QdrantClient()
        old = models.PointStruct(
            id=point_id_for("chunk-a"),
            payload={"raw": {"acl": ["old"]}},
            vector={
                "dense": [99.25, -3.5],
                "sparse": models.SparseVector(indices=[8, 1], values=[0.75, 2.25]),
            },
        )
        client.points[old.id] = deepcopy(old)
        old_snapshot = _point_snapshot(old)
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )

        outcome = await store.upsert_chunks_with_settlement(_chunks())
        assert {receipt.chunk_id for receipt in outcome.receipts} == {"chunk-a", "chunk-b"}
        await store.settle_vector_write(
            outcome.settlement,
            keep_chunk_ids=("chunk-b",),
            restore_chunk_ids=("chunk-a",),
        )

        assert _point_snapshot(client.points[point_id_for("chunk-a")]) == old_snapshot
        assert point_id_for("chunk-b") in client.points
        with pytest.raises(ValueError, match="already consumed"):
            await store.settle_vector_write(
                outcome.settlement,
                keep_chunk_ids=("chunk-b",),
                restore_chunk_ids=("chunk-a",),
            )

    asyncio.run(scenario())


def test_settlement_rejects_invalid_partition_and_wrong_store_without_consuming() -> None:
    async def scenario() -> None:
        first = QdrantChunkStore(
            cast(Any, _QdrantClient()),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )
        second = QdrantChunkStore(
            cast(Any, _QdrantClient()),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )
        outcome = await first.upsert_chunks_with_settlement(_chunks())
        assert repr(outcome.settlement) == "VectorWriteSettlementHandle(<opaque>)"
        assert "vector" not in repr(outcome.settlement).lower().replace(
            "vectorwritesettlementhandle", ""
        )
        with pytest.raises(TypeError, match="cannot be copied"):
            deepcopy(outcome.settlement)
        with pytest.raises(ValueError, match="exactly cover"):
            await first.settle_vector_write(
                outcome.settlement,
                keep_chunk_ids=("chunk-a",),
                restore_chunk_ids=(),
            )
        with pytest.raises(ValueError, match="another store"):
            await second.settle_vector_write(
                outcome.settlement,
                keep_chunk_ids=("chunk-a", "chunk-b"),
                restore_chunk_ids=(),
            )
        await first.settle_vector_write(
            outcome.settlement,
            keep_chunk_ids=("chunk-a", "chunk-b"),
            restore_chunk_ids=(),
        )

    asyncio.run(scenario())


def test_qdrant_settlement_verification_failure_is_truthfully_uncertain() -> None:
    async def scenario() -> None:
        client = _QdrantClient()
        old = models.PointStruct(
            id=point_id_for("chunk-a"),
            payload={"old": True},
            vector={"dense": [1.0], "sparse": models.SparseVector(indices=[1], values=[1.0])},
        )
        client.points[old.id] = old
        store = QdrantChunkStore(
            cast(Any, client),
            "private-customer-collection",
            _FixedEmbedder(_vectors()),
            HashSparseEncoder(),
        )
        outcome = await store.upsert_chunks_with_settlement(_chunks())
        client.corrupt_verification_read = True
        with pytest.raises(QdrantWriteCompensationError, match="settlement"):
            await store.settle_vector_write(
                outcome.settlement,
                keep_chunk_ids=("chunk-b",),
                restore_chunk_ids=("chunk-a",),
            )

    asyncio.run(scenario())


def test_pgvector_settlement_restores_raw_rows_without_reembedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        embedder = _FixedEmbedder(_vectors())
        store = PgvectorChunkStore(
            "postgresql://secret.example/runtime",
            "private_customer_table",
            embedder,
            HashSparseEncoder(),
        )
        connection = _PgConnection()

        @asynccontextmanager
        async def acquire():
            yield connection

        store._acquire = acquire  # type: ignore[method-assign]
        main: dict[str, tuple[Any, str]] = {"chunk-a": ({"old": {"acl": ["ops"]}}, "[91.25,-7.5]")}
        sparse: dict[str, list[tuple[int, float]]] = {"chunk-a": [(9, 0.25), (2, 4.5)]}
        old = deepcopy((main["chunk-a"], sparse["chunk-a"]))

        async def execute(conn: object, query: str, params: object = None):  # type: ignore[no-untyped-def]
            ids = cast(tuple[list[str]], params)[0] if params is not None else []
            if "SELECT chunk_id, payload, dense::text" in query:
                return [(chunk_id, *main[chunk_id]) for chunk_id in ids if chunk_id in main]
            if "SELECT chunk_id, term_index, weight" in query:
                return [
                    (chunk_id, term, weight)
                    for chunk_id in ids
                    for term, weight in sparse.get(chunk_id, [])
                ]
            if "DELETE FROM private_customer_table_sparse" in query:
                for chunk_id in ids:
                    sparse.pop(chunk_id, None)
            elif "DELETE FROM private_customer_table" in query:
                for chunk_id in ids:
                    main.pop(chunk_id, None)
                    sparse.pop(chunk_id, None)
            return None

        async def executemany(conn: object, query: str, rows: list[tuple[Any, ...]]) -> None:
            if "INSERT INTO private_customer_table (" in query:
                for chunk_id, payload, dense in rows:
                    main[chunk_id] = (
                        json.loads(payload) if isinstance(payload, str) else payload,
                        dense,
                    )
            elif "INSERT INTO private_customer_table_sparse" in query:
                for chunk_id, term, weight in rows:
                    sparse.setdefault(chunk_id, []).append((int(term), float(weight)))

        monkeypatch.setattr(_pg, "execute", execute)
        monkeypatch.setattr(_pg, "executemany", executemany)
        outcome = await store.upsert_chunks_with_settlement(_chunks())

        async def forbidden_embed(texts: object) -> list[list[float]]:
            raise AssertionError("settlement must restore raw rows without embedding")

        embedder.embed = forbidden_embed  # type: ignore[method-assign]
        await store.settle_vector_write(
            outcome.settlement,
            keep_chunk_ids=("chunk-b",),
            restore_chunk_ids=("chunk-a",),
        )
        assert (main["chunk-a"], sparse["chunk-a"]) == old
        assert "chunk-b" in main

    asyncio.run(scenario())
