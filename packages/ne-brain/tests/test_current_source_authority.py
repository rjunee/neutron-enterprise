# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE: the current-source authority head, Reader and Writer.

The two ADR-0022 §7 conformance kits are subclassed with the REAL SQLAlchemy
adapter, so their assertions are never re-enumerated by hand here. What this file
adds on top is the deployment-construction-identity contract, which no kit covers:
a replica or a restore of the same history reuses one opaque id, an independently
initialized deployment mints a different one, and foreign history is rejected in
BOTH directions that matter — the Reader leaves it Unknown *and* composition
fails rather than joining two histories.

The concurrency and supersession proofs that need genuinely interleaved
transactions live in ``test_current_source_authority_postgres.py``: SQLite's
global writer lock serializes exactly the interleavings under test there, so a
SQLite "concurrency" proof would pass against a broken implementation too.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from ne.brain.current_source_authority import (
    ForeignConstructionIdentityError,
    SourceAuthorityClaimConflictError,
    SourceAuthorityError,
    SourceAuthorityFenceError,
    SourceReconciliationScopeBusyError,
    SqlAlchemyCurrentSourceAuthorityReader,
    SqlAlchemyCurrentSourceAuthorityWriter,
    bind_deployment_construction_id,
    claim_or_load_deployment_construction_id,
    ensure_source_authority_schema,
    ne_source_authority_receipts,
    ne_source_reconciliation_claims,
    publish_governing_contract_epoch,
)
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    GovernedLocalT3SourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityDecision,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceDeletionObservation,
    SourceIdentity,
    SourceReconciliationClaim,
    SourceReconciliationScope,
)
from ne.contracts.testing.source_authority import (
    CurrentSourceAuthorityReaderContractKit,
    CurrentSourceAuthorityWriterContractKit,
)

DEPLOYMENT = "deployment-alpha"
CONNECTOR = "connector-instance-1"
ACL_A = "a" * 64
ACL_B = "b" * 64


def memory_engine() -> sa.Engine:
    """One shared in-memory SQLite database across every checkout."""
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_source_authority_schema(engine)
    return engine


def t3_identity(
    contributor: str = "row-1", *, generation: int = 1, deployment: str = DEPLOYMENT
) -> SourceIdentity:
    return SourceIdentity(
        deployment_id=deployment,
        source_kind="governed_local_t3",
        connector_instance_id=CONNECTOR,
        point=GovernedLocalT3SourcePoint(dataset_name="ledger", contributor_key=contributor),
        generation=generation,
    )


def t3_contract(*, descriptor_hash: str = "c" * 64, version: int = 1) -> SourceContractIdentity:
    return SourceContractIdentity(
        source_kind="governed_local_t3",
        descriptor_schema_version=version,
        descriptor_hash=descriptor_hash,
    )


def t3_artifact(
    identity: SourceIdentity,
    contract: SourceContractIdentity,
    *,
    content: bytes,
    acl_snapshot_hash: str = ACL_A,
    source_snapshot_hash: str = "d" * 64,
) -> SourceContentArtifactIdentity:
    return SourceContentArtifactIdentity(
        source_identity=identity,
        source_snapshot_hash=source_snapshot_hash,
        content_snapshot_hash=hashlib.sha256(content).hexdigest(),
        acl_snapshot_hash=acl_snapshot_hash,
        source_contract=contract,
        artifact_schema_version=1,
    )


def reconciliation_scope(*path: str) -> SourceReconciliationScope:
    return SourceReconciliationScope(path=path or ("ledger",))


def admission_fence(
    claim: SourceReconciliationClaim,
    identity: SourceIdentity,
    *,
    contract_epoch: int = 1,
    source_revision: str = "rev-1",
) -> SourceAdmissionFence:
    return SourceAdmissionFence(
        deployment_id=identity.deployment_id,
        connector_instance_id=identity.connector_instance_id,
        source_generation=identity.generation,
        external_source_revision=source_revision,
        contract_epoch=contract_epoch,
        reconciliation_scope=claim.scope,
        reconciliation_epoch=claim.reconciliation_epoch,
        fencing_token=claim.fencing_token,
    )


