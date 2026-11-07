# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""DurableApprovalGate.submit() scope-keyed idempotency (NE-INV-APPROVAL-SCOPE).

Closes invariant-audit Finding A: without this fix, a DBOS crash-retry of
``execute_turn_step`` re-runs ``submit()`` from the top and mints a SECOND,
orphaned pending row for what is really the same approval request — the first
row is never polled again and there is no janitor sweep. This mirrors the exact
test shape NE-ACTION-DETERMINISTIC-REQID proved for the write-dedup path
(``test_action_tools.py``), applied to approval submission instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalStatus
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate, canonical_args_hash
from ne.runtime.correlation import (
    ConversationAttemptBinding,
    action_scope,
    conversation_attempt_scope,
)
from ne.runtime.errors import ApprovalRequestNotFoundError
from ne.runtime.store import (
    SqlAlchemySessionStore,
    conversation_request_fingerprint,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
)

from .conftest import RecordingAuditSink, approval_request, approval_settings


def _gate(store: Any, sink: Any, settings: Any = None) -> DurableApprovalGate:
    return DurableApprovalGate(store=store, sink=sink, settings=settings or approval_settings())


def _approval_event_count(store: Any) -> int:
    with store.engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT COUNT(*) FROM ne_outbox_events WHERE event_type = 'approval'")
        ).scalar()


def test_same_scope_resubmit_returns_same_request_id(approval_store: Any) -> None:
    """THE core claim-or-find semantics: a second submit() under the SAME ambient
    scope reattaches to the first request instead of minting a duplicate row."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            first = await gate.submit(approval_request("write.external.email.send", 2))
            second = await gate.submit(approval_request("write.external.email.send", 2))
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second
    pending = asyncio.run(approval_store.list_pending())
    assert [p.request_id for p in pending] == [first]
    assert _approval_event_count(approval_store) == 1
    assert sink.actions().count("approval.requested") == 1
    assert sink.actions().count("approval.reattached") == 1


def test_takeover_attempt_gets_fresh_approval_and_old_decisions_are_powerless(
    approval_store: SqlAlchemyApprovalStore,
) -> None:
    ensure_runtime_schema(approval_store.engine)
    session_store = SqlAlchemySessionStore(approval_store.engine)
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)
    request = approval_request("write.external.email.send", 2)

    async def scenario() -> None:
        conversation = await session_store.create_conversation(
            "console-chat", "engineering", request.principal.id
        )
        turn_id = "turn-approval-takeover"
        trace_id = "0123456789abcdef0123456789abcdef"
        fingerprint = conversation_request_fingerprint(
            conversation.conversation_id,
            request.principal.id,
            "engineering",
            "console-chat",
            "same prompt",
            None,
            None,
            trace_id,
        )
        first = await session_store.claim_conversation_attachment(
            conversation.conversation_id,
            request.principal.id,
            "engineering",
            "console-chat",
            turn_id,
            fingerprint,
            trace_id,
        )
        assert first.attempt_id is not None
        first_binding = ConversationAttemptBinding(
            conversation.conversation_id,
            request.principal.id,
            "engineering",
            "console-chat",
            turn_id,
            first.attempt_id,
            trace_id,
        )
        with action_scope(turn_id), conversation_attempt_scope(first_binding):
            first_request = await gate.submit(request, args_hash="a" * 64)

        with session_store.engine.begin() as conn:
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(ne_agent_conversation_turns.c.attachment_id == turn_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        second = await session_store.claim_conversation_attachment(
            conversation.conversation_id,
            request.principal.id,
            "engineering",
            "console-chat",
            turn_id,
            fingerprint,
            trace_id,
        )
        assert second.attempt_id is not None and second.attempt_id != first.attempt_id
        second_binding = ConversationAttemptBinding(
            conversation.conversation_id,
            request.principal.id,
            "engineering",
            "console-chat",
            turn_id,
            second.attempt_id,
            trace_id,
        )
        with action_scope(turn_id), conversation_attempt_scope(second_binding):
            second_request = await gate.submit(request, args_hash="a" * 64)
        assert second_request != first_request
        with pytest.raises(ApprovalRequestNotFoundError):
            await gate.approve(first_request, approver="carol")
        with pytest.raises(ApprovalRequestNotFoundError):
            await gate.reject(first_request, approver="carol")
        await gate.approve(second_request, approver="carol")
        with conversation_attempt_scope(second_binding):
            await gate.assert_current_attempt(second_request)
        with conversation_attempt_scope(first_binding), pytest.raises(ApprovalRequestNotFoundError):
            await gate.assert_current_attempt(second_request)

    asyncio.run(scenario())


def test_different_scopes_create_independent_rows(approval_store: Any) -> None:
    """No cross-turn over-merge — the mirror of the REQID unit's cross-scope test."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            first = await gate.submit(approval_request("write.external.email.send", 2))
        with action_scope("turn-2"):
            second = await gate.submit(approval_request("write.external.email.send", 2))
        return first, second

    first, second = asyncio.run(scenario())

    assert first != second
    pending = asyncio.run(approval_store.list_pending())
    assert {p.request_id for p in pending} == {first, second}


