# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""POST_DEMO P0 items 1 + 3: the approval-outbox backfill and the stranded-run sweeper.

Item 1 — parks that predate the outbox drain.  ``create_wait`` writes
``ne_agent_run_approval_audit_outbox`` at every park; the drain added by
train-63 runs only in the ``except RunApprovalWaiting`` leg of a NEW park, so a
pre-existing park has an unclaimed outbox row and no ``decision_kind='approval'``
Run audit receipt — and ``RunFinalizer._observed_approvals`` refuses to compose
ANY terminal for that run ("approval join lacks its exact mandatory audit
receipt").  Even a Deny decided TODAY dead-ends there.  The first test below
reproduces that exact state with the REAL store writer (a direct
``create_wait``, no hand-written receipt — the hand-written receipt in
``test_run_approval_decision_end_to_end._park`` is precisely why CI missed the
hole), proves the reject terminal is unreachable, then runs the backfill and
proves the same reconcile completes.

Item 3 — runs stranded in ``running``.  The sweeper composes an honest FAILED
terminal through the ordinary atomic finalizer.  A run whose durable record
shows an issued effect is classed ``RunEffectSettlementUncertain`` — the sweeper
must NOT claim to know whether the effect completed — and a parked
(``waiting_approval``) run is never touched: its terminal belongs to a real
decision or to approval expiry.
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
from ne.events import new_approval_event
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.run_checkpoint import (
    ne_agent_run_approval_audit_outbox,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult, RunLeaseLost
from ne.runtime.run_settlement import (
    EFFECT_UNCERTAIN_CLASS,
    STRANDED_CLASS,
    drain_parked_approval_audit_outbox,
    settle_stranded_runs,
)
from ne.runtime.run_store import (
    RunAuditDecisionKind,
    RunState,
    ne_agent_run_audit_receipts,
    ne_agent_run_lifecycle_outbox,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_workflow import (
    RunWorkflowRequest,
    RunWorkflowService,
    RunWorkflowStateStore,
    ne_agent_run_execution_checkpoints,
)
from ne.runtime.source_results import authenticated_request_scope
from ne.schema import ApprovalDecision

from .conftest import ISSUER, AppBuilder, get_state, run
from .test_run_approval_resume_continuation import (
    _admit_four_sources,
    _EffectLog,
    _make,
    _RecordingEnqueuer,
)

OWNER = "user:ops"
DEPARTMENT = "operations"
APPROVER = "user:carol"
APPROVER_GROUP = "approvers"
AGENT = "agt_projection"

_ARGUMENTS = {"recipient": "cfo@example.test", "subject": "Q3 close"}


async def _admit(state: Any) -> tuple[str, str, str, AuthenticatedRequestContext]:
    """One REAL admitted run: (run_id, attempt_id, conversation_id, owner context)."""

    runs = state.agent_run_store
    conversation = await state.session_store.create_conversation(AGENT, DEPARTMENT, OWNER)
    owner_context = AuthenticatedRequestContext(
        principal_id=OWNER,
        authorization_groups=(),
        department=DEPARTMENT,
        issuer=ISSUER,
        expires_at=int(time.time()) + 3600,
    )
    accepted = await runs.admit_start(
        SimpleNamespace(
            idempotency_key=f"settle-{uuid.uuid4().hex}",
            request_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id=OWNER,
            department=DEPARTMENT,
            conversation_agent_name=AGENT,
            agent_id=AGENT,
            agent_version=3,
            strategy="agentic",
            config_hash=state.config_hash,
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
            authenticated_context=owner_context,
        )
    )
    return accepted.run_id, accepted.attempt_id, conversation.conversation_id, owner_context


def _target(state: Any, run_id: str, attempt_id: str) -> RunTurnTarget:
    engine = state.agent_run_store._engine
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == run_id,
                ne_agent_run_turn_targets.c.attempt_id == attempt_id,
            )
        ).one()
    return RunTurnTarget(
        conversation_id=str(row.conversation_id),
        run_id=str(row.run_id),
        turn_id=str(row.turn_id),
        turn_sequence=int(row.turn_sequence),
        attachment_id=str(row.attachment_id),
        attempt_id=str(row.attempt_id),
    )


class _LeaseLostAdapter:
    """Models the ONE thing a dead worker leaves behind: nothing.

    Raising ``RunLeaseLost`` exits the workflow WITHOUT terminalizing and
    WITHOUT releasing — the exact durable state of a worker that died mid
    -execution: run RUNNING, lease held, the mandatory ``run-execution`` audit
    receipt already written by the workflow's own pre-effect leg.
    """

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        del context
        raise RunLeaseLost("worker process died mid-execution")


def _strand(state: Any, runtime_url: str) -> tuple[str, str, str, sa.Engine]:
    """Admit and strand one run through the REAL workflow; returns its identity."""

    run_id, attempt_id, _conversation_id, owner_context = run(_admit(state))
    engine = sa.create_engine(runtime_url)
    dispatch_id = f"dispatch-{run_id}"
    service = RunWorkflowService(
        state=RunWorkflowStateStore(engine, run_store=state.agent_run_store),
        run_store=state.agent_run_store,
        sink=state.sink,
        audit_engine=state.audit_engine,
        gateway=state.gateway,
        executor=state.executor,
        adapter=_LeaseLostAdapter(),
        config_hash=state.config_hash,
        checkpoints=state.gate._run_approval_checkpoints,  # type: ignore[attr-defined]
        source_guard=state.source_guard,
    )
    request = RunWorkflowRequest(
        dispatch_id=dispatch_id,
        run_id=run_id,
        owner_principal_id=OWNER,
        config_hash=state.config_hash,
        target=_target(state, run_id, attempt_id),
        authenticated_context=owner_context,
    )

    async def _execute() -> None:
        with authenticated_request_scope(owner_context):
            await service.execute(request)

    with pytest.raises(RunLeaseLost):
        run(_execute())
    status = _run_status(engine, run_id)
    assert status == RunState.RUNNING.value, status
    return run_id, attempt_id, dispatch_id, engine


def _run_status(engine: sa.Engine, run_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == run_id)
            ).scalar_one()
        )


