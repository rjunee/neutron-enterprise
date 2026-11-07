# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""F1: the decision survives the attempt; the SUCCESSOR brings its own authority.

THE PROBLEM THIS CLOSES. A Tier-2/3 gate exists because a different human, on
their own schedule, decides. That schedule is hours to days. The run owner's
frozen authorization snapshot expires with the access token that admitted the
run — 900 s on this deployment — so before F1 an approval that took longer than
a login was unanswerable: the attempt died `authorization_expired` and the
approver's decision was thrown away with it. The window being policy-configured
(ADR-0030) set the REQUEST's deadline honestly and changed nothing about that,
because the two clocks are independent.

WHAT F1 CHANGES, AND WHAT IT DELIBERATELY DOES NOT. The expired attempt still
terminalizes, still executes nothing, still discloses nothing; the expired
snapshot is never revived, rebound, refreshed, or replaced by the approver's or
the service's identity (all RED). What changes is that the
DECISION — approver-authority evidence, made under that approver's own fresh
token — is durably recorded as a STANDING APPROVAL bound to
``(owner_principal_id, action_hash, policy_hash, protected_invocation_hash)``
with a policy-configured lifetime. The owner's next authenticated request
admits a SUCCESSOR that re-parks at the same gate under a verifier-authored
context carrying CURRENT groups; on an exact tuple match the gate consumes the
standing approval AT MOST ONCE and resumes immediately, with live stamp
re-checks and its own fresh policy evaluation.

WHY THIS HARNESS. These tests reuse ``test_agentic_ladder_parks_canonical_
approval``'s stack, and the reuse is the point: it is the ONE harness in the
repo that enters ``_require_canonical_run_approval`` for real. Every other
approval end-to-end test drives an adapter that calls ``create_wait`` itself
with a fabricated ``policy_hash="e"*64`` — and a successor whose ``policy_hash``
is fabricated could never be shown to match a predecessor's. Here the planner,
the receipt-gated executor, the governed executor, the PDP, the durable gate and
the checkpoint store are all real over real SQLite, so the binding tuple both
runs produce is computed, not asserted into existence.

Every test below carries a MUTATION-CHECK line naming a mutation and the red it
produces, and every one of those mutations was applied and observed.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as _SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from ne.audit.store import ne_audit_log
from ne.contracts.governance import ApprovalRequest
from ne.contracts.source_authority import (
    ProtectedSourceInvocationRef,
    SourceAuthorityDecision,
    SourceBoundApprovalResolution,
)
from ne.contracts.types import ApprovalTier, Principal
from ne.events import new_approval_event, new_audit_event
from ne.events.outbox import ne_outbox_events
from ne.runtime.approval_store import ne_approval_requests
from ne.runtime.errors import (
    ApprovalSourceWithheldFromApproverError,
    DurabilityUnavailableError,
    RunApprovalAuthorityRetryableError,
    RunApprovalContinuationExhaustedError,
    RuntimeAuditError,
)
from ne.runtime.middleware import _satisfy_from_standing_approval
from ne.runtime.run_audit_receipts import (
    RunAuditReceiptSink,
    RunAuditTarget,
)
from ne.runtime.run_checkpoint import (
    RunApprovalWaiting,
    StandingDecision,
    ne_agent_run_approval_joins,
    ne_agent_run_continuation_checkpoints,
    ne_agent_standing_approvals,
)
from ne.runtime.run_store import (
    RunAuditDecisionKind,
    RunState,
    StoredSourcePayload,
    ne_agent_runs,
)
from ne.runtime.run_workflow import drain_run_approval_audit_intent
from ne.runtime.source_results import (
    AuthorizationExpiredError,
    ProtectedExecutionError,
    ProtectedSourceResultGuard,
)
from ne.schema import ApprovalDecision, AuditOutcome

from .conftest import (
    ALLOWED_RECIPIENT,
    ScriptedGateway,
    text_completion,
    tool_call_completion,
)
from .test_agentic_ladder_parks_canonical_approval import (
    _LADDER,
    _OWNER,
    _AuthorizingResolver,
    _drive,
    _ladder_plan,
    _Stack,
    _structured_completion,
)

_APPROVER = "carol"
_GATED_ACTION = "write.external.email.send"
_QUOTING_ANSWER = (
    'The record is explicit. The source states: "The servicing note records a '
    'hardship credit posted on May ninth." That resolves the dispute.'
)

#: `tiers.rego`'s built-in tier-2 standing floor (7 days), which is what the
#: Rego-faithful PDP in `conftest.py` answers for `write.external.email.send`.
_TIER2_STANDING_SECONDS = 604800.0


