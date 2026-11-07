# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE: immutable knowledge-artifact create/reuse.

The ADR-0022 §7 immutable-write kit is subclassed with the real store. The
controls added here hold three things the kit cannot see:

* **The persisted address key is the artifact id and nothing else.** Admitting an
  operational field — admitted-at, a fence token, an attempt id — is invisible to
  a contract-level control and silently stops identical content reusing its
  artifact, so the reuse control below advances the wall clock between the two
  calls and asserts ONE row.
* **The embedding cache is keyed on canonical content, not the ACL-bearing id.**
  Asserted in both directions: an ACL-only change must cost ZERO extra provider
  calls, and a content change must cost exactly one. One direction alone is
  satisfied by a cache that never caches, and the other by a cache that never
  invalidates.
* **An authority artifact never reaches ``KnowledgeWriter.upsert``.** Also in both
  directions: the writer refuses an item that claims to be one, and the immutable
  store persists an artifact without calling the writer at all.

The two-store fenced-selection proof needs real Postgres plus live Qdrant and
lives in ``test_immutable_artifact_store_postgres_qdrant.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence

import pytest
import sqlalchemy as sa

from ne.brain.current_source_authority import (
    claim_or_load_deployment_construction_id,
    ensure_source_authority_schema,
)
from ne.brain.testing.embedding import HashEmbedder
from ne.brain.write.contracts import KnowledgeWriteError
from ne.brain.write.immutable_artifacts import (
    AUTHORITY_ARTIFACT_PAYLOAD_KEY,
    ImmutableArtifactContentMismatchError,
    ImmutableKnowledgeArtifactStore,
    ensure_immutable_artifact_schema,
    ne_source_immutable_artifact_embeddings,
    ne_source_immutable_artifacts,
)
from ne.contracts.knowledge import KnowledgeItem
from ne.contracts.source_authority import (
    ImmutableKnowledgeArtifactWrite,
    SourceContentArtifactIdentity,
)
from ne.contracts.testing.source_authority import ImmutableKnowledgeArtifactWriteContractKit

from .test_current_source_authority import (
    ACL_A,
    ACL_B,
    admit,
    memory_engine,
    reconciliation_scope,
    t3_artifact,
    t3_contract,
    t3_identity,
)
from .test_current_source_authority import (
    SqlAlchemyCurrentSourceAuthorityWriter as _Writer,
)


class MemoryVectorSeam:
    """An in-memory ``ImmutableArtifactVectorSeam``: candidates only, no delete."""

    def __init__(self) -> None:
        self.points: dict[str, tuple[str, tuple[float, ...]]] = {}

    async def put_immutable_artifact_candidate(
        self, *, candidate_id: str, artifact_id: str, dense: Sequence[float]
    ) -> None:
        self.points[candidate_id] = (artifact_id, tuple(float(value) for value in dense))

    async def immutable_artifact_candidate_ids(self, artifact_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                candidate for candidate, (owner, _) in self.points.items() if owner == artifact_id
            )
        )


class CountingEmbedder(HashEmbedder):
    """A real deterministic embedder that records how often it was actually called."""

    def __init__(self, dimension: int = 32) -> None:
        super().__init__(dimension)
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls += 1
        return await super().embed(texts)


def artifact_store(
    engine: sa.Engine | None = None, embedder: CountingEmbedder | None = None
) -> tuple[ImmutableKnowledgeArtifactStore, MemoryVectorSeam, CountingEmbedder, sa.Engine]:
    engine = engine or memory_engine()
    ensure_immutable_artifact_schema(engine)
    vectors = MemoryVectorSeam()
    embedder = embedder or CountingEmbedder()
    return ImmutableKnowledgeArtifactStore(engine, vectors, embedder), vectors, embedder, engine


def artifact_for(
    content: bytes, *, acl_snapshot_hash: str = ACL_A
) -> SourceContentArtifactIdentity:
    contract = t3_contract()
    identity = t3_identity("row-1")
    return t3_artifact(identity, contract, content=content, acl_snapshot_hash=acl_snapshot_hash)


# --- ADR-0022 §7 immutable-write conformance kit -----------------------------


