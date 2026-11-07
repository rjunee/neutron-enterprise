# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable approval steps — passthrough (off) + fake-DBOS decoration (dbos)."""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
import sqlalchemy as sa

import ne.runtime.approvals_durable as ad
import ne.runtime.durable as durable
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.events import new_approval_event
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    ensure_approval_schema,
    ne_approval_evidence,
    ne_approval_requests,
)
from ne.runtime.approvals import DurableApprovalGate
from ne.runtime.errors import ApprovalAlreadyDecidedError
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_checkpoint import (
    RunApprovalCheckpointStore,
    ne_agent_run_approval_resume_intents,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_store import AgentRunStore, RunState, ensure_run_schema, ne_agent_runs
from ne.runtime.run_workflow import (
    RunWorkflowStateStore,
    ensure_run_workflow_schema,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.schema import ApprovalDecision

from .conftest import RecordingAuditSink, approval_settings


def _url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'durable.db'}"


def _request_json() -> str:
    return json.dumps(
        {
            "action": "write.external.email.send",
            "tier": 2,
            "principal_id": "user:ada",
            "payload": {"tool": "write.external.email.send"},
            "sla_s": 100.0,
            "expiry_s": 1000.0,
        }
    )


def _source_stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a", site_id="", drive_id="drive-a", item_id="item-a"
        ),
        generation=3,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="a" * 64,
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
        source_revision="etag-a",
        source_contract=contract,
        contract_epoch=4,
        acl_snapshot_hash=artifact.acl_snapshot_hash,
        state=SourceAuthorityState.PRESENT,
        admitted_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        authority_as_of=datetime(2026, 8, 21, 11, 59, tzinfo=UTC),
        audit_event_id="audit-a",
        lineage_id="lineage-a",
        freshness_sla_seconds=Decimal("900"),
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


def test_step_and_workflow_decorators_passthrough_when_off(tmp_path: Path) -> None:
    url = _url(tmp_path)
    request_id = ad.create_approval_request_step(url, _request_json())
    assert isinstance(request_id, str)
    assert ad.poll_approval_step(url, request_id) == "pending"

    # Approve directly against the store row (named approver to satisfy the CHECK).
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == request_id)
                .values(status="approved", first_approver="alice", decided_at=datetime.now(UTC))
            )
    finally:
        engine.dispose()

    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert ad.await_approval_workflow(url, request_id, 0.0, deadline) == "approved"


class _FakeDBOS:
    workflow_names: ClassVar[list[str | None]] = []
    step_names: ClassVar[list[str | None]] = []

    @classmethod
    def workflow(cls, *, name: str | None = None):
        def deco(fn):
            cls.workflow_names.append(name)
            return fn

        return deco

    @classmethod
    def step(cls, *, name: str | None = None):
        def deco(fn):
            cls.step_names.append(name)
            return fn

        return deco


def test_decorators_applied_under_fake_dbos(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDBOS.workflow_names.clear()
    _FakeDBOS.step_names.clear()
    monkeypatch.setenv("NE_RUNTIME_DURABILITY", "dbos")
    monkeypatch.setattr(durable, "_load_dbos", lambda: SimpleNamespace(DBOS=_FakeDBOS))
    try:
        importlib.reload(ad)
        assert _FakeDBOS.step_names == ["approval.create", "approval.poll", "approval.expire"]
        assert _FakeDBOS.workflow_names == ["approval.await"]
    finally:
        # Restore the module to clean passthrough decoration for other tests.
        monkeypatch.delenv("NE_RUNTIME_DURABILITY", raising=False)
        importlib.reload(ad)


def test_workflow_respects_deadline(tmp_path: Path) -> None:
    url = _url(tmp_path)
    request_id = ad.create_approval_request_step(url, _request_json())
    # A deadline already in the past → the loop returns 'expired' without hanging.
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert ad.await_approval_workflow(url, request_id, 0.0, past) == "expired"


def test_workflow_expiry_persists_expired_and_blocks_grant(tmp_path: Path) -> None:
    """Fix-#2 regression: deadline expiry DURABLY persists 'expired' before returning.

    The workflow no longer reports 'expired' while leaving the row 'pending'; the
    persisted terminal row is non-grantable and disappears from the pending queue.
    """
    url = _url(tmp_path)
    request_id = ad.create_approval_request_step(url, _request_json())
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert ad.await_approval_workflow(url, request_id, 0.0, past) == "expired"

    # PERSISTED: poll + direct row check both show the terminal 'expired' state.
    assert ad.poll_approval_step(url, request_id) == "expired"
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_approval_requests).where(
                    ne_approval_requests.c.request_id == request_id
                )
            ).first()
            assert row is not None
            assert row.status == "expired"
            assert row.decided_at is not None
            approval_events = conn.execute(
                sa.text("SELECT event_type FROM ne_outbox_events WHERE event_type = 'approval'")
            ).all()
            evidence = conn.execute(
                sa.select(ne_approval_evidence).where(
                    ne_approval_evidence.c.request_id == request_id
                )
            ).all()
        # create_approval_request_step writes NO event → exactly 1 expired event, 1 row.
        assert len(approval_events) == 1
        assert len(evidence) == 1
        assert evidence[0].outcome == "expired"
        assert evidence[0].clean is False
        assert evidence[0].approver is None
    finally:
        engine.dispose()

    # The persisted 'expired' row is non-actionable through the real gate.
    store = SqlAlchemyApprovalStore(sa.create_engine(url))
    gate = DurableApprovalGate(store=store, sink=RecordingAuditSink(), settings=approval_settings())
    try:
        with pytest.raises(ApprovalAlreadyDecidedError):
            asyncio.run(gate.approve(request_id, approver="alice"))
        assert asyncio.run(gate.list_pending()) == []
    finally:
        store.engine.dispose()


