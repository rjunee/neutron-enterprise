# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-M365: what T1 lineage retains for an admitted supported source.

Two claims, both of which a supported-source EvidenceLocator depends on:

* one document version retains the EXACT immutable artifact identity and the
  admission receipt it was admitted under, and
* an M365 authority receipt survives its own storage round trip. The receipt
  carries a STRICT ``Decimal`` SLA that ``model_dump(mode="json")`` renders as a
  string, so the stored payload is not directly re-validatable; a receipt the
  Reader cannot load back is not an admitted receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from ne.brain.current_source_authority import (
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    bind_deployment_construction_id,
    ensure_source_authority_schema,
    ne_source_authority_heads,
    ne_source_authority_receipts,
    ne_source_reconciliation_claims,
)
from ne.brain.lineage import SqlAlchemyDocumentLineageStore, ensure_document_lineage_schema
from ne.brain.write import CuratedKnowledgeItem, ProvenanceClass, ShapeContract
from ne.contracts.source_authority import (
    SOURCE_AUTHORITY_STAMP_METADATA_KEY,
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    SourceReconciliationScope,
    stamp_from_receipt,
)
from ne.contracts.types import Chunk

from .conftest import WriteStack

DEPLOYMENT = "deployment-alpha"
INSTANCE = "conn-1"
SLA = Decimal(3600)


def _engine() -> sa.Engine:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_document_lineage_schema(engine)
    ensure_source_authority_schema(engine)
    return engine


def _identity(item_id: str = "item-user") -> SourceIdentity:
    return SourceIdentity(
        deployment_id=DEPLOYMENT,
        source_kind="m365-sharepoint",
        connector_instance_id=INSTANCE,
        point=M365SourcePoint(
            graph_tenant_id="tenant-1",
            site_id="site-eng",
            drive_id="drive-1",
            item_id=item_id,
        ),
        generation=1,
    )


def _contract() -> SourceContractIdentity:
    return SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="c" * 64
    )


def _artifact(identity: SourceIdentity, *, acl: str = "a" * 64) -> SourceContentArtifactIdentity:
    return SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash="d" * 64,
        content_snapshot_hash="e" * 64,
        acl_snapshot_hash=acl,
        source_contract=_contract(),
        artifact_schema_version=1,
    )


def _pinned_head_revision(engine: sa.Engine, scope: SourceReconciliationScope) -> str | None:
    """The head a previous admission left pinned on this scope's claim row."""
    with engine.begin() as conn:
        value = conn.execute(
            sa.select(ne_source_reconciliation_claims.c.head_revision).where(
                ne_source_reconciliation_claims.c.scope_hash == scope.scope_hash
            )
        ).scalar_one_or_none()
    return None if value is None else str(value)


def _admit(
    engine: sa.Engine,
    *,
    item_id: str = "item-user",
    as_of: datetime | None = None,
) -> CurrentSourceAuthorityReceipt:
    construction_id = bind_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(
        engine, construction_id, freshness_sla_seconds=SLA
    )
    scope = SourceReconciliationScope(path=("m365-sharepoint", DEPLOYMENT, INSTANCE, item_id))
    claim = asyncio.run(
        writer.claim(
            scope,
            expected_head_revision=_pinned_head_revision(engine, scope),
            lease_seconds=60,
        )
    )
    identity = _identity(item_id)
    candidate = SourceAuthorityCandidate(
        source_identity=identity,
        artifact_identity=_artifact(identity),
        source_revision="etag-1",
        source_contract=_contract(),
        contract_epoch=1,
        acl_snapshot_hash="a" * 64,
        state=SourceAuthorityState.PRESENT,
        authority_as_of=as_of or datetime(2020, 1, 1, tzinfo=UTC),
    )
    fence = SourceAdmissionFence(
        deployment_id=DEPLOYMENT,
        connector_instance_id=INSTANCE,
        source_generation=1,
        external_source_revision="etag-1",
        contract_epoch=1,
        reconciliation_scope=scope,
        reconciliation_epoch=claim.reconciliation_epoch,
        fencing_token=claim.fencing_token,
    )
    receipt = asyncio.run(writer.finalize(claim, fence, candidate))
    # Release the lease so a second admission in the same test can re-claim the
    # scope; holding it would fail the NEXT claim on the lease, not on anything
    # this file is proving.
    asyncio.run(writer.release(claim))
    return receipt