class _ApprovalStoredResolver(_AuthorizingResolver):
    """The ladder resolver PLUS the stored-invocation seam the F1 path needs.

    ``check_decision_authority`` re-evaluates every stamp of the PARKED
    invocation for the CURRENT bound context. The ladder's resolver only
    implements ``for_model_send``, and a resolver without
    ``for_approval_stored`` yields the non-committal RETRYABLE_UNKNOWN — which
    is correct fail-closed behaviour but would leave the successor path
    untested. ``withhold`` flips this one seam to PERMANENT_WITHHELD so the
    "owner lost the entitlement at re-login" case can be exercised WITHOUT
    breaking the reads, which is exactly the shape that case has: the successor
    can still read, but its own authority no longer admits the parked
    invocation.
    """

    def __init__(self) -> None:
        super().__init__()
        self.withhold = False
        #: The NON-COMMITTAL arm. ``check_decision_authority`` answers it with
        #: ``RunApprovalAuthorityRetryableError`` ("still pending, retry
        #: later") — a TYPED class, not the bare ``RuntimeError`` it raised
        #: before this branch — and production
        #: reaches it on ordinary transient conditions — a missing head receipt,
        #: a missing artifact identity, an ACL snapshot that has not landed
        #: (``current_source_filter.py``). It is a THIRD outcome, not a
        #: withholding, and the gate must park on it rather than die.
        self.retryable = False
        self.stored_calls = 0

    async def for_approval_stored(
        self,
        stored: StoredSourcePayload,
        reference: ProtectedSourceInvocationRef,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        self.stored_calls += 1
        if self.withhold or self.retryable:
            return SourceBoundApprovalResolution(
                decision=(
                    SourceAuthorityDecision.RETRYABLE_UNKNOWN
                    if self.retryable
                    else SourceAuthorityDecision.PERMANENT_WITHHELD
                ),
                materialized_text=None,
                stamps=(),
                approval_id=approval_id,
                decision_attempt_id=decision_attempt_id,
            )
        return SourceBoundApprovalResolution(
            decision=SourceAuthorityDecision.AUTHORIZED,
            materialized_text=stored.plaintext.decode("utf-8"),
            stamps=stored.stamps,
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )


def _fresh_login() -> int:
    return int(time.time()) + 3600


def _short_login() -> int:
    return int(time.time()) + 300


def _compose(stack: _Stack, resolver: _ApprovalStoredResolver) -> ProtectedSourceResultGuard:
    """Wire the guard the way production composes it: the GATE owns one too.

    ``DurableApprovalGate(source_guard=...)`` is what lets the gate re-check a
    parked invocation's stamps. A gate without one cannot verify a standing
    approval and therefore may not honour one — the F1 path fails closed to a
    park, which is why this wiring is part of the test rather than incidental
    to it.
    """
    guard = ProtectedSourceResultGuard(resolver)  # type: ignore[arg-type]
    stack.gate._source_guard = guard  # type: ignore[attr-defined]
    return guard


async def _park_predecessor(stack: _Stack, resolver: _ApprovalStoredResolver) -> str:
    """Drive one run to a REAL park and return its approval id."""
    with pytest.raises(RunApprovalWaiting) as raised:
        await _drive(stack, _ladder_plan(reads=len(_LADDER)), _compose(stack, resolver))
    return str(raised.value.joined.approval_id)


async def _terminalize_past_the_horizon(
    stack: _Stack,
    approval_id: str,
    *,
    decision: StandingDecision | None,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Kill the attempt the way the horizon does, carrying (or not) a decision.

    The horizon CHECK itself lives in the decide route and is pinned there
    (``ne-api/tests/test_run_approval_resume_continuation.py``). What is under
    test here is the store's half: the attempt terminalizes and, if a human
    answered, that answer becomes durable standing evidence — in ONE
    transaction, so the two facts can never disagree.
    """
    run_id = run_id or stack.run.run_id
    attempt_id = attempt_id or stack.attempt_id
    checkpoints = stack.gate._run_approval_checkpoints  # type: ignore[attr-defined]
    # The mandatory ``run-execution`` state-transition receipt the real workflow
    # writes when it claims the lease. This harness drives the planner directly,
    # so it must supply the one receipt every terminal composition demands —
    # ``RunFinalizer`` refuses to compose ANY terminal without it, which is a
    # property of the finalizer, not of F1.
    await RunAuditReceiptSink(
        stack.sink,
        audit_engine=stack.audit_engine,
        runtime_engine=stack.runtime,
        run_store=stack.runs,
        target=RunAuditTarget(
            run_id=run_id,
            attempt_id=attempt_id,
            step_id="run-execution",
            decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
            event_ordinal=0,
        ),
        config_hash=stack.config_hash,
    ).append(
        new_audit_event(
            action="run.execution.started",
            resource=f"run:{run_id}",
            outcome=AuditOutcome.success,
            actor=_OWNER,
            detail="harness-supplied execution receipt",
            config_hash=stack.config_hash,
        )
    )
    await drain_run_approval_audit_intent(
        checkpoints=checkpoints,
        sink=stack.sink,
        audit_engine=stack.audit_engine,
        runtime_engine=stack.runtime,
        run_store=stack.runs,
        run_id=run_id,
        attempt_id=attempt_id,
        owner_principal_id=_OWNER,
        approval_id=approval_id,
    )
    now = await checkpoints.database_now()
    # The standing event is built HERE, off the MUTABLE ``ne_approval_requests``
    # row, because that is where the real caller builds it: ``ne-api``'s
    # ``_refuse_expired_authorization`` passes ``int(row.tier)`` and
    # ``f"tool:{row.action}"``, and the mint overwrites only ``approval_id``.
    # Hard-coding the un-drifted values here would hide exactly the seam the
    # drift test below has to observe.
    with stack.runtime.connect() as conn:
        request = conn.execute(
            sa.select(ne_approval_requests.c.tier, ne_approval_requests.c.action).where(
                ne_approval_requests.c.request_id == approval_id
            )
        ).one()
    terminal = await checkpoints.terminalize_authorization_expired(
        approval_id,
        decided_at=now,
        decision=decision,
        standing_event=(
            None
            if decision is None
            else new_approval_event(
                approval_id=approval_id,
                tier=int(request.tier),
                decision=ApprovalDecision.approved,
                subject_ref=f"tool:{request.action}",
                actor=decision.approver,
            )
        ),
    )
    assert terminal is True


async def _drive_successor(
    stack: _Stack, resolver: _ApprovalStoredResolver, *, body: str = "the evidenced summary"
) -> Any:
    """Admit a NEW run under a FRESH login and drive it through the same gate."""
    stack.owner_expires_at = _fresh_login()
    plan = _ladder_plan(reads=len(_LADDER), final=_structured_completion(_QUOTING_ANSWER))
    if body != "the evidenced summary":
        plan = _drifted_plan(body)
    return await _drive(stack, plan, _compose(stack, resolver))


def _drifted_plan(body: str) -> Any:
    """The same ladder, but the model authors DIFFERENT arguments for the send."""
    return ScriptedGateway(
        [
            *(
                tool_call_completion(
                    "read.docs.search",
                    {"q": f"hop-{hop}", "step_intent": f"Read ladder hop {hop}."},
                )
                for hop in range(len(_LADDER))
            ),
            tool_call_completion(
                "write.external.email.send",
                {
                    "to": ALLOWED_RECIPIENT,
                    "body": body,
                    "step_intent": "Send the evidenced summary.",
                },
            ),
            text_completion(json.dumps({"final_text": _QUOTING_ANSWER})),
        ]
    )


def _approval_event_governance(stack: _Stack) -> list[tuple[int, str]]:
    """``(tier, subject_ref)`` of every ``ApprovalEvent`` in the outbox."""
    with stack.runtime.connect() as conn:
        payloads = [
            json.loads(row) if isinstance(row, str) else row
            for row in conn.execute(
                sa.select(ne_outbox_events.c.payload)
                .where(ne_outbox_events.c.event_type == "approval")
                .order_by(ne_outbox_events.c.id)
            ).scalars()
        ]
    return [(int(payload["tier"]), str(payload["subject_ref"])) for payload in payloads]


def _standing_rows(stack: _Stack) -> list[Any]:
    with stack.runtime.connect() as conn:
        return list(conn.execute(sa.select(ne_agent_standing_approvals)).all())


def _checkpoint_statuses(stack: _Stack) -> list[str]:
    with stack.runtime.connect() as conn:
        return [
            str(value)
            for value in conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).order_by(
                    ne_agent_run_continuation_checkpoints.c.created_at
                )
            ).scalars()
        ]


def _approval_statuses(stack: _Stack) -> list[str]:
    with stack.runtime.connect() as conn:
        return [
            str(value)
            for value in conn.execute(
                sa.select(ne_approval_requests.c.status).order_by(
                    ne_approval_requests.c.requested_at
                )
            ).scalars()
        ]


# --- (1) the flagship: a park that outlives the token, and resumes -----------


def test_a_park_that_outlives_the_owner_token_resumes_under_a_successor(
    tmp_path: Path,
) -> None:
    """The whole point, end to end: hours are possible and nothing was weakened.

    The predecessor parks under a short-lived login and is terminalized
    ``authorization_expired`` carrying the approver's decision. It executes
    NOTHING and stays terminal. The owner then signs in again; the successor
    re-runs the same ladder, re-parks with the identical binding tuple, consumes
    the standing approval and sends — under ITS OWN verifier-authored context,
    with the stamps re-checked live and the PDP re-consulted from scratch.

    MUTATION-CHECK: make ``_satisfy_from_standing_approval`` return ``None``
    unconditionally (delete the consumption) and this goes red — the successor
    parks a second pending approval and ``sends`` stays empty, which is the
    pre-F1 world.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> tuple[str, Any]:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        # THE PREDECESSOR IS DEAD, and it never sent anything.
        assert stack.tools.sends == []
        with stack.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(ne_agent_runs.c.status).where(
                        ne_agent_runs.c.run_id == stack.run.run_id
                    )
                ).scalar_one()
                == RunState.FAILED.value
            )
        predecessor_run = stack.run.run_id
        result = await _drive_successor(stack, resolver)
        return predecessor_run, result

    try:
        predecessor_run, result = asyncio.run(scenario())

        # THE EFFECT HAPPENED, EXACTLY ONCE, in the successor.
        assert len(stack.tools.sends) == 1, stack.tools.sends
        assert stack.tools.sends[0]["to"] == ALLOWED_RECIPIENT
        assert result.steps  # the run continued past the send to its final answer

        # The standing approval is consumed and names both attempts.
        rows = _standing_rows(stack)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "consumed"
        assert row.approver == _APPROVER
        assert row.predecessor_run_id == predecessor_run
        assert row.successor_run_id == stack.run.run_id
        assert row.successor_run_id != row.predecessor_run_id
        assert row.consumed_at is not None
        # The lifetime is POLICY's, resolved beside the window, not a constant
        # this code picked: 7 days for tier 2 in `tiers.rego`.
        assert (row.lapses_at - row.decided_at).total_seconds() == pytest.approx(
            _TIER2_STANDING_SECONDS, rel=1e-6
        )

        # The predecessor stayed terminal; the successor carried the effect.
        assert sorted(_approval_statuses(stack)) == ["approved", "expired"]
        assert sorted(_checkpoint_statuses(stack)) == ["consumed", "expired"]

        # NOTHING WAS REVIVED. The predecessor's own join and checkpoint are
        # untouched by the successor: two runs, two attempts, two checkpoints.
        with stack.runtime.connect() as conn:
            joins = conn.execute(sa.select(ne_agent_run_approval_joins.c.run_id)).scalars().all()
        assert sorted(joins) == sorted([predecessor_run, stack.run.run_id])

        # The successor's decision is ATTRIBUTED, not blank: a reader can tell
        # a carried decision from a human sitting in front of this request.
        with stack.runtime.connect() as conn:
            reason = conn.execute(
                sa.select(ne_approval_requests.c.decided_reason).where(
                    ne_approval_requests.c.status == "approved"
                )
            ).scalar_one()
        assert reason == "standing_approval_consumed"

        # And the live stamp re-check really ran under the successor.
        assert resolver.stored_calls >= 1
    finally:
        stack.dispose()