def present_candidate(
    identity: SourceIdentity,
    contract: SourceContractIdentity,
    artifact: SourceContentArtifactIdentity | None,
    *,
    source_revision: str = "rev-1",
    contract_epoch: int = 1,
    acl_snapshot_hash: str = ACL_A,
    as_of: datetime | None = None,
) -> SourceAuthorityCandidate:
    return SourceAuthorityCandidate(
        source_identity=identity,
        artifact_identity=artifact,
        source_revision=source_revision,
        source_contract=contract,
        contract_epoch=contract_epoch,
        acl_snapshot_hash=acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        authority_as_of=as_of or (datetime.now(UTC) - timedelta(minutes=5)),
    )


def stamp_for(receipt: CurrentSourceAuthorityReceipt):
    """The immutable cross-package stamp a consumer would have retained."""
    from ne.contracts.source_authority import SourceAuthorityStamp

    assert receipt.artifact_identity is not None
    return SourceAuthorityStamp(
        source_identity=receipt.source_identity,
        artifact_identity=receipt.artifact_identity,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=receipt.source_contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def admit(
    writer: SqlAlchemyCurrentSourceAuthorityWriter,
    identity: SourceIdentity,
    contract: SourceContractIdentity,
    artifact: SourceContentArtifactIdentity | None,
    *,
    scope_path: tuple[str, ...] = ("ledger",),
    source_revision: str = "rev-1",
    contract_epoch: int = 1,
    acl_snapshot_hash: str = ACL_A,
    expected_head_revision: str | None = None,
    as_of: datetime | None = None,
) -> CurrentSourceAuthorityReceipt:
    """Claim, fence and finalize one admission; return the immutable receipt.

    ``as_of`` is explicit because ``authority_as_of`` is part of the receipt
    identity: an observation re-read at a later instant is a NEW immutable
    receipt, so a test that means to vary only the fence must pin the instant or
    it is varying two things at once.
    """

    async def scenario() -> CurrentSourceAuthorityReceipt:
        claim = await writer.claim(
            reconciliation_scope(*scope_path),
            expected_head_revision=expected_head_revision,
            lease_seconds=60,
        )
        receipt = await writer.finalize(
            claim,
            admission_fence(
                claim, identity, contract_epoch=contract_epoch, source_revision=source_revision
            ),
            present_candidate(
                identity,
                contract,
                artifact,
                source_revision=source_revision,
                contract_epoch=contract_epoch,
                acl_snapshot_hash=acl_snapshot_hash,
                as_of=as_of,
            ),
        )
        await writer.release(claim)
        return receipt

    return asyncio.run(scenario())


# --- ADR-0022 §7 conformance kits, subclassed with the real adapter ----------


class TestCurrentSourceAuthorityReaderContract(CurrentSourceAuthorityReaderContractKit):
    def setup_method(self) -> None:
        self.engine = memory_engine()
        self.construction_id = claim_or_load_deployment_construction_id(self.engine)
        writer = SqlAlchemyCurrentSourceAuthorityWriter(self.engine, self.construction_id)
        contract = t3_contract()
        identity = t3_identity("row-1")
        artifact = t3_artifact(identity, contract, content=b"admitted row one")
        self._authorized = stamp_for(admit(writer, identity, contract, artifact))
        # A second genuinely admitted row: the "foreign same shape" stamp names
        # THIS source but carries the other row's receipt identity, so it is
        # structurally valid and still not the current admitted truth.
        other_identity = t3_identity("row-2")
        other_artifact = t3_artifact(other_identity, contract, content=b"admitted row two")
        other = admit(
            writer,
            other_identity,
            contract,
            other_artifact,
            scope_path=("ledger", "row-2"),
            source_revision="rev-2",
        )
        self._foreign = stamp_for(other).model_copy(
            update={
                "authority_receipt_id": self._authorized.authority_receipt_id,
                "authority_receipt_hash": self._authorized.authority_receipt_hash,
            }
        )
        # Never admitted at all — no head exists for it.
        never_identity = t3_identity("row-3")
        never_artifact = t3_artifact(never_identity, contract, content=b"never admitted")
        self._unknown = self._authorized.model_copy(
            update={"source_identity": never_identity, "artifact_identity": never_artifact}
        )

    def create_reader(self) -> SqlAlchemyCurrentSourceAuthorityReader:
        return SqlAlchemyCurrentSourceAuthorityReader(self.engine, self.construction_id)

    def authorized_stamp(self):
        return self._authorized

    def unknown_stamp(self):
        return self._unknown

    def foreign_same_shape_stamp(self):
        return self._foreign


class TestCurrentSourceAuthorityWriterContract(CurrentSourceAuthorityWriterContractKit):
    def setup_method(self) -> None:
        self.engine = memory_engine()
        self.construction_id = claim_or_load_deployment_construction_id(self.engine)
        self._identity = t3_identity("row-1")
        self._contract = t3_contract()
        self._artifact = t3_artifact(self._identity, self._contract, content=b"kit content")

    def create_writer(self) -> SqlAlchemyCurrentSourceAuthorityWriter:
        return SqlAlchemyCurrentSourceAuthorityWriter(self.engine, self.construction_id)

    def scope(self) -> SourceReconciliationScope:
        return reconciliation_scope("ledger")

    def fence(self, claim: SourceReconciliationClaim) -> SourceAdmissionFence:
        return admission_fence(claim, self._identity)

    def candidate(self) -> SourceAuthorityCandidate:
        return present_candidate(self._identity, self._contract, self._artifact)


# --- deployment construction identity ----------------------------------------


def test_bootstrap_claim_or_load_returns_one_id_to_every_replica() -> None:
    """A second root booting on the same database LOADS, never mints."""
    engine = memory_engine()
    first = claim_or_load_deployment_construction_id(engine)
    second = claim_or_load_deployment_construction_id(engine)
    assert first == second
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT COUNT(*) FROM ne_source_authority_deployment")
        ).scalar_one()
    assert rows == 1


