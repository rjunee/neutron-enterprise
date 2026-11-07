# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CKD3 — the PDP-decided tier is the gate authority (Checkpoint-D finding #3).

Closes the CONFIRMED HITL bypass: a Rego-bundle edit escalating an action to
Tier-3 must make the middleware gate at Tier-3 (named approver + distinct second
check), NOT silently degrade to Tier-2 single-approver. The gate tier is the
STRICTER-OF max(PDP-decided tier, spec.tier, tiers.rego python mirror) with
obligation floors; the mirror is a floor, never a ceiling (an ALLOW at effective
tier >= 2 still requires the human).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalOutcome, ApprovalStatus
from ne.contracts.types import ApprovalTier
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.errors import ApprovalNotGrantedError

from .conftest import (
    ALLOWED_RECIPIENT,
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedApprovalGate,
    ToolLog,
    UnusedGate,
    approval_settings,
    make_executor,
    make_principal,
    standard_bindings,
)

# A Rego-bundle escalation of a spec-tier-2 tool (write.external.email.send) to
# Tier-3: named approver + distinct second check.
_TIER3_ESCALATION = {
    "effect": "require-approval",
    "tier": 3,
    "reason": "bundle escalation",
    "obligations": {"named_approver": True, "second_check": True},
    "policy_version": "1.0.0",
}


def _approval_details(sink: RecordingAuditSink) -> list[dict[str, Any]]:
    return [json.loads(e.detail) for e in sink.with_action("tool.approval")]


def test_bundle_escalated_tier3_gates_at_tier3_not_tier2() -> None:
    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={"write.external.email.send": _TIER3_ESCALATION},
    )

    result = asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hi"},
            principal=make_principal(),
        )
    )

    assert result == "sent"
    # spec.tier == 2, mirror == 2, but the bundle DECIDED 3 — decided wins.
    assert gate.requests[0].tier == ApprovalTier.TIER_3
    details = _approval_details(sink)
    assert details and details[0]["tier"] == 3


def _wire_durable(
    sink: RecordingAuditSink, settings: Any
) -> tuple[SqlAlchemyApprovalStore, DurableApprovalGate, ToolLog, Any]:
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(store=store, sink=sink, settings=settings)
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={"write.external.email.send": _TIER3_ESCALATION},
    )
    return store, gate, log, executor


def test_bundle_escalated_tier3_requires_named_plus_distinct_second_check_end_to_end() -> None:
    # THE acceptance test: a spec-tier-2 tool escalated to Tier-3 by the bundle
    # requires TWO distinct named approvers against the REAL DurableApprovalGate —
    # Tier-3 semantics, NOT Tier-2 single-approver.
    sink = RecordingAuditSink()
    store, gate, log, executor = _wire_durable(
        sink, approval_settings(tier3_named_approvers=("alice", "bob"), poll_interval_s=0.0)
    )
    principal = make_principal()

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=principal,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        # The escalated request is genuinely Tier-3 in the durable store.
        pending = await store.list_pending()
        assert pending[0].tier == 3
        # A single named approval does NOT release the tool.
        assert await gate.approve(pending[0].request_id, approver="alice") is None
        for _ in range(5):
            await asyncio.sleep(0)
        assert log.calls == []  # handler NOT run after one approval (pending_second)
        still = await store.list_pending()
        assert still and still[0].tier == 3
        # A DISTINCT second named approver releases exactly one execution.
        await gate.approve(still[0].request_id, approver="bob")
        result = await asyncio.wait_for(task, timeout=10)
        assert result == "sent"
        assert log.names() == ["write.external.email.send"]

    asyncio.run(scenario())


def test_decided_tier_can_never_lower_gating() -> None:
    # The bundle decides Tier-1 for a spec-tier-3 finance action — the gate still
    # fires at TIER_3 (max of the spec/mirror floor); decided tier can only escalate.
    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={
            "finance.payment.initiate": {
                "effect": "require-approval",
                "tier": 1,
                "reason": "rogue low tier",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    result = asyncio.run(
        executor.execute("finance.payment.initiate", {"amount": 100}, principal=make_principal())
    )
    assert result == "paid"
    assert gate.requests[0].tier == ApprovalTier.TIER_3


def test_decided_tier_none_falls_back_to_spec_and_mirror() -> None:
    # tier null on a spec-tier-2 tool ⇒ existing TIER_2 behavior preserved.
    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={
            "write.external.email.send": {
                "effect": "require-approval",
                "tier": None,
                "reason": "no decided tier",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    result = asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hi"},
            principal=make_principal(),
        )
    )
    assert result == "sent"
    assert gate.requests[0].tier == ApprovalTier.TIER_2


def test_second_check_obligation_floors_tier3_without_tier_field() -> None:
    # tier null but obligations.second_check ⇒ TIER_3 by the obligation floor.
    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={
            "write.external.email.send": {
                "effect": "require-approval",
                "tier": None,
                "reason": "second-check obligation",
                "obligations": {"second_check": True},
                "policy_version": "1.0.0",
            }
        },
    )

    asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hi"},
            principal=make_principal(),
        )
    )
    assert gate.requests[0].tier == ApprovalTier.TIER_3


