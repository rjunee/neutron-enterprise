# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Frozen source-authority identities, receipts, and least-privilege seams.

This module is deliberately implementation-free.  It gives supported source producers,
artifact stores, runtime delivery guards, and read-time selectors one additive vocabulary
without changing the frozen KnowledgeRead/KnowledgeWrite/ChunkStore Protocols.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.contracts.types import RetrievalResult

__all__ = [
    "NO_SOURCE_STATUS",
    "SOURCE_AUTHORITY_STAMP_METADATA_KEY",
    "SOURCE_STATUS_METADATA_KEY",
    "AuthorizedArtifact",
    "AuthorizedArtifactPage",
    "AuthorizedArtifactPageStatus",
    "AuthorizedContinuationReplay",
    "AuthorizedContinuationState",
    "AuthorizedContinuationTokenCodec",
    "AuthorizedContinuationTokenLocator",
    "AuthorizedRetrievalEnvelope",
    "CurrentAuthorizedArtifactSelector",
    "CurrentSourceAuthorityReader",
    "CurrentSourceAuthorityReceipt",
    "CurrentSourceAuthorityWriter",
    "CurrentSourceContractHead",
    "EvidenceAssurance",
    "GovernedLocalT3SourcePoint",
    "ImmutableArtifactCreateReceipt",
    "ImmutableKnowledgeArtifactWrite",
    "M365SourcePoint",
    "ProtectedClaimSliceRef",
    "ProtectedSourceExecutionWriter",
    "ProtectedSourceInvocationRef",
    "ProtectedSourceOutputRef",
    "ProtectedSourceOutputWriter",
    "ProtectedWorkflowStateRef",
    "SalesforceSourcePoint",
    "SourceAdmissionFence",
    "SourceAuthorityCandidate",
    "SourceAuthorityDecision",
    "SourceAuthorityResolution",
    "SourceAuthorityStamp",
    "SourceAuthorityState",
    "SourceBoundApprovalResolution",
    "SourceBoundDirectResolution",
    "SourceBoundDispatchResolution",
    "SourceBoundEvidenceProjectionResolution",
    "SourceBoundEvidenceProjectionResolver",
    "SourceBoundModelSendResolution",
    "SourceBoundResultResolver",
    "SourceBoundToolResultRef",
    "SourceBoundToolResultSetRef",
    "SourceBoundWorkflowProjectionResolution",
    "SourceContentArtifactIdentity",
    "SourceContractIdentity",
    "SourceDeletionObservation",
    "SourceIdentity",
    "SourceIdentityPoint",
    "SourceReconciliationClaim",
    "SourceReconciliationScope",
    "VerifiedEmptyResultReceipt",
    "VerifiedRowContributorReceipt",
    "canonical_source_authority_stamps",
    "stamp_from_receipt",
]

#: The ONE server-authored chunk-metadata key carrying a record's
#: :class:`SourceAuthorityStamp`. It is written by the governed ingest path from
#: the receipt its own authority admission just returned, and by nothing else:
#: ``KnowledgeWriter.upsert`` REFUSES an item carrying it, so a connector mapper
#: cannot present a stamp of its own choosing. Its sibling below is the other
#: half of the same decision — a chunk discloses on a stamp OR on the source-free
#: mark, never on the absence of both.
SOURCE_AUTHORITY_STAMP_METADATA_KEY = "ne_source_authority_stamp"

#: The ONE server-authored chunk-metadata key naming a record's source-authority
#: class. It is written by the governed ingest path from the code-authored
#: connector registry and by nothing else: a connector, a record payload, a
#: curate patch or a request may never set it (a source that could label itself
#: source-free would be its own authority). Its ONLY recognised value is
#: :data:`NO_SOURCE_STATUS`; every other value, and its absence, means the
#: record is source-bearing and must carry a :class:`SourceAuthorityStamp` to be
#: disclosed. Shares the vocabulary of the verified-template
#: ``answer.source_status`` (``"source_bearing"`` / ``"no_source"``).
SOURCE_STATUS_METADATA_KEY = "source_status"

#: The one value of :data:`SOURCE_STATUS_METADATA_KEY` that admits a record with
#: no source-authority stamp: the deployment itself supplied the content (a local
#: fixture/CSV lane), so there is no external authority head to re-resolve and the
#: governed write's own ACL is the disclosure boundary.
NO_SOURCE_STATUS = "no_source"

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueId = Annotated[str, Field(min_length=1, max_length=512)]
BoundedText = Annotated[str, Field(min_length=1, max_length=4096)]
ContinuationKeyId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
ExactDecimal = Annotated[Decimal, Field(strict=True)]

_MAX_DECIMAL_DIGITS = 76
_MAX_DECIMAL_SCALE = 38


def _field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


def _canonical_hash(domain: str, values: Sequence[str]) -> str:
    material = b"".join((_field(domain), *(_field(value) for value in values)))
    return hashlib.sha256(material).hexdigest()


def _bind_hash(model: BaseModel, name: str, expected: str) -> None:
    supplied = getattr(model, name)
    if supplied and supplied != expected:
        raise ValueError(f"{name} does not match canonical contract bytes")
    object.__setattr__(model, name, expected)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be UTC")


