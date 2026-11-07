# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The approval RESUME, un-stubbed: does the approved effect actually happen?

The defect these tests pin was found in production on 2026-08-28 and had NEVER
worked on any build. Approving a parked Tier-2 run approval returned HTTP 200
with a genuine APPROVED receipt and then the run never executed the approved
action: no send, no error, no terminal state. The run sat in ``running``
forever with the approval consumed.

There were THREE independent defects, and every one of them is exercised here
through the REAL chain — real ``RunApprovalCheckpointStore``, real
``ProtectedSourceResultGuard`` over REAL admitted authority receipts, real
``DurableApprovalGate`` + ``GovernedToolExecutor`` + Rego-faithful PDP + real
guardrail engine, real ``RunWorkflowService``/``RunFinalizer``, and the real
HTTP approvals router with a verified bearer. Nothing about the approval path is
stubbed. The ONLY doubles are the DBOS enqueue transport (a recorder), the tool
HANDLER at the very end of the governed chain (it records the effect instead of
sending mail), and a test adapter that models the agentic loop's ONE relevant
behaviour: it checkpoints the model-authored ``agentic-intent:{ordinal}`` prose
and then calls the tool, exactly as ``ne.runtime.runner`` does — with FRESH
prose on each call, because a re-called model does not reproduce its own words.

Why ``test_approvals_api.py`` could not catch any of this: it STUBS
``state.gate._run_approval_checkpoints`` and sets ``state.source_guard`` to a
bare ``SimpleNamespace``. A test that cannot fail on the broken build is worth
nothing. Why ``test_run_approval_decision_end_to_end.py`` could not catch it
either: it stops at ``checkpoints.resume`` and never executes the run, and its
parks use ``expires_at = now + 3600`` so they never cross the authorization
horizon.

The three defects:

1. CHECKPOINT INTENT REPLAY. The resume re-entered full agent execution, which
   re-called the model, which regenerated the ``step_intent`` prose embedded in
   the ``agentic-intent:{ordinal}`` checkpoint digest, which collided with the
   parked attempt's row: ``ValueError: Run checkpoint was replayed under a
   different intent`` (``run_workflow.py``), raised BEFORE finalization.
