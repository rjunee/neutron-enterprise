# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Source-authority contract vocabulary and additive compatibility controls."""

from __future__ import annotations

import base64
import inspect
import re
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from ne.contracts import (
    AuthorizedArtifact,
    AuthorizedArtifactPage,
    AuthorizedArtifactPageStatus,
    AuthorizedContinuationReplay,
    AuthorizedContinuationState,
    AuthorizedContinuationTokenCodec,
    AuthorizedContinuationTokenLocator,
    AuthorizedRetrievalEnvelope,
    CurrentAuthorizedArtifactSelector,
    CurrentSourceAuthorityReader,
    CurrentSourceAuthorityReceipt,
    CurrentSourceAuthorityWriter,
    EvidenceAssurance,
    GovernedLocalT3SourcePoint,
    ImmutableArtifactCreateReceipt,
    ImmutableKnowledgeArtifactWrite,
    M365SourcePoint,
    ProtectedClaimSliceRef,
    ProtectedSourceExecutionWriter,
    ProtectedSourceInvocationRef,
    ProtectedSourceOutputRef,
    ProtectedSourceOutputWriter,
    ProtectedWorkflowStateRef,
    SalesforceSourcePoint,
    SourceAdmissionFence,
    SourceAuthorityCandidate,
    SourceAuthorityDecision,
    SourceAuthorityResolution,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundApprovalResolution,
    SourceBoundDirectResolution,
    SourceBoundDispatchResolution,
    SourceBoundEvidenceProjectionResolution,
    SourceBoundEvidenceProjectionResolver,
    SourceBoundModelSendResolution,
    SourceBoundResultResolver,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    SourceBoundWorkflowProjectionResolution,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceDeletionObservation,
    SourceIdentity,
    SourceReconciliationClaim,
    SourceReconciliationScope,
    VerifiedEmptyResultReceipt,
    VerifiedRowContributorReceipt,
    canonical_source_authority_stamps,
)
from ne.contracts.testing import (
    AuthorizedContinuationTokenCodecContractKit,
    CurrentAuthorizedArtifactSelectorContractKit,
    CurrentSourceAuthorityReaderContractKit,
    SourceBoundEvidenceProjectionResolverContractKit,
    SourceBoundResultResolverContractKit,
)
from ne.contracts.testing.source_authority import _resolver_target_names
from ne.contracts.types import RetrievalResult


def _source(*, deployment: str = "deployment-a", generation: int = 3) -> SourceIdentity:
    return SourceIdentity(
        deployment_id=deployment,
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a",
            site_id="",
            drive_id="drive-a",
            item_id="item-a",
        ),
        generation=generation,
    )


def _contract(
    *, digest: str = "a" * 64, source_kind: str = "m365-sharepoint"
) -> SourceContractIdentity:
    return SourceContractIdentity(
        source_kind=source_kind,
        descriptor_schema_version=1,
        descriptor_hash=digest,
    )


def _artifact(*, source: SourceIdentity | None = None) -> SourceContentArtifactIdentity:
    return SourceContentArtifactIdentity(
        source_identity=source or _source(),
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=_contract(),
        artifact_schema_version=1,
    )


def _receipt(*, source: SourceIdentity | None = None) -> CurrentSourceAuthorityReceipt:
    source = source or _source()
    artifact = _artifact(source=source)
    return CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=_contract(),
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )


def _stamp(*, source: SourceIdentity | None = None) -> SourceAuthorityStamp:
    receipt = _receipt(source=source)
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


def test_evidence_assurance_is_distinct_from_graph_assurance() -> None:
    assert tuple(item.value for item in EvidenceAssurance) == (
        "direct",
        "inferred",
        "extracted",
    )
    assert "source" not in EvidenceAssurance
    assert "confirmed" not in EvidenceAssurance


def test_tool_result_set_ref_is_additive_to_single_ref() -> None:
    stamp = _stamp()
    single = SourceBoundToolResultRef(
        reference_id="pending-1", stamps=(stamp,), result_hash="e" * 64
    )
    result_set = SourceBoundToolResultSetRef(
        reference_id="pending-2",
        stamps=(stamp,),
        result_hash="f" * 64,
        evidence_ids=("evidence-1", "evidence-2"),
    )
    assert "evidence_ids" not in single.model_dump()
    assert isinstance(result_set, SourceBoundToolResultRef)
    with pytest.raises(ValidationError, match="evidence ids"):
        SourceBoundToolResultSetRef(
            reference_id="pending-2",
            stamps=(stamp,),
            result_hash="f" * 64,
            evidence_ids=("duplicate", "duplicate"),
        )


class _AlwaysAuthorizingReader:
    def __init__(self, receipt: CurrentSourceAuthorityReceipt) -> None:
        self._receipt = receipt

    async def resolve(
        self,
        stamp: SourceAuthorityStamp,
        *,
        principal_id: str,
        access_context_hash: str,
    ) -> SourceAuthorityResolution:
        del stamp, principal_id, access_context_hash
        return SourceAuthorityResolution(
            decision=SourceAuthorityDecision.AUTHORIZED,
            current_receipt=self._receipt,
        )


class _ReaderKitProbe(CurrentSourceAuthorityReaderContractKit):
    def __init__(self, authorized: SourceAuthorityStamp) -> None:
        self._authorized = authorized

    def create_reader(self) -> CurrentSourceAuthorityReader:
        return _AlwaysAuthorizingReader(_receipt())

    def authorized_stamp(self) -> SourceAuthorityStamp:
        return self._authorized

    def unknown_stamp(self) -> SourceAuthorityStamp:
        return _stamp(source=_source(generation=8))

    def foreign_same_shape_stamp(self) -> SourceAuthorityStamp:
        return _stamp(source=_source(generation=9))


