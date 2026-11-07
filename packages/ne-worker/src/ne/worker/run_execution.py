# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-010D strategy consumer for PRE-010B's receipt-gated Run boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol

from pydantic import BaseModel

from ne.api.agent_skills import run_skill_pins
from ne.api.knowledge_scope import declared_source_ids, run_knowledge_scope
from ne.api.model_selection import (
    ModelSelectionEvidenceError,
    ModelSelectionPolicyRefused,
    ModelSelectionService,
    ModelSelectionUnavailable,
    UnknownModelSelection,
)
from ne.api.tools import TURN_PRINCIPAL
from ne.api.workflows import run_workflow
from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.models import Completion, CompletionRequest, ModelGateway, TurnModelBudget
from ne.contracts.types import Lane, Principal
from ne.events import new_audit_event
from ne.gateway.errors import ModelCallFailedError
from ne.govern.grounding_policy import StrictnessTier
from ne.runtime.agent import AgentExecutionBinding
from ne.runtime.correlation import (
    action_scope,
    current_run_trace_id,
    run_requested_model_scope,
    run_trace_scope,
)
from ne.runtime.errors import (
    AgentOutputContractError,
    RunActivationError,
    RunSkillActivationError,
    RuntimeAuditError,
)
from ne.runtime.grounding import GroundingPolicy, GroundingRecorder, run_grounding_gate
from ne.runtime.middleware import scoped_tool_executor
from ne.runtime.run_claim_verifier import (
    ClaimSlice,
    DirectSearchEvidenceObservation,
    InferredGraphPathEvidenceObservation,
    VerifiedRowSetEvidenceObservation,
    VerifiedRowSetMemberObservation,
    verify_claim,
)
from ne.runtime.run_finalizer import (
    RunClaimEnvelopeDeclaration,
    RunEvidenceBinding,
    RunVerifierBinding,
    StructuredRunOutputEnvelope,
    claim_set_hash,
    served_evidence_binding_hash,
)
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_store import RunClaimEvidenceSetObservation
from ne.runtime.runner import (
    BoundedAgenticAdapter,
    model_request_limit,
    run_deterministic_profile,
)
from ne.runtime.source_results import (
    PROTECTED_REFERENCE_PREFIX,
    complete_run_direct_publication_observation,
    current_authenticated_context,
    direct_publication_scope,
)
from ne.runtime.workflow_execution_profile import WorkflowExecutionProfile
from ne.schema import AuditOutcome


class PrincipalResolver(Protocol):
    async def resolve(self, subject: str) -> Principal: ...


DeterministicExecutor = Callable[
    [WorkflowExecutionProfile, BaseModel, Principal, RunExecutionContext],
    Awaitable[object],
]


def _budget_exhausted_reason(cap: object, adapter: BoundedAgenticAdapter | None) -> str | None:
    """Name WHICH ceiling ended a ``budget_exhausted`` run, or return ``None``.

    ``budget_exhausted`` is one terminal shared by five different ceilings, so a
    reader given only the status word cannot tell a run that spent its tokens from
    one that ran out of clock -- and cannot tell which number to change.  The
    runner already types the distinction as ``ModelPlannedRun.exhausted_cap``; this
    turns that token into the operator sentence the failure record carries, quoting
    the ceiling's CONFIGURED VALUE from the same adapter the loop was bounded by,
    so the sentence cannot drift from what was actually enforced.

    It never manufactures one.  An unrecognised cap, an absent cap (an adapter that
    could not say) and an absent adapter all return ``None``, and the reader is then
    told the status word alone -- the same answer a run that published no reason has
    always given.  The request ceiling CALLS ``model_request_limit`` rather than
    restating its arithmetic, so the sentence cannot disagree with the cap the
    loop enforced -- it was restated here once, and the reserved output retry
    made the restatement wrong by one.
    """
    if adapter is None or not isinstance(cap, str):
        return None
    sentences = {
        "tokens": f"stopped: the turn's {adapter.total_tokens_limit:,}-token budget was exhausted",
        "tool-calls": (
            f"stopped: the turn's {adapter.max_tool_calls:,}-tool-call budget was exhausted"
        ),
        "requests": (
            f"stopped: the turn's {model_request_limit(adapter.max_tool_calls):,}"
            f"-model-request budget was exhausted"
        ),
        "wall-clock": (
            f"stopped: the turn's {adapter.wall_clock_seconds:.0f}-second wall-clock budget "
            "was exhausted"
        ),
        "parked-writes": (
            f"stopped: the turn's {adapter.max_parked_writes:,}-parked-draft budget was exhausted"
        ),
    }
    return sentences.get(cap)


