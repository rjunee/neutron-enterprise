# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""POST_DEMO item 1, proven at the REAL gate: a parked run CAN terminalize.

WHY THIS FILE EXISTS, AND EXACTLY WHAT THE EXISTING COVERAGE ALREADY HAS.

Item 1 is "parked runs can never terminalize". Two halves of that arc are each
already covered, and NEITHER FILE COVERS BOTH:

* ``packages/ne-runtime/tests/test_agentic_ladder_parks_canonical_approval.py``
  parks through the REAL gate — real ``AgenticPlanner``, real
  ``_require_canonical_run_approval``, real ``create_wait`` — and stops at
  ``waiting_approval``. No decision, no expiry, no terminal.
* ``packages/ne-api/tests/test_run_approval_resume_continuation.py`` carries
  the expiry decision all the way to the terminal, but every one of its parks
  is a test adapter calling :meth:`RunApprovalCheckpointStore.create_wait`
  DIRECTLY, with hand-supplied ``action``, ``tier``, ``policy_hash``,
  ``source_stamps`` and ``standing_lifetime_s`` — a deliberate, documented
  double (``run_approval_park.py``). The gate is not on that path.

Nothing joins them, and on the deployment the terminalization path is
resume/decision-triggered with NO RUN HAVING PARKED SINCE THE FIX. Zero
denominator, not a passing test.

WHAT THIS FILE ADDS: the join, on the one Tier-2 binding that can be exercised
on the deployment itself. The park comes from the production chain and nothing
else:

    ReceiptGatedToolExecutor.execute   (mints step_id/tool_call_id, writes the
                                        run.tool.pre_effect receipt, opens the
                                        run_tool_call_scope)
      -> GovernedToolExecutor.execute  (guardrails -> PDP -> _effective_tier)
        -> _require_named_approval     (current_run_tool_call() is not None)
          -> _require_canonical_run_approval
            -> _run_source_references  (the served-evidence completeness gate)
            -> create_wait             -> raise RunApprovalWaiting

on a REAL registered Tier-2 binding, then the REAL workflow park leg, then the
REAL HTTP approve route.

WHY ``write.external.demo.send``. It is the ONE registered Tier-2 action on
this tree whose handler performs no I/O of any kind
(``ne.api.demo_seams._demo_writeback_handler`` returns a constant string —
that is its whole body). Every other Tier-2 grant reaches a real external
system: ``write.external.email.send`` would need a live Microsoft Graph send.
An inert action makes this arc runnable ON THE DEPLOYMENT, not only in a test,
with structurally zero outward effect — which is the only way item 1 stops
being a zero denominator. The action is registered exactly when
``ApiSettings.demo_writeback_tool`` is on, and this file asserts that too, so
nothing here can drift onto a different, effect-bearing tool.

WHAT IS ASSERTED, AND WHY NOT THE HTTP STATUS. A 409 plus an audit row is NOT
evidence that the run terminalized: ``_refuse_expired_authorization`` emits
both from a path that also runs when ``terminalize_authorization_expired``
DECLINES (returns ``False``). The three observables below are the ones only a
real terminalization writes, and the first is the specific vacuity trap — the
lazy approval sweep expires a row with ``decided_reason`` NULL:

  1. ``ne_approval_requests.decided_reason == 'authorization_expired'``
  2. ``ne_agent_runs.status == 'failed'`` with ``terminal_at`` set
  3. an ``execution_failed`` execution checkpoint whose ``safe_detail`` carries
     ``error_class="AuthorizationExpired"``

NON-VACUITY. Make ``_terminalize_authorization_expired_sync`` return ``False``
before it writes anything (the shape of the defect item 1 names — the refusal
happens, the run is left ``waiting_approval``) and all three go RED while the
HTTP status stays 409 and the ``api.approval.authorization_expired`` audit row
is still written. That is the whole point of asserting these three and not the
status.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa

from ne.api.demo_seams import DEMO_WRITEBACK_ACTION
from ne.api.knowledge_source_evidence import (
    KnowledgeSourceEvidenceAuthor,
    current_existing_knowledge_read,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import EvidenceAssurance
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.config import ToolArgPolicy
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.runner import _protected_tool_evidence_refs
from ne.runtime.source_results import authenticated_request_scope, protected_tool_evidence_scope
from ne.runtime.tools import ToolBinding

from .conftest import AppBuilder, get_state, run
from .run_approval_park import (
    APPROVER,
    APPROVER_GROUP,
    RecordingEnqueuer,
    admit_run,
    admit_sources,
    assert_park_left_production_rows,
    build_service,
    workflow_request,
)

#: The Tier-0 read that serves the run's ONE protected source reference.
#: ``_run_source_references`` refuses a Tier-2 park with an empty evidence
#: carrier ("a Tier-2 effect with no server-observed evidence never parks an
#: approval"), so the ladder must have a real governed read below the write —
#: which is also the production shape. The handler is the PRODUCTION evidence
#: author (``KnowledgeSourceEvidenceAuthor``) over the app's own Run store, so
#: the reference, its evidence row and its audit receipt are all real.
READ_TOOL = "read.demo.evidence"

#: The inert write-back's only argument. A recipient LABEL: the handler never
#: reads it and never sends anything to it.
WRITE_ARGS: dict[str, object] = {"to": "meridian-servicing-ops"}


def _read_binding(holder: dict[str, Any]) -> ToolBinding:
    async def _handler(_args: Mapping[str, object]) -> str:
        existing = current_existing_knowledge_read()
        assert existing is not None, "the read must run inside a canonical Run Tool scope"
        author = KnowledgeSourceEvidenceAuthor(holder["state"].agent_run_store)
        claimed = await author.claim(
            existing=existing,
            protected_result=b"meridian hardship evidence",
            locator=None,
            source_stamps=(holder["stamp"],),
            read_kind="search",
            assurance=EvidenceAssurance.DIRECT,
        )
        return claimed.protected_ref

    return ToolBinding(
        spec=ToolSpec(
            name=READ_TOOL,
            description="governed tier-0 read serving one protected source reference",
            tier=ApprovalTier.TIER_0,
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
        handler=_handler,
    )


class _LadderAdapter:
    """Read, then call the inert Tier-2 write through the REAL governed gate.

    Nothing here writes a governance row. The only thing this stands in for is
    the agent loop's own two acts — announcing the plan ordinal and binding the
    server-observed evidence around the escalated call. Both are copied from
    ``ne.runtime.runner._RecordedExecutor``, including its selector
    (``_protected_tool_evidence_refs``), which is imported rather than
    re-implemented.
    """

    def __init__(self) -> None:
        self.read_result = ""

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        principal = Principal(id=context.run.owner_principal_id, groups=())
        self.read_result = await context.executor.execute(READ_TOOL, {}, principal=principal)
        served = set(context.executor.served_protected_refs)
        assert served, "the governed read must have served a protected reference"
        await context.boundary.checkpoint(
            stage="agentic-intent:0",
            safe_detail={
                "ordinal": 0,
                "tool": DEMO_WRITEBACK_ACTION,
                "step_intent": "queue the inert demo write-back",
            },
        )
        with protected_tool_evidence_scope(_protected_tool_evidence_refs(served)):
            # RunApprovalWaiting is raised INSIDE this call by the real gate and
            # propagates to the workflow's own park leg. If this line ever
            # RETURNS, the tier-2 human gate did not fire and the assertion in
            # the test body below reports it.
            await context.executor.execute(DEMO_WRITEBACK_ACTION, WRITE_ARGS, principal=principal)
        raise AssertionError("the inert tier-2 write executed without a human approval")


def _approval_row(runtime_url: str, approval_id: str) -> Any:
    from ne.runtime.approval_store import ne_approval_requests

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.select(ne_approval_requests).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).one()
    finally:
        engine.dispose()


def _run_row(runtime_url: str, run_id: str) -> Any:
    from ne.runtime.run_store import ne_agent_runs

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == run_id)
            ).one()
    finally:
        engine.dispose()


def _execution_failed_details(runtime_url: str, run_id: str) -> list[dict[str, Any]]:
    from ne.runtime.run_workflow import ne_agent_run_execution_checkpoints

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints.c.safe_detail).where(
                    ne_agent_run_execution_checkpoints.c.run_id == run_id,
                    ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
                )
            ).all()
    finally:
        engine.dispose()
    return [json.loads(row.safe_detail) for row in rows]


def _tool_execute_actions(audit_url: str) -> list[tuple[str, str]]:
    from ne.audit.store import ne_audit_log

    engine = sa.create_engine(audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.action, ne_audit_log.c.resource).where(
                    ne_audit_log.c.action == "tool.execute"
                )
            ).all()
    finally:
        engine.dispose()
    return [(str(row.action), str(row.resource)) for row in rows]


def _make(make_app: AppBuilder, holder: dict[str, Any]) -> Any:
    return make_app(
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        demo_writeback_tool=True,
        extra_tool_bindings=(_read_binding(holder),),
        extra_tool_policies={READ_TOOL: ToolArgPolicy(args={})},
    )