class _StampSubstitutingResolver:
    async def for_model_send(
        self,
        reference: SourceBoundToolResultRef | ProtectedSourceOutputRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        model_call_attempt_id: str,
    ) -> SourceBoundModelSendResolution:
        del reference, principal_id, access_context_hash
        return SourceBoundModelSendResolution(
            decision=SourceAuthorityDecision.AUTHORIZED,
            materialized_text="foreign row",
            stamps=(_stamp(),),
            run_id=run_id,
            model_call_attempt_id=model_call_attempt_id,
        )


class _ResolverKitProbe(SourceBoundResultResolverContractKit):
    def create_resolver(self) -> SourceBoundResultResolver:
        return _StampSubstitutingResolver()  # type: ignore[return-value]

    def reference(self) -> SourceBoundToolResultRef:
        return SourceBoundToolResultRef(
            reference_id="expected-tool",
            result_hash="2" * 64,
            stamps=(_stamp(source=_source(generation=7)),),
        )

    def output_reference(self) -> ProtectedSourceOutputRef:
        return ProtectedSourceOutputRef(
            reference_id="expected-output",
            output_hash="3" * 64,
            stamps=(_stamp(source=_source(generation=7)),),
        )

    def invocation_reference(self) -> ProtectedSourceInvocationRef:
        raise NotImplementedError

    def workflow_reference(self) -> ProtectedWorkflowStateRef:
        raise NotImplementedError

    def foreign_same_shape_reference(self) -> SourceBoundToolResultRef:
        return SourceBoundToolResultRef(
            reference_id="foreign-tool",
            result_hash="2" * 64,
            stamps=(_stamp(source=_source(generation=9)),),
        )


class _EvidenceStampSubstitutingResolver(_StampSubstitutingResolver):
    async def for_evidence_projection(
        self,
        reference: ProtectedSourceOutputRef | ProtectedClaimSliceRef,
        *,
        principal_id: str,
        access_context_hash: str,
        run_id: str,
        evidence_projection_attempt_id: str,
    ) -> SourceBoundEvidenceProjectionResolution:
        del reference, principal_id, access_context_hash
        return SourceBoundEvidenceProjectionResolution(
            decision=SourceAuthorityDecision.AUTHORIZED,
            materialized_text="foreign row",
            stamps=(_stamp(),),
            run_id=run_id,
            evidence_projection_attempt_id=evidence_projection_attempt_id,
        )


class _EvidenceResolverKitProbe(
    SourceBoundEvidenceProjectionResolverContractKit, _ResolverKitProbe
):
    def create_resolver(self) -> SourceBoundEvidenceProjectionResolver:
        return _EvidenceStampSubstitutingResolver()  # type: ignore[return-value]

    def claim_slice_reference(self) -> ProtectedClaimSliceRef:
        return ProtectedClaimSliceRef(
            reference_id="expected-claim-slice",
            slice_hash="6" * 64,
            stamps=(_stamp(source=_source(generation=7)),),
        )


def _continuation_state(*, cursor_score: Decimal = Decimal("0.5")) -> AuthorizedContinuationState:
    return AuthorizedContinuationState(
        state_version=1,
        principal_access_hash="7" * 64,
        request_hash="8" * 64,
        normalizer_version=1,
        encoding_version=1,
        requested_top_k=5,
        scan_limit=20,
        config_hash="9" * 64,
        contract_head_hash="a" * 64,
        cursor_score=cursor_score,
        cursor_artifact_id=_artifact().artifact_id,
        prior_page_set_hash="b" * 64,
        expires_at=datetime(2026, 8, 21, 1, tzinfo=UTC),
    )


class _StrictParsingCodec:
    @property
    def key_id(self) -> str:
        return "kit_key"

    def encode(self, state: AuthorizedContinuationState) -> str:
        mac = base64.urlsafe_b64encode(bytes.fromhex(state.request_hash)).rstrip(b"=").decode()
        return f"{self.key_id}.{mac}"

    def decode(self, token: str) -> AuthorizedContinuationTokenLocator:
        if token.count(".") != 1:
            raise ValueError("invalid token")
        key_id, mac = token.split(".")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key_id) is None:
            raise ValueError("invalid token")
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", mac) is None:
            raise ValueError("invalid token")
        raw = base64.urlsafe_b64decode(mac + "=")
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        if len(raw) != 32 or canonical != mac:
            raise ValueError("invalid token")
        return AuthorizedContinuationTokenLocator(key_id=key_id, token_mac=raw.hex())


class _CodecKitProbe(AuthorizedContinuationTokenCodecContractKit):
    def create_codec(self) -> AuthorizedContinuationTokenCodec:
        return _StrictParsingCodec()

    def state(self) -> AuthorizedContinuationState:
        return _continuation_state()

    def mutate_token(self, token: str) -> str:
        key_id, mac = token.split(".")
        replacement = "A" if mac[0] != "A" else "B"
        return f"{key_id}.{replacement}{mac[1:]}"

    def decode_failure(self, codec: AuthorizedContinuationTokenCodec, token: str) -> bool:
        try:
            codec.decode(token)
        except (ValueError, ValidationError):
            return True
        return False


class _InvalidContinuationError(Exception):
    pass


class _InstrumentedContinuationSelector:
    def __init__(self, *, valid_token: str, unsafe_key_lookup: bool) -> None:
        self._valid_token = valid_token
        self._unsafe_key_lookup = unsafe_key_lookup
        self.private_row_lookups = 0
        self.artifact_reads = 0
        self.first_page_restarts = 0

    async def select(
        self,
        *,
        principal_id: str,
        access_context_hash: str,
        request_hash: str,
        top_k: int,
        scan_limit: int,
        continuation_token: str | None = None,
    ) -> AuthorizedArtifactPage:
        del principal_id, access_context_hash, request_hash, top_k
        if continuation_token is not None and continuation_token != self._valid_token:
            valid_key_id = self._valid_token.partition(".")[0]
            candidate_key_id = continuation_token.partition(".")[0]
            if candidate_key_id == valid_key_id or self._unsafe_key_lookup:
                self.private_row_lookups += 1
            raise _InvalidContinuationError
        return AuthorizedArtifactPage(
            items=(),
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=0,
            returned_count=0,
            scan_limit=scan_limit,
        )


