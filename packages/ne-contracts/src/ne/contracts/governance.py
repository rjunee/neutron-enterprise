# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Governance domain Protocols (SPEC section 4, constraints C6 / C12).

Policy decisions, deterministic guardrails, redaction, tamper-evident audit,
and human approval. Guardrail/Redactor are deliberately **sync** — they are
code-enforced, deterministic checks that run BEFORE any model judgment (C12).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from ne.contracts.events import AuditEventLike
from ne.contracts.frozen import AnyMap, FrozenDict
from ne.contracts.types import ApprovalTier, Principal


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class DecisionEffect(StrEnum):
    """The effect a policy decision imposes."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require-approval"


class ApprovalStatus(StrEnum):
    """Terminal (or escalated) state of an approval request."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ESCALATED = "escalated"


class PolicyRequest(_Frozen):
    """A request for an authorization decision."""

    principal: Principal
    action: str
    resource: str
    context: AnyMap = FrozenDict()


class PolicyDecision(_Frozen):
    """The PDP's answer, stamped with the policy bundle version."""

    effect: DecisionEffect
    policy_version: str
    reason: str = ""
    obligations: AnyMap = FrozenDict()
    tier: int | None = None
    """The tier the policy bundle DECIDED for this request (SPEC §4.2 ladder 0-3).

    ``None`` when the bundle did not classify the action, or when a pre-1.2.0
    producer built the decision. Consumers MUST treat it as a gating FLOOR, never
    a ceiling: it can only ESCALATE the effective approval tier (stricter-of),
    never lower what the spec/mirror already require (added 1.2.0, ADR-0004).
    """
    approval_window_seconds: float | None = None
    """How long a human has to answer this decision's approval, in seconds —
    the POLICY's per-action answer, carried first-class (added 1.27.0, ADR-0030).

    Populated by the policy backend on ``REQUIRE_APPROVAL`` decisions only
    (``tiers.rego`` resolves it most-specific-first: exact action override →
    per-tier override → built-in floor; the Cedar evaluator mirrors the same
    resolution in its marshaller). ``None`` means the policy named no window —
    the deployment default applies — and MUST NEVER be read as "expire
    immediately". This is deliberately NOT an obligation: ``obligations`` is the
    Rego/Cedar parity surface and Cedar obligations are static per-rule
    annotations, so a data-resolved per-action value cannot live there (the
    exact CI-caught error that preceded this field; see ADR-0030). Owner
    requirement 2026-08-30: the window is a property of the ACTION, set beside
    its risk tier in policy — never a hard-coded constant, and never a side
    effect of how long a login happens to last.
    """

    approval_window_declared: bool = False
    """Did the DEPLOYMENT declare this window, or is it a shipped floor? (1.28.0)

    ``True`` only when ``approval_window_seconds`` above came from
    ``data.ne.config.approval_windows.actions[<action>]`` or ``.tiers[<tier>]``
    — a number somebody wrote down for this deployment. ``False`` for
    ``tiers.rego``'s built-in floor (4 h tier-2 / 24 h tier-3), for a pre-1.4.0
    bundle that does not emit the bit, and whenever the window itself is
    ``None``.

    ONE CONSUMER, AND THE DISTINCTION IS THE WHOLE POINT.
    ``ne.runtime.middleware._require_honourable_approval_window`` refuses a
    canonical Run park whose window outruns the run owner's authorization
    horizon. That refusal buys legibility, not safety — a park that outruns the
    horizon executes nothing unsafe; its worst case is the already-handled
    ``authorization_expired`` terminalization, its best case succeeds — so it
    must not cost successes. A floor is not a promise anyone made: refusing on
    one punishes an unmade decision. So the guard fires on ``True`` and, on
    ``False``, warns and proceeds exactly as it did before the check existed.
    """

    standing_approval_seconds: float | None = None
    """How long this decision, once MADE, stays usable after the attempt it was
    asked for terminalized ``authorization_expired`` — in seconds (added
    1.29.0, ADR-0030 amended for the F1 standing-approval carryover).

    A DIFFERENT clock from ``approval_window_seconds`` and never a substitute
    for it. The window is measured from PARK and bounds how long the human has
    to ANSWER; this is measured from the DECISION and bounds how long that
    answer outlives the attempt. Past it the standing approval lapses
    (``approval_lapsed``) and authorizes nothing — the owner's next attempt
    parks for a fresh human decision.

    Populated by the policy backend on ``REQUIRE_APPROVAL`` decisions only
    (``tiers.rego``'s ``standing_approval_seconds``, resolved most-specific-
    first exactly like the window: exact action override → per-tier override →
    built-in floor; the Cedar evaluator mirrors the same resolution). Carried
    first-class for the same reason the window is: ``obligations`` is the
    Rego/Cedar parity surface and Cedar obligations are static per-rule
    annotations that cannot express a data-resolved per-action value.

    ``None`` means the policy named no standing lifetime. The runtime reads
    that as "mint NO standing approval", not as "standing forever" — an
    unbounded carryover is the one reading this field must never have.
    """

    @field_validator("approval_window_seconds", "standing_approval_seconds", mode="before")
    @classmethod
    def _window_is_a_real_number(cls, value: object) -> object:
        # isinstance(True, int) is True in Python and pydantic's lax mode
        # coerces bool→float, so without this a producer bug passing ``True``
        # would mint a ONE-SECOND window — an approval that expires before a
        # human can read it, the exact failure this field exists to remove.
        # Strings are the same class of defect: lax mode coerces "600" to
        # 600.0, so a producer that serialised the window as text would mint a
        # window nobody typed as a number. The policy backends emit JSON
        # numbers or nothing; anything else is a producer bug and must fail
        # loudly here, not be laundered into a deadline.
        #
        # ``standing_approval_seconds`` (1.29.0) shares this validator because
        # it shares every one of those hazards: a coerced ``True`` would mint a
        # ONE-SECOND standing lifetime, which lapses the decision before the
        # owner can possibly re-authenticate.
        if isinstance(value, bool | str):
            raise ValueError(
                "approval_window_seconds/standing_approval_seconds must be a number or "
                "None, not a bool or str"
            )
        return value

    @field_validator("approval_window_declared", mode="before")
    @classmethod
    def _declared_is_a_real_bool(cls, value: object) -> object:
        # pydantic's lax mode coerces "false"/"0"/0 to False and "true"/1 to
        # True. A producer that serialised this bit as text would therefore be
        # laundered into a REFUSAL decision, which is the one direction this
        # field must never drift in by accident. Only a JSON boolean counts —
        # the same type-strictness ``tiers.rego`` applies to its own inputs.
        if not isinstance(value, bool):
            raise ValueError("approval_window_declared must be a bool")
        return value


