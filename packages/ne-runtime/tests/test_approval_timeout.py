# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""§4.2 timeout/escalation/expiry semantics driven by a deterministic FakeClock."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalStatus
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.correlation import action_scope
from ne.runtime.errors import ApprovalAlreadyDecidedError

from .conftest import FakeClock, RecordingAuditSink, approval_request, approval_settings


class RecordingMetrics:
    """A record_latency double (the real OTel histogram lives in test_approval_metrics)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, float]] = []

    def record_latency(self, *, tier: int, decision: str, seconds: float, config_hash: str) -> None:
        self.calls.append((tier, decision, seconds))


def _outbox_decisions(store: Any, decision: str) -> int:
    with store.engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT payload FROM ne_outbox_events WHERE event_type = 'approval'")
        ).all()
    return sum(1 for r in rows if f'"decision":"{decision}"' in r.payload.replace(" ", ""))


def test_pending_past_sla_escalates_once_and_notifies(approval_store: Any) -> None:
    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(tier2_sla_s=100.0, expiry_s=1000.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(200.0)  # past SLA, before hard expiry
        task = asyncio.create_task(gate.await_outcome(rid))
        # The await loop escalates exactly once; the request stays approvable.
        while (await approval_store.get(rid)).escalated_at is None:
            await asyncio.sleep(0)
        outcome = await gate.approve(rid, approver="carol")
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED
        final = await asyncio.wait_for(task, timeout=10)
        assert final.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())
    assert sink.actions().count("approval.escalated") == 1
    assert _outbox_decisions(approval_store, "escalated") == 1


def test_pending_past_hard_expiry_returns_expired(approval_store: Any) -> None:
    clock = FakeClock()
    sink = RecordingAuditSink()
    metrics = RecordingMetrics()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(tier2_sla_s=100.0, expiry_s=1000.0, poll_interval_s=0.0),
        metrics=metrics,  # type: ignore[arg-type]
        clock=clock,
    )

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(2000.0)  # past hard expiry
        final = await asyncio.wait_for(gate.await_outcome(rid), timeout=10)
        assert final.status is ApprovalStatus.EXPIRED
        row = await approval_store.get(rid)
        assert row is not None and row.status == "expired"

    asyncio.run(scenario())
    expired = [e for e in sink.events if getattr(e, "action", "") == "approval.expired"]
    assert len(expired) == 1
    assert expired[0].outcome == "denied"
    with approval_store.engine.connect() as conn:
        evidence = conn.execute(
            sa.text("SELECT clean FROM ne_approval_evidence WHERE outcome = 'expired'")
        ).all()
    assert len(evidence) == 1
    assert [(t, d) for (t, d, _s) in metrics.calls] == [(2, "expired")]


def test_expired_request_cannot_be_approved_requires_rerequest(approval_store: Any) -> None:
    clock = FakeClock()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=RecordingAuditSink(),
        settings=approval_settings(expiry_s=1000.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(2000.0)  # past hard expiry
        with pytest.raises(ApprovalAlreadyDecidedError):
            await gate.approve(rid, approver="carol")
        row = await approval_store.get(rid)
        assert row is not None and row.status == "expired"

    asyncio.run(scenario())


def test_list_pending_expires_orphan_at_exact_boundary_without_waiter(
    approval_store: Any,
) -> None:
    """The queue read itself retires a due orphan; no await workflow is alive.

    A second, later request is the negative control: the read must remove only
    the row whose persisted deadline has arrived, not empty the queue wholesale.
    Re-reading proves the terminal evidence is winners-only.
    """
    clock = FakeClock()
    sink = RecordingAuditSink()
    metrics = RecordingMetrics()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(expiry_s=1000.0, poll_interval_s=0.0),
        metrics=metrics,  # type: ignore[arg-type]
        clock=clock,
    )

    async def scenario() -> tuple[str, str]:
        orphan = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(500.0)
        still_live = await gate.submit(approval_request("write.external.crm.note", 2))
        clock.advance(500.0)  # orphan is EXACTLY due; still_live has 500 s left

        first = await gate.list_pending()
        second = await gate.list_pending()
        assert [row.request_id for row in first] == [still_live]
        assert [row.request_id for row in second] == [still_live]
        return orphan, still_live

    orphan, still_live = asyncio.run(scenario())
    assert asyncio.run(approval_store.get(orphan)).status == "expired"
    assert asyncio.run(approval_store.get(still_live)).status == "pending"
    assert sink.actions().count("approval.expired") == 1
    assert _outbox_decisions(approval_store, "expired") == 1
    assert [(tier, decision) for tier, decision, _seconds in metrics.calls] == [(2, "expired")]


@pytest.mark.parametrize("operation", ["payload", "reject"])
def test_expired_row_is_terminal_on_payload_read_and_reject(
    approval_store: Any, operation: str
) -> None:
    """Every non-list server surface sees the same terminal expiry truth."""
    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(expiry_s=1000.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> str:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(1000.0)
        with pytest.raises(ApprovalAlreadyDecidedError, match="re-request is required"):
            if operation == "payload":
                await gate.read_payload(rid, approver="carol")
            else:
                await gate.reject(rid, approver="carol", reason="too late")
        return rid

    rid = asyncio.run(scenario())
    assert asyncio.run(approval_store.get(rid)).status == "expired"
    assert sink.actions().count("approval.expired") == 1
    assert sink.actions().count("approval.payload_read") == 0
    assert sink.actions().count("approval.rejected") == 0


def test_scoped_resubmit_expires_stale_claim_and_creates_fresh_request(
    approval_store: Any,
) -> None:
    """Expired scoped rows cannot absorb the re-request they require."""
    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(expiry_s=1000.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-expiry-retry"):
            stale = await gate.submit(approval_request("write.external.email.send", 2))
            clock.advance(1000.0)
            fresh = await gate.submit(approval_request("write.external.email.send", 2))
        return stale, fresh

    stale, fresh = asyncio.run(scenario())
    assert fresh != stale
    assert asyncio.run(approval_store.get(stale)).status == "expired"
    assert asyncio.run(approval_store.get(fresh)).status == "pending"
    assert sink.actions().count("approval.expired") == 1
    assert sink.actions().count("approval.reattached") == 0


def test_escalated_then_unactioned_expires(approval_store: Any) -> None:
    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(tier2_sla_s=100.0, expiry_s=1000.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(200.0)  # past SLA
        task = asyncio.create_task(gate.await_outcome(rid))
        while (await approval_store.get(rid)).escalated_at is None:
            await asyncio.sleep(0)
        clock.advance(2000.0)  # now past hard expiry
        final = await asyncio.wait_for(task, timeout=10)
        assert final.status is ApprovalStatus.EXPIRED

    asyncio.run(scenario())
    assert sink.actions().count("approval.escalated") == 1
    assert sink.actions().count("approval.expired") == 1


# --- the policy window must never delete §4.2 escalation ---------------------
#
# `await_outcome` checks hard expiry BEFORE the SLA, so `sla_deadline >=
# expires_at` silently deletes escalate-and-notify: the first eligible tick
# returns EXPIRED and `_escalate` can never run. The default tier-2 policy
# window (`tiers.rego` floor: 14400 s) is EXACTLY `ApprovalSettings.tier2_sla_s`
# (14400 s), so honouring the window without deriving the SLA down removed
# escalation for EVERY default tier-2 approval. RED-proved on 1c7ae211 (the
# window fix before `resolve_approval_deadlines_s`): both tests below fail
# there — sla_deadline == expires_at, and the awaited row expires unescalated.


def test_default_tier2_policy_window_still_escalates_and_notifies_before_expiring(
    approval_store: Any,
) -> None:
    """Production defaults: window == tier2_sla_s == 14400 s. Escalation survives."""
    from ne.runtime.approvals import _as_utc
    from ne.runtime.correlation import approval_window_scope

    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        # PRODUCTION values, not the test-friendly conftest ones: this is the
        # exact collision (sla == the tier-2 policy floor the scope binds).
        settings=approval_settings(tier2_sla_s=14400.0, expiry_s=86400.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> None:
        with approval_window_scope(14400.0):  # the tiers.rego tier-2 floor
            rid = await gate.submit(approval_request("write.external.email.send", 2))
        row = await approval_store.get(rid)
        # Structural half: escalation must be REACHABLE — strictly inside the window.
        assert _as_utc(row.sla_deadline) < _as_utc(row.expires_at), (
            "sla_deadline is not strictly before expires_at: "
            "await_outcome checks expiry first, so escalation can never fire"
        )
        # Behavioural half: past the derived SLA (window/2 = 7200 s), inside the
        # window — the awaiter escalates + notifies and the request stays approvable.
        clock.advance(7201.0)
        task = asyncio.create_task(gate.await_outcome(rid))

        async def _escalated() -> None:
            while (await approval_store.get(rid)).escalated_at is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(_escalated(), timeout=5)
        outcome = await gate.approve(rid, approver="carol")
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED
        final = await asyncio.wait_for(task, timeout=10)
        assert final.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())
    assert sink.actions().count("approval.escalated") == 1
    assert _outbox_decisions(approval_store, "escalated") == 1


def test_a_configured_short_window_still_expires_at_the_window(approval_store: Any) -> None:
    """The window stays AUTHORITATIVE: 600 s means expired at 600 s — the SLA is
    what moves (derived to 300 s), so escalate-and-notify still precedes expiry."""
    from ne.runtime.approvals import _as_utc
    from ne.runtime.correlation import approval_window_scope

    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=approval_store,
        sink=sink,
        settings=approval_settings(tier2_sla_s=14400.0, expiry_s=86400.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> None:
        with approval_window_scope(600.0):
            rid = await gate.submit(approval_request("write.external.email.send", 2))
        row = await approval_store.get(rid)
        assert (_as_utc(row.expires_at) - _as_utc(row.requested_at)).total_seconds() == 600.0
        assert (_as_utc(row.sla_deadline) - _as_utc(row.requested_at)).total_seconds() == 300.0
        clock.advance(301.0)  # past the derived SLA, inside the window
        task = asyncio.create_task(gate.await_outcome(rid))

        async def _escalated() -> None:
            while (await approval_store.get(rid)).escalated_at is None:
                await asyncio.sleep(0)

        await asyncio.wait_for(_escalated(), timeout=5)
        clock.advance(300.0)  # t = 601 s: past the configured window
        final = await asyncio.wait_for(task, timeout=10)
        assert final.status is ApprovalStatus.EXPIRED

    asyncio.run(scenario())
    assert sink.actions().count("approval.escalated") == 1
    assert sink.actions().count("approval.expired") == 1


def test_a_configured_sla_strictly_inside_the_window_is_honoured_unchanged(
    approval_store: Any,
) -> None:
    """Derivation only fires on collision: sla < window keeps the configured SLA."""
    from ne.runtime.approvals import _as_utc
    from ne.runtime.correlation import approval_window_scope

    gate = DurableApprovalGate(
        store=approval_store,
        sink=RecordingAuditSink(),
        settings=approval_settings(tier2_sla_s=100.0, expiry_s=86400.0, poll_interval_s=0.0),
    )

    async def scenario() -> None:
        with approval_window_scope(14400.0):
            rid = await gate.submit(approval_request("write.external.email.send", 2))
        row = await approval_store.get(rid)
        assert (_as_utc(row.sla_deadline) - _as_utc(row.requested_at)).total_seconds() == 100.0
        assert (_as_utc(row.expires_at) - _as_utc(row.requested_at)).total_seconds() == 14400.0

    asyncio.run(scenario())
