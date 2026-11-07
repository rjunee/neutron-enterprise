# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DurableApprovalGate — tier semantics + the four-layer never-auto-approve proof."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import get_config_hash
from ne.contracts.governance import ApprovalGate, ApprovalStatus
from ne.runtime.approvals import (
    DurableApprovalGate,
    _provisional_count_from_payload,
    _provisional_refs_from_payload,
)
from ne.runtime.errors import (
    ApprovalIntegrityError,
    ApprovalPolicyError,
)

from .conftest import (
    FailingAuditSink,
    RecordingAuditSink,
    approval_request,
    approval_settings,
)


def _gate(store: Any, sink: Any, settings: Any = None, **kwargs: Any) -> DurableApprovalGate:
    return DurableApprovalGate(
        store=store, sink=sink, settings=settings or approval_settings(), **kwargs
    )


def test_gate_conforms_to_frozen_protocol(approval_store: Any) -> None:
    gate = _gate(approval_store, RecordingAuditSink())
    _g: ApprovalGate = gate  # structural conformance (frozen protocol)
    assert _g is gate


def test_tier2_named_approval_returns_approved_outcome(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        outcome = await gate.approve(rid, approver="carol")
        assert outcome is not None
        assert outcome.status is ApprovalStatus.APPROVED
        assert outcome.approver == "carol"
        final = await asyncio.wait_for(gate.await_outcome(rid), timeout=10)
        assert final.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())

    assert "approval.requested" in sink.actions()
    assert "approval.granted" in sink.actions()
    assert all(e.config_hash == get_config_hash() for e in sink.events)
    # Two approval ApprovalEvents landed in the outbox (requested + approved).
    with approval_store.engine.connect() as conn:
        import sqlalchemy as sa

        count = conn.execute(
            sa.text("SELECT COUNT(*) FROM ne_outbox_events WHERE event_type = 'approval'")
        ).scalar()
    assert count == 2


def test_tier2_empty_approver_rejected(approval_store: Any) -> None:
    gate = _gate(approval_store, RecordingAuditSink())

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        with pytest.raises(ApprovalPolicyError):
            await gate.approve(rid, approver="")
        row = await approval_store.get(rid)
        assert row is not None and row.status == "pending"

    asyncio.run(scenario())


def test_tier3_requires_two_distinct_named_approvers(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink, approval_settings(tier3_named_approvers=("alice", "bob")))

    async def scenario() -> None:
        rid = await gate.submit(approval_request("finance.payment.initiate", 3))
        # First named check: recorded, NOT yet approved.
        assert await gate.approve(rid, approver="alice") is None
        row = await approval_store.get(rid)
        assert row is not None and row.status == "pending_second"
        assert "approval.first-check" in sink.actions()
        # Same approver a second time is refused (must be distinct).
        with pytest.raises(ApprovalPolicyError):
            await gate.approve(rid, approver="alice")
        # A distinct named approver completes the approval.
        outcome = await gate.approve(rid, approver="bob")
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())

    granted = sink.with_action("approval.granted")
    assert granted
    prov = dict(granted[0].provenance)
    assert prov["approver"] == "alice"
    assert prov["second_approver"] == "bob"


def test_tier3_unlisted_approver_rejected(approval_store: Any) -> None:
    gate = _gate(
        approval_store,
        RecordingAuditSink(),
        approval_settings(tier3_named_approvers=("alice", "bob")),
    )

    async def scenario() -> None:
        rid = await gate.submit(approval_request("finance.payment.initiate", 3))
        with pytest.raises(ApprovalPolicyError):
            await gate.approve(rid, approver="mallory")

    asyncio.run(scenario())


def test_tier3_with_empty_named_list_can_never_approve(approval_store: Any) -> None:
    # THE never-auto-approve-tier3 structural test: an empty list = unapprovable.
    gate = _gate(approval_store, RecordingAuditSink(), approval_settings(tier3_named_approvers=()))

    async def scenario() -> None:
        rid = await gate.submit(approval_request("finance.payment.initiate", 3))
        with pytest.raises(ApprovalPolicyError):
            await gate.approve(rid, approver="alice")
        row = await approval_store.get(rid)
        assert row is not None and row.status == "pending"

    asyncio.run(scenario())


def test_reject_is_terminal_and_audited(approval_store: Any) -> None:
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        outcome = await gate.reject(rid, approver="dana", reason="not allowed")
        assert outcome.status is ApprovalStatus.REJECTED
        final = await asyncio.wait_for(gate.await_outcome(rid), timeout=10)
        assert final.status is ApprovalStatus.REJECTED

    asyncio.run(scenario())

    assert "approval.rejected" in sink.actions()
    import sqlalchemy as sa

    with approval_store.engine.connect() as conn:
        clean = conn.execute(
            sa.text("SELECT clean FROM ne_approval_evidence WHERE outcome = 'rejected'")
        ).scalar()
    assert clean == 0  # SQLite boolean False


