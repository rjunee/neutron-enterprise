# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded, receipt-gated adapters for canonical Run execution (PRE-010B).

010B owns the non-bypassable worker boundary, not the strategy profile.  The
``RunExecutionAdapter`` therefore receives only the accepted Run, its protected
input materialized in worker memory, and wrappers around the composed Gateway and
GovernedToolExecutor.  It never receives either raw effect capability.

The execution-profile adapter that supplies the exact effective Principal and
deterministic/agentic strategy is owned by 010D.  Until that adapter is composed,
``UnavailableRunExecutionAdapter`` fails closed; 010B does not fabricate groups,
roles, connector identity or a model policy from the owner id in the Run row.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar, runtime_checkable

from ne.contracts.governance import AuditSink, PolicyDecisionPoint
from ne.contracts.models import (
    Completion,
    CompletionChunk,
    CompletionRequest,
    ModelGateway,
    TurnContextBudget,
    TurnModelBudget,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import SourceBoundToolResultRef, SourceBoundToolResultSetRef
from ne.contracts.types import Lane, Principal
from ne.events import new_audit_event
from ne.runtime.run_audit_receipts import (
    ExistingRunEvidenceProvider,
    RunAuditReceiptSink,
    RunToolCallContext,
    current_agentic_plan_step,
    run_tool_call_scope,
)
from ne.runtime.run_finalizer import (
    RunEvidenceBinding,
    RunTurnTarget,
    RunVerifierBinding,
    StructuredRunOutputEnvelope,
)
from ne.runtime.run_store import _MODEL_NAME_ALPHABET, AgentRun, RunAuditDecisionKind
from ne.runtime.source_results import decode_protected_reference, encode_protected_reference
from ne.schema import AuditOutcome

__all__ = [
    "ReceiptGatedGateway",
    "ReceiptGatedToolExecutor",
    "RunCancellationRequested",
    "RunEffectContinuation",
    "RunExecutionAdapter",
    "RunExecutionBoundary",
    "RunExecutionContext",
    "RunExecutionResult",
    "RunLeaseLost",
    "UnavailableRunExecutionAdapter",
]

_T = TypeVar("_T")


class RunCancellationRequested(RuntimeError):
    """The durable Run entered cancelling at a cooperative step boundary."""


class RunLeaseLost(RuntimeError):
    """The caller no longer owns the exact fenced Run execution lease."""


class RunExecutionBoundary(Protocol):
    async def heartbeat(self, stage: str) -> None: ...

    async def checkpoint(self, *, stage: str, safe_detail: Mapping[str, object]) -> None: ...

    async def await_inflight(self, operation: Awaitable[_T], *, stage: str) -> _T: ...


class _NoopRunExecutionBoundary:
    async def heartbeat(self, stage: str) -> None:
        del stage

    async def checkpoint(self, *, stage: str, safe_detail: Mapping[str, object]) -> None:
        del stage, safe_detail

    async def await_inflight(self, operation: Awaitable[_T], *, stage: str) -> _T:
        del stage
        return await operation


_NOOP_BOUNDARY = _NoopRunExecutionBoundary()


class _ToolExecutor(Protocol):
    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]: ...

    async def execute(
        self, tool: str, args: Mapping[str, object], *, principal: Principal
    ) -> str: ...


@runtime_checkable
class _ToolsetAuthorityProvider(Protocol):
    def _toolset_authority(self) -> tuple[AuditSink, PolicyDecisionPoint]: ...


