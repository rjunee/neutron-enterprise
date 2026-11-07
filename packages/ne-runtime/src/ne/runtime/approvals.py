# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DurableApprovalGate — the production HITL gate that FULFILLS a REQUIRE_APPROVAL.

The NE-031 PDP *returns* ``require-approval``; the NE-033 middleware *routes* it
here; this gate *fulfils* it. A tool call awaiting approval does NOT execute until
a named human approves. Requests are persisted (``SqlAlchemyApprovalStore``) so
they SURVIVE RESTARTS.

SPEC §4.2 lifecycle::

    submit ─► pending ──────────────► approved  (Tier-2: one named approver)
                 │  └► pending_second ► approved  (Tier-3: named + distinct 2nd)
                 ├─(> SLA)─► escalated (flag; still approvable — notify via events)
                 ├─(reject)─► rejected  (terminal, denied)
                 └─(> hard expiry)─► expired  (terminal, denied; re-request required)

NEVER auto-approve (Tier-2/3), proven at FOUR layers:

    1. table CheckConstraint  — an approver-less ``approved`` row is unrepresentable;
    2. store.decide           — raises ApprovalIntegrityError BEFORE any SQL;
    3. gate policy (here)      — a named approver is required; Tier-3 additionally
       requires membership in ``tier3_named_approvers`` PLUS a distinct second
       approver, and an EMPTY configured list makes Tier-3 unapprovable (fail closed);
    4. _to_outcome            — refuses to build an APPROVED outcome with no approver.

Every decision emits a config_hash-stamped ``AuditEvent`` through the frozen
``AuditSink`` (fail closed — a sink append failure raises ``RuntimeAuditError`` and
the result is withheld) PLUS a typed ``ApprovalEvent`` into the ne-events outbox in
the SAME transaction as the row write. Redaction discipline: audit detail /
provenance carry only ids, tiers, statuses, approver ids, and error classes —
never raw argument/recipient values. **The persisted ``payload`` column is an
exception to that same claim, by owner ruling 2026-07-31**: it now MAY embed a
``disclosure`` object (target resource, description, raw tool-call arguments as
``material_parameters``, and a tier-derived reversibility label) — see
:meth:`DurableApprovalGate.read_payload` below. An approver who cannot see what
they are approving is not approving anything (the demo defect this unit closes);
disclosure is served EXCLUSIVELY through that governed, audited, named-approver-gated
read — never through :meth:`list_pending`, which stays byte-identical
payload-free (the queue LISTING redaction is the real security property; keeping
the payload off it is unrelated to, and unweakened by, this ruling).

**Decision-record linkage (ADR-0017, NE-APPROVAL-LINKAGE).** Every decision
record's provenance carries the deciding request's ``approval_id`` (mirroring
``approval.requested``'s existing key), and ``_audit`` returns the appended
record's ``event_id``, which ``_grant``/``reject`` thread onto
``ApprovalOutcome.decision_event_id`` — so a decision chain record is always
linkable back to the request it decided.

**Outcome→request linkage (ADR-0019, B14).** Every ``ApprovalOutcome`` this
gate builds from a known request — ``_grant``, ``reject``, ``_to_outcome``,
and ``await_outcome``'s inline EXPIRED outcome — carries that request's
durable id on ``ApprovalOutcome.approval_id``. Unlike ``decision_event_id``
(populated only by the call that performed the chain append), the request id
is stable row state, so rehydration paths populate it too. The C6 executor
reads it to stamp the execution→approval join onto ``tool.execute`` at the
execution site — see ``middleware.py`` and
``tests/test_approval_execution_join.py``.

Escalation is the §4.2 'escalate + notify': the emitted ``ApprovalEvent(escalated)``
plus ``approval.escalated`` AuditEvent ARE the notification spine — console/Slack
surfaces (NE-062/063) consume them. Escalation is a flag, not a terminal outcome;
an escalated request stays actionable until hard expiry.

**Expiry is read-driven as well as workflow-driven.** Every gate surface that can
expose or act on a pending row (scoped reattachment, queue listing, payload read,
approve, reject) compares the persisted ``expires_at`` with this server's injected
clock and drives the same terminal expiry CAS before returning. A dead waiter can
therefore leave no immortal actionable row behind, and no browser clock is trusted
to hide stale server truth. The existing store CAS still totally orders expiry
against a concurrently landing human decision.

**Idempotent submission (NE-INV-APPROVAL-SCOPE, invariant-audit Finding A).**
:meth:`DurableApprovalGate.submit` is idempotent per ``(principal_id, action,``
``tier, action_scope, args_hash)`` — the ambient
:func:`~ne.runtime.correlation.current_action_scope` PLUS a per-invocation
``args_hash`` discriminator: inside a scoped turn, a DBOS step re-run reattaches
to the existing non-terminal request instead of minting a duplicate row, closing
the orphan a crash mid-``await_outcome`` used to leave forever in the queue.
``tier`` is part of the key — a same-action request at a DIFFERENT tier (e.g.
amount-based tiering) never reattaches to a lower-tier pending row, so it can
never inherit a weaker approval policy (Codex review finding C1). Outside any
ambient scope, behavior is byte-identical to before this unit (a fresh
``uuid4()`` per call). Pinned by ``packages/ne-runtime/tests/test_approval_scope.py``.

**Per-invocation discriminator (Argus re-check P1, round 2).** ``pydantic-ai``
dispatches a single turn's tool calls concurrently via ``asyncio.create_task``
with no sequential guard for governed tools (confirmed; there is no ordering
barrier between distinct tool invocations in one turn). Without a discriminator
narrower than ``(principal_id, action, tier, action_scope)``, two GENUINELY
DISTINCT concurrent calls (same action/tier/principal/scope, different
arguments) would collapse onto ONE approval row — so a single human decision (or
one Tier-3 two-approver ceremony) would authorize BOTH external writes. The
write-dedup layer (``action_tools.py``, keyed on ``payload_hash``) does NOT
catch this: the two calls have different payloads, so both writes actually
execute once approved. :func:`canonical_args_hash` (mirroring
``action_tools.py``'s ``_payload_hash`` byte-for-byte in shape: canonical
``json.dumps(..., sort_keys=True)`` then ``sha256`` hex digest) closes this by
widening the claim-or-find key to ``(principal_id, action, tier, action_scope,``
``args_hash)``. A crash-retry of the SAME call has the SAME args -> SAME hash ->
reattaches (Finding A stays closed); two distinct concurrent calls have
DIFFERENT args -> DIFFERENT hashes -> two independent rows, each requiring its
own human decision (Argus P1 closed). Pinned by
``test_same_scope_different_args_not_merged``,
``test_same_scope_same_args_hash_reattaches``, and
``test_concurrent_distinct_calls_get_independent_approvals``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from ne.contracts import get_config_hash
from ne.contracts.frozen import thaw
from ne.contracts.governance import (
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalStatus,
    AuditSink,
)
from ne.contracts.source_authority import ProtectedSourceInvocationRef
from ne.events import new_approval_event, new_audit_event
from ne.runtime.approval_metrics import ApprovalMetrics
from ne.runtime.approval_store import (
    SOURCE_WITHHELD_STATUS,
    TERMINAL_STATUSES,
    SqlAlchemyApprovalStore,
)
from ne.runtime.config import ApprovalSettings, RuntimeSettings
from ne.runtime.correlation import (
    ConversationAttemptBinding,
    current_action_scope,
    current_approval_window_s,
    current_conversation_attempt,
)
from ne.runtime.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalIntegrityError,
    ApprovalPolicyError,
    ApprovalRequestNotFoundError,
    ApprovalSourceWithheldFromApproverError,
    RuntimeAuditError,
)
from ne.runtime.source_results import (
    MalformedProtectedReferenceError,
    ProtectedSourceResultGuard,
    SourceAuthorityRetryableError,
    SourceAuthorityWithheldError,
    decode_invocation_reference,
)
from ne.schema import ApprovalDecision, AuditOutcome

