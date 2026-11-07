# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed, fail-closed terminal candidates for the sole canonical Run finalizer.

The models in this module are deliberately content-bearing only inside the worker
process.  ``RunWorkflowResult`` and every durable execution checkpoint retain only
the finalizer receipt's opaque identities and hashes; output bytes are consumed by
the atomic store transaction and never cross the DBOS result boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ne.contracts.source_authority import (
    SourceAuthorityStamp,
    canonical_source_authority_stamps,
)
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CanonicalTargetTuple,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    protected_payload_aad_binding,
    protected_target_hash,
)
from ne.runtime.run_store import (
    AgentRun,
    AgentRunStore,
    RunOutputVerdict,
    RunState,
    _canonical_json,
    _envelope_values,
    _id,
    _pack_source_bound_payload,
    _row_envelope,
    _stamp_set_hash,
    _unpack_source_bound_payload_and_stamps,
    ne_agent_claim_verifier_receipts,
    ne_agent_delivery_receipts,
    ne_agent_evidence_locators,
    ne_agent_evidence_records,
    ne_agent_pending_source_tool_results,
    ne_agent_protected_source_outputs,
    ne_agent_protected_turn_contents,
    ne_agent_run_audit_receipts,
    ne_agent_run_claim_evidence,
    ne_agent_run_claims,
    ne_agent_run_events,
    ne_agent_run_finalizations,
    ne_agent_run_history_attachments,
    ne_agent_run_lifecycle_outbox,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_store import (
    _hash as _bytes_hash,
)
from ne.runtime.source_results import RunDeliveryObservation, run_delivery_set_id

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueId = Annotated[str, Field(min_length=1, max_length=512)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunTurnTarget(_ClosedModel):
    """Server-observed canonical target; no client/model id is accepted here."""

    conversation_id: OpaqueId
    run_id: OpaqueId
    turn_id: OpaqueId
    turn_sequence: Annotated[int, Field(ge=1)]
    attachment_id: OpaqueId
    attempt_id: OpaqueId

    def canonical(self) -> CanonicalTargetTuple:
        return CanonicalTargetTuple(**self.model_dump())


class RunFinalizerLease(_ClosedModel):
    """Exact worker lease fence required by every first terminal commit."""

    dispatch_id: OpaqueId
    run_id: OpaqueId
    attempt_id: OpaqueId
    lease_token: OpaqueId
    lease_epoch: Annotated[int, Field(ge=1)]


class RunEvidenceBinding(_ClosedModel):
    """Immutable server-authored evidence/locator/event identity served in this turn."""

    evidence_id: OpaqueId
    evidence_key: OpaqueId
    claim_key: OpaqueId
    claim_content_hash: Sha256
    served_output_hash: Sha256
    span_start_byte: Annotated[int, Field(ge=0)]
    span_end_byte: Annotated[int, Field(gt=0)]
    producing_event_id: OpaqueId
    locator_hash: Sha256
    served_citation_ref_hash: Sha256
    delivery_receipt_id: OpaqueId

    @model_validator(mode="after")
    def _span(self) -> RunEvidenceBinding:
        if self.span_end_byte <= self.span_start_byte:
            raise ValueError("evidence binding span must be non-empty")
        return self


class RunVerifierBinding(_ClosedModel):
    """Server-authored deterministic verifier receipt for one exact local claim."""

    verifier_receipt_id: OpaqueId
    verifier_key: OpaqueId
    claim_key: OpaqueId
    claim_content_hash: Sha256
    served_output_hash: Sha256
    span_start_byte: Annotated[int, Field(ge=0)]
    span_end_byte: Annotated[int, Field(gt=0)]
    verifier_method: OpaqueId
    verifier_version: OpaqueId
    result_hash: Sha256
    event_id: OpaqueId
    receipt_hash: Sha256

    @model_validator(mode="after")
    def _span(self) -> RunVerifierBinding:
        if self.span_end_byte <= self.span_start_byte:
            raise ValueError("verifier receipt span must be non-empty")
        return self


class RunDeliveryBinding(_ClosedModel):
    """One exact server-observed post-authorization delivery receipt."""

    delivery_receipt_id: OpaqueId
    delivery_kind: Literal[
        "tool_result_served", "history_output_served", "direct_result_served", "withheld"
    ]
    decision: Literal["served", "withheld"]
    protected_ref: OpaqueId
    protected_hash: Sha256
    stamp_set_hash: Sha256
    event_id: OpaqueId


class RunAuditBinding(_ClosedModel):
    """Mandatory immutable audit receipt identity and integrity fields."""

    run_audit_receipt_id: OpaqueId
    run_event_id: OpaqueId
    step_id: OpaqueId | None = None
    tool_call_id: OpaqueId | None = None
    approval_id: OpaqueId | None = None
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
    audit_event_id: OpaqueId
    audit_sequence: Annotated[int, Field(ge=1)]
    audit_record_hash: Sha256
    config_hash: Sha256


class RunApprovalBinding(_ClosedModel):
    """Exact approval request/checkpoint join observed by the server."""

    approval_id: OpaqueId
    checkpoint_id: OpaqueId
    protected_invocation_ref: OpaqueId
    protected_invocation_hash: Sha256
    audit_receipt_id: OpaqueId


class RunFinalizerObservations(_ClosedModel):
    """Complete server observation set against which an envelope is checked."""

    target: RunTurnTarget
    delivery_set_id: OpaqueId
    delivery_set_hash: Sha256
    deliveries: tuple[RunDeliveryBinding, ...]
    evidence: tuple[RunEvidenceBinding, ...]
    verifiers: tuple[RunVerifierBinding, ...]
    approvals: tuple[RunApprovalBinding, ...]
    audit_receipts: tuple[RunAuditBinding, ...]

    @model_validator(mode="after")
    def _sets(self) -> RunFinalizerObservations:
        # NE-CLAIM-SHARED-EVIDENCE. ``evidence_id`` is deliberately ABSENT from
        # this list. An evidence binding is a per-CLAIM row, not a per-record
        # one, and two claims may legitimately cite one retrieved record (a
        # returned table naming two loans). Its identity here is
        # ``evidence_key``, checked for uniqueness immediately below, and every
        # per-claim identity downstream — the verifier receipt, the persisted
        # association row, the re-sliced span hash — stays 1:1 with the claim.
        for label, members, attr in (
            ("delivery", self.deliveries, "delivery_receipt_id"),
            ("verifier", self.verifiers, "verifier_receipt_id"),
            ("approval", self.approvals, "approval_id"),
            ("audit", self.audit_receipts, "run_audit_receipt_id"),
        ):
            values = [getattr(member, attr) for member in members]
            if len(values) != len(set(values)):
                raise ValueError(f"server-observed {label} set contains duplicate identities")
        for label, members, attr in (
            ("evidence", self.evidence, "evidence_key"),
            ("verifier", self.verifiers, "verifier_key"),
        ):
            values = [getattr(member, attr) for member in members]
            if len(values) != len(set(values)):
                raise ValueError(f"server-observed {label} set contains duplicate local keys")
        expected = _hash(
            {
                "schema": "ne.run-delivery-set.v1",
                "delivery_set_id": self.delivery_set_id,
                "members": [member.model_dump(mode="json") for member in self.deliveries],
            }
        )
        if expected != self.delivery_set_hash:
            raise ValueError("server-observed delivery set hash is invalid")
        return self


class RunClaimEnvelopeDeclaration(_ClosedModel):
    """Model/runtime declaration keyed only by a bounded envelope-local id."""

    claim_key: OpaqueId
    span_start_byte: Annotated[int, Field(ge=0)]
    span_end_byte: Annotated[int, Field(gt=0)]
    claim_content_hash: Sha256
    evidence_key: OpaqueId | None = None
    verifier_key: OpaqueId | None = None

    @model_validator(mode="after")
    def _span(self) -> RunClaimEnvelopeDeclaration:
        if self.span_end_byte <= self.span_start_byte:
            raise ValueError("claim span must be non-empty")
        return self


class StructuredRunOutputEnvelope(_ClosedModel):
    """Closed terminal output shape; canonical ids and locator authorship are absent."""

    discriminator: Literal["structured_run_output"]
    served_output_hash: Sha256
    claim_count: Annotated[int, Field(ge=0, le=1024)]
    claim_set_hash: Sha256
    claims: tuple[RunClaimEnvelopeDeclaration, ...]
    disposition: Literal["completed", "refused"] = "completed"

    @model_validator(mode="after")
    def _claim_set(self) -> StructuredRunOutputEnvelope:
        if len(self.claims) != self.claim_count:
            raise ValueError("structured claim count does not match its declarations")
        keys = [claim.claim_key for claim in self.claims]
        if len(keys) != len(set(keys)):
            raise ValueError("structured claim local keys must be unique")
        expected = _hash(
            {
                "schema": "ne.run-claim-set.v1",
                "served_output_hash": self.served_output_hash,
                "claims": [claim.model_dump(mode="json") for claim in self.claims],
            }
        )
        if expected != self.claim_set_hash:
            raise ValueError("structured claim set hash is invalid")
        if self.disposition == "refused" and self.claims:
            raise ValueError("refused output must declare zero claims")
        return self


class RunTerminalCandidate(_ClosedModel):
    """Ephemeral worker-to-finalizer handoff; never serialize this into DBOS state."""

    observations: RunFinalizerObservations
    envelope: StructuredRunOutputEnvelope
    output_bytes: bytes

    @model_validator(mode="after")
    def _output(self) -> RunTerminalCandidate:
        if hashlib.sha256(self.output_bytes).hexdigest() != self.envelope.served_output_hash:
            raise ValueError("terminal output bytes do not match the structured envelope")
        try:
            self.output_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("terminal output must be canonical UTF-8") from exc
        for claim in self.envelope.claims:
            if claim.span_end_byte > len(self.output_bytes):
                raise ValueError("claim span exceeds the terminal UTF-8 output")
            if (
                hashlib.sha256(
                    self.output_bytes[claim.span_start_byte : claim.span_end_byte]
                ).hexdigest()
                != claim.claim_content_hash
            ):
                raise ValueError("claim content hash does not match its exact UTF-8 span")
            try:
                self.output_bytes[claim.span_start_byte : claim.span_end_byte].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("claim span must align to UTF-8 code point boundaries") from exc
        return self


class RunFinalizerReceipt(_ClosedModel):
    """Content-free durable result emitted only after the atomic commit succeeds."""

    run_id: OpaqueId
    attempt_id: OpaqueId
    terminal_event_id: OpaqueId
    publication_id: OpaqueId
    protected_output_ref: OpaqueId | None
    protected_output_hash: Sha256 | None
    output_verdict: Literal["verified", "grounded", "refused", "unavailable"]
    mapping_state: Literal["complete", "partial", "unavailable"]
    claim_ids: tuple[OpaqueId, ...]
    finalizer_intent_hash: Sha256
    target_hash: Sha256
    protected_output_kind: Literal[
        "protected_turn_content", "protected_source_output", "unavailable"
    ]
    envelope_hash: Sha256
    delivery_set_id: OpaqueId
    delivery_set_hash: Sha256
    audit_receipt_set_hash: Sha256
    approval_set_hash: Sha256
    evidence_set_hash: Sha256
    verifier_set_hash: Sha256
    replayed: bool = False


#: WHY EVERY FAILED RUN SAYS SOMETHING.
#:
#: The reason a failed Run publishes is served VERBATIM to the reader, so it is
#: bounded exactly where every other safe reason in this system is bounded.  A
#: reason is a NAME - of the failure class, of the step - never a value, never a
#: fragment of a prompt, an answer or a document (C12).
_MAX_SAFE_REASON_CHARS = 256

#: A failure class is a type identifier, which is why it may be shown at all.
#: Anything that is not one is refused rather than half-trusted, because this
#: string crosses to a person.
_SAFE_FAILURE_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

#: What a failed terminal names when its caller supplied no class.  It says the
#: class is missing; it does not pretend to know one.
_UNNAMED_FAILURE_CLASS = "UnnamedRunFailure"

#: The derived sentence is honest about being derived: NOBODY explained this
#: failure, and the class is all the record actually holds.
_DERIVED_FAILURE_REASON_HEAD = (
    "The run failed and the failing step published no explanation. Recorded failure class: "
)


def safe_failure_class(failure_class: str | None) -> str:
    """The failure class as it may be shown, or a constant standing in for it."""

    candidate = (failure_class or "").strip()
    return candidate if _SAFE_FAILURE_CLASS_RE.match(candidate) else _UNNAMED_FAILURE_CLASS


def derived_failure_reason(failure_class: str | None) -> str:
    """A reason for a failure nobody explained - honest about being exactly that.

    This is the floor, not a substitute for a raiser's own marked reason: a Run
    that fails for an unanticipated cause still names its failure class instead
    of leaving its reader an alert with nothing in it.  It discloses NOTHING the
    record did not already hold, and widens nothing.
    """

    return f"{_DERIVED_FAILURE_REASON_HEAD}{safe_failure_class(failure_class)}."[
        :_MAX_SAFE_REASON_CHARS
    ]


def published_failure_reason(*, safe_reason: object, failure_class: str | None) -> str:
    """The reason a failed Run publishes: the raiser's marked one, or the floor.

    ``safe_reason`` stays OPT-IN at the raise site (NE-SELECT-REPROBE), and a
    marked one is published verbatim.  What changed is the OTHER branch: an
    unmarked - or unservable - reason now falls through to
    :func:`derived_failure_reason` rather than to silence.
    """

    if (
        isinstance(safe_reason, str)
        and safe_reason.strip()
        and len(safe_reason) <= _MAX_SAFE_REASON_CHARS
        and all(character.isprintable() for character in safe_reason)
    ):
        return safe_reason
    return derived_failure_reason(failure_class)


class RunUnavailableTerminalCandidate(_ClosedModel):
    """Content-free non-output terminal routed through the same atomic writer."""

    target: RunTurnTarget
    status: Literal[
        "completed", "failed", "cancelled", "expired", "context_exhausted", "budget_exhausted"
    ]
    outcome: Literal[
        "refused", "failed", "cancelled", "expired", "context_exhausted", "budget_exhausted"
    ]
    failure_class: OpaqueId | None = None
    delivery: RunDeliveryObservation | None = None

    @model_validator(mode="after")
    def _outcome(self) -> RunUnavailableTerminalCandidate:
        if (self.status, self.outcome) not in {
            ("completed", "refused"),
            ("failed", "failed"),
            ("cancelled", "cancelled"),
            ("expired", "expired"),
            ("context_exhausted", "context_exhausted"),
            ("budget_exhausted", "budget_exhausted"),
        }:
            raise ValueError("content-free terminal status and outcome mapping is invalid")
        return self


class RunFinalizer:
    """The sole writer for terminal output, claims, history and Run lifecycle."""

    def __init__(self, store: AgentRunStore) -> None:
        self._store = store

    async def observe(
        self,
        *,
        target: RunTurnTarget,
        delivery: RunDeliveryObservation,
        evidence: tuple[RunEvidenceBinding, ...] = (),
        verifiers: tuple[RunVerifierBinding, ...] = (),
    ) -> RunFinalizerObservations:
        """Load exact persisted sets around one completed delivery observation."""

        if not delivery.complete:
            raise RuntimeError("Run delivery observation is unknown")
        if (delivery.run_id, delivery.attempt_id) != (target.run_id, target.attempt_id):
            raise ValueError("Run delivery observation crossed a different target")
        return await self._store._call_db(self._observe_sync, target, delivery, evidence, verifiers)

    async def finalize(
        self, candidate: RunTerminalCandidate, *, lease: RunFinalizerLease
    ) -> RunFinalizerReceipt:
        return await self._store._call_db(self._finalize_sync, candidate, lease)

    async def finalize_unavailable(
        self, candidate: RunUnavailableTerminalCandidate, *, lease: RunFinalizerLease
    ) -> RunFinalizerReceipt:
        return await self._store._call_db(self._finalize_unavailable_sync, candidate, lease)

    def _observe_sync(
        self,
        target: RunTurnTarget,
        observation: RunDeliveryObservation,
        evidence: tuple[RunEvidenceBinding, ...],
        verifiers: tuple[RunVerifierBinding, ...],
    ) -> RunFinalizerObservations:
        with self._store._engine.connect() as conn:
            authored = conn.execute(
                sa.select(ne_agent_run_turn_targets.c.run_id).where(
                    ne_agent_run_turn_targets.c.run_id == target.run_id,
                    ne_agent_run_turn_targets.c.attempt_id == target.attempt_id,
                    ne_agent_run_turn_targets.c.conversation_id == target.conversation_id,
                    ne_agent_run_turn_targets.c.turn_id == target.turn_id,
                    ne_agent_run_turn_targets.c.turn_sequence == target.turn_sequence,
                    ne_agent_run_turn_targets.c.attachment_id == target.attachment_id,
                )
            ).scalar_one_or_none()
            if authored is None:
                raise ValueError("Run delivery observation target is not server-authored")
            rows = conn.execute(
                sa.select(ne_agent_delivery_receipts)
                .where(
                    ne_agent_delivery_receipts.c.run_id == target.run_id,
                    ne_agent_delivery_receipts.c.receiving_attempt_id == target.attempt_id,
                )
                .order_by(
                    ne_agent_delivery_receipts.c.created_at,
                    ne_agent_delivery_receipts.c.delivery_receipt_id,
                )
            ).all()
            receipt_ids = tuple(str(row.delivery_receipt_id) for row in rows)
            if receipt_ids != observation.delivery_receipt_ids or observation.delivery_set_id != (
                run_delivery_set_id(target.run_id, target.attempt_id, receipt_ids)
            ):
                raise ValueError("Run delivery observer did not capture the complete receipt set")
            deliveries = tuple(
                RunDeliveryBinding(
                    delivery_receipt_id=str(row.delivery_receipt_id),
                    delivery_kind=str(row.delivery_kind),  # type: ignore[arg-type]
                    decision=str(row.decision),  # type: ignore[arg-type]
                    protected_ref=str(row.protected_ref),
                    protected_hash=str(row.protected_hash),
                    stamp_set_hash=str(row.stamp_set_hash),
                    event_id=str(row.event_id),
                )
                for row in rows
            )
            audit_rows = conn.execute(
                sa.select(ne_agent_run_audit_receipts)
                .where(
                    ne_agent_run_audit_receipts.c.run_id == target.run_id,
                    ne_agent_run_audit_receipts.c.attempt_id == target.attempt_id,
                )
                .order_by(
                    ne_agent_run_audit_receipts.c.created_at,
                    ne_agent_run_audit_receipts.c.run_audit_receipt_id,
                )
            ).all()
            audits = tuple(
                RunAuditBinding(
                    run_audit_receipt_id=str(row.run_audit_receipt_id),
                    run_event_id=str(row.run_event_id),
                    step_id=None if row.step_id is None else str(row.step_id),
                    tool_call_id=None if row.tool_call_id is None else str(row.tool_call_id),
                    approval_id=None if row.approval_id is None else str(row.approval_id),
                    decision_kind=str(row.decision_kind),  # type: ignore[arg-type]
                    audit_event_id=str(row.audit_event_id),
                    audit_sequence=int(row.audit_sequence),
                    audit_record_hash=str(row.audit_record_hash),
                    config_hash=str(row.config_hash),
                )
                for row in audit_rows
            )
            approvals = self._observed_approvals(conn, target)
        return RunFinalizerObservations(
            target=target,
            delivery_set_id=observation.delivery_set_id,
            delivery_set_hash=delivery_set_hash(observation.delivery_set_id, deliveries),
            deliveries=deliveries,
            evidence=evidence,
            verifiers=verifiers,
            approvals=approvals,
            audit_receipts=audits,
        )

    def _resumed_an_approved_effect(self, conn: sa.Connection, target: RunTurnTarget) -> bool:
        """Did THIS attempt get here by resuming an approved effect? (POST_DEMO item 4)

        An approval resume executes the pinned effect the checkpoint names and
        deliberately does NOT replay the agent loop (``workflows.py``: "instead of
        replaying the agent loop through the model"). Replaying would re-execute
        completed steps, re-spend on the model and re-derive a plan
        nondeterministically — so the design is right. The consequence is that any
        step the plan intended AFTER the approved one never runs, and such a run
        used to publish ``execution_complete``, byte-identical to a run that
        finished everything.

        The signal is DURABLE and read here rather than passed in:

        * ``run.status`` is useless — ``_resume_sync`` moves the run back to
          RUNNING before finalization, so by the time this is asked the resume is
          invisible in the run row.
        * It is NOT added to :class:`RunApprovalBinding`, because that model feeds
          ``approval_set_hash``; a new field would change that governance hash on
          every run, past records included, to record something no hash consumer
          asked for.

        So this is a separate read-only query over the same continuation
        checkpoints ``_observed_approvals`` already joins. ``consumed`` is the
        fenced post-execution state and ``resumed`` is the in-flight one; either
        means this attempt carried an approved effect through a resume.
        """

        from ne.runtime.run_checkpoint import (
            ne_agent_run_approval_joins,
            ne_agent_run_continuation_checkpoints,
        )

        found = conn.execute(
            sa.select(ne_agent_run_continuation_checkpoints.c.status)
            .join(
                ne_agent_run_approval_joins,
                ne_agent_run_approval_joins.c.checkpoint_id
                == ne_agent_run_continuation_checkpoints.c.checkpoint_id,
            )
            .where(
                ne_agent_run_approval_joins.c.run_id == target.run_id,
                ne_agent_run_approval_joins.c.attempt_id == target.attempt_id,
                ne_agent_run_continuation_checkpoints.c.status.in_(("resumed", "consumed")),
            )
            .limit(1)
        ).first()
        return found is not None

    def _observed_approvals(
        self, conn: sa.Connection, target: RunTurnTarget
    ) -> tuple[RunApprovalBinding, ...]:
        from ne.runtime.run_checkpoint import (
            ne_agent_run_approval_joins,
            ne_agent_run_continuation_checkpoints,
        )

        rows = conn.execute(
            sa.select(ne_agent_run_approval_joins, ne_agent_run_continuation_checkpoints.c.status)
            .join(
                ne_agent_run_continuation_checkpoints,
                ne_agent_run_continuation_checkpoints.c.checkpoint_id
                == ne_agent_run_approval_joins.c.checkpoint_id,
            )
            .where(
                ne_agent_run_approval_joins.c.run_id == target.run_id,
                ne_agent_run_approval_joins.c.attempt_id == target.attempt_id,
            )
            .order_by(
                ne_agent_run_approval_joins.c.created_at,
                ne_agent_run_approval_joins.c.approval_id,
            )
        ).all()
        observed: list[RunApprovalBinding] = []
        for row in rows:
            if str(row.status) not in {
                "approved",
                "resumed",
                "consumed",
                "rejected",
                "expired",
                "source_withheld",
            }:
                raise ValueError("terminal Run retains an unresolved approval checkpoint")
            receipt_ids = tuple(
                str(value)
                for value in conn.execute(
                    sa.select(ne_agent_run_audit_receipts.c.run_audit_receipt_id).where(
                        ne_agent_run_audit_receipts.c.run_id == target.run_id,
                        ne_agent_run_audit_receipts.c.attempt_id == target.attempt_id,
                        ne_agent_run_audit_receipts.c.approval_id == row.approval_id,
                        ne_agent_run_audit_receipts.c.decision_kind == "approval",
                    )
                ).scalars()
            )
            if len(receipt_ids) != 1:
                raise ValueError("approval join lacks its exact mandatory audit receipt")
            observed.append(
                RunApprovalBinding(
                    approval_id=str(row.approval_id),
                    checkpoint_id=str(row.checkpoint_id),
                    protected_invocation_ref=str(row.protected_invocation_id),
                    protected_invocation_hash=str(row.protected_invocation_hash),
                    audit_receipt_id=receipt_ids[0],
                )
            )
        return tuple(observed)

    def _finalize_sync(
        self, candidate: RunTerminalCandidate, lease: RunFinalizerLease
    ) -> RunFinalizerReceipt:
        observations = candidate.observations
        target = observations.target
        envelope = candidate.envelope
        envelope_hash = _hash(envelope.model_dump(mode="json"))
        mapping_state, output_verdict = self._mapping(envelope, observations)
        metadata = self._finalizer_metadata(
            target=target,
            protected_output_kind=None,
            protected_output_ref=None,
            protected_output_hash=envelope.served_output_hash,
            mapping_state=mapping_state,
            envelope_hash=envelope_hash,
            delivery_set_id=observations.delivery_set_id,
            delivery_set_hash=observations.delivery_set_hash,
            audit_receipts=observations.audit_receipts,
            approvals=observations.approvals,
            evidence=observations.evidence,
            verifiers=observations.verifiers,
        )
        with self._store._engine.begin() as conn:
            from ne.runtime.run_workflow import _database_now

            now = _database_now(conn)
            run = self._lock_exact_target(conn, target)
            # POST_DEMO item 4 / C10. Derived BEFORE the intent hash and INSIDE it,
            # because a Codex pass scored the alternative a P1 against the repo's
            # tamper-evidence rule and was right on all three legs:
            #   * `publish_terminal` records the intent hash — not the outcome — in
            #     the hash-chained audit provenance, so with the outcome outside the
            #     hash, flipping `approved_effect_complete` to `execution_complete`
            #     would leave the AUDITED evidence byte-identical;
            #   * `RunApprovalBinding` deliberately omits checkpoint status (it feeds
            #     `approval_set_hash`), so nothing else in the hash covers it;
            #   * the explicit `existing.outcome` comparison added in
            #     `commit_run_terminal_in_transaction` is UNREACHABLE on this path —
            #     `_replay` below short-circuits on the stored receipt first.
            # ⚖️ WHAT THIS DOES AND DOES NOT BUY, corrected after an adversarial pass
            # pointed out that the property was asserted in three places and
            # demonstrated in none. The outcome is ALREADY immutable through the
            # database: `trg_run_lifecycle_outbox_guard` raises "Run lifecycle
            # outbox intent is immutable" on any UPDATE that changes it, on both
            # dialects — that is the real protection, and the flip is unreachable
            # through normal means. What binding the route adds is that the route
            # sits INSIDE the attested preimage, so a verifier that re-derives
            # `ne.run-finalizer-intent.v1` can detect an outcome altered under an
            # untouched hash (a doctored restore, a disabled trigger). NOTHING IN
            # THIS TREE RE-DERIVES IT TODAY — `_replay` is the only recomputation
            # and it runs on the same-run path, not as an audit sweep. So this is a
            # PRECONDITION for that detection, not the detection itself, and it is
            # worth its (now conditional, resumed-runs-only) cost on that basis
            # alone.
            resumed_route = self._resumed_an_approved_effect(conn, target)
            finalizer_intent_hash = _hash(
                {
                    "schema": "ne.run-finalizer-intent.v1",
                    #: The RESUME ROUTE, not the outcome word — the word is derived
                    #: from it below, so hashing the route pins the word without
                    #: coupling the digest to a vocabulary that may widen again.
                    #
                    #: ⚠️ EMITTED ONLY WHEN TRUE, and that is load-bearing. My first
                    #: version added this key unconditionally, which changes a
                    #: `sort_keys=True` preimage for EVERY completed run under an
                    #: unchanged `v1` tag — so a run finalized on release N and
                    #: recovered on N+1 recomputes a different hash and `_replay`
                    #: raises "Run finalizer replay changed its exact terminal
                    #: intent": a FALSE C10 TAMPER ALARM on every completed run, in
                    #: both directions across an upgrade, which the staged-rollout
                    #: remedy does not mitigate at all. An adversarial pass caught
                    #: it, and it is exactly what this change's own note gives as
                    #: the reason NOT to touch `RunApprovalBinding` — "a new field
                    #: would change a governance hash on every run, past records
                    #: included." Omitting it when false keeps every non-resumed
                    #: run's preimage byte-identical to prior releases and collapses
                    #: the blast radius to the resumed runs the release note already
                    #: covers.
                    **({"resumed_approved_effect": True} if resumed_route else {}),
                    "target": target.model_dump(mode="json"),
                    "delivery_set_id": observations.delivery_set_id,
                    "delivery_set_hash": observations.delivery_set_hash,
                    "approval_set": [
                        item.model_dump(mode="json") for item in observations.approvals
                    ],
                    "audit_receipt_set": [
                        item.model_dump(mode="json") for item in observations.audit_receipts
                    ],
                    "envelope_hash": envelope_hash,
                    "output_hash": envelope.served_output_hash,
                    "mapping_state": mapping_state,
                    "output_verdict": output_verdict.value,
                    "receipt_sets": {
                        key: value for key, value in metadata.items() if key.endswith("_set_hash")
                    },
                }
            )
            replay = self._replay(conn, target.run_id, finalizer_intent_hash)
            if replay is not None:
                return replay
            self._validate_lease(conn, target, lease, now=now)
            self._validate_server_observations(conn, run, observations)
            self._validate_claim_bindings(envelope, observations)

            protected_ref, protected_domain, content_status = self._protect_output(conn, candidate)
            metadata["protected_output_kind"] = protected_domain.value
            metadata["protected_output_ref"] = protected_ref
            claim_ids, claim_id_by_key = self._insert_claims(
                conn,
                candidate,
                protected_ref=protected_ref,
                protected_domain=protected_domain,
                now=now,
            )
            self._insert_verifiers(conn, candidate, now=now)
            self._insert_associations(conn, candidate, claim_id_by_key=claim_id_by_key, now=now)

            from ne.runtime.run_workflow import commit_run_terminal_in_transaction

            # POST_DEMO item 4: name the ROUTE this terminal took. Never a COUNT —
            # the platform cannot know how many planned steps went unexecuted,
            # because the continuation checkpoint persists ONE invocation and no
            # plan remainder, and the plan lived in the model loop's working state
            # that the resume does not replay.
            completed_outcome = (
                "approved_effect_complete" if resumed_route else "execution_complete"
            )
            publication_id = commit_run_terminal_in_transaction(
                conn,
                run_id=target.run_id,
                attempt_id=target.attempt_id,
                status=RunState.COMPLETED,
                outcome=(
                    "refused" if output_verdict is RunOutputVerdict.REFUSED else completed_outcome
                ),
                output_verdict=output_verdict,
                allowed_from={
                    RunState.RUNNING: frozenset({RunState.COMPLETED}),
                    RunState.WAITING_APPROVAL: frozenset({RunState.COMPLETED}),
                },
                step_id="run-finalizer",
                now=now,
                terminal_intent_hash=finalizer_intent_hash,
                finalizer_metadata=metadata,
            )
            publication = conn.execute(
                sa.select(ne_agent_run_lifecycle_outbox).where(
                    ne_agent_run_lifecycle_outbox.c.publication_id == publication_id
                )
            ).one()
            conn.execute(
                ne_agent_run_history_attachments.insert().values(
                    run_id=target.run_id,
                    conversation_id=target.conversation_id,
                    owner_principal_id=run.owner_principal_id,
                    turn_id=target.turn_id,
                    turn_sequence=target.turn_sequence,
                    attachment_id=target.attachment_id,
                    attempt_id=target.attempt_id,
                    protected_domain=protected_domain.value,
                    protected_ref=protected_ref,
                    protected_hash=envelope.served_output_hash,
                    content_status=content_status.value,
                    created_at=now,
                )
            )
            finalization_id = _id()
            conn.execute(
                ne_agent_run_finalizations.insert().values(
                    finalization_id=finalization_id,
                    run_id=target.run_id,
                    attempt_id=target.attempt_id,
                    turn_id=target.turn_id,
                    target_hash=metadata["target_hash"],
                    delivery_set_id=observations.delivery_set_id,
                    delivery_set_hash=observations.delivery_set_hash,
                    audit_receipt_set_hash=metadata["audit_receipt_set_hash"],
                    approval_set_hash=metadata["approval_set_hash"],
                    evidence_set_hash=metadata["evidence_set_hash"],
                    verifier_set_hash=metadata["verifier_set_hash"],
                    envelope_hash=envelope_hash,
                    mapping_state=mapping_state,
                    output_verdict=output_verdict.value,
                    protected_output_kind=protected_domain.value,
                    protected_output_ref=protected_ref,
                    protected_output_hash=envelope.served_output_hash,
                    terminal_event_id=str(publication.event_id),
                    publication_id=publication_id,
                    finalizer_intent_hash=finalizer_intent_hash,
                    created_at=now,
                )
            )
        return RunFinalizerReceipt(
            run_id=target.run_id,
            attempt_id=target.attempt_id,
            terminal_event_id=str(publication.event_id),
            publication_id=publication_id,
            protected_output_ref=protected_ref,
            protected_output_hash=envelope.served_output_hash,
            output_verdict=output_verdict.value,
            mapping_state=mapping_state,
            claim_ids=claim_ids,
            finalizer_intent_hash=finalizer_intent_hash,
            target_hash=str(metadata["target_hash"]),
            protected_output_kind=cast(
                Literal["protected_turn_content", "protected_source_output"],
                protected_domain.value,
            ),
            envelope_hash=envelope_hash,
            delivery_set_id=observations.delivery_set_id,
            delivery_set_hash=observations.delivery_set_hash,
            audit_receipt_set_hash=str(metadata["audit_receipt_set_hash"]),
            approval_set_hash=str(metadata["approval_set_hash"]),
            evidence_set_hash=str(metadata["evidence_set_hash"]),
            verifier_set_hash=str(metadata["verifier_set_hash"]),
        )

    def _ensure_failure_reason(
        self,
        conn: sa.Connection,
        *,
        target: RunTurnTarget,
        failure_class: str | None,
        now: datetime,
    ) -> None:
        """Guarantee a failed Run has something honest to say about why.

        This is the SOLE writer of every non-completed terminal, so it is the
        one place that can promise a failed Run is never silent - including for
        failure paths that checkpoint nothing at all
        (``RunEffectSettlementUncertain``, ``SourceAuthorityWithheld``) and for
        any future one.  A step that already recorded its own reason keeps it
        untouched: the floor never overwrites a better answer.
        """

        from ne.runtime.run_workflow import (
            _canonical,
            _digest,
            ne_agent_run_execution_checkpoints,
        )

        recorded = conn.execute(
            sa.select(ne_agent_run_execution_checkpoints.c.checkpoint_id).where(
                ne_agent_run_execution_checkpoints.c.run_id == target.run_id,
                ne_agent_run_execution_checkpoints.c.attempt_id == target.attempt_id,
                ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
            )
        ).one_or_none()
        if recorded is not None:
            return
        safe_detail: dict[str, str] = {
            "error_class": safe_failure_class(failure_class),
            "error_reason": derived_failure_reason(failure_class),
        }
        conn.execute(
            ne_agent_run_execution_checkpoints.insert().values(
                checkpoint_id=_id(),
                run_id=target.run_id,
                attempt_id=target.attempt_id,
                stage="execution_failed",
                intent_hash=_digest({"stage": "execution_failed", "safe_detail": safe_detail}),
                safe_detail=_canonical(safe_detail),
                created_at=now,
            )
        )

    def _finalize_unavailable_sync(
        self, candidate: RunUnavailableTerminalCandidate, lease: RunFinalizerLease
    ) -> RunFinalizerReceipt:
        with self._store._engine.begin() as conn:
            return self.finalize_unavailable_in_transaction(conn, candidate, lease=lease)

    def finalize_unavailable_in_transaction(
        self,
        conn: sa.Connection,
        candidate: RunUnavailableTerminalCandidate,
        *,
        lease: RunFinalizerLease,
    ) -> RunFinalizerReceipt:
        """Compose an unavailable terminal inside an owning approval transaction."""

        from ne.runtime.run_workflow import _database_now, commit_run_terminal_in_transaction

        target = candidate.target
        now = _database_now(conn)
        run = self._lock_exact_target(conn, target)
        delivery_set_id, delivery_set_hash_value = self._unavailable_delivery_binding(
            conn, target, candidate.delivery
        )
        audits = self._load_audits(conn, target)
        approvals = self._observed_approvals(conn, target)
        if not audits or not any(item.step_id == "run-execution" for item in audits):
            raise ValueError("mandatory Run execution audit receipt is absent")
        if any(item.config_hash != run.config_hash for item in audits):
            raise ValueError("Run audit receipt config hash differs from the accepted Run")
        envelope_hash = _hash(
            {
                "schema": "ne.run-unavailable-envelope.v1",
                "status": candidate.status,
                "failure_class": candidate.failure_class,
            }
        )
        metadata = self._finalizer_metadata(
            target=target,
            protected_output_kind="unavailable",
            protected_output_ref=None,
            protected_output_hash=None,
            mapping_state="unavailable",
            envelope_hash=envelope_hash,
            delivery_set_id=delivery_set_id,
            delivery_set_hash=delivery_set_hash_value,
            audit_receipts=audits,
            approvals=approvals,
            evidence=(),
            verifiers=(),
        )
        intent = _hash(
            {
                "schema": "ne.run-finalizer-unavailable.v1",
                "target": target.model_dump(mode="json"),
                "status": candidate.status,
                "outcome": candidate.outcome,
                "failure_class": candidate.failure_class,
                "finalizer_metadata": metadata,
            }
        )
        replay = self._replay(conn, target.run_id, intent)
        if replay is not None:
            return replay
        self._validate_lease(conn, target, lease, now=now)
        status = RunState(candidate.status)
        output_verdict = (
            RunOutputVerdict.REFUSED
            if (candidate.status, candidate.outcome) == ("completed", "refused")
            else RunOutputVerdict.UNAVAILABLE
        )
        publication_id = commit_run_terminal_in_transaction(
            conn,
            run_id=target.run_id,
            attempt_id=target.attempt_id,
            status=status,
            outcome=candidate.outcome,
            output_verdict=output_verdict,
            allowed_from={
                RunState.RUNNING: frozenset({status}),
                RunState.WAITING_APPROVAL: frozenset({status}),
                RunState.CANCELLING: frozenset({status}),
            },
            step_id="run-finalizer",
            now=now,
            terminal_intent_hash=intent,
            finalizer_metadata=metadata,
        )
        if status is RunState.FAILED:
            self._ensure_failure_reason(
                conn, target=target, failure_class=candidate.failure_class, now=now
            )
        publication = conn.execute(
            sa.select(ne_agent_run_lifecycle_outbox).where(
                ne_agent_run_lifecycle_outbox.c.publication_id == publication_id
            )
        ).one()
        conn.execute(
            ne_agent_run_finalizations.insert().values(
                finalization_id=_id(),
                run_id=target.run_id,
                attempt_id=target.attempt_id,
                turn_id=target.turn_id,
                target_hash=metadata["target_hash"],
                delivery_set_id=delivery_set_id,
                delivery_set_hash=delivery_set_hash_value,
                audit_receipt_set_hash=metadata["audit_receipt_set_hash"],
                approval_set_hash=metadata["approval_set_hash"],
                evidence_set_hash=metadata["evidence_set_hash"],
                verifier_set_hash=metadata["verifier_set_hash"],
                envelope_hash=envelope_hash,
                mapping_state="unavailable",
                output_verdict=output_verdict.value,
                protected_output_kind="unavailable",
                protected_output_ref=None,
                protected_output_hash=None,
                terminal_event_id=str(publication.event_id),
                publication_id=publication_id,
                finalizer_intent_hash=intent,
                created_at=now,
            )
        )
        return RunFinalizerReceipt(
            run_id=target.run_id,
            attempt_id=target.attempt_id,
            terminal_event_id=str(publication.event_id),
            publication_id=publication_id,
            protected_output_ref=None,
            protected_output_hash=None,
            output_verdict=output_verdict.value,
            mapping_state="unavailable",
            claim_ids=(),
            finalizer_intent_hash=intent,
            target_hash=str(metadata["target_hash"]),
            protected_output_kind="unavailable",
            envelope_hash=envelope_hash,
            delivery_set_id=delivery_set_id,
            delivery_set_hash=delivery_set_hash_value,
            audit_receipt_set_hash=str(metadata["audit_receipt_set_hash"]),
            approval_set_hash=str(metadata["approval_set_hash"]),
            evidence_set_hash=str(metadata["evidence_set_hash"]),
            verifier_set_hash=str(metadata["verifier_set_hash"]),
        )

    def _lock_exact_target(self, conn: sa.Connection, target: RunTurnTarget) -> AgentRun:
        row = conn.execute(
            sa.select(ne_agent_runs)
            .where(ne_agent_runs.c.run_id == target.run_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise LookupError("Run finalizer target is unavailable")
        run = AgentRun(
            run_id=str(row.run_id),
            conversation_id=str(row.conversation_id),
            owner_principal_id=str(row.owner_principal_id),
            department=str(row.department),
            conversation_agent_name=str(row.conversation_agent_name),
            agent_id=str(row.agent_id),
            agent_version=int(row.agent_version),
            strategy=str(row.strategy),
            config_hash=str(row.config_hash),
            capability_snapshot_hash=str(row.capability_snapshot_hash),
            capability_snapshot_receipt_id=str(row.capability_snapshot_receipt_id),
            sequence=int(row.sequence),
            display_label=str(row.display_label),
            status=RunState(str(row.status)),
            created_at=row.created_at,
            updated_at=row.updated_at,
            terminal_at=row.terminal_at,
            version=int(row.version),
            output_verdict=None if row.output_verdict is None else str(row.output_verdict),
        )
        authored = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == target.run_id,
                ne_agent_run_turn_targets.c.attempt_id == target.attempt_id,
            )
        ).one_or_none()
        if authored is None or (
            str(authored.conversation_id),
            str(authored.run_id),
            str(authored.turn_id),
            int(authored.turn_sequence),
            str(authored.attachment_id),
            str(authored.attempt_id),
        ) != (
            target.conversation_id,
            target.run_id,
            target.turn_id,
            target.turn_sequence,
            target.attachment_id,
            target.attempt_id,
        ):
            raise ValueError("Run finalizer target is not the exact server-authored tuple")
        return run

    def _validate_lease(
        self,
        conn: sa.Connection,
        target: RunTurnTarget,
        lease: RunFinalizerLease,
        *,
        now: datetime,
    ) -> None:
        from ne.runtime.run_workflow import ne_agent_run_workflow_leases

        if (lease.run_id, lease.attempt_id) != (target.run_id, target.attempt_id):
            raise ValueError("Run finalizer lease targets a different Run attempt")
        row = conn.execute(
            sa.select(ne_agent_run_workflow_leases)
            .where(ne_agent_run_workflow_leases.c.run_id == target.run_id)
            .with_for_update()
        ).one_or_none()
        expires_at = None if row is None else row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if (
            row is None
            or str(row.dispatch_id) != lease.dispatch_id
            or str(row.attempt_id) != lease.attempt_id
            or str(row.lease_token) != lease.lease_token
            or int(row.lease_epoch) != lease.lease_epoch
            or str(row.state) != "active"
            or expires_at is None
            or expires_at <= now
        ):
            raise RuntimeError("Run atomic finalizer lost its exact execution lease")

    def _load_audits(
        self, conn: sa.Connection, target: RunTurnTarget
    ) -> tuple[RunAuditBinding, ...]:
        rows = conn.execute(
            sa.select(ne_agent_run_audit_receipts)
            .where(
                ne_agent_run_audit_receipts.c.run_id == target.run_id,
                ne_agent_run_audit_receipts.c.attempt_id == target.attempt_id,
            )
            .order_by(
                ne_agent_run_audit_receipts.c.created_at,
                ne_agent_run_audit_receipts.c.run_audit_receipt_id,
            )
        ).all()
        return tuple(
            RunAuditBinding(
                run_audit_receipt_id=str(row.run_audit_receipt_id),
                run_event_id=str(row.run_event_id),
                step_id=None if row.step_id is None else str(row.step_id),
                tool_call_id=None if row.tool_call_id is None else str(row.tool_call_id),
                approval_id=None if row.approval_id is None else str(row.approval_id),
                decision_kind=str(row.decision_kind),  # type: ignore[arg-type]
                audit_event_id=str(row.audit_event_id),
                audit_sequence=int(row.audit_sequence),
                audit_record_hash=str(row.audit_record_hash),
                config_hash=str(row.config_hash),
            )
            for row in rows
        )

    def _unavailable_delivery_binding(
        self,
        conn: sa.Connection,
        target: RunTurnTarget,
        observation: RunDeliveryObservation | None,
    ) -> tuple[str, str]:
        rows = conn.execute(
            sa.select(ne_agent_delivery_receipts)
            .where(
                ne_agent_delivery_receipts.c.run_id == target.run_id,
                ne_agent_delivery_receipts.c.receiving_attempt_id == target.attempt_id,
            )
            .order_by(
                ne_agent_delivery_receipts.c.created_at,
                ne_agent_delivery_receipts.c.delivery_receipt_id,
            )
        ).all()
        ids = tuple(str(row.delivery_receipt_id) for row in rows)
        if observation is not None and observation.complete:
            complete = True
            if (
                (observation.run_id, observation.attempt_id) != (target.run_id, target.attempt_id)
                or observation.delivery_receipt_ids != ids
                or observation.delivery_set_id
                != run_delivery_set_id(target.run_id, target.attempt_id, ids)
            ):
                raise ValueError("unavailable terminal delivery observation is mismatched")
            set_id = observation.delivery_set_id
        else:
            complete = False
            set_id = _hash(
                {
                    "schema": "ne.run-delivery-set-unknown.v1",
                    "run_id": target.run_id,
                    "attempt_id": target.attempt_id,
                    "persisted_receipt_ids": ids,
                }
            )
        return set_id, _hash(
            {
                "schema": "ne.run-delivery-set-terminal.v1",
                "state": "complete" if complete else "unknown",
                "delivery_set_id": set_id,
                "members": [
                    {
                        "delivery_receipt_id": str(row.delivery_receipt_id),
                        "delivery_kind": str(row.delivery_kind),
                        "decision": str(row.decision),
                        "protected_ref": str(row.protected_ref),
                        "protected_hash": str(row.protected_hash),
                        "stamp_set_hash": str(row.stamp_set_hash),
                        "event_id": str(row.event_id),
                    }
                    for row in rows
                ],
            }
        )

    @staticmethod
    def _finalizer_metadata(
        *,
        target: RunTurnTarget,
        protected_output_kind: str | None,
        protected_output_ref: str | None,
        protected_output_hash: str | None,
        mapping_state: str,
        envelope_hash: str,
        delivery_set_id: str,
        delivery_set_hash: str,
        audit_receipts: tuple[RunAuditBinding, ...],
        approvals: tuple[RunApprovalBinding, ...],
        evidence: tuple[RunEvidenceBinding, ...],
        verifiers: tuple[RunVerifierBinding, ...],
    ) -> dict[str, str | None]:
        def set_hash(schema: str, members: tuple[_ClosedModel, ...]) -> str:
            return _hash(
                {
                    "schema": schema,
                    "members": [member.model_dump(mode="json") for member in members],
                }
            )

        return {
            "target_hash": _hash(
                {"schema": "ne.run-terminal-target.v1", **target.model_dump(mode="json")}
            ),
            "protected_output_kind": protected_output_kind,
            "protected_output_ref": protected_output_ref,
            "protected_output_hash": protected_output_hash,
            "mapping_state": mapping_state,
            "envelope_hash": envelope_hash,
            "delivery_set_id": delivery_set_id,
            "delivery_set_hash": delivery_set_hash,
            "audit_receipt_set_hash": set_hash("ne.run-audit-receipt-set.v1", audit_receipts),
            "approval_set_hash": set_hash("ne.run-approval-set.v1", approvals),
            "evidence_set_hash": set_hash("ne.run-evidence-binding-set.v1", evidence),
            "verifier_set_hash": set_hash("ne.run-verifier-binding-set.v1", verifiers),
        }

    def _validate_server_observations(
        self,
        conn: sa.Connection,
        run: AgentRun,
        observations: RunFinalizerObservations,
    ) -> None:
        target = observations.target
        delivery_rows = conn.execute(
            sa.select(ne_agent_delivery_receipts)
            .where(
                ne_agent_delivery_receipts.c.run_id == target.run_id,
                ne_agent_delivery_receipts.c.receiving_attempt_id == target.attempt_id,
            )
            .order_by(
                ne_agent_delivery_receipts.c.created_at,
                ne_agent_delivery_receipts.c.delivery_receipt_id,
            )
        ).all()
        actual_deliveries = tuple(
            RunDeliveryBinding(
                delivery_receipt_id=str(row.delivery_receipt_id),
                delivery_kind=str(row.delivery_kind),  # type: ignore[arg-type]
                decision=str(row.decision),  # type: ignore[arg-type]
                protected_ref=str(row.protected_ref),
                protected_hash=str(row.protected_hash),
                stamp_set_hash=str(row.stamp_set_hash),
                event_id=str(row.event_id),
            )
            for row in delivery_rows
        )
        if actual_deliveries != observations.deliveries:
            raise ValueError("terminal delivery set differs from server-observed receipts")
        expected_delivery_id = _hash(
            [
                target.run_id,
                target.attempt_id,
                [item.delivery_receipt_id for item in actual_deliveries],
            ]
        )
        if observations.delivery_set_id != expected_delivery_id:
            raise ValueError("terminal delivery set identity is not server-authored")

        audit_rows = conn.execute(
            sa.select(ne_agent_run_audit_receipts)
            .where(
                ne_agent_run_audit_receipts.c.run_id == target.run_id,
                ne_agent_run_audit_receipts.c.attempt_id == target.attempt_id,
            )
            .order_by(
                ne_agent_run_audit_receipts.c.created_at,
                ne_agent_run_audit_receipts.c.run_audit_receipt_id,
            )
        ).all()
        actual_audit = tuple(
            RunAuditBinding(
                run_audit_receipt_id=str(row.run_audit_receipt_id),
                run_event_id=str(row.run_event_id),
                step_id=None if row.step_id is None else str(row.step_id),
                tool_call_id=None if row.tool_call_id is None else str(row.tool_call_id),
                approval_id=None if row.approval_id is None else str(row.approval_id),
                decision_kind=str(row.decision_kind),  # type: ignore[arg-type]
                audit_event_id=str(row.audit_event_id),
                audit_sequence=int(row.audit_sequence),
                audit_record_hash=str(row.audit_record_hash),
                config_hash=str(row.config_hash),
            )
            for row in audit_rows
        )
        if not actual_audit or actual_audit != observations.audit_receipts:
            raise ValueError("mandatory Run audit receipt set is incomplete or mismatched")
        if any(item.config_hash != run.config_hash for item in actual_audit):
            raise ValueError("Run audit receipt config hash differs from the accepted Run")
        if not any(item.step_id == "run-execution" for item in actual_audit):
            raise ValueError("mandatory Run execution audit receipt is absent")
        audit_by_event = {
            (item.run_event_id, item.decision_kind, item.approval_id) for item in actual_audit
        }
        for delivery in observations.deliveries:
            if (delivery.event_id, "delivery", None) not in audit_by_event:
                raise ValueError("delivery decision lacks its mandatory audit receipt")
        for evidence in observations.evidence:
            if (evidence.producing_event_id, "evidence", None) not in audit_by_event:
                raise ValueError("evidence record lacks its mandatory audit receipt")
        for approval in observations.approvals:
            if not any(
                item.run_audit_receipt_id == approval.audit_receipt_id
                and item.approval_id == approval.approval_id
                and item.decision_kind == "approval"
                for item in actual_audit
            ):
                raise ValueError("approval join lacks its mandatory exact audit receipt")

        self._validate_approvals(conn, observations)
        self._validate_evidence(conn, observations)
        self._validate_verifiers(conn, observations)

    def _validate_approvals(
        self, conn: sa.Connection, observations: RunFinalizerObservations
    ) -> None:
        from ne.runtime.run_checkpoint import (
            ne_agent_run_approval_joins,
            ne_agent_run_continuation_checkpoints,
        )

        target = observations.target
        rows = conn.execute(
            sa.select(ne_agent_run_approval_joins, ne_agent_run_continuation_checkpoints.c.status)
            .join(
                ne_agent_run_continuation_checkpoints,
                ne_agent_run_continuation_checkpoints.c.checkpoint_id
                == ne_agent_run_approval_joins.c.checkpoint_id,
            )
            .where(
                ne_agent_run_approval_joins.c.run_id == target.run_id,
                ne_agent_run_approval_joins.c.attempt_id == target.attempt_id,
            )
            .order_by(
                ne_agent_run_approval_joins.c.created_at,
                ne_agent_run_approval_joins.c.approval_id,
            )
        ).all()
        actual: list[RunApprovalBinding] = []
        for row in rows:
            if str(row.status) not in {
                "approved",
                "resumed",
                "consumed",
                "rejected",
                "expired",
                "source_withheld",
            }:
                raise ValueError("terminal Run retains an unresolved approval checkpoint")
            receipt_ids = tuple(
                str(value)
                for value in conn.execute(
                    sa.select(ne_agent_run_audit_receipts.c.run_audit_receipt_id).where(
                        ne_agent_run_audit_receipts.c.run_id == target.run_id,
                        ne_agent_run_audit_receipts.c.attempt_id == target.attempt_id,
                        ne_agent_run_audit_receipts.c.approval_id == row.approval_id,
                        ne_agent_run_audit_receipts.c.decision_kind == "approval",
                    )
                ).scalars()
            )
            if len(receipt_ids) != 1:
                raise ValueError("approval join lacks its exact mandatory audit receipt")
            actual.append(
                RunApprovalBinding(
                    approval_id=str(row.approval_id),
                    checkpoint_id=str(row.checkpoint_id),
                    protected_invocation_ref=str(row.protected_invocation_id),
                    protected_invocation_hash=str(row.protected_invocation_hash),
                    audit_receipt_id=receipt_ids[0],
                )
            )
        if tuple(actual) != observations.approvals:
            raise ValueError("terminal approval join set is incomplete or mismatched")

    def _validate_evidence(
        self, conn: sa.Connection, observations: RunFinalizerObservations
    ) -> None:
        for binding in observations.evidence:
            row = conn.execute(
                sa.select(
                    ne_agent_evidence_records,
                    ne_agent_evidence_locators.c.locator_hash,
                )
                .join(
                    ne_agent_evidence_locators,
                    ne_agent_evidence_locators.c.locator_id
                    == ne_agent_evidence_records.c.locator_id,
                )
                .where(ne_agent_evidence_records.c.evidence_id == binding.evidence_id)
            ).one_or_none()
            delivery = conn.execute(
                sa.select(ne_agent_delivery_receipts).where(
                    ne_agent_delivery_receipts.c.delivery_receipt_id == binding.delivery_receipt_id
                )
            ).one_or_none()
            if (
                row is None
                or delivery is None
                or str(row.run_id) != observations.target.run_id
                or str(row.attempt_id) != observations.target.attempt_id
                or str(row.turn_id) != observations.target.turn_id
                or str(row.producing_event_id) != binding.producing_event_id
                or str(row.locator_hash) != binding.locator_hash
                or str(row.pending_result_id) != str(delivery.protected_ref)
                or str(delivery.run_id) != observations.target.run_id
                or str(delivery.receiving_attempt_id) != observations.target.attempt_id
                or str(delivery.decision) != "served"
            ):
                raise ValueError("claim evidence lacks its exact same-Run served receipt set")
            expected_citation_hash = served_evidence_binding_hash(
                claim_key=binding.claim_key,
                claim_content_hash=binding.claim_content_hash,
                served_output_hash=binding.served_output_hash,
                span_start_byte=binding.span_start_byte,
                span_end_byte=binding.span_end_byte,
                evidence_id=binding.evidence_id,
                delivery_receipt_id=binding.delivery_receipt_id,
                protected_ref=str(delivery.protected_ref),
                protected_hash=str(delivery.protected_hash),
            )
            if not hmac.compare_digest(expected_citation_hash, binding.served_citation_ref_hash):
                raise ValueError("served evidence binding hash is invalid")
            source_row = conn.execute(
                sa.select(ne_agent_pending_source_tool_results).where(
                    ne_agent_pending_source_tool_results.c.pending_result_id
                    == delivery.protected_ref
                )
            ).one_or_none()
            if source_row is None:
                raise ValueError("served evidence receipt does not name its protected Tool result")
            raw_binding = source_row.target_binding
            if not isinstance(raw_binding, list) or not raw_binding:
                raise ValueError("served evidence protected target binding is malformed")
            packed = self._store._cipher.materialize_for_target(
                _row_envelope(
                    source_row,
                    domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                    row_id_name="pending_result_id",
                ),
                domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                row_id=str(source_row.pending_result_id),
                owner_principal_id=str(source_row.owner_principal_id),
                binding=protected_payload_aad_binding(
                    target_kind=str(source_row.target_kind),
                    target_binding=tuple(str(value) for value in raw_binding),
                    content_status=None,
                ),
            )
            _plaintext, _stamps, evidence_ids = _unpack_source_bound_payload_and_stamps(packed)
            exact_single_evidence = not evidence_ids and raw_binding[-1] == binding.evidence_id
            if not exact_single_evidence and binding.evidence_id not in evidence_ids:
                raise ValueError("served evidence is absent from its protected result receipt")

    def _validate_verifiers(
        self, conn: sa.Connection, observations: RunFinalizerObservations
    ) -> None:
        for binding in observations.verifiers:
            event = conn.execute(
                sa.select(ne_agent_run_events).where(
                    ne_agent_run_events.c.event_id == binding.event_id,
                    ne_agent_run_events.c.run_id == observations.target.run_id,
                    ne_agent_run_events.c.attempt_id == observations.target.attempt_id,
                )
            ).one_or_none()
            if event is None or str(event.event_kind) != "claim.verifier":
                raise ValueError("claim verifier lacks its exact same-Run event")
            try:
                detail = json.loads(str(event.safe_detail))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("claim verifier event detail is invalid") from exc
            expected_detail = {
                "claim_key": binding.claim_key,
                "claim_content_hash": binding.claim_content_hash,
                "served_output_hash": binding.served_output_hash,
                "span_start_byte": binding.span_start_byte,
                "span_end_byte": binding.span_end_byte,
                "verifier_method": binding.verifier_method,
                "verifier_version": binding.verifier_version,
                "result_hash": binding.result_hash,
            }
            if detail != expected_detail:
                raise ValueError("claim verifier event does not bind its exact claim")
            expected_receipt_hash = _bytes_hash(
                _canonical_json(
                    [
                        observations.target.run_id,
                        binding.claim_key,
                        binding.claim_content_hash,
                        binding.served_output_hash,
                        binding.span_start_byte,
                        binding.span_end_byte,
                        binding.verifier_method,
                        binding.verifier_version,
                        binding.result_hash,
                        binding.event_id,
                    ]
                ).encode("utf-8")
            )
            if not hmac.compare_digest(expected_receipt_hash, binding.receipt_hash):
                raise ValueError("claim verifier receipt hash is invalid")

    def _validate_claim_bindings(
        self,
        envelope: StructuredRunOutputEnvelope,
        observations: RunFinalizerObservations,
    ) -> None:
        evidence = {item.evidence_key: item for item in observations.evidence}
        verifiers = {item.verifier_key: item for item in observations.verifiers}
        used_evidence: set[str] = set()
        used_verifiers: set[str] = set()
        for claim in envelope.claims:
            if claim.evidence_key is not None:
                binding = evidence.get(claim.evidence_key)
                if binding is None or (
                    binding.claim_key,
                    binding.claim_content_hash,
                    binding.served_output_hash,
                    binding.span_start_byte,
                    binding.span_end_byte,
                ) != (
                    claim.claim_key,
                    claim.claim_content_hash,
                    envelope.served_output_hash,
                    claim.span_start_byte,
                    claim.span_end_byte,
                ):
                    raise ValueError("claim evidence was swapped or mismatched")
                # The binding, not the RECORD, is what a claim consumes.
                # Several claims may cite one evidence record; none may share
                # the other's binding row, because that row carries the claim
                # key and span this claim's verifier receipt was minted against.
                if claim.evidence_key in used_evidence:
                    raise ValueError("one evidence binding cannot be reused for two claims")
                used_evidence.add(claim.evidence_key)
            if claim.verifier_key is not None:
                verifier = verifiers.get(claim.verifier_key)
                if (
                    claim.evidence_key is None
                    or verifier is None
                    or (
                        verifier.claim_key,
                        verifier.claim_content_hash,
                        verifier.served_output_hash,
                        verifier.span_start_byte,
                        verifier.span_end_byte,
                    )
                    != (
                        claim.claim_key,
                        claim.claim_content_hash,
                        envelope.served_output_hash,
                        claim.span_start_byte,
                        claim.span_end_byte,
                    )
                ):
                    raise ValueError("claim verifier was swapped or mismatched")
                if verifier.verifier_receipt_id in used_verifiers:
                    raise ValueError("one verifier receipt cannot verify two claims")
                used_verifiers.add(verifier.verifier_receipt_id)
        if used_evidence != {item.evidence_key for item in observations.evidence} or (
            used_verifiers != {item.verifier_receipt_id for item in observations.verifiers}
        ):
            raise ValueError("terminal envelope did not consume the exact observation set")

    @staticmethod
    def _mapping(
        envelope: StructuredRunOutputEnvelope,
        observations: RunFinalizerObservations,
    ) -> tuple[Literal["complete", "partial", "unavailable"], RunOutputVerdict]:
        if envelope.disposition == "refused":
            return "unavailable", RunOutputVerdict.REFUSED
        associated = sum(claim.evidence_key is not None for claim in envelope.claims)
        if not associated:
            return "unavailable", RunOutputVerdict.UNAVAILABLE
        if associated != len(envelope.claims):
            return "partial", RunOutputVerdict.UNAVAILABLE
        verifier_keys = {item.verifier_key for item in observations.verifiers}
        verified = all(claim.verifier_key in verifier_keys for claim in envelope.claims)
        return "complete", (RunOutputVerdict.VERIFIED if verified else RunOutputVerdict.GROUNDED)

    def _protect_output(
        self, conn: sa.Connection, candidate: RunTerminalCandidate
    ) -> tuple[str, PrivatePayloadDomain, ProtectedContentStatus]:
        target = candidate.observations.target.canonical()
        owner = str(
            conn.execute(
                sa.select(ne_agent_runs.c.owner_principal_id).where(
                    ne_agent_runs.c.run_id == target.run_id
                )
            ).scalar_one()
        )
        deliveries = tuple(
            item for item in candidate.observations.deliveries if item.decision == "served"
        )
        # NE-GA-RUN1: a REFUSED terminal is stored source-free even when the
        # attempt DID serve deliveries. The envelope validator already forces a
        # refused output to declare zero claims, and its text is a server-
        # authored account of the decision (reason classes + counts only, C12)
        # — never the withheld draft and never source-derived content — so
        # binding it into the source-bound container would misfile decision
        # prose as source output AND make the one beat where the system
        # correctly declines unreadable (`run_projection._preview_target`
        # discloses refusals only from ``protected_turn_content``). The served
        # deliveries themselves keep their receipts, stamps and evidence rows
        # untouched; only the terminal text's storage domain is decided here.
        if candidate.envelope.disposition == "refused":
            deliveries = ()
        domain = (
            PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT
            if deliveries
            else PrivatePayloadDomain.PROTECTED_TURN_CONTENT
        )
        table = (
            ne_agent_protected_source_outputs if deliveries else ne_agent_protected_turn_contents
        )
        row_id_name = "protected_output_id" if deliveries else "turn_content_id"
        content_status = (
            ProtectedContentStatus.SOURCE_BOUND_CONTAINER
            if deliveries
            else ProtectedContentStatus.SOURCE_FREE
        )
        stamps = self._delivery_stamps(conn, deliveries) if deliveries else ()
        # The packed payload names the SET of records this output is bound to,
        # one entry per DISTINCT record — `_unpack_source_bound_payload_and_stamps`
        # refuses a repeat. Two claims citing one record is one record here, and
        # first-seen order keeps the packing deterministic.
        evidence_ids = tuple(
            dict.fromkeys(item.evidence_id for item in candidate.observations.evidence)
        )
        storage_plaintext = (
            _pack_source_bound_payload(candidate.output_bytes, stamps, evidence_ids=evidence_ids)
            if deliveries
            else candidate.output_bytes
        )
        target_hash = protected_target_hash(
            domain, owner, AGENT_RUN_TURN_TARGET_KIND, target.binding()
        )
        existing = conn.execute(
            sa.select(table).where(
                table.c.target_kind == AGENT_RUN_TURN_TARGET_KIND,
                table.c.target_hash == target_hash,
            )
        ).one_or_none()
        if existing is not None:
            raise RuntimeError("protected terminal target exists without finalization receipt")
        row_id = _id()
        envelope = self._store._cipher.encrypt_for_target(
            domain=domain,
            row_id=row_id,
            owner_principal_id=owner,
            binding=protected_payload_aad_binding(
                target_kind=AGENT_RUN_TURN_TARGET_KIND,
                target_binding=target.binding(),
                content_status=(None if deliveries else content_status),
            ),
            plaintext=storage_plaintext,
        )
        conn.execute(
            table.insert().values(
                **{
                    row_id_name: row_id,
                    "run_id": target.run_id,
                    "owner_principal_id": owner,
                    "target_kind": AGENT_RUN_TURN_TARGET_KIND,
                    "target_hash": target_hash,
                    "target_binding": list(target.binding()),
                    **{key: value for key, value in target.as_columns().items() if key != "run_id"},
                    "content_status": None if deliveries else content_status.value,
                    "stamp_set_hash": _stamp_set_hash(stamps) if stamps else None,
                    "created_at": datetime.now(UTC),
                    **_envelope_values(envelope),
                }
            )
        )
        return row_id, domain, content_status

    def _delivery_stamps(
        self, conn: sa.Connection, deliveries: tuple[RunDeliveryBinding, ...]
    ) -> tuple[SourceAuthorityStamp, ...]:
        stamps: list[SourceAuthorityStamp] = []
        for delivery in deliveries:
            matched = None
            for table, id_name, domain in (
                (
                    ne_agent_pending_source_tool_results,
                    "pending_result_id",
                    PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
                ),
                (
                    ne_agent_protected_source_outputs,
                    "protected_output_id",
                    PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
                ),
            ):
                row = conn.execute(
                    sa.select(table).where(table.c[id_name] == delivery.protected_ref)
                ).one_or_none()
                if row is None:
                    continue
                raw_binding = row.target_binding
                if not isinstance(raw_binding, list) or not raw_binding:
                    raise ValueError("delivered protected reference has no exact target binding")
                packed = self._store._cipher.materialize_for_target(
                    _row_envelope(row, domain=domain, row_id_name=id_name),
                    domain=domain,
                    row_id=delivery.protected_ref,
                    owner_principal_id=str(row.owner_principal_id),
                    binding=protected_payload_aad_binding(
                        target_kind=str(row.target_kind),
                        target_binding=tuple(str(value) for value in raw_binding),
                        content_status=None,
                    ),
                )
                plaintext, source_stamps, _ = _unpack_source_bound_payload_and_stamps(packed)
                if (
                    _bytes_hash(plaintext) != delivery.protected_hash
                    or _stamp_set_hash(source_stamps) != delivery.stamp_set_hash
                ):
                    raise ValueError("delivery receipt differs from its protected source payload")
                matched = source_stamps
                break
            if matched is None:
                raise ValueError("delivery receipt protected source payload is unavailable")
            stamps.extend(matched)
        return canonical_source_authority_stamps(stamps)

    def _insert_claims(
        self,
        conn: sa.Connection,
        candidate: RunTerminalCandidate,
        *,
        protected_ref: str,
        protected_domain: PrivatePayloadDomain,
        now: datetime,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        target = candidate.observations.target
        run = conn.execute(
            sa.select(ne_agent_runs.c.owner_principal_id, ne_agent_runs.c.agent_version).where(
                ne_agent_runs.c.run_id == target.run_id
            )
        ).one()
        evidence_keys = {item.evidence_key for item in candidate.observations.evidence}
        verifier_keys = {item.verifier_key for item in candidate.observations.verifiers}
        claim_ids: list[str] = []
        by_key: dict[str, str] = {}
        for index, claim in enumerate(candidate.envelope.claims):
            claim_id = _id()
            claim_ids.append(claim_id)
            by_key[claim.claim_key] = claim_id
            associated = claim.evidence_key in evidence_keys
            verified = claim.verifier_key in verifier_keys
            conn.execute(
                ne_agent_run_claims.insert().values(
                    claim_id=claim_id,
                    run_id=target.run_id,
                    turn_id=target.turn_id,
                    owner_principal_id=str(run.owner_principal_id),
                    agent_version=int(run.agent_version),
                    conversation_id=target.conversation_id,
                    turn_sequence=target.turn_sequence,
                    attachment_id=target.attachment_id,
                    attempt_id=target.attempt_id,
                    claim_key=claim.claim_key,
                    claim_index=index,
                    claim_verdict=(
                        "verified" if verified else "grounded" if associated else "unavailable"
                    ),
                    display_state=(
                        "protected_source_slice"
                        if protected_domain is PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT
                        else "protected_source_free_slice"
                    ),
                    protected_domain=protected_domain.value,
                    protected_ref=protected_ref,
                    served_output_hash=candidate.envelope.served_output_hash,
                    span_start_byte=claim.span_start_byte,
                    span_end_byte=claim.span_end_byte,
                    claim_content_hash=claim.claim_content_hash,
                    association_status=("associated" if associated else "association_unavailable"),
                    created_at=now,
                )
            )
        return tuple(claim_ids), by_key

    def _insert_verifiers(
        self, conn: sa.Connection, candidate: RunTerminalCandidate, *, now: datetime
    ) -> None:
        for binding in candidate.observations.verifiers:
            conn.execute(
                ne_agent_claim_verifier_receipts.insert().values(
                    verifier_receipt_id=binding.verifier_receipt_id,
                    run_id=candidate.observations.target.run_id,
                    claim_key=binding.claim_key,
                    claim_content_hash=binding.claim_content_hash,
                    served_output_hash=binding.served_output_hash,
                    span_start_byte=binding.span_start_byte,
                    span_end_byte=binding.span_end_byte,
                    verifier_method=binding.verifier_method,
                    verifier_version=binding.verifier_version,
                    result_hash=binding.result_hash,
                    event_id=binding.event_id,
                    receipt_hash=binding.receipt_hash,
                    created_at=now,
                )
            )

    def _insert_associations(
        self,
        conn: sa.Connection,
        candidate: RunTerminalCandidate,
        *,
        claim_id_by_key: dict[str, str],
        now: datetime,
    ) -> None:
        evidence = {item.evidence_key: item for item in candidate.observations.evidence}
        for claim in candidate.envelope.claims:
            if claim.evidence_key is None:
                continue
            binding = evidence[claim.evidence_key]
            association_hash = _hash(
                {
                    "schema": "ne.run-claim-evidence.v1",
                    "run_id": candidate.observations.target.run_id,
                    "claim_id": claim_id_by_key[claim.claim_key],
                    "evidence": binding.model_dump(mode="json"),
                }
            )
            conn.execute(
                ne_agent_run_claim_evidence.insert().values(
                    association_id=_id(),
                    run_id=candidate.observations.target.run_id,
                    claim_id=claim_id_by_key[claim.claim_key],
                    evidence_id=binding.evidence_id,
                    locator_hash=binding.locator_hash,
                    served_citation_ref_hash=binding.served_citation_ref_hash,
                    producing_event_id=binding.producing_event_id,
                    delivery_receipt_id=binding.delivery_receipt_id,
                    association_hash=association_hash,
                    created_at=now,
                )
            )

    def _replay(
        self, conn: sa.Connection, run_id: str, intent_hash: str
    ) -> RunFinalizerReceipt | None:
        row = conn.execute(
            sa.select(ne_agent_run_finalizations).where(
                ne_agent_run_finalizations.c.run_id == run_id
            )
        ).one_or_none()
        if row is None:
            return None
        if not hmac.compare_digest(str(row.finalizer_intent_hash), intent_hash):
            raise ValueError("Run finalizer replay changed its exact terminal intent")
        run = conn.execute(
            sa.select(ne_agent_runs.c.output_verdict).where(ne_agent_runs.c.run_id == run_id)
        ).one()
        claim_ids = tuple(
            str(value)
            for value in conn.execute(
                sa.select(ne_agent_run_claims.c.claim_id)
                .where(ne_agent_run_claims.c.run_id == run_id)
                .order_by(ne_agent_run_claims.c.claim_index)
            ).scalars()
        )
        return RunFinalizerReceipt(
            run_id=run_id,
            attempt_id=str(row.attempt_id),
            terminal_event_id=str(row.terminal_event_id),
            publication_id=str(row.publication_id),
            protected_output_ref=(
                None if row.protected_output_ref is None else str(row.protected_output_ref)
            ),
            protected_output_hash=(
                None if row.protected_output_hash is None else str(row.protected_output_hash)
            ),
            output_verdict=str(run.output_verdict),  # type: ignore[arg-type]
            mapping_state=str(row.mapping_state),  # type: ignore[arg-type]
            claim_ids=claim_ids,
            finalizer_intent_hash=intent_hash,
            target_hash=str(row.target_hash),
            protected_output_kind=str(row.protected_output_kind),  # type: ignore[arg-type]
            envelope_hash=str(row.envelope_hash),
            delivery_set_id=str(row.delivery_set_id),
            delivery_set_hash=str(row.delivery_set_hash),
            audit_receipt_set_hash=str(row.audit_receipt_set_hash),
            approval_set_hash=str(row.approval_set_hash),
            evidence_set_hash=str(row.evidence_set_hash),
            verifier_set_hash=str(row.verifier_set_hash),
            replayed=True,
        )


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def claim_set_hash(served_output_hash: str, claims: tuple[RunClaimEnvelopeDeclaration, ...]) -> str:
    """Canonical helper for trusted envelope producers and focused tests."""

    return _hash(
        {
            "schema": "ne.run-claim-set.v1",
            "served_output_hash": served_output_hash,
            "claims": [claim.model_dump(mode="json") for claim in claims],
        }
    )


def delivery_set_hash(delivery_set_id: str, deliveries: tuple[RunDeliveryBinding, ...]) -> str:
    """Canonical helper for the server observation loader."""

    return _hash(
        {
            "schema": "ne.run-delivery-set.v1",
            "delivery_set_id": delivery_set_id,
            "members": [member.model_dump(mode="json") for member in deliveries],
        }
    )


def served_evidence_binding_hash(
    *,
    claim_key: str,
    claim_content_hash: str,
    served_output_hash: str,
    span_start_byte: int,
    span_end_byte: int,
    evidence_id: str,
    delivery_receipt_id: str,
    protected_ref: str,
    protected_hash: str,
) -> str:
    """Hash the server delivery and exact local claim into one non-authorable ref."""

    return _hash(
        {
            "schema": "ne.served-claim-evidence-binding.v1",
            "claim_key": claim_key,
            "claim_content_hash": claim_content_hash,
            "served_output_hash": served_output_hash,
            "span_start_byte": span_start_byte,
            "span_end_byte": span_end_byte,
            "evidence_id": evidence_id,
            "delivery_receipt_id": delivery_receipt_id,
            "protected_ref": protected_ref,
            "protected_hash": protected_hash,
        }
    )