def test_outcome_mapping_refuses_approverless_approved(approval_store: Any) -> None:
    # Belt-and-braces (enforcement layer 4): _to_outcome refuses an APPROVED row with
    # no named approver, even a hand-forged one bypassing decide + the CHECK.
    forged = SimpleNamespace(request_id="x", status="approved", first_approver="", decided_at=None)
    with pytest.raises(ApprovalIntegrityError):
        DurableApprovalGate._to_outcome(forged)


def test_audit_sink_failure_fails_closed(approval_store: Any) -> None:
    gate = _gate(approval_store, FailingAuditSink())

    async def scenario() -> None:
        from ne.runtime.errors import RuntimeAuditError

        with pytest.raises(RuntimeAuditError):
            await gate.submit(approval_request("write.external.email.send", 2))

    asyncio.run(scenario())


class _ApproveThenExpireStore:
    """Store wrapper that lands a REAL concurrent approve just before each expire().

    Wraps AT the gate↔store seam with REAL SQL underneath (plan §1.4 compliant): the
    gate's expiry CAS therefore always LOSES to an approve that landed first, exactly
    the multi-worker interleave the fix targets. Delegates every other method.
    """

    def __init__(self, engine: Any, approver: str = "alice") -> None:
        from ne.runtime.approval_store import SqlAlchemyApprovalStore

        self._inner = SqlAlchemyApprovalStore(engine)
        self.engine = engine
        self._approver = approver
        self._approved = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def expire(self, request_id: str, *, now: Any, event: Any, evidence_row: Any) -> bool:
        if not self._approved:
            self._approved = True
            from ne.events import new_approval_event
            from ne.schema import ApprovalDecision

            row = await self._inner.get(request_id)
            assert row is not None
            await self._inner.decide(
                request_id,
                "approved",
                approver=self._approver,
                now=now,
                event=new_approval_event(
                    approval_id=request_id,
                    tier=int(row.tier),
                    decision=ApprovalDecision.approved,
                    subject_ref=f"tool:{row.action}",
                    actor=self._approver,
                ),
                evidence_row={
                    "request_id": request_id,
                    "action": str(row.action),
                    "tier": int(row.tier),
                    "outcome": "approved",
                    "approver": self._approver,
                    "decided_at": now,
                    "clean": True,
                },
            )
        return await self._inner.expire(request_id, now=now, event=event, evidence_row=evidence_row)


def test_await_outcome_reports_approved_when_expiry_cas_loses() -> None:
    # RED CONTROL: pre-fix, await_outcome unconditionally returned EXPIRED right after
    # `await self._expire(...)` (approvals.py, the old `return ApprovalOutcome(EXPIRED)`
    # on the expires_at branch), MISREPORTING a concurrent approve as EXPIRED. The fix
    # re-reads the ACTUAL terminal state when the expiry CAS loses.
    import sqlalchemy as sa

    from ne.runtime.approval_store import ensure_approval_schema

    from .conftest import FakeClock

    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = _ApproveThenExpireStore(engine, approver="alice")
    clock = FakeClock()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=store,  # type: ignore[arg-type]
        sink=sink,
        settings=approval_settings(expiry_s=1000.0, poll_interval_s=0.0),
        clock=clock,
    )

    async def scenario() -> Any:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(2000.0)  # past hard expiry: await_outcome drives the expiry CAS
        return await asyncio.wait_for(gate.await_outcome(rid), timeout=10)

    outcome = asyncio.run(scenario())
    assert outcome.status is ApprovalStatus.APPROVED
    assert outcome.approver == "alice"
    engine.dispose()


def test_list_pending_maps_provisional_payload_to_exact_field_values(
    approval_store: Any,
) -> None:
    """DurableApprovalGate.list_pending — the console read-model half of NE-T2X-5.

    A row submitted with the provisional-context payload shape middleware.py
    writes (``{"tool": ..., "provisional_source_count": N, "provisional_source_refs":
    [...]}``) must map onto PendingApproval's ``provisional_source_count`` /
    ``provisional_source_refs`` fields with EXACT values (Argus P2 round-1: this
    path previously shipped with zero test coverage). Value-asserted, not
    field-presence — a hardcoded ``count=0``/``refs=()`` would fail this."""
    gate = _gate(approval_store, RecordingAuditSink())
    refs = ["llm:aaaaaaaaaaaaaaaa:e1", "llm:bbbbbbbbbbbbbbbb:e2"]
    request = approval_request(
        "write.internal.notes.append",
        2,
        payload={
            "tool": "write.internal.notes.append",
            "provisional_source_count": 2,
            "provisional_source_refs": refs,
        },
    )

    async def scenario() -> list[Any]:
        await gate.submit(request)
        return await gate.list_pending()

    pending = asyncio.run(scenario())
    assert len(pending) == 1
    assert pending[0].provisional_source_count == 2
    assert pending[0].provisional_source_refs == tuple(refs)


