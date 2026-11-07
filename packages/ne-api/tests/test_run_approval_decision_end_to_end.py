# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Run-parked approval decisions through the REAL HTTP route.

The governed chain is REAL end to end: real ``RunApprovalCheckpointStore`` over
the app's runtime engine, real ``ProtectedSourceResultGuard`` +
``RunStoreSourceBoundResultResolver`` + ``CurrentSourceDisclosureEvaluator``
over REAL admitted authority receipts — through the HTTP route with a verified
bearer. Honesty about the remaining seams (U2 adversarial review, F4 — an
earlier revision of this docstring claimed "nothing stubbed", which was false):

* the DBOS enqueue transport is a recording seam (``_RecordingEnqueuer``);
* ``_park`` hand-writes the ``run.approval.audit-observed`` safe event and BOTH
  ``record_audit_receipt`` rows (execution + APPROVAL) that the approval
  finalizer requires — in production the run workflow writes those.

The always-409 regression this file pins: ``RunApprovalCheckpointStore.
authorize`` loaded the protected invocation correctly under its PARK-time key
(owner = the RUN owner, binding prefix ``(run_id, attempt_id)``) and then asked
the guard to RE-LOAD the same row under a DECISION-time key — owner = the
approver, prefix ``(approval_id, decision_attempt_hash)``. A decision-time hash
can never match a park-time row, so every approve/deny/reveal of every
run-parked approval failed ``RETRYABLE_UNKNOWN`` → 409 — deterministically —
while the row stayed ``pending`` and the console falsely toasted "Already
decided".

Governance floor deliberately exercised here, not relaxed:

* the invocation carries FOUR protected source refs (stamps) and every stamp is
  re-evaluated at reveal, at decision AND at resume;
* an APPROVER without current authority over even one of the four sources is
  refused (403) and the request STAYS PENDING for another approver — a reader's
  ACL standing never terminalizes the Run or fabricates a revocation record
  (U2 adversarial review, F1);
* only the resume-side re-evaluation under the RUN OWNER's bound context may
  permanently withhold — and its record is attributed to the owner's authority.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import canonical_source_authority_stamps