2. THE MISSING MANDATORY APPROVAL AUDIT RECEIPT. ``create_wait`` writes
   ``ne_agent_run_approval_audit_outbox`` and NOTHING drained it, so no
   ``decision_kind='approval'`` Run audit receipt ever existed in production —
   and ``RunFinalizer._observed_approvals`` refuses to compose ANY terminal for
   a Run that ever parked one ("approval join lacks its exact mandatory audit
   receipt"). So the terminalizer ITSELF crashed and the run could not even
   FAIL: it hung.
3. AUTHORIZATION EXPIRY WAS SILENT. ``AuthorizationExpiredError`` was raised
   into the void. The specification requires the platform to additionally make
   the parked approval/checkpoint non-actionable and terminally fail the attempt
   with safe reason ``authorization_expired``; ``grep`` confirmed that string
   appeared NOWHERE in ``packages/``. The expiry CHECK itself is a deliberate
   security property and is not weakened anywhere here — no snapshot is revived,
   rebound or re-minted, and nothing re-authorizes as a service identity
   (RED list).

RE-CUT 2026-08-29 after adversarial review REJECTED the first fix (which this
file's first cut passed — a green test over a broken build). What changed:

* Parks now persist REAL pre-park protected deliveries through the composed
  guard, because with an empty ``ne_agent_delivery_receipts`` set the
  finalizer's receipt-set comparison passes trivially. With real deliveries the
  first fix's resume SENT and then stranded — three production runs
  (2026-08-28 16:46:44 / 17:27:37 / 18:53:48) are stuck ``running`` in exactly
  that shape.
* The resume is driven through the REAL ``resume_run_approval_step`` (replay
  branch included), not a direct ``checkpoints.resume(...)``.
* New sections pin: one-approval-one-execution (replays SETTLE — F1), the
  pre-park-delivery observer seam (F2), ordinary-run expiry terminalization
  (F3), pre-deploy outbox backfill on both the resume and the expiry routes
  (F4), drain idempotency across lease epochs plus the outbox claim rowcount
  (F6), and the tool-name binding of the middleware short-circuit (F10).
  All eight FAIL on the reviewed build (``b7d861cb``) and pass with the fix.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import canonical_source_authority_stamps
from ne.contracts.types import ApprovalTier, Principal
from ne.events import new_approval_event
from ne.govern.config import ArgSpec, ToolArgKind, ToolArgPolicy
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.approvals import SELF_APPROVAL_REFUSED, canonical_args_hash
from ne.runtime.errors import ApprovalIntegrityError
from ne.runtime.run_checkpoint import (
    RunApprovalResumeRequest,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_finalizer import RunTurnTarget
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_store import (
    RunAuditDecisionKind,
    RunState,
    ne_agent_run_audit_receipts,
    ne_agent_runs,
)
from ne.runtime.run_workflow import (
    AUTHORIZATION_EXPIRED_REASON,
    RunWorkflowRequest,
    RunWorkflowService,
    RunWorkflowStateStore,
)
from ne.runtime.source_results import (
    authenticated_request_scope,
    complete_run_direct_publication_observation,
    direct_publication_scope,
)
from ne.runtime.tools import ToolBinding
from ne.schema import ApprovalDecision

from .conftest import ISSUER, AppBuilder, admit_current_source, get_state, run

OWNER = "user:ops"
DEPARTMENT = "operations"
APPROVER = "user:carol"
APPROVER_GROUP = "approvers"
AGENT = "agt_projection"

#: A Tier-2 effect tool. The SPEC tier is the floor the gate reads
#: (``middleware._effective_tier`` takes the max of spec / mirror / decided), so
#: this call routes to the real human gate exactly like the demo's
#: ``write.external.email.send``.
EFFECT_TOOL = "write.external.demo.notify"

_ARGUMENTS: dict[str, object] = {"recipient": "cfo@example.test", "subject": "Q3 close"}

#: `tiers.rego`'s built-in tier-2 STANDING-approval floor (7 days) — how long a
#: decision outlives the attempt it was asked for. A different clock from the
#: approval window, resolved beside it (F1, bundle 1.5.0).
_TIER2_STANDING_SECONDS = 604800.0


class _EffectLog:
    """The ONE double past the governance seam: the handler records, never sends."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []


def _effect_binding(log: _EffectLog) -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        log.calls.append(dict(args))
        return "sent"

    return ToolBinding(
        spec=ToolSpec(
            name=EFFECT_TOOL,
            description="the governed Tier-2 external effect this approval gates",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["recipient", "subject"],
            },
        ),
        handler=_handler,
    )


_POLICIES = {
    EFFECT_TOOL: ToolArgPolicy(
        args={
            "recipient": ArgSpec(kind=ToolArgKind.text, required=True),
            "subject": ArgSpec(kind=ToolArgKind.text, required=True),
        }
    )
}


class _RecordingEnqueuer:
    def __init__(self) -> None:
        self.requests: list[RunApprovalResumeRequest] = []

    async def enqueue_approval_resume(self, request: RunApprovalResumeRequest) -> None:
        self.requests.append(request)


class _AgenticLoopAdapter:
    """Models the ONE behaviour of the real loop that the resume trips over.

    ``ne.runtime.runner._RecordedExecutor`` durably checkpoints
    ``agentic-intent:{ordinal}`` with the MODEL-AUTHORED ``step_intent`` prose in
    the digest and only then calls the governed tool. A re-called model does not
    reproduce its own prose byte for byte, so a replayed attempt writes a
    different intent for the same stage — which is exactly the production
    ``ValueError: Run checkpoint was replayed under a different intent``.

    When the workflow hands this adapter a pinned effect continuation it takes
    the continuation branch instead: no model, no fresh prose, no replay — the
    approved effect and nothing else. This mirrors
    ``ne.worker.run_execution.RunProfileExecutionAdapter._execute_effect_continuation``,
    which is the production shape.
    """

    def __init__(self, log: _EffectLog) -> None:
        self._log = log
        self.loop_entries = 0
        self.continuation_entries = 0

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        principal = Principal(id=context.run.owner_principal_id, groups=())
        continuation = getattr(context, "continuation", None)
        if continuation is not None:
            self.continuation_entries += 1
            with direct_publication_scope(
                workflow_execution_id=context.run.run_id,
                publication_attempt_id=context.attempt_id,
            ):
                await context.executor.execute(
                    continuation.action, continuation.arguments, principal=principal
                )
                complete_run_direct_publication_observation()
            output = b"the approved action was executed after human approval"
            return RunExecutionResult(
                outcome="execution_complete",
                output_bytes=output,
                structured_envelope=_completed_envelope(output),
            )
        self.loop_entries += 1
        # A fresh model call authors fresh prose. This is not a contrivance: it
        # is the whole reason a replay-based resume cannot work.
        await context.boundary.checkpoint(
            stage="agentic-intent:0",
            safe_detail={
                "ordinal": 0,
                "tool": EFFECT_TOOL,
                "step_intent": f"send the close summary (draft {self.loop_entries})",
            },
        )
        with direct_publication_scope(
            workflow_execution_id=context.run.run_id,
            publication_attempt_id=context.attempt_id,
        ):
            await context.executor.execute(EFFECT_TOOL, _ARGUMENTS, principal=principal)
            complete_run_direct_publication_observation()
        output = b"done"
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=_completed_envelope(output),
        )


def _completed_envelope(output: bytes) -> Any:
    import hashlib

    from ne.runtime.run_finalizer import StructuredRunOutputEnvelope, claim_set_hash

    output_hash = hashlib.sha256(output).hexdigest()
    return StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=0,
        claim_set_hash=claim_set_hash(output_hash, ()),
        claims=(),
        disposition="completed",
    )


def _admit_four_sources(state: Any, *, last_labels: tuple[str, ...] | None = None):
    """Four REAL current-authority sources; ``last_labels`` revokes only the 4th."""

    labels = (APPROVER_GROUP, OWNER)
    stamps = []
    for index in range(4):
        _receipt, stamp = admit_current_source(
            state.source_authority_reader,
            key=f"resume-src-{index}-{time.monotonic_ns()}",
            labels=(last_labels if index == 3 and last_labels is not None else labels),
            content=f"protected evidence {index}".encode(),
        )
        stamps.append(stamp)
    return canonical_source_authority_stamps(stamps)


@dataclass
class _Parked:
    """Everything a park leaves behind that a resume needs to address it."""

    run_id: str
    attempt_id: str
    dispatch_id: str
    conversation_id: str
    config_hash: str
    owner_context: AuthenticatedRequestContext
    approval_id: str = ""
    turn_id: str = ""
    turn_sequence: int = 0
    attachment_id: str = ""


async def _park(
    state: Any,
    runtime_url: str,
    *,
    stamps: Any,
    owner_token_ttl_s: int = 3600,
    pre_park_deliveries: int = 0,
    park_drain: bool = True,
    standing_lifetime_s: float | None = _TIER2_STANDING_SECONDS,
) -> _Parked:
    """Admit and park an owned Run on a REAL Tier-2 approval, through the REAL workflow.

    Production shape end to end: ``admit_start`` (the atomic admission), then the
    REAL :meth:`RunWorkflowService.execute` — which claims the lease, writes the
    mandatory ``run-execution`` audit receipt, and hands the adapter the
    receipt-gated executor. The adapter stands in for the agent loop's ONE
    relevant act: it parks the wait on the exact server-minted
    ``(step_id, tool_call_id)`` the receipt-gated executor mints, with
    ``action_hash = canonical_args_hash(args)`` — the SAME hash
    ``middleware._require_canonical_run_approval`` binds — and raises the durable
    ``RunApprovalWaiting`` the gate raises. The workflow's own park leg then runs
    for real, which is what makes the mandatory approval audit receipt (or its
    absence, on the parent build) part of what these tests observe.

    ``owner_token_ttl_s`` is the run owner's stored authorization snapshot TTL.
    The live ``ne-pilot`` realm sets ``accessTokenLifespan=900``; a negative value
    reproduces a decision that arrives after the horizon.

    ``pre_park_deliveries`` persists that many REAL ``ne_agent_delivery_receipts``
    rows (via the store's own ``claim_delivery``, real stamps, RunEvent appended
    under the Run's lock) BEFORE the park — the shape of every real agent loop,
    which reads protected sources and delivers them to the model before hitting
    the Tier-2 effect. The 2026-08-28 production strandings (three successful
    sends, zero terminals) are invisible without them: an empty delivery set
    makes the finalizer's receipt-set comparison pass trivially.

    ``standing_lifetime_s`` is the POLICY-resolved standing-approval lifetime the
    real park path (``middleware._require_canonical_run_approval``) reads from
    the decision and freezes into the join (F1). This adapter calls
    ``create_wait`` directly, so it supplies the value the tier-2 bundle floor
    would have produced. ``None`` reproduces a park that recorded no policy
    lifetime, which mints no standing approval at all.

    ``park_drain=False`` suppresses the park leg's approval-audit drain,
    reproducing byte-for-byte the state a PRE-DEPLOY park left in production:
    an unclaimed ``ne_agent_run_approval_audit_outbox`` row and NO
    ``decision_kind='approval'`` Run audit receipt (4 such rows live right now).
    """

    from ne.runtime.run_checkpoint import RunApprovalWaiting

    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    assert checkpoints is not None, "protected_transport harness must wire the coordinator"
    runs = state.agent_run_store
    conversation = await state.session_store.create_conversation(AGENT, DEPARTMENT, OWNER)
    owner_context = AuthenticatedRequestContext(
        principal_id=OWNER,
        authorization_groups=(),
        department=DEPARTMENT,
        issuer=ISSUER,
        expires_at=int(time.time()) + owner_token_ttl_s,
    )
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
    parked = _Parked(
        owner_context=owner_context,
        run_id=accepted.run_id,
        attempt_id=accepted.attempt_id,
        dispatch_id=accepted.enqueue_id,
        conversation_id=conversation.conversation_id,
        config_hash=state.config_hash,
    )
    _load_target(state, parked)

    class _ParkingAdapter:
        def __init__(self) -> None:
            self.joined: Any = None

        async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
            now = datetime.now(UTC)
            # The REAL pre-park protected deliveries: the agent loop reads
            # sources (real evidence rows, real encrypted pending Tool results,
            # real stamps) and DELIVERS them to the model through the app's own
            # composed guard — `resolve_for_model_send` writes the durable
            # delivery receipt, its mandatory delivery audit receipt, and the
            # observer entry, exactly as production does before the Tier-2
            # effect parks.
            from ne.contracts.source_authority import SourceBoundToolResultRef
            from ne.runtime.run_store import EvidenceDeclaration

            for index in range(pre_park_deliveries):
                rendering = f"protected evidence rendering {index}".encode()
                stamp = stamps[index % len(stamps)]
                evidence = await state.agent_run_store.claim_evidence(
                    run_id=context.run.run_id,
                    owner_principal_id=OWNER,
                    attempt_id=context.attempt_id,
                    declaration=EvidenceDeclaration(
                        turn_id=context.target.turn_id,
                        step_id="run-tool",
                        tool_call_id=f"run-tool:read:{index}",
                        read_ordinal=index,
                        content_hash=hashlib.sha256(rendering).hexdigest(),
                        safe_event_detail={
                            "read_kind": "search",
                            "evidence_status": "located",
                            "contributor_count": 1,
                            "evidence_assurance": "direct",
                        },
                        locator_plaintext=None,
                        pending_result_plaintext=rendering,
                        source_stamps=(stamp,),
                    ),
                )
                delivered = await state.source_guard.resolve_for_model_send(
                    SourceBoundToolResultRef(
                        reference_id=evidence.pending_result_id,
                        result_hash=hashlib.sha256(rendering).hexdigest(),
                        stamps=(stamp,),
                    )
                )
                assert delivered.authorized, (
                    "pre-park delivery must be a REAL authorized protected delivery"
                )
            # The agent loop checkpoints the MODEL-AUTHORED intent for this plan
            # ordinal before the governed call, and this prose is what a replay
            # cannot reproduce.
            await context.boundary.checkpoint(
                stage="agentic-intent:0",
                safe_detail={
                    "ordinal": 0,
                    "tool": EFFECT_TOOL,
                    "step_intent": "send the close summary (draft 0)",
                },
            )
            # The exact identity the receipt-gated executor mints for the FIRST
            # governed tool call of this attempt.
            step_id = "run-tool"
            tool_call_id = f"{step_id}:tool:0"
            self.joined = await checkpoints.create_wait(
                run_id=context.run.run_id,
                attempt_id=context.attempt_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                action=EFFECT_TOOL,
                action_hash=canonical_args_hash(_ARGUMENTS),
                tier=2,
                principal_id=OWNER,
                policy_hash="e" * 64,
                invocation_plaintext=_invocation_plaintext(),
                source_stamps=stamps,
                stamp_receipt_id=f"stamp-{context.run.run_id}",
                stamp_receipt_hash="f" * 64,
                requested_at=now,
                sla_deadline=now + timedelta(minutes=5),
                expires_at=now + timedelta(hours=1),
                event=new_approval_event(
                    approval_id="allocated-inside-transaction",
                    tier=2,
                    decision=ApprovalDecision.requested,
                    subject_ref=f"tool:{EFFECT_TOOL}",
                    actor=OWNER,
                ),
                standing_lifetime_s=standing_lifetime_s,
            )
            raise RunApprovalWaiting(self.joined)

    adapter = _ParkingAdapter()
    service, engine = _build_service(state, runtime_url, adapter)
    if not park_drain:
        # PRE-DEPLOY SHAPE. The deployed park leg had no drain, so production
        # parks left the outbox row unclaimed and no approval receipt/intent
        # anywhere. Suppressing the drain (never deleting rows afterwards)
        # reproduces that state exactly, chain included.
        async def _no_drain(*_args: Any, **_kwargs: Any) -> None:
            return None

        service._record_parked_approval_receipt = _no_drain  # type: ignore[method-assign]
    try:
        with authenticated_request_scope(parked.owner_context):
            result = await service.execute(_workflow_request(parked))
        assert result.outcome == "waiting_approval"
    finally:
        engine.dispose()
    parked.approval_id = adapter.joined.approval_id
    return parked


def _invocation_plaintext() -> bytes:
    import json

    return json.dumps(_ARGUMENTS, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
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
            run_status = conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == run_id)
            ).scalar_one()
        return str(approval), str(checkpoint), str(run_status)
    finally:
        engine.dispose()


def _standing_row(runtime_url: str, predecessor_approval_id: str) -> Any:
    """The standing approval minted from this predecessor approval, or ``None``."""
    from ne.runtime.run_checkpoint import ne_agent_standing_approvals

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.select(ne_agent_standing_approvals).where(
                    ne_agent_standing_approvals.c.predecessor_approval_id == predecessor_approval_id
                )
            ).one_or_none()
    finally:
        engine.dispose()


def _terminal_intent_hash(runtime_url: str, run_id: str) -> str:
    """The terminal intent hash the finalizer actually PERSISTED for this run."""
    from ne.runtime.run_store import ne_agent_run_lifecycle_outbox

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    sa.select(ne_agent_run_lifecycle_outbox.c.terminal_intent_hash).where(
                        ne_agent_run_lifecycle_outbox.c.run_id == run_id
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _terminal_outcome(runtime_url: str, run_id: str) -> str:
    """The outcome word the run's terminal actually PERSISTED.

    Read from the durable lifecycle outbox, not from a return value: POST_DEMO
    item 4's first attempt passed every test it had while being illegal at the
    storage layer, precisely because nothing it asserted ever wrote a row.
    """
    from ne.runtime.run_store import ne_agent_run_lifecycle_outbox

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    sa.select(ne_agent_run_lifecycle_outbox.c.outcome).where(
                        ne_agent_run_lifecycle_outbox.c.run_id == run_id
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _failure_reason(runtime_url: str, run_id: str) -> str:
    from ne.runtime.run_workflow import ne_agent_run_execution_checkpoints

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            detail = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints.c.safe_detail).where(
                    ne_agent_run_execution_checkpoints.c.run_id == run_id,
                    ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
                )
            ).scalar_one_or_none()
        return "" if detail is None else str(detail)
    finally:
        engine.dispose()


def _build_service(state: Any, runtime_url: str, adapter: Any) -> tuple[Any, Any]:
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


def _workflow_request(parked: _Parked) -> RunWorkflowRequest:
    return RunWorkflowRequest(
        dispatch_id=parked.dispatch_id,
        run_id=parked.run_id,
        owner_principal_id=OWNER,
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


def _load_target(state: Any, parked: _Parked) -> None:
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


async def _approve_over_http(h: Any, approval_id: str) -> Any:
    async with h.client() as client:
        headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
        return await client.post(
            f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
        )


def _owner_context(ttl_s: int = 3600) -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id=OWNER,
        authorization_groups=(),
        department=DEPARTMENT,
        issuer=ISSUER,
        expires_at=int(time.time()) + ttl_s,
    )


def _make(make_app: AppBuilder, log: _EffectLog) -> Any:
    return make_app(
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        extra_tool_bindings=(_effect_binding(log),),
        extra_tool_policies=_POLICIES,
    )


def _resume_via_worker_step(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: Any,
    service: Any,
    engine: Any,
    resume_request: RunApprovalResumeRequest,
) -> Any:
    """Drive the resume through the REAL ``resume_run_approval_step``.

    The step's own logic — the decision-intent echo, the fenced status reads,
    the approved→resumed CAS, the replay branch, ``execute_resumed`` — runs
    verbatim. Only the two process-global composition lookups are redirected at
    the app harness's own governed objects: ``worker_platform()`` (boot-time
    global) and ``_run_workflow_service()`` (which composes the production
    adapter this test replaces with its agent-loop model). Driving
    ``checkpoints.resume`` directly instead — as the first cut of this file did
    — leaves the ENTIRE worker resume path untested, replay branch included.
    """

    import ne.worker.workflows as worker_workflows

    platform = SimpleNamespace(
        gate=state.gate,
        source_guard=state.source_guard,
        agent_run_store=state.agent_run_store,
        runtime_engine=engine,
    )
    monkeypatch.setattr(worker_workflows, "worker_platform", lambda: platform)
    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: service)
    return run(
        worker_workflows.resume_run_approval_step(
            resume_request.approval_id,
            resume_request.action_hash,
            resume_request.owner_principal_id,
            resume_request.policy_hash,
            resume_request.model_dump_json(),
        )
    )


def _outbox_row(runtime_url: str, approval_id: str) -> Any:
    from ne.runtime.run_checkpoint import ne_agent_run_approval_audit_outbox

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.select(ne_agent_run_approval_audit_outbox).where(
                    ne_agent_run_approval_audit_outbox.c.approval_id == approval_id
                )
            ).one_or_none()
    finally:
        engine.dispose()


def _reset_outbox_claim(runtime_url: str, approval_id: str) -> None:
    """Reopen the outbox row — the exact crash window between append-commit and
    ``complete_audit_intent``."""

    from ne.runtime.run_checkpoint import ne_agent_run_approval_audit_outbox

    engine = sa.create_engine(runtime_url)
    try:
        with engine.begin() as conn:
            changed = conn.execute(
                ne_agent_run_approval_audit_outbox.update()
                .where(ne_agent_run_approval_audit_outbox.c.approval_id == approval_id)
                .values(claimed_at=None)
            )
            assert changed.rowcount == 1
    finally:
        engine.dispose()


def _expire_lease(runtime_url: str, run_id: str) -> None:
    """Age the active lease out, as a worker death would — recovery may claim."""

    from ne.runtime.run_workflow import ne_agent_run_workflow_leases

    engine = sa.create_engine(runtime_url)
    try:
        with engine.begin() as conn:
            changed = conn.execute(
                ne_agent_run_workflow_leases.update()
                .where(ne_agent_run_workflow_leases.c.run_id == run_id)
                .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
            )
            assert changed.rowcount == 1
    finally:
        engine.dispose()


# --- (1) the in-window resume actually executes the approved effect ----------


def test_approved_continuation_executes_the_pinned_effect_and_completes(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approve in-window; the REAL worker resume executes the pinned effect ONCE.

    This is the flagship, re-cut after adversarial review of the first fix:

    * The park now persists REAL pre-park protected deliveries. On the first
      fix's build the resume opened a FRESH delivery observer for the SAME
      attempt, so the persisted pre-park receipts could never be accounted for
      again: the send SUCCEEDED and then `RunFinalizer.observe` raised "did not
      capture the complete receipt set" — and because the direct-publication
      boundary had completed, the FAILED terminal rejected the same snapshot
      too. Three production runs are stranded in `running` in exactly this
      shape (sends at 16:46:44 / 17:27:37 / 18:53:48 on 2026-08-28, zero
      terminals). With an empty delivery set — the first cut of this test —
      the comparison passes trivially and none of that is visible.
    * The resume is driven through the REAL `resume_run_approval_step`, using
      the resume request the approve route actually enqueued — not a direct
      `checkpoints.resume(...)` that bypasses the worker path entirely.
    * The continuation must end CONSUMED: `resumed` is not a terminal
      continuation state any more — it is the state that authorized exactly one
      execution, and leaving it in place is what allowed one approval to send
      N times.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, pre_park_deliveries=2))
    _load_target(state, parked)

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    assert len(enqueuer.requests) == 1, "approve must enqueue exactly one resume intent"

    adapter = _AgenticLoopAdapter(log)
    service, engine = _build_service(state, h.runtime_url, adapter)
    try:
        returned = _resume_via_worker_step(
            monkeypatch,
            state=state,
            service=service,
            engine=engine,
            resume_request=enqueuer.requests[0],
        )
    finally:
        engine.dispose()

    # THE EFFECT HAPPENED ONCE, with the EXACT arguments the human approved.
    assert log.calls == [_ARGUMENTS]
    # ...through the continuation, never by replaying the loop through a model.
    assert (adapter.continuation_entries, adapter.loop_entries) == (1, 0)
    _approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert run_status == RunState.COMPLETED.value, (
        "a resume with pre-park protected deliveries must still reach a terminal "
        f"(got {run_status!r}) — a send that terminalizes nothing is the production defect"
    )
    assert checkpoint == "consumed", (
        f"the continuation's one permitted execution must be durably fenced; got {checkpoint!r}"
    )
    # POST_DEMO item 4: the record now says HOW it completed. This run executed the
    # approved effect and did NOT replay the rest of its plan, so calling it
    # `execution_complete` — the word a run that finished its whole plan uses —
    # claimed more than happened. Asserted on the PERSISTED row.
    persisted = _terminal_outcome(h.runtime_url, parked.run_id)
    assert persisted == "approved_effect_complete", (
        "a resumed run must not claim the same completion as a run that ran its whole plan"
    )
    # ...and the outcome the worker step RETURNED must be that same one fact. A
    # Codex pass caught these disagreeing: the result re-derived `execution_complete`
    # at a seam that cannot see the continuation checkpoints, while the durable
    # terminal said otherwise — so a REPLAY, which reads the outbox, would answer
    # differently from the first completion for the very same run. This compares two
    # genuinely different layers: the step's returned JSON against the database row.
    assert json.loads(returned)["outcome"] == persisted, (
        "the returned outcome and the persisted terminal must be one fact, not two"
    )
    # ...and the resume ROUTE is bound into the finalizer intent hash, which is the
    # value `publish_terminal` records in the hash-chained audit provenance.
    #
    # ⚠️ MY FIRST VERSION OF THIS ASSERTION WAS VACUOUS and Codex said so: it
    # compared `_hash` of two dicts I built in the test, which proves only that
    # `_hash` distinguishes them — it would have passed with the binding deleted
    # from the finalizer entirely. This one reads the PERSISTED hash of a really
    # resumed run and recomputes what the same finalizer inputs produce WITHOUT the
    # route key. They must differ, or the route is not in the preimage.
    from ne.runtime.run_finalizer import _hash

    persisted_hash = _terminal_intent_hash(h.runtime_url, parked.run_id)
    assert persisted_hash, "a completed run must record a terminal intent hash"
    assert _hash({"schema": "ne.run-finalizer-intent.v1"}) != persisted_hash

    # And the key is emitted ONLY when the route is true, so a non-resumed run's
    # preimage stays byte-identical to prior releases. Adding it unconditionally
    # changed the digest for EVERY completed run under an unchanged `v1` tag and
    # turned a cross-release replay into a false C10 tamper alarm.
    from ne.runtime import run_finalizer as _rf

    source = Path(_rf.__file__).read_text()
    assert '**({"resumed_approved_effect": True} if resumed_route else {})' in source, (
        "the route key must be emitted conditionally; unconditional emission "
        "changes the intent-hash preimage for every completed run"
    )


def test_a_parked_run_records_the_mandatory_approval_audit_receipt(
    make_app: AppBuilder,
) -> None:
    """Defect 2 in isolation: a parked Run must be TERMINALIZABLE afterwards.

    ``create_wait`` writes ``ne_agent_run_approval_audit_outbox`` and, on the
    parent build, NOTHING drained it — so no ``decision_kind='approval'`` Run
    audit receipt ever existed, and ``RunFinalizer._observed_approvals`` refuses
    to compose ANY terminal for a Run that ever parked one:
    ``ValueError: approval join lacks its exact mandatory audit receipt``. Every
    outcome was blocked by it — completed, failed, rejected AND expired — which
    is why the production run hung in ``running`` rather than failing cleanly.

    The rejection here is the cheapest honest terminal to demand: it runs the
    real HTTP route, the real decision CAS and the real
    ``reconcile_approval_join`` -> ``finalize_unavailable_in_transaction`` path.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))

    assert _approval_receipts(h.runtime_url, parked) == 1, (
        "the park must leave exactly one mandatory approval audit receipt"
    )

    async def _reject() -> Any:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            return await client.post(
                f"/api/v1/approvals/{parked.approval_id}/reject",
                json={"reason": "not during close week"},
                headers=headers,
            )

    decided = run(_reject())
    assert decided.status_code == 200, decided.text

    # The worker reconciles the decision into the Run terminal — this is the
    # call that raised on the parent build.
    engine = sa.create_engine(h.runtime_url)
    try:
        lifecycle = RunWorkflowStateStore(engine, run_store=state.agent_run_store)
        joined = run(
            state.gate._run_approval_checkpoints.get_join(  # type: ignore[attr-defined]
                parked.approval_id
            )
        )
        publication = lifecycle.reconcile_approval_join(
            approval_id=parked.approval_id,
            action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
        )
    finally:
        engine.dispose()

    assert publication is not None
    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("rejected", "rejected")
    assert run_status == RunState.COMPLETED.value
    assert log.calls == []


def _approval_receipts(runtime_url: str, parked: _Parked) -> int:
    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return int(
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
    finally:
        engine.dispose()


# --- (2) the decision itself refuses once the OWNER authorization expired -----


def test_decision_after_owner_authorization_expiry_records_it_durably_inert(
    make_app: AppBuilder,
) -> None:
    """Post-horizon approve: the DECISION is kept, the ATTEMPT still dies.

    Before F1 this route threw the decision away — the approver was told "your
    decision was NOT recorded", which made an approval that took longer than a
    login impossible to give at all. What changed is ONLY the disposition of
    the decision. Deciding is an approver-authority act, evidenced by the
    approver's own fresh token, so it is recorded as a STANDING APPROVAL in the
    SAME transaction that terminalizes the attempt.

    THE SECURITY HALF IS UNTOUCHED AND IS ASSERTED HERE, NOT ASSUMED: the
    parked approval and its checkpoint become non-actionable, the run reaches a
    TERMINAL ``authorization_expired``, ZERO effects execute, ZERO resumes are
    enqueued, and the recorded row is durably INERT — ``standing``, unconsumed,
    naming no successor. Reviving, rebinding, or re-authorizing as the service
    identity remain RED and none of them happens: what runs
    later is a NEW attempt under the owner's own new authentication, pinned in
    ``ne-runtime/tests/test_standing_approval_successor.py``.

    MUTATION-CHECK: make ``_mint_standing_approval`` return the standing id but
    ALSO leave the approval row actionable (skip the expiry CAS) and this goes
    red on the ``("expired", "expired")`` assertion — the decision would be
    preserved by resurrecting the attempt, which is the RED shape.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60))
    _load_target(state, parked)

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 409, decided.text
    detail = decided.json()["detail"]
    # The SPEC-named code stays the PREFIX (machine-matchable); the rest is the
    # approver-facing guidance docs/architecture/approval-horizon.md asked for —
    # and it now tells the truth about what happened to their decision.
    assert detail.startswith(AUTHORIZATION_EXPIRED_REASON)
    assert "WAS recorded" in detail and "stands until" in detail
    assert "NOT recorded" not in detail

    # THE ATTEMPT IS DEAD. Every assertion this test made before F1, unchanged.
    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("expired", "expired")
    assert run_status == RunState.FAILED.value
    assert AUTHORIZATION_EXPIRED_REASON in _failure_reason(h.runtime_url, parked.run_id)
    assert enqueuer.requests == []
    assert log.calls == []

    # AND THE DECISION IS KEPT, DURABLY INERT.
    row = _standing_row(h.runtime_url, parked.approval_id)
    assert row is not None
    assert row.status == "standing"
    assert row.approver == APPROVER
    assert row.second_approver is None
    assert row.predecessor_run_id == parked.run_id
    assert row.predecessor_attempt_id == parked.attempt_id
    # INERT: it authorizes nothing until a successor consumes it, and no
    # successor exists.
    assert row.successor_approval_id is None
    assert row.successor_run_id is None
    assert row.consumed_at is None
    assert (row.lapses_at - row.decided_at).total_seconds() == pytest.approx(
        _TIER2_STANDING_SECONDS, rel=1e-6
    )


def test_a_park_with_no_policy_standing_lifetime_records_no_decision(
    make_app: AppBuilder,
) -> None:
    """No policy lifetime ⇒ no carryover. The pre-F1 answer, verbatim.

    A join that recorded no ``standing_lifetime_s`` (a legacy park, or a bundle
    that named none) must not inherit a lifetime nobody declared. The approver
    is told their decision was NOT recorded, because it was not.

    MUTATION-CHECK: give ``_standing_lifetime`` a default for NULL and this goes
    red — a standing approval appears and the 409 text flips to "WAS recorded".
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(
        _park(
            state,
            h.runtime_url,
            stamps=stamps,
            owner_token_ttl_s=-60,
            standing_lifetime_s=None,
        )
    )
    _load_target(state, parked)

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 409, decided.text
    assert "NOT recorded" in decided.json()["detail"]
    assert _standing_row(h.runtime_url, parked.approval_id) is None
    assert log.calls == []


def test_reject_after_owner_authorization_expiry_records_expiry_not_a_human_no(
    make_app: AppBuilder,
) -> None:
    """REJECT tells the same truth as approve (U2 adversarial review, F9).

    Before this fix ``reject()`` skipped the expiry pre-check, so rejecting a
    continuation whose owner snapshot had already aged out recorded a terminal
    ``rejected`` — a claim that a human weighed the request and said no — when
    the SPEC-named truth is ``authorization_expired``: the continuation was
    already dead no matter what any human decided.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60))
    _load_target(state, parked)

    async def _reject_over_http() -> Any:
        async with h.client() as client:
            headers = h.bearer(APPROVER, groups=[APPROVER_GROUP])
            return await client.post(
                f"/api/v1/approvals/{parked.approval_id}/reject",
                json={"reason": "declined"},
                headers=headers,
            )

    decided = run(_reject_over_http())
    assert decided.status_code == 409, decided.text
    detail = decided.json()["detail"]
    # The SPEC-named code stays the PREFIX (machine-matchable); the rest is the
    # approver-facing guidance docs/architecture/approval-horizon.md asked for.
    assert detail.startswith(AUTHORIZATION_EXPIRED_REASON)
    assert "NOT recorded" in detail and "start a new one" in detail

    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("expired", "expired")
    assert run_status == RunState.FAILED.value
    assert AUTHORIZATION_EXPIRED_REASON in _failure_reason(h.runtime_url, parked.run_id)
    assert enqueuer.requests == []
    assert log.calls == []
    # F1 preserves APPROVALS, never denials: "Denial requires no successor and
    # is never horizon-bounded." A standing DENIAL would authorize nothing, and
    # a successor that finds no standing approval already parks for a fresh
    # human decision — which is exactly what a denial should produce.
    assert _standing_row(h.runtime_url, parked.approval_id) is None


# --- (2b) the refusal may not claim a terminalization that did not happen -----


def _surface_records(audit_url: str, action: str, request_id: str) -> list[Any]:
    """Surface audit records under ``action`` naming ``request_id``, detail PARSED.

    ``emit_surface_audit`` json-encodes ``detail`` into the hash-covered payload,
    so the assertion has to read it back through ``json.loads`` — reading the
    raw string would let a substring coincidence pass for a field.
    """
    from ne.audit import list_records

    engine = sa.create_engine(audit_url)
    try:
        rows = list_records(engine, action=action)
    finally:
        engine.dispose()
    found = []
    for row in rows:
        detail = json.loads(row.payload["detail"])
        if detail.get("request_id") == request_id:
            found.append(SimpleNamespace(actor=row.actor, detail=detail))
    return found


def _force_approval_status(runtime_url: str, approval_id: str, status: str) -> None:
    """Flip ONLY the mutable approval row, exactly as the lazy expiry sweep does.

    The checkpoint stays ``pending`` and the Run stays ``waiting_approval`` —
    the shape three live runs have been sitting in since 2026-08-28, and the
    shape ``terminalize_authorization_expired``'s fail-closed ``actionable``
    set correctly refuses to act on.
    """
    engine = sa.create_engine(runtime_url)
    try:
        with engine.begin() as conn:
            updated = conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == approval_id)
                .values(status=status)
            )
        assert updated.rowcount == 1
    finally:
        engine.dispose()


def test_expiry_refusal_audits_the_terminalization_it_actually_performed(
    make_app: AppBuilder,
) -> None:
    """The ACTIONABLE path: it really terminalizes, so it really audits it.

    The no-regression half of the pair below. ``api.approval.authorization_expired``
    is the record that says "this attempt was made non-actionable and terminally
    failed", and here that is TRUE — so the record must exist, name this request,
    and carry the SPEC-named reason.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60))
    _load_target(state, parked)

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 409, decided.text

    # The terminalization HAPPENED ...
    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("expired", "expired")
    assert run_status == RunState.FAILED.value
    # ... so the record that asserts it is present and about THIS request.
    matching = _surface_records(
        h.audit_url, "api.approval.authorization_expired", parked.approval_id
    )
    assert len(matching) == 1, matching
    assert matching[0].detail["reason"] == AUTHORIZATION_EXPIRED_REASON
    assert matching[0].actor == APPROVER
    assert log.calls == []