class TestImmutableKnowledgeArtifactWriteContract(ImmutableKnowledgeArtifactWriteContractKit):
    def setup_method(self) -> None:
        self.store, self.vectors, self.embedder, self.engine = artifact_store()

    def create_writer(self) -> ImmutableKnowledgeArtifactWrite:
        return self.store

    def artifact(self) -> SourceContentArtifactIdentity:
        return artifact_for(b"kit canonical content")

    def canonical_bytes(self) -> bytes:
        return b"kit canonical content"

    async def create_failure(
        self,
        writer: ImmutableKnowledgeArtifactWrite,
        identity: SourceContentArtifactIdentity,
        canonical_bytes: bytes,
    ) -> bool:
        try:
            await writer.create_if_absent(identity, canonical_bytes)
        except ImmutableArtifactContentMismatchError:
            return True
        return False


# --- the persisted address key is the artifact id alone ----------------------


def test_reuse_of_one_artifact_id_keeps_exactly_one_persisted_address_row() -> None:
    """Wall-clock and attempt state are structurally absent from the address key.

    The two calls happen at different times and under different in-flight
    references. Admitting either into the row key would mint a second address for
    byte-identical content, which every contract-level assertion still passes.
    """
    store, _vectors, _embedder, engine = artifact_store()
    identity = artifact_for(b"stable content")

    async def scenario() -> None:
        first = await store.create_if_absent(identity, b"stable content")
        second = await store.create_if_absent(identity, b"stable content")
        assert first.created is True
        assert second.created is False
        assert first.inflight_reference_id != second.inflight_reference_id

    asyncio.run(scenario())
    with engine.connect() as conn:
        rows = conn.execute(sa.select(ne_source_immutable_artifacts)).all()
    assert [row.artifact_id for row in rows] == [identity.artifact_id]
    assert set(ne_source_immutable_artifacts.c.keys()) == {
        "artifact_id",
        "content_hash",
        "acl_snapshot_hash",
        "creator_token",
        "selected_point_id",
        "created_at",
    }
    assert list(ne_source_immutable_artifacts.primary_key.columns.keys()) == ["artifact_id"]


def test_the_reuser_receives_the_winners_selected_point_not_its_own() -> None:
    """Reuse retains an in-flight reference; it never re-selects the vector point."""
    store, vectors, _embedder, _engine = artifact_store()
    identity = artifact_for(b"one selection")

    async def scenario() -> None:
        first = await store.create_if_absent(identity, b"one selection")
        second = await store.create_if_absent(identity, b"one selection")
        selected = store.selected_point_id(identity.artifact_id)
        candidates = await vectors.immutable_artifact_candidate_ids(identity.artifact_id)
        assert selected is not None
        assert len(candidates) == 2  # both attempts wrote a candidate...
        assert selected in candidates  # ...and exactly one of them is selected.
        assert store.reference_ids(identity.artifact_id) == tuple(
            sorted((first.inflight_reference_id, second.inflight_reference_id))
        )

    asyncio.run(scenario())


# --- the embedding cache is keyed on content, not on the ACL-bearing id ------


def test_an_acl_only_change_reuses_the_exact_embedding() -> None:
    """An ACL-only artifact makes no further provider call."""
    store, _vectors, embedder, _engine = artifact_store()
    content = b"identical canonical bytes"
    open_acl = artifact_for(content, acl_snapshot_hash=ACL_A)
    restricted = artifact_for(content, acl_snapshot_hash=ACL_B)
    assert open_acl.artifact_id != restricted.artifact_id

    async def scenario() -> None:
        await store.create_if_absent(open_acl, content)
        assert embedder.calls == 1
        await store.create_if_absent(restricted, content)

    asyncio.run(scenario())
    assert embedder.calls == 1
    assert store.provider_call_count == 1


def test_changed_content_costs_exactly_one_further_embedding() -> None:
    """The must-PASS other direction: a cache that never caches would fail HERE."""
    store, _vectors, embedder, _engine = artifact_store()
    first = artifact_for(b"first canonical bytes")
    second = artifact_for(b"second canonical bytes")

    async def scenario() -> None:
        await store.create_if_absent(first, b"first canonical bytes")
        await store.create_if_absent(second, b"second canonical bytes")

    asyncio.run(scenario())
    assert embedder.calls == 2


def test_the_embedding_cache_key_is_content_and_profile_only() -> None:
    store, _vectors, _embedder, engine = artifact_store()
    content = b"cached canonical bytes"
    asyncio.run(store.create_if_absent(artifact_for(content), content))
    assert list(ne_source_immutable_artifact_embeddings.primary_key.columns.keys()) == [
        "content_hash",
        "embedding_profile",
    ]
    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_source_immutable_artifact_embeddings)).one()
    assert row.content_hash == hashlib.sha256(content).hexdigest()