def test_an_independently_initialized_deployment_mints_a_different_id() -> None:
    """Two histories must be distinguishable, or a restore could silently merge."""
    assert claim_or_load_deployment_construction_id(
        memory_engine()
    ) != claim_or_load_deployment_construction_id(memory_engine())


def _plant_foreign_history(engine: sa.Engine) -> None:
    """Rewrite an admitted receipt's construction id, as a foreign restore would."""
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    admit(writer, identity, contract, t3_artifact(identity, contract, content=b"foreign"))
    with engine.begin() as conn:
        conn.execute(
            sa.update(ne_source_authority_receipts).values(construction_id="another-deployment")
        )


def test_a_clean_history_composes_and_returns_its_construction_id() -> None:
    """Positive control: the guard below rejects foreign history, not all history."""
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    admit(writer, identity, contract, t3_artifact(identity, contract, content=b"native"))
    assert bind_deployment_construction_id(engine) == construction_id


def test_foreign_construction_history_fails_composition() -> None:
    """Pre-demo has no import-as-new: a foreign history refuses to be joined."""
    engine = memory_engine()
    _plant_foreign_history(engine)
    with pytest.raises(ForeignConstructionIdentityError):
        bind_deployment_construction_id(engine)


def test_foreign_construction_history_also_leaves_that_authority_unknown() -> None:
    """The second half of the claim: a root that started anyway still discloses nothing.

    Composition failing is not sufficient on its own — the Reader is what a
    disclosure path calls, and it must not authorize a receipt it cannot attribute
    to this deployment's own history.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    receipt = admit(writer, identity, contract, t3_artifact(identity, contract, content=b"native"))
    stamp = stamp_for(receipt)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction_id)
    before = asyncio.run(reader.resolve(stamp, principal_id="p", access_context_hash=ACL_A))
    assert before.decision is SourceAuthorityDecision.AUTHORIZED
    with engine.begin() as conn:
        conn.execute(
            sa.update(ne_source_authority_receipts).values(construction_id="another-deployment")
        )
    after = asyncio.run(reader.resolve(stamp, principal_id="p", access_context_hash=ACL_A))
    assert after.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    assert after.current_receipt is None
    assert reader.load_receipt(receipt.receipt_id) is None


# --- admission fence and receipt-byte stability ------------------------------


def test_the_admission_fence_never_enters_the_immutable_receipt_bytes() -> None:
    """A retry under a NEW claim reproduces the receipt byte for byte.

    The fence is an operation authorization, so two admissions of the same
    observed truth under two different fencing tokens must be the same receipt —
    if any fence component leaked into the receipt, the second admission would
    mint a second id and hash for one historical fact.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"stable bytes")
    observed = datetime.now(UTC) - timedelta(minutes=5)
    first = admit(writer, identity, contract, artifact, as_of=observed)
    second = admit(
        writer, identity, contract, artifact, expected_head_revision="rev-1", as_of=observed
    )
    assert first.receipt_id == second.receipt_id
    assert first.receipt_hash == second.receipt_hash
    assert first == second
    dumped = first.model_dump_json()
    assert "fencing" not in dumped and "reconciliation" not in dumped


