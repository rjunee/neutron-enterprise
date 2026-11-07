# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Reusable governed-agent-loop primitives (NE-034).

These helpers are the VERBATIM-SEMANTICS extraction of the per-run tool wiring
that :class:`~ne.runtime.agent.DepartmentAgentSession` shipped in NE-033, factored
here so a sub-agent (:mod:`ne.runtime.dispatch`) and a cron job
(:mod:`ne.runtime.scheduler`) can build the SAME governed pydantic-ai loop over
ANY executor that exposes the governed surface — without copying the C6-critical
closure pattern.

C6 stays airtight because every tool this module builds has exactly one body: a
call to ``executor.execute`` (the ONE :class:`GovernedToolExecutor`, possibly via
a handler-less :class:`~ne.runtime.toolset.ToolsetScopedExecutor` that only
delegates to it). This file invokes NO tool handler directly and imports NO
provider SDK — the C6 guardian source-scans it automatically.

Memory isolation (NE-034): :func:`run_isolated_agent` runs with an EMPTY message
history and takes NO session store, so a sub-agent/cron agent can never see or
write parent-session state. The only sanctioned parent→child channel is the
prompt itself (the caller renders ``SubAgentSpec.context`` into it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import (
    Agent,
    ModelRetry,
    PromptedOutput,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.governance import AuditSink
from ne.contracts.models import ModelGateway
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import SourceAuthorityDecision, SourceBoundToolResultRef
from ne.contracts.types import ApprovalTier, ChatMessage, Principal
from ne.events import new_audit_event
from ne.govern.errors import GuardrailEvaluationError, GuardrailViolation
from ne.govern.grounding import (
    PROFILE_NG2,
    GroundingCorpus,
    GroundingVerdict,
    canonical_numeral,
    check_grounding,
)
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_principal_scope, run_trace_scope
from ne.runtime.errors import (
    ActionValidationError,
    AgenticNoProgressError,
    AgentOutputContractError,
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    NeRuntimeError,
    RuntimeAuditError,
    ToolDeniedError,
    ToolExecutionError,
    ToolNotRegisteredError,
    ToolsetViolationError,
)
from ne.runtime.model import (
    PROSE_WRAPPED_TOOL_CALL_CORRECTION,
    PROTOCOL_RESIDUE_CALL_CORRECTION,
    GatewayModel,
    UnknownToolHook,
    is_malformed_tool_call_residue,
    is_prose_wrapped_tool_call,
    rejected_reply_shape,
    safe_error_location,
)
from ne.runtime.run_audit_receipts import AgenticPlanStepAnnouncement, agentic_plan_step_scope
from ne.runtime.run_runner import _NOOP_BOUNDARY, RunCancellationRequested, RunExecutionBoundary
from ne.runtime.source_results import (
    PROTECTED_REFERENCE_PREFIX,
    ClaimRetainingSourceGuard,
    MalformedProtectedReferenceError,
    ProtectedSourceResultGuard,
    ServedTextRetention,
    SourceAuthorityRetryableError,
    SourceAuthorityWithheldError,
    decode_protected_reference,
    protected_tool_evidence_scope,
)
from ne.runtime.workflow_execution_profile import WorkflowExecutionProfile
from ne.schema import AuditOutcome

# Governance/execution outcomes that become a BLOCKED tool result (already audited)
# rather than killing the run. RuntimeAuditError / AuditEmissionError are NOT here:
# an un-recordable call must propagate and stop the run. ToolsetViolationError is
# included: an out-of-set call raised INSIDE a tool body is an audited governance
# block that must not kill the loop (the model never sees out-of-set tools in its
# list, so this only guards the defensive path; the on_unknown_tool hook covers a
# model-emitted out-of-set NAME before pydantic-ai rejects it).
_BLOCKING_ERRORS = (
    ToolDeniedError,
    GuardrailViolation,
    GuardrailEvaluationError,
    ApprovalNotGrantedError,
    ApprovalIntegrityError,
    ToolNotRegisteredError,
    ToolExecutionError,
    ToolsetViolationError,
    # NE-ACTION-VALIDATE-BEFORE-GATE: a pre-gate typed-input refusal is an audited
    # governance block (tool.validate[denied]) — it becomes a BLOCKED: tool result
    # naming the offending FIELD so the model can correct and retry; it must not
    # kill the run.
    ActionValidationError,
    SourceAuthorityWithheldError,
    SourceAuthorityRetryableError,
)


class ToolExecutorLike(Protocol):
    """The governed executor surface a loop needs: specs to render + a governed execute.

    Both :class:`~ne.runtime.middleware.GovernedToolExecutor` and the handler-less
    :class:`~ne.runtime.toolset.ToolsetScopedExecutor` satisfy this — the loop never
    needs (and never gets) a handler.
    """

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]: ...

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str: ...


DeterministicWorkflowExecutor = Callable[[BaseModel], Awaitable[Any]]


#: The DISTINCT output-contract violations a reply can be sent back to correct,
#: each with its OWN allowance of corrective rounds.
#:
#: pydantic-ai charges every one of them to a SINGLE counter —
#: ``GraphAgentState.output_retries_used`` (``pydantic_ai/_agent_graph.py:143``),
#: debited by ``consume_output_retry`` (``:200``) from four call sites (``:1102``
#: a hook's ``ModelRetry``, ``:1212``/``:1237`` empty replies, ``:1315`` every
#: ``ToolRetryError``, which is what a schema rejection and an output validator's
#: ``ModelRetry`` both become). ``retries={'output': N}`` takes exactly one number
#: for all of them; the library's own docstring calls it "a global budget shared
#: across all output-validation retries in a run"
#: (``pydantic_ai/agent/__init__.py:370``). There is no per-validator budget to
#: ask for, so the partition is enforced HERE, by :class:`_OutputRetryLedger`.
#:
#: Sharing one fungible pool is what broke the deployed run: three ``answer-shape``
#: rejections spent all three slots, and the ``claim-contract`` violation that
#: arrived on the fourth reply was never sent back at all — the run published
#: ``Attempts: absent`` with ``residue_retries=0``, i.e. one recorded claim
#: rejection whose correction was never delivered. The failure could only be
#: repaired by a correction round-trip (the model had copied the source sentence
#: into ``claim_text`` and never carried it into ``final_text``), and the
#: round-trip had no budget left.
#:
#: The allowance spelling that means ONE ROUND PER RUNG OF THE LADDER, resolved
#: against the run's own ``max_tool_calls`` by :func:`output_retry_allowances`.
#:
#: It is a sentinel rather than a number because exactly one violation class
#: here is not a property of the single answer a run ends with: see
#: ``wrapped-call`` below. Negative so that no arithmetic on the raw table can
#: silently take it for an allowance — every consumer goes through the resolver.
_PER_TOOL_CALL: Final = -1

#: The allowances are MEASURED, not chosen:
_OUTPUT_RETRY_ALLOWANCES: Final = (
    # A malformed in-band tool call, demoted to text by parse-never-raises and
    # then taken for the final reply (`_ProtocolResidueCapability`). ONE: the
    # correction is static protocol text, so a model that refused it once will
    # refuse it again.
    ("protocol-residue", 1),
    # NE-WRAPPED-CALL-CLASS. A WELL-FORMED lone tool call that the model WRAPPED
    # IN PROSE (:func:`~ne.runtime.model.is_prose_wrapped_tool_call`). None of
    # ``_parse``'s three transports accepts that shape, so the call is demoted to
    # text, taken for the final reply, and rejected. ONE ROUND PER RUNG.
    #
    # WHY IT IS NOT AN ``answer-shape`` VIOLATION, which is the whole content of
    # this entry. Every other seam in this codebase ALREADY calls this a
    # transport mistake: :func:`~ne.runtime.model.sole_answer_object` refuses to
    # select it as an answer ("protocol residue, never a final answer"),
    # :func:`~ne.runtime.model.rejected_reply_shape` stamps ``sole=tool-call``,
    # and ``GatewayModel._render_retry_prompt`` hands it
    # ``wrapped_tool_call_retry_instruction`` rather than the answer restatement
    # (NE-WRAPPED-CALL, train 84). The LEDGER was the one seam still calling it
    # an answer, so the correction a run SENT and the budget a run SPENT
    # disagreed about which mistake had been made. The model did not attempt an
    # answer it shaped wrongly; it attempted a CALL with a bad transport, and it
    # is told exactly that.
    #
    # WHY IT IS SIZED TO THE LADDER. An answer-shape failure is a property of
    # the ONE final answer a run ends with, so a constant bounds it. A wrapped
    # call is a property of a CALL ATTEMPT, and a run makes as many attempts as
    # its ladder is long — so the demand GROWS WITH ``max_tool_calls`` and no
    # constant can bound it. That is precisely why funding it as ``answer-shape``
    # did not converge, measured twice on the deployed build: raising that
    # constant 3 -> 4 bought EXACTLY one more rung (6 -> 7 tool invocations,
    # 10 -> 12 ``model.served``, 45 s -> 2m50s) against a Customer Support ladder
    # that needs 14. Each increment buys about one rung; the ladder needs many.
    # One round per rung converges BY CONSTRUCTION: the allowance is a function
    # of the same number the ladder is, so a longer ladder cannot outrun it, and
    # there is no number left to raise next time.
    #
    # THE RATE IS ONE PER RUNG, WHICH IS ~2x THE MEASURED DEMAND. Both live runs
    # decompose exactly: 10 served = 6 calls + 3 wraps + 1 final answer, and
    # 12 served = 7 calls + 4 wraps + 1 final answer. The model therefore wrapped
    # 3 of 9 and 4 of 11 call attempts — p ~= 0.36, i.e. 0.56 wraps per
    # SUCCESSFUL call. One per rung is the CEILING above that rate, not the rate.
    # If a live run ever exhausts this class it does so having wrapped every rung
    # of its own ladder, which is a model that cannot use the transport at all —
    # a different finding, and one the failure still states (the rejection keeps
    # ``sole=tool-call``, see :func:`_output_contract_reason`).
    ("wrapped-call", _PER_TOOL_CALL),
    # The reply is not a valid `_StructuredAgenticCompletion` — wrong object,
    # wrong shape, or not JSON at all (pydantic-ai's own schema retry). FOUR,
    # and this is the one allowance above one: the correction here is NOT static
    # — pydantic-ai's own error list, a different list each time, plus the
    # static transport restatement (NE-RETRY-LEGIBLE) — so a model that refused
    # it once may still take the next one.
    #
    # It was THREE, fitted to the 2026-08-28 deployed failure and the two
    # successful runs beside it, which took EXACTLY three corrections before
    # producing a well-shaped answer. NE-SHAPE-ALLOWANCE-4 (train 85) then raised
    # it to FOUR on the 2026-09-11 S5 measurement, whose fourth rejection ended
    # the run ``AgentOutputContractError ... Schema rejected ... [json_invalid;
    # opens=prose fences=0 objects=1 chars=1284 sole=tool-call]``.
    #
    # THAT MEASUREMENT IS NO LONGER THIS CLASS'S, and saying so is the honest
    # half of NE-WRAPPED-CALL-CLASS: ``opens=prose ... objects=1 ...
    # sole=tool-call`` IS the wrapped-call shape, so that rejection — and every
    # sibling of it — now charges ``wrapped-call`` above. What survives here as
    # an INDEPENDENT basis is the 2026-08-28 three-correction measurement.
    #
    # FOUR is nevertheless KEPT rather than re-cut to three, for a reason that is
    # not inertia: the 2026-09-11 run record states the SHAPE of its FOURTH
    # rejection only, so the first three are not KNOWN to have been wrapped
    # calls, and cutting an allowance on an assumption about unrecorded
    # rejections is how a run dies holding the one correction it needed. This
    # slot is therefore one round of headroom over the measured three — and it is
    # the first slot to reclaim if the funding derivation is tight, on a
    # measurement that reads the shapes rather than on this note.
    ("answer-shape", 4),
    # Claims omitted, or a declared claim rejected
    # (`_install_claim_contract_validator`). ONE, RESERVED: this slot exists on
    # top of everything above it precisely so a run that spent its whole shape
    # allowance still gets to correct its claims.
    ("claim-contract", 1),
)

#: The violation classes, in the order they are enumerated above.
_CORRECTABLE_OUTPUT_VIOLATIONS: Final = tuple(name for name, _ in _OUTPUT_RETRY_ALLOWANCES)


def output_retry_allowances(max_tool_calls: int) -> tuple[tuple[str, int], ...]:
    """THIS run's per-class allowances, in the order enumerated above.

    The constant classes pass through unchanged; the one LADDER-SIZED class
    (:data:`_PER_TOOL_CALL`) resolves to ``max_tool_calls`` — one corrective
    round per rung.

    It is a FUNCTION and not a constant because that is the whole content of
    NE-WRAPPED-CALL-CLASS: the demand for a wrapped-call correction is a
    property of how much WORK a run may do, not of the one answer it ends with,
    and no constant can bound a quantity that grows with the ladder. Callers
    outside this module (the API's execution caps, the worker's request
    ceiling) read the allowances THROUGH here, so there is exactly one place
    that knows which class is sized to what.
    """
    return tuple(
        (name, max_tool_calls if allowance == _PER_TOOL_CALL else allowance)
        for name, allowance in _OUTPUT_RETRY_ALLOWANCES
    )


def output_retries(max_tool_calls: int) -> int:
    """The pool handed to pydantic-ai: the SUM of THIS run's allowances.

    Summing them is what keeps the library from cutting a class off before
    :class:`_OutputRetryLedger` does. Each attempt spends a model request, so
    :func:`model_request_limit` reserves exactly this much headroom — without it
    a contract violation would surface as `budget-exhausted`/`requests`, naming
    a ceiling the operator never set. A violation that outlives its OWN
    allowance FAILS, named, via
    :class:`~ne.runtime.errors.AgentOutputContractError`.
    """
    return sum(allowance for _, allowance in output_retry_allowances(max_tool_calls))


@dataclass
class _OutputRetryLedger:
    """pydantic-ai's one output-retry counter, partitioned by violation class.

    The library cannot reserve a slot for a particular validator, so this does:
    every correction this loop sends is charged to its OWN class here first, and
    a class that is out of allowance does not borrow another's — it ends the run.
    Because the pool handed to pydantic-ai is the SUM of the allowances, the
    ledger is always the binding constraint and ``claim-contract``'s slot is
    still there however many times the shape correction fired.

    Ending the run raises :class:`UnexpectedModelBehavior` — the SAME exception
    pydantic-ai raises when its own budget runs out — so the single
    ``except UnexpectedModelBehavior`` handler in :func:`run_model_planned_agent`
    still turns it into an :class:`~ne.runtime.errors.AgentOutputContractError`
    carrying the server-authored ``safe_reason``. There is no second failure
    shape to read, and none of these classes is ever laundered into an answer.
    """

    #: THIS run's resolved allowances, from :func:`output_retry_allowances`.
    #: Held rather than read from the module table because one class is sized to
    #: the run's own ladder, so "the allowance" is not a module-level fact.
    allowances: Mapping[str, int]
    spent: dict[str, int] = field(default_factory=dict)

    def spend(self, violation: str, *, cause: BaseException | None = None) -> None:
        """Charge one corrective round to ``violation``, or end the run.

        ``cause`` is CHAINED onto the raised error rather than dropped, because
        :func:`_output_contract_reason` reads the rejection off that chain: an
        exhausted ``answer-shape`` allowance must still publish the schema
        location and Pydantic error code that rejected the reply, exactly as an
        exhausted library budget did.
        """
        allowance = self.allowances[violation]
        used = self.spent.get(violation, 0)
        if used >= allowance:
            # Same sentence shape pydantic-ai uses, so `_output_contract_reason`
            # reads one exhausted-budget failure however the budget ran out.
            raise UnexpectedModelBehavior(
                f"Exceeded maximum output retries ({allowance})"
            ) from cause
        self.spent[violation] = used + 1


def model_request_limit(max_tool_calls: int) -> int:
    """The model-request ceiling a bounded agentic run actually enforces.

    One final-answer request follows the last tool call.  This run-specific
    request cap is intentionally derived from the stricter distinct tool cap,
    rather than silently inheriting ``RuntimeSettings.max_requests=8`` and making
    a larger ``max_tool_calls`` constant fictional.

    It is a FUNCTION rather than an inline ``+ 1`` because the console has to be
    able to print the number this run enforces, and a second copy of the rule in
    another language is how a displayed cap and an enforced cap come to disagree.

    :func:`output_retries` — the SUM of THIS run's per-class allowances — is
    reserved on top. A rejected final answer costs one more model request, and
    without that headroom a claim-contract violation would surface as
    ``budget-exhausted``/``requests`` — a terminal naming the wrong cause, since
    no ceiling the operator set was actually reached.

    NE-WRAPPED-CALL-CLASS made that reserve a FUNCTION of ``max_tool_calls``
    rather than the constant 6: the ``wrapped-call`` class is sized to the
    ladder, so the worst case this function pins is now ``2 * max_tool_calls +
    7`` rather than ``max_tool_calls + 7``. Every ``max_input_tokens`` in
    ``deploy/agents/registry.toml`` is pinned by equality to the worst case this
    function returns, so moving it REQUIRES re-deriving them; an unfunded retry
    is strictly worse than the loud failure it replaces, because the run dies
    ``budget_exhausted`` BEFORE the correction is ever delivered.
    """
    return max_tool_calls + 1 + output_retries(max_tool_calls)


@dataclass(frozen=True, slots=True)
class BoundedAgenticAdapter:
    """Typed caps and instructions for the existing model-planned runner.

    This adapter does not own another execution path.  It delegates verbatim to
    :func:`run_model_planned_agent`, which keeps gateway-only inference and the
    governed executor chokepoint.  Keeping the caps in one immutable value also
    prevents an Agent binding from advertising "bounded" while passing unrelated
    call-site constants at execution time.
    """

    instructions: str
    max_tool_calls: int
    total_tokens_limit: int
    wall_clock_seconds: float
    max_parked_writes: int

    #: ``instructions`` MAY be empty (NE-RUN-ZERO-PIN): the worker rebinds it to
    #: the run's activated Skill index, and ``ne.skills.active_set.render_index``
    #: renders a zero-pin activation as the EMPTY STRING by design (never a
    #: dangling header). The old non-empty guard here predated that and turned
    #: every zero-pin agentic run into a pre-flight ``ValueError`` at the
    #: worker's ``replace(adapter, instructions=activation.index_text)`` —
    #: after the store had already accepted the empty receipt. GatewayModel's
    #: instruction mapping handles the empty string (test_agent_session_
    #: instructions), so emptiness needs no sentinel text substituted for it.
    def __post_init__(self) -> None:
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if self.total_tokens_limit < 1:
            raise ValueError("total_tokens_limit must be positive")
        if self.wall_clock_seconds <= 0:
            raise ValueError("wall_clock_seconds must be positive")
        if self.max_parked_writes < 0:
            raise ValueError("max_parked_writes must not be negative")

    async def run(
        self,
        *,
        task: str,
        principal: Principal,
        executor: ToolExecutorLike,
        gateway: ModelGateway,
        settings: RuntimeSettings,
        sink: AuditSink,
        source_guard: ProtectedSourceResultGuard | None = None,
        message_history: Sequence[ModelMessage] = (),
        boundary: RunExecutionBoundary = _NOOP_BOUNDARY,
    ) -> ModelPlannedRun:
        return await run_model_planned_agent(
            task=task,
            instructions=self.instructions,
            principal=principal,
            executor=executor,
            gateway=gateway,
            settings=settings,
            sink=sink,
            max_tool_calls=self.max_tool_calls,
            total_tokens_limit=self.total_tokens_limit,
            wall_clock_seconds=self.wall_clock_seconds,
            max_parked_writes=self.max_parked_writes,
            boundary=boundary,
            source_guard=source_guard,
            message_history=message_history,
        )


async def run_deterministic_profile(
    *,
    profile: WorkflowExecutionProfile,
    inputs: Mapping[str, Any],
    executor: DeterministicWorkflowExecutor,
) -> Any:
    """Validate typed inputs, then call the injected real deterministic executor.

    No exception is caught and no agentic adapter is accepted here: a deterministic
    validation or execution failure remains a deterministic failure instead of being
    silently rerouted and relabelled as model-planned work.
    """
    typed_inputs = profile.validate_input(inputs)
    return await executor(typed_inputs)


@dataclass(frozen=True)
class ModelPlannedStep:
    """One model-stated governed tool step, including what that call served."""

    step: int
    tool: str
    intent: str
    outcome: str
    served_row_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    principal_id: str = ""


@dataclass(frozen=True)
class ModelPlannedClaim:
    """One validated model-local claim over exact terminal UTF-8 bytes and evidence."""

    claim_key: str
    span_start_byte: int
    span_end_byte: int
    claim_content_hash: str
    served_ref: str


@dataclass(frozen=True)
class ModelPlannedRun:
    """Result of a bounded model-planned loop over one scoped executor."""

    outcome: Literal["complete", "context-exhausted", "budget-exhausted"]
    output: str = ""
    steps: tuple[ModelPlannedStep, ...] = ()
    claims: tuple[ModelPlannedClaim, ...] = ()
    served_row_ids: tuple[str, ...] = ()
    requests_used: int | None = None
    tool_calls_used: int | None = None
    tokens_used: int | None = None
    exhausted_cap: str | None = None


_LEGACY_SERVED_REF_RE = re.compile(r"urn:ne:[^\s|,;]+")
_PROTECTED_SERVED_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_-])ne-source-ref:v1:[A-Za-z0-9_-]+={0,2}(?![A-Za-z0-9_=])"
)
#: A faithful mirror of ``ne.api.workflows._CHUNK_HEADER_RE`` — the strict
#: envelope the API layer already parses ``read.knowledge.search``'s rendering
#: with. ``ne.api.tools._result_header``
#: (packages/ne-api/src/ne/api/tools.py:106-141) emits ONE grammar for every
#: governed search: ``[showing M of N retrieved chunks]`` with an optional
#: ``; K withheld — <fixed reason>`` clause, where ``N = max(total, shown +
#: withheld)``. Only the two counts this runner needs are captured.
_CHUNK_RESULT_HEADER_RE = re.compile(
    r"^\[showing (\d+) of \d+ retrieved chunks(?:; (\d+) withheld[^\]]*)?\]$"
)


