# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for ne-runtime (the C6 tool-call chokepoint).

Every failure surface of the :class:`~ne.runtime.middleware.GovernedToolExecutor`
fails CLOSED: a denied policy decision, a guardrail block, an un-granted or
integrity-violating approval, a handler error, and — critically — the
middleware's OWN audit-append failure each abort the tool call rather than
letting an un-audited or un-authorized effect through. This mirrors
``ne.govern.errors`` (``AuditEmissionError`` semantics) at the runtime seam.
"""

from __future__ import annotations

from typing import Literal

from ne.contracts.governance import ApprovalStatus


class NeRuntimeError(RuntimeError):
    """Base class for every error raised by ne-runtime.

    Subclasses :class:`RuntimeError` deliberately (U2 adversarial review, F8):
    API seams map ``except RuntimeError`` to retryable/safe HTTP failures, and a
    typed ne-runtime refusal (e.g. ``AuthorizationExpiredError``) escaping those
    handlers as an unaudited 500 was a defect, not a design. Handlers that must
    treat a specific subclass differently catch that subclass FIRST.
    """


class ToolNotRegisteredError(NeRuntimeError):
    """The requested tool has no binding in the executor (unknown tool, refused)."""


class ToolRegistrationError(NeRuntimeError):
    """A tool binding is unclassifiable or under-tiered (fail-loud at registration).

    Raised when a tool's action name matches no tier class (deny-by-default,
    mirrored from the Rego), or when its declared tier is *below* the class tier
    the action string implies (a misconfiguration that would understate control).
    """


class ToolDeniedError(NeRuntimeError):
    """The PDP returned DENY; the tool is not executed (C6 fail closed).

    Carries the policy ``reason`` and ``policy_version`` from the decision — the
    decision itself was already audited by the ``GovernedPdp`` by construction.
    """

    def __init__(self, reason: str, policy_version: str) -> None:
        self.reason = reason
        self.policy_version = policy_version
        super().__init__(f"policy denied the tool call: {reason} (policy {policy_version})")


class ApprovalNotGrantedError(NeRuntimeError):
    """A require-approval tool was not APPROVED (rejected/expired/escalated).

    Carries the terminal :class:`~ne.contracts.governance.ApprovalStatus`; the
    handler was never invoked and the outcome was audited.
    """

    def __init__(self, status: ApprovalStatus) -> None:
        self.status = status
        super().__init__(f"approval not granted: {status.value}")


class ApprovalIntegrityError(NeRuntimeError):
    """An APPROVED outcome carried no named approver at tier >= 2 (never auto-approve).

    SPEC §4.2: Tier-2/3 requests can NEVER auto-approve. An ``APPROVED`` outcome
    without a non-empty ``approver`` is treated as an integrity violation — the
    tool is not executed — rather than trusted as a valid approval.
    """


class RunApprovalContinuationExhaustedError(ApprovalIntegrityError):
    """A Run already holds an approval continuation, so a SECOND gate refuses.

    ``ne_agent_run_continuation_checkpoints.run_id`` and
    ``ne_agent_run_approval_joins.run_id`` are both UNIQUE: a Run may hold ONE
    approval continuation, ever. That is not a storage accident — TWO seams read
    the pair ``(run_id, attempt_id)`` as an at-most-one address and strand the
    Run if it is not: ``RunWorkflowStateStore.resumed_approval_id`` names the ONE
    effect a resume may execute and raises when it finds two, and
    ``RunApprovalCheckpointStore.find_join_for_attempt`` — the drain that gives
    every park its MANDATORY audit receipt — is a ``one_or_none()``. A third
    seam, ``RunFinalizer._observed_approvals``, is NOT one of them and must not
    be cited as one: it ITERATES the joins for the attempt and its "exactly one"
    is one audit receipt PER JOIN, which N joins would each satisfy. Two seams
    are enough — either one strands the Run on its own — so the constraint
    stands on them.

    Nothing had ever driven a second approval-gated tool call in one Run, on
    any path, so the constraint was never observed being hit: it aborted the
    insert with a raw ``IntegrityError`` out of ``create_wait``, after the first
    effect had already been sent. F1 is what made that easy to reach — a
    satisfied park RETURNS and the agent loop runs on, with no human in it. The
    older ``resumed`` continuation branch of ``_require_canonical_run_approval``
    also returns and runs on, so it is not obviously safe from this either; the
    guard below covers both, because it sits on the ``create_wait`` insert that
    is common to them.

    So the limit is stated here instead, as a governance refusal raised BEFORE
    anything durable is written. It is an :class:`ApprovalIntegrityError`, which
    puts it in the runner's ``_BLOCKING_ERRORS``: the second effect becomes a
    BLOCKED tool result the model is told about, the run reaches its own
    terminal, and nothing is stranded. Lifting the one-continuation limit means
    reworking the three seams above, not relaxing the constraint.
    """


class ApprovalWindowUnhonourableError(NeRuntimeError):
    """A DECLARED approval window is longer than the deployment can honour.

    THE COHERENCE CHECK (docs/architecture/approval-horizon.md, remaining item
    2). Two independent clocks bound a parked approval and NOTHING used to
    compare them:

    * the APPROVAL WINDOW — how long a human gets to decide — is a per-action
      POLICY property (``tiers.rego``'s ``approval_window_seconds``, ADR-0030),
      resolved per park and written into the durable row's ``expires_at``;
    * the CALLER'S AUTHORIZATION HORIZON — the ``expires_at`` of the frozen
      verifier-authored snapshot bound for the request (the run owner's, on the
      canonical Run park) — is what every delayed seam re-checks, and past it
      the resume raises
      :class:`~ne.runtime.source_results.AuthorizationExpiredError` and the
      attempt terminalizes ``authorization_expired``.

    ⭐ IT IS A TRIPWIRE ON A DECLARED PROMISE, NOT THE FIX FOR THE OUTAGE. The
    fix is the DECLARATION itself: ``deploy/opa/data/ne/config/data.json``
    declares 180 s for ``write.external.email.send``, which makes the durable
    row's ``expires_at`` 180 s, so a slow approval dies as a visible APPROVAL
    EXPIRY instead of a four-hour promise stranding as ``authorization_expired``
    at minute fifteen. Delete that entry and the measured 12.7% failure returns
    intact with this class still in the tree. What this class adds is a loud
    failure when a deployment writes down a window its own horizon cannot keep.

    ⭐ SO IT IS RAISED ONLY FOR A DECLARED WINDOW —
    ``approval_windows.actions[<action>]`` or ``.tiers[<tier>]``, carried as
    ``PolicyDecision.approval_window_declared`` (bundle 1.4.0). A window that
    came from ``tiers.rego``'s built-in floor, or from the ``ApprovalSettings
    .expiry_s`` fallback when policy named no window at all, does NOT raise:
    :func:`ne.runtime.middleware._require_honourable_approval_window` emits a
    LOG WARNING on the ``ne.runtime.middleware`` logger — NOT an audit row, and
    do not go looking for one in the chain — and lets the park proceed.
    Refusing there would buy
    legibility, not safety — a park that outruns the horizon executes nothing
    unsafe; its worst case is the already-handled ``authorization_expired``, its
    best case succeeds — and a floor is not a promise anyone made.

    ⚠ RAISED FROM THE CANONICAL RUN PARK ONLY
    (``_require_canonical_run_approval``), because that is the only park with a
    durable resume seam for the horizon to collide with. The conversation park
    (``DurableApprovalGate.request_approval``) writes ``expires_at`` from the
    same policy-resolved window but has no such seam, so nothing there enforces
    the horizon at all — with the pre-existing consequence, not introduced here,
    that an approved conversation effect can fire past the owner's
    authorization expiry.

    When the window is the LONGER of the two, the platform creates an approval
    it advertises as answerable for hours that in fact stops being executable
    at the horizon: on the pilot the tier-2 policy floor is 14400 s against a
    900 s session, a 16x gap, and the run died silently ~15 minutes in with the
    approver's queue still showing hours. Raised BEFORE the wait is created,
    so no approval row, no checkpoint and no human decision is ever spent on a
    continuation that could not run.

    THE GAP IS NAMED, NOT CLOSED. Making a parked effect outlive the owner's
    session needs a durable run-scoped authorization grant, which contradicts
    the specification verbatim ("only a new authenticated retry may run") and
    therefore needs a SPEC amendment, not a code change. Raising
    ``accessTokenLifespan`` to close the gap is rejected on two counts: the
    owner ruled twice that the approval window is a per-action policy setting
    and must never be tied to session credential lifetime, and the lifespan is
    simultaneously the replay window of a stolen stateless bearer and the
    staleness window of every governed read platform-wide
    (approval-horizon.md section 4). What is left is to configure a window the
    deployment can keep — 180 s, derived from three enforcement sites (the
    console's 420 s run-credential renew trigger, the 230 s enforced
    per-attempt wall clock, and the 10 s of clock skew OIDC verification
    budgets) and scoped rather than bounded, since the console's renew is
    fail-open and binds only the console. ⚠ INTERIM: under the F1 architecture
    the standing approval survives the owner's token and the value should be
    re-raised to whatever the business wants. It guarantees an honourable PARK,
    not a completing RESUME — at maximum park time the resumed attempt has
    ~10 s of horizon left.

    WHAT ``safe_reason`` MAY DISCLOSE. It names the CONFIGURED WINDOW and the
    ACTION — both public deployment policy, the window already emitted
    first-class on every decision — and the BARE FACT that the horizon is
    shorter. It never carries the horizon, the remaining seconds, or the
    overrun: publishing ``window - remaining`` would make the owner's exact
    token expiry derivable from public policy config, which is the defect that
    sank the first cut of this check. (The pending queue separately serves
    ``authorization_expires_at`` to a NAMED APPROVER on a run-bound row --
    ``ne.api.routers.approvals``, admitted 2026-08-29 -- so nothing here widens
    what an approver already sees; the point is that a RUN RECORD, which has a
    different audience, discloses strictly less.)

    ``safe_reason`` is the opt-in publication seam ``run_workflow`` reads
    (``execution_failed`` / ``error_reason``), so the run publishes this
    sentence instead of the derived floor's "the failing step published no
    explanation. Recorded failure class: ...".

    ⚠ AND ON THE WORKFLOWS-API PROPOSAL SURFACE, WHERE IT IS REACHABLE BUT WILL
    NOT FIRE. ``ne.api.workflows``'s write legs DO run as canonical Run parks —
    ``ReceiptGatedToolExecutor`` is constructed as ``RunExecutionContext``'s
    ``executor`` field and forwarded to the execution seam, so this guard is
    entered on them. It will not raise there under the current scope: those legs
    (``write.internal.crm.note`` under ``write_approval_floor_scope(TIER_2)``)
    reach the guard with NO policy window — the ToolSpec declares tier 1, the
    PDP answers ``allow`` with a null window, and the tier-2 floor is applied
    afterwards by ``_effective_tier``, which the PDP never sees — so the window
    is the ``cfg.expiry_s`` fallback and the guard warns instead. If a future
    change did make one of those legs carry a DECLARED window, the refusal would
    reach that surface as the exception CLASS only: its ``_park`` task
    deliberately carries ``type(exc).__name__`` and never the message (C12), so
    a proposal refused here would read ``ApprovalWindowUnhonourableError`` and
    not name the window.
    """

    #: ``published_failure_reason`` drops a longer reason to the derived floor,
    #: so the sentence is bounded HERE rather than silently discarded there.
    SAFE_REASON_MAX = 256

    #: The action is a registered tool NAME (a code identifier, never a value).
    #: Bounded so a pathological name cannot push the ACTIONABLE tail of the
    #: sentence ("Configure this action's approval window to fit.") past
    #: ``SAFE_REASON_MAX`` and out of the published record.
    #:
    #: SIZED TO THE LONGEST ACTION NAME THIS REPO STATICALLY REGISTERS, which is
    #: 43: ``write.internal.identity.credential_rotation``
    #: (``ne.worker.credential_rotation``) and
    #: ``read.kg.overdue_provisional_credit_disputes``
    #: (``ne.api.workflows``, and a real ``tool_grants`` entry on
    #: ``agt_01k3overduecreditreview00000``). It was 32, then 38 — each time
    #: "sized to the longest action this repo actually registers" against a
    #: SAMPLE rather than the registry, because both scans looked for
    #: ``ToolSpec(name="...")`` and both of the 43s are module constants. So the
    #: number is no longer typed here at all:
    #: ``test_action_max_is_scanned_from_the_registry_not_retyped`` derives it
    #: and this constant must equal what the scan finds.
    #:
    #: 43 IS ALSO THE CEILING, not just the current maximum: the rendered
    #: sentence at 43 characters with an eight-digit window is EXACTLY 256, so
    #: there is no headroom left to spend. A longer action still publishes a
    #: whole sentence — ``__init__`` slices the NAME to 43 first, so the tail
    #: survives; what is lost is the end of the action's own name. RAISING this
    #: constant is what would silently drop the actionable tail, which is why
    #: a longer action arriving here means shortening the sentence instead.
    #:
    #: ⚠ AND IT IS NOT A CLAIM THAT NO ACTION CAN BE LONGER. Connect-sync builds
    #: its request actions at runtime (``f"{SYNC_REQUEST_TOOL_PREFIX}{source}"``,
    #: ``ne.api.connect_sync``), so a long enough source alias exceeds this and
    #: is truncated — which is exactly the case the bound exists for.
    ACTION_MAX = 43

    def __init__(self, *, action: str, window_seconds: float) -> None:
        self.action = action
        self.window_seconds = float(window_seconds)
        named = action[: self.ACTION_MAX]
        seconds = int(self.window_seconds)
        reason = (
            f"The approval could not be parked: policy promises {seconds}s to decide "
            f"{named!r}, but the caller's authorization horizon ends first, so the "
            "approval could never be answered. Configure this action's approval window to fit."
        )
        self.safe_reason = reason[: self.SAFE_REASON_MAX]
        super().__init__(
            f"configured approval window ({seconds}s) for {named!r} outruns the "
            "caller's authorization horizon; the wait is refused instead of parked"
        )


class ToolExecutionError(NeRuntimeError):
    """The tool handler raised while executing (the error was audited, then wrapped)."""


class ActionValidationError(NeRuntimeError):
    """A typed write-action input failed model validation (NE-050, fail closed).

    Raised at TWO call sites, both deriving from the SAME validation helpers
    (``ne.runtime.action_tools._validate_input`` / ``_dedup_key_for`` — never two
    divergent implementations):

    * The executor's PRE-gate validation step (NE-ACTION-VALIDATE-BEFORE-GATE,
      :meth:`~ne.runtime.middleware.GovernedToolExecutor.execute`, step 2b) — a
      binding-owned ``validate_args`` preflight run AFTER guardrails + PDP but
      BEFORE the human approval gate. The refusal is audited
      (``tool.validate[denied]``) and raised BEFORE any approval row exists — an
      invalid call NEVER consumes a human decision.
    * The governed action handler's own re-validation (defense in depth, AFTER
      approval has already cleared) — audited ``tool.execute[error]`` and wrapped as
      :class:`ToolExecutionError`, so a malformed write still never reaches the
      source system even if it somehow reached the handler.

    In both cases the caller-supplied args did not satisfy the action's frozen,
    extra-forbid :class:`~ne.runtime.action_tools.ActionInput` model (bad record id,
    missing ``request_id`` outside any idempotency scope, an undeclared key, an
    over-length field). The message names only the offending field NAMES / error
    classes — never the rejected VALUES (C12 redaction).
    """


class ActionIdempotencyError(NeRuntimeError):
    """The same ``request_id`` was reused with a DIFFERENT payload (NE-050, fail closed).

    The framework keys dedup on ``sha256(action:request_id)``. A retry that reuses a
    request_id but changes the payload is a caller integrity bug (an idempotency key
    must identify exactly one intended write), not a legitimate retry — it is refused
    rather than performing a second, divergent source-system write. The message names
    only the action and the reuse condition, never either payload's VALUES.
    """


class RuntimeAuditError(NeRuntimeError):
    """The middleware's own audit append failed, so the result is WITHHELD (fail closed).

    Mirrors ``ne.govern.errors.AuditEmissionError``: a tool call that cannot be
    recorded never returns its result. Raised (chained) when
    :meth:`AuditSink.append` fails for a middleware-emitted event.
    """


class ApprovalPolicyError(NeRuntimeError):
    """A named-approver policy was violated at the approval gate (fail closed).

    SPEC §4.2 ('no delegation below the named approver'): raised when an approver
    id is empty, when a Tier-3 approver is not in the configured
    ``tier3_named_approvers`` list, when the second Tier-3 approver equals the
    first, or when a Tier-3 approval is attempted with an EMPTY configured list
    (an empty list means Tier-3 can never be approved — fail closed).
    """


class ApprovalSourceWithheldFromApproverError(NeRuntimeError):
    """CURRENT source authority does not admit THIS principal to the parked invocation.

    A fact about the reading/deciding principal's own ACL standing — NEVER a
    revocation of the run owner's authority and NEVER terminal (U2 adversarial
    review, F1). The approval request stays PENDING for another authorized
    approver, nothing is persisted, and no ``source_withheld`` record may be
    written from this refusal: only the resume-side re-evaluation under the RUN
    OWNER's bound context is a genuine authority decision. The message carries
    no material values and is safe to serve verbatim.
    """


class RunApprovalAuthorityRetryableError(NeRuntimeError):
    """The authority floor could not be ESTABLISHED, so it answered neither way.

    The third outcome of ``RunApprovalCheckpointStore.check_decision_authority``,
    beside authorized (returns) and
    :class:`ApprovalSourceWithheldFromApproverError` (permanently withheld from
    THIS principal). ``.authorize`` raises it on the same arm but has a FOURTH
    answer those three do not cover: an ``operation="resume"`` permanent
    withholding runs under the RUN OWNER's bound context, so it is a genuine
    authority decision about the run, and that path persists it through
    ``reconcile_source_authority`` and returns ``None`` rather than raising.
    Production reaches the retryable arm on ordinary transient conditions — a
    missing head receipt, a missing artifact identity, an ACL snapshot that has
    not landed (``current_source_filter.py``) — and on a stored invocation that
    no longer parses to the argument MAPPING a resume would need. Nothing is
    persisted, nothing is terminal, the request stays PENDING and the caller may
    simply try again.

    TYPED RATHER THAN A BARE ``RuntimeError``, and that is the whole point of
    the class. Every ne-runtime error is a ``RuntimeError`` by construction (see
    :class:`NeRuntimeError`), so a caller that wrote ``except RuntimeError`` to
    mean "the retryable arm" was in fact catching the ENTIRE hierarchy. On this
    chain that demonstrably includes the fail-closed
    ``ProtectedExecutionError`` family: ``resolve_stored_for_approval``'s first
    statement is ``self.authorization()``, which raises
    ``AuthorizationExpiredError`` past the bound snapshot's expiry and
    ``MissingAuthenticatedContextError`` with no snapshot bound at all — both of
    which MUST propagate, the first because the specification requires it to
    terminalize the attempt. It would equally have caught
    :class:`RuntimeAuditError`, which this module's docstring forbids
    swallowing anywhere, on the day anything on the chain raises one. Catching
    this class catches the arm and nothing else. It still subclasses
    ``RuntimeError`` through :class:`NeRuntimeError`, so the API decide routes
    that map the arm to 503 keep working unchanged.
    """


class ApprovalRequestNotFoundError(NeRuntimeError):
    """No persisted approval request exists under the given request id."""


class ApprovalAlreadyDecidedError(NeRuntimeError):
    """The approval request is already terminal (approved/rejected/expired).

    A terminal request can never transition again; in particular an EXPIRED
    request can never be granted — a fresh re-request is required (SPEC §4.2).
    """


class SessionDepartmentMismatchError(NeRuntimeError):
    """A principal's declared department did not match the session's department.

    Sessions are scoped per department; a cross-department principal is rejected
    before any model or tool activity (fail closed, no state bleed).
    """


class DurabilityUnavailableError(NeRuntimeError):
    """A durable-runtime operation was requested while durability is not enabled."""


class SubAgentDispatchError(NeRuntimeError):
    """Base class for a failed typed sub-agent dispatch (NE-034, fail closed)."""


class UnknownAgentTypeError(SubAgentDispatchError):
    """A :class:`SubAgentSpec` named an agent with no registered definition.

    The dispatch was audited (``subagent.dispatch`` denied) and refused before any
    model or tool activity — an unknown agent type NEVER runs (fail closed).
    """


class DispatchCapExceededError(SubAgentDispatchError):
    """A dispatch was refused because a server-side cap would be exceeded.

    Depth and concurrency caps are enforced SERVER-SIDE: a spec's declared
    ``max_depth`` / ``max_concurrency`` can only NARROW the dispatcher-carried
    budget, never widen it, and global concurrency is counted atomically in the
    :class:`~ne.runtime.processes.ProcessRegistry`. The rejection was audited
    (``subagent.cap_rejected``) before this terminal error was raised.

    ``reason`` is which cap tripped; ``scope`` is whether it was the server budget
    (``'server'``) or a spec-narrowed local bound (``'spec'``).
    """

    def __init__(
        self,
        message: str = "",
        *,
        reason: Literal["depth", "concurrency"],
        scope: Literal["server", "spec"],
    ) -> None:
        self.reason: Literal["depth", "concurrency"] = reason
        self.scope: Literal["server", "spec"] = scope
        super().__init__(message or f"dispatch cap exceeded: {reason} ({scope})")


class SubAgentRoutePolicyDeniedError(SubAgentDispatchError):
    """The PDP did not ALLOW a child's resolved route (NE-169 route AUTHORITY).

    Raised at the routing decision inside :class:`~ne.runtime.dispatch.
    GovernedSubAgentDispatcher` (design §10.2) — BEFORE the child ever runs,
    after the denial has already been audited on the ``subagent.route`` event.
    Any policy effect OTHER than ``DecisionEffect.ALLOW`` (a ``deny``, or a
    ``require-approval`` — routing has no human-in-the-loop leg) withholds the
    child: fail closed.
    """


class ToolsetViolationError(NeRuntimeError):
    """A gated agent tried to call a tool OUTSIDE its granted toolset (fail closed).

    Toolset gating is ENFORCED, not advisory: an out-of-set call is audited
    (``tool.toolset_denied``) and the PDP is consulted for the record BEFORE this
    terminal error is raised — the tool handler is NEVER reached. Mirrors the
    middleware's unknown-tool branch: an out-of-set tool never executes regardless
    of any policy effect.
    """


class ToolsetConfigurationError(NeRuntimeError):
    """A toolset grant references a tool that is not registered (fail loud).

    Raised at construction/registration time when a
    :class:`~ne.runtime.toolset.ToolsetScopedExecutor` (or a scheduled job) grants
    a tool name absent from the inner executor's specs — a job or sub-agent must
    never silently lose a tool it was configured to use.
    """


class SchedulingError(NeRuntimeError):
    """A scheduler operation was rejected (fail loud, nothing persisted).

    Raised for an invalid cron expression, an unknown toolset name, an unknown
    agent type, a duplicate job name, or an unknown ``job_id`` — the row and its
    outbox event are written atomically or not at all (no partial state).
    """


class AgentMemoryError(NeRuntimeError):
    """Base for episodic-memory failures (NE-037 agent memory v1)."""


class MemoryEventNotFoundError(AgentMemoryError):
    """No such event visible to this agent.

    Also raised on cross-agent access so an event's existence never leaks to a
    non-owner (per-agent memory isolation).
    """


class MemoryPromotionError(AgentMemoryError):
    """A promotion precondition failed.

    Raised for a non-learned-fact kind, a missing/duplicate proposal, or a
    curation-gate violation (self-approval or an unnamed approver).
    """


class MemoryPromotionAuditError(MemoryPromotionError):
    """The ``memory.promotion.promoted`` audit append failed — fail closed.

    The already-upserted chunks were unwound through the governed
    ``KnowledgeWrite.quarantine`` seam (parked with full payload, deleted from the
    store) and NO promotion-approved row was written, so the promotion is cleanly
    retryable and no retrievable-but-unaudited fact remains (Checkpoint-D
    boundary-audit #6).
    """


class AgenticNoProgressError(NeRuntimeError):
    """The model-planned loop stopped because its reads stopped finding anything.

    NE-LOOP-NOPROGRESS. A cap is a ceiling on how much work a run may do; this is
    not one. It is the observation that the run is no longer doing work at all:
    the same Tool has been called repeatedly and each call served the run
    something it had ALREADY BEEN SERVED. Continuing costs one full model
    round-trip per call and cannot produce a grounded answer, because there is
    nothing new to ground one on.

    🔴 THE SENTENCE THIS PUBLISHES USED TO BE FALSE IN A REACHABLE CASE, and the
    wording here is the correction. "No new evidence" meant "no new `urn:` ref",
    so three successful CRM lookups resolving three DIFFERENT customers were
    reported to the reader as "Nothing was retrieved to ground an answer". The
    predicate now counts REPEATS -- a new ref or a result this run has not been
    served before, either one, clears the streak -- and this sentence says that
    instead of overstating it. See ``_NO_PROGRESS_TOOL_CALLS`` in
    ``ne.runtime.runner`` for the measurement and its declared residual.

    🔴 AND THE SCOPE OF THE SENTENCE IS ONE TOOL, because the scope of the
    OBSERVATION is one tool. ``_RecordedExecutor`` keys ``seen_outputs`` by tool
    name and resets the streak whenever the tool changes, so this raises after
    three repeats from a SINGLE Tool -- possibly without another granted read
    having been tried at all. The published sentence used to end "No further
    evidence was reachable", which asserts a fact about the whole tool set that
    this predicate never established and cannot. It now names only what was
    counted: that Tool, those calls, nothing new in them.

    Raised INSIDE the recording executor, immediately after the barren call is
    recorded, so the run stops on the call that proved it rather than after the
    model has been asked to plan one more.

    ``safe_reason`` is the opt-in publication seam ``run_workflow`` reads (see
    ``ne.runtime.run_workflow`` — ``execution_failed`` / ``error_reason``): it is
    written HERE, at the raise site, from a Tool NAME and a COUNT and nothing
    else. It never carries a query, a principal, a Tool argument or a row. The
    console renders it verbatim in place of its own "the run ended before it
    produced an answer", which is the whole point: a reader must be able to tell
    "no evidence exists" from "the system broke", and only the server knows which
    of the two happened.
    """

    #: The projection that publishes ``safe_reason`` drops anything longer than
    #: this, so the sentence is clamped HERE rather than silently discarded there.
    SAFE_REASON_MAX = 256

    def __init__(self, *, tool: str, calls: int) -> None:
        self.tool = tool
        self.calls = calls
        reason = (
            f"Stopped after {calls} consecutive {tool} calls returned nothing this run "
            "had not already been served, so the run was ended without an answer."
        )
        self.safe_reason = reason[: self.SAFE_REASON_MAX]
        super().__init__(
            f"agentic run made no progress: {calls} consecutive {tool} calls "
            "served nothing the run had not already been served"
        )


class RunActivationError(NeRuntimeError):
    """A Run could not be STARTED, and names the precondition that refused it.

    Everything this covers happens BEFORE the model is called: the catalog
    re-check, the composed-binding re-check, and the Skill-surface activation.
    Every one of those was a bare ``RuntimeError`` whose message never left the
    worker, so the run published ``Recorded failure class: RuntimeError`` — the
    derived floor, which names an exception class and explains nothing. A run
    that dies in 14 seconds without touching a source is the case where the
    reader has the LEAST context of their own, so it is the case that most needs
    the server to say what happened.

    ``safe_reason`` is the opt-in publication seam ``run_workflow`` reads. Every
    detail below is a static, server-authored literal built from configuration
    identities and COUNTS — never a principal, never an authorization label,
    never a Skill body, never customer text — so publishing it verbatim keeps
    C12 exactly as the rest of this module holds it.
    """

    #: ``run_projection`` drops a longer reason, so it is clamped at the raise
    #: site rather than silently discarded there.
    SAFE_REASON_MAX = 256

    def __init__(self, detail: str) -> None:
        self.detail = detail
        reason = f"The run could not start: {detail}."
        self.safe_reason = reason[: self.SAFE_REASON_MAX]
        super().__init__(detail)


class RunSkillActivationError(RunActivationError):
    """The governed Skill surface did not serve every Skill the Agent pins.

    An Agent freezes its Skills as EXACT pins (id + version + content hash) and
    the surface must resolve all of them, in order — an Agent assembled with a
    Skill missing is not the Agent that was admitted, so the run refuses rather
    than proceeding with a quietly smaller surface. That exactness is the
    product property and it is NOT relaxed here; what changes is only that the
    refusal now says what it refused.

    The reason carries the two COUNTS and nothing else. It deliberately does not
    name which pin failed or why: ``ne.skills.active_set.resolve_pins`` drops a
    pin that is retired, absent, hash-mismatched, ACL-invisible, missing a
    granted tool or platform-incompatible through ONE silent path precisely so
    that this refusal cannot be used as an existence oracle for the Skill
    library. The counts are over the Agent's OWN declared manifest, which the
    caller may already read, so they disclose nothing new — and they are enough
    to send a reader straight to Skill activation instead of to a bare class
    name.
    """

    def __init__(self, *, pinned: int, activated: int) -> None:
        self.pinned = pinned
        self.activated = activated
        super().__init__(
            f"its Agent pins {pinned} Skill(s) and the governed Skill surface activated "
            f"{activated}, so the Agent could not be assembled exactly as admitted"
        )


class AgentOutputContractError(NeRuntimeError):
    """The model's final reply never satisfied the run's answer contract.

    Raised by :func:`~ne.runtime.runner.run_model_planned_agent` when pydantic-ai
    exhausts its bounded output-retry budget (``UnexpectedModelBehavior:
    Exceeded maximum output retries (N)``). The run FAILS — a reply that cannot
    be validated is never repaired into an answer, and no partial draft is
    returned.

    The point of the class is ``safe_reason``: the raw pydantic-ai message names
    only the retry count, so a run that died this way used to publish
    ``failure_reason: null`` and the reader saw a bare status word. This carries
    a SERVER-AUTHORED reason built from the chained
    :class:`pydantic.ValidationError`'s ``loc`` (our own field path) and ``type``
    (Pydantic's error-code vocabulary) — both server-owned identifiers. The
    rejected VALUE, the validator's rendered ``msg``, and the retry prompt's
    text are all deliberately EXCLUDED: they quote the model's own output, which
    may carry customer text (C12). ``run_workflow`` publishes ``safe_reason`` as
    the ``execution_failed`` checkpoint's ``error_reason``, which
    ``run_projection._failure_reason`` serves and ``TurnFailureReadout`` renders
    verbatim.

    Also raised by ``ne.worker.run_execution`` as the claim-contract BACKSTOP,
    for the same reason: source-bearing output that declared no claims is never
    served, and that refusal has to say why rather than publish a bare status
    word.
    """

    def __init__(self, safe_reason: str) -> None:
        self.safe_reason = safe_reason
        super().__init__(safe_reason)