class _RunContinuityGateway:
    """Recheck exact policy/visibility before each candidate effect and record truth."""

    def __init__(
        self,
        inner: ModelGateway,
        *,
        context: RunExecutionContext,
        principal: Principal,
        catalog: Any,
        selection: ModelSelectionService,
        run_store: Any,
    ) -> None:
        self._inner = inner
        self._context = context
        self._principal = principal
        self._catalog = catalog
        self._selection = selection
        self._run_store = run_store
        self._ordinal = 0
        #: NE item 12: the policy-recheck boundary is measured once per run.
        self._policy_recheck_measured = False

    def _definition(self) -> Any:
        run = self._context.run
        row = self._catalog.get_visible(run.agent_id, self._principal)
        if row is None or not row.present or not row.available:
            raise RuntimeError("Run Agent is no longer visible and available")
        definition = row.definition
        if (
            definition.agent_id != run.agent_id
            or definition.version != run.agent_version
            or definition.definition_hash != run.agent_definition_hash
            or definition.model_policy_id != run.model_policy_id
        ):
            raise RuntimeError("Run Agent/version/model policy drifted before model effect")
        if (
            run.model_authorization_receipt_hash is None
            or run.authorized_model is None
            or run.authorized_model_lane is None
        ):
            raise RuntimeError("Run has no complete frozen model authorization")
        return definition

    async def complete(self, request: CompletionRequest) -> Completion:
        # POST_DEMO item 12, increment 1c. ⚠️ WITHOUT THIS, `profile:agent-built`
        # LIES ON A CONTINUITY RUN, and Codex caught it: the edge
        # `agent-built → run-model:before:0` was described as "everything
        # pydantic-ai does before its first model request", but on a
        # continuity-active run this gateway sits in between and does POLICY work
        # first -- `choices()` on the automatic path, then
        # `verify_model_boundary()` and `authorize()` on both.
        #
        # ⛔ AND I QUANTIFIED IT WITH A NUMBER FROM A DISABLED CONFIGURATION.
        # This comment used to cite the NE-CONTINUITY-PROBE-REUSE note below as
        # "what that costs in its own words -- seconds of the inter-turn gap".
        # Read that note: the seconds it measures are the cost of the FORCED
        # probe pair, which is exactly what `force_refresh_private=False`
        # DISABLES. The current path uses the bounded serving cache, and its cost
        # is UNMEASURED -- which is the reason to mark it, not a number to quote.
        # An adversarial pass caught this, and it is the same class as everything
        # else this increment has had to retract: claiming more than happened.
        #
        # ⛔ AND THE ONCE-PER-RUN GATE LEAVES THE INTER-TURN CASE UNMEASURED, on
        # purpose, and the record has to say so. The only evidence anyone has for
        # this recheck being expensive locates it BETWEEN model calls -- hops
        # 2..N. The gate emits on the first `complete()` only, so those hops'
        # rechecks stay absorbed into `run-model:before:{n}` exactly as before.
        # That is defensible for item 12, whose question is the OPENING gap, and
        # it is not defensible to leave unstated: a later triage of the
        # inter-turn gap will find one marker per run and no data for the
        # interval it cares about.
        #
        # (A paragraph quoting "seconds of the inter-turn gap" as this marker's
        # justification used to sit here. I labelled it "the stale wording said"
        # rather than deleting it, which left the claim standing in the present
        # tense four paragraphs below its own retraction, inside this same block.
        # Ninth instance of that class in this PR. DELETED, not relabelled --
        # a retraction that leaves the sentence readable is not a retraction.)
        #
        # ONE marker divides it, because `run-model:before:0` is emitted by the
        # INNER gateway this wraps. WHAT IS ON EACH SIDE, stated correctly here
        # only after an adversarial pass found THIS copy still claiming the left
        # side is "pydantic-ai's startup". That is the TENTH instance in this PR
        # of a corrected claim left standing, and it is the worst-placed of the
        # ten: the wrong wording sat at the EMISSION SITE of the marker that
        # defines the interval, contradicting the corrected wording the same
        # commit had already written into `runner.py`.
        #
        # LEFT (`agent-built` to `policy-recheck-before`): pydantic-ai's graph
        # startup AND this platform's own protected-history authorization.
        # ⚠️ TWO earlier versions of this line were wrong in OPPOSITE directions.
        # The first said flatly "NOT pydantic-ai", which deleted the graph
        # startup `runner.py` correctly puts here. The second said the history
        # authorization DOMINATES the graph startup — and that is an unmeasured
        # ranking asserted inside the paragraph whose entire subject is that this
        # region is unmeasured. Nothing here times either one. What IS
        # established is that BOTH are in the region and that only the history
        # authorization does I/O; which costs more is exactly the question these
        # markers exist to make answerable, and it is not answered yet.
        # Before this gateway's policy work is reached at all,
        # `GatewayModel.request` runs THREE things in order:
        # `source_guard.materialize_history` -- a protected-store read and a PDP
        # decision PER REFERENCE -- then the context-budget trim, then
        # `source_guard.reauthorize_history`, the pass that CLAIMS: durable
        # delivery receipts and audit rows. ⚠️ ALL THREE DO I/O -- an adversarial
        # pass was right that an earlier version named only the first and third.
        # `_trim` awaits `_emit_budget` whenever it drops a turn or refuses
        # (`model.py:444`, `:452`, `:469`), and that is an audit-sink `append` which
        # FAILS CLOSED (`model.py:527`, `RuntimeAuditError`). So on a long
        # conversation -- the only case where this gap is worth triaging at all --
        # the region contains an audit append per model request whose FREQUENCY does
        # track history size. The "not with conversation length" claim below is true
        # of the protected-history passes and NOT of the trim; both are stated because
        # attributing this region to protected-reference count alone under-counts it.
        # An earlier version of this passage ended "that is the only I/O in the
        # region" with `reauthorize_history` as its nearest antecedent, which
        # reads as excluding `materialize_history`'s per-reference PDP decisions
        # from the very timeline this marker delimits. TOGETHER they are the only
        # I/O in the region, and TOGETHER they scale with the number of PROTECTED
        # REFERENCES in the history -- NOT with conversation length, and NOT with
        # `continuity_active`, which is only `model_policy_id is not None`.
        #
        # ⚠️ And the middle one, the trim, is NOT continuity-gated. An earlier
        # version said it is "armed on exactly this path because `context_budget`
        # is set only when `continuity_active`". FALSE, and it would make a triage
        # engineer exclude the trim when reading a non-continuity timeline:
        # `build_platform` sets `runtime_settings.context_budget` for any ROUTED
        # runtime task (`ne-api/platform.py:2703-2721`), the `model_copy` below
        # that swaps `task` PRESERVES it, and continuity only OVERRIDES it. On the
        # demo box the runtime task is routed, so the trim is armed on
        # non-continuity runs too. What continuity changes is WHICH budget, not
        # whether there is one.
        # Source-free prior turns are rendered as plaintext in the worker,
        # so a first turn can have `continuity_active` true and ZERO I/O
        # here. See the fuller note at `profile:agent-built` in `runner.py`.
        #
        # RIGHT (`policy-recheck-before` to `run-model:before:0`): the
        # model-policy recheck -- `_definition()` and its catalog visibility
        # check, `self._selection.choices(...)` on the automatic path (an audit
        # read plus the 5 s-TTL serving probe the NE-CONTINUITY-PROBE-REUSE note
        # below pairs with `authorize` when attributing seconds),
        # `verify_model_boundary`, and `authorize`. ⚠️ `choices(...)` was missing
        # from this list one round ago, named fifty lines above it and dropped
        # from the enumeration that DEFINES the interval -- which is the only
        # place a reader looks.
        #
        # ⚠️ THE VERY FIRST STATEMENT of this method, and that placement is a
        # correction. It was below `self._definition()` and the task/principal
        # checks — but `_definition()` performs the catalog visibility and
        # frozen-policy recheck this marker exists to delimit, so policy work sat
        # on the WRONG side of it and a failure in there emitted no marker at all,
        # handing that whole interval to "pydantic-ai startup". Codex caught it.
        # Above the `requested_model` branch too, so one line covers both return
        # paths rather than one per path.
        #
        # ⚠️ ONCE PER RUN, NOT ONCE PER MODEL REQUEST. `complete()` is called for
        # EVERY pydantic-ai request including its output-contract retries, so an
        # ungated heartbeat would write an uncounted extra span PER HOP into a
        # table with no reaper and a random-UUID primary key whose insert cost
        # only grows. It costs nothing to gate, because the question is about the
        # OPENING gap: only the first request's recheck sits inside the edge
        # being split.
        #
        # ⛔ AND THE SECOND HALF OF MY JUSTIFICATION FOR THIS GATE WAS AN
        # OVERCLAIM, retracted here. I wrote that an ungated marker would add
        # "a blocking FOR UPDATE transaction on the shared DBOS loop" per hop and
        # cited `_CronTickLoopRunner`'s 182-653s live incident as the shape of the
        # risk. An adversarial pass checked what is already there:
        # `_LeaseExecutionBoundary` ALREADY runs that exact transaction
        # unconditionally on a ~1 s poll for the run's whole duration
        # (`run_workflow.py` `_poll_seconds`, and the entire adapter execution
        # sits inside one `await_inflight`). On a 20-hop, 120 s run that is 120+
        # blocking renewals already; 20 more would have been a ~17% increment on
        # pre-existing load, not a new failure class and nothing like a
        # minutes-long blocking body. **The row-cost half is real and is why the
        # gate stays. The loop-blocking half was not, and this comment no longer
        # claims it.**
        if not self._policy_recheck_measured:
            self._policy_recheck_measured = True
            await self._context.boundary.heartbeat("profile:policy-recheck-before")
        definition = self._definition()
        run = self._context.run
        if request.task != definition.model_policy_id:
            raise RuntimeError("Run gateway task differs from its frozen Agent policy")
        if request.principal_id != self._principal.id:
            raise PermissionError("Run gateway principal differs from its accepted owner")
        candidates = await self._turn_candidates(definition.model_policy_id)
        if run.requested_model is not None:
            model, lane = candidates[0]
            return await self._explicit(request, model, lane)

        last: Exception | None = None
        # WHAT WAS REACHED FOR AND DID NOT ANSWER, in attempt order. This loop
        # IS the fallback chain for a canonical Run -- the per-candidate
        # `_explicit` below refuses a gateway that serves a different model than
        # it was pinned to, so this is the only place a Run can change model
        # mid-request, and therefore the only place that can name the fact.
        #
        # BOTH exits append. A policy refusal and a provider failure are
        # different causes but the same routing fact: that candidate was reached
        # for and did not serve, and the one that eventually did was not first.
        # (The CAUSE stays out of the record on purpose -- see
        # `_require_route_position`. This carries names and positions only.)
        passed_over: list[str] = []
        for name, lane in candidates:
            try:
                return await self._explicit(
                    request,
                    name,
                    lane,
                    request_mode="auto",
                    requested_model=None,
                    passed_over=tuple(passed_over),
                )
            except ModelSelectionPolicyRefused:
                passed_over.append(name)
                continue
            except ModelCallFailedError as exc:
                last = exc
                passed_over.append(name)
                continue
            except ContextBudgetExceededError as exc:
                # A candidate whose window cannot hold this turn is passed over,
                # not the end of the roll. ``_explicit`` pins ``requested_model``
                # to the candidate, and the gateway pre-flight BREAKS instead of
                # rolling on an explicitly-named model (gateway.py, "an explicit
                # per-call choice is a governed primary") — correct there, where
                # a human named the model, and wrong here, where this loop named
                # it and a wider candidate may still be waiting. Recorded in
                # ``passed_over`` exactly like a provider failure: same routing
                # fact, different cause. If NOTHING serves, the last budget
                # refusal is re-raised below with its counts intact.
                last = exc
                passed_over.append(name)
                continue
        if last is not None:
            raise last
        raise ModelSelectionUnavailable("automatic model policy has no authorized candidate")

    async def _explicit(
        self,
        request: CompletionRequest,
        model: str,
        lane: Lane,
        *,
        request_mode: str = "explicit",
        requested_model: str | None = None,
        passed_over: tuple[str, ...] = (),
    ) -> Completion:
        """Serve THIS exact candidate, and record where in the chain it sat.

        ``passed_over`` names the candidates ``_auto`` already reached for and
        did not get an answer from, in attempt order. Empty is the honest
        default and the honest value for the explicitly-pinned path: an explicit
        request attempts one model, and if that model does not serve, the Run
        fails rather than quietly moving -- so nothing was passed over.
        """
        run = self._context.run
        definition = self._definition()
        if request.task != definition.model_policy_id:
            raise RuntimeError("Run gateway task differs from its frozen Agent policy")
        if request.principal_id != self._principal.id:
            raise PermissionError("Run gateway principal differs from its accepted owner")
        if run.context_snapshot_hash is None:
            raise RuntimeError("Run has no frozen context snapshot")
        await self._run_store.verify_model_boundary(
            run_id=run.run_id,
            owner_principal_id=self._principal.id,
            agent_definition_hash=str(run.agent_definition_hash),
            model_policy_id=str(run.model_policy_id),
            authorization_receipt_hash=str(run.model_authorization_receipt_hash),
            context_snapshot_hash=run.context_snapshot_hash,
        )
        authorized = await self._selection.authorize(
            task=str(run.model_policy_id),
            requested_model=model,
            principal=self._principal,
            trace_id=run.run_id,
            # NE-CONTINUITY-PROBE-REUSE: this recheck runs between the model
            # calls of one admitted run. Reuse the selection service's own
            # bounded serving cache instead of forcing a fresh vLLM probe per
            # call -- measured live, the forced probe pair (choices + authorize)
            # against a lane busy generating was seconds of the inter-turn gap.
            # A stale cache still probes for real, and the model call that
            # follows immediately fails loudly into the recorded fallback
            # chain if the lane died since. PDP/air-gap/audit are unchanged.
            force_refresh_private=False,
        )
        if authorized.model != model or authorized.lane is not lane:
            raise RuntimeError("model authorization returned a different candidate")
        if request_mode == "explicit" and (
            run.authorized_model != model or run.authorized_model_lane != lane.value
        ):
            raise RuntimeError("explicit Run model differs from its admission authorization")
        candidate = request.model_copy(update={"requested_model": model, "lane": lane})
        result = await self._inner.complete(candidate)
        if result.model_id != model:
            raise RuntimeError("explicit gateway candidate served a different model")
        ordinal = self._ordinal
        self._ordinal += 1
        await self._run_store.record_model_step(
            run_id=run.run_id,
            attempt_id=self._context.attempt_id,
            ordinal=ordinal,
            model_policy_id=str(run.model_policy_id),
            request_mode=request_mode,
            requested_model=(
                run.requested_model if request_mode == "explicit" else requested_model
            ),
            served_model=result.model_id,
            served_lane=lane.value,
            # An INVOLUNTARY provider fallback, stated positively in both
            # directions: "primary" is a claim that nothing was attempted first,
            # not merely the absence of a fallback record. The console needs the
            # positive form to say "served as requested" from real data instead
            # of reading a null as reassurance.
            route_position="fallback" if passed_over else "primary",
            fallback_from=",".join(passed_over) if passed_over else None,
            authorization_receipt_hash=str(run.model_authorization_receipt_hash),
        )
        return result

    async def _turn_candidates(self, task: str) -> tuple[tuple[str, Lane], ...]:
        """THE attempt order for this Run's next model effect — one function.

        :meth:`complete` attempts exactly this sequence (its explicit branch
        takes the single head; ``_auto``'s loop walks all of it) and
        :meth:`context_budget_for_turn` budgets its head — so the budget the
        trim guard uses and the candidate first attempted cannot diverge by
        construction. Rounds 1-3 of NE-CTX-IN-FORCE each found a fresh private
        copy of this ordering drifting from the real one; this is the only
        copy left on the Run path, and both consumers call it.

        * An explicitly-pinned Run yields its ONE admission-authorized model:
          ``_explicit`` attempts exactly that model and fails the Run if it
          does not serve, so there is nothing else a turn could honestly be
          measured against.
        * An automatic Run yields the EVIDENCE-FILTERED catalog —
          ``self._selection.choices(task)``: posture AND lifecycle AND
          serving/frontier evidence — in frozen-primary-first order:
          ``run.authorized_model`` while the catalog still lists it, then the
          configured effective default, then the rest. ⚠️ Round 4 (finding A):
          the resolver used to read ``configured_catalog``, which makes NO
          liveness claim, so an admission-frozen model that serving or
          lifecycle evidence had removed was still budgeted (wide) while
          ``_auto`` attempted only a narrower survivor — nothing was trimmed,
          the survivor's pre-flight refused, and a Run that could have served
          after trimming failed instead. The resolver now awaits the SAME
          ``choices()`` read the attempt loop makes (the capability went async
          for exactly this — ADR-0029 round-4 amendment); the read is bounded
          by the selection service's own serving/frontier caches, the same
          reuse NE-CONTINUITY-PROBE-REUSE relies on between hops.

        Same function, not same snapshot: the resolver's ``choices()`` read
        and ``_auto``'s are moments apart over the service's bounded caches, so
        a mid-turn evidence flip can still make them disagree — the gateway's
        per-candidate pre-flight (and ``_auto``'s pass-over of a budget-refused
        candidate) remains the belt for that transient, exactly as it is for
        the gateway's own route-table snapshot caveat.
        """
        run = self._context.run
        if run.requested_model is not None:
            return ((str(run.requested_model), Lane(str(run.requested_model_lane))),)
        catalog = await self._selection.choices(task)
        if catalog.effective_default is None:
            raise ModelSelectionUnavailable("automatic model policy has no available candidate")
        by_name = {choice.model: choice for choice in catalog.choices}
        frozen_primary = str(run.authorized_model)
        names = tuple(
            dict.fromkeys(
                (
                    *(name for name in (frozen_primary,) if name in by_name),
                    catalog.effective_default,
                    *by_name,
                )
            )
        )
        return tuple((name, by_name[name].lane) for name in names)

    async def context_budget_for_turn(
        self, task: str, *, requested_model: str | None = None, lane: Lane | None = None
    ) -> TurnModelBudget | None:
        """NE-CTX-IN-FORCE on the canonical Run path — and NOT a plain forward.

        ⚠️ FORWARDING TO THE INNER GATEWAY WOULD BE WRONG HERE, which is why
        this one wrapper answers for itself. ``LiteLLMGateway`` names the model
        in force from the task's route; on a canonical Run that is not who
        decides — :meth:`_turn_candidates` above (the model-policy catalog
        under the posture AND evidence in force right now) is, and this method
        is nothing but its head plus the budget arithmetic, so there is no
        second copy of the order here to drift.

        ``requested_model`` and ``lane`` (the capability's caller-side inputs)
        are deliberately NOT consulted: on a canonical Run the frozen
        admission selection and the policy catalog decide the candidates —
        ``_auto`` overrides the lane per candidate and ``_explicit`` pins the
        admission-authorized model — so the incoming pin never filters this
        path's candidate list.

        Never raises, per the capability contract: an unroutable policy, an
        unregistered model, or unreadable evidence returns ``None`` and the
        caller keeps its composed default; the SAME condition raises typed and
        audited from ``complete`` an instant later.
        """
        del requested_model, lane
        try:
            model, _lane = (await self._turn_candidates(task))[0]
            return TurnModelBudget(model=model, budget=self._selection.context_budget(model))
        except (ModelSelectionUnavailable, ModelSelectionEvidenceError, UnknownModelSelection):
            # The route authority is unreadable or names nothing registered. The
            # SAME condition reaches ``complete`` on the next line of execution
            # and raises there, typed and audited; answering ``None`` here keeps
            # the composed boot budget armed in the meantime rather than
            # inventing a window or turning a routing fault into a trim fault.
            return None

    def stream(self, request: CompletionRequest) -> Any:
        raise RuntimeError("canonical Agent Run streaming is not an admitted continuity effect")


