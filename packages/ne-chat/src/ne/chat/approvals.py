# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ApprovalActionService — THE approval-mirroring security crux (channel-agnostic).

Actioning a mirrored approval from Slack MUST NOT become an approval bypass. This
service is a fail-closed PRE-CHECK plus a signed SUBMISSION onto the REAL NE-035
``DurableApprovalGate``; it NEVER constructs an ``ApprovalOutcome`` itself and
never runs a parallel approval path. The gate's tier / named-approver /
distinct-second-check / expiry logic stays authoritative.

The APPROVER IDENTITY is ALWAYS the mapped ``principal.id`` — never the raw Slack
user id. So a Tier-3 named-approver membership check inside the gate is evaluated
against the platform principal, exactly as it would be from the console.

:meth:`act` order (fail closed at each step; the gate is reached ONLY after both
pre-checks pass):

1. resolve the Slack user to a Principal — unmapped ⇒ ``chat.approval.unauthorized``
   audit + rejected result; the gate is NEVER called (the linker also audited the
   ``chat.identity.denied`` record);
2. authorization pre-check — an EMPTY ``approver_roles`` OR a principal whose groups
   are disjoint from ``approver_roles`` ⇒ ``chat.approval.unauthorized`` + rejected;
   the gate is NEVER called (fail closed);
3. route the decision through the real gate (approve/reject) with
   ``approver=principal.id``;
4. a gate ``ApprovalPolicyError`` (not-a-named-approver / same-second-approver) ⇒
   ``chat.approval.unauthorized`` (reason ``gate-policy``, error CLASS only) +
   rejected; ``ApprovalAlreadyDecidedError`` / ``ApprovalRequestNotFoundError`` ⇒
   ``chat.approval.stale`` + rejected;
5. a successful submission emits ``chat.approval.submitted`` — the Slack-ORIGIN
   record layered ON TOP of the gate's own ``approval.granted`` / ``approval.rejected``
   events.

Redaction (C12): audit detail carries only ids / decisions / reasons / error
classes — never message text or approval payload values.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ne.chat.errors import UnmappedChatUserError
from ne.chat.identity import ChatIdentityLinker
from ne.contracts.governance import ApprovalStatus, AuditSink
from ne.events import new_audit_event
from ne.runtime import (
    ApprovalAlreadyDecidedError,
    ApprovalPolicyError,
    ApprovalRequestNotFoundError,
    ApprovalSourceWithheldFromApproverError,
    DurableApprovalGate,
)
from ne.runtime.source_results import SourceAuthorityRetryableError
from ne.schema import AuditOutcome

__all__ = ["ApprovalActionResult", "ApprovalActionService"]

_STALE_ERRORS = (ApprovalAlreadyDecidedError, ApprovalRequestNotFoundError)


class ApprovalActionResult(BaseModel):
    """The outcome of a Slack-originated approval action (a VIEW, not a gate write)."""

    model_config = ConfigDict(frozen=True)

    status: str
    request_id: str
    note: str = ""
    approver_id: str | None = None