def test_no_ambient_scope_preserves_fresh_uuid_behavior(approval_store: Any) -> None:
    """Legacy no-scope path stays byte-identical: two submits -> two rows, both with
    a NULL action_scope."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        first = await gate.submit(approval_request("write.external.email.send", 2))
        second = await gate.submit(approval_request("write.external.email.send", 2))
        return first, second

    first, second = asyncio.run(scenario())

    assert first != second
    row_first = asyncio.run(approval_store.get(first))
    row_second = asyncio.run(approval_store.get(second))
    assert row_first is not None and row_first.action_scope is None
    assert row_second is not None and row_second.action_scope is None
    assert "approval.reattached" not in sink.actions()


def test_crash_retry_reattaches_across_restart(tmp_path: Path) -> None:
    """THE finding scenario itself: a worker crash mid-approval-wait, followed by
    DBOS's step-retry-from-the-top re-running submit() under the SAME action_scope,
    must reattach to the original row — never orphan it. Mirrors
    test_approval_restart.py's file-backed-sqlite "restart" shape."""
    url = f"sqlite:///{tmp_path / 'approvals.db'}"

    def _fresh_gate() -> tuple[DurableApprovalGate, SqlAlchemyApprovalStore, sa.Engine]:
        engine = sa.create_engine(url)
        ensure_approval_schema(engine)
        store = SqlAlchemyApprovalStore(engine)
        gate = DurableApprovalGate(
            store=store, sink=RecordingAuditSink(), settings=approval_settings()
        )
        return gate, store, engine

    # A DBOS re-run-from-top re-executes the SAME tool call with the SAME
    # arguments, so an identical args_hash across the simulated crash/restart is
    # the realistic scenario (not a new constraint on top of Finding A).
    retry_hash = canonical_args_hash({"to": "ops@example.com", "body": "hi"})

    async def scenario() -> None:
        gate_a, _store_a, engine_a = _fresh_gate()
        with action_scope("turn-9"):
            rid_a = await gate_a.submit(
                approval_request("write.external.email.send", 2), args_hash=retry_hash
            )
        engine_a.dispose()  # the "crash"

        # DBOS re-runs execute_turn_step FROM THE TOP: the same submit() call, under
        # the SAME action_scope with the SAME args_hash, on a fresh engine/store/gate.
        gate_b, store_b, engine_b = _fresh_gate()
        try:
            with action_scope("turn-9"):
                rid_b = await gate_b.submit(
                    approval_request("write.external.email.send", 2), args_hash=retry_hash
                )
            assert rid_b == rid_a

            pending = await store_b.list_pending()
            assert [p.request_id for p in pending] == [rid_a]  # no orphan

            outcome = await gate_b.approve(rid_b, approver="carol")
            assert outcome is not None and outcome.status is ApprovalStatus.APPROVED
            final = await asyncio.wait_for(gate_b.await_outcome(rid_b), timeout=10)
            assert final.status is ApprovalStatus.APPROVED
        finally:
            engine_b.dispose()

    asyncio.run(scenario())


