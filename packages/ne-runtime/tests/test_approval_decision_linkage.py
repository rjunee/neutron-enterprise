# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ADR-0017 / NE-APPROVAL-LINKAGE — a decision chain record links to its request.

``_grant``, ``reject``, and the Tier-3 first-check path in
``DurableApprovalGate`` stamp ``provenance["approval_id"]`` on their decision
``AuditEvent`` (mirroring ``approval.requested``'s existing key), and
``_audit`` returns the appended record's real ``event_id``, threaded onto
``ApprovalOutcome.decision_event_id`` by ``_grant``/``reject``.

Every linkage test below uses **two concurrent requests with different
actions** and asserts each decision links to **its own** request (never
merely *some* request) — a single-fixture test would make every id trivially
match (vacuity trap #1, ``docs/testing/vacuous-guards.md``). Assertions
compare the decision record's provenance value against the ACTUAL
``approval.requested`` record's value (and the real chain row, for the
real-chain test) — never a literal or a mere key-presence check (vacuity
trap #2).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalOutcome, ApprovalStatus
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    ProtectedSourceInvocationRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundApprovalResolution,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.events import new_approval_event
from ne.runtime.approval_store import SOURCE_WITHHELD_STATUS, ne_approval_evidence
from ne.runtime.approvals import (
    PROTECTED_INVOCATION_KEY,
    SOURCE_WITHHELD_REASON,
    DurableApprovalGate,
)
from ne.runtime.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalSourceWithheldFromApproverError,
    RuntimeAuditError,
)
from ne.runtime.source_results import (
    MissingAuthenticatedContextError,
    ProtectedSourceResultGuard,
    SourceAuthorityWithheldError,
    authenticated_request_scope,
    encode_protected_reference,
)
from ne.schema import ApprovalDecision

from .conftest import (
    RecordingAuditSink,
    approval_request,
    approval_settings,
)

ACTION_A = "write.external.email.send"
ACTION_B = "write.internal.notes.append"


def _gate(store: Any, sink: Any, settings: Any = None, **kwargs: Any) -> DurableApprovalGate:
    return DurableApprovalGate(
        store=store, sink=sink, settings=settings or approval_settings(), **kwargs
    )


def _record(sink: RecordingAuditSink, action: str, resource: str) -> Any:
    """The unique sink record for (action, resource); errors loudly if not exactly one."""
    matches = [e for e in sink.with_action(action) if getattr(e, "resource", None) == resource]
    assert len(matches) == 1, (
        f"expected exactly one {action!r} record for resource {resource!r}, found {len(matches)}"
    )
    return matches[0]


def test_granted_record_links_to_its_own_request(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        rid_a = await gate.submit(approval_request(ACTION_A, 2))
        rid_b = await gate.submit(approval_request(ACTION_B, 2))
        outcome_a = await gate.approve(rid_a, approver="carol")
        outcome_b = await gate.approve(rid_b, approver="dave")
        assert outcome_a is not None and outcome_a.status is ApprovalStatus.APPROVED
        assert outcome_b is not None and outcome_b.status is ApprovalStatus.APPROVED
        return rid_a, rid_b

    rid_a, rid_b = asyncio.run(scenario())

    # Control: the two rids actually differ — otherwise linkage would be untestable.
    assert rid_a != rid_b

    for rid, action in ((rid_a, ACTION_A), (rid_b, ACTION_B)):
        resource = f"tool:{action}"
        requested = _record(sink, "approval.requested", resource)
        granted = _record(sink, "approval.granted", resource)
        requested_id = dict(requested.provenance)["approval_id"]
        granted_id = dict(granted.provenance)["approval_id"]
        assert requested_id == rid
        assert granted_id == rid
        assert granted_id == requested_id

    # Cross-check: request A's decision does NOT link to request B's id, and
    # vice versa — proves per-request isolation, not just "some id matched".
    granted_a = dict(_record(sink, "approval.granted", f"tool:{ACTION_A}").provenance)
    granted_b = dict(_record(sink, "approval.granted", f"tool:{ACTION_B}").provenance)
    assert granted_a["approval_id"] != granted_b["approval_id"]


def test_rejected_record_links_to_its_own_request(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        rid_a = await gate.submit(approval_request(ACTION_A, 2))
        rid_b = await gate.submit(approval_request(ACTION_B, 2))
        await gate.reject(rid_a, approver="carol", reason="no")
        await gate.reject(rid_b, approver="dave", reason="no")
        return rid_a, rid_b

    rid_a, rid_b = asyncio.run(scenario())

    assert rid_a != rid_b

    for rid, action in ((rid_a, ACTION_A), (rid_b, ACTION_B)):
        resource = f"tool:{action}"
        requested = _record(sink, "approval.requested", resource)
        rejected = _record(sink, "approval.rejected", resource)
        requested_id = dict(requested.provenance)["approval_id"]
        rejected_id = dict(rejected.provenance)["approval_id"]
        assert requested_id == rid
        assert rejected_id == rid
        assert rejected_id == requested_id

    rejected_a = dict(_record(sink, "approval.rejected", f"tool:{ACTION_A}").provenance)
    rejected_b = dict(_record(sink, "approval.rejected", f"tool:{ACTION_B}").provenance)
    assert rejected_a["approval_id"] != rejected_b["approval_id"]


def test_first_check_record_links_to_its_own_request(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink, approval_settings(tier3_named_approvers=("alice", "bob")))

    async def scenario() -> tuple[str, str]:
        rid_a = await gate.submit(approval_request(ACTION_A, 3))
        rid_b = await gate.submit(approval_request(ACTION_B, 3))
        first_a = await gate.approve(rid_a, approver="alice")
        first_b = await gate.approve(rid_b, approver="alice")
        assert first_a is None and first_b is None
        return rid_a, rid_b

    rid_a, rid_b = asyncio.run(scenario())

    assert rid_a != rid_b

    for rid, action in ((rid_a, ACTION_A), (rid_b, ACTION_B)):
        resource = f"tool:{action}"
        requested = _record(sink, "approval.requested", resource)
        first_check = _record(sink, "approval.first-check", resource)
        prov = dict(first_check.provenance)
        assert prov["approval_id"] == rid
        assert prov["approval_id"] == dict(requested.provenance)["approval_id"]
        # Existing shape preserved.
        assert prov["approver"] == "alice"

    first_a = dict(_record(sink, "approval.first-check", f"tool:{ACTION_A}").provenance)
    first_b = dict(_record(sink, "approval.first-check", f"tool:{ACTION_B}").provenance)
    assert first_a["approval_id"] != first_b["approval_id"]


def test_decision_event_id_matches_real_chain_row(approval_store: Any) -> None:
    """Real chain, no mock past the seam — mirrors test_middleware_to_audit_sink_real.py."""
    from ne.audit import PostgresAuditSink, ensure_audit_schema
    from ne.audit.store import ne_audit_log

    audit_engine = sa.create_engine("sqlite://")
    ensure_audit_schema(audit_engine)
    try:
        sink = PostgresAuditSink(audit_engine)
        gate = _gate(approval_store, sink)

        async def scenario() -> tuple[str, str, Any, Any]:
            rid_granted = await gate.submit(approval_request(ACTION_A, 2))
            rid_rejected = await gate.submit(approval_request(ACTION_B, 2))
            granted_outcome = await gate.approve(rid_granted, approver="carol")
            rejected_outcome = await gate.reject(rid_rejected, approver="dave", reason="no")
            verification = await sink.verify()
            assert verification.valid is True
            return rid_granted, rid_rejected, granted_outcome, rejected_outcome

        rid_granted, rid_rejected, granted_outcome, rejected_outcome = asyncio.run(scenario())
        assert granted_outcome is not None

        with audit_engine.connect() as conn:
            granted_row = conn.execute(
                sa.select(ne_audit_log.c.event_id, ne_audit_log.c.payload).where(
                    sa.and_(
                        ne_audit_log.c.action == "approval.granted",
                        ne_audit_log.c.resource == f"tool:{ACTION_A}",
                    )
                )
            ).one()
            rejected_row = conn.execute(
                sa.select(ne_audit_log.c.event_id, ne_audit_log.c.payload).where(
                    sa.and_(
                        ne_audit_log.c.action == "approval.rejected",
                        ne_audit_log.c.resource == f"tool:{ACTION_B}",
                    )
                )
            ).one()
            requested_row = conn.execute(
                sa.select(ne_audit_log.c.event_id).where(
                    sa.and_(
                        ne_audit_log.c.action == "approval.requested",
                        ne_audit_log.c.resource == f"tool:{ACTION_A}",
                    )
                )
            ).one()

        # Value equality against the row ACTUALLY in the chain — not presence.
        assert granted_outcome.decision_event_id == granted_row.event_id
        assert rejected_outcome.decision_event_id == rejected_row.event_id

        granted_provenance = dict(granted_row.payload)["provenance"]
        rejected_provenance = dict(rejected_row.payload)["provenance"]
        assert granted_provenance["approval_id"] == rid_granted
        assert rejected_provenance["approval_id"] == rid_rejected

        # Controls: the decision id is NOT the requested record's id, and the
        # two decision ids differ from each other.
        assert granted_outcome.decision_event_id != requested_row.event_id
        assert granted_outcome.decision_event_id != rejected_outcome.decision_event_id
    finally:
        audit_engine.dispose()


def test_polled_outcome_carries_no_decision_event_id(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink, approval_settings(poll_interval_s=0.0))

    async def scenario() -> Any:
        rid = await gate.submit(approval_request(ACTION_A, 2))
        await gate.approve(rid, approver="carol")
        polled_outcome = await asyncio.wait_for(gate.await_outcome(rid), timeout=10)
        return polled_outcome

    polled_outcome = asyncio.run(scenario())
    assert polled_outcome.status is ApprovalStatus.APPROVED
    assert polled_outcome.decision_event_id is None


class _FailOnDecisionSink:
    """Fails ``append`` for decision actions; ``approval.requested`` still succeeds."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        if getattr(event, "action", "") in ("approval.granted", "approval.rejected"):
            raise RuntimeError("audit sink is down for decision actions")
        self.events.append(event)

    async def verify(self, *, start: Any = None, end: Any = None) -> Any:
        from ne.contracts.governance import AuditVerification

        return AuditVerification(valid=True, checked=len(self.events))


def test_decision_audit_append_failure_still_fails_closed(approval_store: Any) -> None:
    """C6/C10: this unit must not open a decision-succeeds-without-chain-record path."""
    sink = _FailOnDecisionSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> None:
        rid_grant = await gate.submit(approval_request(ACTION_A, 2))
        rid_reject = await gate.submit(approval_request(ACTION_B, 2))
        # submit still succeeds (approval.requested appended fine above).
        raised_on_approve = False
        try:
            await gate.approve(rid_grant, approver="carol")
        except RuntimeAuditError:
            raised_on_approve = True
        assert raised_on_approve

        raised_on_reject = False
        try:
            await gate.reject(rid_reject, approver="dave", reason="no")
        except RuntimeAuditError:
            raised_on_reject = True
        assert raised_on_reject

        # No ApprovalOutcome was returned on either failed decision path; the
        # rows themselves are left non-terminal at the STORE level only in the
        # sense that no outcome escaped this call — decide() already committed
        # the row+outbox transaction before the (separate) audit append, per
        # the documented "audit AFTER commit, fail closed" contract, so assert
        # the audit-failure contract itself rather than row state.
        assert "approval.granted" not in [getattr(e, "action", "") for e in sink.events]
        assert "approval.rejected" not in [getattr(e, "action", "") for e in sink.events]

    asyncio.run(scenario())


# --- PRE-015A0-ENFORCEMENT: a drifted protected request reaches no decision ---
#
# The decision CAS is re-authorized, so drift after submit cannot produce an
# approval or rejection, an evidence row, or a decision audit record.

_EXPIRES_AT = 1_800_000_000
_BEFORE_EXPIRY = datetime.fromtimestamp(_EXPIRES_AT - 1, tz=UTC)
_ARGUMENTS = {"amount": "250,000.00"}
_APPROVER = "user:alice"


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id="item-a"
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


class DecisionResolver:
    """A ``SourceBoundResultResolver`` whose approval decision can flip mid-test."""

    def __init__(self, decision: SourceAuthorityDecision) -> None:
        self.decision = decision

    async def for_approval(
        self,
        reference: Any,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        authorized = self.decision is SourceAuthorityDecision.AUTHORIZED
        return SourceBoundApprovalResolution(
            decision=self.decision,
            materialized_text=json.dumps(_ARGUMENTS) if authorized else None,
            stamps=(_stamp(),) if authorized else (),
            safe_reason=None if authorized else "safe",
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_model_send(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_direct_result(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_dispatch(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_workflow_projection(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _protected_request() -> Any:
    reference = ProtectedSourceInvocationRef(
        reference_id="invocation-1", stamps=(_stamp(),), invocation_hash="e" * 64
    )
    return approval_request(
        ACTION_A,
        2,
        payload={
            "disclosure": {
                "target_resource": "account:ops-payables",
                "description": "Wire transfer to a named counterparty.",
                PROTECTED_INVOCATION_KEY: encode_protected_reference(reference),
            }
        },
    )


def _context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("group:finance",),
        department="finance",
        issuer="https://oidc.test/realms/ne",
        expires_at=_EXPIRES_AT,
    )


def _withheld_evidence_row(rid: str) -> dict[str, Any]:
    return {
        "request_id": rid,
        "action": ACTION_A,
        "tier": 2,
        "outcome": SOURCE_WITHHELD_STATUS,
        "approver": None,
        "decided_at": _BEFORE_EXPIRY,
        "clean": False,
    }


def test_approver_denial_refuses_the_decision_and_leaves_the_request_pending(
    approval_store: Any,
) -> None:
    """A deciding approver current authority does not admit changes NOTHING durable.

    U2 adversarial review (standalone-gate ruling): the guard evaluates under the
    DECIDING approver's bound context, so a non-authorized outcome is that
    principal's ACL standing, never a proven revocation —
    the earlier revision of this test enshrined the defect by asserting the
    terminal CAS fired. Now: typed refusal, the row untouched (no status change,
    no reason, no decided_at, no approver), no ``approval.source_withheld``
    audit, no evidence row — and once authority admits the approver, the SAME
    still-pending request decides normally.
    """
    resolver = DecisionResolver(SourceAuthorityDecision.PERMANENT_WITHHELD)
    sink = RecordingAuditSink()
    gate = _gate(
        approval_store,
        sink,
        source_guard=ProtectedSourceResultGuard(resolver, clock=lambda: _BEFORE_EXPIRY),
    )

    async def scenario() -> None:
        rid = await gate.submit(_protected_request())
        with (
            authenticated_request_scope(_context()),
            pytest.raises(ApprovalSourceWithheldFromApproverError),
        ):
            await gate.approve(rid, approver=_APPROVER)
        row = await approval_store.get(rid)
        assert row.status == "pending"
        assert row.decided_reason is None
        assert row.decided_at is None
        assert (row.first_approver, row.second_approver) == (None, None)
        assert [e.action for e in sink.events if e.action.startswith("approval.")] == [
            "approval.requested",
        ]
        with approval_store.engine.begin() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).where(ne_approval_evidence.c.request_id == rid)
            ).scalar_one()
        assert count == 0
        # The request stayed actionable: admitted authority decides it normally.
        resolver.decision = SourceAuthorityDecision.AUTHORIZED
        with authenticated_request_scope(_context()):
            outcome = await gate.approve(rid, approver=_APPROVER)
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())


def test_a_source_withheld_request_is_never_revived_by_a_later_authorized_retry(
    approval_store: Any,
) -> None:
    """The genuine terminal, once landed, refuses every entry point — forever.

    The terminal is seeded through the store's own CAS
    (:meth:`mark_source_withheld`) — the seam a genuine owner-context authority
    decision drives — because a reading or deciding approver's denial no longer
    (and must never) author it (U2 adversarial review, standalone-gate ruling).
    """
    resolver = DecisionResolver(SourceAuthorityDecision.AUTHORIZED)
    gate = _gate(
        approval_store,
        RecordingAuditSink(),
        source_guard=ProtectedSourceResultGuard(resolver, clock=lambda: _BEFORE_EXPIRY),
    )

    async def scenario() -> None:
        rid = await gate.submit(_protected_request())
        landed = await approval_store.mark_source_withheld(
            rid,
            now=_BEFORE_EXPIRY,
            reason=SOURCE_WITHHELD_REASON,
            evidence_row=_withheld_evidence_row(rid),
        )
        assert landed is True
        with authenticated_request_scope(_context()):
            # Authority admits the approver — the request STILL cannot be decided.
            for entry in (
                gate.approve(rid, approver=_APPROVER),
                gate.reject(rid, approver=_APPROVER, reason="no"),
                gate.read_payload(rid, approver=_APPROVER),
            ):
                with pytest.raises(ApprovalAlreadyDecidedError):
                    await entry
            with pytest.raises(SourceAuthorityWithheldError):
                await gate.await_outcome(rid)
        row = await approval_store.get(rid)
        assert row.status == SOURCE_WITHHELD_STATUS
        assert row.decided_reason == SOURCE_WITHHELD_REASON
        assert row.first_approver is None
        # Positive control: a re-request under the SAME authorized authority is a
        # FRESH row that decides normally — the terminal state kills that request, not
        # the principal's ability to ask again.
        with authenticated_request_scope(_context()):
            fresh = await gate.submit(_protected_request())
            assert fresh != rid
            outcome = await gate.approve(fresh, approver=_APPROVER)
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())


def test_a_decision_racing_the_drift_cannot_decide_the_request(approval_store: Any) -> None:
    """Concurrent denied approvers decide nothing; the genuine terminal beats decides.

    Two approvers whose OWN authority is denied race: both get the typed
    per-principal refusal, neither obtains an outcome, and the row is UNTOUCHED
    (no terminal, no evidence — a reader/decider denial persists nothing; U2
    adversarial review, standalone-gate ruling). The genuine terminal then lands
    through the store's own CAS exactly once, and a decision arriving after it
    loses its pending-guarded CAS.
    """
    resolver = DecisionResolver(SourceAuthorityDecision.PERMANENT_WITHHELD)
    gate = _gate(
        approval_store,
        RecordingAuditSink(),
        source_guard=ProtectedSourceResultGuard(resolver, clock=lambda: _BEFORE_EXPIRY),
    )

    async def scenario() -> None:
        rid = await gate.submit(_protected_request())
        with authenticated_request_scope(_context()):
            outcomes = await asyncio.gather(
                gate.approve(rid, approver=_APPROVER),
                gate.approve(rid, approver="user:bob"),
                return_exceptions=True,
            )
        # Neither denied approver decided anything or terminalized anything.
        assert not any(isinstance(o, ApprovalOutcome) for o in outcomes)
        assert all(isinstance(o, ApprovalSourceWithheldFromApproverError) for o in outcomes)
        row = await approval_store.get(rid)
        assert row.status == "pending"
        assert row.first_approver is None
        with approval_store.engine.begin() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).where(ne_approval_evidence.c.request_id == rid)
            ).scalar_one()
        assert count == 0
        # The genuine authority terminal (store CAS) lands exactly once...
        assert await approval_store.mark_source_withheld(
            rid,
            now=_BEFORE_EXPIRY,
            reason=SOURCE_WITHHELD_REASON,
            evidence_row=_withheld_evidence_row(rid),
        )
        assert not await approval_store.mark_source_withheld(
            rid,
            now=_BEFORE_EXPIRY,
            reason=SOURCE_WITHHELD_REASON,
            evidence_row=_withheld_evidence_row(rid),
        )
        with approval_store.engine.begin() as conn:
            count = conn.execute(
                sa.select(sa.func.count()).where(ne_approval_evidence.c.request_id == rid)
            ).scalar_one()
        assert count == 1
        # ...and a decision arriving after it loses its own pending-guarded CAS.
        with pytest.raises(ApprovalAlreadyDecidedError):
            await approval_store.decide(
                rid,
                "approved",
                approver=_APPROVER,
                now=_BEFORE_EXPIRY,
                event=new_approval_event(
                    approval_id=rid,
                    tier=2,
                    decision=ApprovalDecision.approved,
                    subject_ref=f"tool:{ACTION_A}",
                    actor=_APPROVER,
                ),
                evidence_row={
                    "request_id": rid,
                    "action": ACTION_A,
                    "tier": 2,
                    "outcome": "approved",
                    "approver": _APPROVER,
                    "decided_at": _BEFORE_EXPIRY,
                    "clean": True,
                },
            )
        assert (await approval_store.get(rid)).status == SOURCE_WITHHELD_STATUS

    asyncio.run(scenario())


def test_a_decision_on_a_protected_request_refuses_without_a_bound_context(
    approval_store: Any,
) -> None:
    resolver = DecisionResolver(SourceAuthorityDecision.AUTHORIZED)
    gate = _gate(
        approval_store,
        RecordingAuditSink(),
        source_guard=ProtectedSourceResultGuard(resolver, clock=lambda: _BEFORE_EXPIRY),
    )

    async def scenario() -> None:
        rid = await gate.submit(_protected_request())
        with pytest.raises(MissingAuthenticatedContextError):
            await gate.approve(rid, approver=_APPROVER)
        assert (await approval_store.get(rid)).status == "pending"
        with authenticated_request_scope(_context()):
            outcome = await gate.approve(rid, approver=_APPROVER)
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())
