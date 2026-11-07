# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE C6 CHOKEPOINT — every tool call passes guardrails + the PDP + HITL, no bypass.

:class:`GovernedToolExecutor` is the ONLY object in the platform that holds a
tool ``handler`` and the ONLY structural path from a tool name to its execution.
Its constructor REQUIRES ``pdp``, ``guardrails``, ``approval_gate``, and ``sink``
— all keyword-only, no defaults, none may be ``None`` — so there is no
sink-less / PDP-less / gate-less way to build one. The audit coupling is
therefore free by construction:

  * the ``GuardrailEngine`` was built with a required ``sink`` (blocks self-audit);
  * the ``GovernedPdp`` was built via ``create_pdp(settings, sink=...)`` (every
    ``decide`` emits a ``pdp.decide`` AuditEvent before returning);
  * the executor's OWN events (unknown tool / approval / execution) fail closed:
    if the audit append fails, :class:`~ne.runtime.errors.RuntimeAuditError` is
    raised and the result is withheld.

Ordering (C12 preserved): guardrails → PDP → binding-owned typed pre-gate
validation (actions) → HITL gate → handler. Guardrails run FIRST (before the PDP and
before any model-adjacent judgment); their ``GuardrailViolation`` /
``GuardrailEvaluationError`` / ``AuditEmissionError`` propagate UNWRAPPED — the
engine already audited them. NE-ACTION-VALIDATE-BEFORE-GATE: immediately after the
PDP (and before the HITL gate), a binding's ``validate_args`` — when set — runs a
typed-input preflight; a failure is an audited ``tool.validate[denied]`` REFUSAL
raised BEFORE any approval row can exist, so a call that cannot execute never
consumes a human decision. Only on guardrails-pass AND policy-allow AND (for actions)
validate_args-pass, with a granted, named approval for require-approval, does the
handler run.

Unknown-tool branch (an unregistered name — e.g. a model-emitted call for a tool
that does not exist): after the ``tool.unknown`` AuditEvent, guardrails STILL run
first (their block — rule ``toolargs.unknown-tool`` — is audited by the engine)
and the PDP is STILL consulted (an unclassified action DENYs), so the governance
record is complete. ``GuardrailViolation`` is caught there ONLY so the branch can
reach the PDP and finish the record — ``GuardrailEvaluationError`` /
``AuditEmissionError`` are NOT caught (fail closed). The branch then TERMINALLY
raises :class:`~ne.runtime.errors.ToolNotRegisteredError`: an unknown tool NEVER
executes, regardless of any decision effect.