def test_terminal_row_not_reattached_in_same_scope(approval_store: Any) -> None:
    """A rejected/expired row never absorbs a re-request (§4.2 re-request semantics
    preserved): submit, reject, submit again under the SAME scope -> a NEW row."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            first = await gate.submit(approval_request("write.external.email.send", 2))
            await gate.reject(first, approver="dana", reason="not now")
            second = await gate.submit(approval_request("write.external.email.send", 2))
        return first, second

    first, second = asyncio.run(scenario())

    assert first != second
    pending = asyncio.run(approval_store.list_pending())
    assert [p.request_id for p in pending] == [second]


def test_same_scope_different_action_or_principal_not_merged(approval_store: Any) -> None:
    """The claim key is the FULL (principal, action, tier, scope) key — this test
    covers action/principal; see test_same_scope_different_tier_not_merged for the
    tier dimension (Codex review finding C1)."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str, str, str]:
        with action_scope("turn-1"):
            a = await gate.submit(approval_request("write.external.email.send", 2))
            b = await gate.submit(approval_request("finance.payment.initiate", 3))
            c = await gate.submit(
                approval_request("write.external.email.send", 2, principal_id="user:ada")
            )
            d = await gate.submit(
                approval_request("write.external.email.send", 2, principal_id="user:bob")
            )
        return a, b, c, d

    a, b, c, d = asyncio.run(scenario())

    assert a != b  # different action -> distinct rows
    assert a == c  # same (principal, action, scope) -> reattached
    assert a != d  # different principal -> distinct rows
    pending = asyncio.run(approval_store.list_pending())
    assert {p.request_id for p in pending} == {a, b, d}


def test_same_scope_different_tier_not_merged(approval_store: Any) -> None:
    """Codex review finding C1: the claim key includes ``tier``. Without it, a
    Tier-3 request submitted after a Tier-2 pending row for the same (principal,
    action, scope) would silently reattach to the Tier-2 row and inherit its
    single-approver policy — bypassing the Tier-3 two-approver requirement. Two
    distinct rows, and each awaits its OWN tier's approval policy."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink, approval_settings(tier3_named_approvers=("carol",)))

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            tier2 = await gate.submit(approval_request("write.external.email.send", 2))
            tier3 = await gate.submit(approval_request("write.external.email.send", 3))
        return tier2, tier3

    tier2, tier3 = asyncio.run(scenario())

    assert tier2 != tier3
    pending = asyncio.run(approval_store.list_pending())
    assert {p.request_id for p in pending} == {tier2, tier3}

    row_tier2 = asyncio.run(approval_store.get(tier2))
    row_tier3 = asyncio.run(approval_store.get(tier3))
    assert row_tier2 is not None and int(row_tier2.tier) == 2
    assert row_tier3 is not None and int(row_tier3.tier) == 3

    # The Tier-3 request still requires TWO distinct named approvers — a single
    # approval on it must not resolve it (proving it never inherited Tier-2's
    # single-approver policy via a merged row).
    first_check = asyncio.run(gate.approve(tier3, approver="carol"))
    assert first_check is None
    row_after_first = asyncio.run(approval_store.get(tier3))
    assert row_after_first is not None and row_after_first.status == "pending_second"


def test_lost_insert_race_reattaches_to_winner(approval_store: Any) -> None:
    """Deterministic exercise of the IntegrityError branch: a find() that misses on
    the FIRST look (as if a concurrent DBOS step-retry hadn't landed its row yet),
    followed by a real duplicate-triple insert winning the race underneath, must
    reattach to the winner's row on the loop's next find — never raise, never
    duplicate."""
    sink = RecordingAuditSink()
    real_find = approval_store.find_pending_by_scope
    calls = {"n": 0}

    async def _flaky_find_once(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # simulate the miss that sends submit() down the insert path
        return await real_find(**kwargs)

    approval_store.find_pending_by_scope = _flaky_find_once  # type: ignore[method-assign]
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            # Pre-insert the "winner" row directly via the real store, simulating a
            # concurrent caller that claimed the triple between our miss and our
            # insert attempt.
            winner_rid = "winner-rid"
            from datetime import UTC, datetime, timedelta

            from ne.events import new_approval_event
            from ne.schema import ApprovalDecision

            now = datetime.now(UTC)
            await approval_store.create(
                request_id=winner_rid,
                action="write.external.email.send",
                tier=2,
                principal_id="user:ada",
                payload='{"tool": "write.external.email.send"}',
                now=now,
                sla_deadline=now + timedelta(hours=4),
                expires_at=now + timedelta(hours=24),
                event=new_approval_event(
                    approval_id=winner_rid,
                    tier=2,
                    decision=ApprovalDecision.requested,
                    subject_ref="tool:write.external.email.send",
                    actor="user:ada",
                    sla_deadline=now + timedelta(hours=4),
                ),
                action_scope="turn-1",
                # gate.submit() below is called WITHOUT an explicit args_hash, so
                # it falls back to canonical_args_hash(request.payload) — this row
                # MUST carry that same fallback hash or the widened find/index
                # cannot see it and the reattach never happens.
                args_hash=canonical_args_hash({"tool": "write.external.email.send"}),
            )

            # gate.submit()'s first find() (the patched one) returns None (miss);
            # its create() then loses the partial-unique-index race to the row we
            # just inserted above and raises IntegrityError; the loop's SECOND find
            # (real, unpatched) discovers the winner and reattaches.
            rid = await gate.submit(approval_request("write.external.email.send", 2))
            return rid, winner_rid

    rid, winner_rid = asyncio.run(scenario())

    assert rid == winner_rid
    pending = asyncio.run(approval_store.list_pending())
    assert [p.request_id for p in pending] == [winner_rid]


# --- per-invocation args_hash discriminator (Argus re-check P1, round 2) ---


def test_same_scope_different_args_not_merged(approval_store: Any) -> None:
    """THE Argus P1 pin: two GENUINELY DISTINCT concurrent-shaped submits under the
    SAME (principal, action, tier, scope) but with DIFFERENT args_hash must land on
    TWO independent rows — a single human decision can never authorize both."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            a = await gate.submit(
                approval_request("write.external.email.send", 2),
                args_hash=canonical_args_hash({"to": "x@allowed.example", "body": "invoice A"}),
            )
            b = await gate.submit(
                approval_request("write.external.email.send", 2),
                args_hash=canonical_args_hash({"to": "x@allowed.example", "body": "invoice B"}),
            )
        return a, b

    a, b = asyncio.run(scenario())

    assert a != b
    pending = asyncio.run(approval_store.list_pending())
    assert {p.request_id for p in pending} == {a, b}

    # Approving ONE distinct call must never resolve the OTHER — each requires its
    # own human decision.
    asyncio.run(gate.approve(a, approver="carol"))
    still_pending = asyncio.run(approval_store.list_pending())
    assert [p.request_id for p in still_pending] == [b]


