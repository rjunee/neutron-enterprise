# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The 3 reference actions end-to-end through the REAL governance stack (NE-050).

No mock past the governance seam: real GovernedToolExecutor + rego-faithful PDP + real
guardrail engine + real DurableApprovalGate; only the connector HTTP wire (httpx
MockTransport) and the SQL engine (sqlite) are stubbed at the source-system seam.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.contracts.governance import ApprovalStatus
from ne.govern.errors import GuardrailViolation
from ne.runtime.action_tools import (
    ActionDedupStore,
    build_action_binding,
    ensure_action_dedup_schema,
)
from ne.runtime.approval_store import SqlAlchemyApprovalStore, ensure_approval_schema
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.errors import (
    ApprovalIntegrityError,
    ApprovalNotGrantedError,
    ToolDeniedError,
)
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.reference_actions import (
    CRM_NOTE_ARG_POLICY,
    EMAIL_DRAFT_ARG_POLICY,
    TICKET_UPDATE_ARG_POLICY,
    crm_note_definition,
    email_draft_definition,
    email_send_definition,
    ticket_update_definition,
)

from ._write_fixtures import (
    ALLOWED_DRAFT_RECIPIENT,
    GRAPH_MAILBOX,
    GRAPH_MESSAGE_ID,
    SF_NOTE_ID,
    SF_RECORD_ID,
    graph_write_client,
    salesforce_write_client,
    sql_tickets_engine,
    sql_write_client,
    ticket_rows,
    write_guardrails,
)
from .conftest import (
    AutoApprovingGate,
    FakeClock,
    NamedApproverGate,
    RecordingAuditSink,
    approval_settings,
    make_pdp,
    make_principal,
)

_DENY = {
    "effect": "deny",
    "tier": 1,
    "reason": "denied for test",
    "obligations": {},
    "policy_version": "1.0.0",
}


def _in_order(haystack: list[str], needles: list[str]) -> bool:
    it = iter(haystack)
    return all(n in it for n in needles)


def _dedup() -> ActionDedupStore:
    engine = sa.create_engine("sqlite://")
    ensure_action_dedup_schema(engine)
    return ActionDedupStore(engine)


def _executor(
    definitions: list[Any], sink: Any, *, gate: Any, overrides: Any = None
) -> GovernedToolExecutor:
    dedup = _dedup()
    bindings = [build_action_binding(d, dedup=dedup) for d in definitions]
    return GovernedToolExecutor(
        bindings,
        pdp=make_pdp(sink, overrides=overrides),
        guardrails=write_guardrails(sink),
        approval_gate=gate,
        sink=sink,
    )


def _durable_gate(sink: Any, settings: Any, *, clock: Any = None):
    engine = sa.create_engine("sqlite://")
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    kwargs = {"clock": clock} if clock is not None else {}
    return store, DurableApprovalGate(store=store, sink=sink, settings=settings, **kwargs)


# --- model-facing schema hygiene (NE-ACTION-DETERMINISTIC-REQID) -----------------


def test_model_facing_schemas_do_not_advertise_request_id() -> None:
    """Models are never invited to mint a ``request_id``: the three reference
    ``input_schema`` dicts drop the field, while the guardrail allowlists still
    accept it (an explicit programmatic caller must still clear guardrails)."""
    ticket_client = sql_write_client(sql_tickets_engine())
    definitions = [
        (crm_note_definition(salesforce_write_client()), CRM_NOTE_ARG_POLICY),
        (email_draft_definition(graph_write_client()), EMAIL_DRAFT_ARG_POLICY),
        (ticket_update_definition(ticket_client), TICKET_UPDATE_ARG_POLICY),
    ]
    for definition, policy in definitions:
        assert "request_id" not in definition.spec.input_schema["properties"]
        assert "request_id" in policy.args


# --- (a) full governance for a CRM note (Tier-1, autonomous — NE-CS-SEND) --------