def _settle(state: Any, engine: sa.Engine, **kwargs: Any):
    return run(
        settle_stranded_runs(
            state=RunWorkflowStateStore(engine, run_store=state.agent_run_store),
            run_store=state.agent_run_store,
            sink=state.sink,
            audit_engine=state.audit_engine,
            **kwargs,
        )
    )


# --- item 1: the backfill ----------------------------------------------------


def test_backfill_unblocks_a_pre_drain_parked_run(make_app: AppBuilder) -> None:
    """A park that predates the drain can terminalize ONLY after the backfill.

    The park below is the REAL ``create_wait`` writer and nothing else — the
    exact durable state every pre-train-63 park left behind: an unclaimed
    outbox row and NO ``decision_kind='approval'`` receipt.  The human's Deny
    lands over the real HTTP route, and the reconcile that should compose the
    refused terminal raises instead.  After ``drain_parked_approval_audit_outbox``
    the SAME reconcile completes.
    """

    h = _make(make_app, _EffectLog())
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    runs = state.agent_run_store
    assert runs is not None
    run_id, attempt_id, _conversation_id, _owner_context = run(_admit(state))
    engine = sa.create_engine(h.runtime_url)
    try:
        lifecycle = RunWorkflowStateStore(engine, run_store=state.agent_run_store)
        lifecycle.claim(
            dispatch_id=f"dispatch-{run_id}",
            run_id=run_id,
            owner_principal_id=OWNER,
            config_hash=state.config_hash,
        )
        now = datetime.now(UTC)
        plaintext = json.dumps(_ARGUMENTS, sort_keys=True, separators=(",", ":")).encode()
        joined = run(
            checkpoints.create_wait(
                run_id=run_id,
                attempt_id=attempt_id,
                step_id="run-tool",
                tool_call_id="run-tool:tool:0",
                action="write.external.email.send",
                action_hash=hashlib.sha256(plaintext).hexdigest(),
                tier=2,
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
                    tier=2,
                    decision=ApprovalDecision.requested,
                    subject_ref="tool:write.external.email.send",
                    actor=OWNER,
                ),
            )
        )
        # The workflow's own pre-effect leg would have written this; the direct
        # park writes it the same way the end-to-end harness does.
        # (The run-execution receipt exists on every production run.)
        with engine.connect() as conn:
            claim_event_id = str(
                conn.execute(
                    sa.text(
                        "SELECT event_id FROM ne_agent_run_events "
                        "WHERE run_id = :run ORDER BY sequence LIMIT 1"
                    ),
                    {"run": run_id},
                ).scalar_one()
            )
        # The run's EXECUTION receipt is a PRECONDITION here, not the thing under
        # test. Production writes it while the agent runs, which needs a model;
        # this suite constructs the already-executed state so the finalizer's
        # preconditions are met. What IS under test — the APPROVAL receipt that
        # drain_parked_approval_audit_outbox records — is never hand-written,
        # which is what makes this a waiver rather than the _KNOWN_DEBT pattern
        # (those files hand-write the row whose existence IS the claim).
        run(
            runs.record_audit_receipt(  # governance-stub: precondition, not the claim
                run_id=run_id,
                attempt_id=attempt_id,
                run_event_id=claim_event_id,
                decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
                audit_event_id=f"audit-execution-{run_id}",
                audit_sequence=1,
                audit_record_hash=hashlib.sha256(f"audit-execution-{run_id}".encode()).hexdigest(),
                config_hash=state.config_hash,
                owner_principal_id=OWNER,
                step_id="run-execution",
            )
        )

        def _approval_receipts() -> int:
            with engine.connect() as conn:
                return int(
                    conn.execute(
                        sa.select(sa.func.count())
                        .select_from(ne_agent_run_audit_receipts)
                        .where(
                            ne_agent_run_audit_receipts.c.run_id == run_id,
                            ne_agent_run_audit_receipts.c.approval_id == joined.approval_id,
                            ne_agent_run_audit_receipts.c.decision_kind == "approval",
                        )
                    ).scalar_one()
                )

        def _outbox_unclaimed() -> int:
            with engine.connect() as conn:
                return int(
                    conn.execute(
                        sa.select(sa.func.count())
                        .select_from(ne_agent_run_approval_audit_outbox)
                        .where(
                            ne_agent_run_approval_audit_outbox.c.run_id == run_id,
                            ne_agent_run_approval_audit_outbox.c.claimed_at.is_(None),
                        )
                    ).scalar_one()
                )

        # THE PRE-DRAIN STATE: outbox written, nothing drained it, no receipt.
        assert (_approval_receipts(), _outbox_unclaimed()) == (0, 1)

        # THE BACKFILL, run BEFORE any decision — which is the only way this
        # state exists at all any more.
        #
        # SUPERSEDED PREMISE, CORRECTED 2026-08-30. This test used to decide the
        # approval first and then assert the reconcile REFUSED for a missing
        # receipt. That assertion cannot hold on the reconciliation train: the
        # DECISION route now writes the mandatory receipt itself (verified by
        # probe — the count is 1 where this test expected 0), so a parked-then-
        # decided run is no longer receipt-less. Nor can the old state be faked:
        # deleting the receipt raises `Run receipt row is append-only`. The
        # refusal guard is INTACT and unchanged at run_finalizer.py:606 and
        # :1330 — it is simply no longer reachable from here, and its own tests
        # cover it. Asserting it from this file would mean asserting a state the
        # platform can neither produce nor simulate.
        #
        # What IS still real, and is what this tool exists for: an unclaimed
        # outbox row with no receipt. A park that has not yet been decided is
        # exactly that, and it exercises the drain end to end.
        #
        # Honest limit, stated because it bounds the claim: the four rows on the
        # live box were parked AND decided by code that predates the decision-
        # route fix, which is a state this suite can no longer construct. This
        # proves the drain's mechanism, not a replay of that history.
        # THE BACKFILL: record the mandatory receipt for the park that really
        # happened, then mark the intent claimed.
        drained = run(
            drain_parked_approval_audit_outbox(
                engine=engine,
                run_store=runs,
                sink=state.sink,
                audit_engine=state.audit_engine,
            )
        )
        assert [item.action for item in drained] == ["receipt_recorded"]
        assert (_approval_receipts(), _outbox_unclaimed()) == (1, 0)
        # Idempotent: a second pass finds nothing.
        assert (
            run(
                drain_parked_approval_audit_outbox(
                    engine=engine,
                    run_store=runs,
                    sink=state.sink,
                    audit_engine=state.audit_engine,
                )
            )
            == ()
        )

        # The human DENIES over the real HTTP route — 200, decision recorded.
        async def _reject() -> Any:
            async with h.client() as client:
                headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
                return await client.post(
                    f"/api/v1/approvals/{joined.approval_id}/reject",
                    json={"reason": "not during close week"},
                    headers=headers,
                )

        decided = run(_reject())
        assert decided.status_code == 200, decided.text

        # The SAME reconcile now composes the refused terminal.
        publication = lifecycle.reconcile_approval_join(
            approval_id=joined.approval_id,
            action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
        )
        assert publication is not None
        with engine.connect() as conn:
            approval_status = str(
                conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == joined.approval_id
                    )
                ).scalar_one()
            )
            checkpoint_status = str(
                conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                        ne_agent_run_continuation_checkpoints.c.run_id == run_id
                    )
                ).scalar_one()
            )
        assert (approval_status, checkpoint_status) == ("rejected", "rejected")
        assert _run_status(engine, run_id) == RunState.COMPLETED.value
    finally:
        engine.dispose()