def test_the_inert_tier2_action_is_registered_only_with_the_demo_seam(
    make_app: AppBuilder,
) -> None:
    """The park below rests on a REGISTERED Tier-2 binding, not a test double.

    Also the guard against this file quietly proving something about an
    effect-bearing tool: the action it parks on is the one whose handler is
    ``_demo_writeback_handler``, and that binding exists only when the demo
    seam is on.
    """

    from ne.contracts import reset_config_hash_for_testing

    holder: dict[str, Any] = {}
    reset_config_hash_for_testing()
    off = get_state(make_app(protected_transport=True).app)
    assert DEMO_WRITEBACK_ACTION not in {spec.name for spec in off.executor.tool_specs}

    # The seam MOVES the frozen config_hash (an honest stamp — the
    # `demo_writeback_tool` precedent, `test_demo_seams.py`), so the second
    # composition needs its own freeze.
    reset_config_hash_for_testing()
    on = get_state(_make(make_app, holder).app)
    spec = next(s for s in on.executor.tool_specs if s.name == DEMO_WRITEBACK_ACTION)
    assert spec.tier is ApprovalTier.TIER_2
    assert DEMO_WRITEBACK_ACTION in on.govern_settings.guardrails.tools


def test_a_run_parked_at_the_inert_gate_terminalizes_when_authorization_expired(
    make_app: AppBuilder,
) -> None:
    """POST_DEMO item 1, end to end, with zero outward effect anywhere on the path.

    A canonical Run reads governed evidence, escalates to the INERT Tier-2
    write, parks through ``_require_canonical_run_approval``; the run owner's
    stored authorization snapshot is already past its ``expires_at``; a named
    approver decides over the real HTTP route; the attempt terminalizes.

    MUTATION-CHECK (the non-vacuity claim): return ``False`` from
    ``RunApprovalCheckpointStore._terminalize_authorization_expired_sync``
    before its first write and the three terminalization assertions go RED
    while the 409 and the ``api.approval.authorization_expired`` audit row are
    unchanged.
    """

    holder: dict[str, Any] = {}
    h = _make(make_app, holder)
    state = get_state(h.app)
    holder["state"] = state
    state.agent_run_enqueuer = RecordingEnqueuer()
    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    assert checkpoints is not None

    stamps = admit_sources(state, key_prefix="item1-inert", count=1)
    holder["stamp"] = stamps[0]

    # The owner's stored snapshot is ALREADY past its horizon — the live
    # ne-pilot realm's accessTokenLifespan is 900 s, and this is the decision
    # that arrives after it.
    parked = run(
        admit_run(
            state,
            action=DEMO_WRITEBACK_ACTION,
            arguments=WRITE_ARGS,
            owner_token_ttl_s=-60,
        )
    )

    adapter = _LadderAdapter()
    service, engine = build_service(state, h.runtime_url, adapter)
    try:
        with authenticated_request_scope(parked.owner_context):
            result = run(service.execute(workflow_request(parked)))
        assert result.outcome == "waiting_approval"
    finally:
        engine.dispose()

    join = run(
        checkpoints.find_join_for_attempt(run_id=parked.run_id, attempt_id=parked.attempt_id)
    )
    assert join is not None, "the real gate parked no approval join"
    # THE PARK IS THE REAL GATE'S, on the real action, at the real tier.
    assert join.action == DEMO_WRITEBACK_ACTION
    assert int(join.tier) == int(ApprovalTier.TIER_2)
    parked.approval_id = join.approval_id
    assert_park_left_production_rows(state, h.runtime_url, parked)

    # --- the decision, over the REAL HTTP approve route --------------------
    async def _approve() -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/approvals/{parked.approval_id}/approve",
                json={},
                headers=h.bearer(APPROVER, groups=[APPROVER_GROUP]),
            )

    decided = run(_approve())
    assert decided.status_code == 409, decided.text

    # (1) THE VACUITY TRAP. The lazy approval sweep also writes status
    # 'expired' — with decided_reason NULL. Only the terminalization writes
    # the SPEC-named reason.
    approval = _approval_row(h.runtime_url, parked.approval_id)
    assert str(approval.status) == "expired"
    assert str(approval.decided_reason) == "authorization_expired"

    # (2) The attempt is terminally dead, not stranded in waiting_approval.
    run_row = _run_row(h.runtime_url, parked.run_id)
    assert str(run_row.status) == "failed"
    assert run_row.terminal_at is not None

    # (3) The reader is told WHY, in the run's own execution checkpoint.
    details = _execution_failed_details(h.runtime_url, parked.run_id)
    assert [d["error_class"] for d in details] == ["AuthorizationExpired"]
    assert details[0]["error_reason"] == "authorization_expired"

    # ZERO EFFECT. The inert handler never ran — and it could not have done
    # anything if it had. The governed read below it DID run, which is what
    # makes the absence of the write's own tool.execute meaningful.
    executed = _tool_execute_actions(h.audit_url)
    assert (f"tool:{READ_TOOL}") in {resource for _action, resource in executed}
    assert (f"tool:{DEMO_WRITEBACK_ACTION}") not in {resource for _action, resource in executed}
    assert state.agent_run_enqueuer.requests == []