#: Bounded claim-or-find retries in :meth:`DurableApprovalGate.submit` (mirrors
#: action_tools.py's ``_CLAIM_MAX_ATTEMPTS``): a lost race against the partial
#: unique index re-finds the winner's row on the next iteration. Unreachable in
#: practice; bounded so pathological churn fails loud instead of spinning forever.
_SUBMIT_CLAIM_MAX_ATTEMPTS = 3

_PROVENANCE_SOURCE = {"source": "ne-runtime", "component": "approval-gate"}


class PendingApproval(BaseModel):
    """A read-only view of a pending approval (the console-queue surface).

    ``provisional_source_count``/``provisional_source_refs`` (NE-T2X-5, design
    §6.3) are ADDITIVE, defaulted fields (this model is ne-runtime's own
    read-model, not a frozen ne-contracts type — C5 is unaffected): a row whose
    approval was escalated by provisional context carries the count and the
    (opaque, identifier-only — C12) evidence refs so the approvals queue can
    render "N unconfirmed LLM-extracted fact(s)" WITHOUT the approver ever
    seeing fact content — only the typed refs, deep-linkable to the governed
    NE-066 steward surface. A row with nothing provisional carries the
    defaults (0, ()) — display-only; the GATING never depends on this parse
    (the tier already escalated at DECISION time, in the PDP — see
    ne.runtime.middleware)."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    action: str
    tier: int
    principal_id: str
    # Digest-only discriminator for correlating a caller with the pending row
    # created for its exact arguments. No material parameter values are exposed.
    args_hash: str | None = None
    requested_at: datetime
    sla_deadline: datetime
    expires_at: datetime
    escalated: bool
    provisional_source_count: int = 0
    provisional_source_refs: tuple[str, ...] = ()


class ApprovalPayloadDisclosure(BaseModel):
    """The governed, per-request disclosure read (owner ruling 2026-07-31).

    Returned ONLY by :meth:`DurableApprovalGate.read_payload` to the NAMED
    approver(s) for the specific request — never joined onto
    :meth:`DurableApprovalGate.list_pending`. Runtime-owned read-model (mirrors
    the :class:`PendingApproval` precedent): NOT an ``ne-contracts`` frozen type,
    so C5 is unaffected by this unit.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str
    action: str
    tier: int
    principal_id: str
    target_resource: str
    description: str
    material_parameters: dict[str, Any] | None
    reversibility: str


def reversibility_label(tier: int) -> str:
    """The SPEC §4.2 action-class reversibility string for ``tier``.

    Tier >= 3 (financial/legal): irreversible. Tier == 2 (external-facing /
    PII-touching): reversal may require external remediation. Tier 0-1
    (internal): reversible. Pure function of tier — never derived from model
    output (C12: deterministic, not model judgment).
    """
    if tier >= 3:
        return "irreversible — financial/legal action class (SPEC §4.2 Tier-3)"
    if tier == 2:
        return (
            "external-facing / PII-touching — reversal may require external "
            "remediation (SPEC §4.2 Tier-2)"
        )
    return "reversible — internal action class (SPEC §4.2 Tier 0-1)"