def test_observation_only_receipt_rotation_preserves_current_truth_authority() -> None:
    """A quiet permission sweep refreshes evidence without invalidating projections.

    The graph and every other source-bearing projection retain an authentic stamp
    from the admission that authored them.  A later sweep of unchanged truth must
    refresh the current receipt's observation instant without withholding all of
    those projections until their content happens to change.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"unchanged truth")
    first_observation = datetime.now(UTC) - timedelta(minutes=5)
    first = admit(writer, identity, contract, artifact, as_of=first_observation)
    refreshed = admit(
        writer,
        identity,
        contract,
        artifact,
        expected_head_revision="rev-1",
        as_of=first_observation + timedelta(minutes=1),
    )

    assert first.receipt_id != refreshed.receipt_id
    resolution = asyncio.run(
        reader.resolve(stamp_for(first), principal_id="p", access_context_hash=ACL_A)
    )
    assert resolution.decision is SourceAuthorityDecision.AUTHORIZED
    assert resolution.current_receipt == refreshed


def test_a_stale_fencing_token_cannot_admit_a_receipt() -> None:
    """A reclaimed scope invalidates the previous holder's fence immediately."""
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"fenced")

    async def scenario() -> None:
        stale = await writer.claim(
            reconciliation_scope("ledger"), expected_head_revision=None, lease_seconds=60
        )
        await writer.release(stale)
        await writer.claim(
            reconciliation_scope("ledger"), expected_head_revision=None, lease_seconds=60
        )
        with pytest.raises(SourceAuthorityFenceError):
            await writer.finalize(
                stale,
                admission_fence(stale, identity),
                present_candidate(identity, contract, artifact),
            )

    asyncio.run(scenario())


def test_a_future_skewed_observation_fails_closed() -> None:
    """DB-authoritative UTC decides: a replica cannot admit tomorrow's truth."""
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"skewed")

    async def scenario() -> None:
        claim = await writer.claim(
            reconciliation_scope("ledger"), expected_head_revision=None, lease_seconds=60
        )
        with pytest.raises(SourceAuthorityFenceError):
            await writer.finalize(
                claim,
                admission_fence(claim, identity),
                present_candidate(
                    identity,
                    contract,
                    artifact,
                    as_of=datetime.now(UTC) + timedelta(hours=1),
                ),
            )

    asyncio.run(scenario())