def test_crm_note_goes_through_full_governance() -> None:
    """The Tier-1 internal write runs the WHOLE spine minus the human gate.

    PDP decided, guardrails ran, the write executed and was audited — and NO
    approval event exists, because write.internal.* is autonomous + digest
    (owner decision 2026-08-27; the gate would only fire on a PDP escalation).
    """
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = crm_note_definition(salesforce_write_client(recorder=recorder))
    executor = _executor([definition], sink, gate=NamedApproverGate("alice"))

    result = json.loads(
        asyncio.run(
            executor.execute(
                "write.internal.crm.note",
                {
                    "request_id": "req-crmnote1",
                    "record_id": SF_RECORD_ID,
                    "title": "Follow-up",
                    "body": "Called the customer.",
                },
                principal=make_principal(),
            )
        )
    )

    assert result["source_ref"] == SF_NOTE_ID
    actions = sink.actions()
    assert "pdp.decide" in actions
    approvals = [e for e in sink.events if getattr(e, "action", "") == "tool.approval"]
    assert approvals == [], "a Tier-1 internal write must not consult the human gate"
    successes = [
        e
        for e in sink.events
        if getattr(e, "action", "") == "tool.execute" and e.outcome == "success"
    ]
    assert len(successes) == 1
    # The POST body carries exactly ParentId/Title/Body.
    posts = [r for r in recorder if r.url.path.endswith("/sobjects/Note")]
    assert len(posts) == 1
    assert json.loads(posts[0].content.decode()) == {
        "ParentId": SF_RECORD_ID,
        "Title": "Follow-up",
        "Body": "Called the customer.",
    }


# --- (b) Tier-3 ticket update blocks until two named approvals -------------------


def test_tier3_ticket_update_blocks_until_two_named_approvals() -> None:
    sink = RecordingAuditSink()
    engine = sql_tickets_engine()
    definition = ticket_update_definition(sql_write_client(engine))
    store, gate = _durable_gate(
        sink, approval_settings(tier3_named_approvers=("alice", "bob"), poll_interval_s=0.0)
    )
    executor = _executor([definition], sink, gate=gate)

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                "write.external.ticket.update",
                {
                    "request_id": "req-ticket01",
                    "ticket_id": 42,
                    "status": "resolved",
                    "public_reply": "Fixed.",
                },
                principal=make_principal(),
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        # Pending: the ticket row is UNCHANGED and no write happened.
        assert ticket_rows(engine) == [(42, "open", "")]
        rid = (await store.list_pending())[0].request_id
        # First named approval => pending_second; STILL unchanged.
        assert await gate.approve(rid, approver="alice") is None
        for _ in range(5):
            await asyncio.sleep(0)
        assert ticket_rows(engine) == [(42, "open", "")]
        still = await store.list_pending()
        assert still and still[0].tier == 3
        # Distinct second approval => the write lands.
        await gate.approve(still[0].request_id, approver="bob")
        await asyncio.wait_for(task, timeout=10)
        assert ticket_rows(engine) == [(42, "resolved", "Fixed.")]

    asyncio.run(scenario())

    assert _in_order(sink.actions(), ["approval.requested", "tool.approval", "tool.execute"])


# --- (c) a denied write never touches the source ---------------------------------


def test_denied_write_never_touches_source() -> None:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = crm_note_definition(salesforce_write_client(recorder=recorder))
    executor = _executor(
        [definition], sink, gate=NamedApproverGate(), overrides={"write.internal.crm.note": _DENY}
    )

    with pytest.raises(ToolDeniedError):
        asyncio.run(
            executor.execute(
                "write.internal.crm.note",
                {
                    "request_id": "req-denied01",
                    "record_id": SF_RECORD_ID,
                    "title": "t",
                    "body": "b",
                },
                principal=make_principal(),
            )
        )

    assert recorder == []  # not even a token POST — fail closed


# --- rejected / expired approvals do not write (fail-closed symmetry) ------------