from ne.events import new_approval_event
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.config import ApprovalSettings
from ne.runtime.run_checkpoint import (
    RunApprovalResumeRequest,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_store import RunAuditDecisionKind, RunState
from ne.runtime.run_workflow import RunWorkflowStateStore, _append_safe_event
from ne.runtime.source_results import authenticated_request_scope
from ne.schema import ApprovalDecision

from .conftest import ISSUER, AppBuilder, admit_current_source, get_state, run

OWNER = "user:ops"
DEPARTMENT = "operations"
APPROVER = "user:carol"
APPROVER_GROUP = "approvers"
AGENT = "agt_projection"

_ARGUMENTS = {"recipient": "cfo@example.test", "subject": "Q3 close"}


def _admit_four_sources(state: Any, *, last_labels: tuple[str, ...] | None = None):
    """Admit four REAL current-authority sources and return their stamps.

    Default labels admit both the approver (via group) and the run owner (via
    principal id). ``last_labels`` overrides the FOURTH source only, so a test
    can revoke exactly one of four authorities.
    """

    labels = (APPROVER_GROUP, OWNER)
    stamps = []
    for index in range(4):
        _receipt, stamp = admit_current_source(
            state.source_authority_reader,
            key=f"run-approval-src-{index}-{time.monotonic_ns()}",
            labels=(last_labels if index == 3 and last_labels is not None else labels),
            content=f"protected evidence {index}".encode(),
        )
        stamps.append(stamp)
    return canonical_source_authority_stamps(stamps)


async def _park(
    state: Any,
    runtime_url: str,
    *,
    stamps,
    tier: int = 2,
    invocation_plaintext: bytes | None = None,
) -> tuple[str, str, str]:
    """Admit, claim and park an owned Run on a REAL Tier-2 Run approval.

    This is the production shape end to end: ``admit_start`` (the atomic
    admission), ``RunWorkflowStateStore.claim`` (the worker's lease — moves the
    Run to RUNNING and writes the durable workflow join), then
    ``create_wait`` (the park), then the execution/approval audit receipts the
    approval finalizer requires.
    """

    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    assert checkpoints is not None, "protected_transport harness must wire the coordinator"
    runs = state.agent_run_store
    conversation = await state.session_store.create_conversation(AGENT, DEPARTMENT, OWNER)
    accepted = await runs.admit_start(
        SimpleNamespace(
            idempotency_key=f"park-{uuid.uuid4().hex}",
            request_hash="1" * 64,
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            conversation_agent_name=AGENT,
            agent_id=AGENT,
            agent_version=3,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="capability-receipt",
            input_plaintext=b"PRIVATE-RUN-INPUT",
            agent_definition_hash="d" * 64,
            model_policy_id="agent.meridian-analyst.v2",
            authorized_model="private-primary",
            authorized_model_lane="private-vllm",
            model_authorization_receipt_hash="e" * 64,
            target_input_budget=16_384,
            requested_model=None,
            requested_model_lane=None,
            authenticated_context=AuthenticatedRequestContext(
                principal_id=OWNER,
                authorization_groups=(),
                department=DEPARTMENT,
                issuer=ISSUER,
                expires_at=int(time.time()) + 3600,
            ),
        )
    )
    run_id, attempt_id = accepted.run_id, accepted.attempt_id
    now = datetime.now(UTC)
    engine = sa.create_engine(runtime_url)
    try:
        lifecycle = RunWorkflowStateStore(engine, run_store=runs)
        claim = lifecycle.claim(
            dispatch_id=f"dispatch-{run_id}",
            run_id=run_id,
            owner_principal_id=OWNER,
            config_hash="1" * 64,
        )
        plaintext = (
            json.dumps(_ARGUMENTS, sort_keys=True, separators=(",", ":")).encode()
            if invocation_plaintext is None
            else invocation_plaintext
        )
        action_hash = hashlib.sha256(plaintext).hexdigest()
        joined = await checkpoints.create_wait(
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=f"step-{run_id}",
            tool_call_id=f"tool-{run_id}",
            action="write.external.email.send",
            action_hash=action_hash,
            tier=tier,
            principal_id=OWNER,
            policy_hash="e" * 64,
            invocation_plaintext=plaintext,
            source_stamps=stamps,
            stamp_receipt_id=f"stamp-{run_id}",
            stamp_receipt_hash="f" * 64,
            requested_at=now,
            sla_deadline=now + timedelta(minutes=5),
            expires_at=now + timedelta(hours=1),
            event=new_approval_event(
                approval_id="allocated-inside-transaction",
                tier=tier,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor=OWNER,
                config_hash="1" * 64,
            ),
        )
        with engine.begin() as conn:
            approval_event_id = _append_safe_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_kind="run.approval.audit-observed",
                event_status="observed",
                safe_detail={"approval_id": joined.approval_id},
                step_id="run-approval",
            )
        await runs.record_audit_receipt(
            run_id=run_id,
            attempt_id=attempt_id,
            run_event_id=claim.claim_event_id,
            decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
            audit_event_id=f"audit-execution-{run_id}",
            audit_sequence=1,
            audit_record_hash=hashlib.sha256(f"audit-execution-{run_id}".encode()).hexdigest(),
            config_hash="1" * 64,
            owner_principal_id=OWNER,
            step_id="run-execution",
        )
        await runs.record_audit_receipt(
            run_id=run_id,
            attempt_id=attempt_id,
            run_event_id=approval_event_id,
            decision_kind=RunAuditDecisionKind.APPROVAL,
            audit_event_id=f"audit-approval-{run_id}",
            audit_sequence=2,
            audit_record_hash=hashlib.sha256(f"audit-approval-{run_id}".encode()).hexdigest(),
            config_hash="1" * 64,
            owner_principal_id=OWNER,
            step_id="run-approval",
            approval_id=joined.approval_id,
        )
    finally:
        engine.dispose()
    return joined.approval_id, run_id, attempt_id


class _RecordingEnqueuer:
    def __init__(self) -> None:
        self.requests: list[RunApprovalResumeRequest] = []

    async def enqueue_approval_resume(self, request: RunApprovalResumeRequest) -> None:
        self.requests.append(request)


def _owner_context() -> AuthenticatedRequestContext:
    """The run owner's snapshot, as the resume worker binds it."""

    return AuthenticatedRequestContext(
        principal_id=OWNER,
        authorization_groups=(),
        issuer=ISSUER,
        expires_at=int(time.time()) + 3600,
    )


def _statuses(runtime_url: str, approval_id: str, run_id: str) -> tuple[str, str, str]:
    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            approval = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).scalar_one()
            checkpoint = conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                    ne_agent_run_continuation_checkpoints.c.run_id == run_id
                )
            ).scalar_one()
            from ne.runtime.run_store import ne_agent_runs

            run_status = conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == run_id)
            ).scalar_one()
        return str(approval), str(checkpoint), str(run_status)
    finally:
        engine.dispose()