def test_expiry_refusal_never_audits_a_terminalization_that_did_not_happen(
    make_app: AppBuilder,
) -> None:
    """The NON-actionable path: refuse truthfully, and do not forge the receipt.

    THE DEFECT. ``_refuse_expired_authorization`` called
    ``terminalize_authorization_expired`` and THREW THE RETURN VALUE AWAY. That
    call is fail-closed: outside its ``actionable`` set it writes nothing and
    answers ``False``. The caller emitted ``api.approval.authorization_expired``
    and the "nothing was sent or written / your decision was NOT recorded" 409
    anyway — so on a run whose approval row a sweep had already flipped to
    ``expired`` with the checkpoint still ``pending``, the audit trail asserted
    a terminalization that never occurred and the operator was told the run had
    been handled while it stayed in ``waiting_approval`` forever. Three live
    runs are in exactly that state.

    This is a governance product; the audit trail overstating what the platform
    did is a worse failure than the stranding itself, and it is what makes the
    audit row useless as evidence that any fix worked.

    THE GUARD IS NOT THE BUG and is not touched: the ``actionable`` set stays
    exactly as strict, no state is forced through it, and the run is left for an
    operator. Only the REPORT changes, from a false one to a true one.

    ON THE PARENT BUILD this test fails twice over: an
    ``api.approval.authorization_expired`` record IS written for this request,
    and the 409 detail claims the decision was not recorded rather than saying
    the request changed nothing.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60))
    _load_target(state, parked)

    # The lazy sweep got there first: approval ``expired``, checkpoint still
    # ``pending``, run still ``waiting_approval``. NOT in ``actionable``.
    _force_approval_status(h.runtime_url, parked.approval_id, "expired")
    assert _statuses(h.runtime_url, parked.approval_id, parked.run_id) == (
        "expired",
        "pending",
        RunState.WAITING_APPROVAL.value,
    )

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 409, decided.text
    detail = decided.json()["detail"]

    # NO forged receipt. Nothing was terminalized, so nothing says it was.
    expired_records = _surface_records(
        h.audit_url, "api.approval.authorization_expired", parked.approval_id
    )
    assert expired_records == [], expired_records

    # The refusal IS recorded — under the codebase's own "refused, nothing
    # changed" action, with a reason that names this refusal specifically.
    stale_records = _surface_records(h.audit_url, "api.approval.stale", parked.approval_id)
    assert len(stale_records) == 1, stale_records
    assert stale_records[0].detail["reason"] == "authorization-expired-not-terminalizable"
    assert stale_records[0].actor == APPROVER

    # The 409 is DISTINGUISHABLE from the terminalized case: same machine-matchable
    # prefix, and prose that says this request changed nothing rather than
    # implying the attempt was closed out.
    assert detail.startswith(AUTHORIZATION_EXPIRED_REASON)
    assert "CHANGED NOTHING" in detail
    assert "needs an operator" in detail

    # And the world is untouched: the guard refused, so no row moved, no effect
    # ran, no resume was enqueued, and no decision was preserved.
    assert _statuses(h.runtime_url, parked.approval_id, parked.run_id) == (
        "expired",
        "pending",
        RunState.WAITING_APPROVAL.value,
    )
    assert _standing_row(h.runtime_url, parked.approval_id) is None
    assert enqueuer.requests == []
    assert log.calls == []


# --- (2c) a refused DECIDER may not cancel the RUN's own reconciliation -------


def _expiry_observables(runtime_url: str, approval_id: str, run_id: str) -> Any:
    """The DISCRIMINATING observables of an authorization-expiry terminalization.

    Deliberately not the HTTP status: a 403 or a 409 plus an audit row is not
    evidence that anything was terminalized — mistaking one for the other is the
    exact confusion the expiry-truthfulness change exists to fix. These four
    columns are what "the run is finished" means in the database.
    """
    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            approval = conn.execute(
                sa.select(
                    ne_approval_requests.c.status,
                    ne_approval_requests.c.decided_reason,
                ).where(ne_approval_requests.c.request_id == approval_id)
            ).one()
            checkpoint = conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                    ne_agent_run_continuation_checkpoints.c.run_id == run_id
                )
            ).scalar_one()
            agent_run = conn.execute(
                sa.select(
                    ne_agent_runs.c.status,
                    ne_agent_runs.c.terminal_at,
                ).where(ne_agent_runs.c.run_id == run_id)
            ).one()
    finally:
        engine.dispose()
    return SimpleNamespace(
        approval_status=str(approval.status),
        decided_reason=None if approval.decided_reason is None else str(approval.decided_reason),
        checkpoint_status=str(checkpoint),
        run_status=str(agent_run.status),
        terminal_at=agent_run.terminal_at,
    )


async def _approve_over_http_as(h: Any, approval_id: str, *, principal: str) -> Any:
    """Approve as ``principal`` while HOLDING the approver group.

    The dual-role shape: one principal that both requested the parked effect and
    carries the ``approver`` role. It is not reachable on the demo box today
    (the ne-pilot realm's ``ops`` user is not in ``/approver``), which is a role
    ASSIGNMENT, so it is exactly the shape the separation-of-duties rule exists
    for — and exactly the shape that must not be able to strand a run.
    """
    async with h.client() as client:
        headers = h.bearer(principal, groups=[APPROVER_GROUP])
        return await client.post(
            f"/api/v1/approvals/{approval_id}/approve", json={}, headers=headers
        )


def test_self_approval_refusal_does_not_cancel_the_expiry_terminalization(
    make_app: AppBuilder,
) -> None:
    """A refused decider is still a decide call, and the RUN still gets reconciled.

    THE DEFECT (interaction, not either change alone). The separation-of-duties
    refusal was hoisted ABOVE both decide branches, which put it ahead of
    ``_refuse_expired_authorization``. But on the run-join path a decide call
    reaching that helper is the ONLY thing that terminalizes a run whose owner
    authorization ran out. So a dual-role requester deciding an expired,
    run-joined request got a 403 and the terminalization never ran — leaving the
    run parked in ``waiting_approval`` with nothing left able to finish it. That
    is the stranded-run condition, arrived at through the door that was just
    built to keep the audit trail honest about it.

    Expiry is a fact about the RUN; separation of duties is a fact about the
    DECIDER. The run is reconciled first, whoever asked.

    NOTHING IS GRANTED BY THAT ORDERING. The F1 standing-approval carryover is
    turned OFF for a refused decider — their decision is not approver-authority
    evidence — so no successor can spend it, no effect ran, and no resume was
    enqueued. The decision itself is still refused, still 403, still audited
    ``requester-may-not-decide``.

    ON THE PARENT BUILD (the refusal hoisted above the branch) this fails on the
    observables, not the status: the approval row stays ``pending``, its
    ``decided_reason`` stays NULL, the checkpoint stays ``pending`` and the run
    stays ``waiting_approval`` with ``terminal_at`` NULL.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    # The run owner IS the requester recorded on the approval row.
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60))
    _load_target(state, parked)
    assert _statuses(h.runtime_url, parked.approval_id, parked.run_id) == (
        "pending",
        "pending",
        RunState.WAITING_APPROVAL.value,
    )

    decided = run(_approve_over_http_as(h, parked.approval_id, principal=OWNER))

    # THE DECISION IS REFUSED, and refused for being self-approval.
    assert decided.status_code == 403, decided.text
    assert decided.json()["detail"] == SELF_APPROVAL_REFUSED

    # THE RUN IS TERMINALIZED ANYWAY — the discriminating observables.
    observed = _expiry_observables(h.runtime_url, parked.approval_id, parked.run_id)
    assert observed.approval_status == "expired"
    assert observed.decided_reason == AUTHORIZATION_EXPIRED_REASON
    assert observed.checkpoint_status == "expired"
    assert observed.run_status == RunState.FAILED.value
    assert observed.terminal_at is not None

    # NOTHING WAS GRANTED. No standing carryover for a successor to spend, no
    # effect, no resume — and no approver recorded on the row.
    assert _standing_row(h.runtime_url, parked.approval_id) is None
    assert enqueuer.requests == []
    assert log.calls == []

    # THE AUDIT TRAIL SAYS BOTH TRUE THINGS AND NEITHER FALSE ONE: the run was
    # terminalized, and this principal was refused as the requester.
    terminalized = _surface_records(
        h.audit_url, "api.approval.authorization_expired", parked.approval_id
    )
    assert len(terminalized) == 1, terminalized
    assert terminalized[0].detail["reason"] == AUTHORIZATION_EXPIRED_REASON
    assert "standing_id" not in terminalized[0].detail
    refused = _surface_records(h.audit_url, "api.approval.unauthorized", parked.approval_id)
    assert len(refused) == 1, refused
    assert refused[0].detail["reason"] == "requester-may-not-decide"
    assert refused[0].actor == OWNER