def test_rejected_and_expired_approvals_do_not_write() -> None:
    """NE-CS-SEND: the fail-closed symmetry now lives on the Tier-2 SEND.

    (The CRM note is Tier-1 autonomous and never parks; the send is the action
    whose whole point is the human gate.) A rejected or expired approval must
    leave the recorder EMPTY — not even the recipient read-back runs.
    """

    def _send_definition(recorder: list[httpx.Request]):
        return email_send_definition(
            graph_write_client(recorder=recorder),
            allowed_mailboxes=(GRAPH_MAILBOX,),
            allowed_recipients=(ALLOWED_DRAFT_RECIPIENT,),
        )

    # Rejected.
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    store, gate = _durable_gate(sink, approval_settings(poll_interval_s=0.0))
    executor = _executor([_send_definition(recorder)], sink, gate=gate)

    async def reject_scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                "write.external.email.send",
                {
                    "request_id": "req-reject01",
                    "mailbox": GRAPH_MAILBOX,
                    "draft_ref": GRAPH_MESSAGE_ID,
                },
                principal=make_principal(),
            )
        )
        while not await store.list_pending():
            await asyncio.sleep(0)
        rid = (await store.list_pending())[0].request_id
        await gate.reject(rid, approver="alice", reason="no")
        with pytest.raises(ApprovalNotGrantedError):
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(reject_scenario())
    assert recorder == []

    # Expired.
    sink2 = RecordingAuditSink()
    recorder2: list[httpx.Request] = []
    clock = FakeClock()
    store2, gate2 = _durable_gate(
        sink2, approval_settings(expiry_s=1000.0, poll_interval_s=0.0), clock=clock
    )
    executor2 = _executor([_send_definition(recorder2)], sink2, gate=gate2)

    async def expiry_scenario() -> None:
        task = asyncio.create_task(
            executor2.execute(
                "write.external.email.send",
                {
                    "request_id": "req-expire01",
                    "mailbox": GRAPH_MAILBOX,
                    "draft_ref": GRAPH_MESSAGE_ID,
                },
                principal=make_principal(),
            )
        )
        while not await store2.list_pending():
            await asyncio.sleep(0)
        # ADR-0030: the policy window (tier-2 floor: 14400 s) governs the row,
        # not expiry_s=1000 — advance past the WINDOW to expire the park.
        clock.advance(16400.0)
        with pytest.raises(ApprovalNotGrantedError) as exc:
            await asyncio.wait_for(task, timeout=10)
        assert exc.value.status is ApprovalStatus.EXPIRED

    asyncio.run(expiry_scenario())
    assert recorder2 == []


# --- (e-part) email-draft recipient guardrail blocks BEFORE the PDP --------------


def test_email_draft_recipient_guardrail_blocks_before_pdp() -> None:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = email_draft_definition(graph_write_client(recorder=recorder))
    executor = _executor([definition], sink, gate=NamedApproverGate())

    with pytest.raises(GuardrailViolation):
        asyncio.run(
            executor.execute(
                "draft.email.create",
                {
                    "request_id": "req-evil0001",
                    "mailbox": GRAPH_MAILBOX,
                    "subject": "s",
                    "body": "b",
                    "to": "attacker@evil.example",
                },
                principal=make_principal(),
            )
        )
    assert sink.with_action("pdp.decide") == []  # guardrails run first
    assert recorder == []

    # An allowed recipient => the draft is created via the real GraphWriteClient.
    result = json.loads(
        asyncio.run(
            executor.execute(
                "draft.email.create",
                {
                    "request_id": "req-good0001",
                    "mailbox": GRAPH_MAILBOX,
                    "subject": "Hi",
                    "body": "Body",
                    "to": ALLOWED_DRAFT_RECIPIENT,
                },
                principal=make_principal(),
            )
        )
    )
    assert result["source_ref"] == GRAPH_MESSAGE_ID


# --- (f) the ladder: Tier-0 draft skips the gate; the Tier-2 send consults it ----


