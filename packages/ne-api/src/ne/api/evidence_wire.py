# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Closed wire contract for one owner-scoped Agent evidence read/export."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueId = Annotated[str, Field(min_length=1, max_length=256)]
SafeLabel = Annotated[str, Field(min_length=1, max_length=512)]


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GraphEntityRefView(_WireModel):
    """One opaque endpoint copied from the captured canonical Graph path."""

    entity_class: SafeLabel
    entity_key: OpaqueId
    display: Annotated[str, Field(max_length=512)]
    assurance: Literal["source", "confirmed", "extracted"]
    source_ref: OpaqueId


class GraphHopView(_WireModel):
    """One independently-authorized historical canonical Graph hop."""

    subject: GraphEntityRefView
    edge_kind: SafeLabel
    obj: GraphEntityRefView
    source_system: SafeLabel
    source_ref: OpaqueId
    assurance: Literal["source", "confirmed", "extracted"]
    # PRE-011C's canonical locator has no review-state field.  Null is the
    # truthful projection until a typed producer persists one; the API never
    # upgrades graph assurance into an invented review decision.
    review_state: Literal["accepted", "pending", "rejected", "not_applicable"] | None = None


class ServedDocumentVersionView(_WireModel):
    """The exact document version a knowledge-search read served, by receipt.

    Resolved server-side from the retained contributor receipt bound at
    admission time to one ``(document_id, version_seq)`` — never inferred from
    the corpus's CURRENT state, so a later supersession cannot re-attribute a
    historical answer.  Carries only material the reader already holds or can
    already reach: the document id equals the served chunk id, and the version
    number and content hash are the Documents workspace's own projection for a
    principal this evidence was disclosed to.  No receipt ids, ACL material, or
    source identity ride here.
    """

    document_id: OpaqueId
    version_seq: Annotated[int, Field(ge=1)]
    content_hash: Sha256
    display_name: SafeLabel