def test_an_m365_receipt_is_readable_back_through_the_reader() -> None:
    engine = _engine()
    receipt = _admit(engine)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, bind_deployment_construction_id(engine))
    loaded = reader.load_receipt(receipt.receipt_id)
    assert loaded is not None
    assert loaded == receipt
    assert loaded.freshness_sla_seconds == SLA
    assert isinstance(loaded.freshness_sla_seconds, Decimal)


def test_retrying_one_attempt_at_its_persisted_instant_reproduces_the_receipt() -> None:
    """The SAME admission attempt retried is one historical fact, not two.

    ``authority_as_of`` is the attempt's persisted observation instant, so a
    retry that presents it again collapses onto the identical row: same id, same
    hash, same ``admitted_at``.
    """
    engine = _engine()
    observed = datetime(2020, 1, 1, tzinfo=UTC)
    first = _admit(engine, as_of=observed)
    second = _admit(engine, as_of=observed)
    assert second.receipt_id == first.receipt_id
    assert second.receipt_hash == first.receipt_hash
    assert second.admitted_at == first.admitted_at
    assert second == first


def test_a_later_re_read_mints_a_new_receipt_and_advances_the_head() -> None:
    """A genuinely re-read point must be able to become fresh again.

    Without ``authority_as_of`` in the receipt identity this admission would
    collapse onto the earlier row and keep its original as-of, so a re-read point
    would age out however often it was actually re-read.
    """
    engine = _engine()
    first = _admit(engine, as_of=datetime(2020, 1, 1, tzinfo=UTC))
    later = _admit(engine, as_of=datetime(2020, 1, 2, tzinfo=UTC))
    assert later.receipt_id != first.receipt_id
    assert later.authority_as_of > first.authority_as_of
    assert later.artifact_identity == first.artifact_identity
    with engine.begin() as conn:
        head = conn.execute(
            sa.select(ne_source_authority_heads.c.current_receipt_id).where(
                ne_source_authority_heads.c.identity_hash == later.source_identity.identity_hash
            )
        ).scalar_one()
        retained = conn.execute(
            sa.select(sa.func.count()).select_from(ne_source_authority_receipts)
        ).scalar_one()
    assert head == later.receipt_id
    # The store is append-only: the superseded receipt is still revalidatable.
    assert retained == 2


