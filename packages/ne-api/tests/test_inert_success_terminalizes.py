# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""POST_DEMO item 2, proven at the REAL gate: a run terminalizes AFTER the effect.

WHY THIS FILE EXISTS.

Item 2 is "runs don't terminalize after a successful send". Its shape is the
2026-08-28 production stranding: three ``write.external.email.send`` executions
succeeded (16:46:44 / 17:27:37 / 18:53:48) and all three runs are STILL
``status='running'`` — sent, then no terminal. The fix (F2 of ``9ab1440c``) is
the observer seed at ``run_workflow.py:2989``; the record called item 2
owner-gated because proving it "requires an actual successful outward send".

IT DOES NOT, AND THE REASON IS THAT THE POST-SUCCESS PATH IS ACTION-AGNOSTIC.
Everything between "the governed handler returned" and "the run is terminal" is
written without reference to which tool ran:

* ``RunWorkflowService.execute`` names the action exactly once — as an opaque
  value it forwards (``continuation.action`` -> ``ReceiptGatedToolExecutor``).
  The seed, the drain, ``_finalizer.observe``, ``_finalizer.finalize`` and
  ``publish_terminal`` never read it.
* ``RunFinalizer._observe_sync`` — the frame that RAISES "Run delivery observer
  did not capture the complete receipt set", i.e. item 2's actual defect —
  selects ``ne_agent_delivery_receipts`` on ``(run_id, receiving_attempt_id)``
  alone. No action, no tool, no recipient.
* The production continuation adapter
  (``ne.worker.run_execution._execute_effect_continuation``) executes
  ``continuation.action`` with ``continuation.arguments`` under
  ``direct_publication_scope`` and returns a content-free
  ``_source_free_envelope`` notice. Identical for every Tier-2 action.

Everything action-SPECIFIC about the email send — the dedup claim, the mailbox
and recipient allowlists, the Graph client, ``extract_recipients`` /
``extract_egress_urls`` — lives INSIDE the ``ToolBinding`` handler, below
``GovernedToolExecutor._run_handler``, and both bindings hand the same thing
back up: a ``str``. ``ReceiptGatedToolExecutor`` then does one thing with it
(``decode_protected_reference``), and neither a send's ``ActionResult`` JSON nor
the inert action's confirmation string is a protected reference. So the two
actions rejoin BEFORE the first line of the terminalization path and never
diverge again.

WHAT THIS FILE ADDS over ``test_run_approval_resume_continuation.py``'s
flagship (``test_approved_continuation_executes_the_pinned_effect_and_completes``):
that test's park is the documented double — ``ParkingAdapter``/``create_wait``
called directly on a TEST-registered ``write.external.demo.notify`` binding, so
``middleware._require_canonical_run_approval`` is not on its path and no
REGISTERED production action is either. This file joins the two halves the same
way ``test_inert_park_terminalizes.py`` does for item 1: the park comes from the
production chain

    ReceiptGatedToolExecutor.execute -> GovernedToolExecutor.execute
      -> _require_named_approval -> _require_canonical_run_approval
        -> _run_source_references -> create_wait -> raise RunApprovalWaiting

on the REGISTERED Tier-2 ``write.external.demo.send`` binding, and the decision
is IN-WINDOW, so the REAL ``resume_run_approval_step`` executes the inert
handler and the run must terminalize on the SUCCESS path.

THE PRE-PARK DELIVERIES ARE THE WHOLE POINT. Item 2's defect is invisible with
an empty ``ne_agent_delivery_receipts`` set — the finalizer's receipt-set
comparison passes trivially. So the park leg here DELIVERS its governed read to
the model through the app's own composed guard (``resolve_for_model_send``,
which writes the durable receipt, its delivery audit receipt and the observer
entry) before it escalates, exactly as the agent loop does, and
``test_the_park_persists_a_real_pre_park_delivery`` refuses to let that silently
become zero.

WHAT IS ASSERTED, AND WHY NOT THE HTTP STATUS. The approve route answers 200
whether or not the resume later terminalizes — it returns before the resume runs
at all. The discriminating observables are all durable:

  1. ``ne_agent_runs.status == 'completed'`` with ``terminal_at`` set
  2. ``ne_agent_run_lifecycle_outbox.outcome == 'approved_effect_complete'``
  3. ``ne_agent_run_continuation_checkpoints.status == 'consumed'``
  4. the inert handler's own ``tool.execute`` audit row + its return string