def test_list_pending_legacy_payload_defaults_to_zero_and_empty(
    approval_store: Any,
) -> None:
    """A pre-NE-T2X-5 row (payload carries only ``{"tool": ...}``, no provisional
    keys) is the legacy-shape control: list_pending must default to
    ``provisional_source_count == 0`` / ``provisional_source_refs == ()`` rather
    than raising or fabricating values — the tolerant-parse branch this
    exercises is otherwise unguarded by any test (Argus P2 round-1)."""
    gate = _gate(approval_store, RecordingAuditSink())
    request = approval_request("write.internal.notes.append", 2)  # legacy {"tool": ...} payload

    async def scenario() -> list[Any]:
        await gate.submit(request)
        return await gate.list_pending()

    pending = asyncio.run(scenario())
    assert len(pending) == 1
    assert pending[0].provisional_source_count == 0
    assert pending[0].provisional_source_refs == ()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"provisional_source_count": 3}', 3),  # well-formed
        ('{"provisional_source_count": true}', 0),  # bool rejected (bool-before-int)
        ('{"provisional_source_count": -1}', 0),  # negative coerced to 0
        ('{"provisional_source_count": 1.5}', 0),  # float rejected
        ('{"provisional_source_count": "2"}', 0),  # numeric string rejected (type-strict)
        ("{}", 0),  # missing key
        ("not json", 0),  # malformed JSON
        ("[1, 2, 3]", 0),  # valid JSON, not an object
    ],
)
def test_provisional_count_from_payload_is_type_strict(payload: str, expected: int) -> None:
    """Bidirectional: a well-formed int passes through; every malformed/wrong-typed
    shape (bool, negative, float, numeric-string, missing, invalid JSON, non-object)
    degrades to 0 rather than raising or fabricating a value (Argus P2 round-1)."""
    assert _provisional_count_from_payload(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            '{"provisional_source_refs": ["llm:aaaaaaaaaaaaaaaa:e1", "llm:bbbbbbbbbbbbbbbb:e2"]}',
            ("llm:aaaaaaaaaaaaaaaa:e1", "llm:bbbbbbbbbbbbbbbb:e2"),
        ),
        # non-str entries dropped
        ('{"provisional_source_refs": ["ok", 1, null, "also-ok"]}', ("ok", "also-ok")),
        ('{"provisional_source_refs": "not-a-list"}', ()),  # wrong type entirely
        ("{}", ()),  # missing key
        ("not json", ()),  # malformed JSON
    ],
)
def test_provisional_refs_from_payload_filters_to_strings(
    payload: str, expected: tuple[str, ...]
) -> None:
    """Bidirectional: a well-formed list of strings passes through unchanged; any
    non-list value or non-string entry is dropped rather than raising (Argus P2
    round-1) — the display-only channel never fabricates a ref."""
    assert _provisional_refs_from_payload(payload) == expected


# --- separation of duties: the requester may never decide -------------------


def test_requester_cannot_approve_its_own_request_at_tier2(approval_store: Any) -> None:
    """SPEC/doc claim ("the requester cannot approve their own lift"), enforced.

    Tier 2 grants on ANY single named approver — only the Tier-3 SECOND approver
    was ever checked for distinctness — so nothing here stopped a principal that
    held both the requesting capability and the approver role from deciding its
    own external write (``docs/ops/air-gap-recovery.md`` §2 step 1).

    Bidirectional: the SAME row, the SAME instant, is granted by a distinct
    approver — so the refusal is about WHO decided, not about the row.
    """
    gate = _gate(approval_store, RecordingAuditSink())

    async def scenario() -> None:
        rid = await gate.submit(
            approval_request("write.external.network.egress.expand", 2, principal_id="user:ada")
        )
        with pytest.raises(ApprovalPolicyError, match="separation of duties"):
            await gate.approve(rid, approver="user:ada")

        # The refusal decided NOTHING: the row is still actionable.
        row = await approval_store.get(rid)
        assert row is not None and row.status == "pending"

        # CONTROL — a distinct approver grants the very same request.
        outcome = await gate.approve(rid, approver="carol")
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())


def test_requester_cannot_cast_the_first_tier3_check(approval_store: Any) -> None:
    """The Tier-3 FIRST check is an approval too — the requester may not cast it.

    The existing distinctness rule only ever compared the SECOND approver to the
    first, so a named requester could cast check one and a single accomplice the
    second: two names on the row, one of them the person who asked.
    """
    gate = _gate(
        approval_store,
        RecordingAuditSink(),
        settings=approval_settings(tier3_named_approvers=("user:ada", "carol", "dave")),
    )

    async def scenario() -> None:
        rid = await gate.submit(
            approval_request("write.external.wire.transfer", 3, principal_id="user:ada")
        )
        with pytest.raises(ApprovalPolicyError, match="separation of duties"):
            await gate.approve(rid, approver="user:ada")

        # CONTROL — the same first check from a named non-requester is recorded.
        assert await gate.approve(rid, approver="carol") is None
        row = await approval_store.get(rid)
        assert row is not None and row.status == "pending_second"
        assert row.first_approver == "carol"

    asyncio.run(scenario())