def test_draft_is_autonomous_and_send_consults_the_human_gate() -> None:
    """NE-CS-SEND replaced the old declared-tier-escalation exhibit.

    The draft now declares its own Rego class (Tier-0): the gate is NOT
    consulted and the unsent draft is created immediately. The send —
    write.external., class Tier-2, undeclarable lower — DOES consult the gate,
    and an approver-less APPROVED never auto-approves it (never-auto-approve is
    unweakened where the gate actually applies).
    """
    sink = RecordingAuditSink()
    named = NamedApproverGate("alice")
    definition = email_draft_definition(graph_write_client())
    executor = _executor([definition], sink, gate=named)
    args = {
        "request_id": "req-esc00001",
        "mailbox": GRAPH_MAILBOX,
        "subject": "s",
        "body": "b",
        "to": ALLOWED_DRAFT_RECIPIENT,
    }

    asyncio.run(executor.execute("draft.email.create", dict(args), principal=make_principal()))
    assert named.requests == []  # Tier-0: no human gate on the unsent draft

    send_args = {
        "request_id": "req-esc00002",
        "mailbox": GRAPH_MAILBOX,
        "draft_ref": GRAPH_MESSAGE_ID,
    }
    sink2 = RecordingAuditSink()
    named2 = NamedApproverGate("alice")
    executor2 = _executor(
        [
            email_send_definition(
                graph_write_client(),
                allowed_mailboxes=(GRAPH_MAILBOX,),
                allowed_recipients=(ALLOWED_DRAFT_RECIPIENT,),
            )
        ],
        sink2,
        gate=named2,
    )
    asyncio.run(
        executor2.execute("write.external.email.send", dict(send_args), principal=make_principal())
    )
    assert named2.requests  # the human gate WAS consulted for the real send

    # An approver-less APPROVED never auto-approves the send.
    sink3 = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    executor3 = _executor(
        [
            email_send_definition(
                graph_write_client(recorder=recorder),
                allowed_mailboxes=(GRAPH_MAILBOX,),
                allowed_recipients=(ALLOWED_DRAFT_RECIPIENT,),
            )
        ],
        sink3,
        gate=AutoApprovingGate(),
    )
    with pytest.raises(ApprovalIntegrityError):
        asyncio.run(
            executor3.execute(
                "write.external.email.send", dict(send_args), principal=make_principal()
            )
        )
    assert recorder == []  # never-auto-approve: zero transport activity


# --- (d) the write trail lands in the REAL tamper-evident chain ------------------


def test_write_trail_lands_in_real_tamper_evident_chain(frozen_config_hash: str) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_audit_schema(engine)
    sink = PostgresAuditSink(engine)
    definition = crm_note_definition(salesforce_write_client())
    executor = _executor([definition], sink, gate=NamedApproverGate("alice"))

    async def scenario() -> None:
        await executor.execute(
            "write.internal.crm.note",
            {"request_id": "req-chain001", "record_id": SF_RECORD_ID, "title": "t", "body": "b"},
            principal=make_principal(),
        )
        verification = await sink.verify()
        assert verification.valid is True

    asyncio.run(scenario())

    from ne.audit.store import ne_audit_log

    with engine.connect() as conn:
        rows = [dict(r.payload) for r in conn.execute(sa.select(ne_audit_log.c.payload)).all()]
    executes = [r for r in rows if r["action"] == "tool.execute" and r["outcome"] == "success"]
    assert len(executes) == 1
    assert executes[0]["config_hash"] == frozen_config_hash
    engine.dispose()