NON-VACUITY, MEASURED — three columns, and the third is the point.

  A  HEAD.                          run=completed, terminal_at set,
                                    outbox outcome=approved_effect_complete.
  B  ``AgentRunStore.delivered_receipt_ids`` stubbed to ``()`` (F2's pre-fix
     build: an unseeded observer over a REAL persisted delivery set).
                                    resume raises "unavailable terminal delivery
                                    observation is mismatched"; run=RUNNING,
                                    terminal_at NULL, NO outbox row.
  C  the same stub, but the ladder does not deliver its read (zero persisted
     receipts).                     GREEN AGAIN — completed, terminal_at set,
                                    approved_effect_complete.

BYTE-IDENTICAL ACROSS ALL THREE: the approve route's 200, the ``tool.execute``
audit rows, the inert handler's returned string, and
``continuation_checkpoints.status='consumed'``. So the HTTP status proves
nothing here, "the handler ran" proves nothing here, and the consumed fence
proves nothing here EITHER — it is asserted below for F1's reason (one approval
authorized exactly one execution), not for item 2's. Only the run status, the
``terminal_at`` stamp and the durable lifecycle-outbox row move.

Column C is why ``test_the_park_persists_a_real_pre_park_delivery`` exists: it
is the exact shape in which this whole file would pass over a broken build.

ZERO OUTWARD EFFECT. The action executed is the one whose handler body is a
single ``return`` of a constant (``ne.api.demo_seams._demo_writeback_handler``):
no recipients extractor, no egress-URL extractor, no cost extractor, no client,
no I/O. Asserted below on the REGISTERED binding, not on a test double.

THE ONE STUB, AND WHY IT IS NOT POST_DEMO ITEM 6.

``_resume_via_worker_step`` monkeypatches ``ne.worker.workflows.worker_platform``
and carries a ``governance-stub:`` waiver for the guardian in
``packages/ne-runtime/tests/test_governance_stub_guardian.py``. A governance
proof that stubs a governance chokepoint deserves the suspicion that guardian
exists to create, so here is the argument in full.

WHAT ``worker_platform()`` IS. Not a governance component. It is the ne-worker
PROCESS's composition-root lookup — a module global installed exactly once by
``ne.worker.main`` via ``set_worker_platform(platform)`` before ``DBOS.launch``
(``ne/worker/runtime_state.py``). This file is the ne-api test harness. That
worker boot never runs here, so the real lookup does not return a weaker
platform — it RAISES ``RuntimeError("worker_platform() called before
set_worker_platform()")``. The stub SUPPLIES the root the absent boot would have
supplied. It replaces nothing.

WHAT IT SUPPLIES. The four attributes ``resume_run_approval_step`` reads, each
of them THIS app's own composed object: ``state.gate`` (the real
``DurableApprovalGate``, the same instance the HTTP approve route above just
decided through), ``state.source_guard`` (the real ``ProtectedSourceResultGuard``
that ``checkpoints.authorize(guard=...)`` re-authorizes against),
``state.agent_run_store`` (the real ``AgentRunStore``) and an engine over the
run's own runtime database. ``_resume_via_worker_step`` ASSERTS all three types
rather than asserting them in prose. Contrast 2026-08-28, which is what item 6
and the guardian are about: ``state.source_guard = SimpleNamespace()`` — a
chokepoint replaced by a fake, so ``authorize`` returning ``RETRYABLE_UNKNOWN``
for every parked approval shipped green.

SO THE GOVERNANCE RUNS FOR REAL: ``checkpoints.get_join``, the action-hash /
principal / policy-hash binding check, ``require_resume_request``,
``record_approval_audit_receipt``, ``reconcile_approval_join``,
``load_checkpoint``, the ``approval_resume_fence`` status reads, the
approved->resumed CAS, ``authorize`` against the real guard, the
``AuthorizationExpiredError`` -> ``terminalize_authorization_expired`` branch,
and the ``RunFinalizer`` receipt-set comparison that IS item 2's defect. Column B
of the non-vacuity table above is the proof that none of it is bypassed: stub
one production method and this file goes RED.