def test_self_approval_refusal_terminalizes_nothing_while_authorization_is_LIVE(
    make_app: AppBuilder,
) -> None:
    """The other half: reconciling first must not become "terminalize on refusal".

    The fix moves the RUN's reconciliation ahead of the DECIDER's refusal; it
    does not give the refusal a terminalizing side effect. Inside the horizon
    ``_refuse_expired_authorization`` returns having done nothing at all, and a
    self-approval attempt must leave the parked run exactly as it found it —
    still ``waiting_approval``, still awaiting a DIFFERENT named human.

    Without this, the reordering could "pass" the test above by stranding the
    live runs instead: a 403 that kills the run it refused is a far worse bug
    than the one being fixed.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=3600))
    _load_target(state, parked)

    decided = run(_approve_over_http_as(h, parked.approval_id, principal=OWNER))
    assert decided.status_code == 403, decided.text
    assert decided.json()["detail"] == SELF_APPROVAL_REFUSED

    observed = _expiry_observables(h.runtime_url, parked.approval_id, parked.run_id)
    assert observed.approval_status == "pending"
    assert observed.decided_reason is None
    assert observed.checkpoint_status == "pending"
    assert observed.run_status == RunState.WAITING_APPROVAL.value
    assert observed.terminal_at is None
    assert _standing_row(h.runtime_url, parked.approval_id) is None
    assert enqueuer.requests == []
    assert log.calls == []
    assert (
        _surface_records(h.audit_url, "api.approval.authorization_expired", parked.approval_id)
        == []
    )

    # Still decidable by a DIFFERENT named human — the refusal refused this
    # decider, not the request.
    granted = run(_approve_over_http(h, parked.approval_id))
    assert granted.status_code == 200, granted.text
    assert granted.json()["status"] == "approved"


def test_expired_self_approval_that_cannot_be_terminalized_still_refuses_and_grants_nothing(
    make_app: AppBuilder,
) -> None:
    """Expired, self-approving, AND outside the fail-closed ``actionable`` set.

    ``terminalize_authorization_expired`` answers ``False`` here (a sweep already
    flipped the approval row to ``expired`` while the checkpoint stayed
    ``pending``), and that guard is not widened by this change. What must hold is
    that the audit trail still refuses to overstate — ``api.approval.stale`` with
    ``authorization-expired-not-terminalizable`` and NO
    ``api.approval.authorization_expired`` — and that the decider is still
    refused with nothing granted.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60))
    _load_target(state, parked)
    _force_approval_status(h.runtime_url, parked.approval_id, "expired")

    decided = run(_approve_over_http_as(h, parked.approval_id, principal=OWNER))
    assert decided.status_code == 403, decided.text
    assert decided.json()["detail"] == SELF_APPROVAL_REFUSED

    # No forged receipt, and the truthful "refused, nothing changed" record.
    assert (
        _surface_records(h.audit_url, "api.approval.authorization_expired", parked.approval_id)
        == []
    )
    stale = _surface_records(h.audit_url, "api.approval.stale", parked.approval_id)
    assert len(stale) == 1, stale
    assert stale[0].detail["reason"] == "authorization-expired-not-terminalizable"

    assert _statuses(h.runtime_url, parked.approval_id, parked.run_id) == (
        "expired",
        "pending",
        RunState.WAITING_APPROVAL.value,
    )
    assert _standing_row(h.runtime_url, parked.approval_id) is None
    assert enqueuer.requests == []
    assert log.calls == []


