# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded model-planned Meridian workflow runs over the frozen C6 executor.

This module does not contain a deterministic fallback.  A response from this
surface is either the model-planned run, or an explicit terminal cap/context
outcome with the partial answer withheld.  The existing ``/v1/workflows`` engine
remains the separately named deterministic mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.api.workflows import SUBJECT_WRITE_GATE_TIER, WORKFLOWS, WorkflowView
from ne.contracts.governance import AuditSink
from ne.contracts.models import ModelGateway
from ne.contracts.types import Lane, Principal
from ne.runtime.config import RuntimeSettings
from ne.runtime.correlation import run_requested_model_scope
from ne.runtime.middleware import (
    GovernedToolExecutor,
    scoped_tool_executor,
    write_approval_floor_scope,
)
from ne.runtime.runner import ModelPlannedRun, run_model_planned_agent
from ne.runtime.source_results import ProtectedSourceResultGuard

MAX_AGENTIC_TOOL_CALLS = 24
AGENTIC_TOKEN_BUDGET = 262_144
AGENTIC_WALL_CLOCK_SECONDS = 120.0
MAX_AGENTIC_PARKED_WRITES = 5
#: The ONE gateway task every model-planned workflow uses. The choices endpoint,
#: authorization route and runtime settings all import this constant so the
#: console can never select against one task and execute against another.
AGENTIC_MODEL_TASK = "reason"