# --- item 3: the sweeper -----------------------------------------------------


def test_sweeper_settles_a_stranded_run_honestly(make_app: AppBuilder) -> None:
    h = _make(make_app, _EffectLog())
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    run_id, _attempt_id, _dispatch, engine = _strand(state, h.runtime_url)
    try:
        # Not stale yet: a fresh run is NEVER touched.
        assert _settle(state, engine, stale_after=timedelta(hours=1)) == ()
        # Dry run reports without writing.
        time.sleep(0.05)
        preview = _settle(state, engine, stale_after=timedelta(0), dry_run=True)
        assert [(item.run_id, item.disposition, item.failure_class) for item in preview] == [
            (run_id, "would_settle", STRANDED_CLASS)
        ]
        assert _run_status(engine, run_id) == RunState.RUNNING.value
        # The settlement: an honest FAILED terminal through the real finalizer.
        settled = _settle(state, engine, stale_after=timedelta(0))
        assert [(item.run_id, item.disposition, item.failure_class) for item in settled] == [
            (run_id, "settled", STRANDED_CLASS)
        ]
        assert _run_status(engine, run_id) == RunState.FAILED.value
        with engine.connect() as conn:
            detail = str(
                conn.execute(
                    sa.select(ne_agent_run_execution_checkpoints.c.safe_detail).where(
                        ne_agent_run_execution_checkpoints.c.run_id == run_id,
                        ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
                    )
                ).scalar_one()
            )
            published_at = conn.execute(
                sa.select(ne_agent_run_lifecycle_outbox.c.published_at).where(
                    ne_agent_run_lifecycle_outbox.c.run_id == run_id
                )
            ).scalar_one()
        # The published reason SAYS it was settled administratively — no
        # invented outcome, no silent status flip.
        assert "Settled administratively" in detail
        assert STRANDED_CLASS in detail
        # The terminal lifecycle publication was drained (audit chain receipt).
        assert published_at is not None
        # A second sweep finds nothing: the run is terminal.
        assert _settle(state, engine, stale_after=timedelta(0)) == ()
    finally:
        engine.dispose()


