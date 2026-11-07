# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The durable chat-turn workflow (NE-103c §5.1) — serializable args, house pattern.

Module-level functions with serializable args/returns; governed resources come from
the worker's process-global composition root (``runtime_state``), never from args —
the ``approvals_durable.py`` precedent. The decorators (``@ne_step`` / ``@ne_workflow``)
are transparent passthroughs under ``durability='off'`` and the real
``DBOS.step`` / ``DBOS.workflow`` under ``durability='dbos'`` (applied at DECORATION
time — importing this module registers the workflow).

Recovery semantics (design §5.2), stated honestly: ``execute_turn_step`` is ONE step
wrapping the whole pydantic-ai loop, so a worker restart mid-turn re-runs the turn
FROM THE TOP (not from the last token). Session history is safe (``append_turn`` runs
once, after success). A re-run that had already obtained a tier-2/3 approval submits a
FRESH approval request — the human may be asked twice; the direction is fail-SAFE
(never auto-approved, never skipped).

Frame semantics (NE-TURN-PROGRESS): the step ALSO streams the turn's REAL governed
tool-call transits as ``tool`` frames WHILE the turn runs — the same
:class:`ne.api.tool_transit.ToolTransitObservingSink` observation the inline chat
route uses (the worker's platform sink is built by the SAME ``build_platform``), so
enqueue-mode readers see per-hop progress instead of silence. Step-context stream
writes are AT-LEAST-ONCE (``DBOS.write_stream`` documents step writes; a re-run step
re-emits its frames from seq 0 and the relay's monotonic dedup drops the overlap).
The terminal ``token``/``done`` pair is still written from WORKFLOW context
(exactly-once), numbered AFTER the step's tool frames via
``TurnStepResult.frames_emitted``. The raised-turn ``error`` frame keeps seq 0 —
the relay delivers terminals regardless of seq (``relay_event_stream``), so a turn
that dies after N tool frames still shows its failure instead of freezing.

Write-back idempotency (NE-ACTION-DETERMINISTIC-REQID): the step re-establishes
``action_scope(req.turn_id)`` around the session run, so a re-run turn's re-prompted
model dedups any NE-050 ActionTool write it re-issues with the same payload — with NO
model-chosen id required. Payload DRIFT on re-prompt (the re-run model rephrasing the
write) still yields a different derived key and can reach a second human approval +
write (the ``ne103c`` §10-Q3 residual — out of scope here). Cron ticks (``main.py``)
establish NO action scope in v1 — cron agents have no write actions today; a
cron-lane scope is future work if that changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.api.citation_capture import citation_capture_scope
from ne.api.tool_transit import TURN_TOOL_TRANSITS, TurnTransitChannel
from ne.api.tools import TURN_PRINCIPAL
from ne.api.turn_wire import (
    PROTECTED_TRANSPORT_GENERATION,
    LegacyTurnRequest,
    ProtectedTerminalPayload,
    ProtectedTurnRef,
    TurnRequest,
    TurnStepResult,
    citations_frame,
    done_frame,
    error_frame,
    token_frame,
    tool_frame,
)
from ne.runtime.correlation import action_scope, run_trace_scope
from ne.runtime.durable import ne_step, ne_workflow
from ne.runtime.factory import rehydrate_department_session
from ne.runtime.private_payloads import (
    CONVERSATION_TURN_TARGET_KIND,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    conversation_turn_content_binding,
)
from ne.runtime.run_checkpoint import RunApprovalResumeRequest
from ne.runtime.run_runner import RunExecutionAdapter
from ne.runtime.run_store import ProtectedPayloadRef, ProtectedTurnUnavailable, RunState
from ne.runtime.run_workflow import (
    RunTerminalPublication,
    RunWorkflowRequest,
    RunWorkflowResult,
    RunWorkflowService,
    RunWorkflowStateStore,
)
from ne.runtime.source_results import AuthorizationExpiredError, authenticated_request_scope
from ne.runtime.store import AttachmentRequestMismatchError
from ne.worker.relay import TurnFrameWriter
from ne.worker.run_execution import build_profile_bound_run_execution_adapter
from ne.worker.runtime_state import frame_writer, worker_platform

_logger = logging.getLogger(__name__)

# Test-only override; production derives the adapter from the worker's exact shared
# Platform so API/worker profile identity cannot fork.
_run_execution_adapter: RunExecutionAdapter | None = None


@ne_step(name="run.approval.resume")
async def resume_run_approval_step(
    approval_id: str,
    action_hash: str,
    principal_id: str,
    policy_hash: str,
    resume_request_json: str | None = None,
) -> str:
    """Reauthorize and resume one exact persisted approval-id continuation.

    No event id, latest-row query or process observer participates. An admitted
    permanent denial terminalizes through the coordinator; an indeterminate
    authority result raises and leaves the continuation pending for retry.
    """
    platform = worker_platform()
    checkpoints = getattr(platform.gate, "_run_approval_checkpoints", None)
    if checkpoints is None or platform.source_guard is None:
        raise RuntimeError("Run approval continuation is unavailable")
    joined = await checkpoints.get_join(approval_id)
    if joined is None:
        raise RuntimeError("Run approval continuation is unavailable")
    if (
        joined.action_hash != action_hash
        or joined.principal_id != principal_id
        or joined.policy_hash != policy_hash
    ):
        raise RuntimeError("Run approval continuation binding changed")
    resume_request: RunApprovalResumeRequest | None = None
    if resume_request_json is not None:
        resume_request = await checkpoints.require_resume_request(
            RunApprovalResumeRequest.model_validate_json(resume_request_json)
        )
    if platform.agent_run_store is None:
        raise RuntimeError("canonical Run storage is unavailable in the worker")
    run_store = platform.agent_run_store
    workflow_request: RunWorkflowRequest | None = None
    if resume_request is not None:
        workflow_request = await run_store.load_workflow_request(
            enqueue_id=resume_request.dispatch_id,
            run_id=resume_request.run_id,
            attempt_id=resume_request.attempt_id,
            owner_principal_id=resume_request.owner_principal_id,
        )
    lifecycle = RunWorkflowStateStore(platform.runtime_engine, run_store=run_store)

    def reconcile() -> RunTerminalPublication | None:
        return lifecycle.reconcile_approval_join(
            approval_id=approval_id,
            action_hash=action_hash,
            principal_id=principal_id,
            policy_hash=policy_hash,
        )

    async def publish_terminal(terminal: RunTerminalPublication) -> str:
        await _run_workflow_service().publish_terminal(terminal)
        return terminal.run_id

    async def fenced_status() -> RunState | None:
        try:
            return lifecycle.approval_resume_fence(
                run_id=joined.run_id, attempt_id=joined.attempt_id
            )
        except RuntimeError:
            raced_terminal = reconcile()
            if raced_terminal is not None:
                await publish_terminal(raced_terminal)
                return None
            raise

    async def terminalize_authorization_expired() -> str:
        """The specification's fail-loud half — never strand, never revive.

        The run owner's bound snapshot is at or past its verified expiry. The
        platform discloses and executes NOTHING (that part always held); what was
        missing is this: the parked approval/checkpoint is made non-actionable and
        the attempt terminally FAILS with safe reason ``authorization_expired``,
        atomically, instead of the error being raised into the void while the run
        hangs in ``running`` with the approval already consumed.

        Re-authenticating as the service identity, treating expiry as non-fatal
        because the approver is fresh, and refreshing/rebinding the stored context
        are all RED and none of them happens here.
        """

        await checkpoints.terminalize_authorization_expired(
            approval_id, decided_at=datetime.now(UTC)
        )
        expired_terminal = reconcile()
        if expired_terminal is not None:
            return await publish_terminal(expired_terminal)
        raise RuntimeError("expired Run approval continuation could not be terminalized")

    async def execute_resumed() -> str:
        if workflow_request is None:
            return checkpoint.run_id
        try:
            with authenticated_request_scope(workflow_request.authenticated_context):
                result = await _run_workflow_service().execute(workflow_request)
        except AuthorizationExpiredError:
            return await terminalize_authorization_expired()
        return result.model_dump_json()

    # BACKFILL THE MANDATORY APPROVAL RECEIPT before ANY terminal can be
    # attempted.  `reconcile()`, expiry and withholding all compose terminals
    # through `RunFinalizer._observed_approvals`, which refuses a Run whose
    # park predates the park-leg drain ("approval join lacks its exact
    # mandatory audit receipt").  Idempotent, epoch-stable address.
    await _run_workflow_service().record_approval_audit_receipt(
        run_id=joined.run_id,
        attempt_id=joined.attempt_id,
        owner_principal_id=joined.principal_id,
        approval_id=approval_id,
    )
    terminal = reconcile()
    if terminal is not None:
        return await publish_terminal(terminal)
    checkpoint = await checkpoints.load_checkpoint(
        approval_id,
        expected_action_hash=action_hash,
        principal_id=principal_id,
        policy_hash=policy_hash,
    )
    state = await fenced_status()
    if state is None:
        return checkpoint.run_id
    if state is RunState.RUNNING and checkpoint.status not in {"resumed", "consumed"}:
        raise RuntimeError("Run approval continuation has no exact resumed checkpoint")
    if state is RunState.WAITING_APPROVAL and checkpoint.status != "approved":
        raise RuntimeError("Run approval continuation is not approved for resume")
    if checkpoint.status == "consumed":
        # The continuation's ONE permitted execution already crossed its
        # durable fence.  A replay SETTLES: an existing terminal publication
        # replays, and an uncertain one terminalizes
        # `RunEffectSettlementUncertain` inside `execute_resumed` — it never
        # re-authorizes and never re-issues the effect.
        terminal = reconcile()
        if terminal is not None:
            return await publish_terminal(terminal)
        return await execute_resumed()
    try:
        if workflow_request is None:
            authorized = await checkpoints.authorize(
                approval_id,
                guard=platform.source_guard,
                operation="resume",
                decided_at=datetime.now(UTC),
            )
        else:
            with authenticated_request_scope(workflow_request.authenticated_context):
                authorized = await checkpoints.authorize(
                    approval_id,
                    guard=platform.source_guard,
                    operation="resume",
                    decided_at=datetime.now(UTC),
                )
    except AuthorizationExpiredError:
        return await terminalize_authorization_expired()
    if authorized is None:
        terminal = reconcile()
        if terminal is not None:
            return await publish_terminal(terminal)
        raise RuntimeError("Run approval continuation was permanently withheld")
    if state is RunState.RUNNING:
        terminal = reconcile()
        if terminal is not None:
            return await publish_terminal(terminal)
        replay_state = await fenced_status()
        if replay_state is None:
            return checkpoint.run_id
        if replay_state is not RunState.RUNNING:
            raise RuntimeError("Run approval continuation did not remain resumed")
        return await execute_resumed()
    try:
        checkpoint = await checkpoints.resume(
            approval_id,
            expected_action_hash=action_hash,
            principal_id=principal_id,
            policy_hash=policy_hash,
            resumed_at=datetime.now(UTC),
        )
    except LookupError:
        terminal = reconcile()
        if terminal is not None:
            return await publish_terminal(terminal)
        replay = await checkpoints.load_checkpoint(
            approval_id,
            expected_action_hash=action_hash,
            principal_id=principal_id,
            policy_hash=policy_hash,
        )
        replay_state = await fenced_status()
        if replay_state is None:
            return replay.run_id
        if replay.status in {"resumed", "consumed"} and replay_state is RunState.RUNNING:
            return await execute_resumed()
        raise
    terminal = reconcile()
    if terminal is not None:
        return await publish_terminal(terminal)
    resumed_state = await fenced_status()
    if resumed_state is None:
        return checkpoint.run_id
    if resumed_state is not RunState.RUNNING:
        raise RuntimeError("Run approval continuation did not resume")
    return await execute_resumed()


@ne_workflow(name="agent.run.approval.resume")
async def run_approval_resume_workflow(request_json: str) -> str:
    """Consume one exact decision intent and execute its durable resume step."""

    request = RunApprovalResumeRequest.model_validate_json(request_json)
    return await resume_run_approval_step(
        request.approval_id,
        request.action_hash,
        request.owner_principal_id,
        request.policy_hash,
        request.model_dump_json(),
    )


def set_run_execution_adapter(adapter: RunExecutionAdapter) -> None:
    """Install 010D's bounded adapter before DBOS launch (also the test seam)."""

    global _run_execution_adapter
    _run_execution_adapter = adapter


def _run_workflow_service() -> RunWorkflowService:
    platform = worker_platform()
    if platform.agent_run_store is None:
        raise RuntimeError("canonical Run storage is unavailable in the worker")
    return RunWorkflowService(
        state=RunWorkflowStateStore(platform.runtime_engine, run_store=platform.agent_run_store),
        run_store=platform.agent_run_store,
        sink=platform.sink,
        audit_engine=platform.audit_engine,
        gateway=platform.gateway,
        executor=platform.executor,
        adapter=(
            _run_execution_adapter
            if _run_execution_adapter is not None
            else build_profile_bound_run_execution_adapter(platform)
        ),
        config_hash=platform.config_hash,
        # The approval-continuation coordinator and the source-authority guard:
        # an approval RESUME executes the pinned effect these two name, instead
        # of replaying the agent loop through the model.
        checkpoints=getattr(platform.gate, "_run_approval_checkpoints", None),
        source_guard=platform.source_guard,
    )


class _ProtectedTurnStepResult(BaseModel):
    """Content-free result crossing the durable step/workflow boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protected_terminal: ProtectedTurnRef
    attempt_id: str
    frames_emitted: int


def _turn_request(request_json: str) -> TurnRequest | LegacyTurnRequest:
    """Decode one of the disjoint protected or rollback-compatible wires."""
    try:
        return TurnRequest.model_validate_json(request_json)
    except ValidationError as protected_error:
        try:
            return LegacyTurnRequest.model_validate_json(request_json)
        except ValidationError:
            raise protected_error from None


def _protected_ref(ref: ProtectedPayloadRef, *, status: ProtectedContentStatus) -> ProtectedTurnRef:
    return ProtectedTurnRef(
        protected_ref=ref.row_id,
        protected_hash=ref.plaintext_hash,
        content_status=status.value,
    )


def _admit_protected_request(req: TurnRequest) -> None:
    """Refuse stale/foreign protected work before materialization or execution."""

    platform = worker_platform()
    if (
        req.transport_generation != PROTECTED_TRANSPORT_GENERATION
        or platform.agent_transport_generation != PROTECTED_TRANSPORT_GENERATION
        or platform.agent_run_store is None
    ):
        raise ProtectedTurnUnavailable
    context = req.authenticated_context
    if (
        context.principal_id != req.principal_id
        or context.department != req.department
        or tuple(context.authorization_groups) != tuple(sorted(set(req.principal_groups)))
        or req.protected_input.content_status != ProtectedContentStatus.SOURCE_FREE.value
        or context.expires_at <= int(time.time())
    ):
        raise ProtectedTurnUnavailable
    # The composed guard owns the fail-closed verified-expiry check. It is invoked
    # before the protected store is asked for bytes and under the exact serialized
    # verifier-authored context below.
    if platform.source_guard is None:
        raise ProtectedTurnUnavailable
    platform.source_guard.authorization()


async def _materialize_input(req: TurnRequest) -> str:
    platform = worker_platform()
    assert platform.agent_run_store is not None
    try:
        prompt = await platform.agent_run_store.materialize_legacy_conversation_turn(
            owner_principal_id=req.principal_id,
            conversation_id=req.session_id,
            attachment_id=req.turn_id,
            attempt_id=req.claimed_attempt_id,
            slot="input",
            protected_ref=req.protected_input.protected_ref,
            plaintext_hash=req.protected_input.protected_hash,
        )
        return prompt.decode("utf-8")
    except (ProtectedTurnUnavailable, UnicodeDecodeError) as exc:
        raise ProtectedTurnUnavailable from exc


async def _protect_terminal(
    req: TurnRequest, *, terminal: TurnStepResult, attempt_id: str
) -> ProtectedTurnRef:
    platform = worker_platform()
    assert platform.agent_run_store is not None
    ref = await platform.agent_run_store.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=req.principal_id,
        target_kind=CONVERSATION_TURN_TARGET_KIND,
        target_binding=conversation_turn_content_binding(
            conversation_id=req.session_id,
            attachment_id=req.turn_id,
            attempt_id=attempt_id,
            slot="terminal",
        ),
        content_status=ProtectedContentStatus.SOURCE_FREE,
        plaintext=ProtectedTerminalPayload(
            output=terminal.output,
            citations=tuple(terminal.citations),
            verified=terminal.verified,
            grounded=terminal.grounded,
        )
        .model_dump_json()
        .encode("utf-8"),
    )
    return _protected_ref(ref, status=ProtectedContentStatus.SOURCE_FREE)


async def _protected_history(req: TurnRequest, limit: int) -> list[ModelMessage]:
    """Materialize the bounded owned history without consulting erased columns."""

    platform = worker_platform()
    assert platform.agent_run_store is not None
    page = await platform.session_store.protected_conversation_ledger_page(
        req.session_id,
        req.principal_id,
        req.department,
        req.agent_name,
        limit=limit,
    )
    messages: list[ModelMessage] = []
    for turn in reversed(page.turns):
        if turn.input.status != "source_free" or turn.terminal.status != "source_free":
            raise ProtectedTurnUnavailable
        try:
            input_payload = await platform.agent_run_store.materialize_legacy_conversation_turn(
                owner_principal_id=req.principal_id,
                conversation_id=req.session_id,
                attachment_id=turn.attachment_id,
                attempt_id=turn.attempt_id,
                slot="input",
                protected_ref=turn.input.protected_ref,
                plaintext_hash=turn.input.plaintext_hash,
            )
            terminal_payload = await platform.agent_run_store.materialize_legacy_conversation_turn(
                owner_principal_id=req.principal_id,
                conversation_id=req.session_id,
                attachment_id=turn.attachment_id,
                attempt_id=turn.attempt_id,
                slot="terminal",
                protected_ref=turn.terminal.protected_ref,
                plaintext_hash=turn.terminal.plaintext_hash,
            )
            prompt = input_payload.decode("utf-8")
            terminal = ProtectedTerminalPayload.model_validate_json(terminal_payload)
        except (ProtectedTurnUnavailable, UnicodeDecodeError, ValidationError) as exc:
            raise ProtectedTurnUnavailable from exc
        messages.extend(
            (
                ModelRequest(parts=[UserPromptPart(content=prompt)]),
                ModelResponse(parts=[TextPart(content=terminal.output)], model_name="protected"),
            )
        )
    return messages


class _TransitFrameDrain:
    """Drain the turn's transit channel onto the frame stream AS TRANSITS HAPPEN.

    One consumer task awaits the channel queue and emits each REAL C6 transit as a
    ``tool`` frame with the next seq (0-based). Emission failures are CONTAINED per
    item (a progress frame must never kill a governed turn — the seq is still
    consumed so numbering stays monotonic and the relay simply sees a gap, which
    its dedup tolerates). ``task_done`` is ALWAYS called, so :meth:`flush` can
    ``queue.join()`` without deadlocking on a contained failure.

    ``seqs_used`` is the count of seqs consumed (emitted + contained failures) —
    the workflow numbers ``token``/``done`` after it via
    ``TurnStepResult.frames_emitted``.
    """

    def __init__(self, channel: TurnTransitChannel, writer: TurnFrameWriter) -> None:
        self._channel = channel
        self._writer = writer
        self.seqs_used = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            transit = await self._channel.queue.get()
            try:
                await self._writer.emit(
                    tool_frame(
                        self.seqs_used,
                        name=transit.name,
                        tier=transit.tier,
                        decision=transit.decision,
                        approval_id=transit.approval_id,
                        audit_seq=transit.audit_seq,
                        reason=transit.reason,
                    )
                )
            except Exception:
                # Contained: a frame-stream hiccup must not fail the turn. The
                # reader misses one progress frame; the audit chain (the record
                # of truth) is untouched — it was written before the observer ran.
                _logger.warning("tool-frame emit failed; turn continues", exc_info=True)
            finally:
                self.seqs_used += 1
                self._channel.queue.task_done()

    async def flush(self) -> None:
        """Emit every already-published transit, then stop the consumer.

        Called AFTER the turn settled: the observing sink runs inside the turn's
        own await chain, so nothing publishes after ``session.run`` returns.
        ``queue.join()`` waits for in-flight emits (``task_done`` runs even on a
        contained failure); the consumer is then cancelled while idle. The
        ``finally`` guarantees the consumer task never outlives the step even
        when flush itself is cancelled (a turn-timeout cancellation arriving
        mid-flush) — the caller's cancellation then propagates normally.
        """
        if self._task is None:
            return
        task, self._task = self._task, None
        try:
            if not task.done():
                await self._channel.queue.join()
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


@ne_step(name="chat.turn.execute")
async def execute_turn_step(request_json: str) -> str:
    """Execute the governed department turn (the C6 chain + fail-closed re-check).

    Rebuilds the ``Principal`` from the serialized request and binds it to
    :data:`TURN_PRINCIPAL` (WITHOUT this every knowledge-tool call fails closed);
    resets it in ``finally``. The API-minted principal-owned conversation is
    rehydrated by exact id/owner/department/agent — asserted for defense. ``run``
    carries the fail-closed ownership re-check + audit-kills-run behavior along.

    NE-TURN-PROGRESS: binds :data:`TURN_TOOL_TRANSITS` around the run (mirroring the
    inline chat route) so the platform's ``ToolTransitObservingSink`` publishes each
    REAL governed transit, and a drain task streams them as ``tool`` frames while
    the turn is still running — per-hop progress, never fabricated: a frame exists
    only because the corresponding audit event was appended to the chain first.

    The step returns the serialized :class:`TurnStepResult` (NE-VAF-U4/NE-GA-G5) —
    output plus structural verified/grounded provenance — so the structural
    provenance survives the DBOS step boundary (enqueue==inline provenance parity).
    ``frames_emitted`` carries the tool-frame seq count to the workflow.
    """
    req = _turn_request(request_json)
    protected = isinstance(req, TurnRequest)
    platform = worker_platform()
    if not protected and platform.agent_transport_generation == PROTECTED_TRANSPORT_GENERATION:
        raise ProtectedTurnUnavailable
    principal = req.to_principal()
    channel = TurnTransitChannel()
    drain = _TransitFrameDrain(channel, frame_writer(req.turn_id))
    session = None
    prepared = False
    request_scope = (
        authenticated_request_scope(req.authenticated_context)
        if protected
        else contextlib.nullcontext()
    )
    with request_scope:
        if protected:
            _admit_protected_request(req)
            prior = await platform.session_store.protected_conversation_turn(
                req.session_id,
                req.principal_id,
                req.department,
                req.agent_name,
                req.turn_id,
                req.claimed_attempt_id,
            )
            if prior is not None:
                return _ProtectedTurnStepResult(
                    protected_terminal=ProtectedTurnRef(
                        protected_ref=prior.terminal.protected_ref,
                        protected_hash=prior.terminal.plaintext_hash,
                        content_status=prior.terminal.status,
                    ),
                    attempt_id=prior.attempt_id,
                    frames_emitted=0,
                ).model_dump_json()
            prompt = await _materialize_input(req)
        else:
            prompt = req.prompt
        token = TURN_PRINCIPAL.set(principal)
        transit_token = TURN_TOOL_TRANSITS.set(channel)
        drain.start()
        try:
            # NE-GA-G5 (Codex P1 fix): mirror routers/chat.py:_open EXACTLY — a
            # grounded-analyst turn must never be short-circuited by the
            # NE-VAF-U4 verified_router pre-step (which returns before the model
            # — and therefore before grounding — ever runs), or GroundedProvenance
            # would silently go missing for routed prompts (enqueue leg too).
            is_grounded_agent = req.agent_name == platform.grounded_agent_name
            if req.agent_name not in platform.api_settings.chat_agents:
                raise ValueError("unknown configured chat agent")
            session = await rehydrate_department_session(
                req.session_id,
                req.agent_name,
                req.department,
                owner_principal_id=principal.id,
                store=platform.session_store,
                gateway=platform.gateway,
                executor=platform.executor,
                settings=platform.runtime_settings,
                router=None if is_grounded_agent else platform.verified_router,
                grounding=platform.grounding if is_grounded_agent else None,
                sink=platform.sink,
                history_loader=(
                    (lambda limit: _protected_history(cast(TurnRequest, req), limit))
                    if protected
                    else None
                ),
            )
            if session is None:
                raise ValueError("unknown conversation")
            trace_scope = run_trace_scope(req.trace_id)
            with citation_capture_scope() as citations, action_scope(req.turn_id), trace_scope:
                result = await session.run_claimed(
                    prompt,
                    principal=principal,
                    requested_model=req.requested_model,
                    requested_model_lane=req.requested_model_lane,
                    attachment_id=req.turn_id,
                    request_fingerprint=req.request_fingerprint,
                    idempotency_fingerprint=req.idempotency_fingerprint,
                    claimed_attempt_id=req.claimed_attempt_id,
                    served_citations=citations.snapshot,
                )
                citations.restore(session.take_replay_citations(req.turn_id))
                prepared = True
            # Flush BEFORE snapshotting ``seqs_used``: a tool-frame write still in
            # flight when the turn settles (a loaded DBOS system DB, or a routed
            # verified turn that returns straight after its last governed read)
            # would otherwise consume its seq AFTER the snapshot — colliding with
            # the terminal ``token`` frame, which BOTH relay dedup sites drop as a
            # non-terminal duplicate.
            #
            # WHAT THAT COSTS, stated exactly: the dropped frame, not the answer. In
            # v1 turn-then-stream the terminal ``done`` carries the same full
            # ``output`` and is delivered regardless of seq, and the console renders
            # it (``text: event.output || m.text``), so the reader still sees the
            # text — the ``done.output`` self-heal the relay contract already names.
            # The ordering is enforced anyway because the frame stream has to be
            # right on its own terms: under any mode whose terminal does NOT carry
            # the whole output — real incremental token deltas — this collision
            # WOULD lose answer text, and a frame dropped here is a defect at the
            # seam that numbers it, not at the one that happens to compensate.
            #
            # After this await every transit the turn published has consumed its seq
            # (``flush`` joins the queue; ``seqs_used`` increments before
            # ``task_done``), so the terminal pair is numbered strictly after every
            # tool frame on EVERY successful exit.
            await drain.flush()
            attempt_id = session.prepared_attempt_id(req.turn_id) or req.claimed_attempt_id
            terminal = TurnStepResult(
                output=result.output,
                verified=result.verified,
                grounded=result.grounded,
                citations=citations.snapshot(),
                attempt_id=attempt_id,
                frames_emitted=drain.seqs_used,
            )
            if protected:
                protected_terminal = await _protect_terminal(
                    req, terminal=terminal, attempt_id=attempt_id
                )
                assert platform.agent_run_store is not None
                await platform.agent_run_store.migrate_conversation_turn_to_protected_refs(
                    conversation_id=req.session_id,
                    owner_principal_id=req.principal_id,
                    run_id=None,
                    attachment_id=req.turn_id,
                    attempt_id=attempt_id,
                    input_status=req.protected_input.content_status,
                    protected_input_ref=req.protected_input.protected_ref,
                    protected_input_hash=req.protected_input.protected_hash,
                    terminal_status=protected_terminal.content_status,
                    protected_terminal_ref=protected_terminal.protected_ref,
                    protected_terminal_hash=protected_terminal.protected_hash,
                )
                await platform.session_store.publish_conversation_turn(
                    req.session_id,
                    req.principal_id,
                    req.turn_id,
                    attempt_id,
                    [],
                    citation_payloads=[],
                )
                return _ProtectedTurnStepResult(
                    protected_terminal=protected_terminal,
                    attempt_id=attempt_id,
                    frames_emitted=drain.seqs_used,
                ).model_dump_json()
            return terminal.model_dump_json()
        finally:
            if session is not None and not prepared:
                with contextlib.suppress(LookupError):
                    await session.abort_turn(req.turn_id)
            await drain.flush()
            TURN_TOOL_TRANSITS.reset(transit_token)
            TURN_PRINCIPAL.reset(token)


@ne_workflow(name="chat.turn")
async def run_turn_workflow(request_json: str) -> str:
    """Run the turn step, then emit the terminal frame pair (v1 turn-then-stream).

    Terminal frames are written from workflow context (exactly-once), numbered
    AFTER the step's tool frames (``token`` at ``frames_emitted``, ``done`` after
    it) so the relay's monotonic dedup never drops them. A raised turn becomes one
    terminal ``error`` frame (safe detail) AND re-raises so DBOS records the
    failure — its seq is 0 because a raised step cannot report how many tool
    frames it wrote; the relay delivers ``done``/``error`` regardless of seq.
    """
    req = _turn_request(request_json)
    protected = isinstance(req, TurnRequest)
    writer = frame_writer(req.turn_id)
    terminal_published = False
    try:
        step_json = await execute_turn_step(request_json)
        # Decoding stays inside this try (NE-VAF-U4 fix): a workflow resumed by DBOS
        # across this upgrade holds a pre-U4 bare-string step result, which fails
        # TurnStepResult validation. That failure must still hit the terminal
        # error-frame + writer-close path below, not bypass it and hang the relay
        # client — so the decode is treated like any other turn failure.
        if protected:
            step = _ProtectedTurnStepResult.model_validate_json(step_json)
            terminal_published = True
            await writer.emit(
                done_frame(
                    step.frames_emitted,
                    req.session_id,
                    protected_terminal=step.protected_terminal,
                    message_id=req.turn_id,
                    trace_id=req.trace_id,
                )
            )
        else:
            step = TurnStepResult.model_validate_json(step_json)
            await writer.emit(token_frame(step.frames_emitted, step.output))
            citation_offset = 0
            if step.citations:
                await writer.emit(citations_frame(step.frames_emitted + 1, step.citations))
                citation_offset = 1
            if step.attempt_id is None:
                raise RuntimeError("worker turn completed without its claimed attempt")
            await worker_platform().session_store.publish_conversation_turn(
                req.session_id,
                req.principal_id,
                req.turn_id,
                step.attempt_id,
                [payload["chunk_id"] for payload in step.citations],
                citation_payloads=step.citations,
            )
            terminal_published = True
            await writer.emit(
                done_frame(
                    step.frames_emitted + 1 + citation_offset,
                    req.session_id,
                    step.output,
                    verified=step.verified,
                    grounded=step.grounded,
                    message_id=req.turn_id,
                    trace_id=(
                        req.trace_id
                        if req.requested_model is not None
                        or step.verified is not None
                        or step.grounded is not None
                        else None
                    ),
                )
            )
    except BaseException as exc:
        platform = worker_platform()
        if not isinstance(exc, AttachmentRequestMismatchError):
            with contextlib.suppress(LookupError):
                await platform.session_store.abort_conversation_turn(
                    req.session_id,
                    req.principal_id,
                    req.turn_id,
                    req.claimed_attempt_id,
                )
        await writer.emit(error_frame(0, exc))
        await writer.close()
        raise
    assert terminal_published
    await writer.close()
    return (
        step.protected_terminal.model_dump_json()
        if isinstance(step, _ProtectedTurnStepResult)
        else step.output
    )


@ne_step(name="agent.run.execute")
async def execute_agent_run_step(request_json: str) -> str:
    """Execute one content-free Run dispatch through the durable runtime service."""

    request = RunWorkflowRequest.model_validate_json(request_json)
    with authenticated_request_scope(request.authenticated_context):
        result = await _run_workflow_service().execute(request)
    return result.model_dump_json()


@ne_workflow(name="agent.run")
async def run_agent_run_workflow(request_json: str) -> str:
    """DBOS-owned canonical Run workflow with content-free input and result."""

    request = RunWorkflowRequest.model_validate_json(request_json)
    result = RunWorkflowResult.model_validate_json(await execute_agent_run_step(request_json))
    if result.run_id != request.run_id:
        raise RuntimeError("Run workflow result crossed a different durable target")
    return result.model_dump_json()
