# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PromotionTracker — evidence trail + logged eligibility, changing NO tier/policy."""

from __future__ import annotations

import asyncio
from typing import Any

import sqlalchemy as sa

from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.promotion import PromotionTracker

from .conftest import RecordingAuditSink, approval_request, approval_settings

_ACTION = "write.external.email.send"


def _gate(store: Any) -> DurableApprovalGate:
    return DurableApprovalGate(store=store, sink=RecordingAuditSink(), settings=approval_settings())


async def _approve_n(gate: DurableApprovalGate, store: Any, n: int) -> None:
    for _ in range(n):
        rid = await gate.submit(approval_request(_ACTION, 2))
        await gate.approve(rid, approver="carol")


async def _reject_n(gate: DurableApprovalGate, store: Any, n: int) -> None:
    for _ in range(n):
        rid = await gate.submit(approval_request(_ACTION, 2))
        await gate.reject(rid, approver="carol")


def _tracker(store: Any, **kwargs: Any) -> PromotionTracker:
    return PromotionTracker(store, approval_settings(**kwargs))


def test_evidence_trail_recorded_per_terminal_decision(approval_store: Any) -> None:
    async def scenario() -> None:
        gate = _gate(approval_store)
        await _approve_n(gate, approval_store, 3)
        await _reject_n(gate, approval_store, 2)
        rows = await approval_store.evidence_window(_ACTION, 50)
        assert len(rows) == 5
        assert sum(1 for r in rows if r.clean) == 3

    asyncio.run(scenario())


def test_not_eligible_below_min_instances(approval_store: Any) -> None:
    async def scenario() -> None:
        gate = _gate(approval_store)
        await _approve_n(gate, approval_store, 5)
        evidence = await _tracker(approval_store, promotion_min_instances=20).evaluate(_ACTION)
        assert evidence.total == 5
        assert evidence.approval_rate == 1.0
        assert evidence.eligible is False

    asyncio.run(scenario())


def test_not_eligible_below_95_percent_rate(approval_store: Any) -> None:
    async def scenario() -> None:
        gate = _gate(approval_store)
        await _approve_n(gate, approval_store, 18)
        await _reject_n(gate, approval_store, 4)  # 18/22 ~= 0.818
        evidence = await _tracker(
            approval_store, promotion_min_instances=20, promotion_min_approval_rate=0.95
        ).evaluate(_ACTION)
        assert evidence.total == 22
        assert evidence.approval_rate < 0.95
        assert evidence.eligible is False

    asyncio.run(scenario())


def test_violation_zeroes_eligibility(approval_store: Any) -> None:
    async def scenario() -> None:
        gate = _gate(approval_store)
        await _approve_n(gate, approval_store, 25)  # 100% rate, above min
        tracker = _tracker(approval_store, promotion_min_instances=20)
        assert (await tracker.evaluate(_ACTION)).eligible is True
        await tracker.record_violation(_ACTION, "GuardrailViolation")
        after = await tracker.evaluate(_ACTION)
        assert after.violations == 1
        assert after.eligible is False

    asyncio.run(scenario())


def test_eligible_action_emits_logged_data_decision(approval_store: Any) -> None:
    sink = RecordingAuditSink()

    async def scenario() -> None:
        gate = _gate(approval_store)
        await _approve_n(gate, approval_store, 25)
        tracker = _tracker(approval_store, promotion_min_instances=20)

        # Snapshot request/evidence/violation counts BEFORE evaluate_and_record.
        def _counts() -> tuple[int, int, int]:
            with approval_store.engine.connect() as conn:
                reqs = conn.execute(sa.text("SELECT COUNT(*) FROM ne_approval_requests")).scalar()
                ev = conn.execute(sa.text("SELECT COUNT(*) FROM ne_approval_evidence")).scalar()
                vio = conn.execute(sa.text("SELECT COUNT(*) FROM ne_promotion_violations")).scalar()
            return int(reqs), int(ev), int(vio)

        before = _counts()
        evidence = await tracker.evaluate_and_record(_ACTION, sink=sink)
        after = _counts()

        assert evidence.eligible is True
        # A logged data decision: NO tier/policy/store state changed.
        assert before == after
        logged = [
            e for e in sink.events if getattr(e, "action", "") == "approval.promotion.evaluated"
        ]
        assert len(logged) == 1
        assert '"eligible": true' in logged[0].detail

    asyncio.run(scenario())


def test_zero_history_not_eligible(approval_store: Any) -> None:
    async def scenario() -> None:
        evidence = await _tracker(approval_store).evaluate(_ACTION)
        assert evidence.total == 0
        assert evidence.approval_rate == 0.0  # no div-by-zero
        assert evidence.eligible is False

    asyncio.run(scenario())
