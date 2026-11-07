# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Restart survival: a pending request written by one engine resolves through a fresh one."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from ne.contracts.governance import ApprovalRequest, ApprovalStatus
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    GovernedLocalT3SourcePoint,
    ProtectedSourceInvocationRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundApprovalResolution,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.approval_store import (
    SOURCE_WITHHELD_STATUS,
    SqlAlchemyApprovalStore,
    ensure_approval_schema,
    ne_approval_requests,
)
from ne.runtime.approvals import (
    PROTECTED_INVOCATION_KEY,
    SOURCE_WITHHELD_REASON,
    DurableApprovalGate,
    canonical_args_hash,
)
from ne.runtime.approvals_durable import await_approval_workflow
from ne.runtime.correlation import (
    ConversationAttemptBinding,
    action_scope,
    conversation_attempt_scope,
)
from ne.runtime.errors import (
    ApprovalRequestNotFoundError,
    ApprovalSourceWithheldFromApproverError,
)
from ne.runtime.source_results import (
    ProtectedSourceResultGuard,
    SourceAuthorityWithheldError,
    authenticated_request_scope,
    encode_protected_reference,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    conversation_request_fingerprint,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
)

from .conftest import (
    ALLOWED_RECIPIENT,
    RecordingAuditSink,
    ToolLog,
    approval_request,
    approval_settings,
    make_executor,
    make_principal,
    standard_bindings,
)


def _fresh_gate(url: str) -> tuple[DurableApprovalGate, SqlAlchemyApprovalStore, sa.Engine]:
    """A brand-new engine + store + gate over ``url`` (simulates a process restart)."""
    engine = sa.create_engine(url)
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(store=store, sink=RecordingAuditSink(), settings=approval_settings())
    return gate, store, engine


def test_pending_request_survives_restart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'approvals.db'}"

    async def scenario() -> None:
        gate_a, _store_a, engine_a = _fresh_gate(url)
        rid = await gate_a.submit(approval_request("write.external.email.send", 2))
        engine_a.dispose()  # the "restart" — no in-memory state carries over

        gate_b, store_b, engine_b = _fresh_gate(url)
        try:
            pending = await store_b.list_pending()
            assert [p.request_id for p in pending] == [rid]
            assert pending[0].tier == 2
            outcome = await gate_b.approve(rid, approver="carol")
            assert outcome is not None and outcome.status is ApprovalStatus.APPROVED
            final = await asyncio.wait_for(gate_b.await_outcome(rid), timeout=10)
            assert final.status is ApprovalStatus.APPROVED
        finally:
            engine_b.dispose()

    asyncio.run(scenario())


def test_terminal_state_survives_restart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'approvals.db'}"

    async def scenario() -> None:
        gate_a, _store_a, engine_a = _fresh_gate(url)
        rid = await gate_a.submit(approval_request("write.external.email.send", 2))
        await gate_a.approve(rid, approver="carol")
        engine_a.dispose()

        gate_b, _store_b, engine_b = _fresh_gate(url)
        try:
            # A decided row reloads as terminal — await returns immediately.
            final = await asyncio.wait_for(gate_b.await_outcome(rid), timeout=10)
            assert final.status is ApprovalStatus.APPROVED
            assert final.approver == "carol"
        finally:
            engine_b.dispose()

    asyncio.run(scenario())