# --- (3) the exact production race: decided in-window, resumed out-of-window --


def test_resume_after_expiry_terminalizes_instead_of_stranding(make_app: AppBuilder) -> None:
    """Decide INSIDE the window, resume AFTER it. Terminal, not stranded.

    This is the production shape: the approver decided at 11:18:17 well inside
    the token window, and the worker's resume then ran against a snapshot that
    had aged out. On the parent build the resume raised
    ``AuthorizationExpiredError`` into the void, the run stayed ``running``
    forever and the approver held a 200 APPROVED receipt for a dead run.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    _load_target(state, parked)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text

    async def _resume_checkpoint() -> None:
        joined = await checkpoints.get_join(parked.approval_id)
        await checkpoints.resume(
            parked.approval_id,
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            resumed_at=datetime.now(UTC),
        )

    run(_resume_checkpoint())

    adapter = _AgenticLoopAdapter(log)
    service, engine = _build_service(state, h.runtime_url, adapter)
    try:
        # The bound snapshot has aged out between the decision and the resume.
        async def _execute() -> Any:
            # The bound snapshot has aged out between the decision and the
            # resume. The STORED snapshot is unchanged — nothing rebinds it.
            with authenticated_request_scope(_owner_context(ttl_s=-30)):
                return await service.execute(_workflow_request(parked))

        with pytest.raises(Exception):  # noqa: B017 - a raise OR a terminal is fine
            run(_execute())
    except BaseException:  # pragma: no cover - the terminal path returns instead
        pass
    finally:
        engine.dispose()

    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("expired", "expired")
    assert run_status == RunState.FAILED.value
    assert AUTHORIZATION_EXPIRED_REASON in _failure_reason(h.runtime_url, parked.run_id)
    assert log.calls == []
    # F1's OTHER source of a standing approval: the decision landed INSIDE the
    # window and the resume then crossed the horizon. The human answered, the
    # answer was never consumed, and the attempt is terminal — so the answer
    # survives, inert, exactly as in the post-horizon-decision case.
    standing = _standing_row(h.runtime_url, parked.approval_id)
    assert standing is not None
    assert standing.status == "standing"
    assert standing.approver == APPROVER
    assert standing.consumed_at is None


# --- (4) the governance floor is not relaxed by any of the above -------------


def test_owner_losing_one_source_of_four_withholds_at_resume_and_fails_the_run(
    make_app: AppBuilder,
) -> None:
    """THREE of four sources still admit the owner; the fourth no longer does.

    The continuation resume must not become a way around the stamp floor. The
    approver's own authority is intact, so the DECISION lands (an approver's
    ACL standing is never the run's authority — U2 adversarial review, F1/F5:
    the earlier revision of this test revoked NOTHING, excluded the approver
    instead, and enshrined a read/decide destroying the Run as intended). The
    RESUME then re-evaluates every stamp under the RUN OWNER's bound context:
    permanently withheld, the request terminalizes ``source_withheld``, the Run
    FAILS and the effect handler is never reached.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state, last_labels=(APPROVER_GROUP, "user:somebody-else"))
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    _load_target(state, parked)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text
    assert [request.approval_id for request in enqueuer.requests] == [parked.approval_id]

    async def _resume_checkpoint() -> None:
        joined = await checkpoints.get_join(parked.approval_id)
        await checkpoints.resume(
            parked.approval_id,
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            resumed_at=datetime.now(UTC),
        )

    run(_resume_checkpoint())

    adapter = _AgenticLoopAdapter(log)
    service, engine = _build_service(state, h.runtime_url, adapter)
    try:

        async def _execute() -> Any:
            with authenticated_request_scope(_owner_context()):
                return await service.execute(_workflow_request(parked))

        with pytest.raises(Exception):  # noqa: B017 - a raise OR a terminal is fine
            run(_execute())
    except BaseException:  # pragma: no cover - the terminal path returns instead
        pass
    finally:
        engine.dispose()

    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("source_withheld", "source_withheld")
    assert run_status == RunState.FAILED.value
    assert log.calls == []