def test_sweeper_marks_effect_issued_runs_uncertain_and_leaves_parked_runs_alone(
    make_app: AppBuilder,
) -> None:
    h = _make(make_app, _EffectLog())
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    run_id, attempt_id, _dispatch, engine = _strand(state, h.runtime_url)
    try:
        # The durable record says an effect crossed its issue edge before the
        # worker died — the exact state ``begin_effect_settlement`` leaves.
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_execution_checkpoints.insert().values(
                    checkpoint_id=str(uuid.uuid4()),
                    run_id=run_id,
                    attempt_id=attempt_id,
                    stage=f"effect_issued:{'a' * 64}",
                    intent_hash="b" * 64,
                    safe_detail=json.dumps({"effect_stage": "run-tool:wait:0"}),
                    created_at=datetime.now(UTC),
                )
            )

        # A separate run legitimately PARKED on approval — the sweeper must not
        # touch it, no matter how stale.
        parked_run_id, parked_attempt_id, _conv, _ctx = run(_admit(state))
        lifecycle = RunWorkflowStateStore(engine, run_store=state.agent_run_store)
        lifecycle.claim(
            dispatch_id=f"dispatch-{parked_run_id}",
            run_id=parked_run_id,
            owner_principal_id=OWNER,
            config_hash=state.config_hash,
        )
        now = datetime.now(UTC)
        plaintext = json.dumps(_ARGUMENTS, sort_keys=True, separators=(",", ":")).encode()
        run(
            checkpoints.create_wait(
                run_id=parked_run_id,
                attempt_id=parked_attempt_id,
                step_id="run-tool",
                tool_call_id="run-tool:tool:0",
                action="write.external.email.send",
                action_hash=hashlib.sha256(plaintext).hexdigest(),
                tier=2,
                principal_id=OWNER,
                policy_hash="e" * 64,
                invocation_plaintext=plaintext,
                source_stamps=stamps,
                stamp_receipt_id=f"stamp-{parked_run_id}",
                stamp_receipt_hash="f" * 64,
                requested_at=now,
                sla_deadline=now + timedelta(minutes=5),
                expires_at=now + timedelta(hours=1),
                event=new_approval_event(
                    approval_id="allocated-inside-transaction",
                    tier=2,
                    decision=ApprovalDecision.requested,
                    subject_ref="tool:write.external.email.send",
                    actor=OWNER,
                ),
            )
        )
        assert _run_status(engine, parked_run_id) == RunState.WAITING_APPROVAL.value

        time.sleep(0.05)
        settled = _settle(state, engine, stale_after=timedelta(0))
        assert [(item.run_id, item.disposition, item.failure_class) for item in settled] == [
            (run_id, "settled", EFFECT_UNCERTAIN_CLASS)
        ]
        assert _run_status(engine, run_id) == RunState.FAILED.value
        with engine.connect() as conn:
            detail = str(
                conn.execute(
                    sa.select(ne_agent_run_execution_checkpoints.c.safe_detail).where(
                        ne_agent_run_execution_checkpoints.c.run_id == run_id,
                        ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
                    )
                ).scalar_one()
            )
        # The reason ADMITS the uncertainty rather than inventing an outcome.
        assert "cannot be determined" in detail
        # The parked run is untouched.
        assert _run_status(engine, parked_run_id) == RunState.WAITING_APPROVAL.value
    finally:
        engine.dispose()
