# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE park leg for approval tests — the real workflow, never fabricated rows.

Every test that needs a run-parked approval must obtain it from here. The rule
this module exists to enforce:

    A governance chokepoint is never stubbed, and the rows a chokepoint is
    supposed to WRITE are never hand-written by test setup.

Both halves cost us a production outage on 2026-08-28.

* ``test_approvals_api.py`` stubbed ``state.gate._run_approval_checkpoints`` and
  set ``state.source_guard`` to a bare ``SimpleNamespace``, so an ``authorize``
  that returned ``RETRYABLE_UNKNOWN`` for **every** run-parked approval shipped
  with green CI.
* ``test_run_approval_decision_end_to_end.py::_park`` called
  ``RunStore.record_audit_receipt`` directly to fabricate the
  ``decision_kind='approval'`` Run audit receipt. Production never wrote that
  receipt — ``create_wait`` only enqueues an intent into
  ``ne_agent_run_approval_audit_outbox`` and, before train-63, nothing drained
  it. ``RunFinalizer._observed_approvals`` then refused to compose ANY terminal
  for a run that had ever parked, so no parked run could terminalize on any
  outcome — not even Deny. The fabricated row made that invisible.

So: this helper drives the REAL :meth:`RunWorkflowService.execute`. It claims
the lease, writes the ``run-execution`` receipt, runs the workflow's own park
leg, and therefore drains (or, on a broken build, fails to drain) the approval
audit outbox exactly as the worker does. The ONLY double is the adapter, which
stands in for the agent loop's one relevant act: park the wait on the exact
server-minted identity and raise the durable ``RunApprovalWaiting``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.types import ApprovalTier
from ne.events import new_approval_event
from ne.runtime.approvals import canonical_args_hash
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_workflow import (
    RunWorkflowRequest,
    RunWorkflowService,
    RunWorkflowStateStore,
)
from ne.runtime.source_results import authenticated_request_scope

from .conftest import ISSUER, admit_current_source

OWNER = "user:ops"
DEPARTMENT = "operations"
APPROVER = "user:carol"
APPROVER_GROUP = "approvers"
AGENT = "agt_projection"


class RecordingEnqueuer:
    """The ONE seam past the governance chain: the DBOS enqueue transport."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def enqueue_approval_resume(self, request: Any) -> None:
        self.requests.append(request)


@dataclass
class Parked:
    """Everything a park leaves behind that a decision or resume needs."""

    run_id: str
    attempt_id: str
    dispatch_id: str
    conversation_id: str
    config_hash: str
    owner_context: AuthenticatedRequestContext
    action: str
    arguments: Mapping[str, object]
    approval_id: str = ""
    turn_id: str = ""
    turn_sequence: int = 0
    attachment_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def admit_sources(
    state: Any,
    *,
    key_prefix: str,
    count: int = 4,
    approver_group: str = APPROVER_GROUP,
    owner: str = OWNER,
    last_labels: tuple[str, ...] | None = None,
):
    """Admit REAL current-authority sources; ``last_labels`` revokes only the last."""

    from ne.contracts.source_authority import canonical_source_authority_stamps

    labels = (approver_group, owner)
    stamps = []
    for index in range(count):
        _receipt, stamp = admit_current_source(
            state.source_authority_reader,
            key=f"{key_prefix}-{index}-{time.monotonic_ns()}",
            labels=(last_labels if index == count - 1 and last_labels is not None else labels),
            content=f"protected evidence {index}".encode(),
        )
        stamps.append(stamp)
    return canonical_source_authority_stamps(stamps)


def invocation_plaintext(arguments: Mapping[str, object]) -> bytes:
    """The canonical invocation bytes the guard seals at park."""

    return json.dumps(
        dict(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def owner_context(ttl_s: int = 3600, *, principal_id: str = OWNER) -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=principal_id,
        authorization_groups=(),
        department=DEPARTMENT,
        issuer=ISSUER,
        expires_at=int(time.time()) + ttl_s,
    )


def build_service(state: Any, runtime_url: str, adapter: Any) -> tuple[Any, Any]:
    """The REAL workflow service over the app's own composed governance objects."""

    engine = sa.create_engine(runtime_url)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(engine, run_store=state.agent_run_store),
        run_store=state.agent_run_store,
        sink=state.sink,
        audit_engine=state.audit_engine,
        gateway=state.gateway,
        executor=state.executor,
        adapter=adapter,
        config_hash=state.config_hash,
        checkpoints=state.gate._run_approval_checkpoints,  # type: ignore[attr-defined]
        source_guard=state.source_guard,
    )
    return service, engine