def _disclosed_nothing(output: str) -> bool:
    """This read matched content and disclosed none of it (a source-health blackout).

    The SAME predicate ``ne.api.workflows.RetrievedText.source_health_blackout``
    publishes for this exact purpose — ``shown == 0 and withheld > 0``, described
    there as "the distinction the no-progress rule's caller needs". This is that
    caller, so it makes the same distinction rather than a second one.

    🔴 WHY THE RUNNER READS A RESULT GRAMMAR AT ALL, which is otherwise none of
    its business. The digest half of the no-progress predicate (see
    :data:`_NO_PROGRESS_TOOL_CALLS` and the use site) stands in for a ref ONLY
    for a result that carries no ref. ``read.knowledge.search`` is a ref-BEARING
    format that goes ref-free exactly here — every match withheld, so no chunk
    line and no protected ref — while ``N`` above stays QUERY-DEPENDENT. A model
    told "0 of 7 withheld" broadens, gets "0 of 11", broadens again, gets "0 of
    4": three byte-distinct ref-free outputs, three digest votes for progress, a
    streak that never opens, and a run that burns ``max_tool_calls`` and
    terminalises ``budget-exhausted``/``tool-calls`` — the one terminal this stop
    must never borrow (see :func:`run_model_planned_agent`). Measured on the
    shipped rendering: twelve broadenings, twelve calls, that terminal. The
    withholding clause exists BECAUSE a model told "0 of 0" broadens three times
    against a blacked-out corpus (``ne.api.tools:118-122`` records that live),
    and that docstring asserts the loop is "stopped by the no-progress rule".
    This predicate is what makes that sentence true.

    WHY NOT A TOOL-NAME ALLOWLIST. "``read.knowledge.search`` is ref-bearing" is
    not a fact: a real chunk id is ``{document_id}:{index:04d}``
    (``ne.brain.ingest.chunker:146``), not a ``urn:ne:`` row identity, so a search
    rendered without a knowledge-evidence author carries no ref even when it
    discloses real passages — and muting the digest for the whole TOOL would make
    those productive searches look barren, which is the false "nothing was
    retrieved" the digest half was added to prevent. The disclosure is a fact
    about the RESULT, so the result is what is read.

    Scoped as narrowly as the defect and no wider. A partial disclosure
    (``showing 2 of 9 … 7 withheld``) served real passages and keeps its digest
    vote; a genuinely empty corpus renders ``[showing 0 of 0 retrieved chunks]``,
    byte-identical however the query is worded, so the digest already stops its
    repeats unaided. Only the FIRST line is read (as the API-side parser does),
    so chunk text that happens to contain the header shape cannot reach this;
    and a tool that fabricated the header could only make the rule STRICTER for
    itself, never looser, which is the safe direction for a misread.
    """
    header = _CHUNK_RESULT_HEADER_RE.match(output.split("\n", 1)[0].strip())
    if header is None:
        return False
    withheld = header.group(2)
    return header.group(1) == "0" and withheld is not None and int(withheld) > 0


def _progress_digest_material(output: str) -> str:
    """What the digest half of the no-progress rule hashes, for THIS result.

    The digest stands in for the ref a ref-free result never had (see the use
    site), so it has to hash what the result DISCLOSED and nothing else. For an
    arbitrary tool that is the whole output. For the ONE grammar this runner can
    recognise -- ``read.knowledge.search``'s ``[showing M of N retrieved
    chunks]`` header followed by one ``<chunk_id>: <text>`` line per hit -- the
    whole output also carries two things that are NOT disclosure:

    * the header line, whose ``N`` is the pre-rerank candidate count, and
    * the ORDER of the body, which is the reranker's, i.e. the query's.

    Both are dropped: the first line goes when it matches the header grammar, and
    what remains is sorted. Sorted, not deduplicated -- a repeated line is still
    a line the result served twice.

    This is what makes the rule's published invariant ("rewording the query
    cannot evade it") true for a search that carries no ref at
    all. A SOURCE-FREE result is exactly that: ``ne.api.tools`` mints a protected
    ref only for a SOURCE_BEARING classification, so a result whose chunks are
    all server-marked source-free is returned as its raw ``<chunk_id>: <text>``
    rendering, and a real chunk id is ``{document_id}:{index:04d}``
    (``ne.brain.ingest.chunker:146``) with ``document_id`` a hex digest -- which
    matches neither ref pattern. Re-serving those same chunks under a new wording
    reorders them and changes no evidence, and before this the reordering alone
    made every call a fresh digest and a fresh vote for progress.

    NEW chunks still change the sorted body, so a search that actually retrieves
    something the run had not been served keeps its full vote. Only the header
    and the ordering lose theirs.
    """
    lines = output.split("\n")
    if _CHUNK_RESULT_HEADER_RE.match(lines[0].strip()) is None:
        return output
    return "\n".join(sorted(lines[1:]))


_EXPLICIT_AGENTIC_WRITE_TOOLS = frozenset({"draft.email.create"})
_STEP_INTENT_ARG = "step_intent"
_EVIDENCE_REFS_ARG = "evidence_refs"


class _AgenticClaimDeclaration(BaseModel):
    """A model-declared claim — TOLERATED IN TRANSPORT, CONSULTED FOR NOTHING.

    NE-SPAN-BIND: the server now selects and binds every claim itself, in
    :func:`_server_bound_claims`, from the two texts only it holds — the final
    answer and the authorized chunk text each delivery actually served (retained
    per run by :class:`~ne.runtime.source_results.ServedTextRetention`). The
    model authors NO part of a claim any more: not the span, not the source, not
    a key, and (as before) no offsets and no hashes.

    Why the field still parses instead of being deleted: a continued run's
    ``message_history`` carries the OLD prompt's corrections and exemplars, all
    of which teach the model to declare claims. Rejecting a well-shaped vestigial
    declaration as an undeclared key would burn ``answer-shape`` retries on a
    habit the server no longer needs corrected — so the declaration is accepted,
    schema-checked (``extra="forbid"`` still refuses every server-owned field:
    offsets, hashes, references), and then ignored entirely. This is the
    ``source_id`` precedent finishing the job: NE-CLAIM-BIND already ignored the
    model's ``source_id`` wherever the server could derive it; now the server can
    derive everything, so everything model-declared is ignored.

    The history of this schema is the measurement that forced the change. It
    began as offsets plus a content hash of the very object being emitted
    (NE-CLAIM-DERIVE removed those — no model can hash its own future bytes),
    then a full 2,773-char reference string the delivery layer exists to
    withhold (NE-REF-VOCAB replaced it with the served ``reference_id``), then a
    transcription-fragile ``source_id`` (NE-CLAIM-BIND derived it wherever only
    one source was served). The last model-authored field, ``claim_text``, had
    to be one string satisfying two gates at once — a span of the answer AND
    verbatim in the cited chunk — and measured over 21 real claims from 6 real
    runs, ZERO satisfied both (19 were answer spans absent from the source, 2
    were source spans absent from the answer). A contract the governed thing
    cannot satisfy is not a control; deriving it server-side is.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_key: str = Field(min_length=1, max_length=512)
    claim_text: str = Field(min_length=1, max_length=100_000)
    source_id: str | None = Field(default=None, min_length=1, max_length=512)


class _StructuredAgenticCompletion(BaseModel):
    """Internal final-answer protocol.

    ``final_text`` is the model's whole authored surface. ``claims`` is a
    tolerated vestige (see :class:`_AgenticClaimDeclaration`): parsed so a model
    still following an older correction is not shape-rejected, consulted never.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    final_text: str = Field(max_length=1_000_000)
    claims: tuple[_AgenticClaimDeclaration, ...] = Field(default=(), max_length=128)


_STRUCTURED_AGENTIC_COMPLETION_TAIL = (
    "FINAL RESPONSE TRANSPORT: return exactly one JSON object and "
    "nothing else, with this root key: "
    '{"final_text":"<answer>"}. '
    'Never return the schema, "$defs", "type", markdown fences, or text outside the JSON object. '
    'This object is NOT a tool call: a reply whose root key is "tool_call" calls a tool, '
    'a reply whose root key is "final_text" is the final answer, and there is no third option — '
    "a plain-text reply is not a valid final answer here and will be rejected. "
    "QUOTE YOUR SOURCES VERBATIM. A tool result that carries source material begins with a "
    "line reading [source-id: <id>]. When your answer rests on such a result, final_text MUST "
    "CONTAIN at least one sentence copied character for character out of that result's chunk "
    'text — quote the source inside your answer, like this: The source states: "<the source '
    'sentence, copied exactly>" — and put your own analysis around the quotation. The server '
    "finds the quoted words itself and binds your answer to the source that served them: you "
    "declare no claims, no ids, no offsets and no hashes. An answer written entirely in your "
    "own words cannot be published over sources, because nothing in it is what the source "
    "actually says; an answer whose quotation is too short (under five consecutive words) "
    "cannot be bound either, so quote a full sentence. Never invent a quotation: if no "
    "sentence in the source states a point you want to make, leave that point out rather than "
    "quote something that does not say it. "
    # NE-ROWS-ARE-NOT-QUOTABLE. The sentences above are written for PROSE, and a
    # verified-answer result is a PIPE TABLE. Following them literally on rows
    # loses the run, in one of two ways, and both were measured against the real
    # selector and the real verifier rather than reasoned about:
    #
    #   THE SPAN IS STOLEN. `_server_bound_claims` binds the LONGEST verbatim run
    #   common to the answer and a served chunk. An answer that quotes a source
    #   sentence correctly AND also pastes the raw row is bound to the PASTED ROW
    #   -- it is longer -- and `verify_claim` then refuses it. The quotation was
    #   right; it simply was not the span the server picked.
    #
    #   OR NOTHING BINDS. `_MIN_BOUND_SPAN_WORDS` counts WHITESPACE-separated
    #   tokens, so on a ` | `-delimited table any 5-token run necessarily crosses
    #   cell boundaries -- while the verifier's corpus makes each CELL its own
    #   phrase (`GroundingCorpus.from_answers` adds one phrase per `row[i]`). So a
    #   quote long enough to bind is, by construction, phrase-recombination. The
    #   table's own structure is not quotable material at all.
    #
    # What IS quotable in a rows result is a PROSE field (a note's `text`, a
    # declaration's `scope_description`): one cell, one phrase, many words. So the
    # rule is to quote prose and to keep the table out of the answer's surface --
    # the row VALUES still get stated, as ordinary sentences, which no longer have
    # to bind anything because the model authors no claim text any more.
    #
    # A run whose rows carry no prose field simply binds nothing from that source
    # and is unharmed, PROVIDED the answer quotes some other served prose: the
    # binder skips a source with no qualifying run and only hard-fails when NO
    # source bound at all. Measured end-to-end on the S3 shape (a policy chunk +
    # an all-atomic disputes table): the chunk binds and verifies, the table binds
    # nothing, the run completes.
    #
    # ONE ROW PER SENTENCE and the connective survive from the pre-NE-SPAN-BIND
    # guidance unchanged in substance -- they are what keeps a pasted fragment out
    # of the answer, which is now what protects the binding rather than what
    # satisfied a model-authored claim.
    "When a result is ROWS, the quotable material is a row's PROSE field, never the table "
    "itself: never paste a row, a header line, or cells side by side. The server binds the "
    "LONGEST verbatim run it finds, so a pasted row is bound INSTEAD of your quotation and "
    "the answer fails on it. Put row values in your own sentences - ONE ROW PER SENTENCE, "
    "joining the row's identifier, the field name and its value with an ordinary word, as in "
    '"id-7 has amount_cents 500". Report only values that appear in a result: never add cells '
    "up. Asked for a total, give the rows that would contribute, one sentence each, and say "
    "plainly that the records do not carry it. "
    # NE-ANSWER-THE-QUESTION. Measured live (2026-08-27): a "which records
    # satisfy this rule" question came back with the governing rule quoted
    # correctly, bound, verified — and not one record named. The guidance above
    # taught the SAFE way to state a row and the refusal path for totals, but
    # nothing said an enumeration question is UNANSWERED until the qualifying
    # records are named, so the prose quotation (the easiest thing to bind) was
    # served as the whole answer. This rule is FORM only: it tells the model to
    # finish the job with the row sentences the two rules above already govern,
    # and its absence branch REQUIRES the same honest refusal the total rule
    # teaches — it never invites a value the rows do not carry.
    "Asked WHICH records meet a rule, quoting the rule is not the answer: name each "
    "qualifying record by its served identifier, one sentence per record stating the served "
    "values the rule turns on, then give your conclusion. If the served rows lack a field "
    "the rule needs, name the records and say that field is absent instead of concluding. "
    "If no tool result carried a [source-id: ...] line, "
    "just answer."
)


#: The final-answer clause this runner's in-band tool catalog carries, REPLACING
#: :data:`ne.runtime.model._PLAIN_TEXT_FINAL_ANSWER`.
#:
#: Why this exists (the NE-OUTPUT-CONTRACT defect): ``GatewayModel`` closes its
#: in-band D3 catalog with "Otherwise, respond with your final answer as plain
#: text (no JSON)." That sentence is correct for every ``output_type=str`` loop
#: and FLATLY CONTRADICTS this runner, whose ``output_type`` is a prompted
#: ``_StructuredAgenticCompletion`` — the model was told, in one merged system
#: message, both to answer in plain text with no JSON and to answer with exactly
#: one JSON object. Whichever way it guessed, the other instruction called it
#: wrong; when it guessed plain text, pydantic-ai's object processor got prose,
#: ``validate_json`` raised, the ``ToolRetryError`` consumed the whole output-retry
#: budget, and the run died on ``UnexpectedModelBehavior: Exceeded maximum output
#: retries (1)`` with nothing to say for itself. Asserting priority in the tail
#: (the previous "(highest priority)" parenthetical) papered over the conflict
#: instead of removing it. Removing the contradicting sentence is the fix; it
#: does NOT widen what counts as a valid answer — the schema, ``extra="forbid"``,
#: and :func:`_server_bound_claims` are all untouched.
_STRUCTURED_AGENTIC_FINAL_ANSWER = (
    "Otherwise, respond with the FINAL RESPONSE TRANSPORT JSON object described below. "
    "A prose reply is not a valid final answer here and will be rejected."
)