WHY NOT REMOVE THE STUB INSTEAD. ``set_worker_platform`` — the production
installer — takes a real ``ne.api.platform.Platform``, and ``create_app`` keeps
the one it builds as a local: no seam exposes it, and surfacing one is a
production change this test does not get to make. Constructing a second
``Platform`` by hand means inventing ~14 governed singletons ``ApiState`` does
not carry (``ledger``, ``counter``, ``scope_rail``, ``credential_service``,
``chunk_store``, ...) — fabricating a platform to avoid stubbing a lookup, which
is strictly worse. Calling ``build_platform`` a second time composes a DIFFERENT
gate over the same databases, while ``build_service`` above composes the workflow
service from ``state``'s gate: the resume would then straddle two compositions.
The redirect at the lookup, over one set of real objects, is the honest option.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.api.demo_seams import DEMO_WRITEBACK_ACTION
from ne.api.knowledge_source_evidence import (
    KnowledgeSourceEvidenceAuthor,
    current_existing_knowledge_read,
)
from ne.contracts.runtime import ToolSpec
from ne.contracts.source_authority import EvidenceAssurance, SourceBoundToolResultRef
from ne.contracts.types import ApprovalTier, Principal
from ne.govern.config import ToolArgPolicy
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.run_runner import RunExecutionContext, RunExecutionResult
from ne.runtime.run_store import AgentRunStore
from ne.runtime.runner import _protected_tool_evidence_refs
from ne.runtime.source_results import (
    ProtectedSourceResultGuard,
    authenticated_request_scope,
    complete_run_direct_publication_observation,
    decode_protected_reference,
    direct_publication_scope,
    protected_tool_evidence_scope,
)
from ne.runtime.tools import ToolBinding
from ne.worker.run_execution import _source_free_envelope

from .conftest import AppBuilder, get_state, run
from .run_approval_park import (
    APPROVER,
    APPROVER_GROUP,
    OWNER,
    RecordingEnqueuer,
    admit_run,
    admit_sources,
    assert_park_left_production_rows,
    build_service,
    workflow_request,
)

#: The Tier-0 read that serves the run's ONE protected source reference.
#: ``_run_source_references`` refuses a Tier-2 park with an empty evidence
#: carrier, so the ladder must have a real governed read below the write — which
#: is also the production shape.
READ_TOOL = "read.demo.evidence"

#: The inert write-back's only argument. A recipient LABEL: the handler never
#: reads it and never sends anything to it.
WRITE_ARGS: dict[str, object] = {"to": "meridian-servicing-ops"}

#: The entire observable output of ``_demo_writeback_handler``.
INERT_RESULT = "external note queued (demo write-back — no egress)"


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
    """The agent loop's two legs: the park, then the approved continuation.

    PARK LEG. Read governed evidence, DELIVER it to the model through the app's
    own composed guard, announce the plan ordinal, then call the inert Tier-2
    write through the REAL governed gate — which raises ``RunApprovalWaiting``
    into the workflow's own park leg. The delivery is not decoration: it is what
    puts real rows in ``ne_agent_delivery_receipts`` for this attempt, and item
    2's defect is invisible without them.

    RESUME LEG. Copied from ``ne.worker.run_execution._execute_effect_continuation``
    — the production adapter — including its ``direct_publication_scope`` and its
    content-free ``_source_free_envelope`` terminal, which is imported here
    rather than re-implemented.
    """

    def __init__(self, guard: Any) -> None:
        self._guard = guard
        self.park_entries = 0
        self.continuation_entries = 0
        self.effect_result = ""
        self.delivered_refs: list[str] = []

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        principal = Principal(id=context.run.owner_principal_id, groups=())
        continuation = context.continuation
        if continuation is not None:
            self.continuation_entries += 1
            with direct_publication_scope(
                workflow_execution_id=context.run.run_id,
                publication_attempt_id=context.attempt_id,
            ):
                self.effect_result = await context.executor.execute(
                    continuation.action, continuation.arguments, principal=principal
                )
                complete_run_direct_publication_observation()
            notice = f"The approved action {continuation.action} was executed after human approval."
            output = notice.encode("utf-8")
            return RunExecutionResult(
                outcome="execution_complete",
                output_bytes=output,
                structured_envelope=_source_free_envelope(output),
            )

        self.park_entries += 1
        await context.executor.execute(READ_TOOL, {}, principal=principal)
        served = set(context.executor.served_protected_refs)
        assert served, "the governed read must have served a protected reference"
        # THE PRE-PARK PROTECTED DELIVERY. The loop hands the read's result to
        # the model before it escalates; ``resolve_for_model_send`` writes the
        # durable delivery receipt, its mandatory delivery audit receipt and the
        # observer entry. Without these rows the finalizer's receipt-set
        # comparison — item 2's defect — passes trivially.
        for encoded in served:
            reference = decode_protected_reference(encoded)
            assert isinstance(reference, SourceBoundToolResultRef)
            delivered = await self._guard.resolve_for_model_send(reference)
            assert delivered.authorized, (
                "the pre-park delivery must be a REAL authorized protected delivery"
            )
            self.delivered_refs.append(reference.reference_id)
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
            # RETURNS, the tier-2 human gate did not fire.
            await context.executor.execute(DEMO_WRITEBACK_ACTION, WRITE_ARGS, principal=principal)
        raise AssertionError("the inert tier-2 write executed without a human approval")


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