class EvidenceLocatorView(_WireModel):
    """Bounded allowlist of captured historical locator fields."""

    kind: Literal["verified_t3", "verified_t3_empty", "knowledge_search"] | None = None
    row_anchor: Sha256 | None = None
    receipt_hash: Sha256 | None = None
    source_system: SafeLabel | None = None
    title: SafeLabel | None = None
    source_revision: SafeLabel | None = None
    as_of: SafeLabel | None = None
    content_hash: Sha256 | None = None
    chunk_id: OpaqueId | None = None
    # The knowledge-search arm: the retrieved chunks a search read served, in the
    # served order. These are the SAME opaque ids the reader already received in
    # the rendering, and the console resolves them through its own authorized
    # retrieval -- no source identity, ACL label or contributor stamp rides here.
    chunk_ids: Annotated[tuple[OpaqueId, ...], Field(max_length=128)] = ()
    # The exact document versions this read's retained contributor stamps were
    # served from, resolved through each stamp's immutable
    # (source_kind, source_snapshot_hash, acl_snapshot_hash) artifact identity.
    # Carried by the three source-bearing arms — knowledge_search (its
    # contributors), the graph-path arm (every hop's stamps) and verified_t3
    # (the row receipt's contributors) — and never by verified_t3_empty, which
    # served no row and therefore no version. May be empty when no resolver is
    # composed or no stamp maps to a lineage snapshot (a governed-local T3 head
    # never does: its stamps name a release, not a document) — absence renders
    # as absence, never as a guessed "current" version.
    served_document_versions: Annotated[
        tuple[ServedDocumentVersionView, ...], Field(max_length=128)
    ] = ()
    page: Annotated[int, Field(ge=1, le=1_000_000)] | None = None
    row_id: OpaqueId | None = None
    entity_id: OpaqueId | None = None
    graph_path: Annotated[tuple[GraphHopView, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def _nonempty_locator(self) -> EvidenceLocatorView:
        generic_fields = (
            self.source_system,
            self.title,
            self.source_revision,
            self.as_of,
            self.content_hash,
            self.chunk_id,
            self.page,
            self.row_id,
            self.entity_id,
            self.graph_path,
        )
        if self.kind is None:
            if (
                self.row_anchor is not None
                or self.receipt_hash is not None
                or self.chunk_ids
                or (self.served_document_versions and not self.graph_path)
            ):
                raise ValueError("verified evidence metadata requires its locator kind")
        elif self.kind == "knowledge_search":
            if (
                not self.chunk_ids
                or self.row_anchor is not None
                or self.receipt_hash is not None
                or any(generic_fields)
            ):
                raise ValueError("knowledge search evidence requires only its chunk set")
        elif self.kind == "verified_t3":
            if (
                self.row_anchor is None
                or self.receipt_hash is None
                or self.chunk_ids
                or any(generic_fields)
            ):
                raise ValueError("verified row evidence requires its row and receipt anchors")
        elif (
            self.row_anchor is not None
            or self.receipt_hash is None
            or self.chunk_ids
            or self.served_document_versions
            or any(generic_fields)
        ):
            raise ValueError("verified empty evidence requires only its receipt anchor")
        if not any(
            (
                self.receipt_hash,
                self.source_system,
                self.title,
                self.source_revision,
                self.as_of,
                self.content_hash,
                self.chunk_id,
                self.chunk_ids,
                self.page,
                self.row_id,
                self.entity_id,
                self.graph_path,
            )
        ):
            raise ValueError("evidence locator requires one projectable field")
        return self


class EvidenceItemView(_WireModel):
    evidence_id: OpaqueId
    state: Literal["available", "withheld", "locator_unavailable"]
    message: Literal[
        "Evidence available",
        "Evidence withheld — access changed",
        "Evidence locator unavailable",
    ]
    assurance: Literal["direct", "inferred", "extracted", "unavailable"]
    producing_event_id: OpaqueId
    produced_at: datetime
    locator_hash: Sha256 | None = None
    locator: EvidenceLocatorView | None = None

    @model_validator(mode="after")
    def _disclosure_shape(self) -> EvidenceItemView:
        if self.state == "available":
            if self.locator is None or self.locator_hash is None or self.assurance == "unavailable":
                raise ValueError("available evidence requires its authorized locator")
        elif self.locator is not None:
            raise ValueError("unavailable evidence cannot carry locator content")
        if self.state == "locator_unavailable" and self.locator_hash is not None:
            raise ValueError("locator-unavailable evidence cannot advertise a locator hash")
        return self


class ClaimItemView(_WireModel):
    claim_id: OpaqueId
    claim_verdict: Literal["verified", "grounded", "unavailable"]
    state: Literal["available", "withheld", "unavailable"]
    # ONE MESSAGE PER CAUSE. "Claim mapping unavailable" used to be the single
    # fallthrough for every way a claim could fail to display -- no verifier
    # receipt, an invalid association, an unresolved evidence locator, a
    # materializer refusal that was never made, a hash or span integrity
    # failure -- which is exactly the conflation that hid tonight's outage on
    # the answer path. Each cause now has its own sentence. Every sentence is
    # content-free (C12): it names a condition of THIS owner's own run rows and
    # never a byte of protected material, a foreign id, or anything a caller
    # could not already see in this same envelope.
    message: Literal[
        "Claim available",
        "Claim withheld — access changed",
        "Claim evidence withheld — access changed",
        "Claim not associated with evidence",
        "Claim verifier receipt unavailable",
        "Claim evidence locator unresolved",
        "Claim content hash mismatch",
        "Claim span mismatch",
        "Claim unavailable — answer not disclosed",
        "Claim mapping unavailable",
    ]
    display: Annotated[str, Field(max_length=100_000)] | None
    served_output_hash: Sha256
    claim_content_hash: Sha256
    evidence_id: OpaqueId | None
    association_hash: Sha256 | None
    verifier_event_id: OpaqueId | None
    verifier_receipt_hash: Sha256 | None

    @model_validator(mode="after")
    def _claim_shape(self) -> ClaimItemView:
        # The state and the message travel together: "available" only ever says
        # available, and "withheld" only ever reports an access DECISION --
        # either the claim's own or its evidence record's -- never an absence.
        withheld_messages = {
            "Claim withheld — access changed",
            "Claim evidence withheld — access changed",
        }
        if (self.state == "available") != (self.message == "Claim available"):
            raise ValueError("available claim state and message are one statement")
        if (self.state == "withheld") != (self.message in withheld_messages):
            raise ValueError("withheld claim state requires a withheld decision message")
        if self.state == "available" and self.display is None:
            raise ValueError("available claim requires exact display material")
        if self.state != "available" and self.display is not None:
            raise ValueError("unavailable claim cannot carry display material")
        if (self.evidence_id is None) != (self.association_hash is None):
            raise ValueError("claim evidence and association hash are one binding")
        if (self.verifier_event_id is None) != (self.verifier_receipt_hash is None):
            raise ValueError("verifier event and receipt hash are one binding")
        if (
            self.claim_verdict == "verified"
            and self.state == "available"
            and self.verifier_event_id is None
        ):
            raise ValueError("available verified claim requires its typed verifier receipt")
        return self


class GovernanceReceiptView(_WireModel):
    run_audit_receipt_id: OpaqueId
    run_event_id: OpaqueId
    attempt_id: OpaqueId
    decision_kind: Literal[
        "submission",
        "state_transition",
        "evidence",
        "delivery",
        "approval",
        "terminal",
        "cancel",
        "retry",
    ]
    step_id: OpaqueId | None
    tool_call_id: OpaqueId | None
    approval_id: OpaqueId | None
    audit_event_id: OpaqueId
    audit_sequence: Annotated[int, Field(ge=1)]
    audit_record_hash: Sha256
    config_hash: Sha256
    created_at: datetime


class GovernanceView(_WireModel):
    """Typed safe governance truth; absent producer joins are explicit, never guessed."""

    status: Literal["complete", "partial", "unavailable"]
    acting_for_principal_id: OpaqueId
    agent_id: OpaqueId
    agent_version: Annotated[int, Field(ge=1)]
    config_hash: Sha256
    capability_snapshot_hash: Sha256
    delegated_user_label: SafeLabel | None
    agent_service_identity: SafeLabel | None
    connector_credential_owner_label: SafeLabel | None
    effective_scopes: Annotated[tuple[SafeLabel, ...], Field(max_length=128)]
    policy_environment: SafeLabel | None
    tool_tier: Annotated[int, Field(ge=0, le=3)] | None
    tool_decision: Literal["allow", "deny", "approval_required"] | None
    receipts: Annotated[tuple[GovernanceReceiptView, ...], Field(max_length=512)]
    unavailable_reasons: Annotated[
        tuple[
            Literal[
                "delegation_join_unavailable",
                "service_identity_join_unavailable",
                "credential_owner_join_unavailable",
                "scope_join_unavailable",
                "policy_join_unavailable",
                "tool_decision_join_unavailable",
                "audit_receipt_join_unavailable",
            ],
            ...,
        ],
        Field(max_length=7),
    ]

    @model_validator(mode="after")
    def _status_matches_reasons(self) -> GovernanceView:
        if self.status == "complete" and self.unavailable_reasons:
            raise ValueError("complete governance cannot carry unavailable reasons")
        if self.status == "unavailable" and self.receipts:
            raise ValueError("unavailable governance cannot carry receipts")
        return self


class RunOutputView(_WireModel):
    verdict: Literal["verified", "grounded", "refused", "unavailable"] | None
    state: Literal["available", "withheld", "unavailable"]
    message: Literal[
        "Answer available",
        "Answer withheld — access changed",
        "Answer unavailable",
    ]
    display: Annotated[str, Field(max_length=1_000_000)] | None

    @model_validator(mode="after")
    def _output_shape(self) -> RunOutputView:
        if (self.state == "available") != (self.display is not None):
            raise ValueError("only available output may carry display material")
        return self


class EvidenceEnvelope(_WireModel):
    """The one serializer shared by interactive read and bounded JSON export."""

    schema_version: Literal[1] = 1
    agent_id: OpaqueId
    run_id: OpaqueId
    run_status: Literal[
        "submitted",
        "queued",
        "running",
        "waiting_approval",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "expired",
        "context_exhausted",
        "budget_exhausted",
    ]
    mapping_state: Literal["mapping_pending", "complete", "partial", "unavailable"]
    mapping_message: Literal[
        "Claim mapping pending",
        "Claim mapping complete",
        "Claim mapping partial",
        "Claim mapping unavailable",
    ]
    output: RunOutputView
    claims: Annotated[tuple[ClaimItemView, ...], Field(max_length=256)]
    evidence: Annotated[tuple[EvidenceItemView, ...], Field(max_length=256)]
    governance: GovernanceView