class ReceiptGatedGateway:
    """ModelGateway capability that persists a pre-effect Run receipt first."""

    def __init__(
        self,
        inner: ModelGateway,
        *,
        audit: RunAuditReceiptSink,
        owner_principal_id: str,
        step_id: str,
        boundary: RunExecutionBoundary = _NOOP_BOUNDARY,
    ) -> None:
        self.__inner = inner
        self.__audit = audit
        self.__owner = owner_principal_id
        self.__step_id = step_id
        self.__boundary = boundary
        self.__ordinal = 0

    async def complete(self, request: CompletionRequest) -> Completion:
        ordinal = self.__ordinal
        self.__ordinal += 1
        await self.__boundary.heartbeat(f"{self.__step_id}:before:{ordinal}")
        await self.__boundary.await_inflight(
            self.__admit(request, ordinal),
            stage=f"{self.__step_id}:audit-wait:{ordinal}",
        )
        # AIR_GAP, provider allowlists and routing remain enforced by the one
        # composed ne-gateway object.  This wrapper has no HTTP/provider seam.
        return await self.__boundary.await_inflight(
            self.__inner.complete(request), stage=f"{self.__step_id}:wait:{ordinal}"
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        ordinal = self.__ordinal
        self.__ordinal += 1

        async def gated() -> AsyncIterator[CompletionChunk]:
            await self.__boundary.heartbeat(f"{self.__step_id}:before:{ordinal}")
            await self.__boundary.await_inflight(
                self.__admit(request, ordinal),
                stage=f"{self.__step_id}:audit-wait:{ordinal}",
            )
            iterator = self.__inner.stream(request).__aiter__()
            while True:
                try:
                    chunk = await self.__boundary.await_inflight(
                        iterator.__anext__(),
                        stage=f"{self.__step_id}:stream-wait:{ordinal}",
                    )
                except StopAsyncIteration:
                    return
                yield chunk

        return gated()

    async def context_budget_for_turn(
        self, task: str, *, requested_model: str | None = None, lane: Lane | None = None
    ) -> TurnModelBudget | None:
        """Forward the NE-CTX-IN-FORCE capability to the wrapped gateway.

        ⚠️ WITHOUT THIS THE PER-TURN BUDGET IS INERT ON EVERY AGENT RUN. This
        wrapper is on the model effect of EVERY canonical Run
        (``run_workflow.py``), and ``GatewayModel._turn_budget`` tests its
        gateway with ``isinstance(..., TurnContextBudget)`` — a method-presence
        Protocol. A wrapper that does not carry the method fails that test, so
        the guard silently keeps the COMPOSED BOOT budget: on the exact path the
        ``context_exhausted`` incident used, nothing was ever re-derived.

        EXPLICIT, not ``__getattr__``: this class deliberately exposes only the
        two ``ModelGateway`` methods plus this one, so a future capability on the
        inner gateway is a decision to forward it rather than something a
        blanket proxy leaks past the receipt gate. Forwarding this one is safe
        because it is a PURE READ of already-composed config — no provider seam,
        no egress, nothing to gate a receipt on.
        """
        inner = self.__inner
        if not isinstance(inner, TurnContextBudget):
            return None
        return await inner.context_budget_for_turn(task, requested_model=requested_model, lane=lane)

    async def __admit(self, request: CompletionRequest, ordinal: int) -> None:
        if request.principal_id != self.__owner:
            raise PermissionError(
                "Run model request lacks the exact accepted effective principal identity"
            )
        sink = self.__audit.for_target(
            step_id=self.__step_id,
            event_ordinal=ordinal,
            decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        )
        # NAME THE MODEL THIS CALL IS PINNED TO, when the caller pinned one.
        # The continuity gateway pins each attempt's exact authorized candidate
        # into `requested_model` before completing through this gate, so the
        # pre-effect record can state WHICH model the call is about -- a
        # declared registry alias, never provider text: anything outside the
        # writer's own model-name alphabet is omitted, not truncated. An unpinned
        # call (platform-routed) records no name, which projects as the honest
        # unknown it is.
        provenance: dict[str, str] = {}
        if isinstance(request.requested_model, str) and (
            0 < len(request.requested_model) <= 256
            and all(character in _MODEL_NAME_ALPHABET for character in request.requested_model)
        ):
            provenance["requested_model"] = request.requested_model
        await sink.append(
            new_audit_event(
                action="run.model.pre_effect",
                resource=f"gateway-task:{request.task}",
                outcome=AuditOutcome.success,
                actor=self.__owner,
                detail=(
                    "pre-effect audit intent; Gateway remains blocked until its exact "
                    "Run receipt persists"
                ),
                provenance=provenance or None,
            )
        )


class ReceiptGatedToolExecutor:
    """Handler-less executor capability with a receipt before governance/effect."""

    def __init__(
        self,
        inner: _ToolExecutor,
        *,
        audit: RunAuditReceiptSink,
        run: AgentRun,
        target: RunTurnTarget,
        owner_principal_id: str,
        step_id: str,
        boundary: RunExecutionBoundary = _NOOP_BOUNDARY,
        pinned_tool_call_id: str | None = None,
    ) -> None:
        self.__inner = inner
        self.__audit = audit
        self.__run = run
        self.__target = target
        self.__owner = owner_principal_id
        self.__step_id = step_id
        self.__boundary = boundary
        self.__ordinal = 0
        self.__served_protected_refs: dict[str, str] = {}
        # APPROVAL-CONTINUATION ONLY.  A normal execution mints its own
        # `tool_call_id` from a fresh per-attempt counter, which is exactly why a
        # replayed resume can never land on the withheld call's identity.  A
        # continuation is re-executing ONE call whose id the park already pinned
        # in `ne_agent_run_continuation_checkpoints.tool_call_id`, so it supplies
        # that id verbatim and is structurally allowed exactly one call: a second
        # call under a pinned id would be a different effect wearing an approved
        # effect's identity.
        self.__pinned_tool_call_id = pinned_tool_call_id

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return self.__inner.tool_specs

    @property
    def served_protected_refs(self) -> tuple[str, ...]:
        """Exact content-free protected references observed from successful Tool calls."""

        return tuple(self.__served_protected_refs.values())

    def _toolset_authority(self) -> tuple[AuditSink, PolicyDecisionPoint]:
        """Preserve the inner governed authority across the receipt-only wrapper."""
        if not isinstance(self.__inner, _ToolsetAuthorityProvider):
            raise TypeError(
                "ReceiptGatedToolExecutor cannot be toolset-scoped without "
                "governed audit/PDP authority"
            )
        return self.__inner._toolset_authority()

    async def execute(self, tool: str, args: Mapping[str, object], *, principal: Principal) -> str:
        if principal.id != self.__owner:
            raise PermissionError(
                "Run Tool call principal differs from the accepted effective identity"
            )
        ordinal = self.__ordinal
        self.__ordinal += 1
        if self.__pinned_tool_call_id is not None and ordinal > 0:
            raise RuntimeError("an approval continuation executes exactly one pinned Tool call")
        await self.__boundary.heartbeat(f"{self.__step_id}:before:{ordinal}")
        tool_call_id = (
            self.__pinned_tool_call_id
            if self.__pinned_tool_call_id is not None
            else f"{self.__step_id}:tool:{ordinal}"
        )
        # BIND THE PLAN ORDINAL TO THIS EXACT CALL ID, from the one frame that
        # knows both. The agentic planner announces the plan ordinal it durably
        # checkpointed for this call (`agentic-intent:{ordinal}`); this frame is
        # minting the `tool_call_id` that every `run.tool.pre_effect` receipt
        # and evidence record will carry. Written BEFORE the receipt, so even a
        # call that dies mid-effect has its binding. No announcement (a
        # deterministic Run, or a non-planner caller) writes nothing, and an
        # announcement naming a DIFFERENT tool writes nothing -- a missing
        # binding projects as an honest unknown, a wrong one would be a lie.
        announced = current_agentic_plan_step()
        if announced is not None and announced.tool == tool:
            await self.__boundary.checkpoint(
                stage=f"agentic-call:{announced.ordinal}",
                safe_detail={
                    "ordinal": announced.ordinal,
                    "tool": tool,
                    "tool_call_id": tool_call_id,
                },
            )
        sink = self.__audit.for_target(
            step_id=self.__step_id,
            tool_call_id=tool_call_id,
            event_ordinal=ordinal,
            decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        )
        receipt = await self.__boundary.await_inflight(
            sink.append_receipt(
                new_audit_event(
                    action="run.tool.pre_effect",
                    resource=f"tool:{tool}",
                    outcome=AuditOutcome.success,
                    actor=principal.id,
                    detail=(
                        "pre-effect audit intent; governed Tool execution remains blocked "
                        "until its exact Run receipt persists"
                    ),
                )
            ),
            stage=f"{self.__step_id}:audit-wait:{ordinal}",
        )
        # This is the only successful path. Guardrails, PDP, approvals and the
        # handler remain owned by the composed GovernedToolExecutor.
        if (
            receipt.run_id != self.__run.run_id
            or receipt.attempt_id != self.__target.attempt_id
            or receipt.tool_call_id != tool_call_id
        ):
            raise RuntimeError("Run Tool receipt differs from its canonical execution target")
        provider = ExistingRunEvidenceProvider.from_existing(run=self.__run, receipt=receipt)
        context = RunToolCallContext(
            run=self.__run,
            receipt=receipt,
            conversation_id=self.__target.conversation_id,
            turn_id=self.__target.turn_id,
            turn_sequence=self.__target.turn_sequence,
            attachment_id=self.__target.attachment_id,
            evidence=provider,
            served_protected_refs=self.served_protected_refs,
        )
        with run_tool_call_scope(context):
            result = await self.__boundary.await_inflight(
                self.__inner.execute(tool, args, principal=principal),
                stage=f"{self.__step_id}:wait:{ordinal}",
            )
        reference = decode_protected_reference(result)
        if isinstance(reference, SourceBoundToolResultRef | SourceBoundToolResultSetRef):
            encoded = encode_protected_reference(reference)
            self.__served_protected_refs[reference.reference_id] = encoded
        return result


@dataclass(frozen=True)
class RunEffectContinuation:
    """The ONE approved effect an approval-resume executes — nothing re-derived.

    Every field is loaded from ``ne_agent_run_continuation_checkpoints`` /
    ``ne_agent_run_approval_joins`` and the re-authorized protected invocation:
    the tool NAME comes from the approval request the human read, the ARGUMENTS
    are the exact parked invocation plaintext that ``authorize`` just
    re-authorized against every source-authority stamp, and ``step_id`` /
    ``tool_call_id`` are the server-minted identity of the withheld call.

    NOTHING here comes from a model. That is the whole point: the resume used to
    replay the agent loop, which re-called the model, which regenerated the
    ``step_intent`` prose embedded in the ``agentic-intent:{ordinal}`` checkpoint
    digest, which collided with the parked attempt's checkpoint and killed the
    workflow with "Run checkpoint was replayed under a different intent" — and
    which could equally have re-derived DIFFERENT arguments for an effect a human
    approved in a specific form.
    """

    approval_id: str
    step_id: str
    tool_call_id: str
    action: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class RunExecutionContext:
    run: AgentRun
    attempt_id: str
    target: RunTurnTarget
    input_bytes: bytes
    gateway: ReceiptGatedGateway
    executor: ReceiptGatedToolExecutor
    audit_sink: RunAuditReceiptSink
    recovered_uncertain_model_attempt: bool
    boundary: RunExecutionBoundary = _NOOP_BOUNDARY
    #: Present only on an approval resume. When set the adapter executes THAT
    #: pinned effect and never enters the agent loop or the model.
    continuation: RunEffectContinuation | None = None


@dataclass(frozen=True)
class RunExecutionResult:
    """Ephemeral result consumed by 010F before the content-free DBOS boundary."""

    outcome: Literal["execution_complete", "context_exhausted", "budget_exhausted"]
    output_bytes: bytes | None = None
    structured_envelope: StructuredRunOutputEnvelope | None = None
    evidence_bindings: tuple[RunEvidenceBinding, ...] = ()
    verifier_bindings: tuple[RunVerifierBinding, ...] = ()
    protected_result_ref: str | None = None
    protected_result_hash: str | None = None
    #: The OPT-IN safe sentence naming WHICH ceiling ended the run, or ``None``.
    #:
    #: ``budget_exhausted`` is the shared terminal for five different ceilings
    #: (tokens, tool calls, model requests, wall clock, parked writes), so the
    #: status word alone cannot tell a reader what to change.  The producer names
    #: the cap AND its configured value; a producer that cannot tell leaves this
    #: ``None`` and the reader is told the status word alone, exactly as before.
    exhausted_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.output_bytes is None) != (self.structured_envelope is None):
            raise ValueError(
                "terminal output and structured envelope must be both present or absent"
            )
        if self.outcome != "execution_complete" and self.output_bytes is not None:
            raise ValueError("exhausted Run execution cannot carry partial terminal output")
        if self.outcome != "execution_complete" and (
            self.evidence_bindings or self.verifier_bindings
        ):
            raise ValueError("exhausted Run execution cannot carry claim bindings")
        if (
            self.structured_envelope is not None
            and hashlib.sha256(self.output_bytes or b"").hexdigest()
            != self.structured_envelope.served_output_hash
        ):
            raise ValueError("Run execution output differs from its structured envelope")
        if self.exhausted_reason is not None:
            if self.outcome == "execution_complete":
                raise ValueError("a completed Run execution has no exhausted-ceiling reason")
            reason = self.exhausted_reason
            # The SAME bounds ``ne.api.run_projection._failure_reason`` enforces on
            # the published record.  Failing loud here is deliberate: a reason that
            # would be silently dropped by the reader is a reason the operator
            # never sees, which is worse than the blank it was meant to replace.
            if (
                not reason.strip()
                or len(reason) > 256
                or any(not character.isprintable() for character in reason)
            ):
                raise ValueError(
                    "Run exhausted-ceiling reason must be printable and at most 256 characters"
                )
        if (self.protected_result_ref is None) != (self.protected_result_hash is None):
            raise ValueError("protected Run result ref/hash must be both present or both absent")
        if self.protected_result_hash is not None and (
            len(self.protected_result_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.protected_result_hash)
        ):
            raise ValueError("protected Run result hash must be lowercase SHA-256")


class RunExecutionAdapter(Protocol):
    async def execute(self, context: RunExecutionContext) -> RunExecutionResult: ...


class UnavailableRunExecutionAdapter:
    """Dormant adapter until 010D supplies accepted profile + identity binding."""

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        del context
        raise RuntimeError(
            "canonical Run execution profile is unavailable; PRE-010D is not composed"
        )