def _terminal_outcome(runtime_url: str, run_id: str) -> str | None:
    """The outcome word the run's terminal actually PERSISTED, or ``None``.

    Read from the durable lifecycle outbox, never from a return value: a run
    that strands writes no row here at all, which is the distinction this file
    exists to make.
    """

    from ne.runtime.run_store import ne_agent_run_lifecycle_outbox

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            value = conn.execute(
                sa.select(ne_agent_run_lifecycle_outbox.c.outcome).where(
                    ne_agent_run_lifecycle_outbox.c.run_id == run_id
                )
            ).scalar_one_or_none()
    finally:
        engine.dispose()
    return None if value is None else str(value)


def _continuation_status(runtime_url: str, run_id: str) -> str:
    from ne.runtime.run_checkpoint import ne_agent_run_continuation_checkpoints

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                        ne_agent_run_continuation_checkpoints.c.run_id == run_id
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _delivery_receipt_ids(runtime_url: str, run_id: str, attempt_id: str) -> tuple[str, ...]:
    from ne.runtime.run_store import ne_agent_delivery_receipts

    engine = sa.create_engine(runtime_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_delivery_receipts.c.delivery_receipt_id).where(
                    ne_agent_delivery_receipts.c.run_id == run_id,
                    ne_agent_delivery_receipts.c.receiving_attempt_id == attempt_id,
                )
            ).all()
    finally:
        engine.dispose()
    return tuple(str(row.delivery_receipt_id) for row in rows)


def _tool_execute_resources(audit_url: str) -> list[str]:
    from ne.audit.store import ne_audit_log

    engine = sa.create_engine(audit_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(ne_audit_log.c.resource).where(ne_audit_log.c.action == "tool.execute")
            ).all()
    finally:
        engine.dispose()
    return [str(row.resource) for row in rows]


def _make(make_app: AppBuilder, holder: dict[str, Any]) -> Any:
    return make_app(
        approver_roles=(APPROVER_GROUP,),
        protected_transport=True,
        demo_writeback_tool=True,
        extra_tool_bindings=(_read_binding(holder),),
        extra_tool_policies={READ_TOOL: ToolArgPolicy(args={})},
    )