class ProfileBoundRunExecutionAdapter:
    """Select one exact boot binding and execute through PRE-010B capabilities."""

    def __init__(
        self,
        *,
        bindings: Mapping[str, AgentExecutionBinding],
        principal_resolver: PrincipalResolver,
        deterministic_executor: DeterministicExecutor,
        runtime_settings: Any,
        sink: Any,
        source_guard: Any = None,
        catalog: Any = None,
        agent_skills: Any = None,
        run_store: Any = None,
        model_selection: ModelSelectionService | None = None,
        effective_tool_grants: Mapping[str, tuple[str, ...]] | None = None,
        scope_executor: Callable[..., Any] = scoped_tool_executor,
        grounding: GroundingPolicy | None = None,
        grounded_run_agent_ids: tuple[str, ...] = (),
        grounding_run_strictness: StrictnessTier = StrictnessTier.STRICT,
    ) -> None:
        self._bindings = bindings
        self._principals = principal_resolver
        self._deterministic = deterministic_executor
        self._runtime_settings = runtime_settings
        self._sink = sink
        self._source_guard = source_guard
        self._catalog = catalog
        self._agent_skills = agent_skills
        self._run_store = run_store
        self._model_selection = model_selection
        self._effective_tool_grants = effective_tool_grants
        self._scope_executor = scope_executor
        # NE-GA-RUN1 (fail-loud config, owner rule: never silently downgrade):
        # a gated agent id with no composed policy would run silently ungated.
        if grounded_run_agent_ids and grounding is None:
            raise ValueError(
                "grounded_run_agent_ids requires a composed GroundingPolicy: "
                "a gated Run agent must never execute ungated"
            )
        # The G4 assist is structurally stripped on the Run path: the server
        # binds every claim to the EXACT terminal bytes (NE-SPAN-BIND), so a
        # post-verdict re-draft/polish would invalidate the claim spans of the
        # very output it rewrites. `replace` re-runs the frozen dataclass
        # construction; chat's policy instance is untouched.
        #
        # NE-GA-RUN4 (U11 fix F1): the Run gate's tier is its OWN deployment
        # knob (OntologySettings.grounding_run_strictness), re-tiered here for
        # the same reason the assist is stripped here — the ONE shared policy
        # arrives carrying the CHAT posture, and inheriting a chat-STANDARD
        # tier let a digit-free empty-corpus draft complete as a served
        # "grounded" Run answer (vocab-ungrounded is advisory at STANDARD).
        # The parameter default is STRICT so no construction path can inherit
        # a looser chat tier by omission. The PROFILE is deliberately NOT
        # re-tiered: the knob moves enforcement, never the rulebook. And the
        # knob moves VOCABULARY enforcement only — the zero-fetch guarantee is
        # the NE-GA-RUN5 empty-evidence invariant in _agentic_result, which no
        # tier (and no allowlist-only phrasing) can evade.
        self._grounding = (
            None
            if grounding is None
            else replace(grounding, assist=None, strictness=grounding_run_strictness)
        )
        self._grounded_run_agent_ids = frozenset(grounded_run_agent_ids)

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        lane = (
            None
            if context.run.requested_model_lane is None
            else Lane(context.run.requested_model_lane)
        )
        with (
            run_trace_scope(context.run.run_id),
            run_requested_model_scope(context.run.requested_model, lane=lane),
            action_scope(f"agent-run:{context.run.run_id}:{context.attempt_id}"),
        ):
            return await self._execute_in_scope(context)

    async def _execute_in_scope(self, context: RunExecutionContext) -> RunExecutionResult:
        await context.boundary.heartbeat("profile:binding")
        try:
            binding = self._bindings[context.run.agent_id]
        except KeyError as exc:
            raise RuntimeError("Run Agent has no composed execution binding") from exc
        if binding.agent_version != context.run.agent_version:
            raise RuntimeError("Run Agent version differs from its composed execution binding")
        if binding.strategy.value != context.run.strategy:
            raise RuntimeError("Run strategy differs from its composed execution binding")

        principal = await context.boundary.await_inflight(
            self._principals.resolve(context.run.owner_principal_id),
            stage="profile:principal-resolution",
        )
        if principal.id != context.run.owner_principal_id:
            raise PermissionError("resolved Run principal differs from the accepted owner")
        authenticated = current_authenticated_context()
        if authenticated is not None:
            if authenticated.principal_id != context.run.owner_principal_id:
                raise PermissionError("authenticated Run principal differs from the accepted owner")
            principal = Principal(
                id=principal.id,
                groups=tuple(
                    sorted(set(principal.groups).union(authenticated.authorization_groups))
                ),
            )

        token = TURN_PRINCIPAL.set(principal)
        try:
            return await self._execute_as_principal(context, binding, principal)
        finally:
            TURN_PRINCIPAL.reset(token)

    async def _execute_as_principal(
        self,
        context: RunExecutionContext,
        binding: AgentExecutionBinding,
        principal: Principal,
    ) -> RunExecutionResult:
        if context.continuation is not None:
            return await self._execute_effect_continuation(context, principal)
        if binding.workflow_profile is not None:
            profile = binding.workflow_profile
            payload = _input_object(context.input_bytes)
            if self._run_store is None:
                raise RuntimeError("deterministic Run profile receipt storage is unavailable")
            await self._run_store.record_execution_profile(
                run_id=context.run.run_id,
                attempt_id=context.attempt_id,
                workflow_id=profile.workflow_id,
                profile_version=profile.version,
                profile_hash=profile.config_hash,
                input_schema=dict(profile.input_schema),
                ordered_plan=tuple(step.model_dump(mode="json") for step in profile.fixed_plan),
            )
            await context.boundary.heartbeat("profile:deterministic-before")

            async def execute_profile(value: BaseModel) -> object:
                return await self._deterministic(profile, value, principal, context)

            with direct_publication_scope(
                workflow_execution_id=context.run.run_id,
                publication_attempt_id=context.attempt_id,
            ):
                executed = await run_deterministic_profile(
                    profile=profile,
                    inputs=payload,
                    executor=execute_profile,
                )
                if not isinstance(executed, BaseModel):
                    raise RuntimeError("deterministic Run returned no typed terminal result")
                if _typed_evidence_refs(executed) and not context.executor.served_protected_refs:
                    raise RuntimeError(
                        "deterministic source-derived result lacks a complete Run delivery set"
                    )
                complete_run_direct_publication_observation()
            await context.boundary.heartbeat("profile:deterministic-after")
            output = json.dumps(
                executed.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            return RunExecutionResult(
                outcome="execution_complete",
                output_bytes=output,
                structured_envelope=_source_free_envelope(output),
            )

        adapter = binding.agentic_adapter
        if adapter is None:  # defensive: AgentExecutionBinding already excludes this
            raise RuntimeError("agentic Run has no bounded adapter")
        instruction = _input_text(context.input_bytes)
        integrations = (self._catalog, self._agent_skills, self._run_store)
        if all(value is None for value in integrations):
            # Test-only/backward-compatible construction. The production builder
            # below always supplies the complete protected-Run composition.
            # U11 fix F5: this lane runs the adapter with NO grounding recorder
            # and NO gate. For an agent id the deployment gated that would
            # silently contradict the constructor's own "a gated Run agent must
            # never execute ungated" rule, so it refuses instead. Unreachable
            # from the production builder (which composes all three
            # integrations); the guard closes the latent lane.
            if context.run.agent_id in self._grounded_run_agent_ids:
                raise RuntimeError(
                    "gated Run agent must never execute ungated: the test-only "
                    "composition lane has no grounding recorder or gate"
                )
            await context.boundary.heartbeat("profile:agentic-before")
            result = await adapter.run(
                task=instruction,
                principal=principal,
                executor=context.executor,
                gateway=context.gateway,
                settings=self._runtime_settings,
                sink=self._sink,
                source_guard=self._source_guard,
                boundary=context.boundary,
            )
            return await self._agentic_result(context, result, adapter)
        if any(value is None for value in integrations):
            raise RunActivationError("its agentic composition is incomplete on this worker")

        catalog_row = self._catalog.get_visible(context.run.agent_id, principal)
        if catalog_row is None:
            raise RunActivationError("its Agent is no longer in the catalog visible to its owner")
        definition = catalog_row.definition
        if not catalog_row.present or not catalog_row.available:
            raise RunActivationError(
                "its Agent is not currently available in the catalog visible to its owner"
            )
        if definition.agent_id != context.run.agent_id:
            raise RunActivationError(
                "the current catalog answers its Agent id with a different Agent"
            )
        if definition.version != context.run.agent_version:
            raise RunActivationError(
                "its frozen Agent version is not the version the catalog now holds"
            )
        if definition.default_strategy != context.run.strategy:
            raise RunActivationError(
                "its frozen strategy is not the strategy its Agent now declares"
            )
        continuity_active = context.run.model_policy_id is not None
        if continuity_active and (
            context.run.agent_definition_hash != definition.definition_hash
            or context.run.model_policy_id != definition.model_policy_id
        ):
            raise RunActivationError(
                "its frozen Agent definition or model policy is not the one currently in force"
            )
        if continuity_active and self._model_selection is None:
            raise RunActivationError("its model-selection recheck is not composed on this worker")

        # POST_DEMO item 12, increment 1b.
        #
        # ⚠️ THESE MARKERS DO NOT MEASURE THE OPENING GAP, and this comment says so
        # HERE because it is the one a reader reaches first. The production
        # `profile:agentic-before` is BELOW this call, so skill activation and
        # history materialization both happen BEFORE the measured
        # `profile:agentic-before` → `run-model:before:0` edge begins. An earlier
        # version of this comment claimed the opposite and was corrected ninety
        # lines below — leaving the wrong claim exactly where it would be read
        # first, which is the same defect the rollout note had three times.
        #
        # ⚠️ AND `profile:adapter-before` IS NOT THE SPLITTER EITHER — this comment
        # said it was, which is the SAME stale attribution one layer down, caught
        # by a reviewer in the round that added the real one. Nothing between the
        # production `profile:agentic-before` and that marker does any I/O, so it
        # partitions the edge into ~0 and ~all of it. The marker that actually
        # divides unmeasured work is `profile:agent-built`, in the ADAPTER
        # (`ne.runtime.runner`), after the governed tools and the Agent are built.
        #
        # What these DO measure is skill activation and history materialization,
        # which nothing measured before.
        #
        # ⚠️ AND THEY BOUND IT RATHER THAN MEASURE IT. Each heartbeat is a full
        # synchronous transaction on the event loop: the `begin` listener's two
        # `SET LOCAL` GUCs, `_database_now`'s clock `SELECT`, two
        # `SELECT ... FOR UPDATE`, an `UPDATE`, the span `INSERT` into a table
        # nothing reaps, and the `COMMIT`. Eight items; FIVE of them are SQL
        # statements `probe()` itself issues (the ledger row states that count) —
        # the GUCs and the `COMMIT` come from the engine, not from `probe()`.
        #
        # `observed_at` is `run_workflow._database_now`, which executes
        # `current_timestamp` on PostgreSQL — each transaction's BEGIN time,
        # fixed for the whole transaction. (`store._database_now` is a DIFFERENT
        # function that uses `clock_timestamp()` and stamps nothing here; reading
        # the wrong one of the two is what made this paragraph wrong once.)
        #
        # So, per marker and NOT claimed exhaustive: the interval carries, from
        # the OPENING marker, everything after ITS begin — all eight items above
        # — and from the CLOSING marker only what happens BEFORE its begin: the
        # pool checkout, bounded by `pool_timeout` and carrying the replacement
        # pre-ping on any checkout after a connection's first. Its lock waits are
        # NOT inside, since its `FOR UPDATE`s follow its own begin.
        #
        # Neither term is hypothetical. `agent-runs` runs at
        # `worker_concurrency=turn_concurrency` = 4 (`main.py:501`), so four runs
        # and their ~1 s renewal polls share one sync engine pool; and the
        # `INSERT` term GROWS as the unreaped table ages.
        #
        # Everything before the agent loop totals 0.11 s — the same order as the
        # instrument. Read `skills-before → skills-after` as an UPPER BOUND on
        # skill activation AND its receipt write (see the note at `skills-after`,
        # which sits below `record_skill_activation` on purpose), never as
        # activation's cost. That is the identical argument that retracted
        # `adapter-before`'s value, and it applies here too.
        #
        # ⚠️ THIS PARAGRAPH WAS WRONG IN FOUR SUCCESSIVE ROUNDS, in both
        # directions, and twice the fix left its predecessor readable underneath
        # it. What kept regenerating the defect was the round-by-round narration
        # living HERE, in the file an operator calibrates from, where the last
        # paragraph wins. The narration now lives in the build ledger
        # — which states every retraction verbatim, and is the
        # thing to read before trusting any number in it. THIS comment states
        # only what is currently true.
        await context.boundary.heartbeat("profile:skills-before")
        surface_key = f"run:{context.run.run_id}:attempt:{context.attempt_id}:skills"
        activation = await self._agent_skills.activate(
            definition,
            principal,
            surface_key=surface_key,
        )
        expected_pins = tuple(
            (grant.skill_id, grant.version, grant.content_hash) for grant in definition.skills
        )
        activated_pins = tuple((pin.name, pin.version, pin.content_hash) for pin in activation.pins)
        if activated_pins != expected_pins:
            # EXACTNESS IS THE PRODUCT PROPERTY and it is unchanged: all of the
            # Agent's pins, in order, or the run refuses. What changed is that the
            # refusal now PUBLISHES. This escaped into DBOS as a bare
            # `RuntimeError` — "Future exception was never retrieved" — and the
            # reader got `Recorded failure class: RuntimeError` for a run that
            # died in seconds without touching a source. The commonest live cause
            # is a caller who cannot see a Skill the Agent pins (the surface drops
            # an ACL-invisible pin silently, by design), which is invisible from
            # the class name alone and obvious from this one.
            raise RunSkillActivationError(pinned=len(expected_pins), activated=len(activated_pins))
        await self._run_store.record_skill_activation(
            run_id=context.run.run_id,
            attempt_id=context.attempt_id,
            agent_id=definition.agent_id,
            agent_version=definition.version,
            surface_key=surface_key,
            pins=activated_pins,
            index_hash=activation.index_hash,
            surface_hash=activation.surface_hash,
        )
        # ⛔ THIS MARKER SITS BELOW THE RECEIPT WRITE, AND THAT PLACEMENT IS A
        # CORRECTION Codex made me earn. I first put it immediately after
        # `activate()`, which reads as the tighter bound and is where the cost
        # ends. But `heartbeat` is not a passive stamp: it renews the lease and
        # RAISES `RunCancellationRequested` when the run is CANCELLING. On
        # `origin/main` there is no boundary between `activate()` returning and
        # `record_skill_activation` committing, so a cancellation admitted during
        # activation could not interrupt that pair. My marker introduced an abort
        # point inside it.
        #
        # What that costs is a FALSE DURABLE RECORD, which is why it is not a
        # placement preference. `AgentRunStore.record_skill_activation` defines an
        # ABSENT receipt to mean the attempt died BEFORE activation. A run
        # cancelled in the new window activated its Skills and then published a
        # record saying it had not — the platform stating something untrue about
        # itself, which is the one thing C10 exists to prevent.
        #
        # HONEST COST OF THE FIX, stated rather than glossed: the interval
        # `skills-before -> skills-after` now covers activation AND the receipt
        # INSERT, so it is a WIDER upper bound than the version above would have
        # been. That is the right trade -- an upper bound that is a little loose
        # still answers "is skill activation where the 7.67 s goes?", whereas a
        # tighter bound bought with a window that mis-states the record answers a
        # question nobody asked at a price nobody agreed to.
        await context.boundary.heartbeat("profile:skills-after")
        if self._effective_tool_grants is None:
            raise RuntimeError("effective Agent Tool grants are not composed")
        try:
            effective_grants = self._effective_tool_grants[definition.agent_id]
        except KeyError as exc:
            raise RuntimeError("Run Agent has no composed effective Tool grants") from exc
        scoped_executor = self._scope_executor(
            context.executor,
            granted=effective_grants,
            scope=f"agent-run:{context.run.run_id}:{context.attempt_id}",
        )
        # The run's instructions are the Agent's OWN manifest instructions plus
        # the rendered Skill index for this attempt's activation, in that order.
        # Either half may legitimately be absent (a Skill-pinned Agent may carry
        # no prose of its own; a zero-pin Agent has an empty index by design —
        # `render_index` renders no packs as "" rather than a dangling header),
        # but BOTH absent is a misconfiguration the manifest already refuses at
        # load time (`AgentManifestDefinition._strategy_contract`). This guard is
        # the worker's defense in depth on the same rule: it publishes a governed
        # reason instead of letting `BoundedAgenticAdapter.__post_init__` kill
        # the attempt with a bare ValueError after admission — the exact
        # second-order failure that emptied the Analyst's prompt when its last
        # Skill pin was removed.
        composed_instructions = "\n\n".join(
            part for part in (definition.instructions, activation.index_text) if part
        )
        if not composed_instructions.strip():
            raise RunActivationError(
                "its Agent declares no instructions and activated no Skills, "
                "so the run would have an empty system prompt"
            )
        run_adapter = replace(adapter, instructions=composed_instructions)
        # Bracketed ONLY when materialization actually runs. My first version put
        # both heartbeats outside the conditional, so the pair appeared on every
        # no-continuity run too — which destroys the one property I claimed for it,
        # that its PRESENCE is diagnostic. Codex caught that. A run without
        # continuity now emits no pair at all, so presence means the work happened.
        if continuity_active:
            await context.boundary.heartbeat("profile:history-before")
            history = await self._run_store.materialize_conversation_history(
                run_id=context.run.run_id,
                owner_principal_id=principal.id,
            )
            await context.boundary.heartbeat("profile:history-after")
        else:
            history = []
        continuity_gateway: ModelGateway = context.gateway
        if continuity_active:
            assert self._model_selection is not None
            continuity_gateway = _RunContinuityGateway(
                context.gateway,
                context=context,
                principal=principal,
                catalog=self._catalog,
                selection=self._model_selection,
                run_store=self._run_store,
            )
        run_settings = self._runtime_settings.model_copy(
            update={"task": definition.model_policy_id}
        )
        if continuity_active:
            # THE ADMISSION PIN, and since NE-CTX-IN-FORCE it is the FALLBACK,
            # not the enforced number. It is pinned ONCE here, to the model
            # authorized at admission, and it cannot follow the `_auto` roll —
            # which is precisely the air-gap beat's failure: admitted on the
            # frontier lane, rolled onto the private one, still trimming to the
            # frontier window. `_RunContinuityGateway.context_budget_for_turn`
            # re-derives per turn from the posture in force and wins whenever it
            # can name a model; this value stays as the armed default for the
            # turn it cannot.
            assert self._model_selection is not None
            run_settings = run_settings.model_copy(
                update={
                    "context_budget": self._model_selection.context_budget(
                        str(context.run.authorized_model)
                    )
                }
            )
        await context.boundary.heartbeat("profile:agentic-before")
        # The Agent's DECLARED knowledge boundary, bound for the whole
        # run alongside its frozen Skill pins. Both are the same kind of fact —
        # what THIS definition authorized this attempt to read — and both are
        # derived from the definition, never from the model or the request.
        # ``read.knowledge.search`` reads this carrier per call and refuses (never
        # widens) when the declared scope resolves to no searchable corpus; see
        # ``ne.api.knowledge_scope``. Binding it unconditionally is the point: an
        # agentic run can then never reach the chunk store unscoped, including one
        # whose definition declares no knowledge source at all.
        # NE-GA-RUN1: a configured grounded Run agent gets its (already
        # toolset-scoped) executor wrapped in a FRESH per-attempt
        # GroundingRecorder — the same "one recorder per run() invocation"
        # discipline as the chat path (grounding.py module docstring): a prior
        # attempt's fetches must never ground this one. The recorder delegates
        # every execute() unchanged (C6 — no new tool path), and the verified-
        # read handlers record their structured VerifiedAnswers into it via
        # current_answer_recorder() for the gate over the terminal draft below.
        gate_policy = (
            self._grounding
            if self._grounding is not None and context.run.agent_id in self._grounded_run_agent_ids
            else None
        )
        recorder = GroundingRecorder(scoped_executor) if gate_policy is not None else None
        with (
            run_skill_pins(activation.pin_map()),
            run_knowledge_scope(declared_source_ids(definition)),
        ):
            # POST_DEMO item 12. ⚠️ THIS MARKER DOES NOT SPLIT THE EDGE EITHER, and
            # the comment that said it did was the same overclaim, one round later.
            #
            # Established first: there are TWO `profile:agentic-before` sites and
            # the production one is BELOW the skills and history calls, so those
            # four markers fire BEFORE the 7.67 s edge begins. My two "suspects"
            # were never inside it. True, and it is why this one was added.
            #
            # Then an adversarial pass read what is actually between
            # `profile:agentic-before` (above) and this line: a comment block, two
            # attribute reads for `gate_policy`, a `GroundingRecorder` constructor
            # that does nothing, and two ContextVar scopes over a dict
            # comprehension and a frozenset build. NO I/O. So this boundary
            # partitions 7.67 s into ~0 and ~7.67 s — a split that is determinate
            # by reading the file, and one a heartbeat's own transaction round
            # trips would dominate anyway.
            #
            # IT IS KEPT, and only for what it honestly is: the fence proving the
            # cost is NOT in the worker's setup. The marker that divides work
            # nobody has measured is `profile:agent-built`, inside the adapter
            # (`ne.runtime.runner`) — after the governed tools and the Agent are
            # built, before pydantic-ai is run. That is where the unmeasured
            # region actually lives -- and note that "before pydantic-ai is run"
            # states that marker's POSITION, not what the region right of it
            # contains. What it contains is the protected-history authorization
            # (`materialize_history`, the budget trim, `reauthorize_history`)
            # AND -- on the continuity floor, which is this row's stated
            # production shape -- the whole model-policy recheck delimited by
            # `profile:policy-recheck-before`: `_definition()`'s catalog and
            # frozen-policy check, `choices(...)`, `verify_model_boundary` and
            # `authorize`. (NOT `record_model_step`: it runs after the inner
            # `complete()` returns, so it is right of `run-model:before:0`, not
            # inside the recheck.) All of it is this platform's own code, not the
            # library's. ⚠️ This sentence named only
            # the first half for one round while `runner.py`'s twin named both --
            # in TWO separate paragraphs there: the model-policy half under its
            # "limit (1)", the protected-history half under the
            # `profile:agent-built` note below it. (An earlier version cited
            # `runner.py:2349-2357`, which is limit (1) ALONE, so a reader
            # checking it found the history half absent and could not tell whether
            # the claim or the citation was wrong -- and it was a raw line anchor
            # into a file this branch edits every round, which this row's own rule
            # calls a citation that rots by construction.) Fixing the copy a
            # reviewer cites and leaving its twin is the failure mode this PR has
            # now hit at four separate sites. Spelled
            # out because attributing that region to pydantic-ai is the exact
            # mis-description this PR has had to retract three times.
            await context.boundary.heartbeat("profile:adapter-before")
            result = await run_adapter.run(
                task=instruction,
                principal=principal,
                executor=recorder if recorder is not None else scoped_executor,
                gateway=continuity_gateway,
                settings=run_settings,
                sink=self._sink,
                source_guard=self._source_guard,
                message_history=history,
                boundary=context.boundary,
            )
        return await self._agentic_result(
            context,
            result,
            run_adapter,
            principal=principal,
            prompt=instruction,
            recorder=recorder,
            gate_policy=gate_policy,
        )

    async def _agentic_result(
        self,
        context: RunExecutionContext,
        result: Any,
        adapter: BoundedAgenticAdapter | None = None,
        *,
        principal: Principal | None = None,
        prompt: str = "",
        recorder: GroundingRecorder | None = None,
        gate_policy: GroundingPolicy | None = None,
    ) -> RunExecutionResult:
        await context.boundary.heartbeat("profile:agentic-after")
        outcomes = {
            "complete": "execution_complete",
            "context-exhausted": "context_exhausted",
            "budget-exhausted": "budget_exhausted",
        }
        outcome = outcomes[result.outcome]
        if outcome != "execution_complete":
            return RunExecutionResult(
                outcome=outcome,  # type: ignore[arg-type]
                exhausted_reason=(
                    _budget_exhausted_reason(getattr(result, "exhausted_cap", None), adapter)
                    if outcome == "budget_exhausted"
                    else None
                ),
            )
        if gate_policy is not None:
            # NE-GA-RUN1: the whole-answer grounding POST-step, run over the
            # terminal draft BEFORE any claim is verified or persisted. This is
            # ADDITIVE to the per-claim verifier below — a draft that passes the
            # gate still has every server-bound claim re-sliced and receipted.
            if recorder is None or principal is None:
                raise RuntimeError("gated Run reached its terminal without a grounding recorder")
            gate = await run_grounding_gate(
                draft=str(result.output),
                prompt=prompt,
                principal=principal,
                fetched=recorder.answers,
                policy=gate_policy,
            )
            if gate.provenance.fallback:
                # WITHHELD. The gate has already emitted its ONE audited
                # guardrail.block (classes + counts, C12). The unproven draft —
                # and every claim the server bound to its exact bytes — is
                # discarded; what is served is a source-free, server-authored
                # account of the decision under the finalizer's existing
                # `disposition="refused"` lane (status COMPLETED, run outcome
                # "refused", console decline card). The gate's rendered
                # fallback tables are NOT served as the answer: the verified
                # records already have their own delivery receipts and
                # evidence rows in this run, and a refused terminal is stored
                # source-free by contract.
                return self._grounding_withheld_result(gate)
            # NE-GA-RUN5 (U11 review round 2, P1) — the NON-EMPTY-EVIDENCE
            # INVARIANT, enforced here and not inside the gate: strictness only
            # moves which finding CLASSES enforce, and a draft composed
            # entirely of core-allowlisted words produces ZERO findings — so
            # "empty-corpus turns always withhold" is not a property any tier
            # can deliver ("The record is verified." passed the gate over an
            # empty corpus at STRICT). A gated Run exists to serve answers
            # certified against evidence fetched THIS run; zero fetches means
            # there is nothing to certify against, so the answer is withheld
            # REGARDLESS of the gate's token analysis — the one place the
            # guarantee is enforceable, unlike vocabulary analysis, which an
            # allowlist-only phrasing always evades. Audited fail-closed like
            # every other grounding decision; never a grounding.pass.
            if not recorder.answers:
                await self._emit_grounding_empty_evidence(gate_policy, gate)
                return self._grounding_empty_evidence_result()
            # PASS with assist=None is byte-parity by G3 contract: the gate may
            # only ever emit the certified draft itself. The claim spans below
            # bind to these exact bytes, so any divergence is a server bug that
            # must kill the attempt rather than serve re-worded bytes under
            # another text's claim receipts.
            if gate.output != result.output:
                raise RuntimeError(
                    "grounding gate altered a passing Run draft; claim spans no longer bind"
                )
            # U11 fix F4: a PASSING gated Run must leave an audit trace. The
            # withhold branch already records its ONE guardrail.block, but a
            # clean PASS previously emitted nothing — the audit trail could not
            # distinguish "gated and certified" from "never gated" for a served
            # answer. Emit exactly one counts-and-identifiers-only
            # `grounding.pass` event carrying the full gate provenance (C12:
            # template ids/versions/config hashes and counts — never draft
            # text), fail-closed like every other grounding audit append (C10).
            await self._emit_grounding_pass(gate_policy, gate)
        output = result.output.encode("utf-8")
        claims = tuple(getattr(result, "claims", ()))
        served_row_ids = tuple(getattr(result, "served_row_ids", ()))
        if claims:
            if self._run_store is None:
                raise RuntimeError("structured Run claims require the exact composed Run store")
            return await self._source_bound_agentic_result(context, result, output)
        if any(
            isinstance(reference, str) and reference.startswith(PROTECTED_REFERENCE_PREFIX)
            for reference in served_row_ids
        ):
            # BACKSTOP. `ne.runtime.runner._install_claim_contract_validator`
            # now enforces this at generation time, where the model still has a
            # bounded retry to correct it, so reaching here means that validator
            # was bypassed or a non-agentic path produced source-bearing output.
            # It raises a TYPED error rather than a bare RuntimeError only so it
            # carries a `safe_reason`: this line is the one that published
            # `failure_reason: null` and left the reader a bare status word.
            raise AgentOutputContractError(
                "The answer used this Run's sources but declared no claims binding them, "
                "so it was not served."
            )
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=_source_free_envelope(output),
        )

    async def _execute_effect_continuation(
        self, context: RunExecutionContext, principal: Principal
    ) -> RunExecutionResult:
        """Execute the ONE approved effect and finish — no model, no agent loop.

        The continuation carries the tool the human's approval NAMED and the
        exact parked arguments that ``authorize(operation="resume")`` just
        re-authorized against every source-authority stamp. It goes through the
        SAME receipt-gated, governed executor as any other effect: the pre-effect
        Run audit receipt still gates it, the PDP still decides it, and the
        approval gate still consults the durable continuation (which it finds
        already ``resumed`` and approved by a named human).

        The terminal output is a content-free notice. The tool's own result is
        deliberately NOT served as the Run's answer: it may carry protected
        source material, and this path has no grounding gate to prove an answer
        against. Naming the executed action is the honest thing this seam knows.
        """

        continuation = context.continuation
        if continuation is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("effect continuation is unavailable")
        await context.boundary.heartbeat("profile:continuation-before")
        # A continuation is a DIRECT publication in exactly the deterministic
        # profile's sense: no model call, one governed effect, a server-authored
        # terminal. It rides the same observation boundary so the finalizer sees
        # a complete delivery set instead of an unknown one.
        with direct_publication_scope(
            workflow_execution_id=context.run.run_id,
            publication_attempt_id=context.attempt_id,
        ):
            await context.executor.execute(
                continuation.action, continuation.arguments, principal=principal
            )
            complete_run_direct_publication_observation()
        await context.boundary.heartbeat("profile:continuation-complete")
        notice = f"The approved action {continuation.action} was executed after human approval."
        output = notice.encode("utf-8")
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=_source_free_envelope(output),
        )

    async def _emit_grounding_pass(self, policy: GroundingPolicy, gate: Any) -> None:
        """Emit the ONE pass-side `grounding.pass` audit event for a gated Run.

        Rides the SAME sink the gate's other audit events use (the policy's own
        composed sink — guardrail.block / grounding.advisory / grounding.assist
        all land there), so the whole grounding decision history for a run
        reads from one channel. `detail` is the serialized
        :class:`~ne.contracts.runtime.GroundedProvenance` — fetch identifiers,
        counts, gate/strictness strings; no draft or row text (C12). Fail
        closed (C10): an un-auditable certification kills the attempt rather
        than serving the answer with its pass-side trace silently missing.
        """
        event = new_audit_event(
            action="grounding.pass",
            resource="grounding.gate",
            outcome=AuditOutcome.success,
            actor="ne-worker",
            detail=json.dumps(gate.provenance.model_dump(mode="json"), sort_keys=True),
            trace_id=current_run_trace_id(),
        )
        try:
            await policy.sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                "audit append failed for 'grounding.pass' on 'grounding.gate'; "
                "the certified answer is withheld (C10 fail closed)"
            ) from exc

    async def _emit_grounding_empty_evidence(self, policy: GroundingPolicy, gate: Any) -> None:
        """Audit the ONE `grounding.empty_evidence` denied event for a gated Run
        whose gate PASSED over zero fetched answers (NE-GA-RUN5). Same sink as
        the gate's other events; counts/identifiers only (C12); fail closed
        (C10) — an un-auditable invariant withhold kills the attempt."""
        event = new_audit_event(
            action="grounding.empty_evidence",
            resource="grounding.gate",
            outcome=AuditOutcome.denied,
            actor="ne-worker",
            detail=json.dumps(
                {
                    "reason": "empty-evidence",
                    "fetch_count": 0,
                    "gate": str(gate.provenance.gate),
                    "strictness": policy.strictness.value,
                },
                sort_keys=True,
            ),
            trace_id=current_run_trace_id(),
        )
        try:
            await policy.sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                "audit append failed for 'grounding.empty_evidence' on 'grounding.gate'; "
                "the answer stays withheld (C10 fail closed)"
            ) from exc

    def _grounding_empty_evidence_result(self) -> RunExecutionResult:
        """The empty-evidence terminal: a completed Run refused for LACK of
        evidence, not for what the draft said (NE-GA-RUN5). Same refused lane
        as `_grounding_withheld_result`; the notice is server-authored from the
        invariant alone — no draft byte, no finding classes to name."""
        notice = (
            "Answer withheld by the grounding gate. This run completed zero verified "
            "record reads, so no retrieved evidence exists to certify any answer "
            "against — the draft was discarded regardless of its wording "
            "(finding class: empty-evidence). Ask a question the agent's verified "
            "record reads can serve, or check that its data release is available."
        )
        output = notice.encode("utf-8")
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=_refused_envelope(output),
        )

    def _grounding_withheld_result(self, gate: Any) -> RunExecutionResult:
        """The withheld-run terminal: a completed Run whose answer was refused.

        NOT ``execution_failed`` — nothing failed. The system worked exactly as
        specified: the model produced a draft, the deterministic gate could not
        prove it against the run's verified evidence, and the deployment's
        posture is to refuse rather than serve. The refusal rides the
        finalizer's existing ``disposition="refused"`` lane (zero claims by
        validator; verdict REFUSED; run outcome "refused"; console renders the
        decline card with this text verbatim), so it is legible on screen as a
        decision, never as an error and never as an empty answer.

        C12 holds end-to-end: the text below is authored from finding CLASS
        strings and a COUNT only — no draft fragment, no fallback table, no
        source-derived content — which is also what licenses the finalizer to
        store it source-free (`run_finalizer._protect_output`).
        """
        count = int(gate.withheld_count)
        classes = ", ".join(dict.fromkeys(gate.withheld_reasons)) or "ungrounded-content"
        # U11 fix F2: the notice says what the gate actually validates —
        # numbers, identifiers, citations and vocabulary provenance — not
        # "statements proven", which overclaimed semantic verification the
        # mechanism never performs (wrong-row prose pairing is a documented
        # residual, see the agent's registry entry).
        notice = (
            "Answer withheld by the grounding gate. The deterministic gate found "
            f"{count} element{'' if count == 1 else 's'} of the draft it could not "
            "verify against the rows retrieved during this run "
            f"(finding classes: {classes}) — unproven numbers, identifiers or "
            "citations, or wording the retrieved rows do not contain. The unverified "
            "draft was discarded and is not shown; the verified records this run "
            "retrieved were served through their governed reads and remain available "
            "in the run's evidence view."
        )
        output = notice.encode("utf-8")
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=_refused_envelope(output),
        )

    async def _source_bound_agentic_result(
        self, context: RunExecutionContext, result: Any, output: bytes
    ) -> RunExecutionResult:
        output_hash = hashlib.sha256(output).hexdigest()
        declarations: list[RunClaimEnvelopeDeclaration] = []
        evidence_bindings: list[RunEvidenceBinding] = []
        verifier_bindings: list[RunVerifierBinding] = []
        # NE-CLAIM-SHARED-EVIDENCE. One retrieved record may carry several
        # facts — a returned table naming two loans is one evidence record and
        # two claims — so N claims MAY cite one evidence record. Nothing below
        # is shared between them: `_local_binding_key` derives BOTH local keys
        # from the claim key, `verify_claim` re-slices this claim's own bytes,
        # and `claim_run_claim_verifier` mints a verifier receipt per claim
        # key. The persisted join agrees — `ne_agent_run_claim_evidence` makes
        # `claim_id` unique and deliberately does NOT constrain `evidence_id`.
        #
        # What must stay 1:1 is the CLAIM key, because both local keys are
        # derived from it: two claims sharing one would collapse two claims
        # onto one verifier row, which is the integrity loss that actually
        # matters. `ne.runtime.runner._server_bound_claims` cannot produce a
        # collision upstream (NE-SPAN-BIND: keys are server-minted, one per
        # served reference); the guard below is the backstop for any other
        # producer, and it is a TYPED error so the run publishes WHY instead
        # of a bare failure class.
        seen_claim_keys: set[str] = set()
        for claim in result.claims:
            if claim.claim_key in seen_claim_keys:
                raise AgentOutputContractError(
                    "The answer declared two claims under one claim key, so they could not each "
                    "be verified separately and the answer was not served."
                )
            seen_claim_keys.add(claim.claim_key)
            # NE-CLAIM-VERIFIED-SET. The load raises a TYPED
            # `RunClaimEvidenceUnavailable` carrying its own `safe_reason`, and it
            # is re-raised untouched: it already distinguishes "the answer cited
            # something this turn never served" from "the store could not read its
            # own evidence back", and collapsing the two into one message would
            # blame the model for a system fault or the reverse. Before this it was
            # a bare `LookupError` and the reader got `Recorded failure class:
            # LookupError` for a condition the server could name exactly.
            observation = await self._run_store.load_run_claim_evidence(
                run_id=context.run.run_id,
                owner_principal_id=context.run.owner_principal_id,
                attempt_id=context.attempt_id,
                turn_id=context.target.turn_id,
                protected_ref=claim.served_ref,
            )
            if isinstance(observation, RunClaimEvidenceSetObservation):
                # A verified (T3) read is backed by a SET — one evidence record per
                # (row, contributor). It is handed to the verifier WHOLE; the anchor
                # below is only the addressable member the claim/evidence join keys
                # on, and `verify_claim` binds every member into its receipt hash.
                evidence_observation = VerifiedRowSetEvidenceObservation(
                    protected_ref=observation.protected_ref,
                    protected_result_hash=observation.protected_result_hash,
                    evidence_set_id=observation.evidence_set_id,
                    set_key=observation.set_key,
                    delivery_receipt_id=observation.delivery_receipt_id,
                    delivery_receipt_hash=observation.delivery_receipt_hash,
                    protected_rendering=observation.protected_rendering,
                    members=tuple(
                        VerifiedRowSetMemberObservation(
                            evidence_id=member.evidence_id,
                            locator_id=member.locator_id,
                            locator_hash=member.locator_hash,
                            producing_event_id=member.producing_event_id,
                            locator=member.locator,
                        )
                        for member in observation.members
                    ),
                )
                anchor = observation.members[0]
                evidence_id = anchor.evidence_id
                locator_hash = anchor.locator_hash
                producing_event_id = anchor.producing_event_id
            else:
                evidence_id = observation.evidence_id
                locator_hash = observation.locator_hash
                producing_event_id = observation.producing_event_id
                single = (
                    DirectSearchEvidenceObservation
                    if (observation.read_kind, observation.assurance) == ("search", "direct")
                    else InferredGraphPathEvidenceObservation
                )
                evidence_observation = single(
                    protected_ref=observation.protected_ref,
                    protected_result_hash=observation.protected_result_hash,
                    evidence_id=observation.evidence_id,
                    locator_id=observation.locator_id,
                    locator_hash=observation.locator_hash,
                    producing_event_id=observation.producing_event_id,
                    delivery_receipt_id=observation.delivery_receipt_id,
                    delivery_receipt_hash=observation.delivery_receipt_hash,
                    protected_rendering=observation.protected_rendering,
                    locator=observation.locator,
                )
            verified = verify_claim(
                ClaimSlice(
                    claim_key=claim.claim_key,
                    claim_content_hash=claim.claim_content_hash,
                    span_start_byte=claim.span_start_byte,
                    span_end_byte=claim.span_end_byte,
                    served_output_hash=output_hash,
                    served_output=output,
                ),
                evidence_observation,
            )
            verifier = await self._run_store.claim_run_claim_verifier(
                run_id=context.run.run_id,
                owner_principal_id=context.run.owner_principal_id,
                attempt_id=context.attempt_id,
                claim_key=verified.claim_key,
                claim_content_hash=verified.claim_content_hash,
                served_output_hash=verified.served_output_hash,
                span_start_byte=verified.span_start_byte,
                span_end_byte=verified.span_end_byte,
                verifier_method=verified.verifier_method,
                verifier_version=verified.verifier_version,
                result_hash=verified.result_hash,
            )
            evidence_key = _local_binding_key("evidence", claim.claim_key)
            verifier_key = _local_binding_key("verifier", claim.claim_key)
            evidence_bindings.append(
                RunEvidenceBinding(
                    evidence_id=evidence_id,
                    evidence_key=evidence_key,
                    claim_key=claim.claim_key,
                    claim_content_hash=claim.claim_content_hash,
                    served_output_hash=output_hash,
                    span_start_byte=claim.span_start_byte,
                    span_end_byte=claim.span_end_byte,
                    producing_event_id=producing_event_id,
                    locator_hash=locator_hash,
                    served_citation_ref_hash=served_evidence_binding_hash(
                        claim_key=claim.claim_key,
                        claim_content_hash=claim.claim_content_hash,
                        served_output_hash=output_hash,
                        span_start_byte=claim.span_start_byte,
                        span_end_byte=claim.span_end_byte,
                        evidence_id=evidence_id,
                        delivery_receipt_id=observation.delivery_receipt_id,
                        protected_ref=verified.protected_ref,
                        protected_hash=verified.protected_result_hash,
                    ),
                    delivery_receipt_id=observation.delivery_receipt_id,
                )
            )
            verifier_bindings.append(
                RunVerifierBinding(
                    verifier_receipt_id=verifier.verifier_receipt_id,
                    verifier_key=verifier_key,
                    claim_key=claim.claim_key,
                    claim_content_hash=claim.claim_content_hash,
                    served_output_hash=output_hash,
                    span_start_byte=claim.span_start_byte,
                    span_end_byte=claim.span_end_byte,
                    verifier_method=verified.verifier_method,
                    verifier_version=verified.verifier_version,
                    result_hash=verified.result_hash,
                    event_id=verifier.event_id,
                    receipt_hash=verifier.receipt_hash,
                )
            )
            declarations.append(
                RunClaimEnvelopeDeclaration(
                    claim_key=claim.claim_key,
                    span_start_byte=claim.span_start_byte,
                    span_end_byte=claim.span_end_byte,
                    claim_content_hash=claim.claim_content_hash,
                    evidence_key=evidence_key,
                    verifier_key=verifier_key,
                )
            )
        claims = tuple(declarations)
        return RunExecutionResult(
            outcome="execution_complete",
            output_bytes=output,
            structured_envelope=StructuredRunOutputEnvelope(
                discriminator="structured_run_output",
                served_output_hash=output_hash,
                claim_count=len(claims),
                claim_set_hash=claim_set_hash(output_hash, claims),
                claims=claims,
                disposition="completed",
            ),
            evidence_bindings=tuple(evidence_bindings),
            verifier_bindings=tuple(verifier_bindings),
        )