def _as_utc(value: datetime) -> datetime:
    """Normalize a store-read datetime to tz-aware UTC (SQLite returns naive)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def resolve_approval_deadlines_s(cfg: ApprovalSettings, tier: int) -> tuple[float, float]:
    """Return ``(sla_s, expiry_s)`` for a new pending request, window-aware.

    ``expiry_s`` honours the ambient policy-resolved approval window
    (``approval_window_scope``) when one is bound and positive, else the
    deployment-wide ``cfg.expiry_s`` — the window is AUTHORITATIVE for hard
    expiry (owner requirement 2026-08-30: how long a human has to answer is the
    ACTION's per-policy property; a configured 600 s window expires at 600 s,
    never later).

    ``sla_s`` is the tier's configured escalation SLA, DERIVED DOWN to half the
    effective window whenever it would not fall STRICTLY inside it. Rationale:

    * SPEC §4.2 is normative on BOTH deadlines: ``pending > SLA → escalate +
      notify`` and hard expiry with re-request. ``await_outcome`` checks expiry
      before the SLA, so ``sla_deadline >= expires_at`` silently deletes
      escalation — the default tier-2 policy window (14400 s, ``tiers.rego``
      floor) is exactly ``ApprovalSettings.tier2_sla_s``, so before this
      function EVERY default tier-2 approval expired unescalated.
    * Between the two knobs, the policy window wins: it is per-action policy
      configuration; the SLA is a deployment-wide runtime setting. Moving hard
      expiry past the SLA instead would make a configured short window a lie
      (600 s living past 4 h).
    * Half the window (not ``expiry - ε``) so the escalation notification
      always has meaningful time to be acted on before expiry, at every window
      size — escalating one tick before expiry preserves the letter of §4.2
      and none of its purpose.

    A configured SLA already strictly inside the window is honoured unchanged.
    """
    window_s = current_approval_window_s()
    expiry_s = float(window_s if window_s is not None and window_s > 0 else cfg.expiry_s)
    sla_s = float(cfg.tier3_sla_s if tier >= 3 else cfg.tier2_sla_s)
    if sla_s >= expiry_s:
        sla_s = expiry_s / 2.0
    return sla_s, expiry_s


def canonical_args_hash(data: Mapping[str, Any]) -> str:
    """A deterministic, key-order-independent digest of ``data`` (Argus P1, round 2).

    Mirrors ``packages/ne-runtime/src/ne/runtime/action_tools.py::_payload_hash``
    byte-for-byte in SHAPE (read that function first): canonical
    ``json.dumps(dict(data), sort_keys=True)`` then a ``sha256`` hex digest. The
    source data type differs (here: the RAW tool-call ``args`` mapping, computed
    BEFORE validation; there: a validated ``ActionInput`` model's ``model_dump``),
    but the hashing shape must match exactly for consistency with the established
    pattern. Used as the per-invocation discriminator on the approval claim key so
    two distinct concurrent same-action/tier/principal/scope calls never collapse
    onto one approval row (see the module docstring). Pinned by
    ``test_canonical_args_hash_is_key_order_independent``.
    """
    # thaw(), not dict(): a shallow dict leaves nested FrozenDicts in place and
    # json.dumps raises TypeError on them. `submit()` calls this on
    # `request.payload` as the scoped-dedup fallback, and the payload now carries
    # a nested `disclosure` block — so the shallow copy that was fine for a flat
    # args mapping is not fine for the shape this unit introduced. The identical
    # bug forty lines below was fixed with thaw() in this same unit; this call
    # site was missed. Unreachable from GovernedToolExecutor (it always supplies
    # an explicit args_hash) but the README documents the fallback for direct
    # callers, so it is reachable by a supported path.
    canonical = json.dumps(thaw(data), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# PRE-015A0-ENFORCEMENT: when an approval's arguments are source-derived the
# disclosure carries ONLY this opaque reference under this key; the material
# parameters live in the producer's encrypted invocation row and are re-authorized
# at every disclosure and decision. Its presence is what makes a request protected.
PROTECTED_INVOCATION_KEY: Final = "protected_invocation_ref"

#: The stored terminal reason for a drift-withheld request. Names the fact, never a
#: source, artifact, principal or argument value. Only a
#: successfully admitted authority fact proving ACL contraction/revocation,
#: authoritative deletion or source-generation replacement may author the
#: ``source_withheld`` terminal — evaluated for the REQUEST'S OWN execution
#: principal, never for whichever approver happened to read or decide (U2
#: adversarial review, standalone-gate ruling).
SOURCE_WITHHELD_REASON: Final = (
    "an admitted source-authority change permanently denies this request's "
    "arguments for its execution principal"
)

# The fixed shell a terminally drift-withheld protected request discloses.
# Byte-stable, names no source, artifact, principal or argument — and asserts no
# revocation HISTORY ("no longer" was a false claim for a reader who was never
# granted access; U2 adversarial review, standalone-gate ruling).
PROTECTED_ARGUMENTS_WITHHELD: Final = (
    "The arguments for this request are withheld: current source authorization "
    "does not permit disclosing them."
)


def _protected_reference_from(disclosure: object) -> ProtectedSourceInvocationRef | None:
    """Return the protected invocation reference a stored disclosure carries.

    A value under :data:`PROTECTED_INVOCATION_KEY` that does not decode to an
    invocation reference is a malformed protected request, not a source-free one:
    it raises rather than degrading to "no protection".
    """

    if not isinstance(disclosure, Mapping):
        return None
    raw = disclosure.get(PROTECTED_INVOCATION_KEY)
    if raw is None:
        return None
    reference = decode_invocation_reference(raw)
    if reference is None:
        raise MalformedProtectedReferenceError(
            "a protected approval row carries a value that is not a protected reference"
        )
    return reference


#: The separation-of-duties refusal message, ONE string for every enforcement
#: site (the gate below and ``ne.api.routers.approvals``' run-join branch, which
#: decides through the checkpoint coordinator rather than through this gate).
SELF_APPROVAL_REFUSED = (
    "the principal who requested this approval may not decide it "
    "(separation of duties); a different named human must approve"
)


def refuse_self_approval(*, requester_id: str, approver: str) -> None:
    """Separation of duties: the REQUESTER of an approval may never grant it.

    Tier 2 means ONE named approval, and nothing else in the ladder checked WHO
    that approver was relative to the requester — only the Tier-3 SECOND approver
    was ever checked for distinctness (:meth:`DurableApprovalGate.approve`). So a
    principal holding both the requesting capability and the ``approver`` role
    could approve its own Tier-2 write: the air-gap lift
    (``write.external.network.egress.expand``), an external send, anything. The
    role split that made that unreachable on the demo box is a role ASSIGNMENT,
    not a rule — ``docs/ops/air-gap-recovery.md`` promises the rule.

    Empty ids never match: a row with no recorded requester (or a caller with no
    id) is refused by the surrounding authorization, not silently by identity
    equality.
    """

    if requester_id and approver and requester_id == approver:
        raise ApprovalPolicyError(SELF_APPROVAL_REFUSED)


def _decision_attempt_id(request_id: str, approver: str, operation: str) -> str:
    """Deterministic per-(request, approver, operation) decision attempt id.

    Deterministic on purpose: a retried disclosure or decision claim-or-loads the
    SAME served receipt rather than minting a second one for the same intent.
    """

    return hashlib.sha256(
        b"".join(
            len(part.encode()).to_bytes(8, "big") + part.encode()
            for part in ("ne.approval-decision-attempt.v1", request_id, approver, operation)
        )
    ).hexdigest()


def _parse_stored_payload(payload: str) -> Mapping[str, Any]:
    """Best-effort JSON parse of a stored approval row's ``payload`` column.

    Display-only channel (NE-T2X-5, design §6.3): the row was already written
    by :meth:`DurableApprovalGate._submit_fresh` (a controlled, first-party
    JSON dump), so malformed content here would only ever come from a legacy
    row shape — never from an attacker. A parse failure or a non-object
    result degrades to an EMPTY mapping (both count/refs default to their
    zero values below) rather than raising; the GATING decision was already
    made at PDP-decide time and never re-derives from this column.
    """
    try:
        parsed = json.loads(payload)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _provisional_count_from_payload(payload: str) -> int:
    """A stored payload's ``provisional_source_count``, coerced non-negative int.

    Missing/malformed -> 0 (no provisional context to display); never raises.
    """
    raw = _parse_stored_payload(payload).get("provisional_source_count")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw >= 0 else 0


def _provisional_refs_from_payload(payload: str) -> tuple[str, ...]:
    """A stored payload's ``provisional_source_refs``, filtered to ``str`` entries.

    Missing/malformed/non-list -> ``()``; any non-string entry is dropped
    rather than raising (display-only — see :func:`_parse_stored_payload`).
    """
    raw = _parse_stored_payload(payload).get("provisional_source_refs")
    if not isinstance(raw, list):
        return ()
    return tuple(ref for ref in raw if isinstance(ref, str))


class DurableApprovalGate:
    """The production, durable, tiered, never-auto-approve HITL gate (SPEC §4.2).

    A request whose stored disclosure carries :data:`PROTECTED_INVOCATION_KEY` is
    source-derived: its arguments live only in the producer's encrypted invocation
    row and are re-authorized through the injected
    :class:`~ne.runtime.source_results.ProtectedSourceResultGuard` before disclosure
    and before either decision CAS. A reader or decider whose own authority does not
    admit the invocation is refused (typed, non-persisting) and the request stays
    pending for another approver; a retryable unknown writes nothing terminal and
    leaves the request pending. The ``source_withheld`` terminal may be authored
    only by a genuine authority decision under the request's own execution
    principal — no such path exists in this gate today.
    Exact behavior is pinned by
    ``tests/test_approval_payload_read.py`` and
    ``tests/test_approval_decision_linkage.py``, not by this prose.
    """

    def __init__(
        self,
        *,
        store: SqlAlchemyApprovalStore,
        sink: AuditSink,
        settings: RuntimeSettings | None = None,
        metrics: ApprovalMetrics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        source_guard: ProtectedSourceResultGuard | None = None,
    ) -> None:
        self._store = store
        self._sink = sink
        self._settings = settings if settings is not None else RuntimeSettings()
        self._metrics = metrics if metrics is not None else ApprovalMetrics()
        self._clock = clock
        # PRE-015A0-ENFORCEMENT: re-authorizes a source-derived request's protected
        # arguments before disclosure and before either decision CAS. None ⇒ this
        # gate cannot disclose a protected request AT ALL (it is not a permission to
        # skip the check), which is what keeps an unwired composition fail-closed.
        self._source_guard = source_guard

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    def _attempt_tuple(
        binding: ConversationAttemptBinding | None,
    ) -> tuple[str, str, str, str, str, str] | None:
        if binding is None:
            return None
        return (
            binding.conversation_id,
            binding.department,
            binding.agent_name,
            binding.attachment_id,
            binding.attempt_id,
            binding.trace_id,
        )

    async def submit(self, request: ApprovalRequest, *, args_hash: str | None = None) -> str:
        """Return a durable request id for ``request`` (idempotent within a scope).

        Outside any ambient :func:`~ne.runtime.correlation.current_action_scope`,
        this is byte-identical to before this unit: a fresh row, a fresh
        ``uuid4()`` id, every call — ``args_hash`` is ignored on this path. Inside
        a scope, a claim-or-find loop first looks for an existing non-terminal row
        for this exact ``(principal_id, action, tier, scope, args_hash)`` key and
        reattaches to it (emitting ``approval.reattached`` instead of writing a
        new row) — see the module docstring and ``test_approval_scope.py``.
        ``tier`` is part of the key so a same-action request at a different tier
        never reattaches to (and inherits the weaker approval policy of) a
        lower-tier pending row (Codex C1). ``args_hash`` is the per-invocation
        discriminator (Argus P1, round 2): when the caller omits it, the key falls
        back to ``canonical_args_hash(request.payload)`` so a scoped submission
        NEVER binds on a NULL hash (the partial unique index requires both
        ``action_scope`` and ``args_hash`` non-NULL to enforce). The production
        caller (``GovernedToolExecutor``) always supplies the real hash of the raw
        tool-call args; the fallback exists for legacy/direct callers and
        preserves round-1 collapse semantics for them (their payload is a
        per-action constant, so they still merge as before this round).
        """
        ambient_attempt = current_conversation_attempt()
        scope = current_action_scope()
        if ambient_attempt is not None:
            if request.principal.id != ambient_attempt.owner_principal_id:
                raise ApprovalIntegrityError("approval principal does not own the active attempt")
            if scope is not None and scope != ambient_attempt.attachment_id:
                raise ApprovalIntegrityError("approval action scope does not match the active turn")
            scope = ambient_attempt.attachment_id
        if scope is None:
            return await self._submit_fresh(
                request, scope=None, args_hash=args_hash, attempt_binding=None
            )

        tier = int(request.tier)
        key_hash = args_hash if args_hash is not None else canonical_args_hash(request.payload)
        attempt_binding = self._attempt_tuple(ambient_attempt)
        for _attempt in range(_SUBMIT_CLAIM_MAX_ATTEMPTS):
            existing = await self._store.find_pending_by_scope(
                principal_id=request.principal.id,
                action=request.action,
                tier=tier,
                action_scope=scope,
                args_hash=key_hash,
                attempt_binding=attempt_binding,
            )
            if existing is not None:
                # A dead waiter may have left a status='pending' row past its
                # persisted deadline. Re-request means a FRESH row, never
                # reattachment to that stale authorization ceremony.
                now = self._clock()
                if now >= _as_utc(existing.expires_at):
                    await self._expire(existing, now)
                    continue
                request_id = str(existing.request_id)
                await self._audit(
                    action="approval.reattached",
                    resource=f"tool:{request.action}",
                    outcome=AuditOutcome.success,
                    actor=request.principal.id,
                    detail={"tier": tier, "action": request.action},
                    provenance={"approval_id": request_id},
                )
                return request_id
            try:
                return await self._submit_fresh(
                    request,
                    scope=scope,
                    args_hash=key_hash,
                    attempt_binding=attempt_binding,
                )
            except IntegrityError:
                # Lost the partial-unique-index race: another caller (or another
                # concurrent DBOS step-retry) won the insert for this exact
                # (principal, action, tier, scope, args_hash) key. The whole create
                # transaction (row + outbox event) rolled back — re-find and
                # reattach to the winner on the next iteration.
                continue
        raise ApprovalIntegrityError(
            f"approval claim-or-find did not converge for {request.action!r} in "
            f"scope {scope!r} after {_SUBMIT_CLAIM_MAX_ATTEMPTS} attempts"
        )

    async def _submit_fresh(
        self,
        request: ApprovalRequest,
        *,
        scope: str | None,
        args_hash: str | None,
        attempt_binding: tuple[str, str, str, str, str, str] | None,
    ) -> str:
        """Insert a brand-new pending row (today's exact behavior, plus ``scope``)."""
        cfg = self._settings.approvals
        request_id = str(uuid.uuid4())
        now = self._clock()
        tier = int(request.tier)
        # NE-APPROVAL-WINDOW: how long a human gets to decide is the ACTION's
        # property, resolved by the policy engine (tiers.rego's
        # ``approval_window_seconds``) and carried ambiently — see
        # ``ne.runtime.correlation.approval_window_scope`` for why it is not a
        # parameter. ``None`` means policy said nothing, and the deployment
        # default applies exactly as before this unit. The single derivation in
        # :func:`resolve_approval_deadlines_s` (shared with the canonical Run
        # park in ``middleware.py``) also keeps the escalation SLA STRICTLY
        # inside the effective window — ``await_outcome`` checks expiry first,
        # so an SLA at-or-past hard expiry would silently delete §4.2
        # escalate-and-notify (the default tier-2 window equals
        # ``tier2_sla_s`` exactly).
        sla_s, expiry_s = resolve_approval_deadlines_s(cfg, tier)
        sla_deadline = now + timedelta(seconds=sla_s)
        expires_at = now + timedelta(seconds=expiry_s)
        await self._store.create(
            request_id=request_id,
            action=request.action,
            tier=tier,
            principal_id=request.principal.id,
            # thaw (not a shallow dict()): request.payload is an AnyMap — a plain
            # dict at the type-annotation level, but pydantic coerces it to a
            # FrozenDict at validation time, and any NESTED mapping value (e.g.
            # the owner-ruled 'disclosure' object, or 'material_parameters'
            # inside it) freezes too. A shallow dict(...) leaves those nested
            # FrozenDicts in place and json.dumps rejects them
            # (TypeError: Object of type FrozenDict is not JSON serializable).
            # thaw() is the sanctioned recursive egress-boundary converter
            # (ne.contracts.frozen) back to plain, JSON-shaped builtins.
            payload=json.dumps(thaw(request.payload)),
            now=now,
            sla_deadline=sla_deadline,
            expires_at=expires_at,
            event=new_approval_event(
                approval_id=request_id,
                tier=tier,
                decision=ApprovalDecision.requested,
                subject_ref=f"tool:{request.action}",
                actor=request.principal.id,
                sla_deadline=sla_deadline,
            ),
            action_scope=scope,
            args_hash=args_hash,
            attempt_binding=attempt_binding,
        )
        # Audit AFTER commit, fail closed: a sink failure raises RuntimeAuditError;
        # the pending row stays durably in the queue (documented).
        await self._audit(
            action="approval.requested",
            resource=f"tool:{request.action}",
            outcome=AuditOutcome.success,
            actor=request.principal.id,
            detail={"tier": tier, "action": request.action},
            provenance={"approval_id": request_id},
        )
        return request_id

    async def approve(
        self, request_id: str, *, approver: str, comment: str | None = None
    ) -> ApprovalOutcome | None:
        """Named human approval. Tier-3 needs two distinct named approvers (2 calls).

        Separation of duties is enforced FIRST, at every tier: the requester
        recorded on the row may never be the approver
        (:func:`refuse_self_approval` ⇒ ``ApprovalPolicyError``, 403 at the API).

        Returns ``None`` for a Tier-3 FIRST check (recorded, not yet approved);
        returns an ``ApprovalOutcome`` once the request is APPROVED.
        """
        if not (isinstance(approver, str) and approver):
            raise ApprovalPolicyError("approver id must be a non-empty string")
        now = self._clock()
        row = await self._require_actionable_row(request_id, now=now, operation="approval")
        # Separation of duties, BEFORE anything is materialized or recorded: the
        # requester on the row may never be the approver — at ANY tier, on the
        # Tier-3 first check as well as the second.
        refuse_self_approval(requester_id=str(row.principal_id), approver=approver)
        await self._assert_protected_arguments_current(row, approver=approver, operation="approval")
        tier = int(row.tier)
        action = str(row.action)

        cfg = self._settings.approvals
        if tier >= 3:
            if approver not in cfg.tier3_named_approvers:
                raise ApprovalPolicyError(
                    f"approver is not in the Tier-3 named-approver list "
                    f"(list size {len(cfg.tier3_named_approvers)}; empty ⇒ never approvable)"
                )
            if row.status == "pending":
                # First named check: record it; not yet approved.
                await self._store.record_first_approval(
                    request_id,
                    approver=approver,
                    now=now,
                    event=new_approval_event(
                        approval_id=request_id,
                        tier=tier,
                        decision=ApprovalDecision.requested,
                        subject_ref=f"tool:{action}",
                        actor=approver,
                    ),
                )
                # Returned event_id intentionally unused: this leg returns None (no
                # ApprovalOutcome exists to carry it).
                await self._audit(
                    action="approval.first-check",
                    resource=f"tool:{action}",
                    outcome=AuditOutcome.success,
                    actor=approver,
                    detail={"tier": tier, "action": action},
                    provenance={"approval_id": request_id, "approver": approver},
                )
                return None
            # Second named check (status == 'pending_second').
            first = str(row.first_approver or "")
            if approver == first:
                raise ApprovalPolicyError(
                    "the second Tier-3 approver must be distinct from the first"
                )
            return await self._grant(row, now, first_approver=first, second_approver=approver)

        # Tier < 3: a single named approval decides immediately.
        return await self._grant(row, now, first_approver=approver, second_approver=None)

    async def reject(self, request_id: str, *, approver: str, reason: str = "") -> ApprovalOutcome:
        """Named rejection — terminal, denied, audited, evidence-recorded."""
        if not (isinstance(approver, str) and approver):
            raise ApprovalPolicyError("approver id must be a non-empty string")
        now = self._clock()
        row = await self._require_actionable_row(request_id, now=now, operation="rejection")
        await self._assert_protected_arguments_current(
            row, approver=approver, operation="rejection"
        )
        tier = int(row.tier)
        action = str(row.action)
        await self._store.decide(
            request_id,
            "rejected",
            approver=approver,
            reason=reason,
            now=now,
            event=new_approval_event(
                approval_id=request_id,
                tier=tier,
                decision=ApprovalDecision.rejected,
                subject_ref=f"tool:{action}",
                actor=approver,
            ),
            evidence_row=self._evidence_row(row, outcome="rejected", approver=approver, now=now),
        )
        decision_event_id = await self._audit(
            action="approval.rejected",
            resource=f"tool:{action}",
            outcome=AuditOutcome.denied,
            actor=approver,
            detail={"tier": tier, "action": action, "status": "rejected"},
            provenance={"approval_id": request_id, "approver": approver},
        )
        self._metrics.record_latency(
            tier=tier,
            decision="rejected",
            seconds=(now - _as_utc(row.requested_at)).total_seconds(),
            config_hash=get_config_hash(),
        )
        return ApprovalOutcome(
            status=ApprovalStatus.REJECTED,
            approver=approver,
            decided_at=now,
            decision_event_id=decision_event_id,
            approval_id=request_id,
        )

    async def await_outcome(self, request_id: str) -> ApprovalOutcome:
        """Poll until the request is terminal; drive §4.2 escalation/expiry en route.

        Expiry is ALWAYS checked before sleeping, so a fake-clock test can never
        hang: advancing the clock past expiry resolves the very next tick.
        """
        poll = self._settings.approvals.poll_interval_s
        while True:
            row = await self._require_row(request_id)
            await self._require_bound_attempt_current(row)
            if row.status in TERMINAL_STATUSES:
                return self._to_outcome(row)
            now = self._clock()
            if now >= _as_utc(row.expires_at):
                if await self._expire(row, now):
                    return ApprovalOutcome(
                        status=ApprovalStatus.EXPIRED,
                        decided_at=now,
                        approval_id=str(row.request_id),
                    )
                # The expiry CAS lost to a concurrent approve/reject/expire: the row
                # is already terminal. Re-read and report the ACTUAL terminal state,
                # so a just-approved call is NEVER misreported as EXPIRED.
                row = await self._require_row(request_id)
                if row.status in TERMINAL_STATUSES:
                    return self._to_outcome(row)
                # Defensive: a rowcount-0 expiry CAS implies terminal, but if the row
                # is somehow still pending, keep polling rather than fabricate a state.
                continue
            if now >= _as_utc(row.sla_deadline) and row.escalated_at is None:
                await self._escalate(row, now)
            await asyncio.sleep(poll)

    async def request_approval(
        self, request: ApprovalRequest, *, args_hash: str | None = None
    ) -> ApprovalOutcome:
        """Frozen ``ApprovalGate`` protocol method: submit then await the outcome.

        ``args_hash`` is a NEW keyword-only, defaulted parameter (Argus P1,
        round 2) — it widens this method's signature without breaking the frozen
        ``ne-contracts`` ``ApprovalGate`` protocol at 1.10.0 (a protocol-typed
        caller that never passes it still type-checks and behaves exactly as
        before: see ``middleware.py``'s ``isinstance`` fallback branch for
        non-``DurableApprovalGate`` fakes).
        """
        request_id = await self.submit(request, args_hash=args_hash)
        return await self.await_outcome(request_id)

    async def list_pending(self) -> list[PendingApproval]:
        """The console queue after persisting every due terminal expiry.

        This read is an expiry driver in its own right. It does not depend on an
        ``await_outcome`` task or DBOS workflow still being alive, and it never
        sends a due row for a browser to hide with its own clock.
        """
        now = self._clock()
        rows: list[Any] = []
        for row in await self._store.list_pending():
            if row.turn_attempt_id is not None and not await self._store.is_bound_attempt_current(
                str(row.request_id)
            ):
                continue
            if now >= _as_utc(row.expires_at):
                # The winner writes row + typed outbox event + evidence in one
                # transaction, then the gate writes the audit/metric evidence.
                # A concurrent terminal winner makes _expire return False; either
                # way this stale snapshot must not be served as actionable.
                await self._expire(row, now)
                continue
            rows.append(row)
        return [
            PendingApproval(
                request_id=str(r.request_id),
                action=str(r.action),
                tier=int(r.tier),
                principal_id=str(r.principal_id),
                args_hash=str(r.args_hash) if r.args_hash is not None else None,
                requested_at=_as_utc(r.requested_at),
                sla_deadline=_as_utc(r.sla_deadline),
                expires_at=_as_utc(r.expires_at),
                escalated=r.escalated_at is not None,
                provisional_source_count=_provisional_count_from_payload(r.payload),
                provisional_source_refs=_provisional_refs_from_payload(r.payload),
            )
            for r in rows
        ]

    async def read_payload(self, request_id: str, *, approver: str) -> ApprovalPayloadDisclosure:
        """The governed, audited, per-request disclosure read (owner ruling 2026-07-31).

        Who may read: for Tier-3, membership in ``ApprovalSettings.``
        ``tier3_named_approvers`` — BOTH dual-flow approvers of a Tier-3 request
        qualify (the same list gates the first AND second check in
        :meth:`approve`), and an empty configured list refuses everyone
        (fail closed, mirrors :meth:`approve`'s own Tier-3 policy). For Tier < 3
        this method performs NO further check: ``ne.api.routers.approvals`` runs
        its fail-closed ``approver_roles`` pre-check (``_require_approver``)
        BEFORE ever reaching this method — see that router's module docstring
        ("this role gate IS the tier-2 authorization layer") — so any caller who
        got this far already cleared the tier-2 named-decider gate. A DIRECT
        (non-HTTP) caller at tier < 3 is trusted the same way :meth:`approve`
        trusts its caller at tier < 3.

        Raises :class:`~ne.runtime.errors.ApprovalRequestNotFoundError`,
        :class:`~ne.runtime.errors.ApprovalAlreadyDecidedError` (terminal rows
        carry nothing left to decide), or :class:`~ne.runtime.errors.ApprovalPolicyError`
        (non-named Tier-3 reader) — the policy-error message is safe-for-display
        (the console renders it verbatim) and deliberately carries NO material
        values, only the list SIZE.

        Every successful read is audited as ``approval.payload_read`` BEFORE the
        disclosure is returned (fail-closed C6: a sink failure raises
        :class:`~ne.runtime.errors.RuntimeAuditError` and the disclosure is
        WITHHELD). The audit detail/provenance carry only tier/action ids — never
        material parameter values (redaction discipline unweakened; see the
        module docstring).
        """
        if not (isinstance(approver, str) and approver):
            raise ApprovalPolicyError("approver id must be a non-empty string")
        now = self._clock()
        row = await self._require_actionable_row(request_id, now=now, operation="payload read")
        tier = int(row.tier)
        action = str(row.action)
        cfg = self._settings.approvals
        if tier >= 3 and approver not in cfg.tier3_named_approvers:
            raise ApprovalPolicyError(
                "payload withheld: not a named approver for this request (Tier-3 "
                "payload is disclosed only to the configured named-approver list, "
                f"size {len(cfg.tier3_named_approvers)}; other approvers may see "
                "the entry exists but not read it)"
            )

        disclosure_raw = _parse_stored_payload(row.payload).get("disclosure")
        if isinstance(disclosure_raw, Mapping):
            d = disclosure_raw
            # TOOL-SUPPLIED, and necessarily so: nothing on the row knows WHICH
            # record an action touches. `action` names the tool, not the target,
            # and an approver deciding a wire needs "account:petty-cash" —
            # "tool:finance.wire.transfer" would strip them of the fact they most
            # need. Description is description; see the safety class below.
            target_resource = str(d.get("target_resource") or f"tool:{action}")
            description = str(d.get("description") or "")
            mp = d.get("material_parameters")
            material_parameters = dict(mp) if isinstance(mp, Mapping) else None
        else:
            target_resource = f"tool:{action}"
            description = ""
            material_parameters = None

        # PRE-015A0-ENFORCEMENT: a source-derived request stores only an opaque
        # invocation reference. The arguments are materialized here, under THIS
        # approver's own current authorization, and never read from the row. A
        # reader that authorization does not admit is refused (typed, 403 at the
        # API) while the request stays pending — the stale tool-supplied
        # description never survives into any refusal either.
        protected_reference = _protected_reference_from(disclosure_raw)
        if protected_reference is not None:
            # A reader whose own authority does not admit the invocation gets the
            # typed non-persisting refusal from the call below (403 at the API,
            # row stays pending) — never a terminalized request or a shell that
            # asserts a revocation (U2 adversarial review, standalone-gate ruling).
            material_parameters = await self._materialize_protected_arguments(
                protected_reference,
                row=row,
                approver=approver,
                operation="payload read",
            )

        # THE SAFETY CLASS IS DERIVED FROM THE ROW, NEVER FROM THE STORED BLOB.
        #
        # This previously preferred the stored value (`d.get("reversibility") or
        # reversibility_label(tier)`). Review produced the consequence: a Tier-3
        # row disclosing "reversible — internal action class". The ONE field an
        # approver reads to decide "can I undo this?" was the one the payload's
        # author could set.
        #
        # `reversibility_label`'s own docstring already promised exactly this —
        # "pure function of tier, never derived from model output (C12)" — while
        # the call site quietly did the opposite. A reviewer who checks the rule
        # reads that docstring and stops, which is why the promise now lives at
        # the point of use and is pinned by a lying-payload test rather than by
        # prose.
        reversibility = reversibility_label(tier)

        disclosure = ApprovalPayloadDisclosure(
            request_id=request_id,
            action=action,
            tier=tier,
            principal_id=str(row.principal_id),
            target_resource=target_resource,
            description=description,
            material_parameters=material_parameters,
            reversibility=reversibility,
        )
        # Audit BEFORE return, fail closed: a sink failure withholds the disclosure.
        await self._audit(
            action="approval.payload_read",
            resource=f"tool:{action}",
            outcome=AuditOutcome.success,
            actor=approver,
            detail={"tier": tier, "action": action},
            provenance={"approval_id": request_id, "approver": approver},
        )
        return disclosure

    # -- internals ---------------------------------------------------------

    async def _materialize_protected_arguments(
        self,
        reference: ProtectedSourceInvocationRef,
        *,
        row: Any,
        approver: str,
        operation: str,
    ) -> dict[str, Any]:
        """Materialize a protected request's arguments under the CALLER's authority.

        The guard evaluates under the bound authenticated context — the READING or
        DECIDING approver. A non-authorized outcome there is therefore a fact about
        THAT principal's ACL standing, never a proven source revocation (only an
        admitted authority fact proving contraction/revocation, deletion or
        generation replacement may author ``permanent_withheld``; only a proven
        permanent source denial uses the terminal source-cancel transition). So it
        raises the typed, NON-PERSISTING
        :class:`ApprovalSourceWithheldFromApproverError` — the row STAYS PENDING for
        another approver or for honest expiry, and no ``source_withheld`` terminal or
        revocation-asserting record is written (U2 adversarial review, standalone-gate
        ruling; mirrors the run-bound ``authorize``/``check_decision_authority`` fix).

        Everything non-committal — an unwired guard, a retryable-unknown authority, an
        unreadable payload — raises :class:`SourceAuthorityRetryableError` so NOTHING
        terminal is written and the pending row survives for a later attempt.
        """

        request_id = str(row.request_id)
        if self._source_guard is None:
            raise SourceAuthorityRetryableError(
                "this approval request is source-derived but the gate was composed "
                "without a protected-source resolver; nothing is disclosed"
            )
        result = await self._source_guard.resolve_for_approval(
            reference,
            approval_id=request_id,
            decision_attempt_id=_decision_attempt_id(request_id, approver, operation),
        )
        if result.retryable:
            raise SourceAuthorityRetryableError(
                "current source authority for this request could not be established; "
                "the request stays pending and nothing terminal was written"
            )
        if not result.authorized:
            raise ApprovalSourceWithheldFromApproverError(
                "current source authority does not admit this approver to the "
                f"protected invocation; the {operation} is refused and the request "
                "remains pending for an approver who holds authority over every "
                "underlying source"
            )
        try:
            arguments = json.loads(result.text)
        except ValueError as exc:
            raise SourceAuthorityRetryableError(
                "the materialized invocation did not parse; nothing is disclosed"
            ) from exc
        if not isinstance(arguments, Mapping):
            raise SourceAuthorityRetryableError(
                "the materialized invocation is not an argument mapping; nothing is disclosed"
            )
        return dict(arguments)

    async def _terminalize_source_withheld(
        self, row: Any, *, approver: str, operation: str
    ) -> bool:
        """Atomically make a drifted request non-actionable. True iff THIS call did it.

        REACHABILITY CONSTRAINT (U2 adversarial review, standalone-gate ruling —
        the analogue of the run-bound ``operation="resume"`` rule): this may be
        called ONLY from a genuine source-authority decision evaluated under the
        REQUEST'S OWN execution principal (``row.principal_id``) — an admitted
        authority fact proving ACL contraction/revocation, deletion or generation
        replacement. A refusal evaluated under a reading or
        deciding approver's context is that principal's 403 and must never reach
        this method. No such owner-context reconciliation path exists in the
        standalone gate today, so this is deliberately UNREACHABLE until SPEC's
        standalone protected producer is built with one; kept
        because the terminal state, its store CAS and its audit contract are the
        specified shape that producer will drive.

        A False return means the store's totally-ordered terminal CAS lost to a
        concurrent approve/reject/expire; the request is terminal either way. The
        audit record runs after the committed CAS (the :meth:`_expire` precedent)
        and is fail-closed: a sink failure raises and nothing is disclosed.
        """

        now = self._clock()
        request_id = str(row.request_id)
        tier = int(row.tier)
        action = str(row.action)
        landed = await self._store.mark_source_withheld(
            request_id,
            now=now,
            reason=SOURCE_WITHHELD_REASON,
            evidence_row=self._evidence_row(
                row, outcome=SOURCE_WITHHELD_STATUS, approver=None, now=now
            ),
        )
        await self._audit(
            action="approval.source_withheld",
            resource=f"tool:{action}",
            outcome=AuditOutcome.denied,
            actor=approver,
            detail={
                "tier": tier,
                "action": action,
                "operation": operation,
                "terminalized_here": landed,
            },
            provenance={"approval_id": request_id},
        )
        return landed

    async def _assert_protected_arguments_current(
        self, row: Any, *, approver: str, operation: str
    ) -> None:
        """Refuse a decision on a source-derived request whose authority drifted.

        A source-free request is unaffected. For a protected one, ONLY an authorized
        materialization lets the decision reach its CAS, so no stale action bytes
        can reach an audit event, the decision response or a handler.
        """

        reference = _protected_reference_from(_parse_stored_payload(row.payload).get("disclosure"))
        if reference is None:
            return
        # Raises ApprovalSourceWithheldFromApproverError when THIS approver's own
        # authority does not admit the invocation (the row stays pending), and
        # SourceAuthorityRetryableError for anything non-committal. Only an
        # authorized materialization returns, letting the decision reach its CAS.
        await self._materialize_protected_arguments(
            reference,
            row=row,
            approver=approver,
            operation=operation,
        )

    async def _require_row(self, request_id: str) -> Any:
        row = await self._store.get(request_id)
        if row is None:
            raise ApprovalRequestNotFoundError(request_id)
        return row

    async def _require_bound_attempt_current(self, row: Any) -> None:
        if row.turn_attempt_id is None:
            return
        if not await self._store.is_bound_attempt_current(str(row.request_id)):
            raise ApprovalRequestNotFoundError(str(row.request_id))

    async def assert_current_attempt(self, request_id: str) -> None:
        """Fence a consumed approval to the caller's exact live attempt epoch."""
        row = await self._require_row(request_id)
        if row.turn_attempt_id is None:
            return
        ambient = current_conversation_attempt()
        if ambient is None or not await self._store.is_bound_attempt_current(
            request_id, attempt_binding=self._attempt_tuple(ambient)
        ):
            raise ApprovalRequestNotFoundError(request_id)

    async def _require_actionable_row(
        self, request_id: str, *, now: datetime, operation: str
    ) -> Any:
        """Return a pending row, persisting hard expiry before any exposure/action.

        ``now`` is captured once by the server-facing caller and threaded through
        the store CAS, so the boundary has one answer. If expiry loses the CAS,
        re-read and report the actual winner rather than fabricating EXPIRED.
        """
        row = await self._require_row(request_id)
        await self._require_bound_attempt_current(row)
        if row.status in TERMINAL_STATUSES:
            raise ApprovalAlreadyDecidedError(f"request {request_id!r} is already {row.status!r}")
        if now < _as_utc(row.expires_at):
            return row
        if await self._expire(row, now):
            raise ApprovalAlreadyDecidedError(
                f"request {request_id!r} expired before {operation}; a re-request is required"
            )
        row = await self._require_row(request_id)
        raise ApprovalAlreadyDecidedError(f"request {request_id!r} is already {row.status!r}")

    async def _grant(
        self, row: Any, now: datetime, *, first_approver: str, second_approver: str | None
    ) -> ApprovalOutcome:
        request_id = str(row.request_id)
        tier = int(row.tier)
        action = str(row.action)
        await self._store.decide(
            request_id,
            "approved",
            approver=first_approver,
            second_approver=second_approver,
            now=now,
            event=new_approval_event(
                approval_id=request_id,
                tier=tier,
                decision=ApprovalDecision.approved,
                subject_ref=f"tool:{action}",
                actor=second_approver or first_approver,
            ),
            evidence_row=self._evidence_row(
                row, outcome="approved", approver=first_approver, now=now
            ),
        )
        provenance = {"approval_id": request_id, "approver": first_approver}
        if second_approver is not None:
            provenance["second_approver"] = second_approver
        decision_event_id = await self._audit(
            action="approval.granted",
            resource=f"tool:{action}",
            outcome=AuditOutcome.success,
            actor=second_approver or first_approver,
            detail={"tier": tier, "action": action, "status": "approved"},
            provenance=provenance,
        )
        self._metrics.record_latency(
            tier=tier,
            decision="approved",
            seconds=(now - _as_utc(row.requested_at)).total_seconds(),
            config_hash=get_config_hash(),
        )
        return ApprovalOutcome(
            status=ApprovalStatus.APPROVED,
            approver=first_approver,
            decided_at=now,
            decision_event_id=decision_event_id,
            approval_id=request_id,
        )

    async def _escalate(self, row: Any, now: datetime) -> None:
        request_id = str(row.request_id)
        tier = int(row.tier)
        action = str(row.action)
        escalated = await self._store.mark_escalated(
            request_id,
            now=now,
            event=new_approval_event(
                approval_id=request_id,
                tier=tier,
                decision=ApprovalDecision.escalated,
                subject_ref=f"tool:{action}",
                actor="system",
            ),
        )
        if not escalated:
            return
        await self._audit(
            action="approval.escalated",
            resource=f"tool:{action}",
            outcome=AuditOutcome.denied,
            actor="system",
            detail={"tier": tier, "action": action},
            provenance={"approval_id": request_id},
        )

    async def _expire(self, row: Any, now: datetime) -> bool:
        """Attempt the terminal expiry CAS. Returns True iff THIS call expired the row.

        A False return means the store's totally-ordered terminal CAS lost to a
        concurrent approve/reject/expire — the caller re-reads and reports the
        ACTUAL terminal state (never a spurious EXPIRED). Audit + metrics stay
        winners-only (they run only on the True branch below).
        """
        request_id = str(row.request_id)
        tier = int(row.tier)
        action = str(row.action)
        expired = await self._store.expire(
            request_id,
            now=now,
            event=new_approval_event(
                approval_id=request_id,
                tier=tier,
                decision=ApprovalDecision.expired,
                subject_ref=f"tool:{action}",
                actor="system",
            ),
            evidence_row=self._evidence_row(row, outcome="expired", approver=None, now=now),
        )
        if not expired:
            return False
        await self._audit(
            action="approval.expired",
            resource=f"tool:{action}",
            outcome=AuditOutcome.denied,
            actor="system",
            detail={"tier": tier, "action": action},
            provenance={"approval_id": request_id},
        )
        self._metrics.record_latency(
            tier=tier,
            decision="expired",
            seconds=(now - _as_utc(row.requested_at)).total_seconds(),
            config_hash=get_config_hash(),
        )
        return True

    @staticmethod
    def _evidence_row(
        row: Any, *, outcome: str, approver: str | None, now: datetime
    ) -> dict[str, Any]:
        return {
            "request_id": str(row.request_id),
            "action": str(row.action),
            "tier": int(row.tier),
            "outcome": outcome,
            "approver": approver,
            "decided_at": now,
            "clean": outcome == "approved",
        }

    @staticmethod
    def _to_outcome(row: Any) -> ApprovalOutcome:
        decided_at = _as_utc(row.decided_at) if row.decided_at is not None else None
        # ADR-0019: the request id is stable row state — every rehydrated
        # outcome names the request it decided (decision_event_id, by contrast,
        # stays None here: only the appending call may claim that).
        approval_id = str(row.request_id)
        if row.status == "approved":
            approver = str(row.first_approver or "")
            if not approver:
                # Enforcement layer 4: never build an approver-less APPROVED outcome.
                raise ApprovalIntegrityError(
                    f"request {row.request_id!r} is 'approved' with no named approver"
                )
            return ApprovalOutcome(
                status=ApprovalStatus.APPROVED,
                approver=approver,
                decided_at=decided_at,
                approval_id=approval_id,
            )
        if row.status == "rejected":
            return ApprovalOutcome(
                status=ApprovalStatus.REJECTED,
                approver=str(row.first_approver) if row.first_approver else None,
                decided_at=decided_at,
                approval_id=approval_id,
            )
        if row.status == SOURCE_WITHHELD_STATUS:
            # The frozen ApprovalStatus has no member for this fact, and the EXPIRED
            # fallback below would report a deadline that was never reached. This is
            # THE chokepoint every row-to-outcome path goes through, so no caller can
            # obtain an APPROVED (or any) outcome for a drift-withheld request.
            raise SourceAuthorityWithheldError(
                f"request {approval_id!r} was made non-actionable because current source "
                "authority permanently denies its arguments; it can never be decided, and "
                "a newly authenticated request is required"
            )
        return ApprovalOutcome(
            status=ApprovalStatus.EXPIRED, decided_at=decided_at, approval_id=approval_id
        )

    async def _audit(
        self,
        *,
        action: str,
        resource: str,
        outcome: AuditOutcome,
        actor: str,
        detail: Mapping[str, Any] | None = None,
        provenance: Mapping[str, str] | None = None,
    ) -> str:
        # Event construction (resolving the FROZEN config hash) is OUTSIDE the try,
        # so ConfigHashNotFrozenError propagates rather than becoming RuntimeAuditError.
        prov = dict(_PROVENANCE_SOURCE)
        if provenance:
            prov.update(provenance)
        # Pre-generated (dashed uuid4, matching new_audit_event's own default and
        # the request_id precedent) so it can be threaded onto the caller's
        # ApprovalOutcome (ADR-0017) — but ONLY returned below, after a successful
        # append, so an id never escapes a decision whose chain record didn't land.
        event_id = str(uuid.uuid4())
        event = new_audit_event(
            action=action,
            resource=resource,
            outcome=outcome,
            actor=actor,
            detail=json.dumps(detail) if detail is not None else None,
            provenance=prov,
            event_id=event_id,
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                f"audit append failed for {action!r} on {resource!r}; "
                "result withheld (C6 fail closed)"
            ) from exc
        return event_id