def _resume_via_worker_step(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: Any,
    service: Any,
    engine: Any,
    resume_request: Any,
) -> Any:
    """Drive the resume through the REAL ``resume_run_approval_step``.

    The step's own logic — the decision-intent echo, the fenced status reads,
    the approved->resumed CAS, the replay branch, ``execute_resumed`` — runs
    verbatim. Only the two process-global composition lookups are redirected at
    this harness's own governed objects.

    The ``worker_platform`` redirect carries a governance-stub waiver; the
    module docstring's "THE ONE STUB" section is the argument, and the three
    ``isinstance`` checks below are that argument made executable — they are
    what a bare waiver comment could not give a reviewer.
    """

    import ne.worker.workflows as worker_workflows

    # THE WAIVER'S LOAD-BEARING CLAIM, CHECKED. 2026-08-28 was
    # ``state.source_guard = SimpleNamespace()`` — a chokepoint REPLACED by a
    # fake. These assert the opposite: what this view hands the worker step is
    # the app's own composed production objects, so every governance decision
    # the step makes is made by the real implementation. If a future edit ever
    # slips a double in here, this fails LOUD rather than passing green.
    assert isinstance(state.gate, DurableApprovalGate)
    assert isinstance(state.source_guard, ProtectedSourceResultGuard)
    assert isinstance(state.agent_run_store, AgentRunStore)

    platform = SimpleNamespace(
        gate=state.gate,
        source_guard=state.source_guard,
        agent_run_store=state.agent_run_store,
        runtime_engine=engine,
    )
    monkeypatch.setattr(  # governance-stub: supplies the ABSENT worker boot global
        # ``worker_platform()`` is the ne-worker process's composition-root
        # lookup, installed by ``ne.worker.main.set_worker_platform`` at boot.
        # This is the ne-api harness: that boot never ran, so the real lookup
        # raises "called before set_worker_platform()". The stub SUPPLIES the
        # root the missing boot would have supplied — it does not replace one.
        # Every field is the app's own composed object, asserted above.
        worker_workflows,
        "worker_platform",
        lambda: platform,
    )
    monkeypatch.setattr(worker_workflows, "_run_workflow_service", lambda: service)
    return run(
        worker_workflows.resume_run_approval_step(
            resume_request.approval_id,
            resume_request.action_hash,
            resume_request.owner_principal_id,
            resume_request.policy_hash,
            resume_request.model_dump_json(),
        )
    )


class _Arc:
    """Everything the park + decision + resume leaves behind."""

    def __init__(self) -> None:
        self.h: Any = None
        self.state: Any = None
        self.parked: Any = None
        self.adapter: _LadderAdapter | None = None
        self.approve_status = 0
        self.returned = ""


def _drive(make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch) -> _Arc:
    """Park at the REAL inert gate, approve IN-WINDOW, resume for real."""

    arc = _Arc()
    holder: dict[str, Any] = {}
    arc.h = h = _make(make_app, holder)
    arc.state = state = get_state(h.app)
    holder["state"] = state
    state.agent_run_enqueuer = RecordingEnqueuer()

    stamps = admit_sources(state, key_prefix="item2-inert", count=1)
    holder["stamp"] = stamps[0]

    # The owner's stored snapshot is comfortably INSIDE its horizon — this is the
    # decision that arrives in time, which is the half item 1 could not reach.
    arc.parked = parked = run(
        admit_run(
            state,
            action=DEMO_WRITEBACK_ACTION,
            arguments=WRITE_ARGS,
            owner_token_ttl_s=3600,
        )
    )

    arc.adapter = adapter = _LadderAdapter(state.source_guard)
    service, engine = build_service(state, h.runtime_url, adapter)
    try:
        with authenticated_request_scope(parked.owner_context):
            result = run(service.execute(workflow_request(parked)))
        assert result.outcome == "waiting_approval"
    finally:
        engine.dispose()

    checkpoints = state.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    join = run(
        checkpoints.find_join_for_attempt(run_id=parked.run_id, attempt_id=parked.attempt_id)
    )
    assert join is not None, "the real gate parked no approval join"
    # THE PARK IS THE REAL GATE'S, on the real action, at the real tier.
    assert join.action == DEMO_WRITEBACK_ACTION
    assert int(join.tier) == int(ApprovalTier.TIER_2)
    parked.approval_id = join.approval_id
    assert_park_left_production_rows(state, h.runtime_url, parked)

    # --- the decision, IN WINDOW, over the REAL HTTP approve route ------------
    # The approver is a DIFFERENT principal from the run owner, which is both the
    # honest shape and what the separation-of-duties enforcement requires.
    assert APPROVER != OWNER

    async def _approve() -> Any:
        async with h.client() as client:
            return await client.post(
                f"/api/v1/approvals/{parked.approval_id}/approve",
                json={},
                headers=h.bearer(APPROVER, groups=[APPROVER_GROUP]),
            )

    decided = run(_approve())
    arc.approve_status = decided.status_code
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    assert len(state.agent_run_enqueuer.requests) == 1

    service, engine = build_service(state, h.runtime_url, adapter)
    try:
        arc.returned = _resume_via_worker_step(
            monkeypatch,
            state=state,
            service=service,
            engine=engine,
            resume_request=state.agent_run_enqueuer.requests[0],
        )
    finally:
        engine.dispose()
    return arc


