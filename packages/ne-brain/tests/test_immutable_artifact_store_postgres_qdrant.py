# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE Postgres + LIVE Qdrant proofs for fenced immutable-artifact selection.

Env-gated on ``NE_TEST_POSTGRES_URL`` and ``NE_TEST_QDRANT_URL`` and PROMOTED into
the REQUIRED ``check`` job, which fails if any node here skips or collects zero.

What is proved here, and exactly this: **fenced selection across two stores, not a
cross-store transaction.** Concurrent ``create_if_absent`` on one identity elects
exactly one winner; an interrupted caller leaves an inert unselected Qdrant point
behind; and that orphan can neither be selected nor unwind-delete the winner. A
crash CAN leave an orphan — the claim is not that it cannot, and a proof that
asserted "no orphan" would be asserting something the design does not provide.

Neither an in-memory Qdrant nor SQLite can stand in. SQLite's global writer lock
serializes the address race, and an in-process Qdrant shares the writer's own
memory, so the orphan is not a durable second store's row at all.
:func:`test_an_unfenced_address_claim_admits_every_caller_under_the_same_harness`
is the positive control that the harness genuinely interleaves.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa
from qdrant_client import AsyncQdrantClient

from ne.brain.current_source_authority import (
    ensure_source_authority_schema,
    source_authority_metadata,
)
from ne.brain.qdrant import QdrantChunkStore
from ne.brain.testing.embedding import HashEmbedder, HashSparseEncoder
from ne.brain.testing.qdrant_live import OwnedQdrantCollections
from ne.brain.write.immutable_artifacts import (
    ImmutableKnowledgeArtifactStore,
    candidate_point_id,
    ensure_immutable_artifact_schema,
    immutable_artifact_metadata,
    ne_source_immutable_artifacts,
)

from .test_current_source_authority import t3_artifact, t3_contract, t3_identity

_PG_URL = os.environ.get("NE_TEST_POSTGRES_URL")
_QDRANT_URL = os.environ.get("NE_TEST_QDRANT_URL")

pytestmark = pytest.mark.skipif(
    not (_PG_URL and _QDRANT_URL),
    reason=(
        "live Postgres + live Qdrant not configured "
        "(set NE_TEST_POSTGRES_URL and NE_TEST_QDRANT_URL); promoted proof"
    ),
)

WORKERS = 8
CONTENT = b"one canonical artifact body"


@pytest.fixture
def owned() -> Iterator[OwnedQdrantCollections]:
    """The janitor for this module's collections — see ne.brain.testing.qdrant_live.

    ``NE_TEST_QDRANT_URL`` is named by ci.yml on the promoted-gate step alone, and
    points at that job's OWN service container. It has no default here for the same
    reason ``NE_QDRANT_URL`` no longer has one.
    """
    assert _QDRANT_URL is not None  # guaranteed by the module-level skipif
    janitor = OwnedQdrantCollections(_QDRANT_URL)
    try:
        yield janitor
    finally:
        janitor.drop_all()


@pytest.fixture
def collection(owned: OwnedQdrantCollections) -> str:
    name = owned.collection("authority")

    async def _create() -> None:
        client = owned.client()
        try:
            await QdrantChunkStore(
                client, name, HashEmbedder(32), HashSparseEncoder()
            ).ensure_collection()
        finally:
            await client.close()

    asyncio.run(_create())
    return name


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _PG_URL is not None
    engine = sa.create_engine(_PG_URL, pool_size=WORKERS * 2, max_overflow=WORKERS * 2)
    immutable_artifact_metadata.drop_all(engine)
    source_authority_metadata.drop_all(engine)
    ensure_source_authority_schema(engine)
    ensure_immutable_artifact_schema(engine)
    try:
        yield engine
    finally:
        immutable_artifact_metadata.drop_all(engine)
        source_authority_metadata.drop_all(engine)
        engine.dispose()


def _race(worker: Callable[[int], None], *, workers: int = WORKERS) -> list[BaseException]:
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrapped(index: int) -> None:
        barrier.wait()
        try:
            worker(index)
        except BaseException as exc:  # the point of the proof is exactly what escaped
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def _artifact():
    contract = t3_contract()
    identity = t3_identity("row-1")
    return t3_artifact(identity, contract, content=CONTENT)


async def _qdrant_candidates(
    owned: OwnedQdrantCollections, collection: str, artifact_id: str
) -> tuple[str, ...]:
    client = owned.client()
    try:
        store = QdrantChunkStore(client, collection, HashEmbedder(32), HashSparseEncoder())
        return await store.immutable_artifact_candidate_ids(artifact_id)
    finally:
        await client.close()


def _store_in_thread(
    owned: OwnedQdrantCollections, collection: str
) -> tuple[ImmutableKnowledgeArtifactStore, AsyncQdrantClient]:
    engine = sa.create_engine(str(_PG_URL))
    client = owned.client()
    vectors = QdrantChunkStore(client, collection, HashEmbedder(32), HashSparseEncoder())
    return ImmutableKnowledgeArtifactStore(engine, vectors, HashEmbedder(32)), client


# --- exactly one winner, across two real stores ------------------------------


