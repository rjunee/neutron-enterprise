# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DepartmentAgentSession — one governed, principal-owned persistent conversation.

Structurally implements the frozen ``ne.contracts.runtime.AgentSession`` Protocol
(``session_id`` / ``agent_name`` properties, async ``run`` / ``close``). It wires
the three NE-033 pieces together:

  * the :class:`~ne.runtime.model.GatewayModel` (C3-clean model path),
  * per-run pydantic-ai tools whose ONLY body is a call to
    :meth:`GovernedToolExecutor.execute` (C6 — the session never reaches a handler
    directly; it can only go through the governed executor; the ToolSpec name is
    bound by CLOSURE and ``**kwargs`` is the tool function's only parameter, so no
    model-emitted argument key can shadow the internal carrier), PLUS a per-run
    ``on_unknown_tool`` hook wired into the :class:`GatewayModel` that routes a
    model-emitted UNREGISTERED tool call through the same governed executor (so an
    unknown name is audited + guardrail-checked + PDP-denied before pydantic-ai
    rejects it — closing the last C6 gap), and
  * the :class:`~ne.runtime.store.SqlAlchemySessionStore` for cross-turn state.

Owner and department scoping (fail closed): a principal whose id or
``claims['department']`` does not exactly match this conversation is rejected with
:class:`SessionDepartmentMismatchError` BEFORE any model or tool activity — no
cross-principal or cross-department conversation reuse.

Governance-blocked tool calls (``ToolDeniedError`` / ``GuardrailViolation`` / …)
are converted to a ``BLOCKED: …`` tool RESULT so the loop can continue and
terminate with a final answer — they were already audited by construction. An
audit-append failure (``RuntimeAuditError`` / ``AuditEmissionError``) is NOT
swallowed: it propagates and kills the run, because a call that cannot be audited
must never continue.

Verified-answer pre-step (NE-VAF-U3): an optional, keyword-only
:class:`~ne.runtime.verified_router.VerifiedRouter` runs AFTER ``load_history``
and BEFORE the model/tools are built. On a ROUTE/CLARIFY/REFUSE outcome the
model is never instantiated: the synthetic exchange is persisted via
``append_turn`` (an unforgeable ``ne-verified-router:*`` marker on the
``ModelResponse``) and the turn returns immediately. On ``ROUTER_PASS`` (or
when ``router`` is left at its default ``None``) the pre-existing model loop
below runs completely unchanged.

Grounded-agent POST-step (NE-GA-G3): an optional, keyword-only
:class:`~ne.runtime.grounding.GroundingPolicy` runs AFTER the model produces
its draft and BEFORE that draft is persisted or returned — the symmetric
mirror of the verified-answer PRE-step above. A fresh
:class:`~ne.runtime.grounding.GroundingRecorder` wraps this session's executor
for the DURATION OF THIS run() CALL ONLY (a prior turn's fetches must never
ground this turn — design §7.6/§3); :func:`~ne.runtime.grounding.run_grounding_gate`
then certifies the draft against the recorder's captured fetches. On PASS the
model's ``result.new_messages()`` are persisted UNCHANGED. On FAIL the draft
is WITHHELD ENTIRELY: only a synthetic fallback exchange (stamped with the
unforgeable ``ne-grounding-gate:fallback`` marker) is persisted, so no
fragment of the ungrounded draft can ever launder into a later turn's
history. When ``grounding`` is left at its default ``None`` this path is
BYTE-IDENTICAL to the pre-existing model loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

from ne.contracts.governance import AuditSink
from ne.contracts.models import ModelGateway
from ne.contracts.runtime import AgentRunResult, GroundedProvenance, ToolSpec, VerifiedProvenance
from ne.contracts.types import Lane, Principal
from ne.events import new_audit_event
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import (
    ConversationAttemptBinding,
    conversation_attempt_scope,
    current_run_trace_id,
    run_principal_scope,
    run_requested_model_scope,
    run_trace_scope,
)
from ne.runtime.errors import RuntimeAuditError, SessionDepartmentMismatchError
from ne.runtime.grounding import (
    FALLBACK_MARKER,
    GroundingPolicy,
    GroundingRecorder,
    run_grounding_gate,
)
from ne.runtime.model import (
    PROTOCOL_RESIDUE_CALL_CORRECTION,
    GatewayModel,
    UnknownToolHook,
    is_malformed_tool_call_residue,
)
from ne.runtime.processes import ProcessRegistry
from ne.runtime.provisional import provisional_context_scope
from ne.runtime.runner import (
    BoundedAgenticAdapter,
    ToolExecutorLike,
    build_governed_tool,
    build_unknown_tool_hook,
)
from ne.runtime.source_results import ProtectedSourceResultGuard
from ne.runtime.store import (
    AttachmentRequestMismatchError,
    SqlAlchemySessionStore,
    conversation_request_fingerprint,
)
from ne.runtime.verified_router import RoutedTurn, VerifiedRouter
from ne.runtime.workflow_execution_profile import ExecutionStrategy, WorkflowExecutionProfile
from ne.schema import AuditOutcome