def test_same_scope_same_args_hash_reattaches(approval_store: Any) -> None:
    """Two submits with the SAME explicit args_hash under the same scope reattach
    to ONE row — mirrors test_same_scope_resubmit_returns_same_request_id for the
    args_hash dimension."""
    sink = RecordingAuditSink()
    gate = _gate(approval_store, sink)
    same_hash = canonical_args_hash({"to": "x@allowed.example", "body": "invoice A"})

    async def scenario() -> tuple[str, str]:
        with action_scope("turn-1"):
            first = await gate.submit(
                approval_request("write.external.email.send", 2), args_hash=same_hash
            )
            second = await gate.submit(
                approval_request("write.external.email.send", 2), args_hash=same_hash
            )
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second
    pending = asyncio.run(approval_store.list_pending())
    assert [p.request_id for p in pending] == [first]
    assert _approval_event_count(approval_store) == 1
    assert sink.actions().count("approval.requested") == 1
    assert sink.actions().count("approval.reattached") == 1


def test_canonical_args_hash_is_key_order_independent() -> None:
    """Pins canonical_args_hash's SHAPE parity with action_tools._payload_hash: a
    key-order-independent json.dumps(sort_keys=True) then sha256 hex digest — a
    TEST, not prose, per the written-rules-disarm-review policy."""
    expected = hashlib.sha256(
        json.dumps({"a": 1, "b": 2}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert canonical_args_hash({"b": 2, "a": 1}) == canonical_args_hash({"a": 1, "b": 2})
    assert canonical_args_hash({"a": 1, "b": 2}) == expected
