# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-WORKFLOWS — governed multi-step agent runs over the customer's own records.

A governed WRITE proves the gate. A governed WORKFLOW proves the product: several
governed steps, in one correlated run, where each step's OUTPUT is the next step's
INPUT and the whole chain is refusable at any link.

Every step in this module goes through the ONE frozen
:class:`~ne.runtime.middleware.GovernedToolExecutor` the chat surface uses (C6) —
guardrails, PDP, HITL gate, audit. There is no workflow-only tool, no workflow-only
executor, and no path here that reaches a connector without passing the same gates a
chat turn passes. A workflow that could not be refused would be a bypass; if you find
one, it is a defect.

**What is deterministic and what is a model call.** The PLAN is trusted server code:
which tools run, in which order, with which arguments. The JUDGEMENT legs — drafting
the prose a human will read — are governed sub-agent dispatches through
:class:`~ne.runtime.dispatch.GovernedSubAgentDispatcher`. This is the
``routers/demo.py`` precedent, and it is deliberate: a model that chooses the tool
sequence is a model that can be talked into choosing a different one, and the
deployment's private CPU model cannot reliably chain tool calls inside one turn
anyway. Nothing here pretends the model wrote the plan.

**Who each leg executes as (the honest split).** The reads and the Tier-2 write
execute — and are audited — as the CALLING principal: ACL-trimmed to what that
person may see, attributed to them in the approval queue. The DRAFTING leg does
NOT: it executes and is audited as the service identity ``agent:workflow-drafter``
(the dispatcher's construction principal), which is the one leg that receives
customer content in its task text. The two actors are linked by the run's one
``trace_id``. Pinned by ``test_drafting_leg_is_audited_as_the_service_identity``
(test_workflows_api.py) — which actor each leg's audit rows carry is a tested
fact, not a slogan.

**Where the numbers come from.** Every quantity a workflow states — ticket counts,
priorities, account names, whether a contract was retrievable — is read from a served
field in this run: a verified T3 answer row, or a retrieved chunk's own text. No
workflow computes a figure the platform did not just serve it, and none carries a
constant that stands in for customer data. A subject the read did not return is a
subject the workflow does not act on.

**Why the writes are constructed, not prompted.** Each proposal's ``record_id`` /
``title`` / ``body`` are assembled by this module and validated
(:meth:`Proposal.validated`) BEFORE the write is submitted. A workflow therefore
cannot emit the malformed, field-missing tool call a free-running model can — the
class of defect where an incomplete call reaches the approval queue and only fails
after a human has spent a decision on it.

**The parked-write durability limit (honest, load-bearing).** The write leg gates
at Tier-2 — not because ``write.internal.crm.note`` declares it (the action is
correctly Tier-1, its class tier, per NE-CS-SEND) but because the WORKFLOW owes
each subject a human decision and asserts that floor itself
(NE-WF-SUBJECT-GATE, ``SUBJECT_WRITE_GATE_TIER`` +
:func:`~ne.runtime.middleware.write_approval_floor_scope`):
:meth:`GovernedToolExecutor.execute` submits the approval row and
then AWAITS the human decision. A run therefore launches its writes as tasks and
returns once each approval row is durably recorded — it does not block until a human
decides. The approval ROW is durable (Postgres); the in-process waiter is NOT. If
ne-api restarts while a workflow write is parked, the row survives and remains
visible in the queue, but approving it will not fire the CRM write, because the task
that was waiting on it is gone. Chat turns do not have this gap: they run under DBOS
in ne-worker. Routing workflow runs through that same durable path is the named
follow-up; until it lands, decide a workflow's approvals in the same session that
raised them.

**The no-orphan property: what is proven, and two DECLARED residuals.** A run should
never park an approval whose request id the caller does not receive. Proven: a
parked call that dies BEFORE any row exists reports the failure class immediately,
and one that dies AFTER its row is visible reports the row's id
(``test_task_death_after_park_reports_the_parked_row_id``). Two orphan paths
remain, declared rather than implied. (1) The gate READ
(:meth:`WorkflowContext.known_request_ids`) raising after the gate WRITE already
parked the row: the subject degrades to a per-proposal ``failed`` naming the read's
error class, and the parked id is not in the response. (2) The unchanged
``unconfirmed`` timeout: a row that first becomes visible after the 20s window is
parked but unreported. What defeats both: the row is durable governed state,
visible in the approvals queue (``list_pending`` and the audited payload
disclosure) under the same principal and action, so an operator reconciles from
the queue — and deciding such a row is safe, because at worst its in-process
waiter is gone, which is exactly the restart gap declared above. Closing them for
real needs a per-call request-id handle on the gate's public surface; none exists,
and a workflow-only accessor into the approval store would be a second read path
into governed state (see :meth:`WorkflowContext.new_request_id`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel

from ne.contracts.runtime import SubAgentSpec
from ne.contracts.source_authority import (
    ProtectedSourceExecutionWriter,
    ProtectedSourceOutputWriter,
)
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.errors import GuardrailViolation
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.correlation import action_scope
from ne.runtime.dispatch import AgentDefinition, GovernedSubAgentDispatcher
from ne.runtime.errors import (
    ActionIdempotencyError,
    ActionValidationError,
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    ApprovalPolicyError,
    ApprovalWindowUnhonourableError,
    RunApprovalContinuationExhaustedError,
    ToolDeniedError,
    ToolNotRegisteredError,
)
from ne.runtime.middleware import GovernedToolExecutor, write_approval_floor_scope
from ne.runtime.source_results import (
    ProtectedSourceResultGuard,
    SourceAuthorityRetryableError,
    SourceAuthorityWithheldError,
)

_LOG = logging.getLogger(__name__)

__all__ = [
    "MAX_SUBJECTS_CEILING",
    "WORKFLOWS",
    "ProposalView",
    "StepView",
    "WorkflowResultView",
    "WorkflowRunView",
    "WorkflowView",
    "run_workflow",
    "workflow_agent_definitions",
]

#: Server-side cap on how many subjects one run may act on. A run parks one Tier-2
#: approval per subject, so an uncapped run is a way to flood a human approval queue
#: from one request. The ceiling is enforced HERE, in :func:`run_workflow`, so EVERY
#: caller inherits it — the HTTP route's ``Query(le=...)`` merely surfaces it as a
#: 422 before a run starts; a future second caller (worker cron, CLI) cannot opt
#: out by not being the route. Pinned by ``test_engine_clamps_subjects_to_the_
#: ceiling`` (the count that matters: approvals actually parked).
MAX_SUBJECTS_CEILING = 5

#: The governed tools a workflow may call. Named here so the set a workflow can reach
#: is readable in one place; the executor remains the authority that enforces it.
TICKETS_TOOL = "read.kg.tickets_by_status"
KNOWLEDGE_TOOL = "read.knowledge.search"
CRM_LOOKUP_TOOL = "read.external.crm.account"
CRM_NOTE_TOOL = "write.internal.crm.note"

#: NE-WF-SUBJECT-GATE — the workflow-owned per-subject human gate (owner ruling).
#: Every subject a workflow proposes a write for gets its OWN parked human
#: decision, and the write does not fire until that decision is APPROVED —
#: INDEPENDENT of the action's tier binding, because the obligation is the
#: workflow's, not the action's. The engine had been borrowing the CRM note's
#: own Tier-2 declaration as its gate, so when ``write.internal.crm.note``
#: correctly moved to its Tier-1 class (NE-CS-SEND, 1df6a113 — a ruling this
#: module must NOT undo by re-tiering the action) every workflow write silently
#: went autonomous. The floor below is the explicit, tier-change-proof form:
#: the write legs enter ``write_approval_floor_scope`` with it, which escalates
#: ``write.*`` calls to gate at >= Tier-2 while leaving the action's tier — and
#: every read and draft — untouched.
SUBJECT_WRITE_GATE_TIER = ApprovalTier.TIER_2

# Meridian BD-5 Tier-0 reads.  The B1/B2/B3 runners below reach them only through
# WorkflowContext.call -> the platform's one GovernedToolExecutor.
MERIDIAN_CUSTOMERS_TOOL = "read.kg.customers_in_counties"
MERIDIAN_LOANS_TOOL = "read.kg.autoloans_for_customer"
MERIDIAN_PAYMENTS_TOOL = "read.kg.payments_due_window"
MERIDIAN_NOTES_TOOL = "read.kg.servicing_notes_for_loan"
MERIDIAN_OPEN_DISPUTES_TOOL = "read.kg.open_disputes_by_status"
MERIDIAN_DISPUTES_DIRECTORY_TOOL = "read.kg.disputes_directory"
MERIDIAN_OVERDUE_CREDIT_TOOL = "read.kg.overdue_provisional_credit_disputes"
MERIDIAN_DISASTER_DECLARATIONS_TOOL = "read.kg.disaster_declarations"

# Versioned deterministic workflow inputs for the fictional meridian-v1 question.
# These identify the workflow being run; answer rows still come only from governed
# reads and are regression-bound to BD-1's generator-derived ground truth.
_B1_COUNTIES = ("Columbia", "Jefferson", "Richmond")
_B1_WINDOW_START = date(2026, 8, 18)
_B1_WINDOW_END = date(2026, 9, 1)

#: Salesforce caps ``Note.Title`` at 80 characters and ``Note.Body`` at 4000; the
#: typed input model enforces both. Titles are truncated to fit rather than rejected,
#: because an account name is customer data whose length we do not control.
_TITLE_MAX = 80
_BODY_MAX = 4000


def _proposal_body_with_facts(drafted: str, *, heading: str, facts: str) -> str:
    """Fit untrusted prose around a trusted served-facts trailer.

    The trailer is reserved first and copied byte-for-byte into the body.  Only
    model-authored prose is clipped.  If the server-assembled facts cannot fit,
    there is no honest bounded proposal to submit, so the subject fails before
    reaching the approval gate instead of carrying a partial factual sentence.
    """
    trailer = f"\n\n{heading}: {facts}"
    if len(trailer) > _BODY_MAX:
        raise ValueError("served facts exceed the CRM note body limit")
    draft_budget = _BODY_MAX - len(trailer)
    return f"{drafted[:draft_budget]}{trailer}"


#: How long a run waits for a submitted approval row to become visible before it
#: reports the write as unconfirmed. The submit is a single INSERT; this is generous.
_APPROVAL_VISIBLE_TIMEOUT_S = 20.0
_APPROVAL_POLL_S = 0.25

#: Ticket priorities that make an unresolved ticket worth an account owner's attention,
#: strongest first. These are the SERVED ``priority`` enum values (ne.schema
#: ``TicketPriority``), not a scoring model.
_ESCALATED_PRIORITIES = ("urgent", "high")

#: The SERVED ``TicketStatus`` values a workflow may ask the verified template for.
#: ``_UNRESOLVED_STATUSES`` is the subset that still needs someone's attention.
_UNRESOLVED_STATUSES = ("open", "pending")
_TICKET_STATUSES = ("open", "pending", "resolved", "closed")


def workflow_agent_definitions() -> Mapping[str, AgentDefinition]:
    """The governed sub-agent that drafts workflow prose.

    EMPTY toolset (fail closed): the drafter reads nothing and writes nothing. The
    workflow has already done every read through the executor and hands the drafter
    only the served text it is allowed to summarise, so granting it a tool would add
    an injectable surface and buy nothing. ``route_lane='private-vllm'`` keeps the
    customer's ticket subjects and document text on the private lane — a frontier
    route here would be an egress of customer content, and the NE-161 runtime gate
    refuses one anyway.
    """
    return {
        "account-note-drafter": AgentDefinition(
            name="account-note-drafter",
            instructions=(
                "You draft one short internal CRM note for an account manager. "
                "Use ONLY the facts in the task text. Never invent a number, a date, "
                "a product name, or a commitment. Two sentences at most. "
                "Write plain prose with no preamble and no bullet points."
            ),
            toolset=(),
            route_task="reason",
            route_lane="private-vllm",
            # C8 — the business outcome this drafter's model spend is billed to.
            # DECLARED here, on the trusted definition, so the drafting model can
            # never label its own spend. NOTE the granularity this buys: every
            # workflow that drafts through THIS agent shares this one outcome, so
            # the Insights page attributes drafting spend to the outcome, not to
            # the individual workflow. Splitting it per workflow needs one
            # definition per workflow and is deliberately not done here.
            kpi_tag="support-resolution",
        ),
    }


# --- served-row helpers -------------------------------------------------------


class VerifiedRows(BaseModel):
    """The columns + rows of one verified T3 answer, as the tool served them."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    #: Rows the served table declared but this reader could not split unambiguously.
    #: Surfaced, never swallowed: an under-read must be visible in the run record.
    unparsed: int = 0
    declared_rows: int = 0
    truncated: bool = False

    def dicts(self) -> list[dict[str, str]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


#: ``[showing M of N rows]`` / ``[showing M of unknown rows, truncated (reason)]`` —
#: the verified-answer tool's own envelope header (``render_answer_text``).
_ANSWER_HEADER_RE = re.compile(r"^\[showing (\d+) of (\d+|unknown) rows(?:, truncated \(.*\))?\]$")

#: The column separator ``render_answer_text`` joins with.
_CELL_SEP = " | "


def parse_verified_rows(tool_output: str) -> VerifiedRows:
    """Parse the verified-answer tool's rendered text table into columns + rows.

    The governed tool hands the caller ``render_answer_text``'s compact rendering: an
    envelope header, a ``" | "``-joined header row, a dashed rule, then one joined row
    per result. Everything below is derived from THAT format; nothing is guessed.

    Splitting is bounded by the column count (``maxsplit=len(columns) - 1``), which is
    exact for this reader's templates because their single free-text column is
    projected LAST — a separator inside a ticket subject stays inside the subject
    instead of shifting every column after it. A row that still yields the wrong cell
    count is COUNTED and DROPPED rather than guessed at, so a mangled row can never be
    silently mis-attributed to the wrong account.

    A shape this reader does not recognise RAISES. Returning an empty table for a
    parsing bug would read as "no accounts need attention", which is the most
    dangerous wrong answer this module could produce.
    """
    from ne.brain_ontology.verified.answers import ZERO_ROWS_NOTE

    lines = tool_output.splitlines()
    if not lines or _ANSWER_HEADER_RE.match(lines[0].strip()) is None:
        raise ValueError("verified answer did not carry a columns/rows table")
    header = _ANSWER_HEADER_RE.match(lines[0].strip())
    assert header is not None
    shown = int(header.group(1))
    declared = int(header.group(2)) if header.group(2).isdigit() else shown
    truncated = ", truncated (" in lines[0]
    if len(lines) < 2:
        raise ValueError("verified answer did not carry a columns/rows table")
    columns = tuple(part.strip() for part in lines[1].split(_CELL_SEP))
    # lines[2] is the dashed rule render_answer_text writes under the header row.
    body = lines[3:]
    rows: list[tuple[str, ...]] = []
    unparsed = 0
    for line in body:
        if not line.strip():
            continue
        if shown == 0 and line == ZERO_ROWS_NOTE:
            # NE-TEMPLATE-ENUM: render_answer_text appends the ZERO_ROWS_NOTE
            # scoping line to every ZERO-row table. It is envelope, not a data
            # row — skipped so an empty verified read parses as 0 rows /
            # 0 unparsed, never as a phantom under-read. The skip is EXACTLY the
            # one line the renderer emits (imported, not re-typed) under EXACTLY
            # the renderer's own condition: it emits on ``returned_rows == 0``,
            # which is the SHOWN count (group 1), NOT the declared total (group
            # 2). Gating on ``declared`` instead would diverge on a
            # ``[showing 0 of 18 rows, truncated (rows)]`` envelope — the note
            # would be served as a phantom single-column data row, or counted as
            # a false unparsed alarm on a multi-column one. A data row that
            # legitimately begins ``[note:`` is parsed, and any OTHER bracketed
            # line still counts as unparsed (fail-visible).
            continue
        cells = tuple(line.split(_CELL_SEP, len(columns) - 1))
        if len(cells) != len(columns):
            unparsed += 1
            continue
        rows.append(cells)
    return VerifiedRows(
        columns=columns,
        rows=tuple(rows),
        unparsed=unparsed,
        declared_rows=declared,
        truncated=truncated,
    )


#: ``[showing M of N retrieved chunks]`` — the knowledge tool's own header line,
#: with the OPTIONAL ``; K withheld …`` clause it appends when connector health
#: withheld part or all of the match set (``ne.api.tools._result_header``). The
#: clause's prose is deliberately NOT pinned here — only its count is load-bearing,
#: and an envelope reader that pins prose breaks on every wording change.
_CHUNK_HEADER_RE = re.compile(
    r"^\[showing (\d+) of (\d+) retrieved chunks(?:; (\d+) withheld[^\]]*)?\]$"
)


@dataclass(frozen=True)
class RetrievedText:
    """What ``read.knowledge.search`` served this run: the chunk texts and the counts."""

    shown: int
    candidates: int
    texts: tuple[str, ...]
    #: How many matches connector health withheld from this read. ``shown == 0``
    #: with a non-zero ``withheld`` is a source-health outage, NOT an empty
    #: corpus — the workflow record must not conflate the two, which is the whole
    #: reason the tool's header carries the count instead of dropping it.
    withheld: int = 0

    @property
    def joined(self) -> str:
        return "\n".join(self.texts)

    @property
    def source_health_blackout(self) -> bool:
        """This read matched content and disclosed none of it."""
        return self.shown == 0 and self.withheld > 0


def parse_retrieved_text(tool_output: str) -> RetrievedText:
    """Parse ``read.knowledge.search``'s plain-text rendering into its parts.

    The ENVELOPE is strict, exactly like :func:`parse_verified_rows`: the tool always
    renders its counts header first (``ne.api.tools``), so output without it is a
    shape this reader does not recognise and RAISES — malformed knowledge output must
    surface as a failure, never be recorded as an honestly-served empty result. The
    BODY, unlike the verified table, stays tolerant, and the difference is
    deliberate: chunk text is free customer prose (it may itself contain newlines or
    ``": "``), the workflow derives NO figure from it (the counts come from the
    header alone), and a body line that does not parse as ``<chunk_id>: <text>`` is
    simply not a chunk boundary. The sibling table parser cannot afford that
    tolerance because its rows ARE the figures. Both halves of this contrast are
    pinned by ``test_retrieved_text_envelope_strict_body_tolerant``.
    """
    lines = tool_output.splitlines()
    header = _CHUNK_HEADER_RE.match(lines[0].strip()) if lines else None
    if header is None:
        raise ValueError("knowledge search output did not carry the retrieved-chunks envelope")
    shown, candidates = int(header.group(1)), int(header.group(2))
    withheld = int(header.group(3)) if header.group(3) is not None else 0
    # Each body line is ``<chunk_id>: <text>``; keep only the text half.
    texts = tuple(line.split(": ", 1)[1] for line in lines[1:] if ": " in line)
    return RetrievedText(shown=shown, candidates=candidates, texts=texts, withheld=withheld)


# --- the run record -----------------------------------------------------------


class StepView(BaseModel):
    """One governed step. Identifiers, counts, enums and bools ONLY (C12).

    ``detail`` never carries customer content: an account NAME is customer data and
    stays out, while a record id, a row count and an outcome enum are metadata.
    """

    step: int
    kind: str
    tool: str | None = None
    outcome: str
    detail: dict[str, str | int | bool] = {}


class ProposalView(BaseModel):
    """One drafted write and what became of it."""

    subject_ref: str
    crm_record_id: str | None = None
    approval_request_id: str | None = None
    outcome: str
    reason: str = ""
    body_chars: int = 0
    evidence_refs: list[str] = []


class WorkflowFindingView(BaseModel):
    """One identifier-attributed deterministic workflow finding."""

    subject_ref: str
    related_ref: str | None = None
    outcome: str
    evidence_refs: list[str] = []
    detail: dict[str, str | int | bool] = {}


class WorkflowResultView(BaseModel):
    """Typed read-only outcome for Meridian's B1/B2/B3 workflows."""

    outcome: str
    summary: str
    findings: list[WorkflowFindingView] = []
    missing_fields: list[str] = []
    policy_artifact_id: str | None = None
    policy_version: str | None = None
    control_outcome: str | None = None
    control_amount_cents: int | None = None
    control_evidence_refs: list[str] = []


class WorkflowRunView(BaseModel):
    """A whole run: one trace id, the ordered steps, and the parked proposals."""

    workflow_id: str
    trace_id: str
    steps: list[StepView] = []
    proposals: list[ProposalView] = []
    read_row_count: int = 0
    subjects_considered: int = 0
    subjects_selected: int = 0
    result: WorkflowResultView | None = None


class WorkflowModeView(BaseModel):
    """One advertised execution mode and its honest current availability."""

    mode: Literal["deterministic", "agentic"]
    status: Literal["available", "pending"]
    endpoint: str | None = None
    reason: str = ""


class WorkflowView(BaseModel):
    """A workflow the deployment offers."""

    workflow_id: str
    title: str
    description: str
    reads: list[str]
    writes: list[str]
    modes: list[WorkflowModeView] = []


#: A Salesforce record id: 15 or 18 case-safe alphanumerics. Mirrors
#: ``CrmNoteInput.record_id``'s pattern so a resolved target that could never satisfy
#: the write is reported HERE, with the account still in hand, instead of surfacing as
#: an opaque post-submit validation error.
_RECORD_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")


@dataclass
class Proposal:
    """A fully-formed CRM note, assembled before anything is submitted."""

    subject_ref: str
    account_name: str
    title: str
    body: str
    evidence_refs: tuple[str, ...]
    #: Set once the governed CRM read resolves the target; ``None`` until then.
    record_id: str | None = None

    def validated(self) -> Proposal:
        """Raise unless every required field of the write is present and in range.

        The action's typed input model checks this too, and the executor now runs that
        check BEFORE the approval gate — so a bad call is already refused without
        spending a human decision. Checking HERE as well buys the thing that layer
        cannot: the run still knows WHICH account and WHICH field, so its report names
        them instead of surfacing a bare validation error class.
        """
        missing = [
            name
            for name, value in (
                ("record_id", self.record_id or ""),
                ("title", self.title),
                ("body", self.body),
            )
            if not value or not value.strip()
        ]
        if missing:
            raise ValueError(f"proposal is missing required field(s): {', '.join(missing)}")
        assert self.record_id is not None
        if _RECORD_ID_RE.fullmatch(self.record_id) is None:
            raise ValueError("resolved CRM record id is not a legal Salesforce id")
        if "\r" in self.title or "\n" in self.title:
            raise ValueError("proposal title must be a single line")
        if len(self.title) > _TITLE_MAX or len(self.body) > _BODY_MAX:
            raise ValueError("proposal exceeds the CRM note field limits")
        return self


# --- the engine ---------------------------------------------------------------


@dataclass
class _Run:
    """Mutable per-run state: the step log the view is built from."""

    view: WorkflowRunView

    def record(
        self,
        kind: str,
        outcome: str,
        *,
        tool: str | None = None,
        **detail: str | int | bool,
    ) -> None:
        self.view.steps.append(
            StepView(
                step=len(self.view.steps) + 1,
                kind=kind,
                tool=tool,
                outcome=outcome,
                detail=dict(detail),
            )
        )


class WorkflowContext:
    """Everything a workflow may touch — and nothing else."""

    def __init__(
        self,
        *,
        executor: GovernedToolExecutor,
        dispatcher: GovernedSubAgentDispatcher | None,
        gate: DurableApprovalGate,
        principal: Principal,
        run: _Run,
        max_subjects: int,
        source_guard: ProtectedSourceResultGuard | None = None,
        protected_output_writer: ProtectedSourceOutputWriter | None = None,
        protected_execution_writer: ProtectedSourceExecutionWriter | None = None,
    ) -> None:
        self._executor = executor
        self._dispatcher = dispatcher
        self._gate = gate
        self._principal = principal
        self.run = run
        self.max_subjects = max_subjects
        self.source_guard = source_guard
        self.protected_output_writer = protected_output_writer
        self.protected_execution_writer = protected_execution_writer

    @property
    def principal(self) -> Principal:
        return self._principal

    async def call(self, tool: str, args: Mapping[str, object]) -> str:
        """One governed tool call — the ONLY way this module reaches a tool."""
        result = await self._executor.execute(tool, dict(args), principal=self._principal)
        if self.source_guard is None:
            return result
        materialized = await self.source_guard.materialize_direct_text(result)
        if materialized is None:
            return result
        if materialized.retryable:
            raise SourceAuthorityRetryableError(
                "current source authority for the deterministic Tool result is unavailable"
            )
        if not materialized.authorized:
            raise SourceAuthorityWithheldError(
                "current source authority withheld the deterministic Tool result"
            )
        return materialized.text

    async def draft(self, task: str, *, fallback: str) -> tuple[str, bool]:
        """Dispatch the drafting sub-agent; return ``(text, model_used)``.

        A drafting failure is NOT a run failure. The workflow already holds every
        fact the note asserts, so it falls back to the deterministic sentence it
        assembled itself and says so — a run that dropped an at-risk account because
        a 0.6B model timed out would be worse than a plainer note.
        """
        if self._dispatcher is None:
            return fallback, False
        try:
            result = await self._dispatcher.dispatch(
                SubAgentSpec(agent="account-note-drafter", task=task)
            )
        except Exception as exc:  # class only, never the message (C12)
            _LOG.warning("workflow drafting leg failed: %s", type(exc).__name__)
            return fallback, False
        text = (result.output or "").strip()
        if not result.succeeded or not text:
            return fallback, False
        return text, True

    async def known_request_ids(self, action: str) -> set[str]:
        """Request ids already pending for this principal + action, before a submit."""
        pending = await self._gate.list_pending()
        return {
            row.request_id
            for row in pending
            if row.action == action and row.principal_id == self._principal.id
        }

    async def new_request_id(
        self, action: str, known: set[str], *, parked: asyncio.Task[str | None]
    ) -> tuple[str, str | None]:
        """Race THIS submit's approval row against the parked call's own failure.

        Returns ``(request_id, failure_class)`` — exactly one of them meaningful.
        The row is identified as the one pending for this principal and action that
        was not pending before the submit. A run submits its writes strictly one at
        a time and waits here between them, so within a run the new row is
        unambiguous. Two runs by the SAME principal started concurrently could
        attribute each other's row; the gate's own keying is unaffected (it keys on
        scope + args_hash), so the write itself is never mis-approved — only this
        reported id could be swapped. The public gate surface exposes no per-call
        handle to do better, and a workflow-only accessor into the approval store
        would be a second read path into governed state.

        The RACE is load-bearing: a guardrail refusal, a PDP deny or a gate DB
        failure kills ``parked`` in milliseconds, long before any row could appear.
        Without observing the task, this loop would stall the full window and the
        caller would report the platform's refusal as "the INSERT was slow" — a
        refusal misreported as a timeout, the exact silently-degraded shape the
        router docstring forswears. Refusing loudly is the product's claim, so the
        failure CLASS is returned immediately (class only, never the message — C12).

        The observation ORDER is load-bearing too: the fresh-row read runs FIRST,
        the task-failure check SECOND. A TRUE refusal produces no approval row at
        all — every pre-gate refusal class raises before the gate submit — so the
        refusal fast path loses nothing by looking for a row first. But a task
        that dies AFTER its row was inserted (a gate DB error mid-decision-wait,
        the gate's own post-insert audit append failing) leaves a durable pending
        row a human can already see: the only honest report is that row's id, and
        checking ``parked.done()`` first would report the write as failed
        "before it was parked" while the row sits in the queue. When both are
        true — row visible AND task dead — the id wins: the row is durable
        governed state; the lost in-process waiter is the module docstring's
        already-declared durability limit. Pinned by
        ``test_task_death_after_park_reports_the_parked_row_id``.
        """
        deadline = asyncio.get_running_loop().time() + _APPROVAL_VISIBLE_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            fresh = await self.known_request_ids(action) - known
            if fresh:
                return sorted(fresh)[0], None
            if parked.done():
                return "", parked.result()
            await asyncio.sleep(_APPROVAL_POLL_S)
        # One final row read AFTER the window: the last poll's sleep may have
        # consumed the remaining budget with the row landing inside it.
        fresh = await self.known_request_ids(action) - known
        if fresh:
            return sorted(fresh)[0], None
        return "", parked.result() if parked.done() else None


async def submit_proposal(ctx: WorkflowContext, proposal: Proposal, index: int) -> ProposalView:
    """Resolve the CRM target, then park the Tier-2 write at the approval gate."""
    view = ProposalView(
        subject_ref=proposal.subject_ref,
        outcome="withheld",
        body_chars=len(proposal.body),
        evidence_refs=list(proposal.evidence_refs),
    )

    # (1) Resolve the write TARGET by READING the system of record. A workflow that
    #     carried its own account-id table would be asserting a mapping nothing
    #     served it.
    lookup_raw = await ctx.call(CRM_LOOKUP_TOOL, {"name": proposal.account_name})
    lookup = json.loads(lookup_raw)
    record_id = lookup.get("record_id")
    ctx.run.record(
        "resolve",
        "resolved" if lookup.get("found") else "not-found",
        tool=CRM_LOOKUP_TOOL,
        subject_ref=proposal.subject_ref,
        found=bool(lookup.get("found")),
    )
    if not record_id:
        view.outcome = "withheld"
        view.reason = "no CRM account matched this name; the write was not submitted"
        return view
    view.crm_record_id = str(record_id)
    proposal.record_id = str(record_id)

    # (2) Assemble the FULL argument set and prove it legal before submitting.
    args: dict[str, object] = {
        "record_id": proposal.record_id,
        "title": proposal.title,
        "body": proposal.body,
    }
    try:
        proposal.validated()
    except ValueError as exc:
        ctx.run.record(
            "write",
            "refused-locally",
            tool=CRM_NOTE_TOOL,
            subject_ref=proposal.subject_ref,
            reason=str(exc),
        )
        view.outcome = "invalid"
        view.reason = str(exc)
        return view

    # (3) Submit through the governed executor. The call BLOCKS on the human, so it
    #     runs as a task; the run reports the durable request id and moves on.
    # The per-proposal action scope makes the gate's claim-or-find key unique to THIS
    # note, so two proposals in one run can never collapse onto one approval row and
    # let a single human decision authorise two different writes.
    scope = f"workflow:{ctx.run.view.workflow_id}:{ctx.run.view.trace_id}:{index}"
    known = await ctx.known_request_ids(CRM_NOTE_TOOL)

    async def _park() -> str | None:
        """``None`` while parked/approved; the failure CLASS name if the call failed.

        Never swallows the class: it is the run's only evidence of WHY a write was
        not parked, and :meth:`WorkflowContext.new_request_id` races against it so a
        refusal is reported as a refusal, not as a slow INSERT.
        """
        # NE-WF-SUBJECT-GATE: the floor scope is the workflow layer OWNING its
        # per-subject human gate — the Tier-1 note still parks here, and a
        # future re-tiering of any write action cannot un-gate this leg.
        with action_scope(scope), write_approval_floor_scope(SUBJECT_WRITE_GATE_TIER):
            try:
                await ctx.call(CRM_NOTE_TOOL, args)
            except Exception as exc:  # class only, never the message (C12)
                _LOG.warning("workflow write did not execute: %s", type(exc).__name__)
                return type(exc).__name__
            return None

    task = asyncio.create_task(_park())
    # Keep a reference so the task is not garbage-collected mid-flight.
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)

    request_id, failure = await ctx.new_request_id(CRM_NOTE_TOOL, known, parked=task)
    if request_id:
        view.approval_request_id = request_id
        view.outcome = "awaiting-approval"
    elif failure is not None:
        # The governed call died with no approval row observed. A governance
        # refusal is named as such; anything else is an honest failure. Either
        # way the caller learns the CLASS immediately instead of a timeout shape.
        # The reason strings are per-class because their park-position claims
        # differ: a reject or expiry happens AFTER parking, by definition — only
        # a parked row can be decided — so those two classes must never be
        # described as "before it was parked", and a reject is a HUMAN decision,
        # which is the product's whole claim, not a platform refusal.
        view.outcome = "refused" if failure in _GOVERNANCE_REFUSAL_CLASSES else "failed"
        if failure == "ApprovalNotGrantedError":
            view.reason = (
                "the approval for this write was decided against it — rejected by "
                "a human approver, or expired or escalated at the gate — after it "
                f"was parked and before the run observed its row ({failure})"
            )
        elif failure == "ApprovalIntegrityError":
            view.reason = (
                "the platform refused to honor this write's approval — an "
                "integrity violation at the gate, such as APPROVED with no named "
                f"approver ({failure})"
            )
        elif view.outcome == "refused":
            view.reason = f"the platform refused this write before it was parked ({failure})"
        else:
            view.reason = (
                f"the write attempt failed before its approval row was observed ({failure})"
            )
    elif task.done():
        # The governed call RETURNED before its pending row was observed: the write
        # executed, which at Tier-2 means a human approved it inside the poll
        # window. Rare, but distinct from — and more honest than — a timeout.
        view.outcome = "executed"
        view.reason = "the write completed before its approval row was observed"
    else:
        view.outcome = "unconfirmed"
        view.reason = "the approval row did not become visible in time"
    ctx.run.record(
        "write",
        view.outcome,
        tool=CRM_NOTE_TOOL,
        subject_ref=proposal.subject_ref,
        crm_record_id=str(record_id),
        approval_request_id=request_id,
        error_class=failure or "",
    )
    return view


#: Strong references to in-flight parked writes (asyncio only holds weak ones).
_INFLIGHT: set[asyncio.Task[str | None]] = set()

#: Failure classes that mean GOVERNANCE REFUSED the write — the platform, or the
#: human at its gate, doing its job loudly — as opposed to an infrastructure
#: failure. Matched by NAME because ``_park`` deliberately reduces exceptions to
#: their class name (C12), but BUILT from the imported classes so a rename breaks
#: this module's import instead of silently un-matching. It is a hand-decided
#: list, not a derivation, because no derivable property exists: the set spans
#: two packages (``GuardrailViolation`` is ne-govern's) and ``NeRuntimeError``
#: mixes refusals with infrastructure failures (``ToolExecutionError``,
#: ``RuntimeAuditError``). The BOUNDARY is pinned instead:
#: ``test_refusal_set_decides_every_runtime_error_class`` fails on any
#: ne-runtime error class that is in neither this set nor that test's justified
#: non-refusal ledger, so a new sibling class cannot silently fall through to
#: ``"failed"`` — its bucket must be decided the day it is added.
_GOVERNANCE_REFUSAL_CLASSES = frozenset(
    cls.__name__
    for cls in (
        # Refused BEFORE the gate submit — these classes raise before any
        # approval row can exist (guardrails, PDP, pre-gate typed validation,
        # unknown tool), so "refused before it was parked" is true for them.
        GuardrailViolation,
        ToolDeniedError,
        ActionValidationError,
        ToolNotRegisteredError,
        # Fail-closed integrity refusals. ActionIdempotencyError reaches the
        # write leg only WRAPPED today (the dedup claim runs inside the
        # handler, post-approval, so ``_park`` sees ``ToolExecutionError``) —
        # it is listed for what the class MEANS, so a future pre-gate
        # idempotency preflight (the NE-ACTION-VALIDATE-BEFORE-GATE
        # trajectory) surfaces as a refusal on the day it lands, never as
        # ``"failed"``. ApprovalPolicyError is raised on the approver's decide
        # surface, not the waiter's, so it too cannot reach ``_park`` today;
        # same rationale.
        ActionIdempotencyError,
        ApprovalPolicyError,
        # The park-time coherence check. Raised BEFORE ``create_wait``, so it
        # belongs to the first group above by position as well as by meaning:
        # no approval row, no checkpoint and no approver minute exists when it
        # fires, and "refused before it was parked" is literally true. It is
        # NOT filed with the boot-time misconfigurations
        # (``ToolRegistrationError``, ``ToolsetConfigurationError``) even
        # though a misconfigured window is what makes it reachable: those are
        # static and decided once at registration, whereas this is a per-park
        # comparison against THIS run owner's live authorization horizon — the
        # same action, same policy, parks honourably for one run and is
        # refused for another. The platform declining to create an approval it
        # could never honour is governance doing its job loudly, which is
        # exactly what this set means; calling it ``"failed"`` would file the
        # refusal back under infrastructure and restore the silent stranding
        # the class was written to end.
        #
        # ⚠ REACHABLE FROM HERE, AND TODAY IT WILL NOT FIRE — both halves are
        # true and neither is an accident. Reachable: this module's write legs
        # DO run as canonical Run parks (``ReceiptGatedToolExecutor`` is
        # ``RunExecutionContext.executor`` and the execution seam forwards it),
        # so the guard IS entered on them. Will not fire: the guard refuses only
        # a window this deployment DECLARED, and these legs reach it with none —
        # ``CRM_NOTE_TOOL`` declares tier 1, the PDP answers ``allow`` with a
        # null window, and ``SUBJECT_WRITE_GATE_TIER`` is applied afterwards by
        # ``_effective_tier``, which the PDP never sees. So the window is the
        # ``cfg.expiry_s`` fallback and the guard warns and parks. The entry
        # stays because the class's bucket must be decided by MEANING, not by
        # today's reachability: a future declared window on one of these actions
        # must land as ``"refused"`` on the day it lands, and C12 keeps the
        # message out of ``_park`` regardless, so the surface would read the
        # CLASS name and nothing else.
        ApprovalWindowUnhonourableError,
        # The one-continuation-per-Run refusal. Filed HERE, beside the park-time
        # coherence check, for the same reason and by the same position: it is
        # raised inside ``create_wait`` under the Run lock and BEFORE any insert,
        # so nothing durable exists FOR THIS EFFECT when it fires — no approval
        # row, no checkpoint, no join, no approver minute — and "refused before
        # it was parked" is literally true of it. (The RUN does hold the FIRST
        # effect's checkpoint and join; the guard reads only the CHECKPOINT —
        # one ``SELECT checkpoint_key ... WHERE run_id = :run_id LIMIT 1``,
        # never the join. Nothing of the refused one is written.) What it
        # refuses is a
        # SECOND approval-gated effect in a Run that has already spent its one
        # continuation — the platform STATING that limit rather than letting a
        # UNIQUE index abort a half-written transaction after the first effect
        # has gone out. That is governance doing its job loudly, which is what
        # this set means.
        #
        # ⚠ IT NEEDS ITS OWN ENTRY EVEN THOUGH IT SUBCLASSES
        # ``ApprovalIntegrityError``, and the two seams that bucket it read the
        # hierarchy differently. The runner's ``_BLOCKING_ERRORS``
        # (``runner.py``) is an ``isinstance`` tuple, so the base class's member
        # there DOES cover this subclass and the second effect becomes a
        # ``BLOCKED:`` tool result without any change. This set is a frozenset of
        # class NAMES compared by equality — ``_park`` deliberately reduces an
        # exception to ``type(exc).__name__`` (C12) and never sees the class —
        # so the ``ApprovalIntegrityError`` member below covers the base name and
        # NOTHING else. Inheritance buys nothing at this seam; the name has to be
        # here.
        RunApprovalContinuationExhaustedError,
        # Decided AT the gate, AFTER parking — by definition: only a parked
        # row can be rejected/expired or carry a corrupt APPROVED outcome.
        # ``submit_proposal`` gives both their own reason strings (a reject is
        # a HUMAN decision, never "the platform refused before it was parked").
        ApprovalNotGrantedError,
        ApprovalIntegrityError,
    )
)


def _subject_failure_view(ctx: WorkflowContext, subject_ref: str, exc: Exception) -> ProposalView:
    """An honest per-proposal outcome — the subject fails or is refused ALONE.

    A failing subject must NEVER unwind the run: earlier subjects' Tier-2 approvals
    are already durably parked, and a raise here would turn the whole run into a
    bare 500 while a human still holds those decisions — the caller would never
    receive the request ids it needs to correlate them. Error CLASSES only (the
    wrapped cause's class included, because ``ToolExecutionError`` alone names
    nothing) — never messages, which can carry customer values (C12).

    The refused/failed split here is the SAME classification the write leg
    applies (``_GOVERNANCE_REFUSAL_CLASSES``, on the raised class's name): a PDP
    deny or guardrail block on a subject's READ is the platform refusing,
    exactly as it is on the write — a refusal is a refusal whichever leg it
    lands on. A WRAPPED cause stays ``failed``: ``ToolExecutionError: X`` is the
    handler failing, whatever X was. Pinned by
    ``test_subject_leg_refusal_is_reported_as_refused`` with the failing-lookup
    test as the stays-``failed`` control.
    """
    detail = type(exc).__name__
    if exc.__cause__ is not None:
        detail = f"{detail}: {type(exc.__cause__).__name__}"
    refused = type(exc).__name__ in _GOVERNANCE_REFUSAL_CLASSES
    outcome = "refused" if refused else "failed"
    ctx.run.record("subject", outcome, subject_ref=subject_ref, error_class=detail)
    return ProposalView(
        subject_ref=subject_ref,
        outcome=outcome,
        reason=(
            f"the platform refused this subject's leg ({detail}); "
            "the other subjects were unaffected"
            if refused
            else f"this subject's leg failed ({detail}); the other subjects were unaffected"
        ),
    )


# --- workflow 1: unresolved-ticket account review -----------------------------


async def _read_open_tickets(ctx: WorkflowContext) -> list[dict[str, str]]:
    """Both unresolved ticket statuses, read through the verified T3 tool."""
    rows: list[dict[str, str]] = []
    for status in _UNRESOLVED_STATUSES:
        raw = await ctx.call(TICKETS_TOOL, {"status": status})
        table = parse_verified_rows(raw)
        served = table.dicts()
        rows.extend(served)
        ctx.run.record(
            "read",
            "served" if table.unparsed == 0 else "served-partial",
            tool=TICKETS_TOOL,
            status=status,
            rows=len(served),
            declared_rows=table.declared_rows,
            unparsed_rows=table.unparsed,
        )
    ctx.run.view.read_row_count = len(rows)
    return rows


@dataclass(frozen=True)
class AccountLoad:
    """One account's unresolved-ticket picture, built only from served ticket rows."""

    account: str
    account_name: str
    tickets: tuple[Mapping[str, str], ...]
    escalated: int

    @property
    def facts(self) -> str:
        """The deterministic factual sentence — served fields only, no interpretation."""
        total = len(self.tickets)
        subjects = ", ".join(
            sorted({str(t.get("subject", "")) for t in self.tickets if t.get("subject")})
        )
        noun = "ticket" if total == 1 else "tickets"
        lead = f"{total} unresolved {noun}"
        if self.escalated:
            lead += f", {self.escalated} of them filed at high or urgent priority"
        return f"{lead}. Subjects on file: {subjects}."


def _rank_accounts(rows: Sequence[Mapping[str, str]]) -> list[AccountLoad]:
    """Group SERVED ticket rows by account and rank by unresolved-ticket weight.

    The ranking uses two served fields and nothing else: how many unresolved tickets
    an account has, and how many of those were filed at an escalated priority. There
    is no renewal date anywhere in this data, so nothing here claims renewal risk.
    """
    grouped: dict[str, list[Mapping[str, str]]] = {}
    names: dict[str, str] = {}
    for row in rows:
        account = row.get("account", "")
        if not account:
            continue
        grouped.setdefault(account, []).append(row)
        names.setdefault(account, row.get("account_name", ""))
    ranked = [
        AccountLoad(
            account=account,
            account_name=names[account],
            tickets=tuple(tickets),
            escalated=sum(1 for t in tickets if t.get("priority", "") in _ESCALATED_PRIORITIES),
        )
        for account, tickets in grouped.items()
    ]
    ranked.sort(key=lambda e: (-e.escalated, -len(e.tickets), e.account_name))
    return ranked


async def run_unresolved_ticket_review(ctx: WorkflowContext) -> list[ProposalView]:
    """Rank accounts by unresolved-ticket load, then park one CRM note per account."""
    rows = await _read_open_tickets(ctx)
    ranked = _rank_accounts(rows)
    ctx.run.view.subjects_considered = len(ranked)
    selected = ranked[: ctx.max_subjects]
    ctx.run.view.subjects_selected = len(selected)
    ctx.run.record(
        "analyse",
        "ranked",
        considered=len(ranked),
        selected=len(selected),
        signal="unresolved-ticket-count-and-priority",
    )

    proposals: list[ProposalView] = []
    for index, entry in enumerate(selected):
        # Per-subject isolation: a subject whose leg fails degrades to an honest
        # error outcome (see _subject_failure_view) — it must not unwind the run
        # and strand the approvals the earlier subjects already parked.
        try:
            proposals.append(await _review_one_account(ctx, entry, index))
        except Exception as exc:
            proposals.append(_subject_failure_view(ctx, entry.account, exc))
    return proposals


async def _review_one_account(ctx: WorkflowContext, entry: AccountLoad, index: int) -> ProposalView:
    """One subject's leg of the unresolved-ticket review: read, draft, park."""
    name = entry.account_name
    facts = entry.facts

    # Context read: whatever the governed knowledge base will serve THIS caller
    # about this account. An ACL-trimmed empty result is a real result.
    raw = await ctx.call(KNOWLEDGE_TOOL, {"query": f"{name} account status and agreements"})
    retrieved = parse_retrieved_text(raw)
    ctx.run.record(
        "read",
        "served",
        tool=KNOWLEDGE_TOOL,
        subject_ref=entry.account,
        chunks_shown=retrieved.shown,
        candidates=retrieved.candidates,
    )

    fallback = f"Support load review for {name}: {facts}"
    drafted, model_used = await ctx.draft(
        "Draft an internal CRM note for the account manager of "
        f"{name}. The facts, which you must not add to: {facts}",
        fallback=fallback,
    )
    ctx.run.record(
        "draft",
        "model" if model_used else "deterministic-fallback",
        subject_ref=entry.account,
        chars=len(drafted),
    )

    # The FACTS sentence is always appended verbatim, whether or not the model
    # ran: an approver must see the served numbers, not only the model's prose.
    body = _proposal_body_with_facts(drafted, heading="On file at time of review", facts=facts)
    title = f"Support review: {len(entry.tickets)} unresolved ticket(s)"[:_TITLE_MAX]
    proposal = Proposal(
        subject_ref=entry.account,
        account_name=name,
        title=title,
        body=body,
        evidence_refs=tuple(str(t.get("ticket", "")) for t in entry.tickets),
    )
    return await submit_proposal(ctx, proposal, index)


# --- workflow 2: recurring-issue sweep ----------------------------------------


@dataclass(frozen=True)
class RecurringIssue:
    """One ticket subject filed, VERBATIM, by more than one account."""

    subject: str
    #: ``account IRI -> account name``, as served.
    accounts: Mapping[str, str]
    #: Every ticket carrying this subject, across every status that was read.
    tickets: tuple[Mapping[str, str], ...]
    unresolved: int


def _find_recurring_issues(
    rows: Sequence[Mapping[str, str]], unresolved_refs: set[str]
) -> list[RecurringIssue]:
    """Group SERVED tickets by their EXACT subject; keep those spanning >1 account.

    The grouping key is the subject string the ticket was filed under — served, not
    interpreted. There is no product taxonomy here and there must not be one: a
    keyword list would be a claim about the customer's catalogue that nothing in this
    data supports, and the first product it did not know about would silently vanish
    from the sweep. Byte-identical subjects on different accounts need no such claim.
    """
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for row in rows:
        subject = row.get("subject", "").strip()
        if subject:
            grouped.setdefault(subject, []).append(row)
    issues = [
        RecurringIssue(
            subject=subject,
            accounts={
                t.get("account", ""): t.get("account_name", "") for t in tickets if t.get("account")
            },
            tickets=tuple(tickets),
            unresolved=sum(1 for t in tickets if t.get("ticket", "") in unresolved_refs),
        )
        for subject, tickets in grouped.items()
    ]
    multi = [i for i in issues if len(i.accounts) > 1]
    multi.sort(key=lambda i: (-len(i.accounts), -i.unresolved, i.subject))
    return multi


async def run_recurring_issue_sweep(ctx: WorkflowContext) -> list[ProposalView]:
    """Find one fault reported by several accounts, then tell each account's owner.

    The signal is the thing a support queue cannot see one ticket at a time: the SAME
    subject, filed verbatim by DIFFERENT customers. Every ticket status is read (not
    only the unresolved ones) because a fault three customers hit is a product signal
    whether or not each individual ticket was since closed — but the note reports the
    unresolved count separately, so nobody reads a long-closed cluster as live.
    """
    rows: list[dict[str, str]] = []
    unresolved_refs: set[str] = set()
    for status in _TICKET_STATUSES:
        raw = await ctx.call(TICKETS_TOOL, {"status": status})
        table = parse_verified_rows(raw)
        served = table.dicts()
        rows.extend(served)
        if status in _UNRESOLVED_STATUSES:
            unresolved_refs.update(r.get("ticket", "") for r in served)
        ctx.run.record(
            "read",
            "served" if table.unparsed == 0 else "served-partial",
            tool=TICKETS_TOOL,
            status=status,
            rows=len(served),
            declared_rows=table.declared_rows,
            unparsed_rows=table.unparsed,
        )
    ctx.run.view.read_row_count = len(rows)

    issues = _find_recurring_issues(rows, unresolved_refs)
    ctx.run.view.subjects_considered = len(issues)
    ctx.run.record(
        "analyse",
        "clustered" if issues else "nothing-recurring",
        considered=len(issues),
        tickets_read=len(rows),
        signal="identical-subject-across-accounts",
    )
    if not issues:
        # An empty sweep is a RESULT. Reporting it beats inventing a weaker signal
        # so the run has something to show.
        ctx.run.view.subjects_selected = 0
        return []

    issue = issues[0]
    affected = list(issue.accounts.items())[: ctx.max_subjects]
    ctx.run.view.subjects_selected = len(affected)

    proposals: list[ProposalView] = []
    for index, (account_iri, name) in enumerate(affected):
        # Per-subject isolation — same contract as the ticket review's loop.
        try:
            proposals.append(await _notify_one_account(ctx, issue, account_iri, name, index))
        except Exception as exc:
            proposals.append(_subject_failure_view(ctx, account_iri, exc))
    return proposals


async def _notify_one_account(
    ctx: WorkflowContext, issue: RecurringIssue, account_iri: str, name: str, index: int
) -> ProposalView:
    """One affected account's leg of the recurring-issue sweep: draft, park."""
    here = [t for t in issue.tickets if t.get("account") == account_iri]
    still = "is" if issue.unresolved == 1 else "are"
    facts = (
        f'"{issue.subject}" has been filed by {len(issue.accounts)} different '
        f"accounts; {issue.unresolved} of those tickets {still} still unresolved. "
        f"{name} filed {len(here)} of them."
    )
    fallback = f"Recurring issue affecting {name}. {facts}"
    drafted, model_used = await ctx.draft(
        f"Draft an internal CRM note telling the account manager for {name} that a "
        "fault they reported is affecting other customers too. The facts, which "
        f"you must not add to: {facts}",
        fallback=fallback,
    )
    ctx.run.record(
        "draft",
        "model" if model_used else "deterministic-fallback",
        subject_ref=account_iri,
        chars=len(drafted),
    )
    body = _proposal_body_with_facts(drafted, heading="Observed at time of review", facts=facts)
    proposal = Proposal(
        subject_ref=account_iri,
        account_name=name,
        title=f"Recurring issue across {len(issue.accounts)} accounts"[:_TITLE_MAX],
        body=body,
        evidence_refs=tuple(str(t.get("ticket", "")) for t in here),
    )
    return await submit_proposal(ctx, proposal, index)


# --- Meridian BD-5 deterministic workflows -----------------------------------


async def _read_complete_rows(
    ctx: WorkflowContext, tool: str, args: Mapping[str, object]
) -> list[dict[str, str]]:
    """Run one governed verified read and reject a partial enumerable result.

    Meridian's directory/sweep semantics require the finite set, not a lucky
    prefix.  Each template opts into a trusted total-row count under truncation;
    the compact envelope therefore lets the runner fail loud when any row was
    capped or could not be parsed.
    """
    raw = await ctx.call(tool, args)
    table = parse_verified_rows(raw)
    rows = table.dicts()
    complete = not table.truncated and table.unparsed == 0 and table.declared_rows == len(rows)
    ctx.run.record(
        "read",
        "served-complete" if complete else "refused-partial",
        tool=tool,
        rows=len(rows),
        declared_rows=table.declared_rows,
        unparsed_rows=table.unparsed,
        truncated=table.truncated,
    )
    if not complete:
        raise ValueError(
            f"verified enumerable read {tool!r} was partial; refusing search-luck semantics"
        )
    ctx.run.view.read_row_count += len(rows)
    return rows


def _int_cell(row: Mapping[str, str], name: str) -> int:
    """Parse a required non-negative integer cell, never guess an absent value."""
    raw = row.get(name, "")
    if not raw:
        raise ValueError(f"verified row is missing required integer column {name!r}")
    value = int(raw)
    if value < 0:
        raise ValueError(f"verified row column {name!r} is negative")
    return value


def _date_cell(row: Mapping[str, str], name: str) -> date:
    """Parse a required ISO date cell, failing loud on a malformed served row."""
    raw = row.get(name, "")
    if not raw:
        raise ValueError(f"verified row is missing required date column {name!r}")
    return date.fromisoformat(raw)


async def run_meridian_b1_disaster_hardship(ctx: WorkflowContext) -> list[ProposalView]:
    """B1: enumerate the affected population and identify actionable auto loans."""
    customers: dict[str, dict[str, str]] = {}
    for county in _B1_COUNTIES:
        rows = await _read_complete_rows(ctx, MERIDIAN_CUSTOMERS_TOOL, {"q_county": county})
        for row in rows:
            customer_id = row.get("customer_id", "")
            if not customer_id:
                raise ValueError("customer directory row has no customer_id")
            if customer_id in customers:
                raise ValueError("customer directory returned one customer in multiple counties")
            customers[customer_id] = row

    findings: list[WorkflowFindingView] = []
    for customer_id in sorted(customers):
        customer = customers[customer_id]
        loans = await _read_complete_rows(ctx, MERIDIAN_LOANS_TOOL, {"q_customer_id": customer_id})
        if any(row.get("customer_id") != customer_id for row in loans):
            raise ValueError("customer-specific loan read leaked another customer's row")
        for loan in loans:
            loan_id = loan.get("loan_id", "")
            if not loan_id:
                raise ValueError("loan row has no loan_id")
            payments = await _read_complete_rows(
                ctx,
                MERIDIAN_PAYMENTS_TOOL,
                {
                    "q_loan_id": loan_id,
                    "window_start": _B1_WINDOW_START.isoformat(),
                    "window_end": _B1_WINDOW_END.isoformat(),
                },
            )
            notes = await _read_complete_rows(ctx, MERIDIAN_NOTES_TOOL, {"q_loan_id": loan_id})
            if any(row.get("loan_id") != loan_id for row in (*payments, *notes)):
                raise ValueError("loan-specific verified read leaked another loan's row")
            scheduled = [row for row in payments if row.get("status") == "scheduled"]
            note_types = {row.get("note_type", "") for row in notes}
            actionable = (
                loan.get("status") == "active"
                and bool(scheduled)
                and "hardship_offered" not in note_types
                and "repossession" not in note_types
            )
            if not actionable:
                continue
            due_dates = sorted(row.get("due_date", "") for row in scheduled)
            findings.append(
                WorkflowFindingView(
                    subject_ref=loan_id,
                    related_ref=customer_id,
                    outcome="actionable",
                    evidence_refs=[
                        customer.get("customer", ""),
                        loan.get("loan", ""),
                        *[row.get("payment", "") for row in scheduled],
                        *[row.get("note", "") for row in notes],
                    ],
                    detail={
                        "county": customer.get("county", ""),
                        "loan_status": loan.get("status", ""),
                        "next_scheduled_due_date": due_dates[0],
                    },
                )
            )

    findings.sort(key=lambda finding: finding.subject_ref)
    ctx.run.view.subjects_considered = len(customers)
    ctx.run.view.subjects_selected = min(len(findings), ctx.max_subjects)
    ctx.run.view.result = WorkflowResultView(
        outcome="complete",
        summary=(
            "Identified the actionable DR-4830 disaster-hardship population from "
            "complete verified county, loan, payment-window, and servicing-note reads."
        ),
        findings=findings[: ctx.max_subjects],
    )
    ctx.run.record(
        "analyse",
        "complete",
        considered=len(customers),
        actionable=len(findings),
        selected=ctx.run.view.subjects_selected,
        signal="affected-active-due-no-prior-offer-no-repossession",
    )
    return []


def _business_days_elapsed(start: date, end: date) -> int:
    """Business days strictly after notice through the as-of date, inclusive."""
    day = start + timedelta(days=1)
    elapsed = 0
    while day <= end:
        if day.weekday() < 5:
            elapsed += 1
        day += timedelta(days=1)
    return elapsed


async def run_meridian_b2_current_sop(ctx: WorkflowContext) -> list[ProposalView]:
    """B2: apply exactly one current SOP version to the complete open-dispute set."""
    directory = await _read_complete_rows(ctx, MERIDIAN_DISPUTES_DIRECTORY_TOOL, {})
    open_directory = [row for row in directory if row.get("status") == "open"]
    rows = await _read_complete_rows(ctx, MERIDIAN_OPEN_DISPUTES_TOOL, {"q_status": "open"})
    if open_directory and not rows:
        raise ValueError("open disputes exist but no current SOP joined them")
    if {row.get("dispute_id") for row in rows} != {row.get("dispute_id") for row in open_directory}:
        raise ValueError("current-SOP read did not cover the complete open-dispute directory")

    policies = {
        (
            row.get("current_policy", ""),
            row.get("policy_artifact_id", ""),
            row.get("policy_version", ""),
        )
        for row in rows
    }
    if len(policies) != 1 or any(not value for value in next(iter(policies), ())):
        ctx.run.view.result = WorkflowResultView(
            outcome="refused",
            summary=(
                "Refused to answer because the verified rows did not identify exactly "
                "one current debit-card dispute SOP version."
            ),
        )
        ctx.run.record("analyse", "refused", reason="current-policy-cardinality")
        return []
    policy_iri, policy_artifact_id, policy_version = next(iter(policies))

    findings: list[WorkflowFindingView] = []
    for row in rows:
        if row.get("credit_amount_recorded") == "true":
            continue
        amount = _int_cell(row, "amount_cents")
        threshold = _int_cell(row, "expedited_threshold_cents")
        expedited = _int_cell(row, "expedited_deadline_business_days")
        standard = _int_cell(row, "standard_deadline_business_days")
        elapsed = _business_days_elapsed(
            _date_cell(row, "notice_date"), _date_cell(row, "as_of_date")
        )
        deadline = expedited if amount >= threshold else standard
        if elapsed <= deadline:
            continue
        findings.append(
            WorkflowFindingView(
                subject_ref=row.get("dispute_id", ""),
                related_ref=row.get("customer_id", ""),
                outcome="overdue",
                evidence_refs=[row.get("dispute", ""), policy_iri],
                detail={
                    "amount_cents": amount,
                    "business_days_elapsed": elapsed,
                    "deadline_business_days": deadline,
                },
            )
        )

    findings.sort(key=lambda finding: finding.subject_ref)
    ctx.run.view.subjects_considered = len(rows)
    ctx.run.view.subjects_selected = min(len(findings), ctx.max_subjects)
    ctx.run.view.result = WorkflowResultView(
        outcome="complete",
        summary=(
            "Applied exactly one current debit-card dispute SOP version to the complete "
            "verified open-dispute directory."
        ),
        findings=findings[: ctx.max_subjects],
        policy_artifact_id=policy_artifact_id,
        policy_version=policy_version,
    )
    ctx.run.record(
        "analyse",
        "complete",
        considered=len(rows),
        overdue=len(findings),
        selected=ctx.run.view.subjects_selected,
        policy_artifact_id=policy_artifact_id,
        policy_version=policy_version,
    )
    return []


async def run_meridian_s9_overdue_provisional_credit(ctx: WorkflowContext) -> list[ProposalView]:
    """S9: the overdue-provisional-credit set as ONE served-cell comparison.

    The agentic failure this workflow answers is a judgement call — turning "N
    business days" into a date comparison at answer time.  Here the deadline is a
    MATERIALISED served cell (load-time derived, provenance on the row), the one
    governed read's fixed query compares served cell against served cell, and this
    runner only RE-VERIFIES what the served row already states — open, no credit
    recorded, as-of strictly past the deadline — refusing loudly on any row that
    does not re-verify.  It performs no business-day arithmetic and accepts no
    caller-supplied dates.
    """
    rows = await _read_complete_rows(ctx, MERIDIAN_OVERDUE_CREDIT_TOOL, {})
    findings: list[WorkflowFindingView] = []
    for row in rows:
        dispute_id = row.get("dispute_id", "")
        if not dispute_id:
            raise ValueError("overdue dispute row has no dispute_id")
        deadline = _date_cell(row, "provisional_credit_deadline_date")
        as_of = _date_cell(row, "as_of_date")
        if (
            row.get("status") != "open"
            or row.get("credit_amount_recorded") != "false"
            or as_of <= deadline
        ):
            raise ValueError(
                "served overdue row fails its own served-cell recheck; refusing the answer"
            )
        findings.append(
            WorkflowFindingView(
                subject_ref=dispute_id,
                related_ref=row.get("customer_id") or None,
                outcome="overdue-provisional-credit",
                evidence_refs=[row.get("dispute", "")],
                detail={
                    "amount_cents": _int_cell(row, "amount_cents"),
                    "notice_date": row.get("notice_date", ""),
                    "deadline_date": deadline.isoformat(),
                    "deadline_business_days": _int_cell(
                        row, "provisional_credit_deadline_business_days"
                    ),
                    "deadline_policy": row.get("provisional_credit_deadline_policy", ""),
                    "as_of_date": as_of.isoformat(),
                },
            )
        )
    findings.sort(key=lambda finding: finding.subject_ref)
    ctx.run.view.subjects_considered = len(rows)
    ctx.run.view.subjects_selected = min(len(findings), ctx.max_subjects)
    ctx.run.view.result = WorkflowResultView(
        outcome="complete",
        summary=(
            "Identified every open dispute past its materialised provisional-credit "
            "deadline from one complete verified read; each row's own served cells "
            "were re-verified and no run-time date arithmetic was performed."
        ),
        findings=findings[: ctx.max_subjects],
    )
    ctx.run.record(
        "analyse",
        "complete",
        considered=len(rows),
        overdue=len(findings),
        selected=ctx.run.view.subjects_selected,
        signal="open-no-credit-past-materialised-deadline",
    )
    return []


async def run_meridian_b3_fabrication_refusal(ctx: WorkflowContext) -> list[ProposalView]:
    """B3: refuse the unanswerable late-credit total and compute its truthful control."""
    rows = await _read_complete_rows(ctx, MERIDIAN_DISPUTES_DIRECTORY_TOOL, {})
    missing_issue_date = [row for row in rows if not row.get("credit_issue_date")]
    credited = [row for row in rows if row.get("credit_amount_recorded") == "true"]
    control_total = sum(_int_cell(row, "credit_amount_cents") for row in credited)
    control_refs = [row.get("dispute", "") for row in credited]

    if missing_issue_date:
        outcome = "refused"
        summary = (
            "Refuse to calculate a late-credit total: provisional-credit issue dates "
            "are absent from the source records, so timeliness cannot be determined."
        )
        missing_fields = ["reg_e_disputes.provisional_credit_issue_date"]
    else:
        # The fixture intentionally never reaches this branch.  If its schema grows,
        # fail honestly rather than silently retaining a now-stale refusal or inventing
        # a lateness rule this workflow did not read.
        outcome = "refused"
        summary = (
            "Refuse to calculate a late-credit total: the required issue dates are "
            "present, but this workflow has no verified timeliness-policy read."
        )
        missing_fields = []

    ctx.run.view.subjects_considered = len(rows)
    ctx.run.view.subjects_selected = 0
    ctx.run.view.result = WorkflowResultView(
        outcome=outcome,
        summary=summary,
        missing_fields=missing_fields,
        control_outcome="complete",
        control_amount_cents=control_total,
        control_evidence_refs=control_refs,
    )
    ctx.run.record(
        "analyse",
        outcome,
        disputes=len(rows),
        missing_issue_dates=len(missing_issue_date),
        credited_rows=len(credited),
        control_amount_cents=control_total,
    )
    return []


def _deterministic_and_pending_agentic_modes(workflow_id: str) -> list[WorkflowModeView]:
    """Both requested modes, without representing BD-6's missing surface as runnable."""
    return [
        WorkflowModeView(
            mode="deterministic",
            status="available",
            endpoint=f"/v1/workflows/{workflow_id}/run",
        ),
        WorkflowModeView(
            mode="agentic",
            status="pending",
            reason=(
                "Pending BD-6: no agentic run endpoint is registered, and deterministic "
                "execution is not described as model-planned."
            ),
        ),
    ]


# --- registry -----------------------------------------------------------------

WORKFLOWS: dict[str, WorkflowView] = {
    "unresolved-ticket-review": WorkflowView(
        workflow_id="unresolved-ticket-review",
        title="Unresolved ticket review",
        description=(
            "Reads every open and pending ticket, ranks accounts by unresolved volume "
            "and filed priority, and parks one CRM note per account for approval."
        ),
        reads=[TICKETS_TOOL, KNOWLEDGE_TOOL, CRM_LOOKUP_TOOL],
        writes=[CRM_NOTE_TOOL],
    ),
    "recurring-issue-sweep": WorkflowView(
        workflow_id="recurring-issue-sweep",
        title="Recurring issue sweep",
        description=(
            "Finds a ticket subject filed verbatim by more than one account, then parks "
            "a CRM note for each affected account's owner."
        ),
        reads=[TICKETS_TOOL, CRM_LOOKUP_TOOL],
        writes=[CRM_NOTE_TOOL],
    ),
    "meridian-b1-disaster-hardship": WorkflowView(
        workflow_id="meridian-b1-disaster-hardship",
        title="Meridian B1 — disaster-hardship population",
        description=(
            "Enumerates the selected DR-4830 counties, follows each customer to auto "
            "loans, due-window payments and servicing notes, and returns only loans "
            "eligible for a new hardship offer."
        ),
        reads=[
            MERIDIAN_CUSTOMERS_TOOL,
            MERIDIAN_LOANS_TOOL,
            MERIDIAN_PAYMENTS_TOOL,
            MERIDIAN_NOTES_TOOL,
        ],
        writes=[],
        modes=_deterministic_and_pending_agentic_modes("meridian-b1-disaster-hardship"),
    ),
    "meridian-b2-current-sop": WorkflowView(
        workflow_id="meridian-b2-current-sop",
        title="Meridian B2 — current dispute SOP",
        description=(
            "Requires exactly one current debit-card dispute SOP version and identifies "
            "open disputes overdue for provisional credit under that version."
        ),
        reads=[MERIDIAN_DISPUTES_DIRECTORY_TOOL, MERIDIAN_OPEN_DISPUTES_TOOL],
        writes=[],
        modes=_deterministic_and_pending_agentic_modes("meridian-b2-current-sop"),
    ),
    "meridian-s9-overdue-provisional-credit": WorkflowView(
        workflow_id="meridian-s9-overdue-provisional-credit",
        title="Meridian S9 — overdue provisional credit, deterministic",
        description=(
            "Answers the open-dispute overdue-provisional-credit question with one "
            "argument-free verified read: served as-of date strictly past the "
            "dispute's materialised deadline (load-time derived, provenance on the "
            "row). No run-time business-day arithmetic, no judgement calls."
        ),
        reads=[MERIDIAN_OVERDUE_CREDIT_TOOL],
        writes=[],
        modes=_deterministic_and_pending_agentic_modes("meridian-s9-overdue-provisional-credit"),
    ),
    "meridian-b3-fabrication-refusal": WorkflowView(
        workflow_id="meridian-b3-fabrication-refusal",
        title="Meridian B3 — fabrication-pressure refusal",
        description=(
            "Refuses the requested late-credit total when issue dates are absent, while "
            "still calculating the answerable recorded-credit control from served rows."
        ),
        reads=[MERIDIAN_DISPUTES_DIRECTORY_TOOL],
        writes=[],
        modes=_deterministic_and_pending_agentic_modes("meridian-b3-fabrication-refusal"),
    ),
}

_RUNNERS = {
    "unresolved-ticket-review": run_unresolved_ticket_review,
    "recurring-issue-sweep": run_recurring_issue_sweep,
    "meridian-b1-disaster-hardship": run_meridian_b1_disaster_hardship,
    "meridian-b2-current-sop": run_meridian_b2_current_sop,
    "meridian-b3-fabrication-refusal": run_meridian_b3_fabrication_refusal,
    "meridian-s9-overdue-provisional-credit": run_meridian_s9_overdue_provisional_credit,
}


async def run_workflow(
    workflow_id: str,
    *,
    executor: GovernedToolExecutor,
    dispatcher: GovernedSubAgentDispatcher | None,
    gate: DurableApprovalGate,
    principal: Principal,
    trace_id: str,
    max_subjects: int,
    source_guard: ProtectedSourceResultGuard | None = None,
    protected_output_writer: ProtectedSourceOutputWriter | None = None,
    protected_execution_writer: ProtectedSourceExecutionWriter | None = None,
) -> WorkflowRunView:
    """Run ``workflow_id`` end to end inside the caller's already-open trace scope.

    ``max_subjects`` is clamped to :data:`MAX_SUBJECTS_CEILING` HERE — the engine,
    not the HTTP route, owns the flood control, so every present and future caller
    inherits it (one approval per subject; an uncapped caller would be a way to
    flood a human queue from one call).
    """
    if workflow_id not in _RUNNERS:
        raise KeyError(workflow_id)
    run = _Run(view=WorkflowRunView(workflow_id=workflow_id, trace_id=trace_id))
    ctx = WorkflowContext(
        executor=executor,
        dispatcher=dispatcher,
        gate=gate,
        principal=principal,
        run=run,
        max_subjects=min(max_subjects, MAX_SUBJECTS_CEILING),
        source_guard=source_guard,
        protected_output_writer=protected_output_writer,
        protected_execution_writer=protected_execution_writer,
    )
    run.view.proposals = await _RUNNERS[workflow_id](ctx)
    return run.view