def _refused_envelope(output: bytes) -> StructuredRunOutputEnvelope:
    """The withheld-run envelope: zero claims (validator-enforced for refused)."""
    output_hash = hashlib.sha256(output).hexdigest()
    claims = ()
    return StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=0,
        claim_set_hash=claim_set_hash(output_hash, claims),
        claims=claims,
        disposition="refused",
    )


def _source_free_envelope(output: bytes) -> StructuredRunOutputEnvelope:
    output_hash = hashlib.sha256(output).hexdigest()
    claims = ()
    return StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=0,
        claim_set_hash=claim_set_hash(output_hash, claims),
        claims=claims,
        disposition="completed",
    )


def _local_binding_key(kind: str, claim_key: str) -> str:
    return f"{kind}:{hashlib.sha256(claim_key.encode('utf-8')).hexdigest()}"


def _typed_evidence_refs(result: BaseModel) -> tuple[str, ...]:
    payload = result.model_dump(mode="json")
    refs: list[str] = []

    def collect(value: object, key: str | None = None) -> None:
        if isinstance(value, dict):
            for nested_key, nested in value.items():
                collect(nested, nested_key)
        elif isinstance(value, list):
            if key in {"evidence_refs", "control_evidence_refs"}:
                refs.extend(str(item) for item in value)
            else:
                for item in value:
                    collect(item, key)

    collect(payload)
    return tuple(refs)