# --- (2) at most once --------------------------------------------------------


def test_a_standing_approval_is_consumed_at_most_once(tmp_path: Path) -> None:
    """One human decision authorizes ONE execution, however many successors run.

    One-way consumption is defended in BOTH places that touch the row — the
    lookup will not return a spent one and the CAS will not update one — so the
    mutation has to relax both to be observable. That is the point of naming
    them together rather than picking whichever one happens to fire first.

    MUTATION-CHECK: relax both status filters that make consumption one-way (the
    ``status == "standing"`` predicate in ``_find_live_standing_approval_sync``
    and the one in ``_consume_standing_approval_sync``) and this goes red — the
    second successor sends too and the send count becomes 2.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        await _drive_successor(stack, resolver)
        # A SECOND successor for the same effect finds the row consumed.
        with pytest.raises(RunApprovalWaiting):
            await _drive_successor(stack, resolver)

    try:
        asyncio.run(scenario())
        assert len(stack.tools.sends) == 1, stack.tools.sends
        rows = _standing_rows(stack)
        assert [row.status for row in rows] == ["consumed"]
        # The second successor is PARKED for a fresh human decision — it did not
        # fail, and it did not execute. That is the degradation direction.
        assert _approval_statuses(stack).count("pending") == 1
    finally:
        stack.dispose()


# --- (3) the lifetime is real ------------------------------------------------


def test_a_lapsed_standing_approval_authorizes_nothing(tmp_path: Path) -> None:
    """Past its policy lifetime the carryover LAPSES and the successor parks.

    The lapse is RECORDED, not merely ignored: a row that reads ``standing``
    forever would be a claim the platform cannot back, so the successor's own
    lookup CASes it to ``approval_lapsed``.

    MUTATION-CHECK: delete BOTH ``now >= lapses_at`` branches (the one in
    ``_find_live_standing_approval_sync`` and the re-check in
    ``_consume_standing_approval_sync``) and this goes red — the successor sends
    on a decision whose policy lifetime ran out.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        # Age the carryover out. Editing the durable deadline is the honest way
        # to test a lifetime whose floor is seven days.
        with stack.runtime.begin() as conn:
            changed = conn.execute(
                ne_agent_standing_approvals.update().values(
                    lapses_at=conn.execute(
                        sa.select(ne_agent_standing_approvals.c.decided_at)
                    ).scalar_one()
                )
            )
            assert changed.rowcount == 1
        with pytest.raises(RunApprovalWaiting):
            await _drive_successor(stack, resolver)

    try:
        asyncio.run(scenario())
        assert stack.tools.sends == []
        assert [row.status for row in _standing_rows(stack)] == ["approval_lapsed"]
        assert _approval_statuses(stack).count("pending") == 1
    finally:
        stack.dispose()