# The BLOCKING-error tuple and the tool-building closures now live in
# ne.runtime.runner (shared by the session, sub-agent, and cron loops); this
# session delegates to them so the C6 closure pattern exists in exactly one place.
__all__ = ["AgentExecutionBinding", "DepartmentAgentSession"]

#: The corrective feedback a malformed in-band tool_call emission earns
#: (ModelRetry). Static protocol text only — no model/user content (C12).
#: The shared call-shape correction plus THIS loop's final-answer sentence. The
#: tail is session-specific: ``output_type=str`` here, so plain text IS the valid
#: final answer. The structured agentic loop appends the opposite instruction.
_RESIDUE_RETRY_MSG = (
    f"{PROTOCOL_RESIDUE_CALL_CORRECTION} If you did not intend to "
    "call a tool, respond with your final answer as plain text with NO JSON."
)


def _install_protocol_residue_validator(agent: Agent[None, str]) -> None:
    """Register the output validator that turns protocol residue into a retry.

    Live-isolated 2026-08-04 (GPU demo lane, hybrid thinking ON): after a tool
    return the model intermittently emits a tool_call object WITHOUT its
    ``name`` key. ``GatewayModel._parse`` correctly rejects the CALL (the strict
    shape never widens) and — by the parse-never-raises design — demotes it to
    prose; without this validator that residue became the user-visible final
    answer AND persisted into session history, where later turns imitated it
    (observed 4/4 in the poisoned live session). The validator raises
    :class:`ModelRetry` with corrective protocol feedback instead, so the model
    gets one bounded chance to re-emit properly; exhausted retries surface
    pydantic-ai's loud ``UnexpectedModelBehavior`` — a governed refusal, never
    silent protocol residue as an answer. Registered on EVERY session Agent
    (grounded and plain) — the trigger predicate
    (:func:`ne.runtime.model.is_malformed_tool_call_residue`) is deliberately
    narrow, so legitimate plain-text answers are byte-identically unaffected.
    """

    @agent.output_validator
    def _reject_protocol_residue(output: str) -> str:
        if is_malformed_tool_call_residue(output):
            raise ModelRetry(_RESIDUE_RETRY_MSG)
        return output


@dataclass(frozen=True, slots=True)
class AgentExecutionBinding:
    """One immutable Agent version bound to exactly one executable strategy seam."""

    agent_id: str
    agent_version: int
    strategy: ExecutionStrategy
    workflow_profile: WorkflowExecutionProfile | None = None
    agentic_adapter: BoundedAgenticAdapter | None = None

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent id must not be empty")
        if isinstance(self.agent_version, bool) or self.agent_version < 1:
            raise ValueError("agent version must be positive")
        if not isinstance(self.strategy, ExecutionStrategy):
            raise TypeError("strategy must be a parsed ExecutionStrategy")
        if self.strategy is ExecutionStrategy.DETERMINISTIC:
            if self.workflow_profile is None or self.agentic_adapter is not None:
                raise ValueError(
                    "deterministic Agent binding requires exactly one WorkflowExecutionProfile"
                )
        elif self.workflow_profile is not None or self.agentic_adapter is None:
            raise ValueError("agentic Agent binding requires exactly one bounded agentic adapter")

    @property
    def profile_identity(self) -> tuple[str, int, str] | None:
        if self.workflow_profile is None:
            return None
        return (
            self.workflow_profile.workflow_id,
            self.workflow_profile.version,
            self.workflow_profile.config_hash,
        )