def test_an_out_of_order_contract_epoch_cannot_move_the_head() -> None:
    """Out-of-order updates fail closed.

    A reconciler that re-presents an older contract epoch is not merely ignored:
    the whole admission transaction is refused, so the receipt it would have
    appended is rolled back with it rather than being retained as history the head
    never pointed at.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"epoch two content")
    admit(writer, identity, contract, artifact, contract_epoch=2)
    stale_artifact = t3_artifact(
        identity, contract, content=b"epoch one content", source_snapshot_hash="f" * 64
    )

    async def scenario() -> None:
        claim = await writer.claim(
            reconciliation_scope("ledger"), expected_head_revision="rev-1", lease_seconds=60
        )
        with pytest.raises(SourceAuthorityError):
            await writer.finalize(
                claim,
                admission_fence(claim, identity, contract_epoch=1, source_revision="rev-0"),
                present_candidate(
                    identity,
                    contract,
                    stale_artifact,
                    source_revision="rev-0",
                    contract_epoch=1,
                ),
            )

    asyncio.run(scenario())
    with engine.connect() as conn:
        receipts = (
            conn.execute(sa.select(ne_source_authority_receipts.c.contract_epoch)).scalars().all()
        )
    # The refused admission left NO retained receipt behind.
    assert list(receipts) == [2]


def test_an_identity_bearing_deletion_carries_the_last_admitted_acl_forward() -> None:
    """Absence is not deletion, and a deletion is not an ACL change.

    Minting a blank ACL snapshot on the tombstone would silently record a wider
    envelope than the source ever had.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"present", acl_snapshot_hash=ACL_B)
    present = admit(writer, identity, contract, artifact, acl_snapshot_hash=ACL_B)
    assert present.acl_snapshot_hash == ACL_B

    async def scenario() -> CurrentSourceAuthorityReceipt:
        claim = await writer.claim(
            reconciliation_scope("ledger"), expected_head_revision="rev-1", lease_seconds=60
        )
        return await writer.finalize(
            claim,
            admission_fence(claim, identity, source_revision="rev-2"),
            SourceDeletionObservation(
                source_identity=identity,
                source_revision="rev-2",
                observed_at=datetime.now(UTC) - timedelta(minutes=1),
                source_contract=contract,
                contract_epoch=1,
            ),
        )

    tombstone = asyncio.run(scenario())
    assert tombstone.state is SourceAuthorityState.DELETED
    assert tombstone.artifact_identity is None
    assert tombstone.acl_snapshot_hash == ACL_B


def test_an_m365_admission_without_a_configured_freshness_sla_fails_closed() -> None:
    """M365 authority is SLA-bound by contract; a writer with none must refuse."""
    from ne.contracts.source_authority import M365SourcePoint

    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    identity = SourceIdentity(
        deployment_id=DEPLOYMENT,
        source_kind="m365-sharepoint",
        connector_instance_id=CONNECTOR,
        point=M365SourcePoint(
            graph_tenant_id="tenant", site_id="site", drive_id="drive", item_id="item"
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="e" * 64
    )
    artifact = t3_artifact(identity, contract, content=b"m365 content")

    async def scenario() -> None:
        claim = await writer.claim(
            reconciliation_scope("sites"), expected_head_revision=None, lease_seconds=60
        )
        with pytest.raises(SourceAuthorityError):
            await writer.finalize(
                claim,
                admission_fence(claim, identity),
                present_candidate(identity, contract, artifact),
            )

    asyncio.run(scenario())


# --- the governing contract epoch a Reader cannot see for itself -------------


def _governing_engine() -> tuple[sa.Engine, str, SqlAlchemyCurrentSourceAuthorityWriter]:
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    return engine, construction_id, SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)


CONNECTOR_SCOPE_HASH = "f" * 64