# --- (4) drift degrades to a fresh human decision ----------------------------


def test_drifted_arguments_park_for_a_fresh_human_decision(tmp_path: Path) -> None:
    """A successor that re-produces a DIFFERENT effect gets no free pass.

    Model nondeterminism is the normal case, not the exotic one: a re-run that
    authors a different body is a different ``action_hash`` and a different
    ``protected_invocation_hash``, so it matches no standing approval and parks
    exactly as a first-time request would.

    MUTATION-CHECK: widen the lookup to ``owner_principal_id`` alone (drop the
    three hash predicates from ``_find_live_standing_approval_sync``) AND drop
    the ``standing_binding``/``claimed_binding`` comparison in
    ``_consume_standing_approval_sync`` — the tuple is checked in both places —
    and this goes red: the drifted effect is sent under a decision no human made
    about it.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        with pytest.raises(RunApprovalWaiting):
            await _drive_successor(stack, resolver, body="a different summary entirely")

    try:
        asyncio.run(scenario())
        assert stack.tools.sends == []
        assert [row.status for row in _standing_rows(stack)] == ["standing"]
        assert _approval_statuses(stack).count("pending") == 1
    finally:
        stack.dispose()


def test_a_successor_that_lost_source_authority_parks_instead_of_executing(
    tmp_path: Path,
) -> None:
    """The owner lost the entitlement at re-login: ask a human, never execute.

    The live stamp re-check runs BEFORE the consumption, so a withheld successor
    leaves the standing approval STANDING and the park pending. Consuming first
    and checking after would spend a human decision on an execution that then
    refused — the carryover would be gone and nobody would have been asked.

    MUTATION-CHECK: delete the ``check_decision_authority`` call in
    ``_satisfy_from_standing_approval`` — equivalently, move it after
    ``consume_standing_approval`` — and this goes red: the standing row ends
    ``consumed`` and the effect is SENT under authority the successor no longer
    holds.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        resolver.withhold = True
        with pytest.raises(RunApprovalWaiting):
            await _drive_successor(stack, resolver)

    try:
        asyncio.run(scenario())
        assert stack.tools.sends == []
        assert [row.status for row in _standing_rows(stack)] == ["standing"]
        assert _approval_statuses(stack).count("pending") == 1
    finally:
        stack.dispose()


# --- (5) nothing is minted where nothing was decided -------------------------


def test_an_unanswered_park_mints_no_standing_approval(tmp_path: Path) -> None:
    """No human answered, so there is no decision to carry. Successors park.

    MUTATION-CHECK: change ``_mint_standing_approval``'s final ``else: return
    None`` to mint from the pending row anyway and this goes red — a standing
    approval appears for a request nobody ever decided, and the successor sends.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(stack, approval_id, decision=None)
        with pytest.raises(RunApprovalWaiting):
            await _drive_successor(stack, resolver)

    try:
        asyncio.run(scenario())
        assert _standing_rows(stack) == []
        assert stack.tools.sends == []
    finally:
        stack.dispose()


def test_a_park_with_no_policy_lifetime_mints_no_standing_approval(tmp_path: Path) -> None:
    """No policy lifetime means MINT NOTHING — never "standing forever".

    A join whose park recorded no ``standing_lifetime_s`` (a legacy row, or a
    bundle that named none) behaves exactly as it did before F1: the decision
    dies with its attempt.

    MUTATION-CHECK: make ``_standing_lifetime`` answer a default (say 7 days)
    when the column is NULL and this goes red — a standing approval is minted
    from a park that never carried a policy answer.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        with stack.runtime.begin() as conn:
            # The exact shape of a park written before this column existed.
            conn.execute(
                ne_agent_run_approval_joins.update()
                .where(ne_agent_run_approval_joins.c.approval_id == approval_id)
                .values(standing_lifetime_s=None)
            )
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )

    try:
        asyncio.run(scenario())
        assert _standing_rows(stack) == []
    finally:
        stack.dispose()