def _write_version(
    store: SqlAlchemyDocumentLineageStore,
    document_id: str,
    *,
    source_ref: str | None = None,
    source_snapshot_hash: str | None = None,
    source_acl_hash: str | None = None,
) -> None:
    metadata = {
        "connector": "m365-sharepoint",
        "source_system": "m365-sharepoint",
        "source_ref": "drive-1:item-user",
    }
    if source_ref is not None:
        metadata["source_ref"] = source_ref
    if source_snapshot_hash is not None:
        metadata["source_snapshot_hash"] = source_snapshot_hash
    if source_acl_hash is not None:
        metadata["source_acl_hash"] = source_acl_hash
    prepared = store.prepare(
        Chunk(
            chunk_id=f"{document_id}#0",
            document_id=document_id,
            text="body",
            acl=("u-ada",),
            metadata=metadata,
        )
    )
    store.finalize(prepared, audit_event_id="evt-1", occurred_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_a_document_version_retains_the_exact_artifact_and_receipt() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    _write_version(store, "doc-1")
    receipt = _admit(engine)

    assert store.bind_source_authority("doc-1", receipt=receipt) is True
    retained = store.retained_source_authority("doc-1", 1)
    assert retained is not None
    assert receipt.artifact_identity is not None
    assert retained.artifact_id == receipt.artifact_identity.artifact_id
    assert retained.receipt_id == receipt.receipt_id
    assert retained.receipt_hash == receipt.receipt_hash
    assert retained.source_identity_hash == receipt.source_identity.identity_hash
    assert retained.contract_epoch == receipt.contract_epoch
    assert retained.state == SourceAuthorityState.PRESENT.value


def test_binding_the_same_version_twice_replaces_rather_than_duplicates() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    _write_version(store, "doc-1")
    first = _admit(engine)
    second = _admit(engine, item_id="item-later")
    store.bind_source_authority("doc-1", receipt=first)
    store.bind_source_authority("doc-1", receipt=second)
    retained = store.retained_source_authority("doc-1", 1)
    assert retained is not None
    assert retained.receipt_id == second.receipt_id
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT COUNT(*) FROM ne_document_source_authority_receipts")
        ).scalar_one()
    assert rows == 1


def test_an_admission_never_invents_a_lineage_row_for_an_unknown_document() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    assert store.bind_source_authority("doc-missing", receipt=_admit(engine)) is False
    assert store.retained_source_authority("doc-missing", 1) is None


def test_retention_is_keyed_by_document_and_version() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    _write_version(store, "doc-1")
    _write_version(store, "doc-2")
    receipt = _admit(engine)
    store.bind_source_authority("doc-1", receipt=receipt)
    assert store.retained_source_authority("doc-2", 1) is None
    assert store.retained_source_authority("doc-1", 2) is None


def test_snapshot_lookup_requires_exact_content_acl_and_retained_version() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    snapshot_hash = "d" * 64
    acl_hash = "a" * 64
    source_ref = "drive-1:item-user:item-user"
    _write_version(
        store,
        "doc-1",
        source_ref=source_ref,
        source_snapshot_hash=snapshot_hash,
        source_acl_hash=acl_hash,
    )
    receipt = _admit(engine)
    assert store.bind_source_authority("doc-1", receipt=receipt) is True

    retained = store.retained_source_authority_for_snapshot(
        source_system="m365-sharepoint",
        source_ref=source_ref,
        source_snapshot_hash=snapshot_hash,
        source_acl_hash=acl_hash,
    )
    assert retained is not None
    assert retained.document_id == "doc-1"
    assert retained.version_seq == 1
    assert retained.receipt_id == receipt.receipt_id

    misses = (
        {"source_system": "foreign-source"},
        {"source_ref": "drive-1:other:other"},
        {"source_snapshot_hash": "e" * 64},
        {"source_acl_hash": "b" * 64},
    )
    exact = {
        "source_system": "m365-sharepoint",
        "source_ref": source_ref,
        "source_snapshot_hash": snapshot_hash,
        "source_acl_hash": acl_hash,
    }
    for mismatch in misses:
        assert store.retained_source_authority_for_snapshot(**(exact | mismatch)) is None