Gate-tier authority (NE-CKD3, Checkpoint-D finding #3): the HITL gate tier is the
STRICTER-OF ``max(PDP-decided tier, spec.tier, tiers.rego python mirror)`` with
obligation floors (``second_check`` / ``named_approver`` ⇒ Tier-3;
``per_item_human_approval`` ⇒ Tier-2), computed by :func:`_effective_tier`. The
decided tier can only ESCALATE gating, never lower it. An ALLOW effect whose
effective tier is >= 2 STILL routes through the human gate — the Python mirror is
a defense-in-depth floor, never a ceiling, so even a hijacked/rogue bundle cannot
execute a Tier-2/3 action without a human (C12).

Redaction discipline (mirrors ``ne.govern.pdp`` / ``engine``): audit ``detail`` /
``provenance`` carry ONLY tool names, tiers, statuses, error classes, and
approval ids — never raw argument, recipient, or context VALUES.

Execution→approval join (B14, ADR-0019): when the HITL gate granted the call,
the gate's ``ApprovalOutcome.approval_id`` — the durable request id of the
approval that was consumed — is threaded from :meth:`_require_named_approval`
into :meth:`_run_handler` and stamped as ``provenance["approval_id"]`` on BOTH
``tool.execute`` outcomes (success and error: the approval authorised the
attempt), and onto the ``tool.approval`` record of either branch. The stamp
happens HERE, at the one site that holds the consumed approval — never
reconstructed afterwards from time or adjacency. When no approval ran (tier
0/1 allow) or the gate recorded no durable id, the key is OMITTED — never
``"None"``, never inferred. Adversarial guard:
``tests/test_approval_execution_join.py`` (two-leg fixture, kills a stamped
constant and last-id-wins).

Consumed-approval carrier (NE-CRM-NOTE-PROVENANCE): the same gate outcome is
also bound — as a frozen :class:`ConsumedApproval` (the gate-recorded named
APPROVER plus the durable approval id) — to the ``current_tool_approval``
ContextVar around the handler call, exactly mirroring the D-1
``current_tool_principal`` carrier below: set only by :meth:`_run_handler`
from the verified gate outcome, reset in ``finally``, ``None`` whenever the
call consumed no approval. It exists so a governed write-back action can
stamp WHO APPROVED the write into the source-system record itself (via an
injected provider seam — see ``reference_actions/crm_note.py``); it never
changes what audit provenance carries (``tool.execute`` provenance stays
exactly ``{source, component, approval_id}`` — ``test_approval_execution_join``
pins that key set).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from ne.contracts.failure import describe_failure, failure_fields, failure_site
from ne.contracts.frozen import thaw
from ne.contracts.governance import (
    ApprovalGate,
    ApprovalRequest,
    ApprovalStatus,
    AuditSink,
    DecisionEffect,
    PolicyDecision,
    PolicyDecisionPoint,
    PolicyRequest,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import (
    SourceBoundToolResultRef,
    canonical_source_authority_stamps,
)
from ne.contracts.types import ApprovalTier, Principal
from ne.events import new_approval_event, new_audit_event
from ne.govern import build_tool_call_payload
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails.engine import GuardrailEngine
from ne.runtime.actions import tier_for_action
from ne.runtime.approvals import (
    DurableApprovalGate,
    canonical_args_hash,
    resolve_approval_deadlines_s,
    reversibility_label,
)
from ne.runtime.correlation import (
    approval_window_scope,
    current_approval_window_declared,
    current_run_trace_id,
    current_standing_lifetime_s,
    standing_lifetime_scope,
)
from ne.runtime.errors import (
    ActionValidationError,
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    ApprovalSourceWithheldFromApproverError,
    ApprovalWindowUnhonourableError,
    RunApprovalAuthorityRetryableError,
    RuntimeAuditError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
)
from ne.runtime.provisional import ProvisionalContext, current_provisional_context
from ne.runtime.run_audit_receipts import RunToolCallContext, current_run_tool_call
from ne.runtime.run_checkpoint import RunApprovalWaiting
from ne.runtime.source_results import (
    current_authenticated_context,
    current_protected_tool_evidence_refs,
    decode_protected_reference,
)
from ne.runtime.tools import ToolBinding, validate_binding
from ne.schema import ApprovalDecision, AuditOutcome

if TYPE_CHECKING:
    from ne.runtime.toolset import ToolsetScopedExecutor

#: The operator channel for a failed governed tool execution. The audit ledger is
#: the DURABLE record and stays authoritative; this logger is where the bounded,
#: redacted MESSAGE goes — the half that C12 forbids the ledger to carry.
_LOG = logging.getLogger(__name__)

_PROVENANCE_SOURCE = {"source": "ne-runtime", "component": "middleware"}


class _ToolsetScopableExecutor(Protocol):
    """Handler-less executor carrying the exact governance authority for scoping."""

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]: ...

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str: ...

    def _toolset_authority(self) -> tuple[AuditSink, PolicyDecisionPoint]: ...


#: The surface-independent tool-principal carrier (D-1). Set ONLY by
#: :meth:`GovernedToolExecutor._run_handler` from its VERIFIED ``principal``
#: argument, around the handler call, and reset in a ``finally`` — so it is
#: un-spoofable (no request/model data can reach it) and universal (every surface
#: — chat, worker cron, MCP — inherits it because the ONE executor sets it). It is
#: read ONLY through an injected ``principal_provider`` seam (e.g. the skill
#: bindings); OTHER handlers must NOT import it. This is deliberately narrow
#: ambient authority: nothing in a handler's ``args``-only signature discloses it,
#: so widening the read surface is a security regression (plan §2.9).
_TOOL_PRINCIPAL: ContextVar[Principal | None] = ContextVar(
    "ne_runtime_tool_principal", default=None
)


def current_tool_principal() -> Principal | None:
    """The principal of the in-flight governed tool call, or ``None`` outside one.

    Set only by :meth:`GovernedToolExecutor._run_handler` from its verified
    principal argument; the ONE sanctioned universal carrier for skill bindings'
    ``principal_provider``. Do NOT read this from other handlers (ambient-authority
    discipline, plan §2.9)."""
    return _TOOL_PRINCIPAL.get()


@dataclass(frozen=True)
class ConsumedApproval:
    """The named human approval the in-flight governed call consumed.

    Built ONLY by :meth:`GovernedToolExecutor._require_named_approval` from the
    gate's APPROVED outcome — never reconstructed from time, adjacency, or any
    request/model data — and bound to the ``current_tool_approval`` carrier by
    :meth:`GovernedToolExecutor._run_handler` around the handler call.

    ``approver`` is the gate-recorded named approver: the human who said YES.
    It is NEVER the requesting principal — the requester rides the separate
    ``current_tool_principal`` carrier, and conflating the two would attribute
    an authorisation to someone who never gave it. ``approval_id`` is the
    durable request id of the consumed approval (the same id the B14 audit
    join stamps), ``None`` when the gate recorded no durable id — a consumer
    stamping it onward MUST omit it then, never invent one.
    """

    approver: str
    approval_id: str | None


#: The consumed-approval carrier (NE-CRM-NOTE-PROVENANCE). Set ONLY by
#: :meth:`GovernedToolExecutor._run_handler` from the verified gate outcome
#: threaded out of :meth:`_require_named_approval`, around the handler call,
#: reset in a ``finally`` — same un-spoofability argument as ``_TOOL_PRINCIPAL``
#: above (no request/model data can reach it; a nested execute shadows and then
#: restores the parent's value). Read ONLY through an injected provider seam
#: (``approval_provider``); other handlers must NOT import it directly —
#: widening the read surface is the same ambient-authority regression as for
#: the principal carrier (plan §2.9).
_TOOL_APPROVAL: ContextVar[ConsumedApproval | None] = ContextVar(
    "ne_runtime_tool_approval", default=None
)


def current_tool_approval() -> ConsumedApproval | None:
    """The approval the in-flight governed call consumed, or ``None``.

    ``None`` outside a governed handler AND inside one whose call consumed no
    approval (tier 0/1 allow) — a provider reading ``None`` has nothing to
    stamp and must stamp nothing. Set only by
    :meth:`GovernedToolExecutor._run_handler`; read it only through an injected
    ``approval_provider`` seam (ambient-authority discipline, plan §2.9)."""
    return _TOOL_APPROVAL.get()


#: The workflow-owned per-subject write gate (NE-WF-SUBJECT-GATE). Owner ruling:
#: a workflow that acts on subjects owes EACH subject a human decision before its
#: write fires — and that obligation belongs to the WORKFLOW layer, not to any
#: action's tier binding. It was silently lost when ``write.internal.crm.note``
#: correctly moved Tier-2 -> Tier-1 (NE-CS-SEND, 1df6a113): the workflow engine
#: had been borrowing the action's own tier as its gate, so re-tiering the action
#: un-gated the workflow. This scope makes the obligation explicit and
#: tier-independent: it carries an ESCALATE-ONLY approval floor that
#: :func:`_effective_tier` adds to its stricter-of candidate set for
#: source-system WRITES only (``write.*`` names). Inside the scope a Tier-1
#: internal write still parks at the human gate; reads and drafts are untouched;
#: and because the floor only ever joins a ``max()``, no scope can LOWER any
#: tier — the same one-directional discipline as the PDP-decided tier.
_WRITE_APPROVAL_FLOOR: ContextVar[ApprovalTier | None] = ContextVar(
    "ne_runtime_write_approval_floor", default=None
)


def current_write_approval_floor() -> ApprovalTier | None:
    """The ambient escalate-only write-gate floor, ``None`` outside any scope.

    Read by :func:`_effective_tier` only; see :func:`write_approval_floor_scope`."""
    return _WRITE_APPROVAL_FLOOR.get()


@contextlib.contextmanager
def write_approval_floor_scope(tier: ApprovalTier) -> Iterator[ApprovalTier]:
    """Escalate every ``write.*`` action in the block to gate at >= ``tier``.

    NE-WF-SUBJECT-GATE: entered by a workflow around its per-subject write leg so
    each subject's write parks for a human decision REGARDLESS of the action's own
    declared tier — the per-subject gate is the workflow's obligation, and it must
    survive any action re-tiering. Escalate-only by construction (the floor joins
    ``_effective_tier``'s ``max()``); applies ONLY to actions whose name
    classifies as a source-system write (``write.*``) so wrapping a whole run
    never gates its reads or drafts. Nested scopes: innermost wins for the block,
    prior ambient value restored on exit (:func:`action_scope` semantics —
    always sets, never mints).
    """
    token = _WRITE_APPROVAL_FLOOR.set(tier)
    try:
        yield tier
    finally:
        _WRITE_APPROVAL_FLOOR.reset(token)


def _run_source_references(context: RunToolCallContext) -> tuple[SourceBoundToolResultRef, ...]:
    """Consume every server-observed protected reference admitted for this Tool call.

    A governed Run effect that followed a multi-read ladder rests on ALL of the
    evidence the run's own successful reads served, so the approval binds the
    COMPLETE admitted set — every reference must be an exact stamped Tool-result
    reference this Run Tool executor itself served, and the union of their
    authority stamps is what the approval re-checks at disclosure and resume.
    Binding fewer sources than the effect rests on would be the weaker guarantee:
    authority revoked on an unbound source between park and approval would no
    longer withhold the effect. Zero admitted references still refuses — a Tier-2
    effect with no server-observed evidence never parks an approval.

    COMPLETENESS IS CHECKED HERE, NOT ASSUMED. Two comparisons run against
    ``context.served_protected_refs`` — the authoritative server-observed set the
    Run Tool executor accumulated from its own successful calls
    (``ReceiptGatedToolExecutor.served_protected_refs``) — and together they pin
    the carrier to exactly that set:

    * SOUNDNESS: every member of the ambient carrier must appear in the served
      set, so nothing the executor did not itself serve can be bound.
    * COMPLETENESS: every ``SourceBoundToolResultRef`` in the served set must
      appear in the carrier, so nothing the executor DID serve can be dropped.

    The second is what makes the word COMPLETE a property of this gate rather
    than of its one caller. The carrier is populated out-of-band by
    ``AgenticPlanner._execute_tool`` (``runner.py``, ``protected_tool_evidence_
    scope(_protected_tool_evidence_refs(self.served))``); before this check a
    one-line narrowing there — binding, say, only the refs the write's own
    ``evidence_refs`` names — would have parked a well-formed approval over a
    PROPER SUBSET of the admitted evidence, whose parked ``source_stamps`` union
    then omits those sources, and revoking authority on an omitted source would
    no longer withhold the effect. Nothing else in the system would have gone
    red.

    Together the two directions pin the carrier to EXACTLY the served set. That
    is not a tightening of honest behaviour: the planner's selector runs over
    ``self.served``, which is a superset of what the executor itself records, so
    an honest run always carries every served reference and the new comparison
    never fires. It fires only when the carrier has been narrowed away from the
    server-observed evidence — precisely the failure it exists to catch.
    """

    selected = current_protected_tool_evidence_refs()
    if not selected:
        # U3-F4: the two empty-carrier failures are DISTINGUISHABLE in audit.
        # The runner now populates the carrier unconditionally from the
        # server-observed served set (`_RecordedExecutor.execute` in
        # runner.py, `protected_tool_evidence_scope(_protected_tool_evidence_
        # refs(self.served))`), so an empty carrier while the executor HAS served
        # protected references is a server-side binding bug — the signature
        # that left live run 7f782fe0 permanently un-parkable — never the
        # model's evidence problem. Only a run that genuinely served nothing
        # gets the evidence-less refusal.
        if context.served_protected_refs:
            raise ApprovalIntegrityError(
                "canonical Run approval evidence carrier is empty although this Run Tool "
                "executor served protected source references; the governed caller did not "
                "bind the served evidence around this escalated call"
            )
        raise ApprovalIntegrityError(
            "canonical Run approval requires at least one admitted protected source "
            "reference: this run's Tool executor served no protected source evidence"
        )
    references: list[SourceBoundToolResultRef] = []
    for encoded in selected:
        try:
            reference = decode_protected_reference(encoded)
        except Exception as exc:
            raise ApprovalIntegrityError(
                "canonical Run approval contains a malformed protected reference"
            ) from exc
        if (
            not isinstance(reference, SourceBoundToolResultRef)
            or encoded not in context.served_protected_refs
        ):
            raise ApprovalIntegrityError(
                "canonical Run approval requires an exact protected reference served "
                "by this Run Tool executor"
            )
        if not reference.stamps:
            raise ApprovalIntegrityError("canonical Run approval source stamps are unavailable")
        references.append(reference)
    # THE COMPLETENESS COMPARISON. The loop above proves the carrier is SOUND
    # (every selected ref was served); this proves it is COMPLETE (every served
    # Tool-result ref was selected). The predicate is deliberately the SAME
    # ``isinstance`` used by the soundness loop above and by the carrier's own
    # selector (``_protected_tool_evidence_refs``) — and since
    # ``SourceBoundToolResultSetRef`` SUBCLASSES ``SourceBoundToolResultRef``,
    # set-shaped results are bindable evidence and carry a completeness
    # obligation too. A narrower predicate here would silently re-open the
    # dropped-reference hole for exactly the set-shaped reads. The other stamped
    # reference kinds (``ProtectedSourceOutputRef``,
    # ``ProtectedSourceInvocationRef``) are siblings, not subclasses, and are
    # bound by neither side.
    admitted = set(selected)
    for encoded in context.served_protected_refs:
        try:
            served = decode_protected_reference(encoded)
        except Exception as exc:
            raise ApprovalIntegrityError(
                "canonical Run approval cannot verify completeness against a malformed "
                "server-observed protected reference"
            ) from exc
        if isinstance(served, SourceBoundToolResultRef) and encoded not in admitted:
            raise ApprovalIntegrityError(
                "canonical Run approval must bind every protected source reference "
                "this Run Tool executor served"
            )
    # Canonical order: the receipt binding below must recompute byte-identically
    # on a durable replay regardless of the ambient carrier's iteration order.
    return tuple(sorted(references, key=lambda reference: reference.reference_id))


#: The stamp capacity of every ``_StampedReference`` contract model
#: (``ne.contracts.source_authority``, ``Field(max_length=128)``). The parked
#: invocation's stamp set is rehydrated through ``ProtectedSourceInvocationRef``
#: on disclosure and resume, so a union larger than this could park a wait no
#: decision could ever resume — it is refused at park time instead.
_MAX_RUN_APPROVAL_SOURCE_STAMPS = 128


def _evidence_set_receipt(references: Sequence[SourceBoundToolResultRef]) -> tuple[str, str]:
    """The durable ``(stamp_receipt_id, stamp_receipt_hash)`` binding of the evidence set.

    One reference binds as itself — its ``reference_id`` and ``result_hash``, the
    checkpoint's historical shape. Several bind as a deterministic digest over the
    canonically ordered ``(reference_id, result_hash)`` pairs, so the checkpoint
    still names the EXACT evidence the approval rests on: a replay that adds,
    drops or substitutes any member recomputes a different binding and fails the
    ``create_wait`` integrity comparison. Both fields stay content-free — the ids
    are random surrogate keys and the hashes are digests, never source material.
    """

    if len(references) == 1:
        return references[0].reference_id, references[0].result_hash
    pairs = [[reference.reference_id, reference.result_hash] for reference in references]
    set_id = hashlib.sha256(
        json.dumps(
            [reference.reference_id for reference in references], separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    set_hash = hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"evidence-set:{set_id}", set_hash


#: The name prefixing the LOG WARNING (``logging.getLogger("ne.runtime
#: .middleware")``, WARNING level — there is NO audit row; see the emitting
#: branch for why) the guard emits when a park's window outruns the owner's
#: horizon but nobody DECLARED that window. One name, so the floor-route parks
#: are greppable in the deployment's logs without reading the code.
#:
#: ⚠ WHAT THAT GREP ACTUALLY RETURNS, on this deployment: EVERY floor-route
#: CANONICAL RUN park, not the misconfigured ones. ``ApprovalSettings.expiry_s``
#: defaults to 86400 s and ``tiers.rego``'s tier-2 floor is 14400 s, while the
#: realm token is 900 s and nothing under ``deploy/`` overrides either — so
#: every window this branch can see outruns the horizon and every floor-route
#: Run park warns. It is a census of parks that took the floor route, useful
#: for spotting an action that SHOULD have a declared window; it is not a
#: shortlist of faults.
#:
#: ⚠ AND IT IS SILENT ON THE CONVERSATION PARK, which never reaches this guard
#: at all (see :func:`_require_honourable_approval_window` on why it is not
#: guarded). The HTTP workflows route parks ``write.internal.crm.note`` under
#: ``write_approval_floor_scope(TIER_2)`` on exactly this floor route and logs
#: NOTHING, so an empty grep is not evidence that no floor-route park happened.
_UNHONOURABLE_FLOOR_EVENT = "approval.window.exceeds_authorization_horizon"


def _require_honourable_approval_window(
    *, action: str, now: datetime, expiry_seconds: float, declared: bool
) -> None:
    """Refuse a DECLARED approval window that outruns the caller's horizon; warn on a floor.

    THE TWO CLOCKS MUST AGREE BEFORE A WAIT IS CREATED, and until this function
    nothing compared them. ``resolve_approval_deadlines_s`` (``ne.runtime
    .approvals``, called by this function's only caller) resolves how
    long a human gets — the ACTION's policy property (``tiers.rego``'s
    ``approval_window_seconds``, ADR-0030; tier-2 floor 14400 s) or, when policy
    named no window at all, the deployment-wide ``cfg.expiry_s``. The caller's
    frozen ``AuthenticatedRequestContext`` — the RUN OWNER's, on the canonical
    Run park this guards — carries a different, absolute deadline: past its
    ``expires_at`` the resume seam raises ``AuthorizationExpiredError`` and the
    attempt terminalizes ``authorization_expired``. On the
    pilot those two are 14400 s and 900 s — a 16x gap — so a run parked against
    a four-hour promise died silently a quarter of an hour later while the
    approver's queue still showed hours. Ten of seventy-nine runs on the current
    lineage, and 100% of all failures.

    ⭐ WHAT ACTUALLY FIXES THAT 12.7%, AND IT IS NOT THIS FUNCTION. The fix is
    the DECLARATION: ``deploy/opa/data/ne/config/data.json`` sets 180 s for
    ``write.external.email.send``, so the durable row's ``expires_at`` is 180 s
    and a slow approval dies as a VISIBLE approval expiry instead of a four-hour
    promise stranding as ``authorization_expired`` at minute fifteen. Remove
    that entry and the defect returns intact, with this guard still in place.
    This function is DEMOTED TO A TRIPWIRE on declared promises: it catches a
    deployment that writes down a window its own horizon cannot keep.

    ⭐ SO IT REFUSES ONLY WHAT SOMEBODY DECLARED. ``declared`` is
    ``PolicyDecision.approval_window_declared`` (bundle 1.4.0), true only for
    ``approval_windows.actions[<action>]`` / ``.tiers[<tier>]``. On a
    FLOOR-resolved or ``cfg.expiry_s``-resolved park this emits a LOG WARNING
    (``ne.runtime.middleware``, not an audit row — see the branch itself)
    naming the action, the resolved window and the owner horizon, and then
    lets the park proceed EXACTLY as it does without this check.

    THE REASONING, because it is easy to re-derive backwards: refusal here buys
    LEGIBILITY, NOT SAFETY. The guard protects no integrity property. A park
    that outruns the horizon executes nothing unsafe — its worst case is the
    already-handled ``authorization_expired`` terminalization at the resume
    seam, its best case is a human who answers in time and a run that completes.
    Failing closed is right when proceeding is DANGEROUS and wrong when
    proceeding merely risks a legible failure. A FLOOR IS NOT A PROMISE ANYONE
    MADE: refusing on one punishes an unmade decision, and it is measurably
    expensive — the first cut of this check refused parks that succeed on
    ``main``.

    WHY REFUSE RATHER THAN CLAMP, on the declared side. Clamping the durable
    ``expires_at`` down to the horizon was built and REVERTED (PR #938):
    ``expires_at`` is what ``_require_actionable_row`` tests, so moving it makes
    the row expire as an ORDINARY approval timeout and the run reconciles
    ``EXPIRED`` — losing the specific ``authorization_expired`` reason and its
    atomic terminalization. The two clocks must stay two clocks; this function
    compares them and moves neither.

    WHY AT PARK RATHER THAN AT RESUME. At resume the human decision has already
    been spent on a continuation that cannot run. Refusing here means no
    approval row, no checkpoint, no queue entry and no approver time is
    committed to a promise the deployment cannot keep — the failure becomes an
    immediate, NAMED misconfiguration instead of a mysterious stranding.

    ⛔ AND WHY "JUST DECLARE A WINDOW FOR EVERY PARKABLE ACTION" IS NOT AN
    OPTION. Route 1 — a workflow write leg, ``write.internal.crm.note`` under
    ``write_approval_floor_scope(TIER_2)`` — reaches this function with NO
    policy window at all: the ToolSpec declares tier 1, the PDP is asked about
    tier 1, it answers ``allow`` with a null window, and the tier-2 floor is
    applied afterwards by :func:`_effective_tier`, which the PDP never sees. No
    entry in the data document can intercept that chain, because the chain
    never consults the document. Declaring more windows cannot make this guard
    safe to fire unconditionally; only the provenance test can.

    WHAT THE 180 s DOES NOT BUY. It guarantees an honourable PARK, not a
    completing RESUME: the 230 s subtrahend is ONE attempt's enforced wall
    clock, so at maximum park time the resumed attempt begins with roughly ten
    seconds of the owner's authorization left. See
    ``packages/ne-runtime/tests/test_approval_window_is_honourable.py``, which
    reads every input of that derivation from its enforcement site.

    IT IS ALSO INTERIM. Under the F1 architecture the standing approval survives
    the owner's token, and the value should then be re-raised to whatever the
    business actually wants a human to have. It is small because THIS
    deployment's horizon is small, not because three minutes is the right answer
    to "how long should a person get to approve an external email".

    ⭐ F1 DOES NOT MAKE THIS GUARD VACUOUS, and the argument is close enough to
    warrant stating precisely. Under F1 an approver who answers past the horizon
    is no longer ignored — the decide route preserves the decision as a standing
    approval and the owner's next login consumes it — so on a declared tier-2/3
    park the failure this guard names has become recoverable. Two things
    nonetheless remain its job.

    FIRST, THE DECLARED WINDOW IS A PROMISE ABOUT THIS ATTEMPT. A deployment
    writing 180 s is saying "this park expires in three minutes", not "the
    attempt dies early and a seven-day carryover fires if the owner happens to
    retry". The second may be the better outcome; it is not the declared one,
    and the party that wrote the number down is the one entitled to be told it
    cannot be kept — here, before any approval row, checkpoint or approver
    minute exists.

    SECOND, F1'S RESCUE IS CONDITIONAL AND ITS CONDITION FAILS ON ROUTE 1.
    Minting requires a non-NULL park-time ``standing_lifetime_s``, frozen from
    the PDP's ``standing_approval_seconds``. The workflow write leg described
    below reaches the park with NEITHER clock — the PDP is asked about tier 1
    and answers ``allow`` with a null window and a null standing lifetime — so
    ``_standing_lifetime`` reads NULL, mints nothing, and a decision made past
    the horizon is still lost there exactly as it was before F1. That is also
    the path this guard must NOT refuse, so it warns there and F1 does not help
    there: the two mechanisms do not cover each other.

    THE SCOPE OF THAT NUMBER, STATED RATHER THAN OVERCLAIMED. It is the largest
    window that holds WHEN THE CONSOLE'S RENEW SUCCEEDS — not a worst-case
    bound. ``RUN_CREDENTIAL_MIN_REMAINING_SECONDS`` is a FAIL-OPEN RENEW
    TRIGGER, not a floor: the console returns early when no renew hook is
    installed and swallows a failed renew ("Fail-open to the current token",
    ``packages/ne-console/src/api/client.ts``), and it binds the console alone,
    so a run started from the API or the CLI never passes through it.

    ⚠ THE CANONICAL RUN PARK IS WHERE THIS RUNS, and the CONVERSATION park
    (``DurableApprovalGate.request_approval`` -> ``_submit_fresh``) is not
    guarded — not because the exception could not travel from there, but because
    nothing on that path ENFORCES the horizon against a durable resume: there is
    no ``ne.worker.workflows`` re-binding the run's admitted context around a
    later execution step, so no second clock terminalizes
    ``authorization_expired`` there.

    ⚠ THE REASON USUALLY GIVEN FOR THAT IS FALSE, and it was repeated here, in
    ADR-0030, in ``tiers.rego`` and in ``approval-horizon.md``: that the approval
    is "awaited INLINE in the request that created it". It is not. On the
    workflows proposal surface ``ne.api.workflows`` dispatches its ``_park``
    coroutine with ``asyncio.create_task`` and the HTTP request returns while
    that task polls ``await_outcome`` in the background. The CONCLUSION holds —
    nothing there enforces the horizon — but the reason is the missing resume
    seam, not inlining. ⚠ PRE-EXISTING RESIDUAL, NOT CLOSED HERE AND NOT
    INTRODUCED HERE: an approved conversation effect can therefore fire PAST the
    owner's authorization expiry.

    NO BOUND SNAPSHOT MEANS NO HORIZON TO COMPARE, and this returns. That is not
    a bypass: the canonical Run park always executes inside
    ``authenticated_request_scope`` (``ne.worker.workflows`` binds the run's
    admitted context around every execution step), and any path that reaches a
    protected materialization or a resume without one is refused by
    ``require_authenticated_context`` with its own typed error. Inventing a
    horizon here would be the only way to get this wrong. Pinned by
    ``test_no_bound_context_means_no_horizon_to_compare``.
    """

    context = current_authenticated_context()
    if context is None:
        return
    # EQUALITY IS COHERENT, so it returns here rather than falling through to a
    # finding. The delayed-seam guard refuses at `now >= context.expires_at`,
    # which is the same instant the window itself stops promising anything, so
    # every instant the window offers is an instant the snapshot still covers.
    # Treating equality as an overrun would reject a coherent configuration; only
    # a horizon STRICTLY earlier than the window's end is one.
    if float(context.expires_at) >= (now + timedelta(seconds=expiry_seconds)).timestamp():
        return
    if not declared:
        # The floor/fallback route. Say so — loudly enough to find, quietly
        # enough to keep the park — and proceed exactly as `main` does.
        #
        # A LOG WARNING RATHER THAN AN AUDIT ROW, deliberately. The audit sink is
        # fail-closed by contract (`RuntimeAuditError` aborts the call), so
        # emitting a chain row here would reintroduce, through the sink's own
        # failure mode, exactly the refusal this branch exists to remove. The
        # horizon is safe to name at this seam: it goes to the deployment's own
        # logs, never to `safe_reason` or any run record a wider audience reads.
        _LOG.warning(
            "%s: action=%s resolved_window_s=%s owner_horizon_epoch_s=%s "
            "(no declared window; the park proceeds and may terminalize "
            "authorization_expired at the resume seam)",
            _UNHONOURABLE_FLOOR_EVENT,
            action,
            float(expiry_seconds),
            int(context.expires_at),
        )
        return
    raise ApprovalWindowUnhonourableError(action=action, window_seconds=expiry_seconds)


async def _satisfy_from_standing_approval(
    *,
    gate: DurableApprovalGate,
    checkpoints: Any,
    joined: Any,
    request: ApprovalRequest,
    tier: int,
    args_hash: str,
    policy_hash: str,
    now: datetime,
) -> ConsumedApproval | None:
    """Resume THIS park from a standing approval, or answer ``None`` to park.

    F1, the successor half. The predecessor attempt died at the owner's
    authorization horizon and stays dead; what survived it is the DECISION —
    durable approver-authority evidence bound to
    ``(owner_principal_id, action_hash, policy_hash, protected_invocation_hash)``.
    This park is a successor iff it produces that identical tuple, and the
    caller reached here through the owner's OWN newly authenticated request:
    ``create_wait`` refuses a Run the caller does not own, the Run itself was
    admitted under a fresh verifier-authored context with CURRENT groups, and
    the PDP has ALREADY decided this call at its current tier. Nothing about
    the owner's expired snapshot is read, revived, rebound or re-minted.

    The order of the three steps is the security property, not an
    implementation detail:

    1. FIND (and lapse) — a carryover past its policy lifetime is CAS'd to
       ``approval_lapsed`` and authorizes nothing.
    2. RE-CHECK EVERY STAMP, live, under THIS attempt's context, before
       anything is consumed. A withholding here leaves the standing approval
       STANDING and answers ``None``, so the park waits for a fresh human
       decision — the "owner lost the entitlement at re-login" case degrades to
       asking a human, never to executing.
    3. CONSUME — one atomic at-most-once CAS that re-verifies the whole binding
       (owner, tool name, tier and all three hashes) and lands the decision on
       THIS park's own row and checkpoint.

    Every ``None`` on every branch means the same thing: park for a fresh human
    decision. There is no branch that returns an approval it did not verify.
    """
    finder = getattr(checkpoints, "find_live_standing_approval", None)
    consumer = getattr(checkpoints, "consume_standing_approval", None)
    if finder is None or consumer is None:
        # A checkpoint coordinator without the standing-approval seam cannot
        # verify a carryover, so it may not honour one. Fail closed to a park —
        # the same discipline as an absent source guard below (``None`` means
        # "cannot check", never "no check needed"). The production coordinator
        # (``RunApprovalCheckpointStore``) always has both.
        return None
    standing = await finder(
        owner_principal_id=request.principal.id,
        action_hash=args_hash,
        policy_hash=policy_hash,
        protected_invocation_hash=joined.protected_invocation_hash,
        now=now,
    )
    if standing is None:
        return None
    if standing.tier != int(tier) or standing.action != request.action:
        # Belt to the store's braces. The tuple does not contain the tool name
        # or the tier, and both are part of what the human approved.
        return None
    guard = gate._source_guard
    if guard is None:
        # An unwired composition cannot re-check stamps, so it cannot consume a
        # standing approval either. Fail closed to a human decision.
        return None
    try:
        await checkpoints.check_decision_authority(
            joined.approval_id, guard=guard, operation="standing-successor"
        )
    except (
        ApprovalSourceWithheldFromApproverError,
        RunApprovalAuthorityRetryableError,
        LookupError,
    ):
        # THE THREE ANSWERS THAT ARE NOT "AUTHORIZED, AND NOT A FAILURE EITHER",
        # named one by one:
        #
        # * ``ApprovalSourceWithheldFromApproverError`` — the successor's OWN
        #   authority no longer admits the parked invocation;
        # * ``RunApprovalAuthorityRetryableError`` — the RETRYABLE arm.
        #   Production reaches it on ordinary transient conditions: a missing
        #   head receipt, a missing artifact identity, an ACL snapshot that has
        #   not landed (``current_source_filter.py``), or an invocation that no
        #   longer parses. ``check_decision_authority``'s own docstring calls
        #   the outcome "still pending, retry later";
        # * ``LookupError`` — the join or the stored invocation is unavailable,
        #   or its plaintext hash moved.
        #
        # None of them is evidence that the carryover applies, so none of them
        # may consume it, and none of them may kill the run either: a retryable
        # unknown that raised out of here terminated the attempt with the effect
        # unsent and the standing approval still standing — the run died for a
        # condition that would have cleared on its own. Persist nothing, consume
        # nothing, terminalize nothing; the park stays an ordinary pending
        # approval and a human may still answer it, which is exactly what this
        # function's contract and ``approval-horizon.md`` both promise.
        #
        # ⛔ THIS CLAUSE MUST NAME ITS CLASSES AND MAY NEVER CATCH
        # ``RuntimeError``. :class:`NeRuntimeError` subclasses ``RuntimeError``
        # deliberately, so a bare ``except RuntimeError`` here catches the WHOLE
        # ne-runtime hierarchy — which is what it did, until this round.
        # ``AuthorizationExpiredError`` is the one that costs a terminal: it is
        # raised out of ``resolve_stored_for_approval``'s FIRST statement,
        # ``self.authorization()``, so it is on this chain rather than
        # hypothetical, and an agent loop that crosses the owner's ``exp``
        # between ``create_wait`` and this check reaches exactly it. The
        # specification requires that attempt to terminalize
        # ``authorization_expired``: the raise crosses ``RunExecutionAdapter``
        # into ``RunWorkflowService.execute``, whose ``except
        # AuthorizationExpiredError`` — ``run_workflow.py``'s ONLY one, and NOT
        # on ``RunWorkflowStateStore``, which never names the class — writes
        # that terminal. Swallowed into a ``None`` it becomes a silent PARK: the
        # run sits ``WAITING_APPROVAL`` under an expired owner snapshot, a human
        # is asked to decide an effect that terminalizes anyway, and the audit
        # terminal is never composed. ``MissingAuthenticatedContextError`` and
        # the rest of the ``ProtectedExecutionError`` family reach here the same
        # way and propagate for the same reason. ``RuntimeAuditError`` is not
        # known to be raised on this chain today, but it is the one class this
        # package's module docstring singles out as aborting rather than being
        # swallowed (``runner.py`` does not catch it either), so a widened clause
        # would swallow it on the day it appears. A park where the SPEC mandates
        # a terminal is a WRONG outcome even though it is a safe-looking one.
        return None
    consumed = await consumer(
        joined.approval_id,
        standing_id=standing.standing_id,
        owner_principal_id=request.principal.id,
        action=request.action,
        tier=int(tier),
        action_hash=args_hash,
        policy_hash=policy_hash,
        protected_invocation_hash=joined.protected_invocation_hash,
        now=now,
        event=new_approval_event(
            approval_id=joined.approval_id,
            tier=int(tier),
            decision=ApprovalDecision.approved,
            subject_ref=f"tool:{request.action}",
            actor=standing.approver,
        ),
    )
    if consumed is None:
        return None
    await checkpoints.resume(
        joined.approval_id,
        expected_action_hash=args_hash,
        principal_id=request.principal.id,
        policy_hash=policy_hash,
        resumed_at=now,
    )
    # ONE APPROVAL, ONE EXECUTION — the same durable fence the worker-resume
    # branch above crosses, for the same reason, BEFORE the governed handler
    # runs. Without it a workflow replay of this attempt would find a `resumed`
    # checkpoint and execute the effect a second time; with it the replay finds
    # `consumed`, matches no standing approval (this one is spent), and parks.
    try:
        await checkpoints.consume_resumed(joined.approval_id, consumed_at=now)
    except RuntimeError as exc:
        # ⚠ A BASE-CLASS CATCH, and unlike the authority seam above it is
        # allowed to stay: ``_consume_resumed_sync`` is a pure DB CAS — no
        # ``guard.authorization()``, no audit append — so the only
        # ``RuntimeError`` on this chain is its own lost-CAS raise, and its
        # missing-join answer is a ``LookupError`` that passes straight through.
        # That is a CLASS-BOUNDARY claim about today's chain, not a reachability
        # one: widen what ``consume_resumed`` calls and this clause has to be
        # narrowed with it.
        raise ApprovalIntegrityError("standing-approval continuation was already consumed") from exc
    # THE AUDIT LINKAGE. One record naming all three: the successor attempt that
    # is about to execute, the predecessor attempt that died at the horizon, and
    # the decision that was made for it. Ids, hashes and statuses only — no
    # argument value has ever been in an approval audit record and none is here.
    await gate._audit(
        action="approval.standing.consumed",
        resource=f"tool:{request.action}",
        outcome=AuditOutcome.success,
        actor=request.principal.id,
        detail={
            "tier": int(tier),
            "action": request.action,
            "action_hash": args_hash,
            "policy_hash": policy_hash,
            "protected_invocation_hash": joined.protected_invocation_hash,
        },
        provenance={
            "standing_id": consumed.standing_id,
            "approval_id": joined.approval_id,
            "successor_run_id": joined.run_id,
            "successor_attempt_id": joined.attempt_id,
            "predecessor_approval_id": consumed.predecessor_approval_id,
            "predecessor_run_id": consumed.predecessor_run_id,
            "predecessor_attempt_id": consumed.predecessor_attempt_id,
            "approver": consumed.approver,
        },
    )
    return ConsumedApproval(approver=consumed.approver, approval_id=joined.approval_id)


async def _require_canonical_run_approval(
    *,
    gate: DurableApprovalGate,
    context: RunToolCallContext,
    request: ApprovalRequest,
    args: Mapping[str, Any],
    args_hash: str,
    policy_version: str,
) -> ConsumedApproval:
    """Park or consume one exact atomic Run approval continuation."""
    checkpoints = getattr(gate, "_run_approval_checkpoints", None)
    if checkpoints is None:
        raise ApprovalIntegrityError("canonical Run approval storage is unavailable")
    if request.principal.id != context.run.owner_principal_id:
        raise ApprovalIntegrityError("canonical Run approval principal does not own the Run")
    if not policy_version:
        raise ApprovalIntegrityError("canonical Run approval policy version is unavailable")
    # CONSUME AN ALREADY-RESUMED CONTINUATION FIRST.  A resumed wait has already
    # had (a) a named human's decision landed and (b) every source-authority
    # stamp re-evaluated under the RUN OWNER's bound context by
    # ``authorize(operation="resume")`` — the full governance floor.  What it
    # does NOT have is the ambient evidence carrier: only the original agent
    # loop's reads populate ``current_protected_tool_evidence_refs()``, so the
    # continuation that executes the pinned effect arrives here with an empty
    # carrier.  Re-deriving the evidence set here would refuse the very effect
    # the human approved.  So the pinned wait is read back by its own key —
    # ``(run_id, attempt_id, step_id, tool_call_id, action_hash)``, every
    # component server-minted or hashed from the raw args — and NOTHING about it
    # is re-derived from anything the model can influence.
    resumed_join = await checkpoints.find_resumed_join(
        run_id=context.run.run_id,
        attempt_id=context.receipt.attempt_id,
        step_id=str(context.receipt.step_id),
        tool_call_id=str(context.receipt.tool_call_id),
        action_hash=args_hash,
    )
    if resumed_join is not None:
        row = await gate._store.get(resumed_join.approval_id)
        if row is None or str(row.status) != ApprovalStatus.APPROVED.value:
            raise ApprovalIntegrityError("resumed Run approval is not durably approved")
        # THE TOOL NAME IS PART OF WHAT THE HUMAN APPROVED.  The continuation
        # key pins (run, attempt, step, tool_call_id, args-hash) but NOT the
        # tool name, so without this check a different registered tool invoked
        # with byte-identical arguments would wear an approved effect's
        # identity.  The AUTHORITATIVE name is the IMMUTABLE park-time join
        # (Sol slice 2, P1): an earlier revision compared the request against
        # the mutable ``ne_approval_requests`` row, and a post-decision UPDATE
        # moved both sides of that comparison at once — tautological under
        # exactly the tampering it existed to stop.  The mutable row must still
        # AGREE with the join, and the requested effect must BE the joined
        # action; either drift refuses.
        parked_action = str(getattr(resumed_join, "action", "") or "")
        if not parked_action:
            raise ApprovalIntegrityError("resumed Run approval join names no parked action")
        if str(row.action) != parked_action or request.action != parked_action:
            raise ApprovalIntegrityError(
                "resumed Run approval names a different tool than the requested effect"
            )
        approver = str(row.second_approver or row.first_approver or "")
        if not approver:
            raise ApprovalIntegrityError("resumed Run approval has no named approver")
        # ONE APPROVAL, ONE EXECUTION.  Fence the continuation as consumed
        # BEFORE the governed handler runs: this durable CAS (resumed ->
        # consumed) is what makes a workflow re-execution SETTLE instead of
        # re-issuing the effect.  A lost CAS here means another execution
        # already crossed the fence — refuse rather than double-send.  A crash
        # after this commit and before the send terminalizes FAILED
        # (`RunEffectSettlementUncertain`), which is the safe side.
        try:
            await checkpoints.consume_resumed(resumed_join.approval_id, consumed_at=gate._clock())
        except RuntimeError as exc:
            # Same base-class catch as the standing-approval twin above, kept
            # for the same reason and with the same caveat: the CAS is the only
            # ``RuntimeError`` on this chain today, and a ``LookupError`` for a
            # missing join propagates. Class boundary, not reachability.
            raise ApprovalIntegrityError(
                "resumed Run approval continuation was already consumed"
            ) from exc
        return ConsumedApproval(approver=approver, approval_id=resumed_join.approval_id)
    references = _run_source_references(context)
    stamp_receipt_id, stamp_receipt_hash = _evidence_set_receipt(references)
    source_stamps = canonical_source_authority_stamps(
        tuple(stamp for reference in references for stamp in reference.stamps)
    )
    if len(source_stamps) > _MAX_RUN_APPROVAL_SOURCE_STAMPS:
        # Fail closed HERE rather than park a wait whose stored stamp set the
        # rehydrating ProtectedSourceInvocationRef (max 128 stamps) can never
        # re-admit — that would be an approval no decision could ever resume.
        raise ApprovalIntegrityError(
            "canonical Run approval evidence exceeds the durable stamp-set capacity"
        )
    now = gate._clock()
    tier = int(request.tier)
    cfg = gate._settings.approvals
    # NE-APPROVAL-WINDOW (ADR-0030): the canonical Run park honours the SAME
    # policy-resolved window the conversation park does (`_submit_fresh`). The
    # caller binds it — with its provenance — in `approval_window_scope` around
    # `_require_named_approval`,
    # which is this function's only entry; before this line the Run row silently
    # kept the deployment-wide `cfg.expiry_s`, so the two park paths disagreed
    # about how long the same policy promise lasted. The shared
    # `resolve_approval_deadlines_s` derivation also keeps the escalation SLA
    # STRICTLY inside the effective window — `await_outcome` checks expiry
    # first, so an SLA at-or-past hard expiry silently deletes §4.2
    # escalate-and-notify (the default tier-2 window equals `tier2_sla_s`
    # exactly), and it repeats the scope's `> 0` normalisation so a zero window
    # can never expire the park the instant it is created.
    sla_seconds, expiry_seconds = resolve_approval_deadlines_s(cfg, tier)
    # THE COHERENCE CHECK, before anything durable exists. A DECLARED window the
    # run owner's frozen authorization cannot outlive is a deployment error, and
    # saying so here costs no approval row, no checkpoint and no human decision.
    # A floor-resolved or `cfg.expiry_s`-resolved one is not a promise anyone
    # made, so it warns and parks instead — see the guard's own docstring.
    _require_honourable_approval_window(
        action=request.action,
        now=now,
        expiry_seconds=expiry_seconds,
        declared=current_approval_window_declared(),
    )
    policy_hash = hashlib.sha256(policy_version.encode("utf-8")).hexdigest()
    invocation = json.dumps(
        thaw(dict(args)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    joined = await checkpoints.create_wait(
        run_id=context.run.run_id,
        attempt_id=context.receipt.attempt_id,
        step_id=str(context.receipt.step_id),
        tool_call_id=str(context.receipt.tool_call_id),
        action=request.action,
        action_hash=args_hash,
        tier=tier,
        principal_id=request.principal.id,
        policy_hash=policy_hash,
        invocation_plaintext=invocation,
        source_stamps=source_stamps,
        stamp_receipt_id=stamp_receipt_id,
        stamp_receipt_hash=stamp_receipt_hash,
        requested_at=now,
        sla_deadline=now + timedelta(seconds=sla_seconds),
        expires_at=now + timedelta(seconds=expiry_seconds),
        event=new_approval_event(
            approval_id="pending-server-allocation",
            tier=tier,
            decision=ApprovalDecision.requested,
            subject_ref=f"tool:{request.action}",
            actor=request.principal.id,
            sla_deadline=now + timedelta(seconds=sla_seconds),
        ),
        standing_lifetime_s=current_standing_lifetime_s(),
    )
    checkpoint = await checkpoints.load_checkpoint(
        joined.approval_id,
        expected_action_hash=args_hash,
        principal_id=request.principal.id,
        policy_hash=policy_hash,
    )
    if checkpoint.status != "resumed":
        satisfied = await _satisfy_from_standing_approval(
            gate=gate,
            checkpoints=checkpoints,
            joined=joined,
            request=request,
            tier=tier,
            args_hash=args_hash,
            policy_hash=policy_hash,
            now=now,
        )
        if satisfied is None:
            raise RunApprovalWaiting(joined)
        return satisfied
    row = await gate._store.get(joined.approval_id)
    if row is None or str(row.status) != ApprovalStatus.APPROVED.value:
        raise ApprovalIntegrityError("resumed Run approval is not durably approved")
    approver = str(row.second_approver or row.first_approver or "")
    if not approver:
        raise ApprovalIntegrityError("resumed Run approval has no named approver")
    return ConsumedApproval(approver=approver, approval_id=joined.approval_id)


def _effective_tier(spec: ToolSpec, decision: PolicyDecision) -> ApprovalTier:
    """The STRICTER-OF gate tier: max(PDP-decided, spec, python-mirror) + obligations.

    NE-CKD3 (Checkpoint-D finding #3): the gate tier is the max of the spec's
    declared tier, the ``tiers.rego`` Python mirror (``tier_for_action``), the
    PDP-decided tier (``decision.tier``), and obligation floors. ``max()`` means
    the decided tier and obligations can only ESCALATE gating, never lower it —
    a rogue ``tier=-5`` / ``tier=0`` / ``null`` can never undercut the spec/mirror
    floor, and a ``tier`` above the ladder clamps to ``TIER_3`` (the strictest
    tier that exists). The Python mirror stays a defense-in-depth floor, never a
    ceiling.

    Obligation floors (mirroring ``tiers.rego`` obligation semantics):
      * ``second_check`` or ``named_approver`` ⇒ Tier-3 (named + distinct 2nd);
      * ``per_item_human_approval``            ⇒ Tier-2 (per-item human approval).

    NE-WF-SUBJECT-GATE: for ``write.*`` actions the ambient
    :func:`write_approval_floor_scope` floor is one more candidate — the caller
    layer (a workflow owing each subject a human decision) escalating its own
    write legs independently of the action's declared tier. Like every other
    candidate it can only ESCALATE (it joins the ``max()``), and it never touches
    reads or drafts.
    """
    candidates: list[int] = [int(spec.tier), int(tier_for_action(spec.name))]
    if decision.tier is not None:
        candidates.append(int(decision.tier))
    obligations = decision.obligations
    if obligations.get("second_check") or obligations.get("named_approver"):
        candidates.append(3)
    if obligations.get("per_item_human_approval"):
        candidates.append(2)
    ambient_floor = current_write_approval_floor()
    if ambient_floor is not None and spec.name.startswith("write."):
        candidates.append(int(ambient_floor))
    return ApprovalTier(min(max(candidates), int(ApprovalTier.TIER_3)))


def _approval_window_s(decision: PolicyDecision) -> float | None:
    """The action's approval window in seconds, as the POLICY decided it.

    ``tiers.rego`` resolves ``approval_window_seconds`` per action (exact action,
    then per tier, then a built-in floor), ``decision.rego`` emits it FIRST-CLASS
    on its require-approval rules, and the decision carries it on the typed
    ``PolicyDecision.approval_window_seconds`` field (ADR-0030). Bundle 1.4.0
    adds ``approval_window_declared`` beside it — a BOOL, so not which of those
    three steps answered but only whether a DECLARED entry
    (``approval_windows.actions[<action>]`` / ``.tiers[<tier>]``) did, or the
    built-in floor — read straight off the decision by the caller and bound with the
    window in ``approval_window_scope``; there is nothing to normalise, because
    the scope refuses to call a normalised-away window declared. The
    field — never an obligation: obligations are the Rego/Cedar parity surface,
    and Cedar's obligations are static per-rule annotations that cannot express
    a data-resolved value. Reading the obligation here was this defect: nothing
    ever put the value there, so the fallback to ``cfg.expiry_s`` fired on every
    path that ever executed.

    This is the only reader, and it is deliberately unforgiving: anything that
    is not a positive FINITE number returns ``None``, which falls back to the
    deployment default. ``bool`` is rejected explicitly because
    ``isinstance(True, int)`` is True in Python, so ``True`` would otherwise
    become a ONE-SECOND window — an approval that expires before a human can
    read it, which is exactly the failure this machinery exists to remove. A
    malformed value must degrade to the default, never to something stricter
    than anyone chose. (The contract validator and ``approval_window_scope``
    guard the same edges; a normalisation this load-bearing is cheap to repeat
    and catastrophic to lose.)
    """
    raw = decision.approval_window_seconds
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    window = float(raw)
    return window if math.isfinite(window) and window > 0 else None


def _standing_lifetime_s(decision: PolicyDecision) -> float | None:
    """The action's STANDING-approval lifetime in seconds, as the POLICY decided it.

    ``tiers.rego`` resolves ``standing_approval_seconds`` per action beside the
    window (exact action, then per tier, then a built-in floor), ``decision.rego``
    1.5.0 emits it first-class on its four require-approval rules, and the
    decision carries it on ``PolicyDecision.standing_approval_seconds``.

    Same unforgiving normalisation as :func:`_approval_window_s`, and one
    additional consequence worth stating out loud: here ``None`` does not fall
    back to a deployment default, because there is none. ``None`` means MINT NO
    STANDING APPROVAL — the decision dies with its attempt, exactly as it did
    before F1. A malformed value must never be able to grant a longer carryover
    than someone chose, and "policy said nothing" must never widen into
    "standing forever".
    """
    raw = decision.standing_approval_seconds
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    lifetime = float(raw)
    return lifetime if math.isfinite(lifetime) and lifetime > 0 else None


def scoped_tool_executor(
    executor: _ToolsetScopableExecutor,
    *,
    granted: Sequence[str],
    scope: str,
) -> ToolsetScopedExecutor:
    """Return a handler-less, least-privilege view over one frozen executor.

    This module-level factory keeps ``execute`` as the executor object's only public
    callable (the C6 guardian's capability-minimisation invariant).  It also keeps
    the executor's owned PDP and sink private to this module while giving the
    standard :class:`~ne.runtime.toolset.ToolsetScopedExecutor` those exact governed
    dependencies.  The returned wrapper owns no handlers; its only successful path
    delegates back to ``executor.execute``.
    """
    from ne.runtime.toolset import ToolsetScopedExecutor

    sink, pdp = executor._toolset_authority()
    return ToolsetScopedExecutor(
        executor,
        granted=granted,
        sink=sink,
        scope=scope,
        pdp=pdp,
    )


class GovernedToolExecutor:
    """The sole, non-bypassable path to any tool handler (C6)."""

    def __init__(
        self,
        bindings: Sequence[ToolBinding],
        *,
        pdp: PolicyDecisionPoint,
        guardrails: GuardrailEngine,
        approval_gate: ApprovalGate,
        sink: AuditSink,
    ) -> None:
        if pdp is None:
            raise TypeError("GovernedToolExecutor requires a non-None pdp (C6)")
        if guardrails is None:
            raise TypeError("GovernedToolExecutor requires a non-None guardrails engine (C6)")
        if approval_gate is None:
            raise TypeError("GovernedToolExecutor requires a non-None approval_gate (C6)")
        if sink is None:
            raise TypeError("GovernedToolExecutor requires a non-None audit sink (C6)")
        bindings = tuple(bindings)
        for binding in bindings:
            validate_binding(binding)
        self._bindings: dict[str, ToolBinding] = {b.spec.name: b for b in bindings}
        self._pdp = pdp
        self._guardrails = guardrails
        self._approval_gate = approval_gate
        self._sink = sink

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """The registered tool SPECS (never handlers) — for catalog rendering."""
        return tuple(b.spec for b in self._bindings.values())

    def _toolset_authority(self) -> tuple[AuditSink, PolicyDecisionPoint]:
        """Return the exact audit/PDP pair owned by this governed executor.

        This private composition seam lets handler-less receipt wrappers preserve
        the same fail-closed authority when a per-Agent toolset is applied after
        Run receipt gating.  It exposes no binding or handler capability.
        """
        return self._sink, self._pdp

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        """Run ``tool`` with ``args`` IFF guardrails pass, policy allows, and (for
        require-approval) a named human approval is granted. Every path audits."""
        binding = self._bindings.get(tool)
        if binding is None:
            # (0) Audit the unknown tool FIRST (fail closed: if this append fails,
            # RuntimeAuditError propagates and the branch aborts before guardrails/PDP).
            await self._emit(
                action="tool.unknown",
                resource=f"tool:{tool}",
                outcome=AuditOutcome.denied,
                actor=principal.id,
                detail={"reason": "tool not registered"},
            )
            # Governance still runs so the record is complete. Guardrails first: the
            # engine audits the block (rule toolargs.unknown-tool); catch ONLY
            # GuardrailViolation so the branch can finish — GuardrailEvaluationError /
            # AuditEmissionError propagate (fail closed). No tier exists for an
            # unregistered action, so context is left at its FrozenDict default.
            payload = build_tool_call_payload(
                principal=principal,
                tool=tool,
                args=args,
                recipients=(),
                egress_urls=(),
                estimated_cost_usd=None,
            )
            # Suppress ONLY GuardrailViolation (the engine already audited the block)
            # so the branch can reach the PDP; GuardrailEvaluationError /
            # AuditEmissionError are NOT suppressed and propagate (fail closed).
            with contextlib.suppress(GuardrailViolation):
                await self._guardrails.enforce(payload)
            decision = await self._pdp.decide(
                PolicyRequest(principal=principal, action=tool, resource=f"tool:{tool}")
            )
            # Terminal: an unknown tool NEVER executes, regardless of the effect.
            raise ToolNotRegisteredError(
                f"no tool binding registered under {tool!r} "
                f"(policy effect: {decision.effect.value}; never executed)"
            )

        spec = binding.spec

        # (1) C12 guardrails FIRST. Violations/eval-errors/audit-failures propagate
        # UNWRAPPED — the engine already audited them; do not catch-and-continue.
        payload = build_tool_call_payload(
            principal=principal,
            tool=spec.name,
            args=args,
            recipients=binding.extract_recipients(args) if binding.extract_recipients else (),
            egress_urls=binding.extract_egress_urls(args) if binding.extract_egress_urls else (),
            estimated_cost_usd=(
                binding.estimate_cost_usd(args) if binding.estimate_cost_usd else None
            ),
        )
        await self._guardrails.enforce(payload)

        # (2) PDP — GovernedPdp emits the pdp.decide AuditEvent by construction.
        #
        # NE-T2X-5 (design §6.3, A-P1-5): read the ambient provisional-context
        # snapshot ONCE and thread it as DATA onto the PDP input. With no
        # active ``provisional_context_scope``, ``snapshot`` is ``None`` and
        # the context dict below is BYTE-IDENTICAL to before this unit (design
        # §2.5 default-identity) — every existing caller is unaffected, INCLUDING
        # toolset.py's out-of-set PDP consult and dispatch.py's subagent.route
        # consult, which build their OWN context dicts and are DELIBERATELY
        # left unchanged here (terminal-refusal / non-write-class paths — the
        # escalation policy only ever fires on a `write.*` action). When a
        # scope IS active the flag threads even when it is ``False`` and even
        # in ``autonomous`` mode — "threads the flag" is literal; the policy
        # bundle (both backends), never this call site, decides whether it
        # escalates.
        snapshot = current_provisional_context()
        context: dict[str, Any] = {"tier": int(spec.tier)}
        if snapshot is not None:
            context["context_provisional"] = snapshot.provisional
            if snapshot.extraction_mode is not None:
                context["extraction_mode"] = snapshot.extraction_mode

        decision = await self._pdp.decide(
            PolicyRequest(
                principal=principal,
                action=spec.name,
                resource=f"tool:{spec.name}",
                context=context,
            )
        )
        if decision.effect is DecisionEffect.DENY:
            raise ToolDeniedError(decision.reason, decision.policy_version)

        # (2b) NE-ACTION-VALIDATE-BEFORE-GATE — binding-owned typed pre-gate
        # validation, AFTER guardrails+PDP but BEFORE the human approval gate below.
        # A call whose typed input cannot execute (missing required field, undeclared
        # key, missing idempotency identity) is refused HERE: audited as
        # tool.validate[denied], never queued as a pending approval — an invalid call
        # never consumes a human decision (the live defect this unit closes).
        if binding.validate_args is not None:
            try:
                binding.validate_args(args)
            except ActionValidationError as exc:
                # RuntimeAuditError from _emit propagates: a refusal that cannot be
                # audited still refuses (C6) — it does NOT fall through to the gate.
                await self._emit(
                    action="tool.validate",
                    resource=f"tool:{spec.name}",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    detail={"error_class": type(exc).__name__},
                )
                raise
            except Exception as exc:
                # The validator itself crashed (not a typed ActionValidationError):
                # fail closed. Audit, then refuse with a wrapped ActionValidationError
                # naming only the crashing error CLASS — never its message/values (C12).
                await self._emit(
                    action="tool.validate",
                    resource=f"tool:{spec.name}",
                    outcome=AuditOutcome.denied,
                    actor=principal.id,
                    detail={"error_class": type(exc).__name__},
                )
                # ``from None``: naming only the crashing CLASS in the message is half
                # the discipline — a chained cause puts that crash's own message AND
                # its ``input_value=``-bearing pydantic ancestors back into
                # ``traceback.format_exception`` output, which is what a
                # ``logger.exception`` here would ship. The class name is preserved in
                # BOTH the audit row above and this message, so severing costs no
                # triage signal. Chain-swept by
                # ``tests/test_action_validate_before_gate.py``.
                raise ActionValidationError(
                    f"input validation for {spec.name!r} could not run "
                    f"({type(exc).__name__}); the call is refused (fail closed)"
                ) from None

        # (3) HITL — gate on the STRICTER-OF effective tier (NE-CKD3). Route to
        # the human when the bundle says require-approval OR the effective tier is
        # >= 2. The `int(tier) >= 2` arm is the defense-in-depth FLOOR: an ALLOW
        # for an action whose spec/mirror/decided class is Tier-2+ still requires
        # the human (C12: a hijacked bundle cannot grant Tier-3 without a human;
        # the mirror is a floor, never a ceiling; approval is strictly stricter
        # than allow). DENY already returned terminal-deny above (strictest).
        tier = _effective_tier(spec, decision)
        approval: ConsumedApproval | None = None
        if decision.effect is DecisionEffect.REQUIRE_APPROVAL or int(tier) >= 2:
            # args_hash (Argus re-check P1, round 2): computed over the RAW args
            # Mapping BEFORE any validation, so a genuinely distinct concurrent
            # call (pydantic-ai dispatches a turn's tool calls concurrently via
            # asyncio.create_task, no sequential guard) never collapses onto the
            # SAME approval row as another same-action/tier/principal/scope call.
            # NE-APPROVAL-WINDOW: bind the POLICY-resolved window for this action
            # around the park. Ambient rather than a parameter — ApprovalRequest is
            # frozen and widening the gate signature breaks protocol-typed stand-ins,
            # including ones monkeypatched onto a real DurableApprovalGate, which the
            # isinstance guard cannot protect.
            # F1 binds the SECOND clock in the same breath: how long a decision
            # on this action outlives the attempt it was asked for. Same ambient
            # mechanism, same reason (frozen signatures), different question —
            # `_approval_window_s` bounds how long the human has to ANSWER,
            # `_standing_lifetime_s` bounds how long that ANSWER survives.
            with (
                approval_window_scope(
                    _approval_window_s(decision),
                    declared=decision.approval_window_declared,
                ),
                standing_lifetime_scope(_standing_lifetime_s(decision)),
            ):
                approval = await self._require_named_approval(
                    spec,
                    principal,
                    tier=tier,
                    args_hash=canonical_args_hash(args),
                    args=args,
                    provisional=snapshot,
                    policy_version=decision.policy_version,
                )

        # (4) Execute — audit success/error with NO raw arg values, carrying the
        # id of the approval THIS call consumed (B14; None when ungated).
        return await self._run_handler(binding, args, principal, approval=approval)

    async def _require_named_approval(
        self,
        spec: ToolSpec,
        principal: Principal,
        *,
        tier: ApprovalTier,
        args_hash: str,
        args: Mapping[str, Any],
        provisional: ProvisionalContext | None = None,
        policy_version: str,
    ) -> ConsumedApproval:
        # NE-T2X-5 (design §6.3): when this turn's context was provisional,
        # the approval payload lists WHICH facts were unconfirmed — IDENTIFIERS
        # AND COUNTS ONLY, never fact text or displays (C12 redaction
        # discipline). The refs are the writer-stamped `llm:<sha16>:<ref>`
        # source_ref grammar (design §3.2/§6.1) — opaque identifiers, not
        # content — so an approver sees WHAT to look up (via the console's
        # flywheel deep-link) without the payload ever carrying the fact
        # itself.
        #
        # Owner ruling 2026-07-31 supersedes the PRIOR claim here (that
        # ``payload`` stayed exactly ``{"tool": spec.name}``): an approver who
        # cannot see what they are approving is not approving anything, so the
        # payload now ALSO embeds a ``disclosure`` object — the RAW tool-call
        # ``args`` (as ``material_parameters``), a target-resource label, the
        # tool's own description, and a tier-derived reversibility string. This
        # material is served EXCLUSIVELY through
        # ``DurableApprovalGate.read_payload``'s named-approver-gated, audited
        # read (see ``approvals.py``) — it is NEVER joined onto
        # ``list_pending``, and audit events (``tool.approval`` below, and
        # every ``approval.*``/``api.approval.*`` record) still NEVER carry a
        # raw arg value (C12 unweakened — see ``test_redteam_c6.py``).
        payload: dict[str, object] = {
            "tool": spec.name,
            "disclosure": {
                "target_resource": f"tool:{spec.name}",
                "description": spec.description,
                "material_parameters": dict(args),
                "reversibility": reversibility_label(int(tier)),
            },
        }
        if provisional is not None and provisional.provisional:
            payload["provisional_source_count"] = provisional.provisional_source_count
            payload["provisional_source_refs"] = list(provisional.provisional_source_refs)
        request = ApprovalRequest(
            tier=tier,
            action=spec.name,
            principal=principal,
            payload=payload,
        )
        # args_hash rides OUT-OF-BAND, never on ApprovalRequest itself: the
        # ne-contracts ApprovalRequest model is FROZEN at 1.10.0 and its payload
        # is deliberately redacted to {'tool': spec.name} (C12) — adding a field
        # would need a version bump, out of scope for this unit. The isinstance
        # branch keeps every protocol-typed ApprovalGate fake (test_middleware.py)
        # working unmodified with no args_hash awareness at all; pyright narrows
        # cleanly on the isinstance check.
        gate = self._approval_gate
        run_context = current_run_tool_call()
        if run_context is not None:
            if not isinstance(gate, DurableApprovalGate):
                raise ApprovalIntegrityError(
                    "canonical Run approval requires the durable production gate"
                )
            return await _require_canonical_run_approval(
                gate=gate,
                context=run_context,
                request=request,
                args=args,
                args_hash=args_hash,
                policy_version=policy_version,
            )
        if isinstance(gate, DurableApprovalGate):
            outcome = await gate.request_approval(request, args_hash=args_hash)
        else:
            outcome = await gate.request_approval(request)
        approver = outcome.approver
        # B14 (ADR-0019): the id of the approval this outcome decided. Stamped
        # onto BOTH branches' tool.approval records (the deny leg's record names
        # the approval that REFUSED it) and returned to execute() so the
        # tool.execute emit carries the join. None (key omitted) when the gate
        # recorded no durable id — never invented.
        approval_id = outcome.approval_id or None
        if outcome.status is ApprovalStatus.APPROVED and isinstance(approver, str) and approver:
            provenance = {"approver": approver}
            if approval_id is not None:
                provenance["approval_id"] = approval_id
            await self._emit(
                action="tool.approval",
                resource=f"tool:{spec.name}",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail={"status": outcome.status.value, "tier": int(tier)},
                provenance=provenance,
            )
            # The ONE site that holds both the gate-recorded approver and the
            # durable approval id — thread them out together so _run_handler can
            # bind the pair to the consumed-approval carrier (never rebuilt later).
            return ConsumedApproval(approver=approver, approval_id=approval_id)

        await self._emit(
            action="tool.approval",
            resource=f"tool:{spec.name}",
            outcome=AuditOutcome.denied,
            actor=principal.id,
            detail={"status": outcome.status.value, "tier": int(tier)},
            provenance={"approval_id": approval_id} if approval_id is not None else None,
        )
        if outcome.status is ApprovalStatus.APPROVED:
            # APPROVED but no named approver: tier >= 2 can NEVER auto-approve.
            raise ApprovalIntegrityError(
                f"tier {tier} approval for {spec.name!r} was APPROVED without a named "
                "approver; auto-approval is structurally forbidden (SPEC §4.2)"
            )
        raise ApprovalNotGrantedError(outcome.status)

    async def _run_handler(
        self,
        binding: ToolBinding,
        args: Mapping[str, Any],
        principal: Principal,
        *,
        approval: ConsumedApproval | None = None,
    ) -> str:
        spec = binding.spec
        # B14: the join rides provenance (an id, nothing else — C12) on both
        # tool.execute outcomes; the key is OMITTED when no approval ran.
        approval_id = approval.approval_id if approval is not None else None
        join = {"approval_id": approval_id} if approval_id is not None else None
        if approval_id is not None and isinstance(self._approval_gate, DurableApprovalGate):
            # A human decision is not a bearer token across claim epochs.  Recheck
            # the exact persisted Conversation/turn/attempt immediately before
            # any handler can produce an external side effect.
            await self._approval_gate.assert_current_attempt(approval_id)
        # D-1: bind the VERIFIED principal to the surface-independent carrier around
        # the ENTIRE handler+emit body, reset in `finally`. The finally covers the
        # exception path AND nested-execute restore: reset(token) restores the PARENT
        # executor's value, so a sub-agent executing as a distinct principal Q inside
        # parent P's handler is isolated and P is restored after Q returns.
        # The consumed-approval carrier is bound with the SAME lifecycle — set
        # unconditionally (a None SHADOWS any parent call's approval, so a nested
        # ungated execute can never inherit and stamp the parent's approval),
        # reset in the same finally.
        token = _TOOL_PRINCIPAL.set(principal)
        approval_token = _TOOL_APPROVAL.set(approval)
        try:
            try:
                result = await binding.handler(args)
            except Exception as exc:
                # The operator's channel FIRST, before the ledger append that can
                # itself fail: a RuntimeAuditError from _emit must not be the
                # reason the cause goes unrecorded a second time. The message is
                # projected (bounded, redacted, never a StatementError's str())
                # rather than logged through exc_info, because an exception
                # message in this repo may interpolate a tool ARGUMENT — the
                # governed read.trace tool raises f"trace not found: {trace_id}".
                _LOG.error(
                    "tool %r handler failed at %s: %s",
                    spec.name,
                    failure_site(exc) or "<no traceback>",
                    " | ".join(describe_failure(exc)),
                )
                await self._emit(
                    action="tool.execute",
                    resource=f"tool:{spec.name}",
                    outcome=AuditOutcome.error,
                    actor=principal.id,
                    # C12: class, raise SITE, driver code, wrapped origin — code
                    # identifiers, never a message and never an argument value.
                    # error_class stays first and unchanged; the rest is additive.
                    detail=failure_fields(exc),
                    provenance=join,
                )
                raise ToolExecutionError(
                    f"tool {spec.name!r} handler raised {type(exc).__name__}"
                ) from exc
            # Binding-owned write-audit detail (NE-050 C7-write): on the SUCCESS path
            # only, a trusted binding may derive redacted detail (what-action /
            # what-target / payload-SHAPE — never values) for the tool.execute[success]
            # event. The extractor runs inside try/except: the write ALREADY happened,
            # so a buggy extractor can NEVER suppress the audit event — it degrades to
            # an error class name, and the config_hash-stamped success event is still
            # emitted (fail safe).
            detail: Mapping[str, Any] | None = None
            if binding.extract_audit_detail is not None:
                try:
                    detail = binding.extract_audit_detail(args, result)
                except Exception as exc:
                    detail = {"audit_detail_error": type(exc).__name__}
            await self._emit(
                action="tool.execute",
                resource=f"tool:{spec.name}",
                outcome=AuditOutcome.success,
                actor=principal.id,
                detail=detail,
                provenance=join,
            )
            return result
        finally:
            _TOOL_APPROVAL.reset(approval_token)
            _TOOL_PRINCIPAL.reset(token)

    async def _emit(
        self,
        *,
        action: str,
        resource: str,
        outcome: AuditOutcome,
        actor: str,
        detail: Mapping[str, Any] | None = None,
        provenance: Mapping[str, str] | None = None,
    ) -> None:
        # Event construction (which resolves the FROZEN config hash) is OUTSIDE the
        # try, so ConfigHashNotFrozenError propagates rather than being swallowed
        # into RuntimeAuditError (plan §1.4 / SPEC §4). detail/provenance carry only
        # tool names / tiers / statuses / error classes — never raw arg values.
        prov = dict(_PROVENANCE_SOURCE)
        if provenance:
            prov.update(provenance)
        event = new_audit_event(
            action=action,
            resource=resource,
            outcome=outcome,
            actor=actor,
            detail=json.dumps(detail) if detail is not None else None,
            provenance=prov,
            trace_id=current_run_trace_id(),
        )
        try:
            await self._sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                f"audit append failed for {action!r} on {resource!r}; "
                "result withheld (C6 fail closed)"
            ) from exc