class ApprovalActionService:
    """Route a Slack approve/deny click onto the real durable gate, fail closed."""

    def __init__(
        self,
        gate: DurableApprovalGate,
        linker: ChatIdentityLinker,
        approver_roles: tuple[str, ...],
        sink: AuditSink,
        *,
        surface: str = "slack",
    ) -> None:
        self._gate = gate
        self._linker = linker
        self._approver_roles = approver_roles
        self._sink = sink
        self._surface = surface

    async def act(
        self,
        *,
        external_user_id: str,
        request_id: str,
        decision: Literal["approve", "deny"],
    ) -> ApprovalActionResult:
        """Action a mirrored approval from chat; the gate stays authoritative."""
        # (1) Identity — unmapped users can never reach the gate.
        try:
            principal = await self._linker.resolve(external_user_id)
        except UnmappedChatUserError:
            await self._unauthorized(
                actor=f"{self._surface}:{external_user_id}",
                request_id=request_id,
                reason="unmapped",
            )
            return ApprovalActionResult(
                status="rejected", request_id=request_id, note="not linked to a platform identity"
            )

        # (2) Authorization pre-check — EMPTY roles ⇒ nobody; disjoint groups ⇒ reject.
        if not self._approver_roles or set(principal.groups).isdisjoint(self._approver_roles):
            await self._unauthorized(
                actor=principal.id, request_id=request_id, reason="not-an-approver"
            )
            return ApprovalActionResult(
                status="rejected", request_id=request_id, note="not an authorized approver"
            )

        # (3) Route the decision through the REAL gate (approver identity == principal.id).
        try:
            if decision == "approve":
                return await self._approve(principal.id, request_id)
            return await self._deny(principal.id, request_id)
        except SourceAuthorityRetryableError:
            # Indeterminate source authority: the gate wrote NOTHING and the
            # request is still pending — but the Slack transport runs this in a
            # background task, so an uncaught raise means the approver sees no
            # reply at all (Sol round 2, P2). Return the honest retry message
            # instead. No audit record: no decision landed, nothing was refused
            # to this principal — the gate's own state is byte-unchanged.
            return ApprovalActionResult(
                status="rejected",
                request_id=request_id,
                note=(
                    "source authority for this request could not be established "
                    "right now; nothing was decided — it remains pending, try "
                    "again shortly"
                ),
            )
        except ApprovalSourceWithheldFromApproverError as exc:
            # THIS approver's own source authority does not admit the request's
            # protected invocation. A per-principal refusal, never a revocation:
            # the request stays PENDING at the gate for another approver (U2
            # adversarial review, standalone-gate ruling).
            await self._unauthorized(
                actor=principal.id,
                request_id=request_id,
                reason="source-authority-withheld-for-principal",
                error_class=type(exc).__name__,
            )
            return ApprovalActionResult(
                status="rejected",
                request_id=request_id,
                note=(
                    "you do not hold current source authority over this request; "
                    "it remains pending for another approver"
                ),
            )
        except ApprovalPolicyError as exc:
            await self._unauthorized(
                actor=principal.id,
                request_id=request_id,
                reason="gate-policy",
                error_class=type(exc).__name__,
            )
            return ApprovalActionResult(
                status="rejected", request_id=request_id, note="rejected by approval policy"
            )
        except _STALE_ERRORS as exc:
            await self._stale(
                actor=principal.id, request_id=request_id, error_class=type(exc).__name__
            )
            return ApprovalActionResult(
                status="rejected", request_id=request_id, note="approval no longer actionable"
            )

    async def _approve(self, approver_id: str, request_id: str) -> ApprovalActionResult:
        outcome = await self._gate.approve(request_id, approver=approver_id)
        if outcome is None:
            # Tier-3 FIRST named check recorded by the gate; still awaiting a distinct second.
            await self._submitted(approver_id, request_id, decision="approve")
            return ApprovalActionResult(
                status="pending_second",
                request_id=request_id,
                approver_id=approver_id,
                note="first approval recorded; a distinct second approver is required",
            )
        await self._submitted(approver_id, request_id, decision="approve")
        return ApprovalActionResult(
            status=outcome.status.value, request_id=request_id, approver_id=approver_id
        )

    async def _deny(self, approver_id: str, request_id: str) -> ApprovalActionResult:
        outcome = await self._gate.reject(
            request_id, approver=approver_id, reason="denied from slack"
        )
        await self._submitted(approver_id, request_id, decision="deny")
        status = "denied" if outcome.status is ApprovalStatus.REJECTED else outcome.status.value
        return ApprovalActionResult(status=status, request_id=request_id, approver_id=approver_id)

    # -- audit helpers ---------------------------------------------------------

    async def _unauthorized(
        self, *, actor: str, request_id: str, reason: str, error_class: str | None = None
    ) -> None:
        detail: dict[str, str] = {"reason": reason, "request_id": request_id}
        if error_class is not None:
            detail["error_class"] = error_class
        await self._emit("chat.approval.unauthorized", AuditOutcome.denied, actor, detail)

    async def _stale(self, *, actor: str, request_id: str, error_class: str) -> None:
        await self._emit(
            "chat.approval.stale",
            AuditOutcome.denied,
            actor,
            {"reason": "stale", "request_id": request_id, "error_class": error_class},
        )

    async def _submitted(self, actor: str, request_id: str, *, decision: str) -> None:
        await self._emit(
            "chat.approval.submitted",
            AuditOutcome.success,
            actor,
            {"decision": decision, "request_id": request_id},
        )

    async def _emit(
        self, action: str, outcome: AuditOutcome, actor: str, detail: dict[str, str]
    ) -> None:
        event = new_audit_event(
            action=action,
            resource="chat:approval",
            outcome=outcome,
            actor=actor,
            detail=json.dumps(detail),
            provenance={"source": "ne-chat", "component": "approvals", "surface": self._surface},
        )
        await self._sink.append(event)