def test_parked_run_approval_reveal_approve_and_resume_all_succeed_unstubbed(
    make_app: AppBuilder,
) -> None:
    """Reveal discloses, approve lands, and the resume-side reauthorization —
    over all FOUR protected refs — materializes the invocation and the Run
    leaves ``waiting_approval``. The exact rehearsal failure was 409
    '"Run approval authority is retryable and remains pending"' on every one of
    these calls."""

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])

            # Reveal: the approver must be able to SEE what they approve.
            payload = await client.get(f"/api/v1/approvals/{approval_id}/payload", headers=headers)
            assert payload.status_code == 200, payload.text
            body = payload.json()
            assert body["material_parameters"] == _ARGUMENTS
            assert body["run_context"]["run_id"] == run_id

            # Approve: the live demo moment.
            decided = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert decided.status_code == 200, decided.text
            assert decided.json()["status"] == "approved"
            assert decided.json()["approver"] == APPROVER

    run(_t())

    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("approved", "approved")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert [request.approval_id for request in enqueuer.requests] == [approval_id]
    assert enqueuer.requests[0].run_id == run_id
    assert enqueuer.requests[0].decision == "approved"

    async def _resume() -> None:
        # The worker's resume-side calls, minus only DBOS transport: the SAME
        # authorize (operation="resume") re-evaluates all four stamps under the
        # RUN OWNER's context, then the claim moves the Run back to RUNNING.
        with authenticated_request_scope(_owner_context()):
            arguments = await checkpoints.authorize(
                approval_id,
                guard=state.source_guard,
                operation="resume",
                decided_at=datetime.now(UTC),
            )
        assert arguments == _ARGUMENTS
        joined = await checkpoints.get_join(approval_id)
        await checkpoints.resume(
            approval_id,
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            resumed_at=datetime.now(UTC),
        )

    run(_resume())
    _approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert checkpoint == "resumed"
    assert run_status == RunState.RUNNING.value


def test_parked_run_approval_deny_lands_through_the_real_chain(make_app: AppBuilder) -> None:
    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            denied = await client.post(
                f"/api/v1/approvals/{approval_id}/reject",
                json={"reason": "not during close week"},
                headers=headers,
            )
            assert denied.status_code == 200, denied.text
            assert denied.json()["status"] == "rejected"

    run(_t())
    approval, checkpoint, _run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("rejected", "rejected")
    assert [request.decision for request in enqueuer.requests] == ["rejected"]


def _decided_reason(runtime_url: str, approval_id: str) -> str | None:
    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            reason = conn.execute(
                sa.select(ne_approval_requests.c.decided_reason).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).scalar_one()
        return None if reason is None else str(reason)
    finally:
        engine.dispose()


def test_approver_without_one_source_is_refused_and_the_run_survives(
    make_app: AppBuilder,
) -> None:
    """A reader's ACL standing is never a revocation (U2 adversarial review, F1).

    Carol holds the approver role but the FOURTH source's ACL admits only the
    run owner and the ``senior-approvers`` group. Before this fix, Carol merely
    OPENING the approval (a GET) — or trying to decide it — terminalized the
    request ``source_withheld``, failed the Run, and wrote a ledger record
    asserting "current source authority permanently denied" when nothing was
    revoked. Now: Carol gets 403 on read, approve AND reject; everything stays
    pending/parked; and Dave, who holds authority over all four sources, still
    approves the untouched request.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state, last_labels=(OWNER, "senior-approvers"))
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))

    async def _refused() -> None:
        async with h.client() as client:
            carol = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            revealed = await client.get(f"/api/v1/approvals/{approval_id}/payload", headers=carol)
            assert revealed.status_code == 403, revealed.text
            assert "material_parameters" not in revealed.json()
            approved = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=carol
            )
            assert approved.status_code == 403, approved.text
            rejected = await client.post(
                f"/api/v1/approvals/{approval_id}/reject",
                json={"reason": "cannot even see it"},
                headers=carol,
            )
            assert rejected.status_code == 403, rejected.text

    run(_refused())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("pending", "pending")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert _decided_reason(h.runtime_url, approval_id) is None
    assert enqueuer.requests == []

    async def _another_approver_still_can() -> None:
        async with h.client() as client:
            dave = h.bearer("user:dave", groups=[APPROVER_GROUP, "senior-approvers"])
            decided = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=dave
            )
            assert decided.status_code == 200, decided.text
            assert decided.json()["status"] == "approved"

    run(_another_approver_still_can())
    approval, checkpoint, _run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("approved", "approved")
    assert [request.approval_id for request in enqueuer.requests] == [approval_id]


def test_owner_revocation_withholds_at_resume_with_owner_attribution(
    make_app: AppBuilder,
) -> None:
    """The stamp floor is NOT weakened (F1/F5): the genuine authority decision.

    The fourth source's ACL admits the approver group but NOT the run owner —
    the owner's authority over that source is gone. The approver may still
    decide (their own authority is intact), but the resume-side re-evaluation
    under the RUN OWNER's bound context permanently withholds: the request
    terminalizes ``source_withheld``, the Run fails, nothing executes — and the
    record is attributed to the OWNER's authority instead of asserting an
    unattributed revocation.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state, last_labels=(APPROVER_GROUP,))
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]

    async def _approve() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            decided = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert decided.status_code == 200, decided.text
            assert decided.json()["status"] == "approved"

    run(_approve())

    async def _resume() -> None:
        with authenticated_request_scope(_owner_context()):
            arguments = await checkpoints.authorize(
                approval_id,
                guard=state.source_guard,
                operation="resume",
                decided_at=datetime.now(UTC),
            )
        assert arguments is None

    run(_resume())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("source_withheld", "source_withheld")
    assert run_status == RunState.FAILED.value
    assert _decided_reason(h.runtime_url, approval_id) == (
        "run owner's current source authority permanently denied at resume"
    )