def test_the_park_records_the_policy_resolved_standing_lifetime(tmp_path: Path) -> None:
    """The lifetime on the join is POLICY's answer, carried from the decision.

    MUTATION-CHECK: stop passing ``standing_lifetime_s=current_standing_
    lifetime_s()`` at the ``create_wait`` call in ``_require_canonical_run_
    approval`` and this goes red — the column is NULL and no park can ever mint
    a carryover.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    try:
        approval_id = asyncio.run(_park_predecessor(stack, resolver))
        with stack.runtime.connect() as conn:
            lifetime = conn.execute(
                sa.select(ne_agent_run_approval_joins.c.standing_lifetime_s).where(
                    ne_agent_run_approval_joins.c.approval_id == approval_id
                )
            ).scalar_one()
        assert lifetime == pytest.approx(_TIER2_STANDING_SECONDS)
    finally:
        stack.dispose()


# --- (6) the audit linkage ---------------------------------------------------


def test_the_audit_receipt_links_successor_predecessor_and_the_decision(
    tmp_path: Path,
) -> None:
    """One record names all three, so the chain is readable without a join query.

    MUTATION-CHECK: delete the ``approval.standing.consumed`` ``gate._audit``
    call in ``_satisfy_from_standing_approval`` and this goes red — the effect
    still executes but nothing in the audit chain says WHY it needed no human.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> tuple[str, str]:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        predecessor_run = stack.run.run_id
        await _drive_successor(stack, resolver)
        return approval_id, predecessor_run

    try:
        predecessor_approval, predecessor_run = asyncio.run(scenario())
        with stack.audit_engine.connect() as conn:
            payloads = [
                json.loads(row) if isinstance(row, str) else row
                for row in conn.execute(
                    sa.select(ne_audit_log.c.payload).where(
                        ne_audit_log.c.action == "approval.standing.consumed"
                    )
                ).scalars()
            ]
        assert len(payloads) == 1, payloads
        provenance = payloads[0]["provenance"]
        assert provenance["predecessor_approval_id"] == predecessor_approval
        assert provenance["predecessor_run_id"] == predecessor_run
        assert provenance["successor_run_id"] == stack.run.run_id
        assert provenance["approver"] == _APPROVER
        assert provenance["standing_id"] == _standing_rows(stack)[0].standing_id
        # C12: ids, hashes and statuses only — never an argument value.
        assert ALLOWED_RECIPIENT not in json.dumps(payloads[0])
    finally:
        stack.dispose()


# --- (7) store-level drift the end-to-end shape cannot reach -----------------