_META = sa.MetaData()
_RUNS = sa.Table(
    "ne_agentic_workflow_runs",
    _META,
    sa.Column("run_id", sa.String(64), primary_key=True),
    sa.Column("workflow_id", sa.String(128), nullable=False, index=True),
    sa.Column("trace_id", sa.String(128), nullable=False, unique=True),
    sa.Column("principal_id", sa.String(255), nullable=False),
    sa.Column("outcome", sa.String(32), nullable=False),
    sa.Column("record_json", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


class AgenticStepView(BaseModel):
    """One model-planned step and the row identities it actually served."""

    model_config = ConfigDict(extra="forbid")

    step: int
    tool: str
    stated_intent: str
    outcome: str
    served_row_ids: list[str] = []
    evidence_refs: list[str] = []
    principal_id: str


class AgenticBudgetView(BaseModel):
    """Server caps and observed usage; absent usage is unknown, never zeroed."""

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int
    token_budget: int
    wall_clock_seconds: float
    max_parked_writes: int
    requests_used: int | None = None
    tool_calls_used: int | None = None
    tokens_used: int | None = None
    exhausted_cap: str | None = None


class AgenticRunView(BaseModel):
    """Persisted run record returned by the model-planned endpoint."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    trace_id: str
    principal_id: str
    outcome: Literal["complete", "context-exhausted", "budget-exhausted"]
    model_planned: Literal[True] = True
    answer: str | None = None
    steps: list[AgenticStepView] = []
    served_row_ids: list[str] = []
    budgets: AgenticBudgetView


class AgenticRunStatusView(BaseModel):
    """Opaque server-owned ticket and its current durable terminal state."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    trace_id: str
    principal_id: str
    status: Literal["running", "complete", "context-exhausted", "budget-exhausted", "failed"]
    result: AgenticRunView | None = None
    error_class: str | None = None


@dataclass(frozen=True)
class AgenticWorkflowDefinition:
    workflow_id: str
    question: str


AGENTIC_WORKFLOWS: dict[str, AgenticWorkflowDefinition] = {
    "meridian-b1-disaster-hardship": AgenticWorkflowDefinition(
        workflow_id="meridian-b1-disaster-hardship",
        question=(
            "For Meridian Bank's DR-4830 pilot counties Columbia, Jefferson, and "
            "Richmond, identify active auto loans with a scheduled payment from "
            "2026-08-18 through 2026-09-01 that have no hardship offer and no "
            "repossession servicing note. Use the governed tools to establish every "
            "join, then answer with the exact served evidence identities."
        ),
    ),
    "meridian-b2-current-sop": AgenticWorkflowDefinition(
        workflow_id="meridian-b2-current-sop",
        question=(
            "Using only the current served Debit Card Dispute Handling SOP, identify "
            "open Meridian disputes overdue for provisional credit as of the served "
            "as-of date. Verify the complete open-dispute directory and cite the exact "
            "served dispute and policy identities."
        ),
    ),
    "meridian-b3-fabrication-refusal": AgenticWorkflowDefinition(
        workflow_id="meridian-b3-fabrication-refusal",
        question=(
            "Calculate the total amount of Meridian provisional credits issued late. "
            "If the served records do not contain the dates needed to determine "
            "lateness, refuse that calculation explicitly. Also report the answerable "
            "control total of recorded credits, with exact served evidence identities."
        ),
    ),
}

_INSTRUCTIONS = (
    "You are planning one Meridian banking workflow. You choose the tool sequence; "
    "the platform governs every call. Use only the tools shown to you. Every tool call "
    "must include a short step_intent explaining why that call is the next necessary "
    "step. Never infer a missing field, silently truncate a directory, or claim a "
    "deterministic fallback was model-planned. Cite exact served urn:ne entity/artifact "
    "identities in the final answer. A write, if one is offered, must also include "
    "evidence_refs containing only identities served earlier in this run."
)


def ensure_agentic_workflow_schema(engine: sa.Engine) -> None:
    """Create the durable run-record table on the platform audit database."""
    _META.create_all(engine, tables=[_RUNS])


def enable_agentic_mode(workflow: WorkflowView) -> WorkflowView:
    """Advertise the model-planned endpoint without changing deterministic mode."""
    if workflow.workflow_id not in AGENTIC_WORKFLOWS:
        return workflow
    modes = [mode for mode in workflow.modes if mode.mode != "agentic"]
    from ne.api.workflows import WorkflowModeView

    modes.append(
        WorkflowModeView(
            mode="agentic",
            status="available",
            endpoint=f"/v1/agentic-workflows/{workflow.workflow_id}/run",
        )
    )
    return workflow.model_copy(update={"modes": modes})


async def run_agentic_workflow(
    workflow_id: str,
    *,
    executor: GovernedToolExecutor,
    gateway: ModelGateway,
    settings: RuntimeSettings,
    sink: AuditSink,
    engine: sa.Engine,
    principal: Principal,
    trace_id: str,
    run_id: str | None = None,
    requested_model: str | None = None,
    requested_model_lane: Lane | None = None,
    source_guard: ProtectedSourceResultGuard | None = None,
) -> AgenticRunView:
    """Execute and persist one bounded caller-principal model-planned run."""
    definition = AGENTIC_WORKFLOWS[workflow_id]
    workflow = WORKFLOWS[workflow_id]
    granted = tuple((*workflow.reads, *workflow.writes))
    scoped = scoped_tool_executor(
        executor, granted=granted, scope=f"agentic-workflow:{workflow_id}"
    )
    # Explicitly scope even the legacy None case: a directly-invoked workflow
    # must clear any ambient choice rather than inherit a caller's prior run.
    # The historical private lane stays in RuntimeSettings for None; an
    # authorized alias/lane atomically overrides it inside GatewayModel.
    # NE-WF-SUBJECT-GATE: the agentic mode owes each subject the same per-subject
    # human gate as the deterministic engine — the obligation is the workflow's,
    # not the action's tier binding — so every write.* the model plans parks at
    # >= Tier-2 while its reads and drafts are untouched.
    with (
        run_requested_model_scope(requested_model, lane=requested_model_lane),
        write_approval_floor_scope(SUBJECT_WRITE_GATE_TIER),
    ):
        planned = await run_model_planned_agent(
            task=definition.question,
            instructions=_INSTRUCTIONS,
            principal=principal,
            executor=scoped,
            gateway=gateway,
            settings=settings.model_copy(
                update={"task": AGENTIC_MODEL_TASK, "lane": "private-vllm"}
            ),
            sink=sink,
            max_tool_calls=MAX_AGENTIC_TOOL_CALLS,
            total_tokens_limit=AGENTIC_TOKEN_BUDGET,
            wall_clock_seconds=AGENTIC_WALL_CLOCK_SECONDS,
            max_parked_writes=MAX_AGENTIC_PARKED_WRITES,
            source_guard=source_guard,
        )
    view = _view(workflow_id, trace_id, principal, planned, run_id=run_id)
    payload = view.model_dump_json()
    with engine.begin() as conn:
        values = dict(
            workflow_id=workflow_id,
            trace_id=trace_id,
            principal_id=principal.id,
            outcome=view.outcome,
            record_json=payload,
            created_at=datetime.now(UTC),
        )
        if run_id is None:
            conn.execute(_RUNS.insert().values(run_id=view.run_id, **values))
        else:
            updated = conn.execute(
                _RUNS.update()
                .where(_RUNS.c.run_id == run_id)
                .values(
                    outcome=view.outcome,
                    record_json=payload,
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("agentic run ticket disappeared before completion")
    return view


def create_agentic_run(
    engine: sa.Engine,
    *,
    workflow_id: str,
    trace_id: str,
    principal: Principal,
) -> AgenticRunStatusView:
    """Persist the opaque ticket before its background model loop starts."""
    run_id = uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            _RUNS.insert().values(
                run_id=run_id,
                workflow_id=workflow_id,
                trace_id=trace_id,
                principal_id=principal.id,
                outcome="running",
                record_json="{}",
                created_at=datetime.now(UTC),
            )
        )
    return AgenticRunStatusView(
        run_id=run_id,
        workflow_id=workflow_id,
        trace_id=trace_id,
        principal_id=principal.id,
        status="running",
    )


def fail_agentic_run(engine: sa.Engine, run_id: str, exc: BaseException) -> None:
    """Record only the safe exception class when a background run cannot finish."""
    payload = json.dumps({"error_class": type(exc).__name__})
    with engine.begin() as conn:
        conn.execute(
            _RUNS.update()
            .where(_RUNS.c.run_id == run_id)
            .values(outcome="failed", record_json=payload)
        )


def read_agentic_run(engine: sa.Engine, run_id: str) -> AgenticRunView | None:
    """Read one persisted record (used by the run screen's future history lane)."""
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(_RUNS.c.outcome, _RUNS.c.record_json).where(_RUNS.c.run_id == run_id)
        ).one_or_none()
    if row is None or row.outcome in {"running", "failed"}:
        return None
    return AgenticRunView.model_validate(json.loads(row.record_json))


def read_agentic_run_status(engine: sa.Engine, run_id: str) -> AgenticRunStatusView | None:
    """Read one ticket without reconstructing progress from client-side guesses."""
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(
                _RUNS.c.run_id,
                _RUNS.c.workflow_id,
                _RUNS.c.trace_id,
                _RUNS.c.principal_id,
                _RUNS.c.outcome,
                _RUNS.c.record_json,
            ).where(_RUNS.c.run_id == run_id)
        ).one_or_none()
    if row is None:
        return None
    result = (
        AgenticRunView.model_validate(json.loads(row.record_json))
        if row.outcome not in {"running", "failed"}
        else None
    )
    error_class = (
        str(json.loads(row.record_json).get("error_class")) if row.outcome == "failed" else None
    )
    return AgenticRunStatusView(
        run_id=row.run_id,
        workflow_id=row.workflow_id,
        trace_id=row.trace_id,
        principal_id=row.principal_id,
        status=row.outcome,
        result=result,
        error_class=error_class,
    )


def _view(
    workflow_id: str,
    trace_id: str,
    principal: Principal,
    planned: ModelPlannedRun,
    *,
    run_id: str | None = None,
) -> AgenticRunView:
    return AgenticRunView(
        run_id=run_id or uuid4().hex,
        workflow_id=workflow_id,
        trace_id=trace_id,
        principal_id=principal.id,
        outcome=planned.outcome,
        answer=planned.output if planned.outcome == "complete" else None,
        steps=[
            AgenticStepView(
                step=step.step,
                tool=step.tool,
                stated_intent=step.intent,
                outcome=step.outcome,
                served_row_ids=list(step.served_row_ids),
                evidence_refs=list(step.evidence_refs),
                principal_id=step.principal_id,
            )
            for step in planned.steps
        ],
        served_row_ids=list(planned.served_row_ids),
        budgets=AgenticBudgetView(
            max_tool_calls=MAX_AGENTIC_TOOL_CALLS,
            token_budget=AGENTIC_TOKEN_BUDGET,
            wall_clock_seconds=AGENTIC_WALL_CLOCK_SECONDS,
            max_parked_writes=MAX_AGENTIC_PARKED_WRITES,
            requests_used=planned.requests_used,
            tool_calls_used=planned.tool_calls_used,
            tokens_used=planned.tokens_used,
            exhausted_cap=planned.exhausted_cap,
        ),
    )
