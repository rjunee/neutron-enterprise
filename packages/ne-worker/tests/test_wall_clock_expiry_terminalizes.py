# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""POST_DEMO item 3: a turn whose wall clock expires MID-EFFECT still terminalizes.

THE PRODUCTION DEFECT, reproduced here at the workflow seam.

Two runs stranded on 2026-09-10 (``ec9a1dea``, ``b6e289da``): ``status=running``,
``terminal_at`` NULL, NO ``ne_agent_run_lifecycle_outbox`` row, lease
``released``, DBOS workflow ``PENDING`` with
``Asyncio task cancelled for workflow or step ... run_agent_run_workflow``.

THE MECHANISM, and it is deterministic rather than flaky.

``_LeaseExecutionBoundary.await_inflight`` deliberately SWALLOWS a
``CancelledError`` so an already-admitted model/Tool/audit effect is never
detached — the lease is renewed until the effect settles, and only then is the
cancellation re-delivered "so durable recovery observes one clear edge". It
re-delivered it twice: once from its own frame (correct), and then forever after
from a sticky ``_caller_cancelled`` flag consulted by every later
``heartbeat``/``checkpoint``/``await_inflight`` (the defect).

The run's OWN wall clock is a cancellation like any other:
``run_model_planned_agent`` opens ``asyncio.timeout(wall_clock_seconds)`` and the
deadline cancels the adapter task. When the deadline lands while that task sits
inside ``await_inflight``, the sequence is:

  1. the swallow sets the sticky flag;
  2. the effect settles — ``effect_settled:<addr>`` is written, so the record
     KNOWS whether it landed;
  3. ``await_inflight`` re-delivers once;
  4. that ``CancelledError`` reaches ``asyncio.timeout.__aexit__``, which spends
     exactly one ``uncancel()`` and converts it into the ``TimeoutError`` that
     produces ``budget-exhausted``/``wall-clock``. **The cancellation is now
     retired and the correct terminal has been computed.**
  5. the very next boundary call (``profile:agentic-after``) reads the sticky
     flag and raises a FRESH raw ``CancelledError``, discarding that terminal;
  6. the OUTER ``await_inflight`` — running in the WORKFLOW task, which nothing
     ever cancelled — reads the same shared flag and raises another.

``CancelledError`` is a ``BaseException``, so ``RunWorkflowService._execute``
caught none of it: the ``finally`` released the lease and the run was left
exactly as the two production rows show.

WHAT THE FIX IS. The boundary now remembers WHICH tasks it swallowed a
cancellation for, and re-delivers only while one of those tasks is still RUNNING
and still owed one — ``Task.cancelling()``, the stdlib's own count of
cancellations no scope has retired. An operator cancel, a shutdown or a DBOS task
cancel is never uncancelled, so it still reaches every frame of the run,
including the adapter task running underneath the cancelled workflow task
(``test_caller_cancellation_settles_admitted_effect_before_releasing_lease``, the
neighbouring guard, measures that as a second model effect issued after Cancel).
A deadline its own ``asyncio.timeout`` already converted is retired, and a task
that has finished cannot be owed anything.

WHAT IS ASSERTED, AND WHY NOT AN HTTP STATUS. Nothing here has an HTTP status;
the discriminating observables are the four the production rows are missing:
``ne_agent_runs.status``, ``terminal_at``, the lifecycle-outbox row, and the
``execution_failed`` checkpoint that names WHICH ceiling stopped the run.

THE TERMINAL VOCABULARY IS ``budget_exhausted``/wall-clock, NOT
``RunEffectSettlementUncertain``, and the ``effect_settled`` checkpoint asserted
below is why. ``RunEffectSettlementUncertain`` exists for a run "whose record
shows an issued governed effect ... whether that effect completed cannot be
determined from the record" (``run_settlement.py``). That is not this run:
``await_inflight`` never detaches an admitted effect, so it waited for the Tool,
wrote ``effect_settled:<addr>`` with its ``settlement`` verdict, and the record
therefore CAN say. Publishing the uncertain class here would claim an ignorance
the durable record contradicts — the mirror image of the fabrication it exists to
prevent. The honest terminal is the ceiling that actually stopped the turn, which
is the same terminal the deadline already publishes when it lands anywhere else.

NON-VACUITY. ``test_a_deadline_outside_the_effect_fence_publishes_the_same_terminal``
is the control: a deadline landing in the gateway's policy recheck — outside
``await_inflight``, the shape ``_RunContinuityGateway`` puts in front of every
model effect — reaches ``asyncio.timeout`` directly, never touches the sticky
flag, and terminalized correctly BEFORE this fix as well as after. It asserts the
same status/outcome words, so a "fix" that terminalized everything into one new
state, or that renamed the wall-clock terminal, fails it.
``test_an_unretired_cancellation_is_still_re_delivered`` is the guard rail on the
other side: an operator cancel or a shutdown must still cancel.