def test_restart_takeover_fences_old_attempt_approval_and_mints_fresh_request(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'attempt-approval.db'}"
    request = approval_request("write.external.email.send", 2)

    async def scenario() -> None:
        gate_a, _approval_store_a, engine_a = _fresh_gate(url)
        ensure_runtime_schema(engine_a)
        sessions_a = SqlAlchemySessionStore(engine_a)
        conversation = await sessions_a.create_conversation(
            "console-chat", "engineering", request.principal.id
        )
        turn_id = "turn-approval-restart-takeover"
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
        first = await sessions_a.claim_conversation_attachment(
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
            old_request = await gate_a.submit(request, args_hash="a" * 64)
        engine_a.dispose()

        gate_b, _approval_store_b, engine_b = _fresh_gate(url)
        ensure_runtime_schema(engine_b)
        sessions_b = SqlAlchemySessionStore(engine_b)
        try:
            with engine_b.begin() as conn:
                conn.execute(
                    ne_agent_conversation_turns.update()
                    .where(ne_agent_conversation_turns.c.attachment_id == turn_id)
                    .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            second = await sessions_b.claim_conversation_attachment(
                conversation.conversation_id,
                request.principal.id,
                "engineering",
                "console-chat",
                turn_id,
                fingerprint,
                trace_id,
            )
            assert second.attempt_id is not None and second.attempt_id != first.attempt_id
            with pytest.raises(ApprovalRequestNotFoundError):
                await gate_b.approve(old_request, approver="carol")
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
                new_request = await gate_b.submit(request, args_hash="a" * 64)
            assert new_request != old_request
            approved = await gate_b.approve(new_request, approver="carol")
            assert approved is not None and approved.status is ApprovalStatus.APPROVED
        finally:
            engine_b.dispose()

    asyncio.run(scenario())


# --- PRE-015A0-ENFORCEMENT: terminal drift survives, and never executes -------


_EXPIRES_AT = 1_800_000_000
_BEFORE_EXPIRY = datetime.fromtimestamp(_EXPIRES_AT - 1, tz=UTC)
_EMAIL_ARGS = {"to": ALLOWED_RECIPIENT, "body": "wire confirmation"}


def _stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="governed_local_t3",
        connector_instance_id="connector-a",
        point=GovernedLocalT3SourcePoint(dataset_name="meridian", contributor_key="payables"),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="governed_local_t3", descriptor_schema_version=1, descriptor_hash="a" * 64
    )
    artifact = SourceContentArtifactIdentity(
        source_identity=source,
        source_snapshot_hash="b" * 64,
        content_snapshot_hash="c" * 64,
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        artifact_schema_version=1,
    )
    receipt = CurrentSourceAuthorityReceipt(
        receipt_id="receipt-a",
        source_identity=source,
        artifact_identity=artifact,
        source_revision="rev-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=artifact,
        selected_head_revision=receipt.source_revision,
        acl_snapshot_hash=receipt.acl_snapshot_hash,
        source_contract=contract,
        contract_epoch=receipt.contract_epoch,
        authority_receipt_id=receipt.receipt_id,
        authority_receipt_hash=receipt.receipt_hash,
    )


class _WithholdingResolver:
    """A ``SourceBoundResultResolver`` that permanently denies every approval."""

    async def for_approval(
        self,
        reference: Any,
        *,
        principal_id: str,
        access_context_hash: str,
        approval_id: str,
        decision_attempt_id: str,
    ) -> SourceBoundApprovalResolution:
        return SourceBoundApprovalResolution(
            decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
            safe_reason="safe",
            approval_id=approval_id,
            decision_attempt_id=decision_attempt_id,
        )

    async def for_model_send(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_direct_result(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_dispatch(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def for_workflow_projection(self, reference: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _withholding_guard() -> ProtectedSourceResultGuard:
    return ProtectedSourceResultGuard(_WithholdingResolver(), clock=lambda: _BEFORE_EXPIRY)


def _context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id="user:ada",
        authorization_groups=("group:staff",),
        issuer="https://oidc.test/realms/ne",
        expires_at=_EXPIRES_AT,
    )


def _protected_email_request() -> ApprovalRequest:
    reference = ProtectedSourceInvocationRef(
        reference_id="invocation-1", stamps=(_stamp(),), invocation_hash="e" * 64
    )
    return approval_request(
        "write.external.email.send",
        2,
        payload={
            "tool": "write.external.email.send",
            "disclosure": {
                "target_resource": "tool:write.external.email.send",
                "description": "Send an external email.",
                PROTECTED_INVOCATION_KEY: encode_protected_reference(reference),
            },
        },
    )


async def _seed_source_withheld(store: SqlAlchemyApprovalStore, rid: str) -> None:
    """Land the GENUINE authority terminal through the store's own CAS.

    A reading/deciding approver's denial no longer authors ``source_withheld``
    (U2 adversarial review, standalone-gate ruling — it is that principal's
    non-persisting refusal); the terminal is the shape a genuine owner-context
    authority decision drives, so these restart/waiter tests seed it directly.
    """
    landed = await store.mark_source_withheld(
        rid,
        now=datetime.now(UTC),
        reason=SOURCE_WITHHELD_REASON,
        evidence_row={
            "request_id": rid,
            "action": "write.external.email.send",
            "tier": 2,
            "outcome": SOURCE_WITHHELD_STATUS,
            "approver": None,
            "decided_at": datetime.now(UTC),
            "clean": False,
        },
    )
    assert landed is True


def test_source_withheld_terminal_state_survives_restart_and_still_refuses(
    tmp_path: Path,
) -> None:
    """A restarted process re-reads the drift fact itself — not 'expired', not pending."""
    url = f"sqlite:///{tmp_path / 'approvals.db'}"

    async def scenario() -> None:
        gate, store, engine = _fresh_gate(url)
        gate = DurableApprovalGate(
            store=store,
            sink=RecordingAuditSink(),
            settings=approval_settings(),
            source_guard=_withholding_guard(),
        )
        try:
            rid = await gate.submit(_protected_email_request())
            # The denied approver's attempt refuses WITHOUT terminalizing (U2
            # ruling) — the row stays pending until the genuine terminal lands.
            with (
                authenticated_request_scope(_context()),
                pytest.raises(ApprovalSourceWithheldFromApproverError),
            ):
                await gate.approve(rid, approver="alice")
            row = await store.get(rid)
            assert row is not None and row.status == "pending"
            await _seed_source_withheld(store, rid)
        finally:
            engine.dispose()

        # A brand-new engine/store/gate — the restart.
        fresh_gate, fresh_store, fresh_engine = _fresh_gate(url)
        try:
            row = await fresh_store.get(rid)
            assert row is not None
            assert row.status == SOURCE_WITHHELD_STATUS
            assert row.decided_reason == SOURCE_WITHHELD_REASON
            with pytest.raises(SourceAuthorityWithheldError):
                await fresh_gate.await_outcome(rid)
            # Positive control: the SAME restarted gate resolves an ordinary
            # approved request normally, so the refusal above is the drift state
            # and not a restart that lost the ability to decide anything.
            other = await fresh_gate.submit(approval_request("write.external.email.send", 2))
            await fresh_gate.approve(other, approver="alice")
            outcome = await fresh_gate.await_outcome(other)
            assert outcome.status is ApprovalStatus.APPROVED
        finally:
            fresh_engine.dispose()

    asyncio.run(scenario())


def test_a_drifted_approval_never_reaches_the_governed_tool_handler(tmp_path: Path) -> None:
    """Call count on the REAL bound handler — the outbound seam, not an observed effect.

    The executor adopts the already-pending PROTECTED row (asserted: exactly one
    approval row exists), blocks on it, and the drift lands mid-wait. The handler
    must be invoked zero times, and the request must be terminal for the drift
    reason — a handler that stayed idle because the executor crashed some other way
    would not satisfy both assertions.
    """
    url = f"sqlite:///{tmp_path / 'approvals.db'}"
    engine = sa.create_engine(url)
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    log = ToolLog()
    sink = RecordingAuditSink()
    gate = DurableApprovalGate(
        store=store,
        sink=sink,
        settings=approval_settings(),
        source_guard=_withholding_guard(),
    )
    executor = make_executor(standard_bindings(log), sink, approval_gate=gate)

    async def scenario() -> None:
        with action_scope("attachment-1"):
            rid = await gate.submit(
                _protected_email_request(), args_hash=canonical_args_hash(_EMAIL_ARGS)
            )
            task = asyncio.create_task(
                executor.execute(
                    "write.external.email.send", _EMAIL_ARGS, principal=make_principal()
                )
            )
            await asyncio.sleep(0)
            # The denied approver's attempt is a non-persisting refusal — the
            # executor keeps waiting on a still-pending row (U2 ruling).
            with (
                authenticated_request_scope(_context()),
                pytest.raises(ApprovalSourceWithheldFromApproverError),
            ):
                await gate.approve(rid, approver="alice")
            pending_row = await store.get(rid)
            assert pending_row is not None and pending_row.status == "pending"
            # The GENUINE authority terminal lands mid-wait via the store CAS.
            await _seed_source_withheld(store, rid)
            with pytest.raises(BaseException) as exc_info:
                await task

        # THE assertion: the outbound seam was never called.
        assert log.calls == []
        # The executor adopted the protected row rather than minting its own.
        with engine.begin() as conn:
            ids = conn.execute(sa.select(ne_approval_requests.c.request_id)).scalars().all()
        assert ids == [rid]
        row = await store.get(rid)
        assert row is not None
        assert row.status == SOURCE_WITHHELD_STATUS
        assert row.decided_reason == SOURCE_WITHHELD_REASON
        # The executor reached the APPROVAL await: guardrails and the PDP both
        # passed, and the only reason the handler stayed idle is the drift. Without
        # this, a guardrail or policy block earlier in the chain would satisfy the
        # call-count assertion above while proving nothing about the terminal CAS.
        actions = [e.action for e in sink.events]
        assert "guardrail.block" not in actions
        assert "pdp.decide" in actions
        # ``approval.reattached`` is the executor ADOPTING the pre-seeded protected
        # row; without it the executor would have been waiting on a source-free row
        # of its own and the drift would be irrelevant to it.
        assert "approval.reattached" in actions
        assert isinstance(exc_info.value, SourceAuthorityWithheldError)

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_durable_wait_reports_source_withheld_as_itself_and_never_as_expired(
    tmp_path: Path,
) -> None:
    """The durable poll loop stops on the drift fact instead of relabelling it.

    A terminal set hardcoded as ('approved','rejected','expired') would poll a
    withheld row to its deadline and then report 'expired' — a deadline it never
    reached. The expiry branch is exercised separately below as the positive control.
    """
    url = f"sqlite:///{tmp_path / 'approvals.db'}"
    engine = sa.create_engine(url)
    ensure_approval_schema(engine)
    store = SqlAlchemyApprovalStore(engine)
    gate = DurableApprovalGate(
        store=store,
        sink=RecordingAuditSink(),
        settings=approval_settings(),
        source_guard=_withholding_guard(),
    )

    async def scenario() -> str:
        rid = await gate.submit(_protected_email_request())
        # The denied approver refuses without terminalizing (U2 ruling); the
        # genuine authority terminal then lands through the store's own CAS.
        with (
            authenticated_request_scope(_context()),
            pytest.raises(ApprovalSourceWithheldFromApproverError),
        ):
            await gate.approve(rid, approver="alice")
        await _seed_source_withheld(store, rid)
        return rid

    try:
        request_id = asyncio.run(scenario())
        far_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        assert await_approval_workflow(url, request_id, 0.01, far_future) == SOURCE_WITHHELD_STATUS

        # Positive control: an ordinary undecided request still expires at its
        # deadline, so the assertion above is the drift state and not a poll loop
        # that stopped reporting 'expired' altogether.
        ordinary = asyncio.run(gate.submit(approval_request("write.external.email.send", 2)))
        assert (
            await_approval_workflow(url, ordinary, 0.01, datetime.now(UTC).isoformat()) == "expired"
        )
    finally:
        engine.dispose()
