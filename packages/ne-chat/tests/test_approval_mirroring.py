# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Approval mirroring — the security crux: Slack action = signed submission onto
the REAL NE-035 DurableApprovalGate, never a bypass. The gate stays authoritative;
the approver identity is ALWAYS the mapped principal.id, never the Slack id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx

from ne.chat.approvals import ApprovalActionService
from ne.chat.slack.mirror import SlackApprovalMirror
from ne.contracts.governance import ApprovalRequest
from ne.contracts.types import ApprovalTier, Principal
from ne.runtime.errors import ApprovalSourceWithheldFromApproverError
from ne.runtime.source_results import SourceAuthorityRetryableError

from .conftest import (
    ALLOWED_RECIPIENT,
    FakeClock,
    RecordingAuditSink,
    ToolLog,
    approver_linker,
    conversation_bindings,
    make_executor,
    make_gate,
    recording_client,
)

APPROVER_ROLES = ("role:approver",)
_REQUESTER = Principal(id="user:ada", claims={"department": "ops"})


def _in_order(haystack: list[str], needles: list[str]) -> bool:
    it = iter(haystack)
    return all(n in it for n in needles)


async def _submit(gate: Any, *, tier: int, action: str, payload: dict[str, Any]) -> str:
    return await gate.submit(
        ApprovalRequest(
            tier=ApprovalTier(tier), action=action, principal=_REQUESTER, payload=payload
        )
    )


async def _row(store: Any, rid: str) -> Any:
    row = await store.get(rid)
    assert row is not None
    return row


def test_pending_tier2_mirrored_with_actionable_buttons() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)
    recorder: list[httpx.Request] = []
    mirror = SlackApprovalMirror(recording_client(recorder))

    async def scenario() -> None:
        # A payload carrying a SENSITIVE value that must never be rendered.
        await _submit(
            gate,
            tier=2,
            action="write.external.email.send",
            payload={"tool": "write.external.email.send", "iban": "SECRET-IBAN-9999"},
        )
        pending = (await gate.list_pending())[0]
        await mirror.mirror_pending(pending, channel="C0APPROVAL0")

    asyncio.run(scenario())
    body = json.loads(recorder[0].content)
    rendered = json.dumps(body)
    assert "ne_approve" in rendered and "ne_deny" in rendered
    # The buttons carry the durable request id as their value.
    request_id = asyncio.run(_only_pending_id(store))
    assert request_id in rendered
    # Redaction: the approval payload VALUES are never rendered.
    assert "SECRET-IBAN-9999" not in rendered


async def _only_pending_id(store: Any) -> str:
    rows = await store.list_pending()
    return str(rows[0].request_id)


def test_authorized_mapped_approver_approves_through_real_gate() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)
    log = ToolLog()
    executor = make_executor(conversation_bindings(log), sink, approval_gate=gate)

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {"to": ALLOWED_RECIPIENT, "body": "hi"},
                principal=_REQUESTER,
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        rid = (await store.list_pending())[0].request_id
        # carol clicks Approve in Slack (mapped principal id "carol", role:approver).
        result = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="approve")
        assert result.status == "approved"
        assert result.approver_id == "carol"  # the mapped principal id, NOT "U0CAROL000"
        assert await asyncio.wait_for(task, timeout=10) == "sent"
        # The gate row records the PLATFORM principal id as the approver.
        row = await _row(store, rid)
        assert row.first_approver == "carol"

    asyncio.run(scenario())
    assert log.names() == ["write.external.email.send"]
    actions = sink.actions()
    assert _in_order(
        actions, ["approval.requested", "approval.granted", "tool.approval", "tool.execute"]
    )
    assert "chat.approval.submitted" in actions


def test_unauthorized_role_less_clicker_rejected_and_audited() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=2, action="write.external.email.send", payload={"tool": "x"})
        # frank is MAPPED but has no approver role.
        result = await svc.act(external_user_id="U0FRANK000", request_id=rid, decision="approve")
        assert result.status == "rejected"
        # The gate was never asked to decide: the row is STILL pending.
        assert (await _row(store, rid)).status == "pending"

    asyncio.run(scenario())
    unauth = sink.with_action("chat.approval.unauthorized")
    assert unauth and unauth[0].actor == "frank"
    assert "not-an-approver" in (unauth[0].detail or "")


def test_unmapped_clicker_rejected_and_audited() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=2, action="write.external.email.send", payload={"tool": "x"})
        result = await svc.act(external_user_id="U0MALLORY0", request_id=rid, decision="approve")
        assert result.status == "rejected"
        assert (await _row(store, rid)).status == "pending"  # gate never touched

    asyncio.run(scenario())
    # both the linker's identity denial AND the approval-layer denial are recorded.
    assert sink.with_action("chat.identity.denied")
    unauth = sink.with_action("chat.approval.unauthorized")
    assert unauth and unauth[0].actor == "slack:U0MALLORY0"


def test_tier3_named_approver_rule_enforced_by_gate() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)  # named approvers: carol, dave

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=3, action="finance.payment.initiate", payload={"tool": "x"})
        # eve has role:approver (passes the chat pre-check) but is NOT a named
        # Tier-3 approver — the GATE rejects her with ApprovalPolicyError.
        result = await svc.act(external_user_id="U0EVE000", request_id=rid, decision="approve")
        assert result.status == "rejected"
        assert (await _row(store, rid)).status == "pending"

    asyncio.run(scenario())
    unauth = sink.with_action("chat.approval.unauthorized")
    assert unauth and "gate-policy" in (unauth[-1].detail or "")