def workflow_request(parked: Parked) -> RunWorkflowRequest:
    return RunWorkflowRequest(
        dispatch_id=parked.dispatch_id,
        run_id=parked.run_id,
        owner_principal_id=parked.owner_context.principal_id,
        config_hash=parked.config_hash,
        target=RunTurnTarget(
            conversation_id=parked.conversation_id,
            run_id=parked.run_id,
            turn_id=parked.turn_id,
            turn_sequence=parked.turn_sequence,
            attachment_id=parked.attachment_id,
            attempt_id=parked.attempt_id,
        ),
        # The EXACT snapshot admission stored — the workflow refuses any other.
        authenticated_context=parked.owner_context,
    )


def load_target(state: Any, parked: Parked) -> None:
    from ne.runtime.run_store import ne_agent_run_turn_targets

    engine = state.agent_run_store._engine
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == parked.run_id,
                ne_agent_run_turn_targets.c.attempt_id == parked.attempt_id,
            )
        ).one()
    parked.turn_id = str(row.turn_id)
    parked.turn_sequence = int(row.turn_sequence)
    parked.attachment_id = str(row.attachment_id)


async def admit_run(
    state: Any,
    *,
    action: str,
    arguments: Mapping[str, object],
    owner_token_ttl_s: int = 3600,
    owner: str = OWNER,
) -> Parked:
    """The atomic admission — ``admit_start`` exactly as the submission route calls it."""

    runs = state.agent_run_store
    conversation = await state.session_store.create_conversation(AGENT, DEPARTMENT, owner)
    context = owner_context(owner_token_ttl_s, principal_id=owner)
    accepted = await runs.admit_start(
        SimpleNamespace(
            idempotency_key=f"park-{uuid.uuid4().hex}",
            request_hash="1" * 64,
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id=owner,
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
            authenticated_context=context,
        )
    )
    parked = Parked(
        owner_context=context,
        run_id=accepted.run_id,
        attempt_id=accepted.attempt_id,
        dispatch_id=accepted.enqueue_id,
        conversation_id=conversation.conversation_id,
        config_hash=state.config_hash,
        action=action,
        arguments=dict(arguments),
    )
    load_target(state, parked)
    return parked


class ParkingAdapter:
    """The agent loop's ONE relevant act: checkpoint the intent, park, raise.

    Everything governance-bearing around it — the lease claim, the
    ``run-execution`` audit receipt, the park leg's approval-outbox drain — is
    the real workflow's, not this adapter's.
    """

    def __init__(
        self,
        checkpoints: Any,
        *,
        action: str,
        arguments: Mapping[str, object],
        tier: int,
        stamps: Any,
        principal_id: str = OWNER,
        expires_in: timedelta = timedelta(hours=1),
    ) -> None:
        self._checkpoints = checkpoints
        self._action = action
        self._arguments = dict(arguments)
        self._tier = tier
        self._stamps = stamps
        self._principal_id = principal_id
        self._expires_in = expires_in
        self.joined: Any = None

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        from ne.runtime.run_checkpoint import RunApprovalWaiting
        from ne.schema import ApprovalDecision

        now = datetime.now(UTC)
        # The agent loop checkpoints the MODEL-AUTHORED intent for this plan
        # ordinal before the governed call; this prose is what a replay cannot
        # reproduce.
        await context.boundary.checkpoint(
            stage="agentic-intent:0",
            safe_detail={
                "ordinal": 0,
                "tool": self._action,
                "step_intent": "send the close summary (draft 0)",
            },
        )
        # The exact identity the receipt-gated executor mints for the FIRST
        # governed tool call of this attempt.
        step_id = "run-tool"
        tool_call_id = f"{step_id}:tool:0"
        self.joined = await self._checkpoints.create_wait(
            run_id=context.run.run_id,
            attempt_id=context.attempt_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            action=self._action,
            # The SAME hash ``middleware._require_canonical_run_approval`` binds.
            action_hash=canonical_args_hash(self._arguments),
            tier=self._tier,
            principal_id=self._principal_id,
            policy_hash="e" * 64,
            invocation_plaintext=invocation_plaintext(self._arguments),
            source_stamps=self._stamps,
            stamp_receipt_id=f"stamp-{context.run.run_id}",
            stamp_receipt_hash="f" * 64,
            requested_at=now,
            sla_deadline=now + timedelta(minutes=5),
            expires_at=now + self._expires_in,
            event=new_approval_event(
                approval_id="allocated-inside-transaction",
                tier=self._tier,
                decision=ApprovalDecision.requested,
                subject_ref=f"tool:{self._action}",
                actor=self._principal_id,
            ),
        )
        raise RunApprovalWaiting(self.joined)