# --- (5) one approval, ONE execution: replays settle, never re-send ----------


class _EffectThenCrashAdapter:
    """Executes the pinned continuation effect, then dies before finalization.

    The production window this models: the send settled, the worker was lost
    (lease takeover / process death) before the terminal committed, and DBOS
    re-executed the resume. On the reviewed build NOTHING marked the
    continuation consumed, so every such re-execution matched ``resumed``
    again, rebuilt a fresh executor (whose one-call limit resets with it),
    was re-admitted by the middleware short-circuit, and SENT AGAIN — one
    human approval, N emails.
    """

    def __init__(self, log: _EffectLog) -> None:
        self._log = log

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        from ne.runtime.run_runner import RunLeaseLost

        continuation = context.continuation
        assert continuation is not None, "this adapter only models the resume execution"
        principal = Principal(id=context.run.owner_principal_id, groups=())
        with direct_publication_scope(
            workflow_execution_id=context.run.run_id,
            publication_attempt_id=context.attempt_id,
        ):
            await context.executor.execute(
                continuation.action, continuation.arguments, principal=principal
            )
            complete_run_direct_publication_observation()
        raise RunLeaseLost("simulated worker death after the send settled")


def test_replayed_resume_settles_instead_of_reexecuting_the_effect(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL: the effect fires ONCE even when the resume workflow replays.

    First execution: the REAL worker step resumes, the governed executor sends,
    the worker dies before any terminal commits. Second execution: the SAME
    decision intent replays through the SAME worker step (exactly what DBOS
    recovery does). The replay must SETTLE — here as the honest
    ``RunEffectSettlementUncertain`` FAILED terminal, because the effect
    provably issued but its finalization is unknown — and must NOT execute the
    approved action a second time. On the reviewed build the second execution
    finds the continuation still ``resumed`` and sends again.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    _load_target(state, parked)

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text
    assert len(enqueuer.requests) == 1

    from ne.runtime.run_runner import RunLeaseLost

    crash_service, crash_engine = _build_service(state, h.runtime_url, _EffectThenCrashAdapter(log))
    try:
        with pytest.raises(RunLeaseLost):
            _resume_via_worker_step(
                monkeypatch,
                state=state,
                service=crash_service,
                engine=crash_engine,
                resume_request=enqueuer.requests[0],
            )
    finally:
        crash_engine.dispose()
    assert log.calls == [_ARGUMENTS], "the first execution must have sent exactly once"

    # The worker is dead; its lease ages out and recovery replays the intent.
    _expire_lease(h.runtime_url, parked.run_id)

    replay_adapter = _AgenticLoopAdapter(log)
    replay_service, replay_engine = _build_service(state, h.runtime_url, replay_adapter)
    try:
        _resume_via_worker_step(
            monkeypatch,
            state=state,
            service=replay_service,
            engine=replay_engine,
            resume_request=enqueuer.requests[0],
        )
    finally:
        replay_engine.dispose()

    assert log.calls == [_ARGUMENTS], (
        f"one approval authorized ONE execution; observed {len(log.calls)} sends — "
        "a replayed resume must settle, never re-execute"
    )
    _approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert checkpoint == "consumed"
    assert run_status == RunState.FAILED.value, (
        "an issued-but-unfinalized effect settles as the honest uncertain FAILED "
        f"terminal; got {run_status!r}"
    )
    assert "RunEffectSettlementUncertain" in _failure_reason(h.runtime_url, parked.run_id)


# --- (6) ordinary runs: mid-execution expiry still reaches a FAILED terminal --


class _OrdinaryExpiryAdapter:
    """An ordinary agent loop whose bound authorization ages out mid-run."""

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        from ne.runtime.source_results import AuthorizationExpiredError

        del context
        raise AuthorizationExpiredError("bound authorization snapshot is past verified expiry")


def test_ordinary_run_expiry_terminalizes_failed_instead_of_stranding(
    make_app: AppBuilder,
) -> None:
    """A run with NO parked approval must still FAIL terminally on expiry.

    The reviewed build put ``except AuthorizationExpiredError`` before
    ``except Exception`` and bare-raised when there was no resumed approval to
    terminalize — and one ``try`` runs at most one handler, so the generic
    failure terminalization (which every other exception reaches) was
    unreachable. Result: a NEW stranding regression for ordinary runs, on the
    exact path that reached a FAILED terminal before the fix.
    """

    from ne.runtime.source_results import AuthorizationExpiredError

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    stamps = _admit_four_sources(state)
    del stamps  # ordinary run: no park, no approval, no continuation
    runs = state.agent_run_store
    assert runs is not None
    conversation = run(state.session_store.create_conversation(AGENT, DEPARTMENT, OWNER))
    owner_context = _owner_context()
    accepted = run(
        runs.admit_start(
            SimpleNamespace(
                idempotency_key=f"ordinary-{uuid.uuid4().hex}",
                request_hash="1" * 64,
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
    )
    parked = _Parked(
        owner_context=owner_context,
        run_id=accepted.run_id,
        attempt_id=accepted.attempt_id,
        dispatch_id=accepted.enqueue_id,
        conversation_id=conversation.conversation_id,
        config_hash=state.config_hash,
    )
    _load_target(state, parked)

    service, engine = _build_service(state, h.runtime_url, _OrdinaryExpiryAdapter())
    try:

        async def _execute() -> Any:
            with authenticated_request_scope(owner_context):
                return await service.execute(_workflow_request(parked))

        with pytest.raises(AuthorizationExpiredError):
            run(_execute())
    finally:
        engine.dispose()

    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.connect() as conn:
            run_status = str(
                conn.execute(
                    sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == parked.run_id)
                ).scalar_one()
            )
    finally:
        engine.dispose()
    assert run_status == RunState.FAILED.value, (
        f"an ordinary run's expiry must terminalize FAILED, not strand in {run_status!r}"
    )
    assert "AuthorizationExpiredError" in _failure_reason(h.runtime_url, parked.run_id)


# --- (7) pre-deploy parks: undrained outbox rows must become terminalizable --


def test_predeploy_park_drains_on_resume_and_completes(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A park that PRE-DATES the drain must still resume to a terminal.

    Production holds 4 unclaimed ``ne_agent_run_approval_audit_outbox`` rows
    written before the park-leg drain existed: no ``decision_kind='approval'``
    receipt exists for them, and the ONLY drain site on the reviewed build is
    the park leg itself — which already ran, pre-deploy, without draining. On
    that build every terminal for such a run dies in
    ``RunFinalizer._observed_approvals`` ("approval join lacks its exact
    mandatory audit receipt"). The decision routes and the workflow must
    backfill the receipt instead.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps, park_drain=False))
    _load_target(state, parked)

    outbox = _outbox_row(h.runtime_url, parked.approval_id)
    assert outbox is not None and outbox.claimed_at is None, (
        "precondition: the pre-deploy park left its outbox intent unclaimed"
    )
    assert _approval_receipts(h.runtime_url, parked) == 0, (
        "precondition: the pre-deploy park has no mandatory approval receipt"
    )

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text
    assert len(enqueuer.requests) == 1

    adapter = _AgenticLoopAdapter(log)
    service, engine = _build_service(state, h.runtime_url, adapter)
    try:
        _resume_via_worker_step(
            monkeypatch,
            state=state,
            service=service,
            engine=engine,
            resume_request=enqueuer.requests[0],
        )
    finally:
        engine.dispose()

    assert log.calls == [_ARGUMENTS]
    _approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert run_status == RunState.COMPLETED.value, (
        f"a pre-deploy park must terminalize after its resume; got {run_status!r}"
    )
    assert checkpoint == "consumed"
    assert _approval_receipts(h.runtime_url, parked) == 1, (
        "the backfill must land exactly one mandatory approval receipt"
    )
    outbox = _outbox_row(h.runtime_url, parked.approval_id)
    assert outbox is not None and outbox.claimed_at is not None, (
        "the backfill must claim the pre-deploy outbox row"
    )


def test_predeploy_park_expiry_route_refuses_409_not_500(make_app: AppBuilder) -> None:
    """The expiry refusal must not 500 on a pre-deploy park.

    On the reviewed build the approve route's expiry leg
    (``terminalize_authorization_expired`` → ``finalize_unavailable_in_transaction``
    → ``_observed_approvals``) raises "approval join lacks its exact mandatory
    audit receipt" for a park with no drained receipt, the transaction rolls
    back, and the approver gets a 500 for a park the platform itself left
    undrainable. With the decision-route backfill it is the designed 409.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(
        _park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60, park_drain=False)
    )
    _load_target(state, parked)
    assert _approval_receipts(h.runtime_url, parked) == 0

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 409, (
        f"expiry on a pre-deploy park must refuse 409, got {decided.status_code}: {decided.text}"
    )
    detail = decided.json()["detail"]
    # The SPEC-named code stays the PREFIX (machine-matchable); the rest is the
    # approver-facing guidance docs/architecture/approval-horizon.md asked for.
    # This park carries a policy standing lifetime, so the decision is preserved
    # (F1) — the point of THIS test is the 409-not-500 on an undrained
    # pre-deploy park, and preserving the decision must not disturb it.
    assert detail.startswith(AUTHORIZATION_EXPIRED_REASON)
    assert "WAS recorded" in detail

    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("expired", "expired")
    assert run_status == RunState.FAILED.value
    assert _approval_receipts(h.runtime_url, parked) == 1
    assert enqueuer.requests == []
    assert log.calls == []


# --- (8) the drain is idempotent ACROSS lease epochs -------------------------


def test_drain_replay_across_lease_epochs_keeps_exactly_one_receipt(
    make_app: AppBuilder,
) -> None:
    """Crash window: append committed, ``complete_audit_intent`` did not.

    Recovery then re-drains under the NEXT lease epoch. On the reviewed build
    the receipt address embedded ``event_ordinal=claim.lease_epoch``, so the
    replay landed on a DIFFERENT deterministic address and minted a SECOND
    ``decision_kind='approval'`` receipt — after which
    ``RunFinalizer._observed_approvals`` (which demands exactly one) can never
    compose ANY terminal for the run, ever. The address must be epoch-stable.

    This drives the drain seam directly (the private method the park leg and
    every backfill site share) because the crash window between its two
    commits cannot be scheduled from the public surface.
    """

    from ne.runtime.run_audit_receipts import RunAuditReceiptSink, RunAuditTarget
    from ne.runtime.run_store import RunAuditDecisionKind
    from ne.runtime.run_workflow import RunWorkflowClaim

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    assert _approval_receipts(h.runtime_url, parked) == 1

    # The crash: the park's append committed but the outbox claim did not.
    _reset_outbox_claim(h.runtime_url, parked.approval_id)

    # Recovery re-drains under a BUMPED lease epoch.
    service, engine = _build_service(state, h.runtime_url, _AgenticLoopAdapter(log))
    try:
        recovered_claim = RunWorkflowClaim(
            dispatch_id=parked.dispatch_id,
            run_id=parked.run_id,
            owner_principal_id=OWNER,
            attempt_id=parked.attempt_id,
            lease_token=str(uuid.uuid4()),
            lease_epoch=7,
            recovered=True,
            claim_event_id=str(uuid.uuid4()),
        )
        run_store = state.agent_run_store
        assert run_store is not None
        audit = RunAuditReceiptSink(
            state.sink,
            audit_engine=state.audit_engine,
            runtime_engine=run_store._engine,
            run_store=run_store,
            target=RunAuditTarget(
                run_id=parked.run_id,
                attempt_id=parked.attempt_id,
                step_id="run-execution",
                decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
                event_ordinal=0,
            ),
            config_hash=state.config_hash,
        )
        run(
            service._record_parked_approval_receipt(
                audit, claim=recovered_claim, approval_id=parked.approval_id
            )
        )
    finally:
        engine.dispose()

    assert _approval_receipts(h.runtime_url, parked) == 1, (
        "a re-drained park must land on the SAME receipt address; a second "
        "decision_kind='approval' receipt makes the run permanently unterminalizable"
    )
    outbox = _outbox_row(h.runtime_url, parked.approval_id)
    assert outbox is not None and outbox.claimed_at is not None


def test_drain_refuses_to_claim_over_duplicate_approval_receipts(
    make_app: AppBuilder,
) -> None:
    """>1 approval receipts: the drain must refuse LOUDLY and claim nothing.

    `_observed_approvals` demands exactly one `decision_kind='approval'`
    receipt per join, so a join carrying two is already permanently
    unterminalizable and this seam cannot know which receipt is genuine.
    Claiming the intent anyway — what the first cut of the fix did for any
    count `> 0` — consumes the one durable marker pointing an operator at the
    corruption while leaving the run just as dead. The runbook
    (docs/ops/approval-resume-repair.md) requires owner intervention at
    `receipts > 1`; the code must match it: raise, do not claim, change
    nothing.
    """

    from ne.runtime.run_workflow import _append_safe_event

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    assert _approval_receipts(h.runtime_url, parked) == 1

    # The F6 disease, forged durably: a SECOND approval receipt at a different
    # address (as an epoch-keyed drain replay used to mint), with the outbox
    # intent back in its unclaimed crash-window state.
    _reset_outbox_claim(h.runtime_url, parked.approval_id)
    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.begin() as conn:
            duplicate_event_id = _append_safe_event(
                conn,
                run_id=parked.run_id,
                attempt_id=parked.attempt_id,
                event_kind="run.approval.audit-observed",
                event_status="observed",
                safe_detail={"approval_id": parked.approval_id},
                step_id="run-approval",
            )
    finally:
        engine.dispose()
    duplicate_audit_id = f"duplicate-approval-audit-{uuid.uuid4().hex}"
    run_store = state.agent_run_store
    assert run_store is not None
    run(
        run_store.record_audit_receipt(
            run_id=parked.run_id,
            attempt_id=parked.attempt_id,
            run_event_id=duplicate_event_id,
            decision_kind=RunAuditDecisionKind.APPROVAL,
            audit_event_id=duplicate_audit_id,
            audit_sequence=99,
            audit_record_hash=hashlib.sha256(duplicate_audit_id.encode()).hexdigest(),
            config_hash=state.config_hash,
            owner_principal_id=OWNER,
            step_id="run-approval",
            approval_id=parked.approval_id,
        )
    )
    assert _approval_receipts(h.runtime_url, parked) == 2

    service, engine = _build_service(state, h.runtime_url, _AgenticLoopAdapter(log))
    try:
        with pytest.raises(RuntimeError, match="exactly one is mandated") as duplicate_refusal:
            run(
                service.record_approval_audit_receipt(
                    run_id=parked.run_id,
                    attempt_id=parked.attempt_id,
                    owner_principal_id=OWNER,
                    approval_id=parked.approval_id,
                )
            )
    finally:
        engine.dispose()

    # The refusal must read as an INTERVENTION path, not a retry (coordinator
    # ruling on the loud 500): it names the runbook and its exact section, so
    # whoever hits it is routed to the owner instead of retrying it away.
    refusal = str(duplicate_refusal.value)
    assert "approval-resume-repair.md" in refusal
    assert "Step 4" in refusal
    assert "NOT a retry" in refusal

    # NOTHING changed: the intent is still unclaimed (the operator's marker),
    # and no third receipt appeared.
    outbox = _outbox_row(h.runtime_url, parked.approval_id)
    assert outbox is not None and outbox.claimed_at is None, (
        "refusing must leave the unclaimed intent intact for operator recovery — "
        "claiming it destroys the evidence of the corruption"
    )
    assert _approval_receipts(h.runtime_url, parked) == 2


def test_complete_audit_intent_refuses_an_unknown_intent(make_app: AppBuilder) -> None:
    """Claiming an intent that does not exist must raise, not silently no-op.

    On the reviewed build ``complete_audit_intent`` never checked its rowcount,
    so a caller could "drain" nothing and report success.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        run(
            checkpoints.complete_audit_intent(
                f"intent-{uuid.uuid4().hex}", claimed_at=datetime.now(UTC)
            )
        )


# --- (9) the middleware short-circuit is bound to the approved TOOL ----------


OTHER_TOOL = "write.external.demo.escalate"


def _other_binding(log: _EffectLog) -> ToolBinding:
    async def _handler(args: Mapping[str, object]) -> str:
        log.calls.append(dict(args))
        return "escalated"

    return ToolBinding(
        spec=ToolSpec(
            name=OTHER_TOOL,
            description="a DIFFERENT Tier-2 effect with the same argument shape",
            tier=ApprovalTier.TIER_2,
            input_schema={
                "type": "object",
                "properties": {
                    "recipient": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["recipient", "subject"],
            },
        ),
        handler=_handler,
    )


class _WrongToolAdapter:
    """Invokes a DIFFERENT registered tool with the approved call's identity."""

    def __init__(self, other_log: _EffectLog) -> None:
        self._other_log = other_log

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        continuation = context.continuation
        assert continuation is not None
        principal = Principal(id=context.run.owner_principal_id, groups=())
        with direct_publication_scope(
            workflow_execution_id=context.run.run_id,
            publication_attempt_id=context.attempt_id,
        ):
            await context.executor.execute(OTHER_TOOL, continuation.arguments, principal=principal)
            complete_run_direct_publication_observation()
        output = b"wrong tool executed"
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=_completed_envelope(output),
        )


def test_resumed_short_circuit_refuses_a_different_tool_with_identical_args(
    make_app: AppBuilder,
) -> None:
    """The approval names a TOOL, not just an argument hash.

    The continuation key pins (run, attempt, step, tool_call_id, args-hash) but
    not the tool name, and on the reviewed build the middleware short-circuit
    re-admitted on that key alone — so a different registered Tier-2 tool
    invoked with byte-identical arguments consumed the human's approval and
    executed. It must refuse.
    """

    log = _EffectLog()
    other_log = _EffectLog()
    h = make_app(
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        extra_tool_bindings=(_effect_binding(log), _other_binding(other_log)),
        extra_tool_policies={
            **_POLICIES,
            OTHER_TOOL: _POLICIES[EFFECT_TOOL],
        },
    )
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    _load_target(state, parked)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text

    async def _resume_checkpoint() -> None:
        joined = await checkpoints.get_join(parked.approval_id)
        await checkpoints.resume(
            parked.approval_id,
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            resumed_at=datetime.now(UTC),
        )

    run(_resume_checkpoint())

    service, engine = _build_service(state, h.runtime_url, _WrongToolAdapter(other_log))
    try:

        async def _execute() -> Any:
            with authenticated_request_scope(parked.owner_context):
                return await service.execute(_workflow_request(parked))

        # The EXACT refusal, not any exception: an unrelated audit, lease or
        # finalizer failure must not turn this security regression test green.
        with pytest.raises(
            ApprovalIntegrityError,
            match="resumed Run approval names a different tool",
        ):
            run(_execute())
    finally:
        engine.dispose()

    assert other_log.calls == [], (
        "a different tool with identical arguments must NEVER consume a resumed "
        "approval — the human approved a named action"
    )
    assert log.calls == []
    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    # The refusal precedes the consume fence: the continuation stays `resumed`
    # (its one permitted execution is NOT spent on a refused impostor call) and
    # the approval row is untouched.
    assert (approval, checkpoint) == ("approved", "resumed")
    # The workflow still terminalizes the attempt honestly instead of stranding.
    assert run_status == RunState.FAILED.value
    assert "ApprovalIntegrityError" in _failure_reason(h.runtime_url, parked.run_id)


def test_predeploy_drain_across_config_hash_boundary_uses_the_runs_pinned_hash(
    make_app: AppBuilder,
) -> None:
    """The backfilled receipt carries the RUN's hash, not the new process's.

    Sol cross-model review round 2 (P1): the drain took the calling process's
    ``state.config_hash`` — but the one population it exists for (parks written
    BEFORE the drain deployed, backfilled AFTER the restart) is exactly the
    population accepted under the OLD hash, and ``RunFinalizer`` rejects any
    receipt whose hash differs from the accepted Run. This is the live box's
    own upgrade shape: 4 unclaimed pre-deploy outbox rows, runbook says they
    self-drain on next touch. Repro: park under hash A with the intent
    undrained, "restart" under hash B, touch a decision route. The drain must
    write A; with A written, the expiry terminal composes (409, run FAILED)
    instead of the finalizer refusing the mismatched receipt.
    """

    log = _EffectLog()
    h = _make(make_app, log)
    state = get_state(h.app)
    enqueuer = _RecordingEnqueuer()
    state.agent_run_enqueuer = enqueuer
    stamps = _admit_four_sources(state)
    parked = run(
        _park(state, h.runtime_url, stamps=stamps, owner_token_ttl_s=-60, park_drain=False)
    )
    _load_target(state, parked)
    assert _approval_receipts(h.runtime_url, parked) == 0, (
        "precondition: the pre-deploy park has no mandatory approval receipt"
    )
    pinned = parked.config_hash
    state.config_hash = "b" * 64  # the restarted process's NEW frozen hash
    assert state.config_hash != pinned

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 409, (
        "the expired pre-deploy park must terminalize across the config-hash "
        f"boundary (409), got {decided.status_code}: {decided.text}"
    )

    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.connect() as conn:
            receipt_hashes = [
                str(value)
                for value in conn.execute(
                    sa.select(ne_agent_run_audit_receipts.c.config_hash).where(
                        ne_agent_run_audit_receipts.c.run_id == parked.run_id,
                        ne_agent_run_audit_receipts.c.attempt_id == parked.attempt_id,
                        ne_agent_run_audit_receipts.c.approval_id == parked.approval_id,
                        ne_agent_run_audit_receipts.c.decision_kind == "approval",
                    )
                ).scalars()
            ]
    finally:
        engine.dispose()
    assert receipt_hashes == [pinned], (
        "the backfilled approval receipt must carry the run's pinned config hash "
        f"{pinned[:8]}…, got {[value[:8] for value in receipt_hashes]}"
    )
    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    assert (approval, checkpoint) == ("expired", "expired")
    assert run_status == RunState.FAILED.value
    assert enqueuer.requests == []
    assert log.calls == []


def test_post_decision_row_tamper_cannot_substitute_a_different_tool_at_resume(
    make_app: AppBuilder,
) -> None:
    """The tampered-row substitution is refused at resume (Sol slice 2, P1).

    The immutable join stores the park-time action, but the resume loaded the
    action from the MUTABLE ``ne_approval_requests`` row and the middleware
    compared the requested effect against that same row — both sides of the
    comparison moved together under a post-decision
    ``UPDATE ne_approval_requests SET action = B``, so a different registered
    tool with the same argument schema executed under the human's approval of
    A (the earlier wrong-tool test only made the ADAPTER disagree with an
    unchanged row, which could not catch this). Now the workflow verifies the
    mutable row against the join and executes the JOIN's action; drift refuses
    loudly, nothing executes, the continuation's one permitted execution is
    not spent, and the run fails instead of stranding.
    """

    log = _EffectLog()
    other_log = _EffectLog()
    h = make_app(
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        extra_tool_bindings=(_effect_binding(log), _other_binding(other_log)),
        extra_tool_policies={
            **_POLICIES,
            OTHER_TOOL: _POLICIES[EFFECT_TOOL],
        },
    )
    state = get_state(h.app)
    state.agent_run_enqueuer = _RecordingEnqueuer()
    stamps = _admit_four_sources(state)
    parked = run(_park(state, h.runtime_url, stamps=stamps))
    _load_target(state, parked)
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]

    decided = run(_approve_over_http(h, parked.approval_id))
    assert decided.status_code == 200, decided.text

    # THE TAMPER: after the human approved EFFECT_TOOL, the mutable request row
    # is rewritten to a different registered tool with the same argument schema.
    engine = sa.create_engine(h.runtime_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == parked.approval_id)
                .values(action=OTHER_TOOL)
            )
    finally:
        engine.dispose()

    async def _resume_checkpoint() -> None:
        joined = await checkpoints.get_join(parked.approval_id)
        await checkpoints.resume(
            parked.approval_id,
            expected_action_hash=joined.action_hash,
            principal_id=joined.principal_id,
            policy_hash=joined.policy_hash,
            resumed_at=datetime.now(UTC),
        )

    run(_resume_checkpoint())

    adapter = _AgenticLoopAdapter(log)
    service, engine = _build_service(state, h.runtime_url, adapter)
    try:

        async def _execute() -> Any:
            with authenticated_request_scope(parked.owner_context):
                return await service.execute(_workflow_request(parked))

        # The EXACT parked-governance refusal — an unrelated failure must not
        # turn this security regression test green.
        with pytest.raises(LookupError, match="governance metadata drifted"):
            run(_execute())
    finally:
        engine.dispose()

    assert other_log.calls == [], (
        "a post-decision row tamper must NEVER execute a different tool under the human's approval"
    )
    assert log.calls == [], "the original tool must not run either — the drift refuses the resume"
    approval, checkpoint, run_status = _statuses(h.runtime_url, parked.approval_id, parked.run_id)
    # The refusal precedes the consume fence: the continuation stays `resumed`.
    assert (approval, checkpoint) == ("approved", "resumed")
    assert run_status == RunState.FAILED.value
