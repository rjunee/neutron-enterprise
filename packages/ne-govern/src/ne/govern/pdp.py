# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""GovernedPdp — the ONLY PolicyDecisionPoint, with audit coupled by construction.

C6 (CORE): every decision passes the PDP AND emits an audit event, with no
bypass path. :class:`GovernedPdp` structurally implements the frozen
:class:`ne.contracts.governance.PolicyDecisionPoint`. It:

  * has NO sink-less constructor — the ``AuditSink`` is a required positional
    argument (mirrored by the required ``sink=`` kwarg on the NE-033 factory seam);
  * appends the ``AuditEvent`` BEFORE returning the decision — if the append
    fails, :class:`~ne.govern.errors.AuditEmissionError` is raised and NO decision
    escapes (fail closed);
  * on an OPA backend failure, audits an ``error`` outcome and then re-raises
    :class:`~ne.govern.errors.PolicyBackendError` (fail closed both ways);
  * translates the private ``_RawOpaResult`` into a ``PolicyDecision`` only along
    this audited path — ``OpaClient`` cannot mint a decision on its own.

REDACTION (ABI lift-pattern, teardown §3.8): ``request.context`` contents NEVER
appear in the audit ``detail`` or ``provenance`` — only effect / reason / tier /
policy_version / requested_action / resource travel to the sink.

TIER AUTHORITY (NE-CKD3, ADR-0004): the OPA-decided ``tier`` (``raw.tier``) — the
same int already recorded in the audit ``detail`` — now also travels on the
returned ``PolicyDecision.tier`` so the runtime HITL gate can compose stricter-of
and never under-tier what the bundle decided. ``obligations`` (second_check /
named_approver / per_item flags) rides the same decision.

RUN-TRACE THREADING (NE-DEMO-PDP-TRACE, NE-166): the ``pdp.decide`` event carries
``trace_id=current_run_trace_id()`` — the ambient run-scoped correlation id — so
the decision appears on the SAME trace as the tool execution it authorized. The
carrier is :mod:`ne.events.correlation`, NOT :mod:`ne.runtime.correlation`:
``ne-runtime`` depends on ``ne-govern``, so reading the id from the runtime layer
would be a circular package dependency (which is exactly why every ``pdp.decide``
event used to carry a null trace id). ABSENCE STAYS ABSENT: a decision taken
outside any run scope — a bare API call, a preflight, a health probe — stamps
``None``, and no code path here mints or defaults an id, because a fabricated id
would write a false association into the hash-chained audit record.
"""

from __future__ import annotations

import json

from ne.contracts.governance import (
    AuditSink as AuditSink,
)
from ne.contracts.governance import (
    DecisionEffect,
    PolicyDecision,
    PolicyRequest,
)
from ne.events import new_audit_event
from ne.events.correlation import current_run_trace_id
from ne.govern.config import GovernSettings
from ne.govern.errors import AuditEmissionError, PolicyBackendError
from ne.govern.opa import OpaClient
from ne.schema import AuditOutcome

_OUTCOME_BY_EFFECT: dict[DecisionEffect, AuditOutcome] = {
    DecisionEffect.ALLOW: AuditOutcome.success,
    # REQUIRE_APPROVAL renders a decision (the human gate is NE-035's job); the
    # effect itself travels in provenance/detail. Not a denial.
    DecisionEffect.REQUIRE_APPROVAL: AuditOutcome.success,
    DecisionEffect.DENY: AuditOutcome.denied,
}


class GovernedPdp:
    """The audited PDP: OPA decision + mandatory audit append, coupled by design."""

    def __init__(self, opa: OpaClient, sink: AuditSink) -> None:
        self._opa = opa
        self._sink = sink

    @classmethod
    def from_settings(cls, settings: GovernSettings, *, sink: AuditSink) -> GovernedPdp:
        opa = OpaClient(
            settings.opa_url,
            decision_path=settings.decision_path,
            timeout_s=settings.timeout_s,
        )
        return cls(opa, sink)

    async def decide(self, request: PolicyRequest) -> PolicyDecision:
        input_doc = request.model_dump(mode="json")
        try:
            raw = await self._opa.evaluate(input_doc)
        except PolicyBackendError as exc:
            # Fail closed: audit the backend failure, then re-raise. If the audit
            # append ALSO fails, AuditEmissionError (chained) propagates instead.
            await self._append_audit(
                request,
                outcome=AuditOutcome.error,
                effect="error",
                policy_version="unavailable",
                reason=str(exc),
                tier=None,
            )
            raise
        decision = PolicyDecision(
            effect=DecisionEffect(raw.effect),
            policy_version=raw.policy_version,
            reason=raw.reason,
            obligations=raw.obligations,
            tier=raw.tier,
            # ADR-0030: the policy-resolved per-action approval window, carried
            # first-class (NOT an obligation — that is the static Cedar parity
            # surface). None when the bundle named no window; the runtime then
            # applies the deployment default, never "expire immediately".
            approval_window_seconds=raw.approval_window_seconds,
            # Bundle 1.4.0: whether that window was DECLARED by this
            # deployment or is the shipped floor. Carried, never derived —
            # the resolution order lives in the policy engine.
            approval_window_declared=raw.approval_window_declared,
            # Bundle 1.5.0 (F1): how long that decision outlives the attempt
            # it was asked for. A DIFFERENT clock from the window and never a
            # substitute for it. None ⇒ mint no standing approval.
            standing_approval_seconds=raw.standing_approval_seconds,
        )
        # Audit BEFORE returning: a decision that cannot be recorded is withheld.
        await self._append_audit(
            request,
            outcome=_OUTCOME_BY_EFFECT[decision.effect],
            effect=raw.effect,
            policy_version=raw.policy_version,
            reason=raw.reason,
            tier=raw.tier,
        )
        return decision

    async def _append_audit(
        self,
        request: PolicyRequest,
        *,
        outcome: AuditOutcome,
        effect: str,
        policy_version: str,
        reason: str,
        tier: int | None,
    ) -> None:
        # Event construction (incl. config_hash via the FROZEN process hash) is
        # OUTSIDE the try: ConfigHashNotFrozenError must propagate, not be
        # swallowed into AuditEmissionError (plan §1.4 / SPEC §4).
        # NE-DEMO-PDP-TRACE: stamp the ambient run-scoped correlation id so the
        # decision lands on ITS OWN run's trace, next to the tool execution it
        # authorized. ``current_run_trace_id()`` is ``None`` outside any run
        # (a bare API call, a preflight, a health probe) and that ``None`` is
        # STAMPED AS-IS — an absent trace is the honest value, and inventing an
        # id here would write a FALSE association into the hash-chained record.
        event = new_audit_event(
            action="pdp.decide",
            resource=request.resource,
            outcome=outcome,
            actor=request.principal.id,
            detail=json.dumps(
                {
                    "effect": effect,
                    "reason": reason,
                    "tier": tier,
                    "policy_version": policy_version,
                }
            ),
            trace_id=current_run_trace_id(),
            provenance={
                "source": "ne-govern",
                "policy_version": policy_version,
                "requested_action": request.action,
                "effect": effect,
            },
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise AuditEmissionError(
                f"audit append failed for pdp.decide on {request.resource!r}; "
                "decision withheld (C6 fail closed)"
            ) from exc

    async def aclose(self) -> None:
        """Close the owned OPA client."""
        await self._opa.aclose()