MEASURED ON THIS CHANGE (``origin/main`` 8c46e80b, CPython 3.13):

    parametrization              before               after
    tool  (inside the fence)     STRANDED             budget_exhausted
    model (inside the fence)     STRANDED             budget_exhausted
    control (outside the fence)  budget_exhausted     budget_exhausted

"STRANDED" is literally the production row: raw ``asyncio.CancelledError`` out of
``service.execute``, ``status='running'``, ``terminal_at`` NULL, zero outbox rows,
lease ``released``.

⚠️ THE LIVE MODEL-AWAIT RUN THAT TERMINALIZED CLEANLY (``d8e00f2b``) IS NOT
EVIDENCE THAT THE MODEL PATH IS IMMUNE — the middle row above says it is not. A
model effect reaches the provider through ``await_inflight`` exactly as a Tool
does. What differs is that ``_RunContinuityGateway`` performs seconds of policy
work (``verify_model_boundary``, ``choices``, ``authorize``, its serving probe)
OUTSIDE the fence before every model request, so a model-step deadline has a
wide window in which to land where the control test puts it. A Tool effect has no
such window: ``await_inflight`` spans essentially the whole call, which is why the
defect reads as tool-specific in the field.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.runtime import ToolSpec
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.config import RuntimeSettings
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_store import RunState
from ne.runtime.run_workflow import (
    RunWorkflowService,
    RunWorkflowStateStore,
    _LeaseExecutionBoundary,
    ne_agent_run_execution_checkpoints,
    ne_agent_run_lifecycle_outbox,
)
from ne.runtime.runner import run_model_planned_agent

from .test_agent_run_workflow import _harness, _queued_run, _workflow_request

#: Short enough that the deadline lands inside the blocked effect on a loaded
#: box, long enough that the agent's own setup never eats it.
_WALL = 0.6
#: Comfortably past the deadline, so the blocked effect settles only after it.
_BLOCK = 2.0
#: Spelled exactly as ``ne.worker.run_execution._budget_exhausted_reason`` spells
#: it, from the same ceiling, so the sentence cannot drift from what was enforced.
_WALL_CLOCK_SENTENCE = f"stopped: the turn's {_WALL:.0f}-second wall-clock budget was exhausted"


class _NullSink:
    """The audit ledger is proven elsewhere; this file is about terminals."""

    async def append(self, event: AuditEventLike) -> None:
        return None

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        del start, end
        return AuditVerification(valid=True, checked=0)


def _completion(payload: dict[str, object]) -> Completion:
    return Completion(
        text=json.dumps(payload),
        model_id="stub",
        usage=Usage(prompt_tokens=3, completion_tokens=5),
    )


class _ToolThenAnswerGateway:
    """First reply is a governed read, then a final answer. Optionally slow."""

    def __init__(self, *, blocks: bool) -> None:
        self.blocks = blocks
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> Completion:
        del request
        self.calls += 1
        if self.blocks:
            await asyncio.sleep(_BLOCK)
        if self.calls == 1:
            return _completion(
                {
                    "tool_call": {
                        "name": "read.kg.directory",
                        "arguments": {"step_intent": "Find the evidence."},
                    }
                }
            )
        return _completion({"final_text": "unreached"})

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


class _ReadExecutor:
    """One governed read that optionally outlives the turn's wall clock."""

    def __init__(self, *, blocks: bool) -> None:
        self.blocks = blocks
        self.calls = 0

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                name="read.kg.directory",
                description="Read the evidence directory.",
                tier=ApprovalTier.TIER_0,
                input_schema={"type": "object", "properties": {}},
            ),
        )

    async def execute(self, tool: str, args: Mapping[str, Any], *, principal: Principal) -> str:
        del tool, args, principal
        self.calls += 1
        if self.blocks:
            await asyncio.sleep(_BLOCK)
        return "row | status\nurn:ne:probe:row-a | served"