def test_a_governing_epoch_ahead_of_the_stamp_is_unknown_not_a_refusal() -> None:
    """The contract head advanced; nothing has been admitted under the new epoch.

    The source-authority head still names the epoch-1 receipt, so every check the
    Reader could make on its own agrees with the stamp. The binding is what says
    the answer is no longer current — and "not yet re-read" is Unknown, never a
    withdrawal.
    """
    engine, construction_id, writer = _governing_engine()
    contract = t3_contract()
    identity = t3_identity("row-1")
    receipt = admit(writer, identity, contract, t3_artifact(identity, contract, content=b"one"))
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction_id)
    stamp = stamp_for(receipt)
    publish_governing_contract_epoch(
        engine,
        identity_hash=identity.identity_hash,
        contract_scope_hash=CONNECTOR_SCOPE_HASH,
        contract_epoch=1,
    )
    before = asyncio.run(reader.resolve(stamp, principal_id="p", access_context_hash=ACL_A))
    assert before.decision is SourceAuthorityDecision.AUTHORIZED

    publish_governing_contract_epoch(
        engine,
        identity_hash=identity.identity_hash,
        contract_scope_hash=CONNECTOR_SCOPE_HASH,
        contract_epoch=2,
    )
    after = asyncio.run(reader.resolve(stamp, principal_id="p", access_context_hash=ACL_A))
    assert after.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
    assert after.decision is not SourceAuthorityDecision.PERMANENT_WITHHELD
    assert after.current_receipt is None and after.safe_reason is None
    # The retained receipt itself is untouched by any of this.
    assert reader.load_receipt(receipt.receipt_id) == receipt


def test_admitting_under_the_new_epoch_authorizes_the_exact_new_stamp() -> None:
    """Completion restores authority for the new stamp and only for it."""
    engine, construction_id, writer = _governing_engine()
    contract = t3_contract()
    identity = t3_identity("row-1")
    first = admit(writer, identity, contract, t3_artifact(identity, contract, content=b"one"))
    publish_governing_contract_epoch(
        engine,
        identity_hash=identity.identity_hash,
        contract_scope_hash=CONNECTOR_SCOPE_HASH,
        contract_epoch=2,
    )
    second = admit(
        writer,
        identity,
        contract,
        t3_artifact(identity, contract, content=b"two", source_snapshot_hash="e" * 64),
        source_revision="rev-2",
        contract_epoch=2,
        expected_head_revision="rev-1",
    )
    reader = SqlAlchemyCurrentSourceAuthorityReader(engine, construction_id)
    current = asyncio.run(
        reader.resolve(stamp_for(second), principal_id="p", access_context_hash=ACL_A)
    )
    prior = asyncio.run(
        reader.resolve(stamp_for(first), principal_id="p", access_context_hash=ACL_A)
    )
    assert current.decision is SourceAuthorityDecision.AUTHORIZED
    assert current.current_receipt == second
    assert prior.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN


def test_the_governing_epoch_publish_never_moves_backwards() -> None:
    """Monotonic by the write itself, not by the caller promising to be ordered."""
    engine, _construction_id, _writer = _governing_engine()
    identity = t3_identity("row-1")
    kwargs = {
        "identity_hash": identity.identity_hash,
        "contract_scope_hash": CONNECTOR_SCOPE_HASH,
    }
    assert publish_governing_contract_epoch(engine, contract_epoch=1, **kwargs) == 1
    assert publish_governing_contract_epoch(engine, contract_epoch=3, **kwargs) == 3
    assert publish_governing_contract_epoch(engine, contract_epoch=2, **kwargs) == 3
    # Positive control: forward motion still lands, so the guard is a floor and
    # not a first-write-wins latch.
    assert publish_governing_contract_epoch(engine, contract_epoch=4, **kwargs) == 4