#: NE-RETRY-LEGIBLE. Appended by ``GatewayModel._render_retry_prompt`` to every
#: schema-rejection retry of THIS loop's final answer, restating the one action
#: that repairs the reply. Written against the two rejection shapes measured on
#: train-49 (worker log, four S1 no-answer runs): a prose final answer
#: (``opens=prose``), and an object whose strings carry literal line breaks
#: (``opens=object objects=0`` — recovered without a retry by
#: NE-ANSWER-CTRLCHAR, named here anyway so a model that produces some OTHER
#: control character is told the rule rather than left to a parser error naming
#: a line number). "Format, not content" leads the sentence because the measured
#: failures carried CORRECT answers — the model must resend, not rethink.
#: ~470 chars = 160 tokens, SMALLER than the raw error-details dump it replaces,
#: which echoed the whole rejected reply (2,655 chars measured) back as input.
_STRUCTURED_ANSWER_RETRY_CORRECTION = (
    "Your reply was rejected for its FORMAT, not its content. Send the same answer "
    "again as ONE JSON object with exactly the two root keys "
    '{"final_text":"<answer>","claims":[...]} — the first character of your reply '
    "must be { and the last must be }, with no prose, no markdown fence and no "
    "schema text outside the object. Inside every JSON string write a line break "
    "as the two characters \\n; a literal line break inside a string is invalid "
    "JSON and will be rejected again."
)


#: NE-WRAPPED-CALL. This loop's correction for a prose-wrapped tool call: the
#: shared call-transport text, then the sentence that is TRUE HERE. Exactly the
#: shape :data:`_RESIDUE_RETRY` already has for the MALFORMED lone call — same
#: seam, same split between the shared call correction and the loop-specific
#: tail — because this is the same mistake made with a well-formed object.
#:
#: The tail matters: without it a model that had finished its work and merely
#: quoted a call would be told only how to call tools. The session path answers
#: in plain text; this loop rejects a plain-text final answer, so the escape
#: hatch must point at the transport, not at prose.
_WRAPPED_CALL_RETRY = (
    f"{PROSE_WRAPPED_TOOL_CALL_CORRECTION} If you did NOT intend to call a tool, "
    "reply with the FINAL RESPONSE TRANSPORT JSON object described in your "
    "instructions — a plain-text reply is not a valid final answer here."
)


class _StructuredAgenticGatewayModel(GatewayModel):
    """Prepare and advertise prompted output for this structured runner only.

    ``GatewayModel`` predates prompted structured output and intentionally does not
    call pydantic-ai's request preparation hook.  This bounded subclass does so,
    then repeats the generated schema instruction after the generic tool catalog
    and ends with a short literal root-shape reminder.  The latter addresses small
    local models that otherwise echo the generated schema itself, while validation
    still remains wholly owned by pydantic-ai/Pydantic.  ``_assemble`` is also used
    by context-budget estimation, keeping the estimated and emitted bytes identical.
    """

    @property
    def final_answer_instruction(self) -> str:
        """This runner's answer transport is the prompted JSON object, not prose.

        See :data:`_STRUCTURED_AGENTIC_FINAL_ANSWER`.
        """
        return _STRUCTURED_AGENTIC_FINAL_ANSWER

    @property
    def final_answer_retry_instruction(self) -> str:
        """A rejected answer's retry restates THIS loop's transport.

        See :data:`_STRUCTURED_ANSWER_RETRY_CORRECTION` (NE-RETRY-LEGIBLE).
        """
        return _STRUCTURED_ANSWER_RETRY_CORRECTION

    @property
    def wrapped_tool_call_retry_instruction(self) -> str:
        """A rejected PROSE-WRAPPED TOOL CALL's retry restates the CALL transport.

        NE-WRAPPED-CALL — the S5 governed-send failure. The base restatement above
        is written for a model that tried to ANSWER; handing it to a model that
        tried to CALL A TOOL tells it to abandon the call, which is how the live
        ladder stalled before ``draft.email.create``. See
        :data:`_WRAPPED_CALL_RETRY`.
        """
        return _WRAPPED_CALL_RETRY

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        prepared_settings, prepared_parameters = self.prepare_request(
            model_settings, model_request_parameters
        )
        return await super().request(messages, prepared_settings, prepared_parameters)

    def _assemble(
        self,
        messages: Sequence[ModelMessage],
        model_request_parameters: ModelRequestParameters,
    ) -> tuple[ChatMessage, ...]:
        chat = super()._assemble(messages, model_request_parameters)
        output_instructions = model_request_parameters.prompted_output_instructions
        if not output_instructions:
            raise RuntimeError("structured agentic output schema instructions are unavailable")
        structured_tail = f"{output_instructions}\n\n{_STRUCTURED_AGENTIC_COMPLETION_TAIL}"
        if chat and chat[0].role == "system":
            head = chat[0].model_copy(update={"content": f"{chat[0].content}\n\n{structured_tail}"})
            return (head, *chat[1:])
        return (ChatMessage(role="system", content=structured_tail), *chat)


def _is_agentic_write_tool(tool: str) -> bool:
    """Classify the closed write surface the bounded agent may invoke.

    The legacy runtime namespace remains write-prefixed, while standard governed
    actions use exact product names.  Only the admitted email-draft action is added
    here: approval tier alone must never silently turn an unrelated tool into a
    model-planned write.
    """
    return tool.startswith("write.") or tool in _EXPLICIT_AGENTIC_WRITE_TOOLS


#: NE-LOOP-NOPROGRESS. How many CONSECUTIVE calls to the SAME Tool may add no
#: new server-observed evidence before the run stops and says so.
#:
#: WHY THIS IS NOT A SECOND, SMALLER CAP. A cap bounds how much work a run may
#: do and fires on work that is happening. This fires on work that has STOPPED
#: happening: a call that adds no ref to ``_RecordedExecutor.served`` produced
#: nothing the run can ground an answer on, and three of them in a row to one
#: Tool is the loop re-asking a question that has already been answered "nothing"
#: three times. Each of those re-asks costs a full model round-trip, which is why
#: an empty retrieval layer dominates a turn's latency long before any cap bites.
#:
#: WHY IT CANNOT CLIP MULTI-HOP WORK, which is the property that matters most.
#: The reset condition is EVIDENCE, not similarity of the arguments: any call
#: that serves a ref the run has not already seen sets the streak back to zero.
#: A multi-hop question is a chain of reads each of which feeds the next, so
#: every hop that actually finds something clears the counter. A run only reaches
#: this limit by making three consecutive reads that found nothing new — and
#: three reads that found nothing new have, by construction, gathered no evidence
#: at all. Stopping there caps no useful work; it declines to spend three more
#: model calls confirming an answer the server already has.
#:
#: WHY EVIDENCE AND NOT ARGUMENT TEXT. A predicate keyed on the query string is
#: evaded by rewording, and the model rewords. The served set is server-observed
#: and cannot be talked around.
#:
#: 🔴 WHY "NEW EVIDENCE" IS NOT "A NEW `urn:` REF", corrected after the rule killed
#: a run that was working. `_served_refs` matches only `urn:ne:…` and
#: `ne-source-ref:v1:…`, so a SUCCESSFUL read whose result format carries neither
#: was indistinguishable from one that found nothing. `read.external.crm.contact`
#: and `read.external.crm.account` are exactly that shape -- their handler returns
#: `{"name": …, "record_id": "003AX…", "email": …, "found": true}`, identifiers with
#: no ref anywhere -- and both are appended to the SAME executor tuple this runner
#: is built over (`ne.api.platform`). They are Tier-0 reads, so the write exemption
#: below did not save them either. Three consecutive lookups resolving three
#: DIFFERENT customers stopped the run and published "Nothing was retrieved to
#: ground an answer" -- a false statement, on the one surface whose entire purpose
#: is to let a reader tell "no evidence exists" from "the system broke". That is
#: the S5 outreach shape and the B1 flagship fans out over several customers, so
#: it is an ordinary plan and not a corner case.
#:
#: The predicate is therefore "this call produced something this run had not
#: already seen from this Tool": a new ref, OR output whose digest is new. Both
#: halves are server-observed. A tool that resolves three different records clears
#: the streak on every call; a model re-asking one barren question gets the
#: identical `"(no rows)"` back and the repeats still accumulate.
#:
#: 🔴 DECLARED RESIDUAL, because the digest half IS weaker than the ref half and
#: saying so is the point. A Tool that echoes its arguments into an otherwise
#: barren result gives a different digest for each reworded query, so a rewording
#: model could evade the streak against such a Tool -- the exact evasion the
#: paragraph above rules out for arguments. It is bounded by `max_tool_calls`,
#: which is where this behaviour sat before this rule existed at all, so the
#: worst case is a return to the prior cost. That is strictly better than
#: publishing a false statement about a run that was working, and the trade is
#: recorded here rather than left for the next reader to infer.
#:
#: COST OF THE CORRECTION: one extra round-trip on the motivating case. A first
#: barren read genuinely IS new information ("this query has no rows"), so it no
#: longer opens the streak; the run now stops on the fourth identical call rather
#: than the third. Deliberate -- the counter measures REPEATS, which is what
#: "no progress" actually means.
_NO_PROGRESS_TOOL_CALLS = 3


def _served_refs(output: str) -> tuple[str, ...]:
    """Extract only legacy row identities and valid server-minted protected refs."""
    refs = set(_LEGACY_SERVED_REF_RE.findall(output))
    for match in _PROTECTED_SERVED_REF_RE.finditer(output):
        candidate = match.group(0)
        try:
            reference = decode_protected_reference(candidate)
        except MalformedProtectedReferenceError:
            continue
        if reference is not None:
            refs.add(candidate)
    return tuple(sorted(refs))


def _protected_tool_evidence_refs(served: set[str]) -> tuple[str, ...]:
    """Select canonical protected Tool refs from the server-observed served set."""

    protected: list[str] = []
    for candidate in sorted(served):
        try:
            reference = decode_protected_reference(candidate)
        except MalformedProtectedReferenceError:
            continue
        if isinstance(reference, SourceBoundToolResultRef):
            protected.append(candidate)
    return tuple(protected)


#: The static rejection sentence :func:`_server_bound_claims` raises. It is sent
#: back to the model as corrective feedback AND published verbatim inside the
#: run's ``safe_reason`` when the bounded retry is exhausted, so it is kept
#: short enough that the assembled reason still fits
#: :data:`_MAX_SAFE_REASON_CHARS` (pinned by
#: ``test_every_claim_rejection_reason_survives_the_projection_cap``). Coaching
#: on how to FIX the violation belongs in :data:`_CLAIM_CORRECTION_BODIES`, not
#: here — this half has to stay publishable.
#:
#: NE-SPAN-BIND collapsed the old eight-reason vocabulary to this one sentence,
#: and that is the point of the change, not a loss of diagnosis: every field a
#: model could get wrong (a mistyped id, a re-rendered quotation, a duplicate
#: key, a claim over nothing served) is now server-derived, so the only way a
#: source-bearing answer can still fail its claim contract is the only failure
#: that was ever REAL — none of the answer's own words are verbatim in anything
#: this run served. That is the same conclusion the downstream verifier reaches
#: today as ``claim is not grounded by exact evidence``, reached BEFORE
#: publication, while the model can still fix it.
_CLAIM_NO_COMMON_SPAN = "no span of the final answer is verbatim in any source this run served"

#: The short code each rejection is published under when the reason names the
#: SEQUENCE of attempts rather than just the last one. Codes, not sentences: the
#: sequence has to fit beside the sentence inside :data:`_MAX_SAFE_REASON_CHARS`.
_CLAIM_REJECTION_CODES: Final = {
    _CLAIM_NO_COMMON_SPAN: "no-span",
}

#: Every static reason the claim contract can reject a reply with. Exists so the
#: cap test can enumerate them instead of trusting that a new one was measured.
_CLAIM_REJECTION_REASONS: Final = (_CLAIM_NO_COMMON_SPAN,)


def _claimable_sources(served_refs: set[str]) -> tuple[dict[str, str], frozenset[str]]:
    """Map each served reference's ``reference_id`` to the reference itself.

    Built fresh per validation from ``recorded.served`` — the references THIS run
    actually observed on its own successful reads. That is what keeps the control
    intact while the vocabulary changes: an id this run never served is simply not
    in the table, so a claim naming it fails exactly as a claim naming an unserved
    reference used to.

    Returns the resolvable table AND the ids that are ambiguous. A duplicate
    ``reference_id`` is REMOVED from the table and reported separately, so
    :func:`_server_bound_claims` never binds a span "verbatim in the source this
    id names" when the id names two different sources — the same
    silent-wrong-answer that :class:`ServedTextRetention` poisons its own entry
    for on drifted text. Absent from the table alone would have said "never
    served", which is a different and false statement about a source that WAS
    served.
    """
    table: dict[str, str] = {}
    ambiguous: set[str] = set()
    for candidate in sorted(served_refs):
        try:
            reference = decode_protected_reference(candidate)
        except MalformedProtectedReferenceError:
            # Unreachable from model input: `_served_refs` only admits references
            # that already decoded. A future producer that widens `served` cannot
            # turn this into a crash on the answer path.
            continue
        if reference is None:
            continue  # a legacy `urn:ne:` row identity is not claimable
        source_id = reference.reference_id
        if source_id in table and table[source_id] != candidate:
            ambiguous.add(source_id)
        table[source_id] = candidate
    for source_id in ambiguous:
        del table[source_id]
    return table, frozenset(ambiguous)


def _fold_whitespace(text: str) -> str:
    """Collapse every whitespace run to one space."""
    return " ".join(text.split())


#: The punctuation a model most often re-renders when it re-types its own
#: sentence: curly quotes, the dash family, the ellipsis, and non-breaking
#: spaces. A CLOSED table — this is a diagnostic vocabulary, not a matcher.
_PUNCTUATION_FOLD: Final = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2007": " ",
        "\u202f": " ",
    }
)


def _fold_punctuation(text: str) -> str:
    return text.translate(_PUNCTUATION_FOLD)


def _fold_for_locating(text: str) -> tuple[str, tuple[int, ...]]:
    """``ws+punct``-folded text, plus each folded character's index in ``text``.

    The locating fold has to be INVERTIBLE in one direction: having found the
    span in folded space, :func:`_server_bound_claims` must bind the
    ORIGINAL answer's characters, never the folded rendering. So the fold is
    written once as a scan that carries an origin index alongside every character
    it keeps, rather than as ``_fold_whitespace(_fold_punctuation(text))`` — which
    produces the same string and throws the positions away.

    That equivalence is not left to inspection: a test asserts this function's
    first return value equals ``_fold_whitespace(_fold_punctuation(text))`` for
    every sample it checks, so the locator and the two named folds can
    never come to disagree about what "the same span" means.

    Leading and trailing whitespace is dropped and every internal run collapses
    to one space, exactly as ``" ".join(text.split())`` does. A collapsed run's
    origin is the index of its FIRST character, which keeps a mapped-back span
    minimal: it can never reach past the answer text the model actually wrote.
    """
    folded: list[str] = []
    origin: list[int] = []
    previous_was_space = True
    for index, character in enumerate(text):
        mapped = _PUNCTUATION_FOLD.get(ord(character), character)
        if mapped.isspace():
            if previous_was_space:
                continue
            folded.append(" ")
            origin.append(index)
            previous_was_space = True
            continue
        folded.append(mapped)
        origin.append(index)
        previous_was_space = False
    while folded and folded[-1] == " ":
        folded.pop()
        origin.pop()
    return "".join(folded), tuple(origin)


#: The floor a server-selected span must clear, in folded words. Below it the
#: overlap between an answer and a served chunk is boilerplate — "of the billing
#: review", "at the same time" — and binding it would publish an answer as
#: "supported" by a fragment that states nothing. Five consecutive words is the
#: shortest run that still reads as a statement the source actually makes; the
#: prompt teaches the model to quote a FULL sentence, which clears this floor by
#: a wide margin. The floor never relaxes the verbatim rule — it narrows what is
#: eligible to bind, and a run whose only overlaps are under it refuses.
_MIN_BOUND_SPAN_WORDS: Final = 5

#: The span search's operation budget per (answer, source) pair, measured in
#: word MATCH PAIRS (an answer word occurrence times a source occurrence of the
#: same word — exactly the inner-loop count of :func:`_longest_common_word_run`,
#: computed BEFORE the loop runs). Real traffic sits orders of magnitude below
#: this; a pathological pair over it is skipped deterministically — that source
#: simply cannot server-bind — rather than letting a hot failure path stall the
#: run. Skipping can only ever REFUSE more (fail closed), never bind more.
_MAX_SPAN_MATCH_PAIRS: Final = 250_000


def _folded_words(folded: str) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """A ``ws+punct``-folded text's words, plus each word's folded start index.

    ``folded`` must be a first return value of :func:`_fold_for_locating`: no
    leading/trailing space and single internal spaces, so ``split(" ")`` yields
    exactly the words and every word's start is recoverable by accumulation.
    """
    words: list[str] = []
    starts: list[int] = []
    index = 0
    for word in folded.split(" "):
        if word:
            words.append(word)
            starts.append(index)
        index += len(word) + 1
    return tuple(words), tuple(starts)