def test_decision_refuses_when_tier_drifts_from_the_parked_join(
    make_app: AppBuilder,
) -> None:
    """Decision-time governance metadata is bound to the park (U2 review, F2).

    ``row.tier`` selects which approval policy applies, and before this fix it
    was re-loaded at decide time from the mutable ``ne_approval_requests`` row
    with nothing binding it to the parked join. Here the row's tier is tampered
    from 2 to 1 after the park; the decision must refuse (409) instead of
    proceeding under the drifted (weaker) tier.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))
    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == approval_id)
                .values(tier=1)
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            decided = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert decided.status_code == 409, decided.text
            assert decided.json()["detail"] == "approval context unavailable"

    run(_t())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("pending", "pending")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert enqueuer.requests == []


def test_run_bound_tier3_dual_approval_through_the_real_chain(
    make_app: AppBuilder,
) -> None:
    """Tier-3 over the REAL chain (U2 adversarial review, F3).

    The former ``test_approvals_api.py`` Tier-3 run-bound test stubbed the
    checkpoint store, the source guard and the approval row, so the entire
    run-bound Tier-3 branch was proven only against a fake. This drives it end
    to end: real park at tier 3, real named-approver refusals, real
    ``record_first_approval`` CAS, real distinct-second ``decide``, one enqueue
    only after the second named check.
    """

    h = make_app(
        approver_roles=(APPROVER_GROUP,),
        approvals=ApprovalSettings(
            tier3_named_approvers=("user:alice", "user:bob"), poll_interval_s=0.05
        ),
        protected_transport=True,
    )
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps, tier=3))

    async def _t() -> None:
        async with h.client() as client:
            # A group-authorized but NON-listed approver: refused on read AND
            # decide, with the Tier-3 policy naming served, and nothing lands.
            mallory = h.bearer("user:mallory", groups=[APPROVER_GROUP])
            revealed = await client.get(f"/api/v1/approvals/{approval_id}/payload", headers=mallory)
            assert revealed.status_code == 403, revealed.text
            refused = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=mallory
            )
            assert refused.status_code == 403, refused.text
            assert "Tier-3 named-approver list" in refused.json()["detail"]

            # First listed approver: reveal works, first check lands, no resume.
            alice = h.bearer("user:alice", groups=[APPROVER_GROUP])
            payload = await client.get(f"/api/v1/approvals/{approval_id}/payload", headers=alice)
            assert payload.status_code == 200, payload.text
            assert payload.json()["material_parameters"] == _ARGUMENTS
            first = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=alice
            )
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "pending_second"
            assert enqueuer.requests == []

            # The same named approver again: distinct-second policy, 403.
            repeat = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=alice
            )
            assert repeat.status_code == 403, repeat.text

            # A distinct second listed approver: approved, approver = the FIRST.
            bob = h.bearer("user:bob", groups=[APPROVER_GROUP])
            second = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=bob
            )
            assert second.status_code == 200, second.text
            assert second.json()["status"] == "approved"
            assert second.json()["approver"] == "user:alice"

    run(_t())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("approved", "approved")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert [request.approval_id for request in enqueuer.requests] == [approval_id]
    assert enqueuer.requests[0].decision == "approved"


@pytest.mark.parametrize(
    "bad_plaintext",
    [
        pytest.param(b"not-json", id="invalid-json"),
        pytest.param(b"[1, 2, 3]", id="json-list"),
        pytest.param(b'"scalar"', id="json-scalar"),
    ],
)
def test_malformed_parked_invocation_cannot_be_decided(
    make_app: AppBuilder, bad_plaintext: bytes
) -> None:
    """A decision may never persist against an unresumable invocation (Sol P1).

    ``create_wait`` accepts arbitrary bytes and the resolver authorizes any
    valid UTF-8, so the shape floor lives in the decision preflight: the old
    materializing ``authorize`` rejected non-JSON and non-mapping invocations,
    and ``check_decision_authority`` must keep that floor without returning the
    plaintext. approve AND reject refuse retryably (503 — the invocation row is
    the problem, not the decision), nothing is enqueued, everything stays
    parked/pending, and the reveal refuses too.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(
        _park(state, h.runtime_url, stamps=stamps, invocation_plaintext=bad_plaintext)
    )

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            revealed = await client.get(f"/api/v1/approvals/{approval_id}/payload", headers=headers)
            assert revealed.status_code == 409, revealed.text
            approved = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert approved.status_code == 503, approved.text
            rejected = await client.post(
                f"/api/v1/approvals/{approval_id}/reject",
                json={"reason": "no"},
                headers=headers,
            )
            assert rejected.status_code == 503, rejected.text

    run(_t())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("pending", "pending")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert enqueuer.requests == []