def test_a_standing_approval_for_a_different_tool_or_tier_cannot_be_consumed(
    tmp_path: Path,
) -> None:
    """The tool name and the tier are NOT in the binding tuple, so they are checked.

    ``action_hash`` digests the ARGUMENTS and ``policy_hash`` digests the BUNDLE
    VERSION. A standing approval recorded for a DIFFERENT tool called with
    byte-identical arguments — or for the same tool at a different tier, which
    is what a provisional-context escalation produces — matches all three hashes
    exactly. Equality on the name and the tier is the only thing that stops a
    Tier-2 decision from satisfying a Tier-3 gate.

    The drift is applied to the STANDING ROW rather than to the caller's
    arguments on purpose: a mismatched ARGUMENT is already refused by the
    successor-join comparison, so mutating that would prove nothing about this
    guard. The exact consumption at the end is the positive control — without
    it, three ``None``s would prove only that this call never works.

    MUTATION-CHECK: delete the ``int(row.tier) != tier or ... action`` guard in
    ``_consume_standing_approval_sync`` and this goes red — a decision recorded
    for another tool, and one recorded at another tier, both consume.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> tuple[Any, Any, Any, Any]:
        # Park BOTH runs before any standing approval exists, so the second park
        # is an ordinary pending wait this test can aim mismatched consumptions
        # at without the gate having already satisfied it.
        predecessor_id = await _park_predecessor(stack, resolver)
        predecessor_run, predecessor_attempt = stack.run.run_id, stack.attempt_id
        stack.owner_expires_at = _fresh_login()
        successor_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack,
            predecessor_id,
            decision=StandingDecision(approver=_APPROVER),
            run_id=predecessor_run,
            attempt_id=predecessor_attempt,
        )
        checkpoints = stack.gate._run_approval_checkpoints  # type: ignore[attr-defined]
        with stack.runtime.connect() as conn:
            join = conn.execute(
                sa.select(ne_agent_run_approval_joins).where(
                    ne_agent_run_approval_joins.c.approval_id == successor_id
                )
            ).one()
        now = await checkpoints.database_now()

        async def _consume(**overrides: Any) -> Any:
            arguments: dict[str, Any] = {
                "standing_id": _standing_rows(stack)[0].standing_id,
                "owner_principal_id": str(join.principal_id),
                "action": str(join.action),
                "tier": int(join.tier),
                "action_hash": str(join.action_hash),
                "policy_hash": str(join.policy_hash),
                "protected_invocation_hash": str(join.protected_invocation_hash),
                "now": now,
                "event": new_approval_event(
                    approval_id=successor_id,
                    tier=2,
                    decision=ApprovalDecision.approved,
                    subject_ref="tool:write.external.email.send",
                    actor=_APPROVER,
                ),
            }
            arguments.update(overrides)
            return await checkpoints.consume_standing_approval(successor_id, **arguments)

        def _tamper(**values: Any) -> None:
            with stack.runtime.begin() as conn:
                conn.execute(ne_agent_standing_approvals.update().values(**values))

        wrong_owner = await _consume(owner_principal_id="mallory")
        _tamper(action="write.external.demo.notify")
        other_tool = await _consume()
        _tamper(action="write.external.email.send", tier=3)
        other_tier = await _consume()
        _tamper(tier=2)
        exact = await _consume()
        return wrong_owner, other_tool, other_tier, exact

    try:
        wrong_owner, other_tool, other_tier, exact = asyncio.run(scenario())
        assert wrong_owner is None
        assert other_tool is None
        assert other_tier is None
        # POSITIVE CONTROL: the exact binding DOES consume, so the three Nones
        # above are the guards refusing, not the call being inert.
        assert exact is not None
        assert exact.approver == _APPROVER
        assert [row.status for row in _standing_rows(stack)] == ["consumed"]
        # The three refusals spent nothing and decided nothing: the effect never
        # ran, because nothing resumed the successor's checkpoint.
        assert stack.tools.sends == []
    finally:
        stack.dispose()


# --- (8) the two shapes F1's own continuation opened -------------------------
#
# Both exist because F1 changed what happens AFTER a park. An UNsatisfied park
# still raises ``RunApprovalWaiting`` and ends the agent loop; a SATISFIED one
# returns and the loop runs on, with no human in it — and nothing in the tree
# had ever driven a run past a park and into a second approval-gated call.


def _two_gated_sends_plan() -> ScriptedGateway:
    """The ladder, then TWO Tier-2 sends — the shape a satisfied park unlocks.

    ``max_parked_writes`` on the shipped demo agent is 3:
    ``agent_execution_caps.enforced_agentic_caps`` takes
    ``min(max_tool_calls, <non-read. grants>)``, and
    ``agt_01k3customersupport000000000`` has ``max_tool_calls = 17`` against the
    three non-``read.`` grants ``draft.email.create``,
    ``write.external.email.send`` and ``write.internal.crm.note``
    (``deploy/agents/registry.toml``). So a model that plans two sends is within
    its own cap and this plan is not a contrived one.
    """

    return ScriptedGateway(
        [
            *(
                tool_call_completion(
                    "read.docs.search",
                    {"q": f"hop-{hop}", "step_intent": f"Read ladder hop {hop}."},
                )
                for hop in range(len(_LADDER))
            ),
            *(
                tool_call_completion(
                    "write.external.email.send",
                    {
                        "to": ALLOWED_RECIPIENT,
                        "body": body,
                        "step_intent": "Send the evidenced summary.",
                    },
                )
                for body in ("the evidenced summary", "a second evidenced summary")
            ),
            text_completion(json.dumps({"final_text": _QUOTING_ANSWER})),
        ]
    )


def test_a_second_gated_call_after_a_standing_consume_is_refused_not_stranded(
    tmp_path: Path,
) -> None:
    """One continuation per Run — said as governance, never as an IntegrityError.

    A satisfied park returns and the loop CONTINUES, so a second
    approval-gated tool call in the same run now reaches ``create_wait`` — and
    both durable tables are UNIQUE on ``run_id``. That uniqueness is
    load-bearing: ``RunWorkflowStateStore.resumed_approval_id`` calls two
    resumed continuations for one attempt "structurally unreachable" and raises
    if it sees them, ``find_join_for_attempt`` (the drain that gives every park
    its MANDATORY audit receipt) is a ``one_or_none()``, and
    ``RunFinalizer._observed_approvals`` demands exactly one receipt per join.
    So the limit stays and is STATED, before anything durable is written.

    What that buys is the whole point: the first effect stands, the second is a
    BLOCKED tool result the model is told about, and the run reaches its own
    terminal. Nothing is stranded — which is the defect
    ``terminalize_authorization_expired`` was written to close, and it must not
    come back through the fix for it.

    MUTATION-CHECK: delete the ``if held is not None: raise
    RunApprovalContinuationExhaustedError`` guard in ``_create_wait_sync`` and
    this goes red — ``sqlalchemy.exc.IntegrityError: UNIQUE constraint failed:
    ne_agent_run_continuation_checkpoints.run_id`` escapes
    ``run_model_planned_agent``, with the first send already delivered.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> Any:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        stack.owner_expires_at = _fresh_login()
        return await _drive(
            stack, _two_gated_sends_plan(), _compose(stack, resolver), max_parked_writes=3
        )

    try:
        result = asyncio.run(scenario())

        # THE RUN FINISHED. No exception escaped, and the loop went on to author
        # its final answer after the refusal.
        assert result.outcome == "complete"
        # The first send stands; the second was refused BY NAME, not by a crash.
        assert [step.outcome for step in result.steps][-2:] == [
            "submitted",
            f"refused:{RunApprovalContinuationExhaustedError.__name__}",
        ]
        assert len(stack.tools.sends) == 1, stack.tools.sends
        assert stack.tools.sends[0]["body"] == "the evidenced summary"

        # NOTHING DURABLE WAS WRITTEN FOR THE REFUSED CALL, so every seam that
        # reads (run_id, attempt_id) as an at-most-one address still holds: one
        # checkpoint and one join per run, and the standing approval spent once.
        with stack.runtime.connect() as conn:
            checkpoints = sorted(
                conn.execute(sa.select(ne_agent_run_continuation_checkpoints.c.run_id)).scalars()
            )
            joins = sorted(conn.execute(sa.select(ne_agent_run_approval_joins.c.run_id)).scalars())
        assert checkpoints == joins
        assert len(joins) == 2  # one predecessor, one successor
        assert [row.status for row in _standing_rows(stack)] == ["consumed"]
        assert sorted(_checkpoint_statuses(stack)) == ["consumed", "expired"]
    finally:
        stack.dispose()