def test_allow_on_tier3_class_still_requires_human() -> None:
    # Rogue-bundle simulation: effect ALLOW, tier 0 on a Tier-3-class action. The
    # mirror is a FLOOR: the gate IS consulted at TIER_3, and the handler NEVER
    # runs without approval (C12).
    sink = RecordingAuditSink()
    gate = ScriptedApprovalGate([ApprovalOutcome(status=ApprovalStatus.REJECTED)])
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={
            "finance.payment.initiate": {
                "effect": "allow",
                "tier": 0,
                "reason": "rogue allow",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    with pytest.raises(ApprovalNotGrantedError):
        asyncio.run(
            executor.execute(
                "finance.payment.initiate", {"amount": 100}, principal=make_principal()
            )
        )
    assert gate.requests and gate.requests[0].tier == ApprovalTier.TIER_3
    assert log.calls == []  # handler never ran on a rogue ALLOW


def test_out_of_range_decided_tier_clamps_to_tier3() -> None:
    # A decided tier above the ladder clamps to TIER_3 — no ValueError.
    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(
        standard_bindings(log),
        sink,
        approval_gate=gate,
        pdp_overrides={
            "write.external.email.send": {
                "effect": "require-approval",
                "tier": 7,
                "reason": "out of range",
                "obligations": {},
                "policy_version": "1.0.0",
            }
        },
    )

    asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"to": ALLOWED_RECIPIENT, "body": "hi"},
            principal=make_principal(),
        )
    )
    assert gate.requests[0].tier == ApprovalTier.TIER_3


def test_allow_tier0_path_unchanged() -> None:
    # No false-positive gating regression: a tier-0 allow runs with no gate.
    sink = RecordingAuditSink()
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    result = asyncio.run(
        executor.execute("read.docs.search", {"q": "hello"}, principal=make_principal())
    )
    assert result == "docs: 3 results"
    assert log.names() == ["read.docs.search"]


# --- NE-WF-SUBJECT-GATE: the workflow-owned per-subject write floor ----------
#
# The per-subject human gate is a WORKFLOW-layer obligation, never a property of
# an action's tier binding (owner ruling; the gate was silently lost when
# write.internal.crm.note correctly moved Tier-2 -> Tier-1 in NE-CS-SEND because
# the workflow engine had been borrowing that declaration as its gate). These
# guardians pin the seam that makes the obligation tier-change-proof: inside
# ``write_approval_floor_scope`` a Tier-1 write STILL parks, so no future
# re-tiering of any write action can un-gate a workflow's write leg again.


def test_write_floor_scope_parks_a_tier1_internal_write() -> None:
    # THE guardian: a Tier-1 action inside a workflow subject still parks. The
    # binding is Tier-1 BY CONSTRUCTION (spec, mirror class, and PDP all agree),
    # so this test cannot be satisfied by any action's tier and cannot be
    # re-lost by a future tier change.
    from ne.runtime.middleware import write_approval_floor_scope

    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    async def scenario() -> str:
        with write_approval_floor_scope(ApprovalTier.TIER_2):
            return await executor.execute(
                "write.internal.notes.append", {"note": "hi"}, principal=make_principal()
            )

    result = asyncio.run(scenario())
    assert result == "noted"
    # The gate WAS consulted, at the workflow's Tier-2 floor — not the spec's 1.
    assert gate.requests and gate.requests[0].tier == ApprovalTier.TIER_2
    details = _approval_details(sink)
    assert details and details[0]["tier"] == 2


def test_write_floor_scope_withholds_the_tier1_write_until_approved() -> None:
    # Reject at the gate => the Tier-1 handler NEVER runs inside the scope.
    from ne.runtime.middleware import write_approval_floor_scope

    sink = RecordingAuditSink()
    gate = ScriptedApprovalGate([ApprovalOutcome(status=ApprovalStatus.REJECTED)])
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    async def scenario() -> None:
        with write_approval_floor_scope(ApprovalTier.TIER_2):
            await executor.execute(
                "write.internal.notes.append", {"note": "hi"}, principal=make_principal()
            )

    with pytest.raises(ApprovalNotGrantedError):
        asyncio.run(scenario())
    assert log.calls == []  # parked, rejected, never written


def test_write_floor_scope_never_touches_reads_and_is_scope_bounded() -> None:
    # Reads inside the scope stay autonomous (the floor is write-class only), and
    # OUTSIDE the scope the Tier-1 write is back to its own autonomous tier —
    # the floor is the workflow's, entered per write leg, never ambient creep.
    from ne.runtime.middleware import current_write_approval_floor, write_approval_floor_scope

    sink = RecordingAuditSink()
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=UnusedGate())

    async def scenario() -> None:
        with write_approval_floor_scope(ApprovalTier.TIER_2):
            assert (
                await executor.execute(
                    "read.docs.search", {"q": "hello"}, principal=make_principal()
                )
                == "docs: 3 results"
            )
        assert current_write_approval_floor() is None  # restored on exit
        # The same Tier-1 write with no scope active: autonomous, gate unconsulted.
        assert (
            await executor.execute(
                "write.internal.notes.append", {"note": "hi"}, principal=make_principal()
            )
            == "noted"
        )

    asyncio.run(scenario())
    assert log.names() == ["read.docs.search", "write.internal.notes.append"]


def test_write_floor_scope_can_never_lower_gating() -> None:
    # Escalate-only: a (nonsensical) TIER_0 floor around the spec-tier-2 send
    # still gates at TIER_2 — the floor joins a max(), it never relaxes.
    from ne.runtime.middleware import write_approval_floor_scope

    sink = RecordingAuditSink()
    gate = NamedApproverGate()
    log = ToolLog()
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    async def scenario() -> str:
        with write_approval_floor_scope(ApprovalTier.TIER_0):
            return await executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=make_principal(),
            )

    assert asyncio.run(scenario()) == "sent"
    assert gate.requests and gate.requests[0].tier == ApprovalTier.TIER_2