class DepartmentAgentSession:
    """A live, department-scoped agent session over the governed tool executor."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_name: str,
        department: str,
        owner_principal_id: str,
        store: SqlAlchemySessionStore,
        executor: ToolExecutorLike,
        gateway: ModelGateway,
        settings: RuntimeSettings,
        registry: ProcessRegistry | None = None,
        router: VerifiedRouter[Any] | None = None,
        grounding: GroundingPolicy | None = None,
        instructions: str | None = None,
        sink: AuditSink | None = None,
        source_guard: ProtectedSourceResultGuard | None = None,
        history_loader: Callable[[int], Awaitable[list[ModelMessage]]] | None = None,
    ) -> None:
        self._session_id = session_id
        self._agent_name = agent_name
        self._department = department
        self._owner_principal_id = owner_principal_id
        self._store = store
        self._executor = executor
        self._gateway = gateway
        self._settings = settings
        self._registry = registry
        self._router = router
        self._grounding = grounding
        # PRE-015A0-ENFORCEMENT: the protected-source delivery guard handed to BOTH
        # GatewayModel constructions below. None ⇒ byte-identical legacy behavior.
        self._source_guard = source_guard
        self._history_loader = history_loader
        # Per-rehydrated-session ownership of prepared turn attempts. The
        # internal nonce keeps a failed/concurrent retry from aborting another
        # request's row merely because both requests share an idempotency key.
        self._prepared_attempts: dict[str, str] = {}
        self._replay_citations: dict[str, tuple[dict[str, str], ...]] = {}
        # D-2 (NE-SKILL-1b): the terminal-event sink. None ⇒ byte-identical no-event
        # behavior (a session.run event is emitted only when a sink is wired). The
        # event durably records a completed interactive session so the Skill Forge
        # can fold it into a WorkflowSnapshot (ProcessRegistry is in-memory).
        self._sink = sink
        # The session instructions seam (design §10-Q1 / D-1 carrier): the skills
        # index (or any composed instruction) is passed to BOTH Agent() constructions
        # below. None ⇒ byte-identical to pre-unit behavior (pydantic-ai treats a
        # None instructions= as absent).
        self._instructions = instructions

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def department(self) -> str:
        return self._department

    @property
    def owner_principal_id(self) -> str:
        return self._owner_principal_id

    async def run(
        self,
        prompt: str,
        *,
        principal: Principal,
        context: Mapping[str, Any] | None = None,
        requested_model: str | None = None,
        requested_model_lane: Lane | None = None,
        attachment_id: str | None = None,
        request_fingerprint: str | None = None,
        idempotency_fingerprint: str | None = None,
        claimed_attempt_id: str | None = None,
        served_citations: Callable[[], list[dict[str, str]]] | None = None,
    ) -> AgentRunResult:
        declared = principal.claims.get("department")
        if declared != self._department:
            raise SessionDepartmentMismatchError(
                f"principal department {declared!r} does not match session department "
                f"{self._department!r}; cross-department session reuse is forbidden"
            )
        if principal.id != self._owner_principal_id:
            raise SessionDepartmentMismatchError(
                "principal does not own this conversation; conversation reuse is forbidden"
            )
        turn_attachment_id = attachment_id or f"turn-{uuid.uuid4()}"
        claim_trace_id = current_run_trace_id() or uuid.uuid4().hex
        fingerprint = conversation_request_fingerprint(
            self._session_id,
            self._owner_principal_id,
            self._department,
            self._agent_name,
            prompt,
            requested_model,
            None if requested_model_lane is None else requested_model_lane.value,
            claim_trace_id,
            principal.groups,
            dict(principal.claims),
        )
        if request_fingerprint is not None and not hmac.compare_digest(
            request_fingerprint, fingerprint
        ):
            raise AttachmentRequestMismatchError(
                "conversation execution does not match its canonical request"
            )
        claim_fingerprint = idempotency_fingerprint or fingerprint
        deadline = asyncio.get_running_loop().time() + 5.0
        preferred_attempt_id = claimed_attempt_id
        staged_replay = None
        while True:
            claim = await self._store.claim_conversation_attachment(
                self._session_id,
                self._owner_principal_id,
                self._department,
                self._agent_name,
                turn_attachment_id,
                claim_fingerprint,
                claim_trace_id,
                preferred_attempt_id=preferred_attempt_id,
            )
            if claim.disposition == "published":
                prior = await self._store.published_conversation_replay(
                    self._session_id,
                    self._owner_principal_id,
                    self._department,
                    self._agent_name,
                    turn_attachment_id,
                    claim_fingerprint,
                )
                if prior is None:  # pragma: no cover - same locked row invariant
                    raise RuntimeError("published conversation replay is unavailable")
                # A transport retry replays only the exact already-served bytes.
                # It never re-enters the model/tool loop and never appends another
                # turn, even when the server trace/idempotency key is reused.  The
                # original successful run already has its terminal session.run
                # audit; a replay is not a second execution and must not duplicate
                # that record.  Transport composition audits delivery separately.
                self._replay_citations[turn_attachment_id] = prior.citations
                return AgentRunResult(
                    output=prior.output,
                    session_id=self._session_id,
                    verified=prior.verified,
                    grounded=prior.grounded,
                )
            if claim.disposition == "prepared" and claim.attempt_id is not None:
                prior = await self._store.prepared_conversation_replay(
                    self._session_id,
                    self._owner_principal_id,
                    self._department,
                    self._agent_name,
                    turn_attachment_id,
                    claim_fingerprint,
                )
                if prior is None:  # pragma: no cover - same locked row invariant
                    raise RuntimeError("prepared conversation replay is unavailable")
                self._prepared_attempts[turn_attachment_id] = claim.attempt_id
                self._replay_citations[turn_attachment_id] = prior.citations
                return AgentRunResult(
                    output=prior.output,
                    session_id=self._session_id,
                    verified=prior.verified,
                    grounded=prior.grounded,
                )
            if claim.disposition == "staged" and claim.attempt_id is not None:
                staged_replay = await self._store.staged_conversation_replay(
                    self._session_id,
                    self._owner_principal_id,
                    self._department,
                    self._agent_name,
                    turn_attachment_id,
                    claim_fingerprint,
                )
                if staged_replay is None:  # pragma: no cover - locked row invariant
                    raise RuntimeError("staged conversation replay is unavailable")
                self._prepared_attempts[turn_attachment_id] = claim.attempt_id
                self._replay_citations[turn_attachment_id] = staged_replay.citations
                break
            if claim.disposition == "owned" and claim.attempt_id is not None:
                self._prepared_attempts[turn_attachment_id] = claim.attempt_id
                await self._store.bind_conversation_execution(
                    self._session_id,
                    self._owner_principal_id,
                    turn_attachment_id,
                    claim.attempt_id,
                    fingerprint,
                )
                break
            if claimed_attempt_id is not None:
                raise LookupError("conversation attachment attempt is stale")
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("conversation attachment is still in flight")
            await asyncio.sleep(0.02)
            preferred_attempt_id = None
        if claim.attempt_id is None:  # pragma: no cover - claim-state invariant
            raise RuntimeError("active conversation claim has no attempt epoch")
        lease_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        lease_task = asyncio.create_task(
            self._renew_attachment_lease(
                turn_attachment_id,
                claim.attempt_id,
                stop=lease_stop,
                lost=lease_lost,
            )
        )

        # NE-T2X-5 (design §6.1/§6.3): a FRESH provisional-context accumulator
        # scope for THIS run() call only, entered alongside run_trace_scope —
        # a prior turn's provisional-ness must never taint this turn (the same
        # "fresh per turn" discipline as the GroundingRecorder below). Every
        # tool call's PDP input threads this turn's snapshot via
        # ne.runtime.middleware; retrieval tool handlers record into it via
        # current_provisional_recorder() — wired in ne-api (build_chat_bindings,
        # NE-T2X-5-RECORDER-WIRE).
        with (
            run_trace_scope(claim_trace_id) as trace_id,
            conversation_attempt_scope(
                ConversationAttemptBinding(
                    conversation_id=self._session_id,
                    owner_principal_id=self._owner_principal_id,
                    department=self._department,
                    agent_name=self._agent_name,
                    attachment_id=turn_attachment_id,
                    attempt_id=claim.attempt_id,
                    trace_id=claim_trace_id,
                )
            ),
            provisional_context_scope(),
            run_principal_scope(principal.id),
            run_requested_model_scope(requested_model, lane=requested_model_lane),
        ):
            record = None
            if self._registry is not None:
                record = self._registry.register(
                    kind="session", name=self._agent_name, principal_id=principal.id
                )
            succeeded = False
            try:
                if staged_replay is not None:
                    # A prior process durably staged the complete immutable
                    # envelope but disappeared before/after its idempotent
                    # terminal audit.  Re-enter only that audit+prepare tail;
                    # never reconstruct history or execute model/tools again.
                    succeeded = True
                    return AgentRunResult(
                        output=staged_replay.output,
                        session_id=self._session_id,
                        verified=staged_replay.verified,
                        grounded=staged_replay.grounded,
                    )
                history_limit = max(1, self._settings.session_history_max_turns)
                history = (
                    await self._history_loader(history_limit)
                    if self._history_loader is not None
                    else await self._store.load_conversation_history(
                        self._session_id,
                        self._owner_principal_id,
                        self._department,
                        self._agent_name,
                        limit=history_limit,
                    )
                )

                if self._router is not None:
                    outcome = await self._router.route(prompt, principal=principal, history=history)
                    if isinstance(outcome, RoutedTurn):
                        # Model NEVER instantiated on ROUTE/CLARIFY/REFUSE. The
                        # synthetic exchange MUST be persisted even on this
                        # short-circuit — otherwise the next model turn's
                        # history misses it and the model could contradict a
                        # verified answer it never saw.
                        messages = [
                            ModelRequest(parts=[UserPromptPart(content=prompt)]),
                            ModelResponse(
                                parts=[TextPart(content=outcome.output)],
                                model_name=outcome.marker,
                            ),
                        ]
                        await self._append_served_turn(
                            messages,
                            principal=principal,
                            trace_id=trace_id,
                            served_output=outcome.output,
                            attachment_id=turn_attachment_id,
                            verified=outcome.verified,
                            trace_linked=requested_model is not None,
                            request_fingerprint=fingerprint,
                            idempotency_fingerprint=claim_fingerprint,
                            served_citations=(served_citations() if served_citations else []),
                        )
                        succeeded = True
                        return AgentRunResult(
                            output=outcome.output,
                            session_id=self._session_id,
                            verified=outcome.verified,
                        )
                    # ROUTER_PASS: fall through to the unchanged model loop below.

                if self._grounding is not None:
                    # Grounded-agent POST-step (NE-GA-G3): a FRESH recorder for
                    # THIS run() call only — see the module docstring + design
                    # §7.6/§3: a prior turn's fetches must never ground this
                    # turn, so the recorder is never held across calls.
                    recorder = GroundingRecorder(self._executor)
                    tools = [
                        build_governed_tool(recorder, spec, principal)
                        for spec in recorder.tool_specs
                    ]
                    agent = Agent(
                        model=GatewayModel(
                            self._gateway,
                            self._settings,
                            on_unknown_tool=build_unknown_tool_hook(recorder, principal),
                            # NE-CTXB-3: the Layer-1 trim guard is enabled iff the
                            # composed RuntimeSettings carries a context_budget
                            # (platform-derived from the routed task's primary). The
                            # sink is this session's OWN audit sink so the mandatory
                            # counts-only context.budget event is hash-chained (C10);
                            # budget-without-sink is a construction-time ValueError.
                            budget=self._settings.context_budget,
                            sink=self._sink,
                            source_guard=self._source_guard,
                        ),
                        output_type=str,
                        tools=tools,
                        instructions=self._instructions,
                    )
                    _install_protocol_residue_validator(agent)
                    result = await agent.run(
                        prompt,
                        message_history=history,
                        usage_limits=UsageLimits(request_limit=self._settings.max_requests),
                    )
                    gate = await run_grounding_gate(
                        draft=str(result.output),
                        prompt=prompt,
                        principal=principal,
                        fetched=recorder.answers,
                        policy=self._grounding,
                    )
                    if not gate.provenance.fallback:
                        # NE-GA-G4: emit `gate.output`, not `str(result.output)`.
                        # With `assist=None` (or a rejected/erroring assist)
                        # `gate.output` IS `draft` (== str(result.output)) —
                        # byte-identical to G3. With an ACCEPTED polish,
                        # `gate.output` is the gate-recertified assist text,
                        # so an accepted polish can actually surface. History
                        # is UNCHANGED either way (`result.new_messages()`
                        # persists the model's own gate-CERTIFIED passing
                        # draft) — no ungrounded text can enter history
                        # through either path.
                        await self._append_served_turn(
                            result.new_messages(),
                            principal=principal,
                            trace_id=trace_id,
                            served_output=gate.output,
                            attachment_id=turn_attachment_id,
                            grounded=gate.provenance,
                            trace_linked=requested_model is not None,
                            request_fingerprint=fingerprint,
                            idempotency_fingerprint=claim_fingerprint,
                            served_citations=(served_citations() if served_citations else []),
                        )
                        succeeded = True
                        return AgentRunResult(
                            output=gate.output,
                            session_id=self._session_id,
                            grounded=gate.provenance,
                        )
                    # FAIL: the ungrounded draft is WITHHELD ENTIRELY — never
                    # persisted, never returned. Only the deterministic
                    # fallback is stamped and persisted, so no fragment of the
                    # failed exchange (draft text or its tool-call/return
                    # parts) can ever launder into a later turn's history.
                    messages = [
                        ModelRequest(parts=[UserPromptPart(content=prompt)]),
                        ModelResponse(
                            parts=[TextPart(content=gate.output)],
                            model_name=FALLBACK_MARKER,
                        ),
                    ]
                    await self._append_served_turn(
                        messages,
                        principal=principal,
                        trace_id=trace_id,
                        served_output=gate.output,
                        attachment_id=turn_attachment_id,
                        grounded=gate.provenance,
                        trace_linked=requested_model is not None,
                        request_fingerprint=fingerprint,
                        idempotency_fingerprint=claim_fingerprint,
                        served_citations=(served_citations() if served_citations else []),
                    )
                    succeeded = True
                    return AgentRunResult(
                        output=gate.output,
                        session_id=self._session_id,
                        grounded=gate.provenance,
                    )

                tools = [self._build_tool(spec, principal) for spec in self._executor.tool_specs]
                agent = Agent(
                    model=GatewayModel(
                        self._gateway,
                        self._settings,
                        on_unknown_tool=self._unknown_tool_hook(principal),
                        # NE-CTXB-3: same trim guard wiring as the grounding branch
                        # above — enabled iff RuntimeSettings carries a
                        # context_budget; the session's own sink audits every trim.
                        budget=self._settings.context_budget,
                        sink=self._sink,
                        source_guard=self._source_guard,
                    ),
                    output_type=str,
                    tools=tools,
                    instructions=self._instructions,
                )
                _install_protocol_residue_validator(agent)
                result = await agent.run(
                    prompt,
                    message_history=history,
                    usage_limits=UsageLimits(request_limit=self._settings.max_requests),
                )
                await self._append_served_turn(
                    result.new_messages(),
                    principal=principal,
                    trace_id=trace_id,
                    served_output=str(result.output),
                    attachment_id=turn_attachment_id,
                    trace_linked=requested_model is not None,
                    request_fingerprint=fingerprint,
                    idempotency_fingerprint=claim_fingerprint,
                    served_citations=(served_citations() if served_citations else []),
                )
                succeeded = True
                return AgentRunResult(output=str(result.output), session_id=self._session_id)
            finally:
                already_failing = sys.exc_info()[0] is not None
                try:
                    if succeeded:
                        try:
                            renewed = await self._store.renew_conversation_attachment_lease(
                                self._session_id,
                                self._owner_principal_id,
                                turn_attachment_id,
                                claim.attempt_id,
                            )
                        except Exception:
                            renewed = False
                        if not renewed:
                            lease_lost.set()
                    if lease_lost.is_set():
                        succeeded = False
                    if self._registry is not None and record is not None:
                        self._registry.finish(record.process_id, succeeded=succeeded)
                    if lease_lost.is_set() and not already_failing:
                        raise RuntimeError("conversation attachment lease ownership was lost")
                    await self._emit_session_run(
                        principal,
                        succeeded=succeeded,
                        attachment_id=turn_attachment_id,
                        attempt_id=claim.attempt_id,
                    )
                    if succeeded:
                        if lease_lost.is_set():
                            raise RuntimeError("conversation attachment lease ownership was lost")
                        attempt_id = self._prepared_attempts.get(turn_attachment_id)
                        if attempt_id is None:  # pragma: no cover - append invariant
                            raise LookupError("successful turn lost its attachment claim")
                        await self._store.prepare_conversation_turn(
                            self._session_id,
                            self._owner_principal_id,
                            turn_attachment_id,
                            attempt_id,
                        )
                except BaseException:
                    if not succeeded:
                        with suppress(LookupError):
                            await self.abort_turn(turn_attachment_id)
                    raise
                finally:
                    # Ownership stays live through the potentially blocking
                    # terminal audit and the successful staged->prepared CAS.
                    lease_stop.set()
                    await lease_task
                if not succeeded:
                    with suppress(LookupError):
                        await self.abort_turn(turn_attachment_id)
                elif attachment_id is None:
                    # Direct runtime callers have no outer SSE/worker publication
                    # boundary. Their successful audited return is the boundary.
                    await self.publish_turn(turn_attachment_id, citations=[])
                    self.finalize_turn(turn_attachment_id)

    async def run_claimed(
        self,
        prompt: str,
        *,
        principal: Principal,
        requested_model: str | None,
        requested_model_lane: Lane | None,
        attachment_id: str,
        request_fingerprint: str,
        idempotency_fingerprint: str,
        claimed_attempt_id: str,
        served_citations: Callable[[], list[dict[str, str]]] | None = None,
    ) -> AgentRunResult:
        """Execute only an exact server-claimed durable worker request."""
        if not all(
            value.strip()
            for value in (
                attachment_id,
                request_fingerprint,
                idempotency_fingerprint,
                claimed_attempt_id,
            )
        ):
            raise ValueError("claimed conversation execution binding is incomplete")
        return await self.run(
            prompt,
            principal=principal,
            requested_model=requested_model,
            requested_model_lane=requested_model_lane,
            attachment_id=attachment_id,
            request_fingerprint=request_fingerprint,
            idempotency_fingerprint=idempotency_fingerprint,
            claimed_attempt_id=claimed_attempt_id,
            served_citations=served_citations,
        )

    async def recover_staged_turn(
        self,
        *,
        principal: Principal,
        attachment_id: str,
        idempotency_fingerprint: str,
        claimed_attempt_id: str,
        trace_id: str,
    ) -> AgentRunResult:
        """Finish only the audit/prepare tail of one immutable staged envelope."""
        if (
            principal.id != self._owner_principal_id
            or principal.claims.get("department") != self._department
        ):
            raise SessionDepartmentMismatchError("principal does not own this conversation")
        claim = await self._store.claim_conversation_attachment(
            self._session_id,
            self._owner_principal_id,
            self._department,
            self._agent_name,
            attachment_id,
            idempotency_fingerprint,
            trace_id,
            preferred_attempt_id=claimed_attempt_id,
        )
        if claim.disposition != "staged" or claim.attempt_id != claimed_attempt_id:
            raise LookupError("conversation attachment attempt is not staged")
        replay = await self._store.staged_conversation_replay(
            self._session_id,
            self._owner_principal_id,
            self._department,
            self._agent_name,
            attachment_id,
            idempotency_fingerprint,
        )
        if replay is None:
            raise LookupError("conversation attachment staged envelope is unavailable")
        self._prepared_attempts[attachment_id] = claimed_attempt_id
        self._replay_citations[attachment_id] = replay.citations
        lease_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        lease_task = asyncio.create_task(
            self._renew_attachment_lease(
                attachment_id,
                claimed_attempt_id,
                stop=lease_stop,
                lost=lease_lost,
            )
        )
        try:
            with run_trace_scope(trace_id):
                await self._emit_session_run(
                    principal,
                    succeeded=True,
                    attachment_id=attachment_id,
                    attempt_id=claimed_attempt_id,
                )
                if lease_lost.is_set():
                    raise RuntimeError("conversation attachment lease ownership was lost")
                await self._store.prepare_conversation_turn(
                    self._session_id,
                    self._owner_principal_id,
                    attachment_id,
                    claimed_attempt_id,
                )
        finally:
            lease_stop.set()
            await lease_task
        return AgentRunResult(
            output=replay.output,
            session_id=self._session_id,
            verified=replay.verified,
            grounded=replay.grounded,
        )

    async def adopt_prepared_turn(
        self,
        *,
        principal: Principal,
        attachment_id: str,
        idempotency_fingerprint: str,
        claimed_attempt_id: str,
        trace_id: str,
    ) -> AgentRunResult:
        """Adopt one exact terminal-audited envelope after process restart.

        This is the public recovery seam used by the API composition.  It
        revalidates every immutable owner/Agent/request/trace dimension through
        the store claim before registering the rotated epoch locally; callers
        never mutate ``_prepared_attempts`` directly.
        """
        if (
            principal.id != self._owner_principal_id
            or principal.claims.get("department") != self._department
        ):
            raise SessionDepartmentMismatchError("principal does not own this conversation")
        claim = await self._store.claim_conversation_attachment(
            self._session_id,
            self._owner_principal_id,
            self._department,
            self._agent_name,
            attachment_id,
            idempotency_fingerprint,
            trace_id,
            preferred_attempt_id=claimed_attempt_id,
        )
        if claim.disposition != "prepared" or claim.attempt_id != claimed_attempt_id:
            raise LookupError("conversation attachment attempt is not prepared")
        replay = await self._store.prepared_conversation_replay(
            self._session_id,
            self._owner_principal_id,
            self._department,
            self._agent_name,
            attachment_id,
            idempotency_fingerprint,
        )
        if replay is None:
            raise LookupError("conversation attachment prepared envelope is unavailable")
        self._prepared_attempts[attachment_id] = claimed_attempt_id
        self._replay_citations[attachment_id] = replay.citations
        return AgentRunResult(
            output=replay.output,
            session_id=self._session_id,
            verified=replay.verified,
            grounded=replay.grounded,
        )

    async def _renew_attachment_lease(
        self,
        attachment_id: str,
        attempt_id: str,
        *,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self._store.conversation_heartbeat_seconds
                )
                return
            except TimeoutError:
                try:
                    renewed = await self._store.renew_conversation_attachment_lease(
                        self._session_id,
                        self._owner_principal_id,
                        attachment_id,
                        attempt_id,
                    )
                except Exception:
                    lost.set()
                    return
                if not renewed:
                    lost.set()
                    return

    async def _append_served_turn(
        self,
        messages: list[ModelMessage],
        *,
        principal: Principal,
        trace_id: str,
        served_output: str,
        attachment_id: str,
        verified: VerifiedProvenance | None = None,
        grounded: GroundedProvenance | None = None,
        trace_linked: bool = False,
        request_fingerprint: str | None = None,
        idempotency_fingerprint: str | None = None,
        served_citations: list[dict[str, str]] | None = None,
    ) -> None:
        if principal.id != self._owner_principal_id:
            raise SessionDepartmentMismatchError(
                "principal does not own this conversation; append is forbidden"
            )
        attempt_id = self._prepared_attempts.get(attachment_id)
        if attempt_id is None:
            raise LookupError("session does not own the conversation attachment claim")
        await self._store.append_conversation_turn(
            self._session_id,
            self._owner_principal_id,
            self._department,
            self._agent_name,
            messages,
            served_output=served_output,
            trace_id=trace_id,
            attachment_id=attachment_id,
            attempt_id=attempt_id,
            verified=verified,
            grounded=grounded,
            trace_linked=trace_linked,
            request_fingerprint=request_fingerprint,
            idempotency_fingerprint=idempotency_fingerprint,
            citation_payloads=served_citations or [],
            terminal_audited=False,
        )

    def prepared_attempt_id(self, attachment_id: str) -> str | None:
        """Return this composed session's private nonce for its prepared row."""
        return self._prepared_attempts.get(attachment_id)

    def take_replay_citations(self, attachment_id: str) -> tuple[dict[str, str], ...]:
        return self._replay_citations.pop(attachment_id, ())

    async def publish_turn(self, attachment_id: str, *, citations: list[dict[str, str]]) -> None:
        attempt_id = self._prepared_attempts.get(attachment_id)
        if attempt_id is None:
            replay = await self._store.published_conversation_output(
                self._session_id,
                self._owner_principal_id,
                self._department,
                self._agent_name,
                attachment_id,
            )
            if replay is not None:
                return
            raise LookupError("session did not prepare this conversation turn")
        await self._store.publish_conversation_turn(
            self._session_id,
            self._owner_principal_id,
            attachment_id,
            attempt_id,
            [payload["chunk_id"] for payload in citations],
            citation_payloads=citations,
        )

    def finalize_turn(self, attachment_id: str) -> None:
        """Forget an attempt only after its outer transport completed terminally."""
        self._prepared_attempts.pop(attachment_id, None)

    async def abort_turn(self, attachment_id: str) -> None:
        attempt_id = self._prepared_attempts.get(attachment_id)
        if attempt_id is None:
            return
        try:
            await self._store.abort_conversation_turn(
                self._session_id,
                self._owner_principal_id,
                attachment_id,
                attempt_id,
            )
        finally:
            if self._prepared_attempts.get(attachment_id) == attempt_id:
                self._prepared_attempts.pop(attachment_id, None)

    async def _emit_session_run(
        self,
        principal: Principal,
        *,
        succeeded: bool,
        attachment_id: str,
        attempt_id: str,
    ) -> None:
        """Emit the D-2 ``session.run`` terminal audit event (names/ids only, C12).

        Runs inside ``run_trace_scope`` so ``trace_id`` matches this run's
        ``tool.execute`` events (the Skill Forge joins on it). Failure semantics
        (decided): if the run SUCCEEDED and the sink append fails ⇒ raise
        :class:`RuntimeAuditError` (C6 fail-closed, result withheld — mirrors
        ``middleware._emit``); if an exception is ALREADY propagating ⇒ suppress
        the emit failure so the original error wins.
        """
        if self._sink is None:
            return
        already_failing = sys.exc_info()[0] is not None
        provenance: dict[str, str] = {}
        if succeeded:
            try:
                context = await self._store.context_snapshot(
                    self._session_id,
                    max_components=max(1, self._settings.session_history_max_turns),
                    prepared_attachment_id=attachment_id,
                )
                if context is None:
                    raise LookupError("session disappeared before terminal context stamp")
            except Exception as exc:
                # A successful answer without its terminal context identity would
                # make the model-flip ledger unverifiable. Withhold the result just
                # like an audit-append failure; never emit an unstamped success.
                raise RuntimeAuditError(
                    f"context snapshot failed for session.run on session:{self._session_id}; "
                    "result withheld (C6 fail closed)"
                ) from exc
            provenance = {
                "context_id": context.context_id,
                "context_active_components": str(len(context.components)),
                "context_total_components": str(context.total_components),
            }
        event = new_audit_event(
            action="session.run",
            resource=f"session:{self._session_id}",
            outcome=AuditOutcome.success if succeeded else AuditOutcome.error,
            actor=principal.id,
            detail=json.dumps(
                {
                    "agent": self._agent_name,
                    "department": self._department,
                    "session_id": self._session_id,
                    "succeeded": succeeded,
                }
            ),
            trace_id=current_run_trace_id(),
            provenance=provenance,
            event_id=hashlib.sha256(
                (
                    "principal-conversation-session-run-v1:"
                    f"{self._session_id}:{attachment_id}:"
                    f"{'success' if succeeded else attempt_id}"
                ).encode()
            ).hexdigest(),
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            if already_failing:
                # An error is already propagating (the run failed for another
                # reason) — the original error must win; do not mask it.
                return
            raise RuntimeAuditError(
                f"audit append failed for session.run on session:{self._session_id}; "
                "result withheld (C6 fail closed)"
            ) from exc

    async def close(self) -> None:
        """No-op: the engine is owned by the store / composition root."""

    def _unknown_tool_hook(self, principal: Principal) -> UnknownToolHook:
        # One-line delegate into the shared runner primitive (verbatim NE-033
        # semantics). The C6 guardian calls this private method by name, so the
        # SIGNATURE is unchanged — only the body moved to ne.runtime.runner.
        return build_unknown_tool_hook(self._executor, principal)

    def _build_tool(self, spec: ToolSpec, principal: Principal) -> Tool[Any]:
        # One-line delegate into the shared runner primitive (verbatim NE-033
        # closure pattern — **kwargs the only parameter, tool name by closure). The
        # C6 guardian calls this private method by name; the SIGNATURE is unchanged.
        return build_governed_tool(self._executor, spec, principal)