def _canonical_decimal(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if value == 0:
        return Decimal(0)
    sign, raw_digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):  # finite values always use an integer exponent
        raise ValueError(f"{name} must be finite")
    exponent = raw_exponent
    digits = list(raw_digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    normalized = Decimal((sign, tuple(digits), exponent))
    precision = len(digits)
    scale = -exponent
    if precision > _MAX_DECIMAL_DIGITS or abs(scale) > _MAX_DECIMAL_SCALE:
        raise ValueError(f"{name} exceeds canonical precision/scale bounds")
    return normalized


def _canonical_decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _stamp_hash(domain: str, stamp: SourceAuthorityStamp) -> str:
    return _canonical_hash(
        domain,
        (
            stamp.source_identity.identity_hash,
            stamp.artifact_identity.artifact_id,
            stamp.selected_head_revision,
            stamp.acl_snapshot_hash,
            stamp.source_contract.identity_hash,
            str(stamp.contract_epoch),
            stamp.authority_receipt_id,
            stamp.authority_receipt_hash,
        ),
    )


def stamp_from_receipt(
    receipt: CurrentSourceAuthorityReceipt,
) -> SourceAuthorityStamp | None:
    """Derive the retained stamp from the receipt an admission just produced.

    The ONE canonical derivation. It is a pure projection of an immutable
    receipt — it verifies nothing, because a producer holding the receipt its own
    ``finalize`` returned under its own live claim has nothing to verify it
    against; checking a receipt against itself proves only that it was copied
    correctly. Callers that instead RECONSTRUCT a stamp for a record they merely
    hold (``ne.api.platform``'s graph-sync provider, which starts from a record
    and looks a receipt up through the retained T1 lineage row) must do their own
    binding cross-checks BEFORE calling this, and then re-resolve the result
    against the current head.

    Returns ``None`` for a receipt that selects no artifact — a withdrawn or
    non-present source has no disclosable identity, and inventing one is exactly
    the fabrication the stamp exists to prevent.
    """

    artifact = receipt.artifact_identity
    if receipt.state is not SourceAuthorityState.PRESENT or artifact is None:
        return None
    return SourceAuthorityStamp(
        source_identity=receipt.source_identity,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=receipt.source_contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


def canonical_source_authority_stamps(
    stamps: Sequence[SourceAuthorityStamp],
) -> tuple[SourceAuthorityStamp, ...]:
    """Deduplicate and order a complete stamp set using the contract preimage."""

    return tuple(
        sorted(
            set(stamps),
            key=lambda stamp: _stamp_hash("ne.source-authority-stamp-order.v1", stamp),
        )
    )


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceAuthorityState(StrEnum):
    """Immutable observation admitted by the authority producer."""

    PRESENT = "present"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class SourceAuthorityDecision(StrEnum):
    """The only disclosure/execution outcomes exposed by the Reader."""

    AUTHORIZED = "authorized"
    PERMANENT_WITHHELD = "permanent_withheld"
    RETRYABLE_UNKNOWN = "retryable_unknown"


class EvidenceAssurance(StrEnum):
    """How immutable read-time evidence supports the served result.

    This is deliberately independent of graph element assurance
    (``source|confirmed|extracted``). A traversed graph path is inferred
    evidence regardless of how an individual graph element was produced.
    """

    DIRECT = "direct"
    INFERRED = "inferred"
    EXTRACTED = "extracted"


class AuthorizedArtifactPageStatus(StrEnum):
    """Truthful bounded-selection completion status."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EXHAUSTED = "exhausted"


class M365SourcePoint(_Frozen):
    """Exact Graph-native M365 point beneath one admitted connector instance."""

    kind: Literal["m365_sharepoint"] = "m365_sharepoint"
    graph_tenant_id: Annotated[str, Field(min_length=1, max_length=512)]
    site_id: Annotated[str, Field(max_length=512)]
    drive_id: Annotated[str, Field(min_length=1, max_length=512)]
    item_id: Annotated[str, Field(min_length=1, max_length=1024)]


class SalesforceSourcePoint(_Frozen):
    """Exact record point in one provider-identified Salesforce organization."""

    kind: Literal["salesforce"] = "salesforce"
    organization_id: Annotated[str, Field(pattern=r"^00D[A-Za-z0-9]{15}$")]
    object_name: Annotated[str, Field(min_length=1, max_length=512)]
    record_id: Annotated[str, Field(min_length=1, max_length=512)]


class GovernedLocalT3SourcePoint(_Frozen):
    """Exact contributor point in one governed local Meridian dataset instance."""

    kind: Literal["governed_local_t3"] = "governed_local_t3"
    dataset_name: Annotated[str, Field(min_length=1, max_length=512)]
    contributor_key: Annotated[str, Field(min_length=1, max_length=2048)]


SourceIdentityPoint = Annotated[
    M365SourcePoint | SalesforceSourcePoint | GovernedLocalT3SourcePoint,
    Field(discriminator="kind"),
]


class SourceIdentity(_Frozen):
    """Collision-safe, generation-bound identity issued by source authority."""

    schema_version: Annotated[int, Field(ge=1)] = 1
    deployment_id: Annotated[str, Field(min_length=1, max_length=512)]
    source_kind: Annotated[str, Field(min_length=1, max_length=128)]
    connector_instance_id: Annotated[str, Field(min_length=1, max_length=512)]
    point: SourceIdentityPoint
    generation: Annotated[int, Field(ge=1)]
    identity_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_identity(self) -> SourceIdentity:
        values = [
            str(self.schema_version),
            self.deployment_id,
            self.source_kind,
            self.connector_instance_id,
            str(self.generation),
        ]
        if isinstance(self.point, M365SourcePoint):
            if self.source_kind != "m365-sharepoint":
                raise ValueError("M365 source point requires source kind 'm365-sharepoint'")
            values.extend(
                (
                    self.point.kind,
                    self.point.graph_tenant_id,
                    self.point.site_id,
                    self.point.drive_id,
                    self.point.item_id,
                )
            )
        elif isinstance(self.point, SalesforceSourcePoint):
            if self.source_kind != "salesforce":
                raise ValueError("Salesforce source point requires source kind 'salesforce'")
            values.extend(
                (
                    self.point.kind,
                    self.point.organization_id,
                    self.point.object_name,
                    self.point.record_id,
                )
            )
        else:
            if self.source_kind != "governed_local_t3":
                raise ValueError("governed local T3 point requires source kind 'governed_local_t3'")
            values.extend((self.point.kind, self.point.dataset_name, self.point.contributor_key))
        _bind_hash(self, "identity_hash", _canonical_hash("ne.source-identity.v1", values))
        return self


class SourceContractIdentity(_Frozen):
    """Credential-free versioned hash of a supported source's interpretation contract."""

    source_kind: Annotated[str, Field(min_length=1, max_length=128)]
    descriptor_schema_version: Annotated[int, Field(ge=1)]
    descriptor_hash: Sha256
    identity_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_identity(self) -> SourceContractIdentity:
        _bind_hash(
            self,
            "identity_hash",
            _canonical_hash(
                "ne.source-contract-identity.v1",
                (self.source_kind, str(self.descriptor_schema_version), self.descriptor_hash),
            ),
        )
        return self


class SourceContentArtifactIdentity(_Frozen):
    """Immutable ACL- and contract-bound content address.

    Current-head, lease, fencing, attempt, and wall-clock values are structurally absent.
    """

    schema_version: Annotated[int, Field(ge=1)] = 1
    source_identity: SourceIdentity
    source_snapshot_hash: Sha256
    content_snapshot_hash: Sha256
    acl_snapshot_hash: Sha256
    source_contract: SourceContractIdentity
    artifact_schema_version: Annotated[int, Field(ge=1)]
    artifact_id: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_artifact(self) -> SourceContentArtifactIdentity:
        if self.source_contract.source_kind != self.source_identity.source_kind:
            raise ValueError("artifact source and contract kinds differ")
        _bind_hash(
            self,
            "artifact_id",
            _canonical_hash(
                "ne.source-content-artifact.v1",
                (
                    str(self.schema_version),
                    self.source_identity.identity_hash,
                    self.source_snapshot_hash,
                    self.content_snapshot_hash,
                    self.acl_snapshot_hash,
                    self.source_contract.identity_hash,
                    str(self.artifact_schema_version),
                ),
            ),
        )
        return self


class SourceReconciliationScope(_Frozen):
    """Hierarchical reconciliation scope, with component boundaries preserved."""

    schema_version: Annotated[int, Field(ge=1)] = 1
    path: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    scope_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_scope(self) -> SourceReconciliationScope:
        if any(not component or len(component) > 512 for component in self.path):
            raise ValueError("reconciliation scope components must be non-empty and bounded")
        _bind_hash(
            self,
            "scope_hash",
            _canonical_hash(
                "ne.source-reconciliation-scope.v1",
                (str(self.schema_version), str(len(self.path)), *self.path),
            ),
        )
        return self


class SourceReconciliationClaim(_Frozen):
    """CAS/lease-bearing exclusive claim over one reconciliation scope."""

    scope: SourceReconciliationScope
    reconciliation_epoch: Annotated[int, Field(ge=1)]
    expected_head_revision: OpaqueId | None = None
    lease_expires_at: datetime
    fencing_token: OpaqueId

    @model_validator(mode="after")
    def _time_is_utc(self) -> SourceReconciliationClaim:
        _require_utc(self.lease_expires_at, "lease_expires_at")
        return self


class SourceAdmissionFence(_Frozen):
    """Operation-only admission authorization; never immutable evidence identity."""

    deployment_id: OpaqueId
    connector_instance_id: OpaqueId
    source_generation: Annotated[int, Field(ge=1)]
    external_source_revision: OpaqueId
    contract_epoch: Annotated[int, Field(ge=1)]
    reconciliation_scope: SourceReconciliationScope
    reconciliation_epoch: Annotated[int, Field(ge=1)]
    fencing_token: OpaqueId


class CurrentSourceContractHead(_Frozen):
    """Current source contract hash and epoch for a connector scope."""

    connector_scope: SourceReconciliationScope
    source_generation: Annotated[int, Field(ge=1)]
    contract_epoch: Annotated[int, Field(ge=1)]
    source_contract: SourceContractIdentity
    head_revision: OpaqueId


class SourceAuthorityCandidate(_Frozen):
    """Producer-authored candidate presented under an admission fence."""

    source_identity: SourceIdentity
    artifact_identity: SourceContentArtifactIdentity | None = None
    source_revision: OpaqueId
    source_contract: SourceContractIdentity
    contract_epoch: Annotated[int, Field(ge=1)]
    acl_snapshot_hash: Sha256
    state: SourceAuthorityState
    authority_as_of: datetime

    @model_validator(mode="after")
    def _cross_check(self) -> SourceAuthorityCandidate:
        _require_utc(self.authority_as_of, "authority_as_of")
        if self.source_contract.source_kind != self.source_identity.source_kind:
            raise ValueError("candidate source and contract kinds differ")
        if self.state is SourceAuthorityState.PRESENT and self.artifact_identity is None:
            raise ValueError("present authority candidate requires an artifact")
        if self.state is not SourceAuthorityState.PRESENT and self.artifact_identity is not None:
            raise ValueError("non-present authority candidate cannot select an artifact")
        if self.artifact_identity is not None:
            if self.artifact_identity.source_identity != self.source_identity:
                raise ValueError("candidate artifact belongs to another source")
            if self.artifact_identity.source_contract != self.source_contract:
                raise ValueError("candidate artifact uses another contract")
            if self.artifact_identity.acl_snapshot_hash != self.acl_snapshot_hash:
                raise ValueError("candidate ACL does not bind its artifact")
        return self


class SourceDeletionObservation(_Frozen):
    """Identity-bearing deletion observation; never an unlabeled tombstone."""

    source_identity: SourceIdentity
    source_revision: OpaqueId
    observed_at: datetime
    source_contract: SourceContractIdentity
    contract_epoch: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _cross_check(self) -> SourceDeletionObservation:
        _require_utc(self.observed_at, "observed_at")
        if self.source_contract.source_kind != self.source_identity.source_kind:
            raise ValueError("deletion source and contract kinds differ")
        return self


class CurrentSourceAuthorityReceipt(_Frozen):
    """Stable immutable result of a successful authority admission transaction."""

    receipt_schema_version: Annotated[int, Field(ge=1)] = 1
    receipt_id: OpaqueId
    source_identity: SourceIdentity
    artifact_identity: SourceContentArtifactIdentity | None = None
    source_revision: OpaqueId
    source_contract: SourceContractIdentity
    contract_epoch: Annotated[int, Field(ge=1)]
    acl_snapshot_hash: Sha256
    state: SourceAuthorityState
    admitted_at: datetime
    authority_as_of: datetime
    audit_event_id: OpaqueId
    lineage_id: OpaqueId
    freshness_sla_seconds: ExactDecimal | None = None
    receipt_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_receipt(self) -> CurrentSourceAuthorityReceipt:
        _require_utc(self.admitted_at, "admitted_at")
        _require_utc(self.authority_as_of, "authority_as_of")
        if self.authority_as_of > self.admitted_at:
            raise ValueError("future authority_as_of is invalid")
        if self.state is SourceAuthorityState.PRESENT and self.artifact_identity is None:
            raise ValueError("present authority receipt requires an artifact")
        if self.state is not SourceAuthorityState.PRESENT and self.artifact_identity is not None:
            raise ValueError("non-present authority receipt cannot select an artifact")
        if (
            self.source_identity.source_kind == "m365-sharepoint"
            and self.freshness_sla_seconds is None
        ):
            raise ValueError("M365 authority receipt requires a fixed freshness SLA")
        if (
            self.source_identity.source_kind == "governed_local_t3"
            and self.freshness_sla_seconds is not None
        ):
            raise ValueError("governed local T3 authority cannot use a connector freshness SLA")
        if self.source_contract.source_kind != self.source_identity.source_kind:
            raise ValueError("receipt source and contract kinds differ")
        if self.freshness_sla_seconds is not None:
            object.__setattr__(
                self,
                "freshness_sla_seconds",
                _canonical_decimal(
                    self.freshness_sla_seconds,
                    "freshness_sla_seconds",
                    positive=True,
                ),
            )
        artifact_id = ""
        if self.artifact_identity is not None:
            artifact_id = self.artifact_identity.artifact_id
            if self.artifact_identity.source_identity != self.source_identity:
                raise ValueError("receipt artifact belongs to another source")
            if self.artifact_identity.source_contract != self.source_contract:
                raise ValueError("receipt artifact uses another contract")
            if self.artifact_identity.acl_snapshot_hash != self.acl_snapshot_hash:
                raise ValueError("receipt ACL does not bind its artifact")
        _bind_hash(
            self,
            "receipt_hash",
            _canonical_hash(
                "ne.current-source-authority-receipt.v1",
                (
                    str(self.receipt_schema_version),
                    self.receipt_id,
                    self.source_identity.identity_hash,
                    artifact_id,
                    self.source_revision,
                    self.source_contract.identity_hash,
                    str(self.contract_epoch),
                    self.acl_snapshot_hash,
                    self.state.value,
                    self.admitted_at.isoformat(),
                    self.authority_as_of.isoformat(),
                    self.audit_event_id,
                    self.lineage_id,
                    ""
                    if self.freshness_sla_seconds is None
                    else _canonical_decimal_text(self.freshness_sla_seconds),
                ),
            ),
        )
        return self


class SourceAuthorityStamp(_Frozen):
    """Immutable cross-package receipt retained by every source-bearing result."""

    source_identity: SourceIdentity
    artifact_identity: SourceContentArtifactIdentity
    selected_head_revision: OpaqueId
    acl_snapshot_hash: Sha256
    source_contract: SourceContractIdentity
    contract_epoch: Annotated[int, Field(ge=1)]
    authority_receipt_id: OpaqueId
    authority_receipt_hash: Sha256

    @model_validator(mode="after")
    def _cross_check(self) -> SourceAuthorityStamp:
        if self.artifact_identity.source_identity != self.source_identity:
            raise ValueError("stamp artifact belongs to another source")
        if self.artifact_identity.source_contract != self.source_contract:
            raise ValueError("stamp artifact uses another contract")
        if self.artifact_identity.acl_snapshot_hash != self.acl_snapshot_hash:
            raise ValueError("stamp ACL does not bind its artifact")
        return self


class VerifiedRowContributorReceipt(_Frozen):
    """Template/row-bound ordered contributor receipt."""

    receipt_schema_version: Annotated[int, Field(ge=1)] = 1
    template_id: OpaqueId
    template_version: Annotated[int, Field(ge=1)]
    canonical_row_hash: Sha256
    contributor_schema_version: Annotated[int, Field(ge=1)]
    contributors: Annotated[tuple[SourceAuthorityStamp, ...], Field(min_length=1, max_length=128)]
    contributor_count: Annotated[int, Field(ge=1)] = 1
    contributor_set_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""
    receipt_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_receipt(self) -> VerifiedRowContributorReceipt:
        if len(self.contributors) != len(set(self.contributors)):
            raise ValueError("verified row contributors must be unique")
        object.__setattr__(self, "contributor_count", len(self.contributors))
        member_hashes = sorted(
            _stamp_hash("ne.verified-row-contributor-member.v1", stamp)
            for stamp in self.contributors
        )
        _bind_hash(
            self,
            "contributor_set_hash",
            _canonical_hash("ne.verified-row-contributor-set.v1", member_hashes),
        )
        ordered = [
            _stamp_hash("ne.verified-row-contributor-ordered.v1", stamp)
            for stamp in self.contributors
        ]
        _bind_hash(
            self,
            "receipt_hash",
            _canonical_hash(
                "ne.verified-row-contributor-receipt.v1",
                (
                    str(self.receipt_schema_version),
                    self.template_id,
                    str(self.template_version),
                    self.canonical_row_hash,
                    str(self.contributor_schema_version),
                    str(self.contributor_count),
                    self.contributor_set_hash,
                    *ordered,
                ),
            ),
        )
        return self


class VerifiedEmptyResultReceipt(_Frozen):
    """Template/query/dataset-bound receipt for a complete zero-row enumeration.

    An empty answer has no row on which to hang a row-contributor receipt.  Its
    authority is instead the complete current contributor set for the exact scoped
    dataset.  The query arguments and graph set are represented only by canonical
    hashes, keeping source values and ACL labels out of the receipt surface.
    """

    receipt_schema_version: Annotated[int, Field(ge=1)] = 1
    template_id: OpaqueId
    template_version: Annotated[int, Field(ge=1)]
    canonical_args_hash: Sha256
    dataset_graphs_hash: Sha256
    contributor_schema_version: Annotated[int, Field(ge=1)]
    contributors: Annotated[tuple[SourceAuthorityStamp, ...], Field(min_length=1, max_length=128)]
    contributor_count: Annotated[int, Field(ge=1)] = 1
    contributor_set_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""
    receipt_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_receipt(self) -> VerifiedEmptyResultReceipt:
        canonical = canonical_source_authority_stamps(self.contributors)
        if canonical != self.contributors:
            raise ValueError("empty-result contributors must be unique and canonical")
        object.__setattr__(self, "contributor_count", len(self.contributors))
        member_hashes = [
            _stamp_hash("ne.verified-empty-result-contributor-member.v1", stamp)
            for stamp in self.contributors
        ]
        _bind_hash(
            self,
            "contributor_set_hash",
            _canonical_hash("ne.verified-empty-result-contributor-set.v1", member_hashes),
        )
        _bind_hash(
            self,
            "receipt_hash",
            _canonical_hash(
                "ne.verified-empty-result-receipt.v1",
                (
                    str(self.receipt_schema_version),
                    self.template_id,
                    str(self.template_version),
                    self.canonical_args_hash,
                    self.dataset_graphs_hash,
                    str(self.contributor_schema_version),
                    str(self.contributor_count),
                    self.contributor_set_hash,
                ),
            ),
        )
        return self


class ImmutableArtifactCreateReceipt(_Frozen):
    """Create/reuse result with deletion ownership only for the exact creator."""

    artifact_identity: SourceContentArtifactIdentity
    created: bool
    creator_token: OpaqueId | None
    inflight_reference_id: OpaqueId

    @model_validator(mode="after")
    def _ownership(self) -> ImmutableArtifactCreateReceipt:
        if self.created != (self.creator_token is not None):
            raise ValueError("only a newly-created artifact receives a creator token")
        return self


class _StampedReference(_Frozen):
    reference_id: OpaqueId
    stamps: Annotated[tuple[SourceAuthorityStamp, ...], Field(min_length=1, max_length=128)]

    @model_validator(mode="after")
    def _unique_stamps(self) -> _StampedReference:
        if len(self.stamps) != len(set(self.stamps)):
            raise ValueError("source authority stamps must be canonical-deduplicated")
        expected = canonical_source_authority_stamps(self.stamps)
        if self.stamps != expected:
            raise ValueError("source authority stamps must use canonical order")
        return self


class SourceBoundToolResultRef(_StampedReference):
    """Opaque reference to a bounded protected Tool result."""

    result_hash: Sha256


class SourceBoundToolResultSetRef(SourceBoundToolResultRef):
    """Opaque Tool result backed by an ordered complete EvidenceRecord set."""

    evidence_ids: Annotated[tuple[OpaqueId, ...], Field(min_length=1, max_length=1024)]

    @model_validator(mode="after")
    def _unique_evidence_ids(self) -> SourceBoundToolResultSetRef:
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("source-bound result evidence ids must be unique and ordered")
        return self


class ProtectedSourceOutputRef(_StampedReference):
    """Opaque reference to protected source-derived output."""

    output_hash: Sha256


class ProtectedSourceInvocationRef(_StampedReference):
    """Opaque reference to a target-bound approval or sub-agent invocation."""

    invocation_hash: Sha256


class ProtectedWorkflowStateRef(_StampedReference):
    """Opaque reference to protected source-bearing Workflow state."""

    state_hash: Sha256


class ProtectedClaimSliceRef(_StampedReference):
    """Opaque reference to a protected source-bearing claim slice."""

    slice_hash: Sha256


class _Resolution(_Frozen):
    decision: SourceAuthorityDecision
    materialized_text: Annotated[str, Field(max_length=1_000_000)] | None = None
    stamps: Annotated[tuple[SourceAuthorityStamp, ...], Field(max_length=128)] = ()
    safe_reason: Annotated[str, Field(max_length=256)] | None = None

    @model_validator(mode="after")
    def _decision_shape(self) -> _Resolution:
        if self.decision is SourceAuthorityDecision.AUTHORIZED:
            if self.materialized_text is None or not self.stamps:
                raise ValueError("authorized resolution requires material and stamps")
            if self.safe_reason is not None:
                raise ValueError("authorized resolution cannot carry a refusal reason")
        elif self.materialized_text is not None or self.stamps:
            raise ValueError(
                "withheld/unknown resolution cannot carry protected material or stamps"
            )
        return self


class SourceBoundModelSendResolution(_Resolution):
    run_id: OpaqueId
    model_call_attempt_id: OpaqueId


class SourceBoundDirectResolution(_Resolution):
    workflow_execution_id: OpaqueId
    publication_attempt_id: OpaqueId


class SourceBoundApprovalResolution(_Resolution):
    approval_id: OpaqueId
    decision_attempt_id: OpaqueId


class SourceBoundDispatchResolution(_Resolution):
    workflow_execution_id: OpaqueId
    dispatch_attempt_id: OpaqueId


class SourceBoundWorkflowProjectionResolution(_Resolution):
    workflow_execution_id: OpaqueId
    projection_attempt_id: OpaqueId


class SourceBoundEvidenceProjectionResolution(_Resolution):
    """Content-free target envelope for one owner-scoped evidence-projection attempt."""

    run_id: OpaqueId
    evidence_projection_attempt_id: OpaqueId


class SourceAuthorityResolution(_Frozen):
    """Read-only current authority decision for one immutable stamp."""

    decision: SourceAuthorityDecision
    current_receipt: CurrentSourceAuthorityReceipt | None = None
    safe_reason: Annotated[str, Field(max_length=256)] | None = None

    @model_validator(mode="after")
    def _decision_shape(self) -> SourceAuthorityResolution:
        if self.decision is SourceAuthorityDecision.AUTHORIZED:
            if (
                self.current_receipt is None
                or self.current_receipt.state is not SourceAuthorityState.PRESENT
            ):
                raise ValueError("authorized resolution requires a current present receipt")
        elif self.decision is SourceAuthorityDecision.PERMANENT_WITHHELD:
            if self.current_receipt is None or self.safe_reason is None:
                raise ValueError("permanent withholding requires admitted current truth")
        elif self.current_receipt is not None:
            raise ValueError("retryable unknown cannot claim a current authority receipt")
        return self


class AuthorizedArtifact(_Frozen):
    artifact_identity: SourceContentArtifactIdentity
    authority_stamp: SourceAuthorityStamp
    score: ExactDecimal

    @model_validator(mode="after")
    def _binding(self) -> AuthorizedArtifact:
        if self.authority_stamp.artifact_identity != self.artifact_identity:
            raise ValueError("authorized artifact stamp refers to another artifact")
        object.__setattr__(self, "score", _canonical_decimal(self.score, "artifact score"))
        return self


class AuthorizedArtifactPage(_Frozen):
    items: Annotated[tuple[AuthorizedArtifact, ...], Field(max_length=1000)]
    status: AuthorizedArtifactPageStatus
    scanned_count: Annotated[int, Field(ge=0)]
    returned_count: Annotated[int, Field(ge=0)]
    scan_limit: Annotated[int, Field(ge=1)]
    continuation_token: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def _truthful_counts(self) -> AuthorizedArtifactPage:
        if self.returned_count != len(self.items):
            raise ValueError("returned_count must equal disclosed page items")
        if self.returned_count > self.scanned_count or self.scanned_count > self.scan_limit:
            raise ValueError("page counts exceed scanned/scan bounds")
        if self.status is AuthorizedArtifactPageStatus.PARTIAL and not self.items:
            raise ValueError("partial means at least one authorized item was returned")
        if self.status is AuthorizedArtifactPageStatus.EXHAUSTED and self.items:
            raise ValueError("exhausted means zero authorized items were returned")
        if (
            self.status is AuthorizedArtifactPageStatus.COMPLETE
            and self.continuation_token is not None
        ):
            raise ValueError("a complete page cannot advertise a continuation")
        if (
            self.status is not AuthorizedArtifactPageStatus.COMPLETE
            and self.continuation_token is None
        ):
            raise ValueError("an incomplete page requires an opaque continuation")
        return self


class AuthorizedContinuationState(_Frozen):
    """Private canonical state; raw query/filter bytes are structurally absent."""

    state_version: Annotated[int, Field(ge=1)]
    principal_access_hash: Sha256
    request_hash: Sha256
    normalizer_version: Annotated[int, Field(ge=1)]
    encoding_version: Annotated[int, Field(ge=1)]
    requested_top_k: Annotated[int, Field(ge=1, le=1000)]
    scan_limit: Annotated[int, Field(ge=1)]
    config_hash: Sha256
    contract_head_hash: Sha256
    cursor_score: ExactDecimal
    cursor_artifact_id: Sha256
    prior_page_set_hash: Sha256
    expires_at: datetime

    @model_validator(mode="after")
    def _valid_state(self) -> AuthorizedContinuationState:
        _require_utc(self.expires_at, "expires_at")
        object.__setattr__(
            self,
            "cursor_score",
            _canonical_decimal(self.cursor_score, "cursor_score"),
        )
        return self


class AuthorizedContinuationReplay(_Frozen):
    """Immutable first-resolution replay row keyed by the token MAC."""

    token_mac: Sha256
    state: AuthorizedContinuationState
    item_ids: Annotated[tuple[OpaqueId, ...], Field(max_length=1000)]
    status: AuthorizedArtifactPageStatus
    scanned_count: Annotated[int, Field(ge=0)]
    returned_count: Annotated[int, Field(ge=0)]
    scan_limit: Annotated[int, Field(ge=1)]
    contributor_stamps: Annotated[tuple[SourceAuthorityStamp, ...], Field(max_length=1000)]
    acl_decision_fingerprints: Annotated[tuple[Sha256, ...], Field(max_length=1000)]
    scope_health_epoch: Annotated[int, Field(ge=1)]
    next_state_hash: Sha256 | None
    replay_hash: Sha256 | Annotated[str, Field(max_length=0)] = ""

    @model_validator(mode="after")
    def _canonical_replay(self) -> AuthorizedContinuationReplay:
        if self.returned_count != len(self.item_ids):
            raise ValueError("replay returned_count must equal disclosed ids")
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("replay item ids must be unique")
        if len(self.contributor_stamps) != len(self.item_ids):
            raise ValueError("every replay item requires one contributor stamp")
        if len(self.acl_decision_fingerprints) != len(self.item_ids):
            raise ValueError("every replay item requires one ACL decision fingerprint")
        if self.returned_count > self.scanned_count or self.scanned_count > self.scan_limit:
            raise ValueError("replay counts exceed scanned/scan bounds")
        if self.status is AuthorizedArtifactPageStatus.PARTIAL and self.returned_count == 0:
            raise ValueError("partial replay must disclose at least one item")
        if self.status is AuthorizedArtifactPageStatus.EXHAUSTED and self.returned_count != 0:
            raise ValueError("exhausted replay must disclose zero items")
        if self.status is AuthorizedArtifactPageStatus.COMPLETE:
            if self.next_state_hash is not None:
                raise ValueError("complete replay cannot carry next continuation state")
        elif self.next_state_hash is None:
            raise ValueError("incomplete replay requires next continuation state")
        _bind_hash(
            self,
            "replay_hash",
            _canonical_hash(
                "ne.authorized-continuation-replay.v1",
                (
                    self.token_mac,
                    self.state.request_hash,
                    self.state.principal_access_hash,
                    self.status.value,
                    str(self.scanned_count),
                    str(self.returned_count),
                    str(self.scan_limit),
                    *self.item_ids,
                    *(
                        _stamp_hash("ne.authorized-continuation-replay-stamp.v1", stamp)
                        for stamp in self.contributor_stamps
                    ),
                    *self.acl_decision_fingerprints,
                    str(self.scope_health_epoch),
                    self.next_state_hash or "",
                ),
            ),
        )
        return self


class AuthorizedRetrievalEnvelope(_Frozen):
    """Current-authorized wrapper around, never a mutation of, RetrievalResult."""

    result: RetrievalResult
    status: AuthorizedArtifactPageStatus
    scanned_count: Annotated[int, Field(ge=0)]
    returned_count: Annotated[int, Field(ge=0)]
    scan_limit: Annotated[int, Field(ge=1)]
    continuation_token: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def _truthful_status(self) -> AuthorizedRetrievalEnvelope:
        if self.returned_count != len(self.result.chunks):
            raise ValueError("returned_count must equal disclosed retrieval chunks")
        if self.returned_count > self.scanned_count or self.scanned_count > self.scan_limit:
            raise ValueError("retrieval counts exceed scanned/scan bounds")
        if self.status is AuthorizedArtifactPageStatus.PARTIAL and self.returned_count == 0:
            raise ValueError("partial retrieval must disclose at least one result")
        if self.status is AuthorizedArtifactPageStatus.EXHAUSTED and self.returned_count != 0:
            raise ValueError("exhausted retrieval must disclose zero results")
        if (
            self.status is AuthorizedArtifactPageStatus.COMPLETE
            and self.continuation_token is not None
        ):
            raise ValueError("complete retrieval cannot advertise a continuation")
        if (
            self.status is not AuthorizedArtifactPageStatus.COMPLETE
            and self.continuation_token is None
        ):
            raise ValueError("incomplete retrieval requires a continuation")
        return self


class AuthorizedContinuationTokenLocator(_Frozen):
    """Parsed opaque locator; authentication requires its private canonical-state row."""

    key_id: ContinuationKeyId
    token_mac: Sha256


@runtime_checkable
class CurrentSourceAuthorityReader(Protocol):
    """Read-only current-source evaluator; request composition receives only this seam."""

    async def resolve(
        self,
        stamp: SourceAuthorityStamp,
        *,
        principal_id: str,
        access_context_hash: str,
    ) -> SourceAuthorityResolution: ...


@runtime_checkable
class CurrentSourceAuthorityWriter(Protocol):
    """Write-only reconciliation authority; never available to API projectors."""

    async def claim(
        self,
        scope: SourceReconciliationScope,
        *,
        expected_head_revision: str | None,
        lease_seconds: int,
    ) -> SourceReconciliationClaim: ...

    async def renew(
        self, claim: SourceReconciliationClaim, *, lease_seconds: int
    ) -> SourceReconciliationClaim: ...

    async def finalize(
        self,
        claim: SourceReconciliationClaim,
        fence: SourceAdmissionFence,
        candidate: SourceAuthorityCandidate | SourceDeletionObservation,
    ) -> CurrentSourceAuthorityReceipt: ...


@runtime_checkable
class ImmutableKnowledgeArtifactWrite(Protocol):
    """Immutable create/reuse plus creator-token-only bounded unwind."""

    async def create_if_absent(
        self, identity: SourceContentArtifactIdentity, canonical_bytes: bytes
    ) -> ImmutableArtifactCreateReceipt: ...

    async def release_creator(
        self,
        receipt: ImmutableArtifactCreateReceipt,
        *,
        expected_current_head: str | None,
    ) -> bool: ...


@runtime_checkable
class CurrentAuthorizedArtifactSelector(Protocol):
    """Bounded current-authorized selection with opaque continuation."""

    async def select(
        self,
        *,
        principal_id: str,
        access_context_hash: str,
        request_hash: str,
        top_k: int,
        scan_limit: int,
        continuation_token: str | None = None,
    ) -> AuthorizedArtifactPage: ...


@runtime_checkable
class SourceBoundResultResolver(Protocol):
    """Target-specific read-only protected-result materialization methods."""

    async def for_model_send(
        self,
        reference: SourceBoundToolResultRef
        | SourceBoundToolResultSetRef
        | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        model_call_attempt_id: str,
    ) -> SourceBoundModelSendResolution: ...

    async def for_direct_result(
        self,
        reference: SourceBoundToolResultRef | SourceBoundToolResultSetRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        publication_attempt_id: str,
    ) -> SourceBoundDirectResolution: ...

    async def for_approval(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution: ...

    async def for_dispatch(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        dispatch_attempt_id: str,
    ) -> SourceBoundDispatchResolution: ...

    async def for_workflow_projection(
        self,
        reference: ProtectedWorkflowStateRef | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        workflow_execution_id: str,
        projection_attempt_id: str,
    ) -> SourceBoundWorkflowProjectionResolution: ...


@runtime_checkable
class SourceBoundEvidenceProjectionResolver(SourceBoundResultResolver, Protocol):
    """The five delivery targets plus the read-only evidence-projection target.

    Evidence projection is a SIXTH target, not a reuse of an existing one: an owner
    reopen, export or deep link is not a model call, a deterministic publication, an
    approval decision, a sub-agent dispatch or a Workflow-state projection, and
    resolving it through any of those would bind a served receipt to an attempt that
    never happened.  It extends rather than mutates the five-target seam so an
    already-composed five-target implementation keeps satisfying that seam; a
    projector is typed against this Protocol and gets all six.
    """

    async def for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        evidence_projection_attempt_id: str,
    ) -> SourceBoundEvidenceProjectionResolution: ...


@runtime_checkable
class ProtectedSourceOutputWriter(Protocol):
    """Write-only creation of protected output from a complete observed stamp set."""

    async def claim_output(
        self,
        *,
        target_id: str,
        attempt_id: str,
        output: bytes,
        output_hash: str,
        stamps: Sequence[SourceAuthorityStamp],
    ) -> ProtectedSourceOutputRef: ...


@runtime_checkable
class ProtectedSourceExecutionWriter(Protocol):
    """Write-only creation of target-bound invocation and Workflow-state rows."""

    async def claim_invocation(
        self,
        *,
        target_kind: str,
        target_id: str,
        attempt_id: str,
        invocation: bytes,
        invocation_hash: str,
        stamps: Sequence[SourceAuthorityStamp],
    ) -> ProtectedSourceInvocationRef: ...

    async def claim_workflow_state(
        self,
        *,
        workflow_execution_id: str,
        workflow_version: int,
        step_id: str,
        publication_id: str,
        state: bytes,
        state_hash: str,
        stamps: Sequence[SourceAuthorityStamp],
    ) -> ProtectedWorkflowStateRef: ...


@runtime_checkable
class AuthorizedContinuationTokenCodec(Protocol):
    """Opaque token codec; decode strictly parses a locator for private durable state."""

    @property
    def key_id(self) -> ContinuationKeyId: ...

    def encode(self, state: AuthorizedContinuationState) -> str: ...

    def decode(self, token: str) -> AuthorizedContinuationTokenLocator: ...