def test_a_retryable_authority_answer_parks_instead_of_killing_the_run(
    tmp_path: Path,
) -> None:
    """RETRYABLE is not WITHHELD and it is not AUTHORIZED — so it parks.

    ``check_decision_authority`` has three outcomes and only one of them is a
    withholding: it returns on authorized, raises
    ``ApprovalSourceWithheldFromApproverError`` on permanent withholding, and
    raises ``RunApprovalAuthorityRetryableError`` ("still pending, retry later")
    on the retryable arm — which production reaches on ordinary transient
    conditions (a missing head receipt, a missing artifact identity, an ACL
    snapshot that has not landed). That arm was a BARE ``RuntimeError`` until
    this branch typed it; catching only the withholding let it escape and
    terminate the attempt, with the effect unsent and the carryover
    still standing: the run died for a condition that would have cleared on its
    own, and no human was ever asked.

    The contract this restores is the one already written down in
    ``_satisfy_from_standing_approval``'s docstring and in
    ``approval-horizon.md``: every ``None`` means park for a fresh human
    decision, and no branch anywhere answers "execute anyway".

    MUTATION-CHECK: narrow the handler back to ``except
    ApprovalSourceWithheldFromApproverError`` and this goes red —
    ``RunApprovalAuthorityRetryableError: Run approval authority is retryable
    and remains pending`` escapes instead of ``RunApprovalWaiting``.
    """

    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )
        resolver.retryable = True
        with pytest.raises(RunApprovalWaiting):
            await _drive_successor(stack, resolver)

    try:
        asyncio.run(scenario())
        # The successor PARKED: nothing executed, the carryover is untouched and
        # still spendable, and there is a pending request for a human to answer.
        assert stack.tools.sends == []
        assert [row.status for row in _standing_rows(stack)] == ["standing"]
        assert _approval_statuses(stack).count("pending") == 1
        # The re-check really ran — this is the retryable ARM, not a resolver
        # that was never consulted.
        assert resolver.stored_calls >= 1
    finally:
        stack.dispose()