def _longest_common_word_run(
    answer_words: Sequence[str], source_words: Sequence[str]
) -> tuple[int, int, int] | None:
    """The longest consecutive word run common to both texts, or ``None``.

    Returns ``(run_length_in_words, answer_word_start, source_word_start)``.
    DETERMINISTIC: ties on
    length keep the run that ENDS earliest in the answer (strict ``>`` on the
    running maximum), so the same two texts always select the same span.

    Algorithm: the classic longest-common-substring recurrence over WORD
    sequences, sparse — only positions where the two words actually match are
    visited, via a positions index of the source. Time is O(A + S + P) and space
    O(S), where A/S are the word counts and P is the number of match pairs
    (Σ over distinct words of answer-count x source-count). P is computed FIRST
    and the pair is refused over :data:`_MAX_SPAN_MATCH_PAIRS`, so the worst
    case is a cheap counting pass, never a stall.

    Word granularity is deliberate, not an optimisation: a bound span must not
    start or end mid-word ("...ther the billing rev..." is not a statement), and
    in folded space a common word run IS a verbatim common substring — words
    equal pairwise and separated by single spaces on both sides — so nothing
    character-exact is lost by matching words. The one thing word matching alone
    would lose — a boundary word that differs only by ATTACHED punctuation, like
    the ``"All`` that opens a quoted sentence — is recovered by
    :func:`_char_extended_bounds`, which never relaxes character equality.
    """
    positions: dict[str, list[int]] = {}
    for j, word in enumerate(source_words):
        positions.setdefault(word, []).append(j)
    pairs = 0
    for word in answer_words:
        pairs += len(positions.get(word, ()))
        if pairs > _MAX_SPAN_MATCH_PAIRS:
            return None
    best_length = 0
    best_answer_start = -1
    best_source_start = -1
    previous: dict[int, int] = {}
    for i, word in enumerate(answer_words):
        current: dict[int, int] = {}
        for j in positions.get(word, ()):
            length = previous.get(j - 1, 0) + 1
            current[j] = length
            if length > best_length:
                best_length = length
                best_answer_start = i - length + 1
                best_source_start = j - length + 1
        previous = current
    if best_length == 0:
        return None
    return best_length, best_answer_start, best_source_start


def _honest_word_cut(folded_answer: str, index: int, *, direction: int) -> bool:
    """May a span boundary land at ``index`` of the folded answer?

    ``direction=-1`` asks about a span START at ``index``; ``+1`` about a span
    END at ``index`` (inclusive). A boundary is honest when the rest of the
    answer word it lands inside — scanning outward to the nearest space or text
    edge — carries no alphanumeric character. So a span may begin inside
    ``"All`` (the residue is the quotation mark) but may never end at the
    ``review`` inside ``reviewer`` (the residue ``er`` would silently change
    what the answer said).
    """
    j = index + direction
    while 0 <= j < len(folded_answer) and folded_answer[j] != " ":
        if folded_answer[j].isalnum():
            return False
        j += direction
    return True


def _char_extended_bounds(
    folded_answer: str,
    folded_source: str,
    answer_first: int,
    answer_last: int,
    source_first: int,
    source_last: int,
) -> tuple[int, int]:
    """Widen a common span by strict character equality, then keep it honest.

    Extension: while the characters immediately outside the span are EQUAL in
    the two folded texts, take them. No fold is applied here beyond the
    ``ws+punct`` fold both inputs already carry, and no character is taken that
    is not identical in both — so the widened span is verbatim-common by the
    same definition the word run was.

    Honesty repair: the widened boundary may now sit inside an answer word (that
    is the point — it recovers the ``All`` from ``"All``). It is walked back in
    while the cut it makes would leave an ALPHANUMERIC residue of that answer
    word (:func:`_honest_word_cut`): binding ``review`` out of ``reviewer``
    would publish answer bytes that say something the answer does not. The walk
    is bounded by the word-aligned run it started from, which is always honest.
    """
    while (
        answer_first > 0
        and source_first > 0
        and folded_answer[answer_first - 1] == folded_source[source_first - 1]
    ):
        answer_first -= 1
        source_first -= 1
    while _honest_word_cut(folded_answer, answer_first, direction=-1) is False:
        answer_first += 1
    while (
        answer_last + 1 < len(folded_answer)
        and source_last + 1 < len(folded_source)
        and folded_answer[answer_last + 1] == folded_source[source_last + 1]
    ):
        answer_last += 1
        source_last += 1
    while _honest_word_cut(folded_answer, answer_last, direction=1) is False:
        answer_last -= 1
    return answer_first, answer_last


#: The sentence boundary the TABLE-SENTENCE path cuts candidate spans at — the
#: same boundary class ``ne.govern.grounding._SENT_SPLIT_RE`` uses to split a
#: draft, so a sentence bound here is graded downstream as the ONE sentence it
#: was selected as, never re-fused with its neighbours.
_ANSWER_SENTENCE_SPLIT_RE: Final = re.compile(r"[.!?;]+(?=\s|$)|\n+")

#: The most answer sentences the table-sentence path will grade per source. A
#: real answer sits far below this; a pathological one is truncated
#: DETERMINISTICALLY, which can only refuse more (fail closed), never bind more.
_MAX_GRADED_SENTENCES: Final = 128


def _answer_sentence_ranges(final_text: str) -> list[tuple[int, int]]:
    """Character ranges of the answer's sentences, whitespace-stripped, in order."""
    ranges: list[tuple[int, int]] = []
    position = 0
    for match in _ANSWER_SENTENCE_SPLIT_RE.finditer(final_text):
        if match.start() > position:
            ranges.append((position, match.start()))
        position = match.end()
    if position < len(final_text):
        ranges.append((position, len(final_text)))
    stripped: list[tuple[int, int]] = []
    for start, end in ranges:
        while start < end and final_text[start].isspace():
            start += 1
        while end > start and final_text[end - 1].isspace():
            end -= 1
        if start < end:
            stripped.append((start, end))
    return stripped


#: Clause boundaries inside ONE sentence, for the NE-CLAIM-ROWCLAUSE fallback:
#: the punctuation (and the one conjunction) an enumerating sentence uses to
#: chain row statements. Splitting at a spurious boundary is harmless — the cut
#: clause simply fails the gate and binds nothing (fail closed).
_CLAUSE_SPLIT_RE: Final = re.compile(r"[,:;()\u2013\u2014]|\band\b", re.IGNORECASE)

#: Edge punctuation stripped from a whitespace token before asking whether it
#: is a parseable value numeral. '%' and currency symbols are deliberately NOT
#: stripped — they are meaning-bearing (F2) and part of the value.
_VALUE_EDGE_PUNCT: Final = ".,;:()[]{}<>\"'\u201c\u201d\u2018\u2019"


def _carries_value_numeral(text: str) -> bool:
    """True iff ``text`` carries a digit-bearing token ``canonical_numeral`` can
    parse — an ASSERTED VALUE (20000, 4.0, $200.00), never a bare compound
    identifier or date (mer-dsp-005, 2026-08-03), whose canonical is ``None``."""
    for word in text.split():
        word = word.strip(_VALUE_EDGE_PUNCT)
        if (
            word
            and any(character.isdigit() for character in word)
            and canonical_numeral(word) is not None
        ):
            return True
    return False