def test_snapshot_without_retained_admission_is_unavailable() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    source_ref = "drive-1:item-user:item-user"
    snapshot_hash = hashlib.sha256(b"snapshot").hexdigest()
    acl_hash = hashlib.sha256(
        json.dumps({"acl": ["u-ada"]}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_version(
        store,
        "doc-1",
        source_ref=source_ref,
        source_snapshot_hash=snapshot_hash,
        source_acl_hash=acl_hash,
    )
    assert (
        store.retained_source_authority_for_snapshot(
            source_system="m365-sharepoint",
            source_ref=source_ref,
            source_snapshot_hash=snapshot_hash,
            source_acl_hash=acl_hash,
        )
        is None
    )


def test_stamping_a_chunk_leaves_the_bound_document_version_untouched(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    """The ordering hazard, closed: ``curate`` mints no version for ``bind`` to lose.

    A disclosure stamp can only be applied AFTER ``finalize`` returns the receipt
    it is derived from, which is after the governed write already bound this
    document version to that admission. If applying it minted a NEW version, the
    retained authority row would be stranded on the old one and every
    ``retained_source_authority_for_snapshot`` lookup — the T2 graph-evidence
    path — would go dark. It does not: ``curate`` rewrites the chunk in place
    through the audited path and never touches lineage, which only ``upsert``
    prepares and finalizes.
    """
    engine = _engine()
    lineage = SqlAlchemyDocumentLineageStore(engine)
    contract = ShapeContract(
        name="ne.connect.record",
        version="1",
        json_schema={"type": "object", "required": ["content"]},
    )
    stack = write_stack(
        contracts={contract.name: contract},
        default_shape=contract.name,
        default_provenance_class=ProvenanceClass.INGESTED,
        lineage=lineage,
    )
    source_ref = "drive-1:item-user:item-user"
    snapshot_hash = "d" * 64
    acl_hash = "a" * 64
    written = asyncio.run(
        stack.writer.upsert(
            [
                CuratedKnowledgeItem(
                    item_id="item-user",
                    content=json.dumps({"body": "Consumer Auto Collections Policy"}),
                    metadata={
                        "connector": "m365-sharepoint",
                        "source_system": "m365-sharepoint",
                        "source_ref": source_ref,
                        "source_snapshot_hash": snapshot_hash,
                        "source_acl_hash": acl_hash,
                    },
                    provenance="connector:m365-sharepoint:item-user",
                    provenance_class=ProvenanceClass.INGESTED,
                    acl=("u-ada",),
                )
            ]
        )
    )
    assert written.accepted == ("item-user",)

    receipt = _admit(engine)
    assert lineage.bind_source_authority("item-user", receipt=receipt) is True

    def _snapshot_lookup() -> object:
        return lineage.retained_source_authority_for_snapshot(
            source_system="m365-sharepoint",
            source_ref=source_ref,
            source_snapshot_hash=snapshot_hash,
            source_acl_hash=acl_hash,
        )

    before = _snapshot_lookup()
    assert before is not None

    stamp = stamp_from_receipt(receipt)
    assert stamp is not None
    asyncio.run(
        stack.writer.curate(
            "item-user",
            {"metadata": {SOURCE_AUTHORITY_STAMP_METADATA_KEY: stamp.model_dump(mode="json")}},
            provenance="connector:m365-sharepoint:source-authority-stamp",
        )
    )

    # The binding is exactly where finalize left it.
    assert _snapshot_lookup() == before
    assert lineage.retained_source_authority("item-user", 1) is not None
    with engine.begin() as conn:
        versions = conn.execute(
            sa.text("SELECT COUNT(*) FROM ne_document_versions WHERE document_id = 'item-user'")
        ).scalar_one()
    assert versions == 1

    # And the chunk now carries the stamp, over unchanged content.
    chunk = asyncio.run(stack.store.get(["item-user"]))[0]
    retained_stamp = chunk.metadata[SOURCE_AUTHORITY_STAMP_METADATA_KEY]
    assert SourceAuthorityStamp.model_validate(retained_stamp) == stamp
    assert "Consumer Auto Collections Policy" in chunk.text


def test_an_upsert_may_not_present_a_source_authority_stamp_of_its_own(
    write_stack: Callable[..., WriteStack], frozen_config: str
) -> None:
    """Only the post-admission curate authors a stamp — never the item written.

    The paired mutation for the write path: a connector mapper that supplied its
    own stamp would be handing the read filter a disclosure decision the server
    never made.
    """
    from ne.brain.write import KnowledgeWriteError

    engine = _engine()
    stamp = stamp_from_receipt(_admit(engine))
    assert stamp is not None
    contract = ShapeContract(
        name="ne.connect.record",
        version="1",
        json_schema={"type": "object", "required": ["content"]},
    )
    stack = write_stack(
        contracts={contract.name: contract},
        default_shape=contract.name,
        default_provenance_class=ProvenanceClass.INGESTED,
    )
    forged = CuratedKnowledgeItem(
        item_id="item-forged",
        content=json.dumps({"body": "anything"}),
        metadata={
            "connector": "m365-sharepoint",
            SOURCE_AUTHORITY_STAMP_METADATA_KEY: stamp.model_dump(mode="json"),
        },
        provenance="connector:m365-sharepoint:item-forged",
        provenance_class=ProvenanceClass.INGESTED,
        acl=("u-ada",),
    )
    with pytest.raises(KnowledgeWriteError, match="never presented by the item"):
        asyncio.run(stack.writer.upsert([forged]))
    assert asyncio.run(stack.store.get(["item-forged"])) == []


# ---------------------------------------------------------------------------
# POST_DEMO #13: the version stamp a served answer's contributor stamps resolve.
#
# The projection used to go through the stamp's ``authority_receipt_id`` and the
# ``ne_document_source_authority_receipts`` binding.  That binding keeps ONE row
# per (document_id, version_seq) and REBINDS it on every re-admission, so a
# receipt id retained by an older read stops resolving the moment the same
# version is re-admitted.  Measured on the live box 2026-08-28: a re-sync in the
# 21:00 hour rebound 248 of 252 rows, and NONE of the 45 receipt ids retained by
# the graph vertices built at 16:43 resolved any more.  The snapshot identity is
# content- and ACL-derived, never rebinds, and resolved all 45.


def test_artifact_keys_resolve_the_exact_version_a_stamp_was_served_from() -> None:
    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    snapshot_hash = "d" * 64
    acl_hash = "a" * 64
    _write_version(
        store,
        "doc-1",
        source_ref="drive-1:item-user:item-user",
        source_snapshot_hash=snapshot_hash,
        source_acl_hash=acl_hash,
    )

    key = ("m365-sharepoint", snapshot_hash, acl_hash)
    bound = store.versions_for_artifacts([key])
    assert set(bound) == {key}
    assert bound[key].document_id == "doc-1"
    assert bound[key].version_seq == 1
    assert bound[key].display_name

    # Every component of the key is load-bearing; a miss is absence, not a guess.
    for miss in (
        ("foreign-source", snapshot_hash, acl_hash),
        ("m365-sharepoint", "e" * 64, acl_hash),
        ("m365-sharepoint", snapshot_hash, "b" * 64),
    ):
        assert store.versions_for_artifacts([miss]) == {}
    assert store.versions_for_artifacts([]) == {}


def test_a_rebound_receipt_does_not_disturb_the_snapshot_resolution() -> None:
    """The exact live failure: re-admission rebinds the receipt, not the snapshot."""

    engine = _engine()
    store = SqlAlchemyDocumentLineageStore(engine)
    snapshot_hash = "d" * 64
    acl_hash = "a" * 64
    _write_version(
        store,
        "doc-1",
        source_ref="drive-1:item-user:item-user",
        source_snapshot_hash=snapshot_hash,
        source_acl_hash=acl_hash,
    )
    first = _admit(engine)
    assert store.bind_source_authority("doc-1", receipt=first) is True

    key = ("m365-sharepoint", snapshot_hash, acl_hash)
    before = store.versions_for_artifacts([key])

    # A later re-admission of the SAME version replaces the receipt binding --
    # this is what silently emptied the version stamp on the live box.
    second = _admit(engine, item_id="item-later")
    assert store.bind_source_authority("doc-1", receipt=second) is True
    retained = store.retained_source_authority("doc-1", 1)
    assert retained is not None
    assert retained.receipt_id == second.receipt_id
    assert retained.receipt_id != first.receipt_id

    assert store.versions_for_artifacts([key]) == before