def test_source_withheld_for_approver_is_reported_pending_not_crashed() -> None:
    """The gate's typed per-principal source refusal maps to an honest chat reply.

    U2 adversarial review (standalone-gate ruling): a deciding approver whose own
    authority does not admit a protected request's invocation gets a typed,
    NON-PERSISTING refusal from the gate — the request stays pending for another
    approver. The chat surface must report exactly that (audited, no crash, no
    claim that the request itself was decided). The gate is stubbed here only to
    inject the typed raise; the mapping under test is the service's.
    """
    sink = RecordingAuditSink()

    class _WithholdingGate:
        async def approve(self, request_id: str, *, approver: str) -> Any:
            raise ApprovalSourceWithheldFromApproverError(
                "current source authority does not admit this approver"
            )

    async def scenario() -> None:
        svc = ApprovalActionService(
            cast(Any, _WithholdingGate()), await approver_linker(sink), APPROVER_ROLES, sink
        )
        result = await svc.act(
            external_user_id="U0CAROL000", request_id="req-1", decision="approve"
        )
        assert result.status == "rejected"
        assert result.note is not None and "remains pending" in result.note

    asyncio.run(scenario())
    unauth = sink.with_action("chat.approval.unauthorized")
    assert unauth and "source-authority-withheld-for-principal" in (unauth[-1].detail or "")


def test_retryable_source_authority_gets_an_honest_retry_reply_not_silence() -> None:
    """Indeterminate authority must answer the approver, not vanish (Sol P2).

    The gate raises ``SourceAuthorityRetryableError`` while writing nothing —
    the row stays pending. Slack runs ``act`` in a background task that logs
    and rethrows, so an uncaught raise means the approver sees NO ephemeral
    reply at all. The service must return the honest "still pending, try again"
    view instead. The gate is stubbed only to inject the raise; the mapping
    under test is the service's.
    """
    sink = RecordingAuditSink()

    class _RetryableGate:
        async def approve(self, request_id: str, *, approver: str) -> Any:
            raise SourceAuthorityRetryableError("current source authority could not be established")

    async def scenario() -> None:
        svc = ApprovalActionService(
            cast(Any, _RetryableGate()), await approver_linker(sink), APPROVER_ROLES, sink
        )
        result = await svc.act(
            external_user_id="U0CAROL000", request_id="req-1", decision="approve"
        )
        assert result.status == "rejected"
        assert result.note is not None
        assert "remains pending" in result.note and "try again" in result.note
        assert "nothing was decided" in result.note

    asyncio.run(scenario())


def test_tier3_distinct_second_check_from_slack() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=3, action="finance.payment.initiate", payload={"tool": "x"})
        # carol first check -> pending_second.
        r1 = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="approve")
        assert r1.status == "pending_second"
        assert (await _row(store, rid)).status == "pending_second"
        # carol again -> the gate refuses a same-approver second check.
        r2 = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="approve")
        assert r2.status == "rejected"
        # dave (distinct named approver) completes it.
        r3 = await svc.act(external_user_id="U0DAVE000", request_id=rid, decision="approve")
        assert r3.status == "approved"
        row = await _row(store, rid)
        assert row.status == "approved"
        assert row.first_approver == "carol"
        assert row.second_approver == "dave"

    asyncio.run(scenario())


def test_expired_request_not_grantable_from_slack() -> None:
    sink = RecordingAuditSink()
    clock = FakeClock()
    store, gate, _e = make_gate(sink, expiry_s=100.0, clock=clock)

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=3, action="finance.payment.initiate", payload={"tool": "x"})
        clock.advance(500.0)  # past hard expiry
        result = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="approve")
        assert result.status == "rejected"
        assert (await _row(store, rid)).status == "expired"

    asyncio.run(scenario())
    assert sink.with_action("chat.approval.stale")


def test_deny_from_slack_routes_through_gate_reject() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=2, action="write.external.email.send", payload={"tool": "x"})
        result = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="deny")
        assert result.status == "denied"
        assert result.approver_id == "carol"
        row = await _row(store, rid)
        assert row.status == "rejected"
        assert row.first_approver == "carol"

    asyncio.run(scenario())
    assert "chat.approval.submitted" in sink.actions()


def test_empty_approver_roles_fails_closed() -> None:
    sink = RecordingAuditSink()
    store, gate, _e = make_gate(sink)

    async def scenario() -> None:
        # approver_roles=() ⇒ NOBODY can action approvals from Slack — even a named,
        # role-bearing Tier-3 approver like carol.
        svc = ApprovalActionService(gate, await approver_linker(sink), (), sink)
        rid = await _submit(gate, tier=3, action="finance.payment.initiate", payload={"tool": "x"})
        result = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="approve")
        assert result.status == "rejected"
        assert (await _row(store, rid)).status == "pending"  # gate never called

    asyncio.run(scenario())
    assert sink.with_action("chat.approval.unauthorized")


def test_actioned_message_buttons_replaced() -> None:
    sink = RecordingAuditSink()
    _store, gate, _e = make_gate(sink)
    recorder: list[httpx.Request] = []
    mirror = SlackApprovalMirror(recording_client(recorder))

    async def scenario() -> None:
        svc = ApprovalActionService(gate, await approver_linker(sink), APPROVER_ROLES, sink)
        rid = await _submit(gate, tier=2, action="write.external.email.send", payload={"tool": "x"})
        result = await svc.act(external_user_id="U0CAROL000", request_id=rid, decision="approve")
        await mirror.replace_with_outcome(
            channel="C0APPROVAL0",
            ts="1720000000.000100",
            request_id=rid,
            status=result.status,
            approver_id=result.approver_id,
        )

    asyncio.run(scenario())
    body = json.loads(recorder[-1].content)
    assert recorder[-1].url.path.endswith("chat.update")
    rendered = json.dumps(body)
    assert "ne_approve" not in rendered  # buttons removed on the actioned mirror
    assert "approved" in rendered