class _PolicyRecheckGateway:
    """``_RunContinuityGateway``'s shape: policy work OUTSIDE the effect fence.

    The production gateway rechecks catalog visibility, the frozen policy,
    ``verify_model_boundary`` and ``authorize`` (with its serving probe) before
    it hands the request to the receipt-gated gateway. None of that is wrapped in
    ``await_inflight``, so a deadline landing there is delivered straight to
    ``asyncio.timeout``. That is the shape this stands in for.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def complete(self, request: CompletionRequest) -> Completion:
        await asyncio.sleep(_BLOCK)
        return await self._inner.complete(request)

    def stream(self, request: CompletionRequest) -> Any:
        raise NotImplementedError


class _WallClockAdapter:
    """The production agentic adapter's shape: real runner, real boundary.

    ``ne.worker.run_execution`` is not importable at this seam without the whole
    worker composition root, so this reproduces the two things that matter and
    nothing else: the agent loop runs under a real ``asyncio.timeout`` against
    the workflow's own ``ReceiptGatedGateway``/``ReceiptGatedToolExecutor``, and
    the budget terminal is mapped exactly as ``_agentic_result`` maps it —
    including the ``heartbeat("profile:agentic-after")`` that is the FIRST
    boundary call after the loop returns, and therefore the first place a stale
    sticky cancellation can discard the terminal that was just computed.
    """

    def __init__(self, *, policy_recheck: bool = False) -> None:
        self.policy_recheck = policy_recheck
        self.loop_outcome: str | None = None

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        await context.boundary.heartbeat("profile:agentic-before")
        gateway = _PolicyRecheckGateway(context.gateway) if self.policy_recheck else context.gateway
        result = await run_model_planned_agent(
            task="do the bounded work",
            instructions="use governed tools",
            principal=Principal(id=context.run.owner_principal_id),
            executor=context.executor,
            gateway=gateway,
            settings=RuntimeSettings(),
            sink=_NullSink(),
            max_tool_calls=4,
            total_tokens_limit=1000,
            wall_clock_seconds=_WALL,
            max_parked_writes=1,
            boundary=context.boundary,
        )
        self.loop_outcome = result.outcome
        await context.boundary.heartbeat("profile:agentic-after")
        if result.outcome != "budget-exhausted":
            raise AssertionError(f"the wall clock did not stop this turn: {result.outcome}")
        assert result.exhausted_cap == "wall-clock"
        return RunExecutionResult(
            outcome="budget_exhausted",
            exhausted_reason=_WALL_CLOCK_SENTENCE,
        )


def _drive(
    tmp_path: Path,
    *,
    blocked_effect: str,
    policy_recheck: bool = False,
) -> dict[str, Any]:
    """Run one turn to its end and read back every durable observable."""

    harness = _harness(tmp_path)
    gateway = _ToolThenAnswerGateway(blocks=blocked_effect == "model")
    executor = _ReadExecutor(blocks=blocked_effect == "tool")
    adapter = _WallClockAdapter(policy_recheck=policy_recheck)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime, lease_seconds=3.0),
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=gateway,
        executor=executor,
        adapter=adapter,
        config_hash=harness.config_hash,
    )
    observed: dict[str, Any] = {}

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id=f"dispatch-{blocked_effect}")
        try:
            observed["outcome"] = (await service.execute(request)).outcome
            observed["escaped"] = None
        except BaseException as exc:  # a BaseException escaping IS the defect
            observed["outcome"] = None
            observed["escaped"] = type(exc).__name__
        terminal = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert terminal is not None
        observed["status"] = terminal.status
        observed["terminal_at"] = terminal.terminal_at
        with harness.runtime.connect() as conn:
            observed["outbox"] = [
                (str(row.terminal_status), str(row.outcome))
                for row in conn.execute(
                    sa.select(ne_agent_run_lifecycle_outbox).where(
                        ne_agent_run_lifecycle_outbox.c.run_id == run.run_id
                    )
                ).all()
            ]
            observed["checkpoints"] = {
                str(row.stage): json.loads(str(row.safe_detail))
                for row in conn.execute(
                    sa.select(
                        ne_agent_run_execution_checkpoints.c.stage,
                        ne_agent_run_execution_checkpoints.c.safe_detail,
                    ).where(ne_agent_run_execution_checkpoints.c.run_id == run.run_id)
                ).all()
            }
        observed["loop_outcome"] = adapter.loop_outcome
        observed["effect_calls"] = executor.calls if blocked_effect == "tool" else gateway.calls

    asyncio.run(scenario())
    return observed


def _settlements(checkpoints: Mapping[str, Any]) -> list[str]:
    return [
        str(detail["settlement"])
        for stage, detail in sorted(checkpoints.items())
        if stage.startswith("effect_settled:")
    ]


@pytest.mark.parametrize("blocked_effect", ["tool", "model"])
def test_a_deadline_inside_the_effect_fence_still_reaches_a_terminal(
    tmp_path: Path, blocked_effect: str
) -> None:
    """The stranding itself: the turn's wall clock expires MID-EFFECT.

    Without the fix this run is the production row — a raw
    ``asyncio.CancelledError`` out of ``service.execute``, ``status='running'``,
    ``terminal_at`` NULL, zero outbox rows — even though ``loop_outcome`` proves
    the runner had already computed the right terminal and the boundary threw it
    away.
    """

    observed = _drive(tmp_path, blocked_effect=blocked_effect)

    # The defect's own signature, stated as the thing that must NOT happen.
    assert observed["escaped"] is None, (
        f"a {observed['escaped']} escaped the workflow: the run is stranded"
    )
    assert observed["status"] is RunState.BUDGET_EXHAUSTED
    assert observed["terminal_at"] is not None
    assert observed["outbox"] == [("budget_exhausted", "budget_exhausted")]
    assert observed["checkpoints"]["execution_failed"] == {"error_reason": _WALL_CLOCK_SENTENCE}
    assert observed["outcome"] == "budget_exhausted"

    # The run stopped on a CEILING, and that is what it says. The runner reached
    # `_wall_clock_terminal`; nothing downstream renamed it.
    assert observed["loop_outcome"] == "budget-exhausted"

    # WHY THIS IS NOT `RunEffectSettlementUncertain`. The blocked effect was
    # admitted exactly once and `await_inflight` waited for it rather than
    # detaching it, so the durable record states its settlement. A terminal
    # claiming the platform cannot tell would contradict this row.
    assert observed["effect_calls"] == 1
    assert "success" in _settlements(observed["checkpoints"])


def test_a_deadline_outside_the_effect_fence_publishes_the_same_terminal(
    tmp_path: Path,
) -> None:
    """THE CONTROL — green before this fix and after, with the SAME words.

    The deadline lands in the gateway's policy recheck, which is not wrapped in
    ``await_inflight``, so it is delivered straight to ``asyncio.timeout``, never
    touches the sticky flag and always terminalized correctly. It is here so the
    fix cannot pass by terminalizing everything into some new state, and so the
    wall-clock terminal's own vocabulary is pinned rather than re-invented for
    the fenced path.
    """

    observed = _drive(tmp_path, blocked_effect="none", policy_recheck=True)

    assert observed["escaped"] is None
    assert observed["status"] is RunState.BUDGET_EXHAUSTED
    assert observed["terminal_at"] is not None
    assert observed["outbox"] == [("budget_exhausted", "budget_exhausted")]
    assert observed["checkpoints"]["execution_failed"] == {"error_reason": _WALL_CLOCK_SENTENCE}
    assert observed["loop_outcome"] == "budget-exhausted"
    # The deadline landed BEFORE the model effect was ever admitted, so there is
    # no settlement to report — the exact opposite of the fenced case above, and
    # the reason the two cannot be collapsed into one assertion.
    assert _settlements(observed["checkpoints"]) == []


def test_an_unretired_cancellation_is_still_re_delivered(tmp_path: Path) -> None:
    """THE GUARD RAIL: operator cancel and shutdown must still cancel.

    ``await_inflight`` swallows the cancellation to keep the exact lease alive
    until the admitted effect settles, and a caller that catches the re-delivery
    must not be able to keep using the boundary afterwards. Nothing here calls
    ``uncancel()``, so the cancellation is still owed and every later boundary
    call must still raise — which is precisely what a wall-clock deadline's own
    ``asyncio.timeout`` scope makes false by retiring the one it issued.

    The pair below is the whole discrimination, on ONE boundary object, with the
    ONLY difference being who retired the cancellation.
    """

    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime, lease_seconds=3.0)

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id="dispatch-guard-rail")
        claim = state.claim(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            owner_principal_id=request.owner_principal_id,
            config_hash=request.config_hash,
        )
        boundary = _LeaseExecutionBoundary(state, claim)
        settled = asyncio.Event()
        redelivered: list[str] = []

        async def admitted_effect() -> str:
            await settled.wait()
            return "the effect landed"

        async def cancelled_caller() -> None:
            try:
                await boundary.await_inflight(admitted_effect(), stage="run-tool:wait:0")
            except asyncio.CancelledError:
                redelivered.append("await_inflight")
            # A caller that caught the re-delivery is still cancelled: nobody
            # uncancelled it, so the boundary must refuse to start new work.
            try:
                await boundary.heartbeat("run-execution:after-catch")
            except asyncio.CancelledError:
                redelivered.append("heartbeat")

        task = asyncio.ensure_future(cancelled_caller())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        # The effect was never detached: the lease is still held and the caller
        # is still inside the fence.
        assert not task.done()
        settled.set()
        await task

        assert redelivered == ["await_inflight", "heartbeat"]
        assert task.cancelling() == 1, "the cancellation is still owed to this task"
        boundary.close()
        state.release(claim)

    asyncio.run(scenario())