class GuardrailVerdict(_Frozen):
    """Result of a deterministic guardrail check."""

    passed: bool
    rule: str
    detail: str = ""


class RedactionResult(_Frozen):
    """Redacted text plus the categories that were redacted."""

    text: str
    redactions: tuple[str, ...] = ()


class AuditVerification(_Frozen):
    """Result of verifying the tamper-evidence of an audit range."""

    valid: bool
    checked: int
    detail: str = ""


class ApprovalRequest(_Frozen):
    """A human-approval request at a given tier."""

    tier: ApprovalTier
    action: str
    principal: Principal
    payload: AnyMap = FrozenDict()


class ApprovalOutcome(_Frozen):
    """The decided outcome of an approval request."""

    status: ApprovalStatus
    approver: str | None = None
    decided_at: datetime | None = None
    decision_event_id: str | None = None
    """The ``event_id`` of the decision ``AuditEvent`` (``approval.granted`` /
    ``approval.rejected``) that the deciding gate call appended to the
    tamper-evident chain (ADR-0017, 1.14.0). ``None`` when the outcome was
    rehydrated from the store (``await_outcome`` polling / ``_to_outcome``)
    rather than built directly by the call that appended the record, or when
    built by a pre-1.14.0 producer.
    """
    approval_id: str | None = None
    """The durable request id of the approval this outcome decided (ADR-0019,
    1.16.0) — the same id ``approval.*`` chain records carry in provenance.
    Unlike ``decision_event_id`` it is stable row state, so EVERY path that
    builds an outcome from a known request (direct decision or store
    rehydration) populates it. ``None`` only when the producer holds no
    durable request at all (a pre-1.16.0 producer, or an in-memory test gate)
    — a consumer stamping the id onward MUST omit it then, never invent one
    (the B14 execution→approval join reads this field).
    """


class PolicyDecisionPoint(Protocol):
    """C6 — every tool call passes the PDP; the decision carries policy_version."""

    async def decide(self, request: PolicyRequest) -> PolicyDecision: ...


class Guardrail(Protocol):
    """C12 — deterministic, code-enforced check that runs BEFORE model judgment.

    Sync on purpose: guardrails must be fast and side-effect-free predicates.
    """

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict: ...


class Redactor(Protocol):
    """C12 — deterministic PII/secret redaction, sync on purpose."""

    def redact(self, text: str, *, categories: Sequence[str] | None = None) -> RedactionResult: ...


class AuditSink(Protocol):
    """Append-only, tamper-evident audit log."""

    async def append(self, event: AuditEventLike) -> None: ...

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification: ...


class ApprovalGate(Protocol):
    """Human-in-the-loop approval (SPEC section 4.2 semantics).

    Tier 2/3 requests **never** auto-approve; an expired approval requires a
    fresh re-request; a request pending past its SLA escalates and notifies.
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome: ...