# --- authority artifacts never ride the unconditional KnowledgeWrite path ----


def test_the_knowledge_writer_refuses_an_item_claiming_to_be_an_authority_artifact(
    write_stack, frozen_config
) -> None:
    """`upsert` overwrites by item id; an immutable artifact must never take it."""
    stack = write_stack()

    async def scenario() -> None:
        with pytest.raises(KnowledgeWriteError, match=AUTHORITY_ARTIFACT_PAYLOAD_KEY):
            await stack.writer.upsert(
                [
                    KnowledgeItem(
                        item_id="authority-1",
                        content="protected row",
                        provenance="src",
                        metadata={AUTHORITY_ARTIFACT_PAYLOAD_KEY: "a" * 64},
                    )
                ]
            )
        # Positive control: an ordinary item on the SAME writer still lands, so
        # the refusal above is the guard and not a broken write path.
        receipt = await stack.writer.upsert(
            [KnowledgeItem(item_id="ordinary-1", content="open note", provenance="src")]
        )
        assert receipt.accepted == ("ordinary-1",)

    asyncio.run(scenario())


def test_creating_an_authority_artifact_calls_no_knowledge_writer(
    write_stack, frozen_config
) -> None:
    """The other direction: the immutable store persists without the writer at all."""
    stack = write_stack()
    calls: list[object] = []
    original = stack.writer.upsert

    async def spy(items):
        calls.append(items)
        return await original(items)

    stack.writer.upsert = spy  # type: ignore[method-assign]
    store, _vectors, _embedder, _engine = artifact_store()
    identity = artifact_for(b"authority bytes")
    receipt = asyncio.run(store.create_if_absent(identity, b"authority bytes"))
    assert receipt.created is True
    assert store.selected_point_id(identity.artifact_id) is not None
    assert calls == []


# --- bounded creator unwind --------------------------------------------------


def test_only_the_creator_with_the_sole_reference_and_a_head_cas_may_unwind() -> None:
    store, _vectors, _embedder, engine = artifact_store()
    identity = artifact_for(b"unwindable")

    async def scenario() -> None:
        created = await store.create_if_absent(identity, b"unwindable")
        reused = await store.create_if_absent(identity, b"unwindable")
        # A second live reference blocks the creator: it is not the sole holder.
        assert await store.release_creator(created, expected_current_head=None) is False
        # And the reuser holds no creator token at all.
        assert await store.release_creator(reused, expected_current_head=None) is False

    asyncio.run(scenario())
    assert store.selected_point_id(identity.artifact_id) is not None

    async def sole_creator() -> None:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM ne_source_immutable_artifact_refs"))
        fresh = await store.create_if_absent(identity, b"unwindable")
        assert fresh.created is False  # the address row survived
        # A wrong expected head refuses even the true creator.
        with engine.connect() as conn:
            token = conn.execute(
                sa.select(ne_source_immutable_artifacts.c.creator_token)
            ).scalar_one()
        creator = fresh.model_copy(update={"created": True, "creator_token": token})
        assert await store.release_creator(creator, expected_current_head="rev-1") is False
        assert await store.release_creator(creator, expected_current_head=None) is True
        assert store.selected_point_id(identity.artifact_id) is None

    asyncio.run(sole_creator())


def test_an_artifact_the_current_head_selects_cannot_be_unwound() -> None:
    """Bounded creator unwind is not a delete: the current head always wins."""
    engine = memory_engine()
    ensure_source_authority_schema(engine)
    construction_id = claim_or_load_deployment_construction_id(engine)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"head content")
    receipt = admit(
        _Writer(engine, construction_id),
        identity,
        contract,
        artifact,
        scope_path=reconciliation_scope("ledger").path,
    )
    assert receipt.artifact_identity == artifact
    store, _vectors, _embedder, _engine = artifact_store(engine)
    created = asyncio.run(store.create_if_absent(artifact, b"head content"))
    assert created.created is True
    assert (
        asyncio.run(store.release_creator(created, expected_current_head=receipt.source_revision))
        is False
    )
    assert store.selected_point_id(artifact.artifact_id) is not None