def test_contact_lookup_serves_email_and_account_and_audits_identifiers_only() -> None:
    """NE-CS-LOOKUP: the Tier-0 contact read that feeds the outreach draft.

    The handler serves the recorded email and parent Account id (the recipient
    and note target are READ, never prompt-supplied); a miss is reported, not
    raised; and the audit detail carries identifiers only — never the contact
    name or email (C12).
    """
    from ne.runtime.reference_actions import (
        CRM_CONTACT_LOOKUP_ARG_POLICY,
        crm_contact_lookup_binding,
    )

    class _Client:
        def __init__(self, record: dict[str, str] | None) -> None:
            self.record = record
            self.calls: list[str] = []

        async def find_contact_by_name(self, name: str) -> dict[str, str] | None:
            self.calls.append(name)
            return self.record

    hit = _Client(
        {
            "record_id": "003000000000001AAA",
            "email": "owner@example.com",
            "account_id": "001000000000001AAA",
        }
    )
    binding = crm_contact_lookup_binding(hit)
    assert binding.spec.name == "read.external.crm.contact"
    assert binding.spec.tier.value == 0

    async def _call(args: dict[str, Any]) -> str:
        return await binding.handler(args)

    out = json.loads(asyncio.run(_call({"name": "Avery Arbor"})))
    assert out == {
        "name": "Avery Arbor",
        "found": True,
        "record_id": "003000000000001AAA",
        "email": "owner@example.com",
        "account_id": "001000000000001AAA",
    }
    assert hit.calls == ["Avery Arbor"]

    assert binding.extract_audit_detail is not None
    detail = binding.extract_audit_detail({"name": "Avery Arbor"}, json.dumps(out))
    assert detail == {
        "action_kind": "read",
        "source_system": "salesforce",
        "sobject": "Contact",
        "found": True,
        "target": "003000000000001AAA",
    }
    assert "Avery" not in json.dumps(dict(detail))
    assert "owner@example.com" not in json.dumps(dict(detail))

    miss = _Client(None)
    miss_binding = crm_contact_lookup_binding(miss)

    async def _call_miss(args: dict[str, Any]) -> str:
        return await miss_binding.handler(args)

    out = json.loads(asyncio.run(_call_miss({"name": "Nobody"})))
    assert out == {"name": "Nobody", "found": False}

    with pytest.raises(ValueError, match="name must not be empty"):
        asyncio.run(_call({"name": "   "}))

    # the allowlisted argument shape admits exactly one text arg
    assert set(CRM_CONTACT_LOOKUP_ARG_POLICY.args) == {"name"}


# --- NE-CS-USABLE-ADDRESS: mailbox defaulting (deployment config, not model text) --


def test_draft_omitted_mailbox_uses_the_deployment_default() -> None:
    """NE-162 masks mailbox addresses out of frontier-visible text, so the model
    cannot reliably quote one back; an omitted ``mailbox`` uses the composition
    root's sole configured allowlisted mailbox."""
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = email_draft_definition(
        graph_write_client(recorder=recorder), default_mailbox=GRAPH_MAILBOX
    )
    executor = _executor([definition], sink, gate=NamedApproverGate("alice"))
    result = asyncio.run(
        executor.execute(
            "draft.email.create",
            {
                "request_id": "req-mbxdflt01",
                "subject": "s",
                "body": "b",
                "to": ALLOWED_DRAFT_RECIPIENT,
            },
            principal=make_principal(),
        )
    )
    parsed = json.loads(result)
    assert parsed["succeeded"] is True
    encoded_mailbox = GRAPH_MAILBOX.replace("@", "%40")
    assert any(encoded_mailbox in str(r.url) for r in recorder)


def test_draft_omitted_mailbox_with_no_default_fails_closed() -> None:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    definition = email_draft_definition(graph_write_client(recorder=recorder))
    executor = _executor([definition], sink, gate=NamedApproverGate("alice"))
    from ne.runtime.errors import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        asyncio.run(
            executor.execute(
                "draft.email.create",
                {
                    "request_id": "req-mbxdflt02",
                    "subject": "s",
                    "body": "b",
                    "to": ALLOWED_DRAFT_RECIPIENT,
                },
                principal=make_principal(),
            )
        )
    assert recorder == []  # nothing reached the connector — fail closed


def test_send_omitted_mailbox_uses_the_deployment_default() -> None:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    executor = _executor(
        [
            email_send_definition(
                graph_write_client(recorder=recorder),
                allowed_mailboxes=(GRAPH_MAILBOX,),
                allowed_recipients=(ALLOWED_DRAFT_RECIPIENT,),
                default_mailbox=GRAPH_MAILBOX,
            )
        ],
        sink,
        gate=NamedApproverGate("alice"),
    )
    result = asyncio.run(
        executor.execute(
            "write.external.email.send",
            {"request_id": "req-mbxdflt03", "draft_ref": GRAPH_MESSAGE_ID},
            principal=make_principal(),
        )
    )
    parsed = json.loads(result)
    assert parsed["succeeded"] is True
    encoded_mailbox = GRAPH_MAILBOX.replace("@", "%40")
    assert any(encoded_mailbox in str(r.url) for r in recorder)