def _clause_ranges(final_text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Whitespace-trimmed clause ranges of ``final_text[start:end]``, in order."""
    ranges: list[tuple[int, int]] = []
    position = start
    boundaries = [
        (match.start() + start, match.end() + start)
        for match in _CLAUSE_SPLIT_RE.finditer(final_text[start:end])
    ]
    for boundary_start, boundary_end in [*boundaries, (end, end)]:
        clause_start, clause_end = position, boundary_start
        position = boundary_end
        while clause_start < clause_end and final_text[clause_start].isspace():
            clause_start += 1
        while clause_end > clause_start and final_text[clause_end - 1].isspace():
            clause_end -= 1
        if clause_start < clause_end:
            ranges.append((clause_start, clause_end))
    return ranges


#: NE-ROWSENT-SKIPS. The `rowsent` shape field carries one integer per claimable
#: source, and a NON-NEGATIVE value is the count of candidate sentences graded.
#: These negatives say why a source never got that far, because "no entry" used
#: to conflate three different defects and the field was one character from being
#: able to separate them:
#:
#:   -1  the source retained NO text at all -- claimable, but nothing was
#:       delivered to prove a span against. Upstream of the claim binder entirely.
#:   -2  retained text that is NOT a recognised verified-T3 rendering, so the
#:       table path does not apply and only the prose word floor could bind it.
#:   -3  a recognised rendering that produced an EMPTY grounding corpus.
#:
#: Integers only, per C12: no answer byte and no source byte leaves that frame.
_ROWSENT_NO_RETAINED_TEXT: Final = -1
_ROWSENT_NOT_A_RENDERING: Final = -2
_ROWSENT_EMPTY_CORPUS: Final = -3


#: NE-ROWSENT-CODES. `rowsent` says HOW MANY candidate sentences were graded; it
#: never said WHY any of them refused, so a run that graded four candidates and
#: bound none looked identical whether the model paraphrased, crossed a row
#: boundary, or invented a number -- three defects wanting three different fixes.
#: `rowsentcodes` carries one integer per graded candidate, in grading order.
#:
#: The vocabulary is NOT invented here. :func:`ne.govern.grounding.check_grounding`
#: already classifies every refusal into the closed ``FindingReason`` literal
#: (``ne/govern/grounding.py``, ``FindingReason``), and these codes are that
#: literal's members numbered in its own alphabetical order -- the order
#: ``check_grounding`` itself sorts findings into -- so a code is a stable name
#: for a rule that already exists rather than a parallel dialect of it:
#:
#:    1  citation-out-of-range      6  relation-ungrounded
#:    2  iri-ungrounded             7  vocab-extended
#:    3  numeral-ungrounded         8  vocab-ungrounded
#:    4  numeral-unparseable        9  vocab-ungrounded-factual
#:    5  phrase-recombination
#:
#: A verdict carries every class it found; the code reported is the LOWEST one
#: present, which (by that shared alphabetical order) is the first finding of the
#: verdict's own sorted tuple. Deterministic, and one integer per candidate.
_ROWSENT_REFUSAL_CODES: Final[Mapping[str, int]] = {
    "citation-out-of-range": 1,
    "iri-ungrounded": 2,
    "numeral-ungrounded": 3,
    "numeral-unparseable": 4,
    "phrase-recombination": 5,
    "relation-ungrounded": 6,
    "vocab-extended": 7,
    "vocab-ungrounded": 8,
    "vocab-ungrounded-factual": 9,
}
#: 0 -- the candidate PASSED the gate and is the span that binds. Emitted so the
#: field has exactly one integer per graded candidate, count and codes agreeing.
_ROWSENT_CODE_BOUND: Final = 0
#: 10 -- the gate itself passed, but the candidate leaned on PROMPT echo, which
#: :func:`_table_sentence_span` refuses separately (``prompt_echo_count``). Not a
#: ``FindingReason``, so it takes a code outside the mapped block rather than
#: being laundered into one of the nine.
_ROWSENT_CODE_PROMPT_ECHO: Final = 10
#: 99 -- RESERVED "other": a refusal this mapping does not recognise. A finding
#: class added to ``FindingReason`` upstream, or a refusal carrying no finding at
#: all, lands here. The vocabulary is CLOSED: the field can only ever speak these
#: integers, and an unrecognised reason is never rendered as its own text.
_ROWSENT_CODE_OTHER: Final = 99
#: The most NE-ROWSENT-CODES integers `rowsentcodes` renders in ONE shape, across
#: all of its groups. A pathological answer can grade up to
#: :data:`_MAX_GRADED_SENTENCES` candidates per source; the leading ones say what
#: the run died of, and `rowsent` still carries the EXACT counts either way, so
#: nothing is lost past this bound but repetition.
#:
#: This is a READABILITY bound, not the safety one. What keeps the field from
#: costing the diagnostic it was added to is :func:`_shape_renderings`, which
#: sheds codes -- and only codes -- until the shape fits the safe reason. The
#: group separators are one per claimable source and this cap does not bound
#: them, so a cap alone could never have been the guarantee.
_ROWSENT_CODES_RENDERED: Final = 32

#: The one `span_shape` field the fitter is allowed to sacrifice, spelled ONCE.
#: The builder writes it and :func:`_shape_renderings` finds it by this exact
#: text, so a rename cannot leave the two disagreeing about which field is the
#: expendable one.
_ROWSENT_CODES_FIELD: Final = "rowsentcodes="


def _rowsent_codes_field(groups: Sequence[Sequence[int]]) -> str:
    """The `rowsentcodes` shape value: `/`-separated groups of `,`-separated codes.

    One group per `rowsent` entry, in the same order, so the two fields read
    positionally against each other. Deterministically truncated from the FRONT
    of the budget: a group renders what is left of :data:`_ROWSENT_CODES_RENDERED`
    when it is reached, and nothing after that.
    """
    budget = _ROWSENT_CODES_RENDERED
    rendered: list[str] = []
    for group in groups:
        shown = list(group)[:budget]
        budget -= len(shown)
        rendered.append(",".join(str(code) for code in shown))
    return "/".join(rendered) or "none"


def _shape_renderings(shape: str) -> Iterator[str]:
    """``shape`` in descending order of detail, `rowsentcodes` first to go.

    NE-ROWSENT-CODES-FIT. :func:`_output_contract_reason` fits ``[shape]`` as ONE
    fragment and keeps it only if the whole thing lands under
    :data:`_MAX_SAFE_REASON_CHARS`, so a shape that overflows is not a
    shorter diagnostic -- it is NO diagnostic. Before this generator existed,
    appending `rowsentcodes` could push a shape over that line and take `aw`,
    `sources`, `maxrun`, `floor` and `rowsent` down with it: the newest field
    could cost strictly more information than it added. The 32-integer render cap
    does not prevent that, because the `/` group separators are one per claimable
    source and are not capped at all.

    The invariant, and the only one this has to hold: **the shape WITHOUT codes
    is always the last rendering offered**, so whatever the caller could have
    published before the field existed it can still publish now. Codes are
    strictly best-effort on top of it. Shed from the BACK, at group and code
    boundaries, matching :func:`_rowsent_codes_field`'s own front-of-the-budget
    discipline: the leading groups are the ones a reader diagnoses from.

    Yields at least two renderings for a shape carrying codes, exactly one for a
    shape without them -- and never a rendering that is not integers and
    separators, because it only ever CUTS the value the builder wrote.
    """
    yield shape
    marker = f" {_ROWSENT_CODES_FIELD}"
    head, separator, codes = shape.rpartition(marker)
    if not separator:
        # No codes field to spend: this shape is already its own floor.
        return
    while codes:
        cut = max(codes.rfind(","), codes.rfind("/"))
        if cut < 0:
            break
        # Trailing separators go with the groups they used to separate: a cut
        # list should not read as "and then some empty groups", which is a claim
        # about the run rather than about the budget.
        codes = codes[:cut].rstrip(",/")
        if codes:
            yield f"{head}{marker}{codes}"
    yield head


def _rowsent_refusal_code(verdict: GroundingVerdict) -> int:
    """The one NE-ROWSENT-CODES integer for a refusing grounding verdict.

    C12: reads only the verdict's reason CLASSES -- which carry no answer or
    source bytes by construction (:class:`ne.govern.grounding.GroundingFinding`)
    -- and returns an integer from the closed set above. Nothing else escapes.
    """
    if verdict.passed:
        # Passed the gate proper, so the only thing left that refuses it here.
        return _ROWSENT_CODE_PROMPT_ECHO
    codes = [
        _ROWSENT_REFUSAL_CODES[finding.reason]
        for finding in verdict.findings
        if finding.reason in _ROWSENT_REFUSAL_CODES
    ]
    if len(codes) != len(verdict.findings):
        # An unmapped class is present: say "other" rather than report a code
        # that names the wrong rule. Fail loud in the diagnostic, not silently.
        return _ROWSENT_CODE_OTHER
    return min(codes, default=_ROWSENT_CODE_OTHER)


def _table_sentence_span(
    final_text: str, source_text: str, *, graded: list[int], codes: list[list[int]]
) -> tuple[int, int] | None:
    """NE-CLAIM-ROWSENT: the earliest answer sentence that GROUNDS in a table source.

    The verbatim-run rule cannot bind an honest answer over a verified table: on
    a ``" | "`` rendering a cell is one or two words, so any run clearing
    :data:`_MIN_BOUND_SPAN_WORDS` must cross cell boundaries — while the answer
    shape both the claim-form guidance and the grounding gate reward states ONE
    ROW PER SENTENCE in the model's own connectives ("mer-dsp-003 has
    credit_amount_cents 20000"), which shares at most two or three consecutive
    words with the rendering. Measured live 2026-08-27 (post-linefact): a
    154-word answer over two sources with ``maxrun=1`` — the squeeze is exact,
    and no floor value fixes it, because the floor measures contiguity and the
    honest sentence has none to offer.

    So for a source whose retained text IS a verified-T3 rendering, sufficiency
    is not a word count at all: a candidate span binds iff the REAL grounding
    gate passes it against a corpus built from that rendering alone
    (:func:`ne.runtime.run_claim_verifier.rendering_preview_answers` — the
    verifier's own NE-CLAIM-LINEFACT construction, minus the template-id
    vocabulary, so it is strictly narrower than what ``verify_claim`` will
    grade with). This CANNOT be satisfied coincidentally or by fabrication:
    passing means every prose token, every numeral (under F3 row-binding and
    NE-GROUND-ROWID) and every compound identifier of the sentence is grounded
    in exactly this source's served bytes — a strictly stronger bar than any
    contiguity floor. A paraphrase ("about two hundred dollars"), a unit
    conversion ("$200.00" over a cents cell), a wrong-row pairing and a
    fabricated value all still refuse, and an answer containing none but
    grounded sentences over a table it never received cannot reach here at all
    (retention holds only text this run's deliveries actually served).

    A sentence must also CARRY a numeral-classified token (any digit) to bind:
    a vocabulary-only sentence ("the disputes directory records notice dates")
    grounds trivially in the table's own column words and would bind a claim
    that asserts nothing — the same boilerplate the word floor exists to refuse
    on the prose path.

    Deterministic: whole sentences are graded in answer order first, then the
    single-row CLAUSES of the failing ones (NE-CLAIM-ROWCLAUSE, see pass 2
    below), and the FIRST passing candidate binds; at most
    :data:`_MAX_GRADED_SENTENCES` candidates are graded in total (fail closed).
    ``graded`` receives the count of sentences graded, for the C12-safe shape
    diagnostic (integers only — no answer or source byte leaves this frame).
    ``codes`` receives THIS call's own list of NE-ROWSENT-CODES integers, one per
    graded candidate in grading order (see :data:`_ROWSENT_REFUSAL_CODES`): the
    count says how many were tried, the codes say what each one died of. Appended
    here, alongside ``graded``, so the two can never drift apart. Purely
    observational — no candidate's verdict is read for anything but its code.
    """
    from ne.runtime.run_claim_verifier import rendering_preview_answers

    own: list[int] = []
    codes.append(own)
    # NE-ROWSENT-SKIPS: both early exits record WHY they left, for the same
    # reason the retained-text skip does -- an absent entry used to mean either
    # "not a recognised rendering" or "nothing retained", and those want
    # different fixes.
    answers = rendering_preview_answers(source_text)
    if answers is None:
        graded.append(_ROWSENT_NOT_A_RENDERING)
        return None
    corpus = GroundingCorpus.from_answers(answers)
    if not corpus.tokens:
        graded.append(_ROWSENT_EMPTY_CORPUS)
        return None
    count = 0

    def _passes(start: int, end: int) -> bool:
        nonlocal count
        count += 1
        verdict = check_grounding(final_text[start:end], corpus, "", profile=PROFILE_NG2)
        passed = verdict.passed and not verdict.prompt_echo_count
        # NE-ROWSENT-CODES. Recorded for EVERY candidate, pass or refuse, and
        # recorded AFTER the decision is already made: this line reads the
        # verdict, it never participates in it.
        own.append(_ROWSENT_CODE_BOUND if passed else _rowsent_refusal_code(verdict))
        return passed

    ranges = _answer_sentence_ranges(final_text)[:_MAX_GRADED_SENTENCES]
    # A candidate must carry a digit, and a candidate WITHOUT a parseable value
    # numeral must carry at least four words — a bare-identifier sentence ("See
    # mer-dsp-005.") grounds trivially by containment and would bind a span
    # that asserts nothing, the boilerplate hazard the prose floor refuses.
    digit_ranges = [
        (start, end)
        for start, end in ranges
        if any(character.isdigit() for character in final_text[start:end])
        and (
            _carries_value_numeral(final_text[start:end]) or len(final_text[start:end].split()) >= 4
        )
    ]
    # Pass 1 — whole sentences, exactly the shipped NE-CLAIM-ROWSENT rule.
    for start, end in digit_ranges:
        if count >= _MAX_GRADED_SENTENCES:
            break
        if _passes(start, end):
            graded.append(count)
            return start, end
    # Pass 2 (NE-CLAIM-ROWCLAUSE) — clauses of the failing sentences. The
    # natural answer to an enumerating question joins its rows in ONE sentence
    # ("mer-dsp-003 ... 20000, and mer-dsp-005 ... 90000"), which the gate
    # REFUSES whole — correctly: a sentence naming two rows is an unverifiable
    # pairing under NE-GROUND-ROWID. Its single-row clauses are each fully
    # verifiable, so they are graded as candidates of their own, in answer
    # order. A clause must CARRY A PARSEABLE VALUE NUMERAL (not merely a
    # compound identifier) to be graded: a bare "mer-dsp-005" grounds trivially
    # by containment and would bind a span that asserts nothing — the
    # boilerplate hazard the prose floor exists to refuse. The bound slice is
    # re-graded by ``verify_claim`` downstream as its own sentence, with full
    # force; splitting can therefore only ever narrow what binds, never widen
    # what verifies.
    for start, end in digit_ranges:
        if count >= _MAX_GRADED_SENTENCES:
            break
        for clause_start, clause_end in _clause_ranges(final_text, start, end):
            if count >= _MAX_GRADED_SENTENCES:
                break
            if (clause_start, clause_end) == (start, end):
                continue
            if not _carries_value_numeral(final_text[clause_start:clause_end]):
                continue
            if _passes(clause_start, clause_end):
                graded.append(count)
                return clause_start, clause_end
    graded.append(count)
    return None


def _server_bound_claims(
    final_text: str,
    *,
    served_refs: set[str],
    retention: ServedTextRetention | None,
) -> tuple[ModelPlannedClaim, ...]:
    """THE SERVER selects and binds every claim span (NE-SPAN-BIND).

    Why the server and not the model. A claim has to be one string satisfying
    two gates owned by two different checkers: a span of ``final_text`` (this
    module) whose tokens come from the CITED CHUNK (``check_grounding``,
    downstream). Measured over 21 real claims from 6 real model runs, ZERO
    model-authored strings satisfied both — 19 were answer spans absent from
    the source, 2 were source spans absent from the answer. The contract was
    near-unsatisfiable by construction, and every ``claim text does not appear
    verbatim in the final answer`` and ``pfx=0 fixedby=none`` failure was this.
    The server holds BOTH texts — the answer, and (now, retained per run by
    :class:`~ne.runtime.source_results.ServedTextRetention`) the authorized
    chunk text each delivery served — so it computes the span itself.

    What binds. For each citable served reference, the LONGEST verbatim word run
    (under the same documented ``ws+punct`` fold the old locator applied — and
    nothing beyond it: no case folding, no approximation) common to the answer
    and that reference's retained delivered text, if it reaches
    :data:`_MIN_BOUND_SPAN_WORDS`. When no run qualifies and the retained text
    is a verified-T3 TABLE rendering, :func:`_table_sentence_span` binds the
    first answer sentence that fully GROUNDS in that rendering instead
    (NE-CLAIM-ROWSENT) — a stricter sufficiency than the word floor, measured by
    the same gate ``verify_claim`` re-runs downstream. The folded hit is mapped BACK to a character
    range of ``final_text`` and the offsets and SHA-256 are taken from THE
    ANSWER'S OWN BYTES at that range — the model authors no offsets and no
    hashes, exactly as before. ``claim_key`` is now server-derived too
    (``bound:<reference_id>``): it was the last model-authored free text, it
    exists only to join the claim to its verifier and evidence rows, and a
    surrogate the server mints cannot collide, cannot leak, and cannot be wrong.

    What still refuses, and where. A source-bearing run none of whose answer
    words are verbatim in anything it served raises :data:`_CLAIM_NO_COMMON_SPAN`
    here — the same conclusion the downstream verifier reaches today as ``claim
    is not grounded by exact evidence``, reached BEFORE publication while one
    bounded correction can still teach the model to quote. And binding here
    exempts the span from NOTHING downstream: ``verify_claim`` still re-slices
    the answer's bytes, re-hashes them, rebuilds the corpus from the sealed
    evidence observation and runs ``check_grounding`` with full force —
    containment, anti-recombination, NE-GROUND-ROWID and the numeral rules can
    all still refuse the bound span. This function decides WHICH span is
    checked; it checks nothing away.

    C12: the raised refusal is a static server-authored literal; the attached
    ``span_shape`` diagnostic is integers from closed counters (word counts,
    source count, best run length, the floor, and the NE-ROWSENT-CODES refusal
    classes) and never a byte of answer or chunk text. Retained text is READ here
    and published nowhere.
    """
    folded_answer, origin = _fold_for_locating(final_text)
    answer_words, answer_starts = _folded_words(folded_answer)
    claimable, _ambiguous = _claimable_sources(served_refs)
    bound: list[ModelPlannedClaim] = []
    longest_seen = 0
    sentences_graded: list[int] = []
    # NE-ROWSENT-CODES: one inner list per `sentences_graded` entry, holding that
    # entry's per-candidate refusal codes. Appended in lockstep with it, at both
    # of the two sites that can add an entry.
    sentence_codes: list[list[int]] = []
    for source_id in sorted(claimable):
        text = retention.text_for(source_id) if retention is not None else None
        if text is None:
            # Never delivered as authorized text in this run (or poisoned, or
            # over the retention caps): there is nothing this source can prove a
            # span against, so it binds nothing. Fail-closed by construction.
            #
            # NE-ROWSENT-SKIPS: recorded as -1 rather than silently skipped. Read
            # live, `rowsent` was the only field that could tell the failures
            # apart, and it could not tell THIS case from a source whose text was
            # simply not a table rendering -- both left no entry, and the two are
            # different defects: "claimable but delivered nothing to retain" is
            # upstream of anything the claim binder does. One integer separates
            # them. C12-safe by the same rule as the rest of the shape: a count,
            # never a byte.
            sentences_graded.append(_ROWSENT_NO_RETAINED_TEXT)
            sentence_codes.append([])
            continue
        folded_source, _ = _fold_for_locating(text)
        source_words, source_starts = _folded_words(folded_source)
        run = _longest_common_word_run(answer_words, source_words)
        length = 0
        if run is not None:
            length = run[0]
            longest_seen = max(longest_seen, length)
        if run is None or length < _MIN_BOUND_SPAN_WORDS:
            # NE-CLAIM-ROWSENT: no qualifying verbatim run. For a TABLE source
            # that is the expected shape of an HONEST answer (one row per
            # sentence, in the model's own connectives), so sufficiency falls
            # back from contiguity to the real grounding gate: bind the first
            # answer sentence that fully grounds in this rendering. Prose
            # sources are untouched — a chunk has real sentences to quote, and
            # the verbatim floor stays their only path.
            sentence = _table_sentence_span(
                final_text, text, graded=sentences_graded, codes=sentence_codes
            )
            if sentence is None:
                continue
            first, sentence_end = sentence
            last = sentence_end - 1
        else:
            start_word, source_start_word = run[1], run[2]
            last_word = start_word + length - 1
            source_last_word = source_start_word + length - 1
            answer_first, answer_last = _char_extended_bounds(
                folded_answer,
                folded_source,
                answer_starts[start_word],
                answer_starts[last_word] + len(answer_words[last_word]) - 1,
                source_starts[source_start_word],
                source_starts[source_last_word] + len(source_words[source_last_word]) - 1,
            )
            first = origin[answer_first]
            last = origin[answer_last]
        span = final_text[first : last + 1]
        # Byte offsets, because that is what every downstream verifier
        # re-slices. Both ends are measured by ENCODING whole ``str`` prefixes,
        # so neither can land inside a code point.
        start = len(final_text[:first].encode("utf-8"))
        selected = span.encode("utf-8")
        bound.append(
            ModelPlannedClaim(
                claim_key=f"bound:{source_id}",
                span_start_byte=start,
                span_end_byte=start + len(selected),
                claim_content_hash=hashlib.sha256(selected).hexdigest(),
                served_ref=claimable[source_id],
            )
        )
    if bound or not any(ref.startswith(PROTECTED_REFERENCE_PREFIX) for ref in served_refs):
        # The predicate mirrors the old omitted-claims rule EXACTLY: an answer
        # over served protected source material must carry at least one claim;
        # a source-free run completes with none. Equal force, derived not
        # declared.
        return tuple(bound)
    # The shape is attached at the RAISE site because this is the only frame
    # that still knows how close the best overlap came. Read back with
    # ``getattr`` — the same opt-in pattern ``safe_reason`` uses.
    refusal = ActionValidationError(_CLAIM_NO_COMMON_SPAN)
    refusal.span_shape = (  # type: ignore[attr-defined]
        f"aw={len(answer_words)} sources={len(claimable)} "
        f"maxrun={longest_seen} floor={_MIN_BOUND_SPAN_WORDS} "
        f"rowsent={'/'.join(str(count) for count in sentences_graded) or 'none'} "
        # NE-ROWSENT-CODES: same shape as `rowsent` -- one '/'-separated group per
        # entry there, each a ','-separated run of per-candidate codes in grading
        # order, so the two fields read positionally against each other. Integers
        # and separators ONLY: a code names a RULE from the closed vocabulary,
        # never a sentence, a column or an identifier.
        # Written LAST, and the only field `_shape_renderings` may shed: the
        # fields before it are the ones a reader needs whatever else happens.
        f"{_ROWSENT_CODES_FIELD}{_rowsent_codes_field(sentence_codes)}"
    )
    raise refusal


def _bounded_step_intent(value: object) -> str:
    """Return one closed display intent, never Tool arguments or hidden reasoning."""
    intent = value.strip() if isinstance(value, str) else ""
    if not intent:
        raise ActionValidationError("step_intent must be a non-empty string")
    if len(intent) > 256:
        raise ActionValidationError("step_intent must not exceed 256 characters")
    if any(not character.isprintable() for character in intent):
        raise ActionValidationError("step_intent cannot contain control characters or newlines")
    return intent


@dataclass
class _RecordedExecutor:
    """Metadata-only wrapper whose successful path is still the governed executor."""

    inner: ToolExecutorLike
    principal: Principal
    sink: AuditSink
    max_tool_calls: int
    max_parked_writes: int
    boundary: RunExecutionBoundary = _NOOP_BOUNDARY
    source_guard: ProtectedSourceResultGuard | None = None
    #: NE-LOOP-NOPROGRESS: consecutive same-Tool calls that may serve no new
    #: evidence before the run stops. A field, not a hard-coded constant, so the
    #: bound is visible at the one place it is enforced and exercisable in tests.
    max_no_progress_calls: int = _NO_PROGRESS_TOOL_CALLS
    steps: list[ModelPlannedStep] = field(default_factory=list)
    served: set[str] = field(default_factory=set)
    tool_calls: int = 0
    write_calls: int = 0
    #: The Tool the current barren streak belongs to, and its length. A call that
    #: serves new evidence clears both; a call to a DIFFERENT Tool restarts them.
    no_progress_tool: str | None = None
    no_progress_calls: int = 0
    #: Per-Tool sha256 digests of the outputs this run has already been served.
    #: DIGESTS, never the text: a result may carry customer data and this object
    #: outlives the call. Bounded by `max_tool_calls`, which is small.
    seen_outputs: dict[str, set[str]] = field(default_factory=dict)

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(_model_facing_spec(spec) for spec in self.inner.tool_specs)

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        if principal.id != self.principal.id:
            raise RuntimeAuditError(
                "agentic run principal changed between planning and execution; "
                "the call is refused (single-principal invariant)"
            )
        if self.tool_calls >= self.max_tool_calls:
            raise UsageLimitExceeded(
                f"the next tool call would exceed max_tool_calls={self.max_tool_calls}"
            )

        governed_args = dict(args)
        intent_value = governed_args.pop(_STEP_INTENT_ARG, "")
        # ``evidence_refs`` is never a business argument and never an identity
        # authority. Older prompts may still emit it, but the runner ignores the
        # model-authored value and derives the effect carrier solely from results
        # that its own successful read path observed below.
        governed_args.pop(_EVIDENCE_REFS_ARG, None)
        intent = intent_value.strip() if isinstance(intent_value, str) else ""
        available = {spec.name: spec for spec in self.inner.tool_specs}
        if tool not in available:
            # The model-facing catalog cannot add our required metadata schema to
            # a name it did not receive.  Still route the emitted name through the
            # scoped executor: it owns the toolset-denied audit + PDP consultation,
            # and can never execute an out-of-set handler.  Recorder-only keys are
            # stripped before that governance call just as on an in-set call.
            self.tool_calls += 1
            try:
                output = await self.inner.execute(tool, governed_args, principal=principal)
            except asyncio.CancelledError:
                self.steps.append(
                    ModelPlannedStep(
                        step=len(self.steps) + 1,
                        tool=tool,
                        intent=intent,
                        outcome="cancelled",
                        principal_id=principal.id,
                    )
                )
                raise
            except Exception as exc:
                self.steps.append(
                    ModelPlannedStep(
                        step=len(self.steps) + 1,
                        tool=tool,
                        intent=intent,
                        outcome=f"refused:{type(exc).__name__}",
                        principal_id=principal.id,
                    )
                )
                raise
            raise NeRuntimeError(
                f"out-of-set tool {tool!r} unexpectedly executed and returned "
                f"{len(output)} characters (C6 violation)"
            )
        intent = _bounded_step_intent(intent_value)
        evidence_refs: tuple[str, ...] = ()
        protected_evidence_refs: tuple[str, ...] = ()
        is_write = _is_agentic_write_tool(tool)
        is_protected_effect = is_write or int(available[tool].tier) >= int(ApprovalTier.TIER_2)
        if is_write:
            evidence_refs = tuple(sorted(self.served))
            if not evidence_refs:
                await self._emit_evidence_refusal(tool, reason="no-server-observed-evidence")
                self._record_refusal(
                    tool, intent, "evidence-ref:no-server-observed-evidence", evidence_refs
                )
                raise ActionValidationError(
                    "agentic writes require evidence observed by an earlier successful read"
                )
            if self.write_calls >= self.max_parked_writes:
                self._record_refusal(tool, intent, "cap:parked-writes", evidence_refs)
                raise UsageLimitExceeded(
                    f"the next write could exceed max_parked_writes={self.max_parked_writes}"
                )
        # U3-F4 (live run 7f782fe0): the carrier is populated UNCONDITIONALLY
        # from the server-observed served set, not only when the runner
        # PREDICTS a protected effect. The middleware gates on the STRICTER-OF
        # effective tier — the tiers.rego mirror, the PDP-decided tier, and
        # obligation floors (`per_item_human_approval`/`second_check`) can all
        # escalate a non-write TIER-0/1 tool, and the PDP arm is one the
        # runner can never predict from the spec it reads. Before this, such
        # an escalated call reached `_run_source_references` with an EMPTY
        # carrier and died permanently un-parkable with an evidence-shaped
        # refusal. Populating for every governed call is safe: the middleware
        # consumes the carrier ONLY on the canonical-Run approval path, its
        # exact-serve and stamp checks still bind every member, and the
        # source-guard re-authorization below still runs exactly where it did.
        protected_evidence_refs = _protected_tool_evidence_refs(self.served)

        self.tool_calls += 1
        ordinal = self.tool_calls - 1
        await self.boundary.checkpoint(
            stage=f"agentic-intent:{ordinal}",
            safe_detail={"ordinal": ordinal, "tool": tool, "step_intent": intent},
        )
        # NAME THIS PLAN ORDINAL for the frames below. The receipt-gated
        # executor mints the step's `tool_call_id` and is the ONLY frame that
        # knows both identifiers, so it -- not a parallel counter up here --
        # durably binds `agentic-call:{ordinal}` to that id. The scope carries
        # two names and nothing else: no argument, no result, no content.
        announcement = AgenticPlanStepAnnouncement(ordinal=ordinal, tool=tool)
        try:
            if is_protected_effect and protected_evidence_refs:
                if self.source_guard is None:
                    raise SourceAuthorityRetryableError(
                        "current source authority is unavailable immediately before the effect"
                    )
                decoded = tuple(
                    decode_protected_reference(encoded) for encoded in protected_evidence_refs
                )
                if not all(
                    isinstance(reference, SourceBoundToolResultRef) for reference in decoded
                ):
                    raise RuntimeAuditError(
                        "server-observed protected evidence changed kind before effect execution"
                    )
                references = tuple(
                    reference
                    for reference in decoded
                    if isinstance(reference, SourceBoundToolResultRef)
                )
                decision = await self.source_guard.reauthorize_for_effect(references)
                if decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN:
                    raise SourceAuthorityRetryableError(
                        "current source authority is unknown immediately before the effect"
                    )
                if decision is SourceAuthorityDecision.PERMANENT_WITHHELD:
                    raise SourceAuthorityWithheldError(
                        "current source authority withheld the evidence before the effect"
                    )
                self.write_calls += 1
                with (
                    agentic_plan_step_scope(announcement),
                    protected_tool_evidence_scope(protected_evidence_refs),
                ):
                    output = await self.inner.execute(tool, governed_args, principal=principal)
            elif protected_evidence_refs:
                # Not a predicted protected effect, but the run HAS served
                # protected evidence: carry it so a middleware-side escalation
                # (mirror/PDP/obligation) can park over the COMPLETE set.
                if is_write:
                    self.write_calls += 1
                with (
                    agentic_plan_step_scope(announcement),
                    protected_tool_evidence_scope(protected_evidence_refs),
                ):
                    output = await self.inner.execute(tool, governed_args, principal=principal)
            else:
                if is_write:
                    self.write_calls += 1
                with agentic_plan_step_scope(announcement):
                    output = await self.inner.execute(tool, governed_args, principal=principal)
        except asyncio.CancelledError:
            self.steps.append(
                ModelPlannedStep(
                    step=len(self.steps) + 1,
                    tool=tool,
                    intent=intent,
                    outcome="cancelled",
                    evidence_refs=evidence_refs,
                    principal_id=principal.id,
                )
            )
            raise
        except Exception as exc:
            self.steps.append(
                ModelPlannedStep(
                    step=len(self.steps) + 1,
                    tool=tool,
                    intent=intent,
                    outcome=f"refused:{type(exc).__name__}",
                    evidence_refs=evidence_refs,
                    principal_id=principal.id,
                )
            )
            raise

        served = _served_refs(output)
        # Measured BEFORE the update: "new" means new to this run, which is the
        # only sense in which a read has moved the run forward. A call that
        # re-serves rows an earlier call already served has added nothing, even
        # though its output is not empty.
        served_new_evidence = not self.served.issuperset(served)
        self.served.update(served)
        # The second half of the predicate (see `_NO_PROGRESS_TOOL_CALLS`), and
        # it is a FALLBACK, not a second opinion: a read whose RESULT carries no
        # ref at all is not a read that found nothing, so for those results --
        # and only those -- an unseen output stands in for the ref that was never
        # there. Whenever the result does carry refs, the refs ARE the evidence
        # and the bytes have no vote. `not served` is read exactly that way: it
        # is a fact about this CALL's output, not about the tool or its format.
        #
        # WHAT AN EARLIER VERSION GOT WRONG: the digest was taken over the whole
        # output, so ANY byte change cleared the streak. `read.knowledge.search`
        # is the read that exposed it -- `ne.api.tools` renders a counts header
        # carrying the pre-rerank candidate count and then one
        # `<chunk_id>: <text>` line per hit in RERANK order, and the ordering
        # moves when the QUERY is reworded while the evidence does not.
        # `_progress_digest_material` is what removes both from the digest; the
        # published invariant for this rule -- "rewording the
        # query cannot evade it" -- is that function's job to keep true, for a
        # ref-BEARING search by way of `served` and for a ref-free (source-free)
        # one by way of the digest.
        #
        # `_disclosed_nothing` is the separate case where the result states it
        # matched content and disclosed NONE of it: a source-health blackout is
        # not evidence however the header counts it. Its body is empty, so the
        # digest agrees with it today; it is kept because the blackout is a
        # published property of the RESULT and is asserted as such, not an
        # accident of what the rendering happens to leave behind.
        disclosed_nothing = _disclosed_nothing(output)
        # Digest only -- the output itself is never retained.
        output_digest = hashlib.sha256(
            _progress_digest_material(output).encode("utf-8")
        ).hexdigest()
        seen_for_tool = self.seen_outputs.setdefault(tool, set())
        served_new_output = (
            not served and not disclosed_nothing and output_digest not in seen_for_tool
        )
        seen_for_tool.add(output_digest)
        self.steps.append(
            ModelPlannedStep(
                step=len(self.steps) + 1,
                tool=tool,
                intent=intent,
                outcome="submitted" if is_write else "served",
                served_row_ids=served,
                evidence_refs=evidence_refs,
                principal_id=principal.id,
            )
        )
        # NE-LOOP-NOPROGRESS. A WRITE is an effect: it changed something in the
        # world whether or not it echoed a row back, so it is progress by
        # definition and is never counted here. Everything else is a read, and a
        # read is progress if it served the run ANYTHING it did not already have
        # -- a ref it had not seen or, WHEN THE RESULT CARRIES NO REF AND STATES
        # NO WITHHOLDING, a DISCLOSURE it had not been served. Requiring the ref
        # alone is what made three successful CRM lookups look barren; counting
        # the whole output as the disclosure is what let a reworded search evade
        # the rule, and counting a result that says it disclosed nothing is what
        # let a 100%-withheld search evade it after that.
        if is_write or served_new_evidence or served_new_output:
            self.no_progress_tool = None
            self.no_progress_calls = 0
            return output
        if self.no_progress_tool == tool:
            self.no_progress_calls += 1
        else:
            self.no_progress_tool = tool
            self.no_progress_calls = 1
        if self.no_progress_calls >= self.max_no_progress_calls:
            # Raised AFTER the step is recorded, so the barren call that proved
            # it is in the run's own plan and the reader can count them. The
            # error carries the published sentence; it is deliberately NOT
            # translated into a cap terminal, because no cap was reached and
            # reporting one would tell the reader the run ran out of budget when
            # what actually happened is that there was nothing to find.
            raise AgenticNoProgressError(tool=tool, calls=self.no_progress_calls)
        return output

    def _record_refusal(
        self,
        tool: str,
        intent: str,
        outcome: str,
        evidence_refs: tuple[str, ...],
    ) -> None:
        self.steps.append(
            ModelPlannedStep(
                step=len(self.steps) + 1,
                tool=tool,
                intent=intent,
                outcome=outcome,
                evidence_refs=evidence_refs,
                principal_id=self.principal.id,
            )
        )

    async def _emit_evidence_refusal(self, tool: str, *, reason: str) -> None:
        event = new_audit_event(
            action="agentic.evidence_ref",
            resource=f"tool:{tool}",
            outcome=AuditOutcome.denied,
            actor=self.principal.id,
            detail=json.dumps({"reason": reason}),
        )
        try:
            await self.sink.append(event)
        except Exception as exc:
            raise RuntimeAuditError(
                "audit append failed for agentic evidence-ref refusal; "
                "the write remains refused (C6 fail closed)"
            ) from exc


def _model_facing_spec(spec: ToolSpec) -> ToolSpec:
    """Require a stated intent while keeping evidence identity server-owned."""
    schema = _to_plain_schema(spec.input_schema)
    properties = dict(schema.get("properties", {}))
    properties[_STEP_INTENT_ARG] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 256,
        "pattern": r"^[^\x00-\x1f\x7f-\x9f\r\n]+$",
        "description": "State why this tool call is the next necessary step.",
    }
    required = list(schema.get("required", []))
    if _STEP_INTENT_ARG not in required:
        required.append(_STEP_INTENT_ARG)
    schema["properties"] = properties
    schema["required"] = required
    return spec.model_copy(update={"input_schema": schema})


def build_governed_tool(
    executor: ToolExecutorLike, spec: ToolSpec, principal: Principal
) -> Tool[Any]:
    """Build ONE governed pydantic-ai tool whose only body calls ``executor.execute``.

    C6 — un-shadowable spec binding. pydantic-ai 2.5.0 ``Tool.from_schema`` calls
    this function with KEYWORDS ONLY and SKIPS schema validation, so ANY named
    parameter — even underscore-prefixed, even with a default — could be rebound by
    a model-emitted JSON argument of the same name. The tool name is therefore
    captured by CLOSURE and ``**kwargs`` is the ONLY parameter: no model-emitted key
    can collide with the carrier; adversarial keys arrive as ordinary args and flow
    through guardrails/PDP/audit as data.
    """
    tool_name = spec.name

    async def _call(**kwargs: Any) -> str:
        try:
            return await executor.execute(tool_name, kwargs, principal=principal)
        except _BLOCKING_ERRORS as exc:
            # Already audited by construction; surface a safe, non-leaky result so the
            # loop continues and terminates with a final answer.
            return f"BLOCKED: {type(exc).__name__}: {exc}"

    return Tool.from_schema(
        function=_call,
        name=spec.name,
        description=spec.description,
        json_schema=_to_plain_schema(spec.input_schema),
        takes_ctx=False,
    )


def build_unknown_tool_hook(executor: ToolExecutorLike, principal: Principal) -> UnknownToolHook:
    """Build the hook that routes a model-emitted UNREGISTERED tool call through governance.

    Mirrors the NE-033 session hook verbatim: an unknown name is audited +
    guardrail-checked + PDP'd (via the governed executor) BEFORE pydantic-ai's
    ToolManager rejects it. ``_BLOCKING_ERRORS`` (incl. ``ToolsetViolationError`` for
    an out-of-set name through a scoped executor) are the expected terminals;
    ``RuntimeAuditError`` / ``AuditEmissionError`` are NOT caught and propagate,
    killing the run (an un-recordable call must not continue).
    """

    async def _on_unknown_tool(name: str, args: Mapping[str, Any]) -> None:
        try:
            await executor.execute(name, dict(args), principal=principal)
        except _BLOCKING_ERRORS:
            return
        # Unreachable: the Agent's tool list is built from this same executor's
        # tool_specs, so an unknown name can never resolve to a handler here.
        raise NeRuntimeError(
            f"unknown-tool hook for {name!r} unexpectedly executed a handler (C6 violation)"
        )

    return _on_unknown_tool


async def run_isolated_agent(
    *,
    task: str,
    principal: Principal,
    executor: ToolExecutorLike,
    gateway: ModelGateway,
    settings: RuntimeSettings,
    source_guard: ProtectedSourceResultGuard | None = None,
) -> str:
    """Run a single ISOLATED governed agent turn over ``executor`` and return its output.

    Memory isolation by construction: ``message_history=[]`` (EMPTY) and NO session
    store is referenced in this function, so the agent has no access to any parent
    session's history and writes none. Every tool the model can call is built from
    ``executor.tool_specs`` (already scoped/gated by the caller) and routes through
    the governed executor; a model-emitted out-of-set/unknown name goes through the
    ``on_unknown_tool`` hook first.
    """
    with run_trace_scope(), run_principal_scope(principal.id):
        tools = [build_governed_tool(executor, spec, principal) for spec in executor.tool_specs]
        agent = Agent(
            model=GatewayModel(
                gateway,
                settings,
                on_unknown_tool=build_unknown_tool_hook(executor, principal),
                source_guard=source_guard,
            ),
            output_type=str,
            tools=tools,
        )
        result = await agent.run(
            task,
            message_history=[],
            usage_limits=UsageLimits(request_limit=settings.max_requests),
        )
        return str(result.output)


#: How many distinct schema rejections the published reason names. A reply that
#: misses the shape entirely can produce one error per declared field; the reason
#: is a sentence for a person, not a validator dump.
_MAX_REPORTED_OUTPUT_ERRORS = 5

#: The longest ``safe_reason`` that actually REACHES a reader.
#: ``ne.api.run_projection.RunProjection._failure_reason`` drops any checkpointed
#: ``error_reason`` longer than this (or carrying a non-printable character)
#: rather than half-trusting it — so a reason built past the cap is not a longer
#: explanation, it is NO explanation and a ``failure_reason: null`` all over
#: again. The builder below therefore fits itself to the budget instead of
#: assuming one; ``test_the_published_reason_survives_the_projection_s_own
#: _validation`` pins the two limits against each other.
_MAX_SAFE_REASON_CHARS = 256

#: How far back along the ``__cause__``/``__context__`` chain to look for the
#: Pydantic error that actually rejected the reply. pydantic-ai raises
#: ``UnexpectedModelBehavior from ToolRetryError``, and ``ToolRetryError`` in turn
#: was raised ``from`` the ``ValidationError`` — two hops on the observed path;
#: the bound is slack, not a guess at that exact depth.
_OUTPUT_ERROR_CHAIN_DEPTH = 8


def _chained_validation_error(exc: BaseException) -> ValidationError | None:
    """The :class:`pydantic.ValidationError` behind ``exc``, or ``None``."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_OUTPUT_ERROR_CHAIN_DEPTH):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__ or current.__context__
    return None


# The ``loc`` sanitiser used to live here. It now lives in
# :mod:`ne.runtime.model` (imported above as :func:`safe_error_location`) because
# the RETRY RENDER needs the same rule and ``model`` cannot import ``runner``.
# There is exactly ONE such function in the tree — the ``safe_reason`` built below
# and the retry the model actually reads can no longer disagree about which ``loc``
# segments are the server's to publish.


#: Protocol shapes a rejected top-level object can match, as CLOSED tokens. Key
#: NAMES are read to decide the token and are never published — the same C12
#: guarantee :func:`~ne.runtime.model.rejected_reply_shape` carries.
_REJECTED_OBJECT_SHAPES: Final = (
    ("tool-call", frozenset({"tool_call"}), True),
    ("tool-call-fields", frozenset({"name", "arguments"}), False),
    ("claim", frozenset({"claim_key", "claim_text"}), False),
    ("claim", frozenset({"source_id"}), False),
)


def _rejected_object_shape(errors: Sequence[Mapping[str, Any]]) -> str:
    """Say WHICH other object the model sent, in a closed vocabulary.

    Why this exists. ``extra="forbid"`` plus a missing ``final_text`` is the
    signature of "the model emitted some other object entirely", and every reading
    of it renders IDENTICALLY today: :func:`~ne.runtime.model.safe_error_location`
    masks each
    undeclared key to ``<undeclared key>`` (correctly — it is model-authored text),
    and the builder then dedupes those identical entries. A lone tool call, a bare
    ``name``/``arguments`` pair, a claim hoisted to the top level and an
    ``{"answer": ...}`` wrapper all produce the same sentence, and they want four
    different fixes.

    What it may say: one token from the table above plus an integer count. It
    reads the whole rejected object — Pydantic hands it over as the ``missing``
    error's ``input`` — and republishes no key and no value.
    """
    undeclared = sum(1 for error in errors if error.get("type") == "extra_forbidden")
    payload: Mapping[str, Any] | None = None
    for error in errors:
        candidate = error.get("input")
        if error.get("type") == "missing" and isinstance(candidate, Mapping):
            payload = candidate
            break
    if payload is None:
        return f"undeclared={undeclared}"
    keys = frozenset(str(key) for key in payload)
    token = "other"
    for name, probe, exact in _REJECTED_OBJECT_SHAPES:
        if (keys == probe) if exact else probe <= keys:
            token = name
            break
    else:
        if len(keys) == 1:
            nested = next(iter(payload.values()))
            if isinstance(nested, Mapping) and "final_text" in nested:
                token = "wrapper"
    return f"obj={token} undeclared={undeclared}"


def _output_contract_reason(
    exc: BaseException,
    *,
    claim_rejection: _ClaimContractRejection | None = None,
    residue_retries: int = 0,
) -> str:
    """The SERVER-AUTHORED ``safe_reason`` for an exhausted output-retry budget.

    Built from the chained ``ValidationError``'s ``loc`` (a path through OUR OWN
    :class:`_StructuredAgenticCompletion` schema) and ``type`` (Pydantic's error-code
    vocabulary). Both are server-owned identifiers, so the sentence says exactly
    WHY the reply was rejected without quoting one byte of it. The error's
    ``input`` and its rendered ``msg`` are deliberately NOT read: they echo the
    model's own output, which may carry customer text (C12). When no
    ``ValidationError`` is reachable the sentence stays generic rather than
    inventing a cause the server did not observe.
    """
    if claim_rejection is not None and claim_rejection.reason is not None:
        # The claim contract rejected the reply, so there is no Pydantic
        # ``ValidationError`` to read: the schema was satisfied and the BINDING
        # was not. Every fragment below is server-authored — the reasons are
        # literals from :data:`_CLAIM_REJECTION_REASONS`, the codes come from a
        # closed table, and the shape is integers plus a fold name — so none of
        # them republishes a byte of the reply (C12).
        # Deliberately short. "the bounded retry did not correct it" was dropped
        # from this sentence because the `Attempts:` fragment below SHOWS the
        # retry — and the room it frees is what lets the shape fragment fit,
        # which is the one fragment that decides whether the next fix is a
        # locator change or a contract change.
        reason = "The model's answer used this Run's sources but could not be bound to them."
        # Fitted one fragment at a time, most readable first. A reason built past
        # the projection's cap is not a longer explanation, it is NO explanation —
        # `_failure_reason` drops it whole and the reader gets `null` again.
        # The claim record alone under-reports what the run actually spent:
        # `Attempts: absent` reads as one round when residue corrections may
        # already have fired. Prepending them makes the sequence the run's, not
        # the class's. Schema retries are pydantic-ai's own error lists and are
        # not counted here — they now spend `answer-shape`'s allowance, never
        # this class's, which is what `_OutputRetryLedger` is for.
        attempts = ",".join(["residue"] * residue_retries + [claim_rejection.sequence()])
        # Each entry is one fragment's renderings, richest first: the first that
        # fits is taken and the rest of that entry is skipped. Only the shape has
        # more than one, and its last is always the shape WITHOUT `rowsentcodes`
        # (NE-ROWSENT-CODES-FIT) — so the codes can shrink or vanish, but they
        # can never take `aw`, `sources`, `maxrun`, `floor` or `rowsent` with
        # them. Same discipline as the `(shaped, entry)` pair on the schema path.
        for renderings in (
            (f"Rejected: {claim_rejection.reason}.",),
            (f"Attempts: {attempts}.",),
            *(
                tuple(f"[{rendering}]" for rendering in _shape_renderings(shape))
                for shape in claim_rejection.shapes[-1:]
            ),
        ):
            for fragment in renderings:
                candidate = f"{reason} {fragment}"
                if len(candidate) <= _MAX_SAFE_REASON_CHARS:
                    reason = candidate
                    break
        return reason
    validation = _chained_validation_error(exc)
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    raw_errors: Sequence[Mapping[str, Any]] = ()
    if validation is not None:
        try:
            raw_errors = validation.errors()
        except Exception:  # pragma: no cover - defensive: never mask the real failure
            raw_errors = ()
        for error in raw_errors:
            code = str(error.get("type", "unknown"))
            location = safe_error_location(error.get("loc", ()), code=code)
            entry = f"{location} [{code}]"
            # NE-OUTPUT-TRANSPORT. ``json_invalid`` against the whole reply is the
            # ONE code whose location tells a reader nothing they did not already
            # know: the reply was not JSON. It cannot say WHICH not-JSON — prose,
            # a fence this build's normalisation could not resolve, an answer cut
            # off at the output budget, two candidate objects — and those want
            # four different fixes. ``rejected_reply_shape`` reads the offending
            # input to describe its SHAPE in a closed, server-owned vocabulary and
            # republishes no byte of it, so the same C12 guarantee that keeps
            # ``msg``/``input`` out of this sentence holds for what replaces them.
            shaped = entry
            offending = error.get("input")
            if code == "json_invalid" and isinstance(offending, str):
                shaped = f"{location} [{code}; {rejected_reply_shape(offending)}]"
            if entry not in seen:
                seen.add(entry)
                rejected.append((shaped, entry))
            if len(rejected) == _MAX_REPORTED_OUTPUT_ERRORS:
                break
    reason = (
        "The model's final reply did not match this Run's required answer format, and "
        "the bounded retry did not correct it. No answer was produced."
    )
    # Name as many rejections as fit UNDER the projection's cap, in the order
    # Pydantic reported them: one named field a reader receives beats five they
    # never see because the whole sentence was dropped for length.
    named: list[str] = []
    for shaped, entry in rejected:
        # Prefer the shape-bearing rendering; fall back to the bare one rather
        # than lose the whole entry to the cap. A reader who learns WHICH slot
        # failed is ahead of a reader who is told nothing because the richer
        # sentence did not fit.
        for candidate_entry in (shaped, entry):
            candidate = f"{reason} Schema rejected: {', '.join([*named, candidate_entry])}."
            if len(candidate) <= _MAX_SAFE_REASON_CHARS:
                named.append(candidate_entry)
                break
        else:
            break
    if not named and residue_retries:
        # A `ModelRetry` leaves no `ValidationError` on the chain, so without this
        # a run that died correcting a malformed tool call would publish the
        # generic sentence and the next reader would lose the one-run diagnosis.
        # The `obj=` token matches the one the schema path emits, deliberately:
        # the same mistake reads the same way whichever seam caught it.
        return (
            "The model's final reply was a malformed tool call rather than an "
            "answer, and the bounded retry did not correct it. No answer was "
            "produced. [obj=tool-call]"
        )
    if named:
        reason = f"{reason} Schema rejected: {', '.join(named)}."
    # NE-ANSWER-SELECT: when the reply was a DIFFERENT object rather than a
    # malformed one, name which. Appended last and only if it fits, on the same
    # discipline as every other fragment — a reason past the projection's cap is
    # not a longer explanation, it is `failure_reason: null` again.
    if any(error.get("type") == "extra_forbidden" for error in raw_errors):
        candidate = f"{reason} [{_rejected_object_shape(raw_errors)}]"
        if len(candidate) <= _MAX_SAFE_REASON_CHARS:
            reason = candidate
    return reason


#: The correction the one remaining claim rejection earns. Static protocol text
#: only — no model or customer content (C12), and no hint about what the answer
#: should have said. The last sentence is load-bearing: a retry that pressures
#: the model to produce a quotation at any cost is a retry that teaches it to
#: fabricate one. A run that genuinely cannot quote must still fail, so the way
#: OUT of this retry is to say the sources do not state the point, never to
#: invent a quotation that does.
_CLAIM_CORRECTION_BODIES: Final = {
    _CLAIM_NO_COMMON_SPAN: (
        "No span of your final_text appears verbatim in any of this run's tool-result chunk "
        "texts, so the server cannot bind your answer to the sources it used, and an unbound "
        "answer over sources is rejected. ONE string has to do BOTH jobs: it must be copied "
        "character for character out of a tool result's chunk text, AND it must appear inside "
        "final_text. Re-send the SAME JSON object with final_text rewritten to QUOTE at least "
        "one relevant source sentence of five or more consecutive words, like this: The source "
        'states: "<the source sentence, copied exactly>" — then your own analysis around the '
        "quotation. The server finds the quoted words itself: declare no claims, no ids, no "
        "offsets and no hashes. An answer written entirely in your own words cannot be "
        "published over sources; and if no source sentence states your point, say the sources "
        "do not state it rather than invent a quotation. If the result you used is a TABLE OF "
        "ROWS, write one short sentence per row that pairs the row's id with a column name and "
        "its value copied exactly as served -- like: id-7 has credit_amount_cents 20000 -- ONE "
        "row per sentence, never two rows joined in one sentence, values in the units served "
        "(write 20000, never $200.00), never a value the table does not show; or copy one full "
        "row line exactly as served, pipes and all."
    ),
}

#: The correction a rejection with no entry above earns. Reachable only if a new
#: reason is added without one, so it says the rule and nothing invented.
_CLAIM_CORRECTION_DEFAULT = (
    "Re-send the SAME JSON object with final_text rewritten to contain at least one sentence "
    "copied character for character out of one of this run's tool-result chunk texts."
)


def _claim_correction(reason: str) -> str:
    """The corrective feedback ONE rejection earns, and only that one.

    Server-authored throughout: the preamble is a literal and the body is looked
    up by the rejection (also a literal). Nothing model- or customer-derived is
    interpolated (C12) — under NE-SPAN-BIND there are no ids to hand back,
    because the model no longer declares any.
    """
    return (
        f"Your final answer could not be bound to this run's sources: {reason}. "
        f"{_CLAIM_CORRECTION_BODIES.get(reason, _CLAIM_CORRECTION_DEFAULT)}"
    )


#: How many rejections the record keeps. The output-retry budget is 1, so a run
#: can produce at most 2; the slack is so a future budget change cannot silently
#: turn this list into an unbounded accumulator on a hot failure path.
_MAX_RECORDED_REJECTIONS: Final = 4


@dataclass
class _ClaimContractRejection:
    """Every static reason the claim validator sent the model back for, in order.

    pydantic-ai reports an exhausted output-retry budget as
    ``UnexpectedModelBehavior`` naming only the retry count, and a
    :class:`~pydantic_ai.ModelRetry` leaves no ``ValidationError`` on the
    exception chain for :func:`_output_contract_reason` to read. Recording at the
    raise site is therefore not a convenience — it is the only place the cause
    still exists, and it is what keeps this failure from publishing
    ``failure_reason: null`` all over again.

    It keeps the SEQUENCE rather than the last reason alone, and that distinction
    is the whole point. ``omitted`` then ``absent`` says the retry WORKED — it
    taught the model to declare claims — and then had no budget left to correct
    the quote; the fix there is the retry budget. ``absent`` then ``absent`` says
    the model was told exactly what was wrong and could not comply; the fix there
    is the contract. The two are indistinguishable from the last rejection alone,
    which is precisely the ambiguity that made this failure hard to read.
    """

    reasons: list[str] = field(default_factory=list)
    shapes: list[str] = field(default_factory=list)

    def record(self, reason: str, *, shape: str | None = None) -> None:
        if len(self.reasons) >= _MAX_RECORDED_REJECTIONS:
            return
        self.reasons.append(reason)
        if shape is not None:
            self.shapes.append(shape)

    @property
    def reason(self) -> str | None:
        """The rejection the run actually died on."""
        return self.reasons[-1] if self.reasons else None

    def sequence(self) -> str:
        """The attempts as short codes, e.g. ``omitted,absent``."""
        return ",".join(_CLAIM_REJECTION_CODES.get(r, "other") for r in self.reasons)


def _install_claim_contract_validator(
    agent: Agent[None, _StructuredAgenticCompletion],
    *,
    served_refs: set[str],
    retention: ServedTextRetention | None,
    ledger: _OutputRetryLedger,
) -> _ClaimContractRejection:
    """Enforce the claim contract WHERE THE MODEL CAN STILL FIX IT.

    The rule — an answer built on this run's sources must contain, verbatim,
    words those sources actually served — used to be enforceable only after the
    model was gone. This runs :func:`_server_bound_claims` at generation time,
    so the one way a source-bearing answer can still fail (nothing in it is
    verbatim in anything served) is caught while one bounded correction can
    still teach the model to quote its source.

    Enforcement is STRENGTHENED, never relaxed: the same
    :func:`_server_bound_claims` runs here and again on the returned completion,
    and a run that cannot be bound still fails — it just fails naming itself.
    ``served_refs`` is the recorder's live set and ``retention`` is the run's
    own delivered-text buffer, both of which by output-validation time hold
    everything this run served and delivered.

    The correction is charged to ``claim-contract``'s OWN allowance in ``ledger``,
    which is why a reply that cannot be bound still gets its round-trip after
    the shape correction has fired every time it is allowed to.
    """
    rejection = _ClaimContractRejection()

    @agent.output_validator
    def _enforce_claim_contract(
        completion: _StructuredAgenticCompletion,
    ) -> _StructuredAgenticCompletion:
        try:
            _server_bound_claims(
                completion.final_text, served_refs=served_refs, retention=retention
            )
        except ActionValidationError as exc:
            # Recorded BEFORE the charge: an allowance that runs out here must
            # still publish the rejection it died on, not a bare retry count.
            rejection.record(str(exc), shape=getattr(exc, "span_shape", None))
            ledger.spend("claim-contract")
            raise ModelRetry(_claim_correction(str(exc))) from exc
        return completion

    return rejection


#: This loop's residue correction: the shared call shape, then the sentence that
#: is TRUE HERE. The session path tells the model to answer in plain text; this
#: loop rejects a plain-text final answer, so it must point at the transport
#: instead. Same predicate, same call shape, opposite tail — see
#: :data:`~ne.runtime.model.PROTOCOL_RESIDUE_CALL_CORRECTION`.
_RESIDUE_RETRY = (
    f"{PROTOCOL_RESIDUE_CALL_CORRECTION} If you did not intend to call a tool, "
    "reply with the FINAL RESPONSE TRANSPORT JSON object described in your "
    "instructions — a plain-text reply is not a valid final answer here."
)


@dataclass
class _ProtocolResidueCapability(AbstractCapability[object]):
    """Turn a malformed in-band tool call into a correction, not an answer.

    NE-RESIDUE-RETRY. In-band tool calling (D3) means a tool call IS a JSON
    object in a text reply. ``GatewayModel._parse`` turns a well-formed one into a
    ``ToolCallPart``; a MALFORMED one is demoted to text by the parse-never-raises
    design, and pydantic-ai then processes that residue as the FINAL ANSWER. It
    fails the schema as an undeclared key with no ``final_text``, and the run dies
    holding eight unused tool calls because the model fumbled the protocol once
    and had no way back.

    ``before_output_validate`` is pydantic-ai's documented hook for exactly this:
    it sees the raw reply before parsing, and a :class:`ModelRetry` raised here
    becomes a ``RetryPromptPart`` carrying THIS message rather than a dump of
    Pydantic errors that never mentions the tool protocol.

    Why a capability and not an output validator: an output validator runs only
    on a reply that ALREADY parsed into the output model. Residue never does — it
    fails schema validation first — so the session path's validator seam does not
    exist here. The predicate is still the shared one, so the two paths cannot
    disagree about what residue is.

    The count is kept because an exhausted budget must still name its cause: a
    ``ModelRetry`` leaves no ``ValidationError`` on the chain for
    :func:`_output_contract_reason` to read.

    It also carries the run's :class:`_OutputRetryLedger`, because this is the
    one object pydantic-ai hands BOTH of the non-claim violation classes:
    ``before_output_validate`` sees residue before parsing, and
    ``on_output_validate_error`` sees the schema rejection that residue is not.
    Charging each to its own allowance here is what keeps either of them from
    spending the ``claim-contract`` slot.
    """

    ledger: _OutputRetryLedger
    residue_retries: int = 0
    wrapped_call_retries: int = 0

    async def before_output_validate(
        self,
        ctx: Any,
        *,
        output_context: Any,
        output: Any,
    ) -> Any:
        if isinstance(output, str) and is_malformed_tool_call_residue(output):
            self.residue_retries += 1
            self.ledger.spend("protocol-residue")
            raise ModelRetry(_RESIDUE_RETRY)
        return output

    async def on_output_validate_error(
        self,
        ctx: Any,
        *,
        output_context: Any,
        output: Any,
        error: Any,
    ) -> Any:
        """Charge pydantic-ai's OWN schema rejection to the class it BELONGS to.

        This hook fires for the ``ValidationError`` raised by parsing the reply
        into ``_StructuredAgenticCompletion`` — the one violation class this loop
        never raises itself and therefore never used to count. Re-raising the
        error unchanged leaves pydantic-ai's behaviour byte-identical (it still
        becomes a ``RetryPromptPart`` carrying the library's own error list);
        the charge is what stops a shape rejection from taking the slot the claim
        contract is holding.

        NE-WRAPPED-CALL-CLASS — two rejections arrive here, not one. A reply that
        is a WELL-FORMED lone tool call the model wrapped in prose failed on
        TRANSPORT, not on answer shape: it is exactly the reply that earns
        ``wrapped_tool_call_retry_instruction`` one layer down, and charging it
        to ``answer-shape`` was the last seam that still disagreed with every
        other one about which mistake it is (see the table at
        :data:`_OUTPUT_RETRY_ALLOWANCES`). It is charged to its own ladder-sized
        class here, so a long ladder stops draining a budget the model never
        meant to touch.

        ``output`` is the RAW reply pydantic-ai was asked to validate — the same
        value :meth:`before_output_validate` inspects — so the predicate reads
        the reply itself rather than re-deriving it from the error's ``input``.
        The two predicates are DISJOINT by construction: residue is a LONE
        tool-call object (the whole stripped reply, or its first line), and a
        wrapped call is one that is NOT the whole reply, so no rejection can be
        charged twice.

        This SELECTS A BUDGET CLASS and nothing else. Nothing is promoted, no
        transport widens, and the reply is re-raised unchanged: a prose-wrapped
        call still executes NOTHING (NE-CALL-SELECT, reverted 2026-08-29 — see
        :func:`~ne.runtime.model.sole_answer_object`).

        The output validators run in the PROCESS hooks, not these, so a claim
        rejection cannot arrive here and be double-charged.
        """
        if isinstance(output, str) and is_prose_wrapped_tool_call(output):
            self.wrapped_call_retries += 1
            self.ledger.spend("wrapped-call", cause=error)
        else:
            self.ledger.spend("answer-shape", cause=error)
        raise error


def _wall_clock_terminal(recorded: _RecordedExecutor) -> ModelPlannedRun:
    """The one wall-clock-exhaustion terminal, built in ONE place.

    ONE arm reaches it: the ``TimeoutError`` ``asyncio.timeout`` raises when it
    converts its own cancellation. It used to be two — the ``CancelledError``
    arm in ``run_model_planned_agent`` returned this terminal as well — and that
    was the defect, not the design: the only way that arm can see
    ``wall_clock.expired()`` is a cancellation this scope did not issue, so
    everything it published here was an outside cancellation reported as a
    ceiling. It now raises ``RunCancellationRequested`` instead; the argument is
    at that arm.

    The helper stays because the terminal is still worth having one author: it
    is the shape a wall-clock exhaustion publishes, and a second,
    separately-written copy would be a second way to say one thing.

    No partial draft is ever returned as an answer: ``output`` stays at its
    empty default, exactly as the other budget terminals do.
    """
    return ModelPlannedRun(
        outcome="budget-exhausted",
        steps=tuple(recorded.steps),
        served_row_ids=tuple(sorted(recorded.served)),
        tool_calls_used=recorded.tool_calls,
        exhausted_cap="wall-clock",
    )


async def run_model_planned_agent(
    *,
    task: str,
    instructions: str,
    principal: Principal,
    executor: ToolExecutorLike,
    gateway: ModelGateway,
    settings: RuntimeSettings,
    sink: AuditSink,
    max_tool_calls: int,
    total_tokens_limit: int,
    wall_clock_seconds: float,
    max_parked_writes: int,
    boundary: RunExecutionBoundary = _NOOP_BOUNDARY,
    source_guard: ProtectedSourceResultGuard | None = None,
    message_history: Sequence[ModelMessage] = (),
) -> ModelPlannedRun:
    """Run a bounded, single-principal model-planned tool loop.

    ``executor`` is expected to be a handler-less scoped view over the platform's
    frozen :class:`GovernedToolExecutor`.  The recorder enriches only the model-facing
    schemas and strips its two metadata fields before delegating, so every business
    argument and every actual tool execution still traverses the unchanged C6 path.

    Cap hits are typed terminal outcomes.  They never return the model's partial
    draft as an answer.  Context overflow remains distinct because it says the
    current turn could not be represented without truncation; all spend/fan-out/time
    ceilings share the honest ``budget-exhausted`` terminal.

    NE-LOOP-NOPROGRESS: a run whose reads stop finding anything raises
    :class:`~ne.runtime.errors.AgenticNoProgressError` out of this function rather
    than returning a ``ModelPlannedRun``, and that is deliberate.  It is NOT a
    budget terminal — no ceiling was reached — so it must not borrow one; a reader
    told "budget exhausted" would conclude the run was cut short with more to find,
    which is the opposite of what happened.  The error carries a ``safe_reason``
    the Run workflow publishes verbatim (``execution_failed``/``error_reason``), so
    the turn states why it stopped instead of ending on a generic line.  It is
    never converted into an answer: an agent that found no evidence says so.

    A cancellation this call did not issue also leaves as an exception rather
    than a terminal.  When it lands WITH the wall-clock deadline it is raised as
    :class:`~ne.runtime.run_runner.RunCancellationRequested`, which the Run
    workflow terminalises as CANCELLED; when it lands without one the
    ``CancelledError`` propagates untouched.  Neither is reported as
    ``budget-exhausted``: a ceiling nobody hit is not what stopped the run.  The
    argument, and the measurement behind it, are at the handler.
    """
    if max_tool_calls < 1:
        raise ValueError("max_tool_calls must be positive")
    if total_tokens_limit < 1:
        raise ValueError("total_tokens_limit must be positive")
    if wall_clock_seconds <= 0:
        raise ValueError("wall_clock_seconds must be positive")
    if max_parked_writes < 0:
        raise ValueError("max_parked_writes must not be negative")

    # NE-SPAN-BIND: the run's OWN retention of the authorized text its
    # deliveries actually carry, and the run-scoped guard view that fills it.
    # Everything downstream of this frame sees only `retained_guard`, so every
    # authorized Tool-result delivery of this run is recorded under the same
    # reference_id the model may quote. The buffer's whole lifetime is this
    # call: no other object outlives the `finally` below holding it, and the
    # explicit clear() means even a leaked reference to the retention object
    # holds no text once the run has returned.
    retention: ServedTextRetention | None = None
    retained_guard = source_guard
    if source_guard is not None:
        retention = ServedTextRetention()
        retained_guard = ClaimRetainingSourceGuard(source_guard, retention)
    try:
        recorded = _RecordedExecutor(
            inner=executor,
            principal=principal,
            sink=sink,
            boundary=boundary,
            source_guard=retained_guard,
            max_tool_calls=max_tool_calls,
            max_parked_writes=max_parked_writes,
        )
        request_limit = model_request_limit(max_tool_calls)
        model_settings = settings.model_copy(update={"max_requests": request_limit})

        with run_trace_scope(), run_principal_scope(principal.id):
            tools = [build_governed_tool(recorded, spec, principal) for spec in recorded.tool_specs]
            # ONE ledger for the whole run: the residue capability charges the two
            # classes pydantic-ai routes through it, the claim validator charges its
            # own reserved slot, and the pool below is their SUM.
            retry_ledger = _OutputRetryLedger(
                allowances=dict(output_retry_allowances(max_tool_calls))
            )
            residue = _ProtocolResidueCapability(ledger=retry_ledger)
            agent = Agent(
                model=_StructuredAgenticGatewayModel(
                    gateway,
                    model_settings,
                    on_unknown_tool=build_unknown_tool_hook(recorded, principal),
                    budget=model_settings.context_budget,
                    sink=sink,
                    source_guard=retained_guard,
                    preserve_history=bool(message_history),
                ),
                output_type=PromptedOutput(
                    _StructuredAgenticCompletion,
                    name="structured_agentic_completion",
                    description=(
                        "Final answer text. When tool results carried source material, quote "
                        "at least one source sentence verbatim inside final_text; the server "
                        "finds the quoted words and binds the claims itself. Declare nothing "
                        "else."
                    ),
                ),
                tools=tools,
                instructions=instructions,
                # Exactly the budget model_request_limit reserved headroom for.
                retries={"output": output_retries(max_tool_calls)},
                capabilities=[residue],
            )
            claim_rejection = _install_claim_contract_validator(
                agent,
                served_refs=recorded.served,
                retention=retention,
                ledger=retry_ledger,
            )
            # POST_DEMO item 12, increment 1c. THE MARKER THAT ACTUALLY DIVIDES
            # UNKNOWN WORK, and it is here because the previous one did not.
            #
            # `profile:adapter-before` (worker, immediately above `run_adapter.run`)
            # was shipped as "the marker that splits the 7.67 s edge". An
            # adversarial pass showed it does not: the region between the
            # production `profile:agentic-before` and it contains a comment block,
            # two attribute reads, a thin wrapper constructor and two ContextVar
            # scopes — no I/O of any kind — so that split is 0 / 7.67 s, knowable
            # by reading the file and worth no round trip to learn. Worse, each
            # heartbeat is itself a synchronous transaction, so the prefix it
            # reports is dominated by the PREVIOUS heartbeat's own round trips: it
            # measures the instrument.
            #
            # Everything genuinely unmeasured is on THIS side of the adapter call.
            # Between entering it and `run-model:before:0` sits real work: one
            # `build_governed_tool` per granted spec (each generating a schema),
            # the `Agent` construction over them, and pydantic-ai's own graph
            # startup and prompt assembly. This boundary lands between the two —
            # after the agent is built, before it is run — so the edge partitions
            # into "build the tools and the agent" versus everything after it.
            #
            # ⛔ TWO HONEST LIMITS ON THAT, both caught by an adversarial pass.
            #
            # (1) THE RIGHT-HAND SIDE IS NOT "everything pydantic-ai does before
            # its first model request" ON THE PRODUCTION PATH. On a
            # continuity-active run `_RunContinuityGateway.complete()` sits in
            # between and does POLICY work first -- the catalog/frozen-policy
            # recheck, then `choices()`, `verify_model_boundary()` and
            # `authorize()`. That is exactly why `profile:policy-recheck-before`
            # exists, and this comment used to describe the edge as though it did
            # not. On the 12-row continuity floor -- the PRODUCTION floor -- the
            # mis-description was the one always in force.
            #
            # (2) THE LEFT-HAND SIDE HAS NO I/O, which is the same property that
            # demoted `profile:adapter-before` from "the splitter" to "a fence".
            # Between the two: argument validation, a retention buffer, a
            # dataclass, `model_copy`, two ContextVar scopes, N
            # `build_governed_tool` schema builds, `Agent(...)` and the validator
            # install.
            #
            # ⛔ AND MY FIRST WORDING OF THIS LIMIT WAS BACKWARDS -- caught by an
            # adversarial pass that read the RIGHT-hand side, which I had not. I
            # wrote that this marker's value "rests entirely on N pydantic schema
            # generations being non-trivial, asserted and never measured". The
            # opposite is true: the right-hand side carries **the only I/O in the
            # region**, so the marker is well placed and my stated reason
            # inverted it.
            #
            # WHAT IS ACTUALLY ON THE RIGHT. Before `_RunContinuityGateway`'s
            # policy work is even reached, `GatewayModel.request` runs
            # `source_guard.materialize_history(...)` -- a protected-store read
            # plus a PDP decision PER REFERENCE -- then the context-budget trim,
            # then `source_guard.reauthorize_history(...)`, a SECOND full pass and
            # the one that CLAIMS: durable delivery receipts and audit rows are
            # minted there. ⚠️ THIS USED TO SAY "that work scales with
            # conversation length, i.e. with `continuity_active`", and BOTH halves
            # were wrong. `continuity_active` is `run.model_policy_id is not None`
            # -- an Agent-configuration fact, not a turn count. And the work scales
            # with neither: `materialize_history`/`reauthorize_history` do I/O only
            # for messages carrying PROTECTED REFERENCES, while
            # `materialize_conversation_history` renders source-free prior turns as
            # plaintext in the worker, leaving nothing to resolve at send time. So
            # on the FIRST turn of any conversation -- and on any conversation
            # whose prior turns were source-free -- the named I/O here is ZERO
            # while `continuity_active` is true. It scales with the number of
            # PROTECTED REFERENCES in the history, which is the thing to say.
            #
            # ⚠️ THAT IS TRUE OF THE TWO PROTECTED-HISTORY PASSES AND NOT OF THE
            # TRIM BETWEEN THEM, which this paragraph named and then left out of
            # its own I/O account. `_trim` awaits `_emit_budget` whenever it drops
            # a turn or refuses (`model.py:444`, `:452`, `:469`), and that is an
            # audit-sink `append` that FAILS CLOSED (`model.py:527`). Its frequency
            # DOES track history size -- so on the long conversation, the only case
            # where this gap is worth triaging, the region carries an audit append
            # per model request that "scales with protected references" excludes.
            #
            # So by this increment's own criterion (no I/O => a fence, not a
            # splitter), the interval that most deserves its own marker is
            # `agent-built -> materialize_history`, and this change does not add
            # it. Recorded rather than added: it is new instrumentation, and this
            # PR has already spent twelve rounds.
            #
            # The uncalibrated-instrument caveat still holds: each heartbeat is a
            # synchronous transaction whose `observed_at` is the DB clock at
            # transaction START, so `adapter-before -> agent-built` is an UPPER
            # BOUND on construction, never its cost.
            await boundary.heartbeat("profile:agent-built")
            # Constructed OUTSIDE the try so the `except asyncio.CancelledError`
            # arm below can always read `wall_clock.expired()`. The deadline
            # still starts at `__aenter__`, so this changes no timing.
            wall_clock = asyncio.timeout(wall_clock_seconds)
            try:
                async with wall_clock:
                    result = await agent.run(
                        task,
                        message_history=list(message_history),
                        usage_limits=UsageLimits(
                            request_limit=request_limit,
                            tool_calls_limit=max_tool_calls,
                            total_tokens_limit=total_tokens_limit,
                        ),
                    )
            except ContextBudgetExceededError:
                return ModelPlannedRun(
                    outcome="context-exhausted",
                    steps=tuple(recorded.steps),
                    served_row_ids=tuple(sorted(recorded.served)),
                    tool_calls_used=recorded.tool_calls,
                    exhausted_cap="context",
                )
            except TimeoutError:
                return _wall_clock_terminal(recorded)
            except asyncio.CancelledError as exc:
                # THE STRANDED-RUN DEFECT. `asyncio.timeout` converts its own
                # cancellation into `TimeoutError` only when
                # `task.uncancel() <= self._cancelling` — i.e. only when nothing
                # else cancelled this task in the same window. When something
                # did (measured: a second `cancel()` on the run's task landing at
                # the deadline instant makes the escape deterministic, 200/200 —
                # and DBOS cancelling the workflow task is exactly that second
                # party: "Asyncio task cancelled for workflow or step"), the
                # conversion is skipped and a raw `CancelledError` comes out
                # here. `CancelledError` derives from `BaseException`, so it was
                # caught by NOTHING on this path: DBOS recorded nothing, the run
                # row kept `status=running` with `terminal_at=NULL`, and the run
                # was stranded forever. Twelve were settled by hand.
                #
                # ⛔ AND THE FIRST FIX SWALLOWED THE CANCELLATION IT MEANT TO
                # PRESERVE. It returned the wall-clock terminal whenever
                # `wall_clock.expired()`, on the stated grounds that `expired()`
                # "is true exactly when THIS scope's deadline passed and this
                # scope issued the cancel". That is false, and the way it is
                # false is the premise of this whole arm. `Timeout.expired()` is
                # true for EXPIRING *or* EXPIRED, and `__aexit__` sets EXPIRED on
                # the expiring path INCLUDING the branch where
                # `task.uncancel() > self._cancelling` and the `TimeoutError`
                # conversion is therefore skipped. Measured, 40/40 deterministic
                # on CPython 3.12:
                #
                #     A deadline only ............... TimeoutError, expired=True
                #     B outside cancel, deadline far  CancelledError, expired=False
                #     C deadline AND outside cancel   CancelledError, expired=True
                #
                # Case A never reaches this arm at all — it converts. So the ONLY
                # way to arrive here with `expired()` true is case C, and case C
                # is BY CONSTRUCTION a cancellation this scope did not issue: the
                # second cancel is the very thing that defeated the conversion.
                # The old guard therefore fired only, always and exactly on an
                # outside cancellation — it consumed one every time, returned
                # normally, published `budget-exhausted`/`wall-clock`, and left
                # DBOS awaiting a task it had cancelled and getting a result.
                #
                # WHAT WINS, AND WHY. Two things are true here and only one can be
                # published: this scope's deadline expired, and another party
                # cancelled the task. The cancellation wins. A deadline is a
                # MEASUREMENT and is recoverable from the run's own timings; a
                # cancellation is an INSTRUCTION from another party, recoverable
                # from nowhere else, and erasing it leaves the durable row
                # disagreeing with the party that issued it — an operator who
                # pressed Cancel reading `budget-exhausted` is being told a
                # ceiling stopped a run that was stopped by them.
                #
                # It is raised as `RunCancellationRequested` and NOT re-raised as
                # `CancelledError`, because a raw `CancelledError` off this path
                # is caught by nothing (it is a `BaseException`) and IS the
                # stranded-run defect: `status=running`, `terminal_at=NULL`,
                # twelve settled by hand. `RunCancellationRequested` is the
                # sanctioned in-loop cancellation signal — `_LeaseExecutionBoundary
                # .heartbeat` already raises this exact type from inside this same
                # call stack — so `run_workflow` terminalises it as CANCELLED and
                # publishes. One cancellation, delivered two ways (at a
                # cooperative boundary, or as a task cancel between boundaries),
                # published as one terminal.
                #
                # `uncancel()` is NOT called. This scope's own `__aexit__` already
                # spent exactly one, so the outside cancellation's count is intact
                # for whatever encloses this run; decrementing it here would be
                # the corruption of an outer scope's accounting that an earlier
                # agent deliberately avoided.
                #
                # ⚠️ DECLARED RESIDUAL, both halves. (1) Provenance is NOT
                # observable: `Task.cancelling()` is a count, not a cause, so an
                # operator cancel, a shutdown and an outer deadline are one case
                # to this code and all three publish CANCELLED. That is the safe
                # direction — an instruction is never erased — and the cost is
                # that a run which really did exhaust its wall clock while some
                # unrelated party cancelled it reports `cancelled` rather than
                # `budget-exhausted`. (2) Case B is deliberately left alone: this
                # scope's deadline did not fire, so it has nothing of its own to
                # publish and does the structurally correct thing instead. That a
                # bare `CancelledError` may still reach a caller that records no
                # terminal is a KNOWN and untouched limitation of this arm, not a
                # claim that case B is safe.
                if not wall_clock.expired():
                    raise
                raise RunCancellationRequested(
                    "run cancellation observed while the wall-clock deadline was expiring"
                ) from exc
            except UnexpectedModelBehavior as exc:
                # pydantic-ai exhausted its output-retry budget: the model's final
                # reply failed the declared output contract on every attempt. This is
                # a FAILURE, not a terminal cap outcome — an unvalidatable reply is
                # never repaired into an answer and no partial draft is returned. It
                # is re-raised as a typed error ONLY so it carries a `safe_reason`;
                # without one, `run_workflow` records `error_class` alone and the run
                # publishes `failure_reason: null`, leaving the reader a bare status
                # word for a failure the server could describe precisely.
                raise AgentOutputContractError(
                    _output_contract_reason(
                        exc,
                        claim_rejection=claim_rejection,
                        residue_retries=residue.residue_retries,
                    )
                ) from exc
            except UsageLimitExceeded as exc:
                message = str(exc)
                cap = (
                    "parked-writes"
                    if "max_parked_writes" in message
                    else "tool-calls"
                    if "tool call" in message or "max_tool_calls" in message
                    else "tokens"
                    if "token" in message
                    else "requests"
                )
                return ModelPlannedRun(
                    outcome="budget-exhausted",
                    steps=tuple(recorded.steps),
                    served_row_ids=tuple(sorted(recorded.served)),
                    tool_calls_used=recorded.tool_calls,
                    exhausted_cap=cap,
                )

        usage = result.usage
        completion = result.output
        claims = _server_bound_claims(
            completion.final_text, served_refs=recorded.served, retention=retention
        )
        return ModelPlannedRun(
            outcome="complete",
            output=completion.final_text,
            steps=tuple(recorded.steps),
            claims=claims,
            served_row_ids=tuple(sorted(recorded.served)),
            requests_used=usage.requests,
            tool_calls_used=recorded.tool_calls,
            tokens_used=usage.total_tokens,
        )
    finally:
        if retention is not None:
            retention.clear()


def _to_plain_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-convert a (possibly FrozenDict) JSON schema to plain dicts/lists.

    ``pydantic_ai.tools.Tool.from_schema`` needs a mutable ``JsonSchemaValue``;
    an empty schema defaults to the permissive ``{'type':'object','properties':{}}``.
    """
    plain = _plain(schema)
    if not plain:
        return {"type": "object", "properties": {}}
    return plain  # type: ignore[return-value]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Sequence):
        return [_plain(v) for v in value]
    return value