class _SelectorKitProbe(CurrentAuthorizedArtifactSelectorContractKit):
    def __init__(self, *, unsafe_key_lookup: bool = False) -> None:
        self._unsafe_key_lookup = unsafe_key_lookup

    def create_selector(self) -> CurrentAuthorizedArtifactSelector:
        return _InstrumentedContinuationSelector(
            valid_token=self.valid_continuation_token(),
            unsafe_key_lookup=self._unsafe_key_lookup,
        )

    def valid_continuation_token(self) -> str:
        return _StrictParsingCodec().encode(_continuation_state())

    async def invalid_continuation_outcome(
        self,
        selector: CurrentAuthorizedArtifactSelector,
        token: str,
    ) -> str:
        try:
            await selector.select(
                principal_id="kit-principal",
                access_context_hash="a" * 64,
                request_hash="b" * 64,
                top_k=2,
                scan_limit=3,
                continuation_token=token,
            )
        except _InvalidContinuationError:
            return "invalid_continuation"
        return ""

    def private_row_lookup_count(self, selector: CurrentAuthorizedArtifactSelector) -> int:
        return selector.private_row_lookups  # type: ignore[attr-defined]

    def artifact_read_count(self, selector: CurrentAuthorizedArtifactSelector) -> int:
        return selector.artifact_reads  # type: ignore[attr-defined]

    def first_page_restart_count(self, selector: CurrentAuthorizedArtifactSelector) -> int:
        return selector.first_page_restarts  # type: ignore[attr-defined]


def test_source_identity_is_length_prefixed_collision_safe_and_generation_bound() -> None:
    base = _source()
    assert len(base.identity_hash) == 64
    assert ":" not in base.identity_hash
    assert base != _source(generation=4)
    assert base.identity_hash != _source(generation=4).identity_hash
    assert base.identity_hash != _source(deployment="DEPLOYMENT-A").identity_hash
    joined_left = SourceIdentity(
        deployment_id="d",
        source_kind="m365-sharepoint",
        connector_instance_id="i",
        point=M365SourcePoint(
            graph_tenant_id="a",
            site_id="",
            drive_id="b:c",
            item_id="d",
        ),
        generation=1,
    )
    joined_right = SourceIdentity(
        deployment_id="d",
        source_kind="m365-sharepoint",
        connector_instance_id="i",
        point=M365SourcePoint(
            graph_tenant_id="a",
            site_id="",
            drive_id="b",
            item_id="c:d",
        ),
        generation=1,
    )
    assert joined_left.identity_hash != joined_right.identity_hash


def test_source_point_kind_and_exact_native_shape_are_structural() -> None:
    m365 = _source()
    assert isinstance(m365.point, M365SourcePoint)
    assert m365.point.site_id == ""  # direct-drive scope remains explicit
    t3 = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="governed_local_t3",
        connector_instance_id="meridian-dataset-instance-a",
        point=GovernedLocalT3SourcePoint(
            dataset_name="meridian",
            contributor_key="hardship:row:1",
        ),
        generation=1,
    )
    assert t3.identity_hash != m365.identity_hash
    with pytest.raises(ValidationError):
        SourceIdentity(
            deployment_id="deployment-a",
            source_kind="m365-sharepoint",
            connector_instance_id="connector-a",
            point=t3.point,
            generation=1,
        )


def test_identity_preserves_raw_case_whitespace_and_unicode_bytes() -> None:
    exact = _source(deployment=" Deployment-e\N{COMBINING ACUTE ACCENT}")
    normalized = _source(deployment="deployment-é")
    assert exact.deployment_id.startswith(" ")
    assert exact.identity_hash != normalized.identity_hash


def test_artifact_identity_changes_for_every_interpretation_or_acl_member() -> None:
    base = _artifact()
    values = base.model_dump(exclude={"artifact_id"})
    changed_acl = SourceContentArtifactIdentity(**{**values, "acl_snapshot_hash": "e" * 64})
    changed_contract = SourceContentArtifactIdentity(
        **{**values, "source_contract": _contract(digest="f" * 64)}
    )
    changed_content = SourceContentArtifactIdentity(**{**values, "content_snapshot_hash": "0" * 64})
    changed_generation = _artifact(source=_source(generation=4))
    ids = {
        value.artifact_id
        for value in (base, changed_acl, changed_contract, changed_content, changed_generation)
    }
    assert len(ids) == 5
    assert all(":" not in value for value in ids)


def test_operational_fields_do_not_enter_immutable_artifact_identity() -> None:
    artifact = _artifact()
    assert (
        artifact.artifact_id == "649c0c830d0d67696332635a169833c774e2c0da5dfad4bfece5bb291b6416cf"
    )
    assert set(type(artifact).model_fields) == {
        "schema_version",
        "source_identity",
        "source_snapshot_hash",
        "content_snapshot_hash",
        "acl_snapshot_hash",
        "source_contract",
        "artifact_schema_version",
        "artifact_id",
    }
    candidate = SourceAuthorityCandidate(
        source_identity=artifact.source_identity,
        artifact_identity=artifact,
        source_revision="rev-a",
        source_contract=artifact.source_contract,
        contract_epoch=2,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        authority_as_of=datetime(2026, 8, 21, tzinfo=UTC),
    )
    scope = SourceReconciliationScope(path=("tenant", "drive-a"))
    claim = SourceReconciliationClaim(
        scope=scope,
        reconciliation_epoch=8,
        expected_head_revision="head-before",
        lease_expires_at=datetime(2026, 8, 21, 1, tzinfo=UTC),
        fencing_token="fence-a",
    )
    fence = SourceAdmissionFence(
        deployment_id=artifact.source_identity.deployment_id,
        connector_instance_id=artifact.source_identity.connector_instance_id,
        source_generation=artifact.source_identity.generation,
        external_source_revision=candidate.source_revision,
        contract_epoch=candidate.contract_epoch,
        reconciliation_scope=scope,
        reconciliation_epoch=claim.reconciliation_epoch,
        fencing_token=claim.fencing_token,
    )
    assert fence.fencing_token not in artifact.artifact_id
    assert claim.reconciliation_epoch != artifact.artifact_schema_version


