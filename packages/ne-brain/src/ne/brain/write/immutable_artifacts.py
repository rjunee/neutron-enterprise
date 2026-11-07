# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Immutable knowledge-artifact create/reuse over Postgres plus a vector store.

This implements the additive ``ImmutableKnowledgeArtifactWrite`` seam. It does
not change the frozen ``KnowledgeWrite``/``ChunkStore`` signatures and it never
routes an authority artifact through ``KnowledgeWriter.upsert``: an unconditional
upsert would overwrite an immutable artifact another operation already selected
and retained.

**Fenced selection across two stores, not a cross-store transaction.** Every
caller writes its own candidate vector point FIRST and then races one Postgres
``INSERT ... ON CONFLICT DO NOTHING`` on the artifact address. Exactly one caller
creates the row, and that row names the single selected point. A caller that
crashes between the two steps leaves an inert unselected point behind; it cannot
overwrite, select or unwind-delete the winner. That is the honest claim — an
orphan is possible, an incorrect winner is not.

**The persisted address key is the artifact id and nothing else.** Admitted-at,
fence tokens and attempt ids are structurally absent from
``SourceContentArtifactIdentity`` (ADR-0022 §2); admitting one into the row key
here would silently stop identical content reusing its artifact, which no
contract-level control can see.

**The embedding cache is keyed on canonical content, never on the artifact id.**
The artifact id binds the captured ACL, so an ACL-only change mints a new
artifact over byte-identical content; keying the cache on it would buy a second
provider call for content the deployment has already embedded.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa

from ne.brain.current_source_authority import (
    ne_source_authority_heads,
    ne_source_authority_receipts,
)
from ne.contracts.knowledge import Embedder
from ne.contracts.source_authority import (
    ImmutableArtifactCreateReceipt,
    SourceContentArtifactIdentity,
)

__all__ = [
    "AUTHORITY_ARTIFACT_CANDIDATE_KEY",
    "AUTHORITY_ARTIFACT_PAYLOAD_KEY",
    "ImmutableArtifactContentMismatchError",
    "ImmutableArtifactError",
    "ImmutableArtifactVectorSeam",
    "ImmutableKnowledgeArtifactStore",
    "ensure_immutable_artifact_schema",
    "immutable_artifact_metadata",
    "ne_source_immutable_artifact_embeddings",
    "ne_source_immutable_artifact_refs",
    "ne_source_immutable_artifacts",
]

#: The reserved vector payload key naming the immutable artifact a point belongs
#: to. ``KnowledgeWriter.upsert`` refuses any item carrying it, so the ordinary
#: unconditional write path cannot land in the authority namespace.
AUTHORITY_ARTIFACT_PAYLOAD_KEY = "ne_source_artifact_id"

#: The reserved payload key naming one candidate write attempt for that artifact.
AUTHORITY_ARTIFACT_CANDIDATE_KEY = "ne_source_artifact_candidate_id"

_POSTGRES_SCHEMA_LOCK_KEY = 0x4E45_5341_5241

immutable_artifact_metadata = sa.MetaData()


class ImmutableArtifactError(RuntimeError):
    """An immutable artifact create/reuse could not be completed."""


class ImmutableArtifactContentMismatchError(ImmutableArtifactError):
    """Presented bytes do not hash to the identity's content snapshot."""