def test_a_governing_binding_is_keyed_by_digests_and_a_positive_epoch() -> None:
    """An unvalidated key would let a caller bind an epoch to arbitrary text."""
    engine, _construction_id, _writer = _governing_engine()
    identity = t3_identity("row-1")
    with pytest.raises(ValueError):
        publish_governing_contract_epoch(
            engine,
            identity_hash="drive-1:item-1",
            contract_scope_hash=CONNECTOR_SCOPE_HASH,
            contract_epoch=1,
        )
    with pytest.raises(ValueError):
        publish_governing_contract_epoch(
            engine,
            identity_hash=identity.identity_hash,
            contract_scope_hash="m365-sharepoint/tenant-1",
            contract_epoch=1,
        )
    with pytest.raises(ValueError):
        publish_governing_contract_epoch(
            engine,
            identity_hash=identity.identity_hash,
            contract_scope_hash=CONNECTOR_SCOPE_HASH,
            contract_epoch=0,
        )
    # Positive control: the well-formed call this triple brackets does bind.
    assert (
        publish_governing_contract_epoch(
            engine,
            identity_hash=identity.identity_hash,
            contract_scope_hash=CONNECTOR_SCOPE_HASH,
            contract_epoch=1,
        )
        == 1
    )


# --- a busy scope and a wrong-view conflict are not the same refusal ---------


def test_a_live_lease_refuses_the_claim_as_a_distinguishable_busy_scope() -> None:
    """The holder keeps the scope, and the loser can tell WHY it lost.

    A scheduled reconciler that fires while its own predecessor is still sweeping
    is ordinary operation, not a broken connector, and the only thing that lets
    the caller skip instead of erroring is this exact subclass. The refusal itself
    is unchanged: the second caller gets no claim, so it can admit nothing.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    scope = reconciliation_scope("ledger")

    async def scenario() -> None:
        held = await writer.claim(scope, expected_head_revision=None, lease_seconds=600)
        with pytest.raises(SourceReconciliationScopeBusyError):
            await writer.claim(scope, expected_head_revision=None, lease_seconds=600)
        # Still a claim conflict: nothing about the control was relaxed.
        assert issubclass(SourceReconciliationScopeBusyError, SourceAuthorityClaimConflictError)
        # And the holder is undisturbed — it kept the scope it was granted.
        with engine.connect() as conn:
            row = conn.execute(sa.select(ne_source_reconciliation_claims)).one()
        assert row.fencing_token == held.fencing_token
        assert row.reconciliation_epoch == held.reconciliation_epoch

    asyncio.run(scenario())


def test_a_moved_head_stays_a_plain_conflict_that_no_caller_may_skip() -> None:
    """The paired refusal: a caller whose view of authority is WRONG still errors.

    The lease has lapsed, so nothing is busy — this claim fails because the
    scope's head moved out from under the revision the caller presented. Skipping
    that would let a reconciler resume against authority it has not read, so it
    must NOT be reported as a busy scope.
    """
    engine = memory_engine()
    construction_id = claim_or_load_deployment_construction_id(engine)
    writer = SqlAlchemyCurrentSourceAuthorityWriter(engine, construction_id)
    contract = t3_contract()
    identity = t3_identity("row-1")
    artifact = t3_artifact(identity, contract, content=b"revision one")
    scope = reconciliation_scope("ledger")

    async def scenario() -> None:
        claim = await writer.claim(scope, expected_head_revision=None, lease_seconds=600)
        await writer.finalize(
            claim,
            admission_fence(claim, identity),
            present_candidate(identity, contract, artifact),
        )
        # The finalize pinned the claim row at "rev-1"; release it so the next
        # claim is refused for the head, not for the lease.
        await writer.release(claim)
        with pytest.raises(SourceAuthorityClaimConflictError) as raised:
            await writer.claim(scope, expected_head_revision=None, lease_seconds=600)
        assert not isinstance(raised.value, SourceReconciliationScopeBusyError)
        # Presenting the head the scope actually sits at is granted.
        resumed = await writer.claim(scope, expected_head_revision="rev-1", lease_seconds=600)
        assert resumed.expected_head_revision == "rev-1"

    asyncio.run(scenario())