def test_concurrent_creates_of_one_artifact_elect_exactly_one_winner(
    pg_engine: sa.Engine, owned: OwnedQdrantCollections, collection: str
) -> None:
    identity = _artifact()
    created: list[str] = []
    reused: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        store, client = _store_in_thread(owned, collection)

        async def run() -> None:
            receipt = await store.create_if_absent(identity, CONTENT)
            assert receipt.artifact_identity == identity
            with lock:
                (created if receipt.created else reused).append(receipt.inflight_reference_id)

        try:
            asyncio.run(run())
        finally:
            asyncio.run(client.close())

    errors = _race(worker)
    assert errors == [], errors
    assert len(created) == 1
    assert len(reused) == WORKERS - 1

    with pg_engine.connect() as conn:
        rows = conn.execute(sa.select(ne_source_immutable_artifacts)).all()
    assert len(rows) == 1
    selected = rows[0].selected_point_id
    candidates = asyncio.run(_qdrant_candidates(owned, collection, identity.artifact_id))
    # Every caller wrote a candidate into the REAL second store...
    assert len(candidates) == WORKERS
    # ...and exactly one of them is the selected winner. The rest are inert.
    assert selected in candidates
    assert selected == candidate_point_id(identity.artifact_id, created[0])


def test_an_interrupted_caller_leaves_an_inert_orphan_that_is_never_selected(
    pg_engine: sa.Engine, owned: OwnedQdrantCollections, collection: str
) -> None:
    """A crash between the vector write and the address claim is survivable.

    The orphan is written EXACTLY as a real interrupted caller would have written
    it — through the production vector seam, under a real in-flight reference id —
    so this is the durable state a crash leaves, not a hand-planted point.
    """
    identity = _artifact()
    abandoned_reference = uuid.uuid4().hex
    orphan_id = candidate_point_id(identity.artifact_id, abandoned_reference)

    async def interrupted() -> None:
        client = owned.client()
        try:
            vectors = QdrantChunkStore(client, collection, HashEmbedder(32), HashSparseEncoder())
            dense = (await HashEmbedder(32).embed([CONTENT.decode()]))[0]
            await vectors.put_immutable_artifact_candidate(
                candidate_id=orphan_id, artifact_id=identity.artifact_id, dense=dense
            )
        finally:
            await client.close()

    asyncio.run(interrupted())
    # ...and then the process died before it claimed the address.

    store, client = _store_in_thread(owned, collection)
    try:
        receipt = asyncio.run(store.create_if_absent(identity, CONTENT))
    finally:
        asyncio.run(client.close())
    assert receipt.created is True

    selected = store.selected_point_id(identity.artifact_id)
    candidates = asyncio.run(_qdrant_candidates(owned, collection, identity.artifact_id))
    assert orphan_id in candidates  # the orphan is still there — that IS the claim
    assert selected == candidate_point_id(identity.artifact_id, receipt.inflight_reference_id)
    assert selected != orphan_id


def test_a_losing_caller_cannot_unwind_delete_the_winner(
    pg_engine: sa.Engine, owned: OwnedQdrantCollections, collection: str
) -> None:
    """The loser holds no creator token, so the winner's row and point survive."""
    identity = _artifact()
    store, client = _store_in_thread(owned, collection)
    try:

        async def run() -> None:
            winner = await store.create_if_absent(identity, CONTENT)
            loser = await store.create_if_absent(identity, CONTENT)
            assert winner.created is True and loser.created is False
            assert await store.release_creator(loser, expected_current_head=None) is False
            # Nor by presenting the winner's own reference id with no token.
            forged = loser.model_copy(
                update={"inflight_reference_id": winner.inflight_reference_id}
            )
            assert await store.release_creator(forged, expected_current_head=None) is False

        asyncio.run(run())
    finally:
        asyncio.run(client.close())

    with pg_engine.connect() as conn:
        rows = conn.execute(sa.select(ne_source_immutable_artifacts)).all()
    assert len(rows) == 1
    assert rows[0].content_hash == hashlib.sha256(CONTENT).hexdigest()
    assert rows[0].selected_point_id in asyncio.run(
        _qdrant_candidates(owned, collection, identity.artifact_id)
    )


def test_an_unfenced_address_claim_admits_every_caller_under_the_same_harness(
    pg_engine: sa.Engine, owned: OwnedQdrantCollections, collection: str
) -> None:
    """Positive control: the harness DOES interleave, so the arbitration is real.

    A read-then-insert address claim — check whether the row exists, then write it —
    is the shape a plausible implementation takes without ON CONFLICT. Under the
    identical barrier and worker count every caller reads "absent" and believes it
    created the artifact, which is exactly the lost arbitration the fenced path
    must not have.
    """
    identity = _artifact()
    believed_created: list[int] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        engine = sa.create_engine(str(_PG_URL))
        try:
            with engine.connect() as conn:
                exists = conn.execute(
                    sa.select(ne_source_immutable_artifacts.c.artifact_id).where(
                        ne_source_immutable_artifacts.c.artifact_id == identity.artifact_id
                    )
                ).scalar_one_or_none()
            if exists is None:
                with lock:
                    believed_created.append(index)
        finally:
            engine.dispose()

    assert _race(worker) == []
    assert len(believed_created) == WORKERS