ne_source_immutable_artifacts = sa.Table(
    "ne_source_immutable_artifacts",
    immutable_artifact_metadata,
    # The ADDRESS KEY is the artifact id alone. Nothing operational belongs here.
    sa.Column("artifact_id", sa.String(64), primary_key=True),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("acl_snapshot_hash", sa.String(64), nullable=False),
    sa.Column("creator_token", sa.String(512), nullable=False),
    sa.Column("selected_point_id", sa.String(512), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_source_immutable_artifacts_content", "content_hash"),
)

ne_source_immutable_artifact_refs = sa.Table(
    "ne_source_immutable_artifact_refs",
    immutable_artifact_metadata,
    sa.Column("reference_id", sa.String(512), primary_key=True),
    sa.Column("artifact_id", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_source_immutable_artifact_refs_artifact", "artifact_id"),
)

ne_source_immutable_artifact_embeddings = sa.Table(
    "ne_source_immutable_artifact_embeddings",
    immutable_artifact_metadata,
    # Keyed on canonical content + embedding profile. The ACL-bearing artifact id
    # is deliberately NOT part of this key.
    sa.Column("content_hash", sa.String(64), primary_key=True),
    sa.Column("embedding_profile", sa.String(512), primary_key=True),
    sa.Column("vector", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_immutable_artifact_schema(engine: sa.Engine) -> None:
    """Create the immutable-artifact tables, serialized across booting replicas."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _POSTGRES_SCHEMA_LOCK_KEY},
            )
        immutable_artifact_metadata.create_all(conn)


@runtime_checkable
class ImmutableArtifactVectorSeam(Protocol):
    """The two vector operations immutable artifacts need, and no others.

    Deliberately narrow: the artifact store must not be able to delete, update or
    unconditionally upsert anything through the vector adapter it holds.
    """

    async def put_immutable_artifact_candidate(
        self, *, candidate_id: str, artifact_id: str, dense: Sequence[float]
    ) -> None: ...

    async def immutable_artifact_candidate_ids(self, artifact_id: str) -> tuple[str, ...]: ...


def candidate_point_id(artifact_id: str, reference_id: str) -> str:
    """One deterministic candidate id per (artifact, in-flight reference)."""
    return hashlib.sha256(
        b"ne.brain.immutable-artifact-candidate.v1\x1e"
        + artifact_id.encode("utf-8")
        + b"\x1e"
        + reference_id.encode("utf-8")
    ).hexdigest()


class ImmutableKnowledgeArtifactStore:
    """``ImmutableKnowledgeArtifactWrite`` over one runtime engine + vector seam."""

    def __init__(
        self,
        engine: sa.Engine,
        vectors: ImmutableArtifactVectorSeam,
        embedder: Embedder,
    ) -> None:
        self._engine = engine
        self._vectors = vectors
        self._embedder = embedder
        self._embedding_profile = f"{embedder.model_id}@{embedder.dimension}"
        self._provider_calls = 0

    @property
    def embedding_profile(self) -> str:
        return self._embedding_profile

    @property
    def provider_call_count(self) -> int:
        """Embedding provider calls this instance actually issued."""
        return self._provider_calls

    async def create_if_absent(
        self, identity: SourceContentArtifactIdentity, canonical_bytes: bytes
    ) -> ImmutableArtifactCreateReceipt:
        content_hash = hashlib.sha256(canonical_bytes).hexdigest()
        if content_hash != identity.content_snapshot_hash:
            raise ImmutableArtifactContentMismatchError(
                "canonical bytes do not hash to the identity's content snapshot"
            )
        reference_id = uuid.uuid4().hex
        self._retain_reference(identity.artifact_id, reference_id)
        dense = await self._embedding(content_hash, canonical_bytes)
        candidate_id = candidate_point_id(identity.artifact_id, reference_id)
        # The candidate point is written BEFORE the address row is claimed: a
        # crash here leaves an inert unselected point, never a selected row with
        # no vector behind it.
        await self._vectors.put_immutable_artifact_candidate(
            candidate_id=candidate_id, artifact_id=identity.artifact_id, dense=dense
        )
        created, creator_token = self._claim_address(identity, content_hash, candidate_id)
        return ImmutableArtifactCreateReceipt(
            artifact_identity=identity,
            created=created,
            creator_token=creator_token if created else None,
            inflight_reference_id=reference_id,
        )

    async def release_creator(
        self,
        receipt: ImmutableArtifactCreateReceipt,
        *,
        expected_current_head: str | None,
    ) -> bool:
        """Bounded creator unwind: exact creator token, zero other references, head CAS.

        There is no unconditional delete here and never a delete of somebody
        else's artifact: a reuser holds no creator token, so it fails at the first
        check, and an artifact the current head selects is refused outright.
        """
        if receipt.creator_token is None:
            return False
        artifact_id = receipt.artifact_identity.artifact_id
        identity_hash = receipt.artifact_identity.source_identity.identity_hash
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.select(ne_source_immutable_artifacts.c.creator_token).where(
                    ne_source_immutable_artifacts.c.artifact_id == artifact_id
                )
            ).one_or_none()
            if row is None or row.creator_token != receipt.creator_token:
                return False
            head = conn.execute(
                sa.select(
                    ne_source_authority_heads.c.head_revision,
                    ne_source_authority_receipts.c.artifact_id.label("head_artifact_id"),
                )
                .select_from(
                    ne_source_authority_heads.join(
                        ne_source_authority_receipts,
                        ne_source_authority_heads.c.current_receipt_id
                        == ne_source_authority_receipts.c.receipt_id,
                    )
                )
                .where(ne_source_authority_heads.c.identity_hash == identity_hash)
            ).one_or_none()
            current_head = None if head is None else str(head.head_revision)
            if current_head != expected_current_head:
                return False
            if head is not None and head.head_artifact_id == artifact_id:
                return False
            references = (
                conn.execute(
                    sa.select(ne_source_immutable_artifact_refs.c.reference_id).where(
                        ne_source_immutable_artifact_refs.c.artifact_id == artifact_id
                    )
                )
                .scalars()
                .all()
            )
            if set(references) != {receipt.inflight_reference_id}:
                return False
            conn.execute(
                sa.delete(ne_source_immutable_artifact_refs).where(
                    ne_source_immutable_artifact_refs.c.artifact_id == artifact_id
                )
            )
            unwound = conn.execute(
                sa.delete(ne_source_immutable_artifacts).where(
                    ne_source_immutable_artifacts.c.artifact_id == artifact_id,
                    ne_source_immutable_artifacts.c.creator_token == receipt.creator_token,
                )
            ).rowcount
        return unwound == 1

    def selected_point_id(self, artifact_id: str) -> str | None:
        """The one selected candidate point, or ``None`` while unclaimed."""
        with self._engine.connect() as conn:
            return conn.execute(
                sa.select(ne_source_immutable_artifacts.c.selected_point_id).where(
                    ne_source_immutable_artifacts.c.artifact_id == artifact_id
                )
            ).scalar_one_or_none()

    def reference_ids(self, artifact_id: str) -> tuple[str, ...]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.select(ne_source_immutable_artifact_refs.c.reference_id).where(
                        ne_source_immutable_artifact_refs.c.artifact_id == artifact_id
                    )
                )
                .scalars()
                .all()
            )
        return tuple(sorted(str(value) for value in rows))

    # -- internals ---------------------------------------------------------

    def _retain_reference(self, artifact_id: str, reference_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(ne_source_immutable_artifact_refs).values(
                    reference_id=reference_id,
                    artifact_id=artifact_id,
                    created_at=datetime.now(UTC),
                )
            )

    async def _embedding(self, content_hash: str, canonical_bytes: bytes) -> Sequence[float]:
        with self._engine.connect() as conn:
            cached = conn.execute(
                sa.select(ne_source_immutable_artifact_embeddings.c.vector).where(
                    ne_source_immutable_artifact_embeddings.c.content_hash == content_hash,
                    ne_source_immutable_artifact_embeddings.c.embedding_profile
                    == self._embedding_profile,
                )
            ).scalar_one_or_none()
        if cached is not None:
            return _as_vector(cached)
        try:
            text = canonical_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImmutableArtifactError(
                "canonical artifact bytes must be UTF-8 text pre-demo"
            ) from exc
        self._provider_calls += 1
        vectors = await self._embedder.embed([text])
        dense = [float(value) for value in vectors[0]]
        with self._engine.begin() as conn:
            _insert_absent(
                conn,
                ne_source_immutable_artifact_embeddings,
                ("content_hash", "embedding_profile"),
                {
                    "content_hash": content_hash,
                    "embedding_profile": self._embedding_profile,
                    "vector": dense,
                    "created_at": datetime.now(UTC),
                },
            )
        return dense

    def _claim_address(
        self,
        identity: SourceContentArtifactIdentity,
        content_hash: str,
        candidate_id: str,
    ) -> tuple[bool, str]:
        creator_token = uuid.uuid4().hex
        with self._engine.begin() as conn:
            inserted = _insert_absent(
                conn,
                ne_source_immutable_artifacts,
                ("artifact_id",),
                {
                    "artifact_id": identity.artifact_id,
                    "content_hash": content_hash,
                    "acl_snapshot_hash": identity.acl_snapshot_hash,
                    "creator_token": creator_token,
                    "selected_point_id": candidate_id,
                    "created_at": datetime.now(UTC),
                },
            )
            stored = conn.execute(
                sa.select(ne_source_immutable_artifacts.c.content_hash).where(
                    ne_source_immutable_artifacts.c.artifact_id == identity.artifact_id
                )
            ).scalar_one()
        if stored != content_hash:
            raise ImmutableArtifactContentMismatchError(
                "this artifact id is already bound to different canonical content"
            )
        return inserted, creator_token


def _as_vector(value: Any) -> list[float]:
    if isinstance(value, str):
        value = json.loads(value)
    return [float(item) for item in value]


def _insert_absent(
    conn: sa.Connection,
    table: sa.Table,
    conflict_columns: Sequence[str],
    values: dict[str, Any],
) -> bool:
    """Shared with :mod:`ne.brain.current_source_authority` in intent, not in code.

    ``RETURNING`` rather than ``rowcount``: psycopg reports -1 for an
    ON CONFLICT DO NOTHING insert, so a rowcount test reads every insert as a loss
    on PostgreSQL — which would hand the artifact-address race to nobody.
    """
    from ne.brain.current_source_authority import _insert_absent as _shared

    return _shared(conn, table, conflict_columns, values)