def test_the_park_persists_a_real_pre_park_delivery(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup invariant item 2 is invisible without.

    ``RunFinalizer._observe_sync`` compares the observer snapshot against the
    persisted ``ne_agent_delivery_receipts`` rows for this attempt. With ZERO
    rows the comparison is ``() == ()`` and the whole F2 seam is unexercised —
    which is exactly how the first cut of the resume tests passed over a broken
    build. This refuses to let that happen silently.
    """

    arc = _drive(make_app, monkeypatch)
    assert arc.adapter is not None
    persisted = _delivery_receipt_ids(arc.h.runtime_url, arc.parked.run_id, arc.parked.attempt_id)
    assert persisted, "the park left no protected delivery; the F2 comparison is vacuous"
    assert set(arc.adapter.delivered_refs), "the ladder delivered no protected reference"


def test_a_run_that_executes_the_inert_effect_terminalizes_on_the_success_path(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST_DEMO item 2, end to end, with zero outward effect anywhere on the path.

    A canonical Run reads governed evidence, delivers it, escalates to the INERT
    Tier-2 write and parks through ``_require_canonical_run_approval``; a
    DISTINCT named approver decides IN WINDOW over the real HTTP route; the real
    ``resume_run_approval_step`` executes the pinned effect; the run reaches a
    TERMINAL state on the success path.

    MUTATION-CHECK (the non-vacuity claim, measured): stub
    ``AgentRunStore.delivered_receipt_ids`` to ``()`` — F2's pre-fix build — and
    (1) and (2) go RED (``running`` / ``terminal_at`` NULL / no outbox row)
    while the approve route still answers 200, the inert handler still executed,
    and (3) is unchanged. (3) is asserted for F1's reason, not item 2's.
    """

    arc = _drive(make_app, monkeypatch)
    assert arc.adapter is not None
    h = arc.h

    # THE EFFECT HAPPENED ONCE, through the continuation — never by replaying
    # the loop through a model.
    assert (arc.adapter.continuation_entries, arc.adapter.park_entries) == (1, 1)
    assert arc.adapter.effect_result == INERT_RESULT

    # (1) The run is TERMINAL, on the SUCCESS side. This is the observable the
    # three stranded production runs never reached: they are still 'running'.
    row = _run_row(h.runtime_url, arc.parked.run_id)
    assert str(row.status) == "completed"
    assert row.terminal_at is not None

    # (2) A terminal was durably COMPOSED and published, and it says HOW: this
    # run executed the approved effect, it did not run its whole plan.
    persisted = _terminal_outcome(h.runtime_url, arc.parked.run_id)
    assert persisted == "approved_effect_complete"
    assert json.loads(arc.returned)["outcome"] == persisted, (
        "the returned outcome and the persisted terminal must be one fact, not two"
    )

    # (3) The continuation's one permitted execution is durably fenced. NOT an
    # item-2 observable — measured identical in all three mutation columns —
    # but F1's, and a resume that left it `resumed` is how one approval sent N
    # times.
    assert _continuation_status(h.runtime_url, arc.parked.run_id) == "consumed"

    # ZERO OUTWARD EFFECT. The action that executed is the REGISTERED binding
    # whose handler body is a single `return` of a constant: no recipients are
    # extracted, no egress URLs are extracted, no cost is estimated, and nothing
    # leaves the box. Asserted on the composed executor's own binding.
    binding = arc.state.executor._bindings[DEMO_WRITEBACK_ACTION]  # type: ignore[attr-defined]
    assert binding.handler.__module__ == "ne.api.demo_seams"
    assert binding.handler.__qualname__ == "_demo_writeback_handler"
    assert binding.extract_recipients is None
    assert binding.extract_egress_urls is None
    assert binding.estimate_cost_usd is None
    executed = _tool_execute_resources(h.audit_url)
    assert sorted(executed) == sorted([f"tool:{READ_TOOL}", f"tool:{DEMO_WRITEBACK_ACTION}"]), (
        "exactly the governed read and the inert write executed — nothing else"
    )