async def park_run(
    state: Any,
    runtime_url: str,
    *,
    stamps: Any,
    action: str,
    arguments: Mapping[str, object],
    tier: ApprovalTier | int = ApprovalTier.TIER_2,
    owner_token_ttl_s: int = 3600,
    adapter_factory: Any = None,
) -> Parked:
    """Admit and park an owned Run on a REAL approval, through the REAL workflow.

    Production shape end to end: ``admit_start`` (the atomic admission), then
    :meth:`RunWorkflowService.execute` — which claims the lease, writes the
    mandatory ``run-execution`` audit receipt, hands the adapter the
    receipt-gated executor, and runs its own park leg when the adapter raises
    ``RunApprovalWaiting``. Nothing here writes a governance row by hand.

    ``owner_token_ttl_s`` is the run owner's stored authorization snapshot TTL.
    The live ``ne-pilot`` realm sets ``accessTokenLifespan=900``; a negative
    value reproduces a decision that arrives after the horizon.
    """

    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    assert checkpoints is not None, "protected_transport harness must wire the coordinator"
    parked = await admit_run(
        state, action=action, arguments=arguments, owner_token_ttl_s=owner_token_ttl_s
    )
    factory = adapter_factory or ParkingAdapter
    adapter = factory(
        checkpoints,
        action=action,
        arguments=arguments,
        tier=int(tier),
        stamps=stamps,
    )
    service, engine = build_service(state, runtime_url, adapter)
    try:
        with authenticated_request_scope(parked.owner_context):
            result = await service.execute(workflow_request(parked))
        assert result.outcome == "waiting_approval"
    finally:
        engine.dispose()
    parked.approval_id = adapter.joined.approval_id
    assert_park_left_production_rows(state, runtime_url, parked)
    return parked


def assert_park_left_production_rows(state: Any, runtime_url: str, parked: Parked) -> None:
    """The setup invariant that no fabricated park could ever have satisfied.

    ``create_wait`` only ENQUEUES the approval audit intent into
    ``ne_agent_run_approval_audit_outbox``; the workflow's park leg is what
    drains it into the mandatory ``decision_kind='approval'`` Run audit receipt.
    Before train-63 nothing drained it, and
    ``RunFinalizer._observed_approvals`` therefore refused to compose ANY
    terminal for a run that had ever parked — so the run hung on every outcome,
    Deny included.

    Asserting it HERE means every test built on this helper fails at setup on a
    build with that defect, instead of quietly proceeding on hand-written rows.
    """

    from ne.runtime.run_checkpoint import ne_agent_run_approval_audit_outbox
    from ne.runtime.run_store import ne_agent_run_audit_receipts

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            receipts = int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_audit_receipts)
                    .where(
                        ne_agent_run_audit_receipts.c.run_id == parked.run_id,
                        ne_agent_run_audit_receipts.c.attempt_id == parked.attempt_id,
                        ne_agent_run_audit_receipts.c.approval_id == parked.approval_id,
                        ne_agent_run_audit_receipts.c.decision_kind == "approval",
                    )
                ).scalar_one()
            )
            unclaimed = int(
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_approval_audit_outbox)
                    .where(
                        ne_agent_run_approval_audit_outbox.c.approval_id == parked.approval_id,
                        ne_agent_run_approval_audit_outbox.c.claimed_at.is_(None),
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()
    assert receipts == 1, (
        "the REAL park leg must leave exactly one mandatory approval audit receipt "
        f"for {parked.approval_id}; found {receipts}. Without it "
        "RunFinalizer._observed_approvals refuses every terminal and the run hangs."
    )
    assert unclaimed == 0, (
        f"the park left {unclaimed} undrained ne_agent_run_approval_audit_outbox "
        f"row(s) for {parked.approval_id}"
    )