def test_expire_step_is_noop_when_already_terminal(tmp_path: Path) -> None:
    """CAS-lost branch: expire_approval_step can never clobber a landed decision."""
    url = _url(tmp_path)
    request_id = ad.create_approval_request_step(url, _request_json())

    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                ne_approval_requests.update()
                .where(ne_approval_requests.c.request_id == request_id)
                .values(status="approved", first_approver="alice", decided_at=datetime.now(UTC))
            )
    finally:
        engine.dispose()

    assert ad.expire_approval_step(url, request_id) == "approved"

    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == request_id
                )
            ).first()
            assert row is not None and row.status == "approved"
            approval_events = conn.execute(
                sa.text("SELECT event_type FROM ne_outbox_events WHERE event_type = 'approval'")
            ).all()
            evidence = conn.execute(
                sa.select(ne_approval_evidence).where(
                    ne_approval_evidence.c.request_id == request_id
                )
            ).all()
        # No expiry side effects: create wrote nothing, the no-op wrote nothing.
        assert len(approval_events) == 0
        assert len(evidence) == 0
    finally:
        engine.dispose()


def test_run_bound_expiry_atomically_checkpoints_and_authors_one_resume_intent(
    tmp_path: Path,
) -> None:
    """A crash/replay cannot strand the canonical Run at an unjoined expiry."""

    url = _url(tmp_path)
    engine = sa.create_engine(url)
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    ensure_approval_schema(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    sessions = SqlAlchemySessionStore(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)

    async def park() -> str:
        conversation = await sessions.create_conversation("agent-a", "finance", "alice")
        context = AuthenticatedRequestContext(
            principal_id="alice",
            authorization_groups=(),
            department="finance",
            issuer="https://issuer.example.test",
            expires_at=4_102_444_800,
        )
        accepted = await runs.admit_start(
            SimpleNamespace(
                idempotency_key=f"start-{uuid.uuid4()}",
                request_hash=uuid.uuid4().hex * 2,
                expected_version=0,
                input_plaintext=b"protected prompt",
                conversation_id=conversation.conversation_id,
                owner_principal_id="alice",
                department="finance",
                conversation_agent_name="agent-a",
                agent_id="agent-a",
                agent_version=1,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id="snapshot-receipt",
                authenticated_context=context,
            )
        )
        RunWorkflowStateStore(engine, run_store=runs).claim(
            dispatch_id=accepted.enqueue_id,
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="1" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id="run-tool",
            tool_call_id="run-tool:tool:0",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"subject":"redacted"}',
            source_stamps=(_source_stamp(),),
            stamp_receipt_id="source-result-1",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside-transaction",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        return joined.approval_id

    request_id = asyncio.run(park())
    engine.dispose()

    assert ad.expire_approval_step(url, request_id) == "expired"
    # Restart/replay is a no-op over the exact same decision and resume identity.
    assert ad.expire_approval_step(url, request_id) == "expired"

    restarted = sa.create_engine(url)
    try:
        with restarted.connect() as conn:
            checkpoint = conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status)
            ).scalar_one()
            intent = conn.execute(sa.select(ne_agent_run_approval_resume_intents)).one()
            run_status = conn.execute(sa.select(ne_agent_runs.c.status)).scalar_one()
            evidence_count = conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_approval_evidence)
                .where(ne_approval_evidence.c.request_id == request_id)
            ).scalar_one()
        assert checkpoint == "expired"
        assert intent.approval_id == request_id
        assert intent.decision == "expired"
        assert run_status == RunState.WAITING_APPROVAL.value
        assert evidence_count == 1
    finally:
        restarted.dispose()