def test_legacy_join_without_provable_governance_fails_closed_after_upgrade(
    make_app: AppBuilder,
) -> None:
    """The schema upgrade must never bless tampered metadata as park-time truth.

    Sol cross-model review (P1): an earlier upgrade backfilled the join's
    tier/action from the MUTABLE ``ne_approval_requests`` row, so a legacy
    Tier-2 join whose request row had been tampered to Tier 1 came out of the
    migration with BOTH rows agreeing on the tampered value — the F2 downgrade
    guard accepted it. Reproduced here: park, drop the governance columns (the
    legacy schema), tamper the request row, re-run ``ensure_run_schema``. The
    legacy join must stay UNTRUSTED: read, approve and reject all refuse (409),
    nothing is decided or enqueued, and the Run stays parked for the owner to
    intervene.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))

    from ne.runtime.run_store import ensure_run_schema

    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.begin() as conn:
            # The legacy shape: the join predates the governance columns.
            conn.exec_driver_sql("ALTER TABLE ne_agent_run_approval_joins DROP COLUMN tier")
            conn.exec_driver_sql("ALTER TABLE ne_agent_run_approval_joins DROP COLUMN action")
            # The tamper the upgrade must not launder: Tier-3-grade metadata
            # downgraded on the mutable row before the migration runs.
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == approval_id)
                .values(tier=1, action="write.internal.note.append")
            )
        ensure_run_schema(engine)
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            revealed = await client.get(f"/api/v1/approvals/{approval_id}/payload", headers=headers)
            assert revealed.status_code == 409, revealed.text
            approved = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert approved.status_code == 409, approved.text
            assert approved.json()["detail"] == "approval context unavailable"
            rejected = await client.post(
                f"/api/v1/approvals/{approval_id}/reject",
                json={"reason": "no"},
                headers=headers,
            )
            assert rejected.status_code == 409, rejected.text

    run(_t())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("pending", "pending")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert enqueuer.requests == []


def test_decision_refuses_when_action_drifts_from_the_parked_join(
    make_app: AppBuilder,
) -> None:
    """Action drift is refused on BOTH decide paths (Sol P1 coverage gap).

    The tier-drift test only exercised approve; this tampers ``action`` on the
    mutable request row after the park and proves approve AND reject both
    refuse with 409, leaving everything pending.
    """

    h = make_app(approver_roles=(APPROVER_GROUP,), protected_transport=True)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    approval_id, run_id, _attempt_id = run(_park(state, h.runtime_url, stamps=stamps))
    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == approval_id)
                .values(action="write.internal.note.append")
            )
    finally:
        engine.dispose()

    async def _t() -> None:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            approved = await client.post(
                f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
            )
            assert approved.status_code == 409, approved.text
            assert approved.json()["detail"] == "approval context unavailable"
            rejected = await client.post(
                f"/api/v1/approvals/{approval_id}/reject",
                json={"reason": "no"},
                headers=headers,
            )
            assert rejected.status_code == 409, rejected.text

    run(_t())
    approval, checkpoint, run_status = _statuses(h.runtime_url, approval_id, run_id)
    assert (approval, checkpoint) == ("pending", "pending")
    assert run_status == RunState.WAITING_APPROVAL.value
    assert enqueuer.requests == []