def test_receipts_and_stamps_cross_check_exact_source_artifact_contract_and_acl() -> None:
    receipt = _receipt()
    stamp = _stamp()
    assert stamp.source_identity == receipt.source_identity
    assert stamp.artifact_identity == receipt.artifact_identity
    assert stamp.authority_receipt_hash == receipt.receipt_hash
    with pytest.raises(ValidationError):
        SourceAuthorityStamp(
            **{
                **stamp.model_dump(),
                "acl_snapshot_hash": "f" * 64,
            }
        )


def test_receipt_source_kind_controls_contract_and_freshness_shape() -> None:
    m365 = _receipt()
    fractional = CurrentSourceAuthorityReceipt(
        **{
            **m365.model_dump(exclude={"receipt_hash"}),
            "freshness_sla_seconds": Decimal("0.500"),
        }
    )
    assert fractional.freshness_sla_seconds == Decimal("0.5")
    assert fractional.receipt_hash != m365.receipt_hash
    with pytest.raises(ValidationError, match="fixed freshness SLA"):
        CurrentSourceAuthorityReceipt(
            **{
                **m365.model_dump(exclude={"receipt_hash", "freshness_sla_seconds"}),
            }
        )

    t3_source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="governed_local_t3",
        connector_instance_id="dataset-instance-a",
        point=GovernedLocalT3SourcePoint(
            dataset_name="meridian",
            contributor_key="row:1",
        ),
        generation=1,
    )
    t3_contract = _contract(source_kind="governed_local_t3")
    t3_artifact = SourceContentArtifactIdentity(
        source_identity=t3_source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=t3_contract,
        artifact_schema_version=1,
    )
    common = {
        "receipt_id": "receipt-t3",
        "source_identity": t3_source,
        "artifact_identity": t3_artifact,
        "source_revision": "release-a",
        "source_contract": t3_contract,
        "contract_epoch": 1,
        "acl_snapshot_hash": t3_artifact.acl_snapshot_hash,
        "state": SourceAuthorityState.PRESENT,
        "admitted_at": datetime(2026, 8, 21, 12, tzinfo=UTC),
        "authority_as_of": datetime(2026, 8, 21, 12, tzinfo=UTC),
        "audit_event_id": "audit-t3",
        "lineage_id": "lineage-t3",
    }
    assert CurrentSourceAuthorityReceipt(**common).freshness_sla_seconds is None
    with pytest.raises(ValidationError, match="cannot use a connector freshness SLA"):
        CurrentSourceAuthorityReceipt(
            **common,
            freshness_sla_seconds=Decimal("0.5"),
        )
    with pytest.raises(ValidationError, match="source and contract kinds differ"):
        SourceAuthorityCandidate(
            source_identity=t3_source,
            artifact_identity=None,
            source_revision="release-a",
            source_contract=_contract(),
            contract_epoch=1,
            acl_snapshot_hash="d" * 64,
            state=SourceAuthorityState.UNKNOWN,
            authority_as_of=datetime(2026, 8, 21, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        CurrentSourceAuthorityReceipt(
            **{
                **m365.model_dump(),
                "state": SourceAuthorityState.DELETED,
                "artifact_identity": m365.artifact_identity,
            }
        )


def test_deletion_observation_is_identity_bearing_and_has_no_artifact() -> None:
    deleted = SourceDeletionObservation(
        source_identity=_source(),
        source_revision="deleted-etag",
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
        source_contract=_contract(),
        contract_epoch=5,
    )
    assert deleted.source_identity.generation == 3
    assert "artifact_identity" not in type(deleted).model_fields


def test_immutable_create_receipt_gives_ownership_only_to_creator() -> None:
    created = ImmutableArtifactCreateReceipt(
        artifact_identity=_artifact(),
        created=True,
        creator_token="creator-only-token",
        inflight_reference_id="inflight-a",
    )
    reused = ImmutableArtifactCreateReceipt(
        artifact_identity=_artifact(),
        created=False,
        creator_token=None,
        inflight_reference_id="inflight-b",
    )
    assert created.creator_token is not None
    assert reused.creator_token is None
    with pytest.raises(ValidationError):
        ImmutableArtifactCreateReceipt(
            **{
                **reused.model_dump(),
                "creator_token": "stolen",
            }
        )


def test_verified_row_receipt_binds_order_count_set_and_template() -> None:
    stamp = _stamp()
    other = stamp.model_copy(update={"selected_head_revision": "rev-b"})
    receipt = VerifiedRowContributorReceipt(
        template_id="template-a",
        template_version=2,
        canonical_row_hash="1" * 64,
        contributor_schema_version=1,
        contributors=(stamp, other),
    )
    reversed_receipt = VerifiedRowContributorReceipt(
        template_id="template-a",
        template_version=2,
        canonical_row_hash="1" * 64,
        contributor_schema_version=1,
        contributors=(other, stamp),
    )
    assert receipt.contributor_count == 2
    assert receipt.contributor_set_hash == reversed_receipt.contributor_set_hash
    assert receipt.receipt_hash != reversed_receipt.receipt_hash


def test_verified_empty_receipt_binds_query_dataset_and_canonical_contributors() -> None:
    stamp = _stamp()
    other = stamp.model_copy(update={"selected_head_revision": "rev-b"})
    receipt = VerifiedEmptyResultReceipt(
        template_id="template-a",
        template_version=2,
        canonical_args_hash="1" * 64,
        dataset_graphs_hash="2" * 64,
        contributor_schema_version=1,
        contributors=canonical_source_authority_stamps((stamp, other)),
    )
    assert receipt.contributor_count == 2
    assert receipt.receipt_hash
    with pytest.raises(ValidationError, match="unique and canonical"):
        VerifiedEmptyResultReceipt(
            template_id="template-a",
            template_version=2,
            canonical_args_hash="1" * 64,
            dataset_graphs_hash="2" * 64,
            contributor_schema_version=1,
            contributors=(stamp, stamp),
        )


def test_protected_references_are_bounded_opaque_and_content_free() -> None:
    stamp = _stamp()
    tool = SourceBoundToolResultRef(
        reference_id="tool-ref-a",
        result_hash="2" * 64,
        stamps=(stamp,),
    )
    output = ProtectedSourceOutputRef(
        reference_id="output-ref-a",
        output_hash="3" * 64,
        stamps=(stamp,),
    )
    invocation = ProtectedSourceInvocationRef(
        reference_id="invocation-ref-a",
        invocation_hash="4" * 64,
        stamps=(stamp,),
    )
    workflow = ProtectedWorkflowStateRef(
        reference_id="workflow-ref-a",
        state_hash="5" * 64,
        stamps=(stamp,),
    )
    claim = ProtectedClaimSliceRef(
        reference_id="claim-ref-a",
        slice_hash="6" * 64,
        stamps=(stamp,),
    )
    for value in (tool, output, invocation, workflow, claim):
        dumped = value.model_dump()
        assert not ({"content", "text", "bytes", "payload"} & dumped.keys())


def test_protected_reference_stamp_set_is_unique_and_canonically_ordered() -> None:
    first = _stamp()
    second = first.model_copy(update={"selected_head_revision": "rev-b"})
    ordered = tuple(
        sorted(
            (first, second),
            key=lambda stamp: (
                stamp.source_identity.identity_hash,
                stamp.artifact_identity.artifact_id,
                stamp.selected_head_revision,
                stamp.authority_receipt_hash,
            ),
        )
    )
    # The production order is a domain-separated hash, so discover it from the two
    # accepted permutations rather than copying the private helper into this test.
    accepted: tuple[SourceAuthorityStamp, ...] | None = None
    for candidate in (ordered, tuple(reversed(ordered))):
        try:
            SourceBoundToolResultRef(
                reference_id="tool-ref-order",
                result_hash="2" * 64,
                stamps=candidate,
            )
        except ValidationError:
            continue
        accepted = candidate
    assert accepted is not None
    with pytest.raises(ValidationError):
        SourceBoundToolResultRef(
            reference_id="tool-ref-order",
            result_hash="2" * 64,
            stamps=tuple(reversed(accepted)),
        )
    with pytest.raises(ValidationError):
        SourceBoundToolResultRef(
            reference_id="tool-ref-duplicate",
            result_hash="2" * 64,
            stamps=(first, first),
        )


def test_target_specific_resolution_envelopes_cannot_be_interchanged() -> None:
    stamp = _stamp()
    common = {
        "decision": SourceAuthorityDecision.AUTHORIZED,
        "materialized_text": "ephemeral text",
        "stamps": (stamp,),
    }
    model = SourceBoundModelSendResolution(**common, run_id="run-a", model_call_attempt_id="call-a")
    direct = SourceBoundDirectResolution(
        **common, workflow_execution_id="workflow-a", publication_attempt_id="publication-a"
    )
    approval = SourceBoundApprovalResolution(
        **common, approval_id="approval-a", decision_attempt_id="decision-a"
    )
    dispatch = SourceBoundDispatchResolution(
        **common, workflow_execution_id="workflow-a", dispatch_attempt_id="dispatch-a"
    )
    projection = SourceBoundWorkflowProjectionResolution(
        **common, workflow_execution_id="workflow-a", projection_attempt_id="projection-a"
    )
    assert len({type(model), type(direct), type(approval), type(dispatch), type(projection)}) == 5
    with pytest.raises(ValidationError):
        SourceBoundModelSendResolution.model_validate(direct.model_dump())
    with pytest.raises(ValidationError, match="material or stamps"):
        SourceBoundModelSendResolution(
            decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
            stamps=(stamp,),
            safe_reason="foreign_reference",
            run_id="run-a",
            model_call_attempt_id="call-a",
        )


def test_current_authority_decisions_cannot_upgrade_unknown_or_unauthenticated_denial() -> None:
    present = _receipt()
    authorized = SourceAuthorityResolution(
        decision=SourceAuthorityDecision.AUTHORIZED,
        current_receipt=present,
    )
    permanently_withheld = SourceAuthorityResolution(
        decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
        current_receipt=present,
        safe_reason="current_acl_contracted",
    )
    unknown = SourceAuthorityResolution(
        decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
        safe_reason="authority_unavailable",
    )
    assert authorized.current_receipt is present
    assert permanently_withheld.current_receipt is present
    assert unknown.current_receipt is None
    with pytest.raises(ValidationError):
        SourceAuthorityResolution(
            decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
            safe_reason="unproven_denial",
        )
    with pytest.raises(ValidationError):
        SourceAuthorityResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            current_receipt=present,
            safe_reason="pretend_unknown",
        )


def test_reader_kit_rejects_exact_stamp_substitution_and_foreign_rows() -> None:
    wrong_exact = _ReaderKitProbe(_stamp(source=_source(generation=7)))
    with pytest.raises(AssertionError):
        wrong_exact.test_exact_current_stamp_authorizes()
    with pytest.raises(AssertionError):
        wrong_exact.test_foreign_same_shape_stamp_fails_closed()


def _resolver_targets() -> dict[str, type[SourceBoundModelSendResolution]]:
    """Derive {resolver target -> its resolution envelope} from the Protocol itself."""

    return {
        name: get_type_hints(getattr(SourceBoundEvidenceProjectionResolver, name))["return"]
        for name in _resolver_target_names(SourceBoundEvidenceProjectionResolver)
    }


def _shared_delivery_fields(targets: dict[str, type[SourceBoundModelSendResolution]]) -> set[str]:
    return set.intersection(*(set(envelope.model_fields) for envelope in targets.values()))


def test_evidence_projection_is_a_sixth_closed_target_with_a_content_free_envelope() -> None:
    targets = _resolver_targets()
    assert set(targets) == {
        "for_model_send",
        "for_direct_result",
        "for_approval",
        "for_dispatch",
        "for_workflow_projection",
        "for_evidence_projection",
    }
    assert targets["for_evidence_projection"] is SourceBoundEvidenceProjectionResolution

    shared = _shared_delivery_fields(targets)
    # Positive control for the absence assertions below: the ONE decision-gated
    # content channel is real, is shared by all six targets, and is not something
    # the sixth target added.
    assert shared == {"decision", "materialized_text", "stamps", "safe_reason"}
    added = set(SourceBoundEvidenceProjectionResolution.model_fields) - shared
    assert added == {"run_id", "evidence_projection_attempt_id"}
    assert not ({"text", "content", "bytes", "payload", "span", "slice", "title"} & added)
    for field in added:
        assert SourceBoundEvidenceProjectionResolution.model_fields[field].annotation is str
        assert SourceBoundEvidenceProjectionResolution.model_fields[field].is_required()

    # The target names a projection attempt; it cannot be minted without one.
    with pytest.raises(ValidationError):
        SourceBoundEvidenceProjectionResolution(
            decision=SourceAuthorityDecision.RETRYABLE_UNKNOWN,
            safe_reason="authority_unavailable",
            run_id="run-a",
            evidence_projection_attempt_id="",
        )
    authorized = SourceBoundEvidenceProjectionResolution(
        decision=SourceAuthorityDecision.AUTHORIZED,
        materialized_text="ephemeral text",
        stamps=(_stamp(),),
        run_id="run-a",
        evidence_projection_attempt_id="evidence-read-a",
    )
    assert authorized.materialized_text == "ephemeral text"


def test_evidence_projection_accepts_only_the_two_protected_evidence_references() -> None:
    reference = get_type_hints(SourceBoundEvidenceProjectionResolver.for_evidence_projection)[
        "reference"
    ]
    members = get_args(reference)
    assert set(members) == {ProtectedSourceOutputRef, ProtectedClaimSliceRef}
    # Paired control: the union is closed over stamped protected references, so no
    # open string/object/any member can smuggle an unstamped target in.
    assert not ({str, bytes, object} & set(members))
    for member in members:
        assert "stamps" in member.model_fields


def test_no_resolver_target_envelope_validates_another_targets_attempt() -> None:
    targets = _resolver_targets()
    shared = _shared_delivery_fields(targets)
    stamp = _stamp()
    built = {
        name: envelope(
            decision=SourceAuthorityDecision.AUTHORIZED,
            materialized_text="ephemeral text",
            stamps=(stamp,),
            **{field: f"{name}-{field}" for field in set(envelope.model_fields) - shared},
        )
        for name, envelope in targets.items()
    }
    assert len({type(value) for value in built.values()}) == len(targets)
    for name, value in built.items():
        dumped = value.model_dump()
        # Positive control: each envelope round-trips its OWN attempt.
        assert targets[name].model_validate(dumped) == value
        for other, envelope in targets.items():
            if other == name:
                continue
            with pytest.raises(ValidationError):
                envelope.model_validate(dumped)


def test_resolver_conformance_kits_cover_every_declared_target() -> None:
    _ResolverKitProbe().test_every_resolver_target_is_exercised_by_this_kit()
    _EvidenceResolverKitProbe().test_every_resolver_target_is_exercised_by_this_kit()

    class _UncoveredSixthTargetKit(_EvidenceResolverKitProbe):
        target_coverage: ClassVar[Mapping[str, str]] = dict(
            SourceBoundResultResolverContractKit.target_coverage
        )

    with pytest.raises(AssertionError, match="for_evidence_projection"):
        _UncoveredSixthTargetKit().test_every_resolver_target_is_exercised_by_this_kit()

    class _MissingScenarioKit(_EvidenceResolverKitProbe):
        target_coverage: ClassVar[Mapping[str, str]] = {
            **SourceBoundResultResolverContractKit.target_coverage,
            "for_evidence_projection": "test_scenario_that_does_not_exist",
        }

    with pytest.raises(AssertionError, match="missing kit scenario"):
        _MissingScenarioKit().test_every_resolver_target_is_exercised_by_this_kit()


def test_evidence_projection_kit_rejects_stamp_substitution() -> None:
    probe = _EvidenceResolverKitProbe()
    with pytest.raises(AssertionError):
        probe.test_evidence_projection_binds_exact_owner_read_attempt()


def test_resolver_kit_rejects_stamp_substitution_and_foreign_rows() -> None:
    probe = _ResolverKitProbe()
    with pytest.raises(AssertionError):
        probe.test_model_send_binds_exact_target()
    with pytest.raises(AssertionError):
        probe.test_foreign_same_shape_reference_never_materializes()


def test_codec_kit_separates_strict_parsing_from_private_state_authentication() -> None:
    probe = _CodecKitProbe()
    probe.test_token_is_deterministic_and_decodes_only_opaque_locator()
    probe.test_canonical_mac_change_remains_only_an_opaque_locator()
    probe.test_malformed_or_noncanonical_token_syntax_fails()


def test_selector_kit_rejects_unsafe_tamper_guess_and_key_mismatch_replay() -> None:
    probe = _SelectorKitProbe()
    probe.test_valid_continuation_claim_loads_a_bounded_page()
    probe.test_invalid_continuations_share_one_no_page_outcome()
    unsafe = _SelectorKitProbe(unsafe_key_lookup=True)
    with pytest.raises(AssertionError):
        unsafe.test_invalid_continuations_share_one_no_page_outcome()


def test_authorized_page_status_counts_and_continuation_are_truthful() -> None:
    artifact = AuthorizedArtifact(
        artifact_identity=_artifact(),
        authority_stamp=_stamp(),
        score=Decimal("0.750"),
    )
    partial = AuthorizedArtifactPage(
        items=(artifact,),
        status=AuthorizedArtifactPageStatus.PARTIAL,
        scanned_count=4,
        returned_count=1,
        scan_limit=4,
        continuation_token="opaque-token",
    )
    assert partial.returned_count == len(partial.items)
    assert partial.items[0].score == Decimal("0.75")
    with pytest.raises(ValidationError):
        AuthorizedArtifactPage(
            items=(),
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=4,
            returned_count=1,
            scan_limit=4,
        )
    with pytest.raises(ValidationError):
        AuthorizedArtifactPage(
            items=(),
            status=AuthorizedArtifactPageStatus.EXHAUSTED,
            scanned_count=4,
            returned_count=0,
            scan_limit=4,
            continuation_token=None,
        )
    with pytest.raises(ValidationError):
        AuthorizedArtifactPage(
            items=(),
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=0,
            returned_count=0,
            scan_limit=4,
            continuation_token="inert-after-complete",
        )


def test_continuation_state_and_replay_bind_principal_query_cursor_and_contributors() -> None:
    expires = datetime(2026, 8, 21, 1, tzinfo=UTC)
    state = AuthorizedContinuationState(
        state_version=1,
        principal_access_hash="7" * 64,
        request_hash="8" * 64,
        normalizer_version=1,
        encoding_version=1,
        requested_top_k=5,
        scan_limit=20,
        config_hash="9" * 64,
        contract_head_hash="a" * 64,
        cursor_score=Decimal("0.500"),
        cursor_artifact_id=_artifact().artifact_id,
        prior_page_set_hash="b" * 64,
        expires_at=expires,
    )
    replay = AuthorizedContinuationReplay(
        token_mac="c" * 64,
        state=state,
        item_ids=("item-a",),
        status=AuthorizedArtifactPageStatus.PARTIAL,
        scanned_count=10,
        returned_count=1,
        scan_limit=20,
        contributor_stamps=(_stamp(),),
        acl_decision_fingerprints=("d" * 64,),
        scope_health_epoch=3,
        next_state_hash="e" * 64,
    )
    assert replay.replay_hash
    assert replay.state.cursor_score == Decimal("0.5")
    assert AuthorizedContinuationState.model_validate_json(
        state.model_dump_json()
    ).cursor_score == Decimal("0.5")
    zero_cursor = AuthorizedContinuationState(
        **{
            **state.model_dump(exclude={"cursor_score"}),
            "cursor_score": Decimal("-0.000"),
        }
    )
    assert zero_cursor.cursor_score.as_tuple().sign == 0
    assert replay.state.expires_at == expires
    assert "query" not in replay.model_dump()
    with pytest.raises(ValidationError):
        AuthorizedContinuationReplay(
            **{
                **replay.model_dump(exclude={"replay_hash"}),
                "item_ids": ("item-a", "item-a"),
                "returned_count": 2,
                "contributor_stamps": (_stamp(), _stamp()),
                "acl_decision_fingerprints": ("d" * 64, "e" * 64),
            }
        )

    changed_stamp = _stamp().model_copy(update={"selected_head_revision": "other-head"})
    changed_replay = AuthorizedContinuationReplay(
        **{
            **replay.model_dump(exclude={"replay_hash"}),
            "contributor_stamps": (changed_stamp,),
        }
    )
    assert changed_replay.replay_hash != replay.replay_hash
    with pytest.raises(ValidationError, match="incomplete replay"):
        AuthorizedContinuationReplay(
            **{
                **replay.model_dump(exclude={"replay_hash"}),
                "next_state_hash": None,
            }
        )
    with pytest.raises(ValidationError, match="complete replay"):
        AuthorizedContinuationReplay(
            **{
                **replay.model_dump(exclude={"replay_hash"}),
                "status": AuthorizedArtifactPageStatus.COMPLETE,
            }
        )


def test_continuation_locator_is_the_only_public_decode_result() -> None:
    locator = AuthorizedContinuationTokenLocator(key_id="key_2026", token_mac="c" * 64)
    assert set(locator.model_dump()) == {"key_id", "token_mac"}
    hints = get_type_hints(AuthorizedContinuationTokenCodec.decode)
    assert hints["return"] is AuthorizedContinuationTokenLocator
    with pytest.raises(ValidationError):
        AuthorizedContinuationTokenLocator(
            key_id="bad.key",
            token_mac="c" * 64,
        )


@pytest.mark.parametrize(
    "cursor_score",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1e39"),
        Decimal("1.000000000000000000000000000000000000001"),
    ],
)
def test_continuation_cursor_decimal_is_exact_and_bounded(cursor_score: Decimal) -> None:
    with pytest.raises(ValidationError):
        AuthorizedContinuationState(
            state_version=1,
            principal_access_hash="7" * 64,
            request_hash="8" * 64,
            normalizer_version=1,
            encoding_version=1,
            requested_top_k=5,
            scan_limit=20,
            config_hash="9" * 64,
            contract_head_hash="a" * 64,
            cursor_score=cursor_score,
            cursor_artifact_id=_artifact().artifact_id,
            prior_page_set_hash="b" * 64,
            expires_at=datetime(2026, 8, 21, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize("cursor_score", ["0.5", 0.5, True])
def test_python_continuation_cursor_never_enters_through_lossy_scalar(
    cursor_score: object,
) -> None:
    with pytest.raises(ValidationError):
        AuthorizedContinuationState(
            state_version=1,
            principal_access_hash="7" * 64,
            request_hash="8" * 64,
            normalizer_version=1,
            encoding_version=1,
            requested_top_k=5,
            scan_limit=20,
            config_hash="9" * 64,
            contract_head_hash="a" * 64,
            cursor_score=cursor_score,  # type: ignore[arg-type]
            cursor_artifact_id=_artifact().artifact_id,
            prior_page_set_hash="b" * 64,
            expires_at=datetime(2026, 8, 21, 1, tzinfo=UTC),
        )


def test_authorized_retrieval_envelope_wraps_frozen_result_without_changing_it() -> None:
    result = RetrievalResult(chunks=(), citations=())
    envelope = AuthorizedRetrievalEnvelope(
        result=result,
        status=AuthorizedArtifactPageStatus.COMPLETE,
        scanned_count=0,
        returned_count=0,
        scan_limit=10,
    )
    assert envelope.result is result
    assert result.model_dump() == {"chunks": (), "citations": (), "graph_paths": ()}
    with pytest.raises(ValidationError, match="cannot advertise a continuation"):
        AuthorizedRetrievalEnvelope(
            result=result,
            status=AuthorizedArtifactPageStatus.COMPLETE,
            scanned_count=0,
            returned_count=0,
            scan_limit=10,
            continuation_token="inert-after-complete",
        )


def test_all_new_protocols_are_separate_runtime_protocols() -> None:
    protocols = (
        CurrentSourceAuthorityReader,
        CurrentSourceAuthorityWriter,
        ImmutableKnowledgeArtifactWrite,
        CurrentAuthorizedArtifactSelector,
        SourceBoundResultResolver,
        SourceBoundEvidenceProjectionResolver,
        ProtectedSourceOutputWriter,
        ProtectedSourceExecutionWriter,
        AuthorizedContinuationTokenCodec,
    )
    assert all(getattr(protocol, "_is_protocol", False) for protocol in protocols)
    assert len(set(protocols)) == 9


def test_checkpoint_a_knowledge_protocol_method_sets_are_byte_stable() -> None:
    from ne.contracts.knowledge import ChunkStore, KnowledgeRead, KnowledgeWrite

    def public_methods(protocol: type[object]) -> set[str]:
        return {
            name
            for name, member in protocol.__dict__.items()
            if not name.startswith("_")
            and (inspect.isfunction(member) or inspect.iscoroutinefunction(member))
        }

    assert public_methods(KnowledgeRead) == {"retrieve", "search"}
    assert public_methods(KnowledgeWrite) == {"curate", "quarantine", "upsert"}
    assert public_methods(ChunkStore) == {"delete", "get", "query", "upsert_chunks"}


def test_frozen_models_reject_mutation_and_unknown_fields() -> None:
    source = _source()
    with pytest.raises(ValidationError):
        source.generation = 99  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SourceIdentity.model_validate({**source.model_dump(), "invented": "not-contract"})


def test_utc_timestamps_are_required() -> None:
    receipt = _receipt()
    with pytest.raises(ValidationError):
        CurrentSourceAuthorityReceipt(
            **{
                **receipt.model_dump(),
                "admitted_at": datetime(2026, 8, 21),
            }
        )
    assert receipt.freshness_sla_seconds is not None
    assert (
        receipt.authority_as_of + timedelta(seconds=float(receipt.freshness_sla_seconds))
        >= receipt.admitted_at
    )


def test_source_authority_conformance_kits_are_public_and_not_pytest_backed() -> None:
    import ne.contracts.testing as testing

    expected = {
        "AuthorizedContinuationTokenCodecContractKit",
        "CurrentAuthorizedArtifactSelectorContractKit",
        "CurrentSourceAuthorityReaderContractKit",
        "CurrentSourceAuthorityWriterContractKit",
        "ImmutableKnowledgeArtifactWriteContractKit",
        "ProtectedSourceExecutionWriterContractKit",
        "ProtectedSourceOutputWriterContractKit",
        "SourceBoundEvidenceProjectionResolverContractKit",
        "SourceBoundResultResolverContractKit",
    }
    assert set(testing.__all__) == expected
    for name in expected:
        kit = getattr(testing, name)
        assert not name.startswith("Test")
        assert any(method.startswith("test_") for method in vars(kit))


def test_existing_consumers_keep_workspace_contract_pins() -> None:
    root = Path(__file__).resolve().parents[3]
    for relative in (
        "packages/ne-api/pyproject.toml",
        "packages/ne-brain/pyproject.toml",
        "packages/ne-connect/pyproject.toml",
        "packages/ne-worker/pyproject.toml",
    ):
        data = tomllib.loads((root / relative).read_text())
        dependencies = data["project"]["dependencies"]
        assert "ne-contracts" in dependencies
        assert data["tool"]["uv"]["sources"]["ne-contracts"] == {"workspace": True}


def test_salesforce_source_identity_is_organization_object_record_bound() -> None:
    base = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="salesforce",
        connector_instance_id="connector-a",
        point=SalesforceSourcePoint(
            organization_id="00D000000000001EAA",
            object_name="Account",
            record_id="001000000000001AAA",
        ),
        generation=1,
    )
    for point in (
        base.point.model_copy(update={"organization_id": "00D000000000002EAA"}),
        base.point.model_copy(update={"object_name": "Contact"}),
        base.point.model_copy(update={"record_id": "001000000000002AAA"}),
    ):
        changed = SourceIdentity(
            deployment_id=base.deployment_id,
            source_kind=base.source_kind,
            connector_instance_id=base.connector_instance_id,
            point=point,
            generation=base.generation,
        )
        assert changed.identity_hash != base.identity_hash