class _AuthorityRaisingCheckpoints:
    """A checkpoint coordinator whose authority floor raises one chosen error.

    Small on purpose. The seam under test — ``_satisfy_from_standing_approval``'s
    handler around ``check_decision_authority`` — is decided entirely by the
    CLASS that comes out of that call, so driving the whole ladder to reach it
    would prove nothing the stub does not, and would not let the fail-closed
    classes be raised at that exact point at all.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.authority_calls = 0
        self.consume_calls = 0
        self.resume_calls = 0

    async def find_live_standing_approval(self, **_: Any) -> Any:
        return _SimpleNamespace(
            standing_id="standing-1",
            tier=2,
            action=_GATED_ACTION,
            approver=_APPROVER,
        )

    async def check_decision_authority(self, *_: Any, **__: Any) -> None:
        self.authority_calls += 1
        raise self._error

    async def consume_standing_approval(self, *_: Any, **__: Any) -> Any:
        self.consume_calls += 1
        raise AssertionError("a carryover was consumed on an unanswered authority check")

    async def resume(self, *_: Any, **__: Any) -> None:
        self.resume_calls += 1
        raise AssertionError("a park was resumed on an unanswered authority check")


async def _call_authority_seam(checkpoints: _AuthorityRaisingCheckpoints) -> Any:
    """Call ``_satisfy_from_standing_approval`` against one stubbed authority floor."""
    return await _satisfy_from_standing_approval(
        gate=_SimpleNamespace(_source_guard=object()),  # type: ignore[arg-type]
        checkpoints=checkpoints,
        joined=_SimpleNamespace(approval_id="approval-1", protected_invocation_hash="p" * 64),
        request=ApprovalRequest(
            tier=ApprovalTier.TIER_2,
            action=_GATED_ACTION,
            principal=Principal(id=_OWNER),
        ),
        tier=2,
        args_hash="a" * 64,
        policy_hash="b" * 64,
        now=datetime.now(UTC),
    )


def test_the_authority_seam_parks_on_its_three_non_authorized_answers() -> None:
    """The handler catches every answer that means "not authorized, not a failure".

    There are exactly three and the clause names all three:
    ``ApprovalSourceWithheldFromApproverError`` (the successor's own authority no
    longer admits the parked invocation), ``RunApprovalAuthorityRetryableError``
    (the arm ``check_decision_authority`` documents as "still pending, retry
    later"), and ``LookupError`` (the join or the stored invocation is
    unavailable, or its plaintext hash moved). None of them is evidence that the
    carryover applies, so none may consume it — and none may kill the run
    either, because a human can still answer the park.

    MUTATION-CHECK: drop any one class from the clause and that iteration goes
    red, the exception escaping instead of parking.
    """
    for parking in (
        ApprovalSourceWithheldFromApproverError("withheld from this approver"),
        RunApprovalAuthorityRetryableError("retryable and remains pending"),
        LookupError("Run approval join unavailable"),
    ):
        checkpoints = _AuthorityRaisingCheckpoints(parking)
        assert asyncio.run(_call_authority_seam(checkpoints)) is None, type(parking).__name__
        assert checkpoints.authority_calls == 1
        assert checkpoints.consume_calls == 0
        assert checkpoints.resume_calls == 0


def test_an_expired_owner_horizon_at_the_authority_seam_is_not_swallowed() -> None:
    """``AuthorizationExpiredError`` must PROPAGATE from the successor's park.

    THE SCENARIO IS THIS BRANCH'S OWN. The successor re-parks under a fresh login
    and then re-checks the parked invocation's stamps;
    ``resolve_stored_for_approval``'s FIRST statement is ``self.authorization()``,
    which raises :class:`AuthorizationExpiredError` the moment the bound snapshot
    is at or past its ``exp``. An agent loop that crosses the owner's horizon
    between ``create_wait`` and this check reaches exactly that.

    The specification is unambiguous about what must then happen: the platform
    executes and discloses nothing, makes the parked approval non-actionable and
    terminally fails the attempt with ``authorization_expired``. PROPAGATING is
    what produces that terminal — the raise crosses ``RunExecutionAdapter`` into
    ``RunWorkflowService.execute``, whose ``except AuthorizationExpiredError``
    (``run_workflow.py``'s only one — ``RunWorkflowStateStore`` never names the
    class) terminalizes. Swallowed into a ``None`` it becomes a silent PARK
    instead:
    the run sits ``WAITING_APPROVAL`` under an expired owner snapshot, a human is
    asked to decide an effect that terminalizes anyway, and the audit terminal
    the SPEC mandates is never composed. A safe-looking outcome is still the
    wrong one.

    The three siblings are pinned as a CLASS boundary, not as measured
    reachability: ``ProtectedExecutionError`` (the family
    ``AuthorizationExpiredError`` belongs to, whose members reach this chain the
    same way through ``self.authorization()``), ``RuntimeAuditError`` (the one
    class this package's module docstring singles out as aborting rather than
    being swallowed, and which ``runner.py`` deliberately does not catch either),
    and ``DurabilityUnavailableError``. Nothing is known to raise the last two on
    this chain TODAY — which is the point: a clause that names its classes cannot
    start swallowing them the day something does.

    MUTATION-CHECK: widen the handler back to ``except (RuntimeError,
    LookupError)`` and this goes red — every class below is an ``NeRuntimeError``
    and therefore a ``RuntimeError``, so all four return ``None`` instead.
    """
    for propagating in (
        AuthorizationExpiredError("the bound authorization snapshot is past its expiry"),
        ProtectedExecutionError("protected execution refused"),
        RuntimeAuditError("the audit append failed"),
        DurabilityUnavailableError("durability is not enabled"),
    ):
        checkpoints = _AuthorityRaisingCheckpoints(propagating)
        with pytest.raises(type(propagating)):
            asyncio.run(_call_authority_seam(checkpoints))
        # Nothing was consumed on the way out: the carryover is still spendable
        # by the owner's next authenticated retry, which is the only thing that
        # may run.
        assert checkpoints.consume_calls == 0
        assert checkpoints.resume_calls == 0


def test_the_standing_row_names_the_parked_tool_not_a_drifted_request_row(tmp_path: Path) -> None:
    """The mint reads governance metadata from the INSERT-ONLY join.

    ``_mint_standing_approval`` writes ``action`` and ``tier`` into the durable
    standing approval row, and ``tier`` also selects the ceremony it validates
    (Tier-3 needs two distinct named approvers). Taking either from the MUTABLE
    ``ne_approval_requests`` row would let a drifted row mint a carryover naming
    a tool or a tier no human ever approved. Every decide path already refuses
    that drift through ``_require_parked_governance``; this seam cannot raise —
    it runs inside the transaction that must write the ``authorization_expired``
    terminal — so it reads the park-time record, which is insert-only and has
    nothing to drift.

    ⚠ THE STANDING AUDIT EVENT IS NOT COVERED BY THAT, and the second assertion
    below pins it rather than leaving the boundary to be guessed. The event is
    built by the CALLER off the same mutable row (``ne-api``'s
    ``_refuse_expired_authorization``: ``int(row.tier)`` and
    ``f"tool:{row.action}"``) and the mint overwrites only its ``approval_id``,
    so here the durable row says ``(write.external.email.send, 2)`` while the
    event says ``tool:finance.wire`` at tier 3. Over HTTP the drifted event is
    unreachable: both decide routes call ``_parked_governance_matches(joined,
    row)`` and answer 409 BEFORE ``_refuse_expired_authorization`` builds
    anything. This store-level harness bypasses that route check, which is
    exactly what makes the split observable.

    The drift here is deliberately BOTH fields at once and in the dangerous
    direction: a tier-2 park re-labelled ``finance.wire`` at Tier 3.

    MUTATION-CHECK: read ``tier``/``action`` from ``approval_row`` again and this
    goes red with ``[]`` — NO standing approval minted at all, because the
    drifted Tier 3 fails the two-distinct-approvers ceremony before the drifted
    ``finance.wire`` can be written. The action mislabel is MASKED behind that,
    which is exactly why both fields are read from the join and not only the one
    a test can see.
    """
    stack = _Stack(tmp_path, owner_expires_at=_short_login())
    resolver = _ApprovalStoredResolver()

    async def scenario() -> None:
        approval_id = await _park_predecessor(stack, resolver)
        with stack.runtime.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == approval_id)
                .values(action="finance.wire", tier=3)
            )
        await _terminalize_past_the_horizon(
            stack, approval_id, decision=StandingDecision(approver=_APPROVER)
        )

    try:
        asyncio.run(scenario())
        assert [(str(row.action), int(row.tier)) for row in _standing_rows(stack)] == [
            (_GATED_ACTION, 2)
        ]
        # And the boundary: the caller-built event carried the DRIFTED values
        # through unchanged, so this fix closed the durable row and moved the
        # audit event not at all.
        assert _approval_event_governance(stack) == [
            # The park's own event, emitted before the drift landed.
            (2, f"tool:{_GATED_ACTION}"),
            # The mint's, built by the caller off the drifted row and passed
            # through with only its ``approval_id`` overwritten.
            (3, "tool:finance.wire"),
        ]
    finally:
        stack.dispose()