def _input_object(value: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Run input is not a UTF-8 JSON object") from exc
    if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
        raise ValueError("Run input must be a JSON object")
    return parsed


def _input_text(value: bytes) -> str:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("agentic Run input is not UTF-8 text") from exc
    if not text.strip():
        raise ValueError("agentic Run input must be non-empty text")
    return text


def build_profile_bound_run_execution_adapter(platform: Any) -> ProfileBoundRunExecutionAdapter:
    """Build the worker consumer from the exact shared API/worker platform."""
    from ne.api.frontier_warm import GatewayFrontierWarm
    from ne.api.model_routing import build_route_provider
    from ne.api.posture import effective_egress
    from ne.api.serving import ServingStatusService
    from ne.govern import create_pdp
    from ne.permissions.resolver import StorePrincipalResolver

    def air_gap() -> bool:
        provider = platform.egress_settings_provider
        return True if provider is None else effective_egress(provider())[0]

    selection = ModelSelectionService(
        gateway_settings=platform.gateway_settings,
        # NE-CONTINUITY-PROBE-REUSE: the worker's serving evidence feeds only
        # the per-call continuity recheck of already admitted runs. The cache
        # TTL must stay WITHIN model_selection.PRIVATE_OBSERVATION_MAX_AGE_S
        # (5s): _private_choices rejects any row older than that bar, so a
        # longer TTL serves cached rows selection then refuses -- an
        # explicitly-pinned private Run died between calls with "model has no
        # current serving evidence" (measured live on demo.example.host,
        # 2026-08-28, runs c5df5012/05a7aecf: first call served, recheck
        # denied). The prior 30s TTL saved a ~36ms local probe per call and
        # cost the whole pinned-private Run path; the default 5s TTL keeps
        # reuse for rapid tool loops and re-probes across longer gaps.
        serving_status=ServingStatusService(),
        audit_engine=platform.audit_engine,
        pdp=create_pdp(platform.govern_settings, sink=platform.sink),
        sink=platform.sink,
        config_hash=platform.config_hash,
        air_gap_provider=air_gap,
        lifecycle_availability=(
            platform.inference_lifecycle.availability
            if platform.inference_lifecycle is not None
            else None
        ),
        route_provider=build_route_provider(platform.route_store),
        # NE-FRONTIER-LIVE: the worker re-authorizes at execution time, so it
        # needs the SAME warm seam the API has — otherwise a run admitted onto
        # the frontier lane could be refused (or silently demoted) here because
        # the API's evidence aged out of the 60s window while the run queued.
        frontier_warm=(
            GatewayFrontierWarm(platform.gateway)
            if platform.api_settings.frontier_warm_enabled
            else None
        ),
    )

    async def deterministic(
        profile: WorkflowExecutionProfile,
        inputs: BaseModel,
        principal: Principal,
        context: RunExecutionContext,
    ) -> object:
        max_subjects = inputs.model_dump().get("max_subjects")
        if not isinstance(max_subjects, int) or isinstance(max_subjects, bool):
            raise ValueError("deterministic profile input has no integer max_subjects")
        with run_trace_scope(context.run.run_id) as trace_id:
            return await run_workflow(
                profile.workflow_id,
                executor=context.executor,  # type: ignore[arg-type]
                dispatcher=platform.workflow_dispatcher,
                gate=platform.gate,
                principal=principal,
                trace_id=trace_id,
                max_subjects=max_subjects,
                source_guard=platform.source_guard,
                protected_output_writer=platform.protected_source_output_writer,
                protected_execution_writer=platform.protected_source_execution_writer,
            )

    return ProfileBoundRunExecutionAdapter(
        bindings=platform.agent_execution_bindings,
        principal_resolver=StorePrincipalResolver(platform.expander),
        deterministic_executor=deterministic,
        runtime_settings=platform.runtime_settings,
        sink=platform.sink,
        source_guard=platform.source_guard,
        catalog=platform.agent_catalog,
        agent_skills=platform.agent_skills,
        run_store=platform.agent_run_store,
        model_selection=selection,
        effective_tool_grants=platform.effective_agent_tool_grants,
        # NE-GA-RUN1: the ONE platform-composed GroundingPolicy (built at the
        # C3 root next to the verified router) + the configured gated Run agent
        # ids. Both default to off/empty — the adapter constructor fails loud
        # on ids-without-policy and strips the G4 assist for the Run path.
        grounding=platform.grounding,
        grounded_run_agent_ids=platform.grounded_run_agent_ids,
        # NE-GA-RUN4: the Run-path tier is its own deployment knob
        # (NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS, default strict) — the adapter
        # re-tiers the shared chat policy so a chat-STANDARD posture never
        # loosens the Run gate silently.
        grounding_run_strictness=StrictnessTier(platform.grounded_run_strictness),
    )
