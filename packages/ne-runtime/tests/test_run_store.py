# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Insert

from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    EvidenceAssurance,
    M365SourcePoint,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceBoundToolResultRef,
    SourceBoundToolResultSetRef,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
    canonical_source_authority_stamps,
)
from ne.events import new_approval_event
from ne.runtime.approval_store import (
    SqlAlchemyApprovalStore,
    ensure_approval_schema,
    ne_approval_requests,
)
from ne.runtime.approvals_durable import expire_approval_step
from ne.runtime.model_continuity import (
    CONTINUITY_PROMPT_RESERVE_TOKENS,
    ne_agent_model_boundary_receipts,
)
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CONVERSATION_TURN_TARGET_KIND,
    LEGACY_TARGET_KINDS,
    SESSION_MESSAGE_TARGET_KIND,
    CanonicalTargetTuple,
    PrivatePayloadDomain,
    ProtectedContentStatus,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
    conversation_turn_content_binding,
    is_claimable_target_kind,
    protected_target_hash,
    session_message_content_binding,
)
from ne.runtime.run_checkpoint import (
    RunApprovalCheckpointStore,
    ne_agent_run_approval_audit_outbox,
    ne_agent_run_approval_joins,
    ne_agent_run_approval_resume_intents,
    ne_agent_run_continuation_checkpoints,
)
from ne.runtime.run_claim_verifier import (
    ClaimSlice,
    DirectSearchEvidenceObservation,
    VerifiedRowSetEvidenceObservation,
    VerifiedRowSetMemberObservation,
    verify_claim,
)
from ne.runtime.run_finalizer import published_failure_reason
from ne.runtime.run_store import (
    LEGACY_TRANSPORT_GENERATION,
    OWNERLESS_RETIREMENT_REASON,
    AgentRunStore,
    ClaimDeclaration,
    ClaimEvidenceDeclaration,
    EvidenceContributorDeclaration,
    EvidenceDeclaration,
    EvidenceRowDeclaration,
    EvidenceSetDeclaration,
    ProtectedSliceUnavailable,
    ProtectedTurnUnavailable,
    RunAuditDecisionKind,
    RunClaimEvidenceObservation,
    RunClaimEvidenceSetObservation,
    RunClaimEvidenceUnavailable,
    RunOutputVerdict,
    RunState,
    RunSubmissionConflictError,
    RunSubmissionNotFoundError,
    _pack_source_bound_payload,
    ensure_run_schema,
    ne_agent_delivery_receipts,
    ne_agent_evidence_locators,
    ne_agent_evidence_records,
    ne_agent_evidence_sets,
    ne_agent_pending_source_tool_results,
    ne_agent_protected_source_invocations,
    ne_agent_protected_source_outputs,
    ne_agent_protected_turn_contents,
    ne_agent_protected_workflow_states,
    ne_agent_run_attempt_links,
    ne_agent_run_audit_receipts,
    ne_agent_run_claims,
    ne_agent_run_enqueue_outbox,
    ne_agent_run_events,
    ne_agent_run_finalizations,
    ne_agent_run_inputs,
    ne_agent_run_retry_lineage,
    ne_agent_run_submissions,
    ne_agent_runs,
)
from ne.runtime.run_workflow import (
    RunWorkflowStateStore,
    _append_safe_event,
    commit_run_terminal_in_transaction,
    ensure_run_workflow_schema,
    ne_agent_run_lifecycle_outbox,
    ne_agent_run_workflow_claims,
)
from ne.runtime.source_results import (
    MalformedProtectedReferenceError,
    ProtectedSourceResultGuard,
    RunStoreProtectedSourceOutputWriter,
    RunStoreSourceBoundResultResolver,
    authenticated_request_scope,
    complete_run_delivery_observation,
    direct_publication_scope,
    encode_protected_reference,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
    ne_agent_conversations,
    ne_agent_session_messages,
)
from ne.schema import ApprovalDecision


def test_whole_turn_materialization_requires_exact_source_free_target() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        claimed = await _claim_turn_content(runs, target, plaintext=b"protected prompt")
        assert (
            await runs.materialize_source_free_turn(
                owner_principal_id="alice",
                target=target,
                protected_ref=claimed.row_id,
                plaintext_hash=claimed.plaintext_hash,
            )
            == b"protected prompt"
        )
        base = {
            "owner_principal_id": "alice",
            "target": target,
            "protected_ref": claimed.row_id,
            "plaintext_hash": claimed.plaintext_hash,
        }
        for changed in (
            {"owner_principal_id": "foreign"},
            {"protected_ref": "random"},
            {"plaintext_hash": "f" * 64},
            {"target": _target(conversation.conversation_id, run.run_id, "other")},
        ):
            with pytest.raises(ProtectedTurnUnavailable, match=r"^protected turn unavailable$"):
                await runs.materialize_source_free_turn(**{**base, **changed})  # type: ignore[arg-type]
        bound_target = _target(
            conversation.conversation_id, run.run_id, attempt.attempt_id, turn_id="bound"
        )
        bound = await _claim_turn_content(runs, bound_target, source_stamps=(_stamp(),))
        with pytest.raises(ProtectedTurnUnavailable, match=r"^protected turn unavailable$"):
            await runs.materialize_source_free_turn(
                owner_principal_id="alice",
                target=bound_target,
                protected_ref=bound.row_id,
                plaintext_hash=bound.plaintext_hash,
            )

    asyncio.run(scenario())
    engine.dispose()


def test_legacy_conversation_materialization_is_exact_and_source_free_only() -> None:
    engine, _, runs = _stores()

    async def scenario() -> None:
        binding = conversation_turn_content_binding(
            conversation_id="conversation-1",
            attachment_id="attachment-1",
            attempt_id="attempt-1",
            slot="input",
        )
        claimed = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id="alice",
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            target_binding=binding,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=b"protected prompt",
        )
        base = {
            "owner_principal_id": "alice",
            "conversation_id": "conversation-1",
            "attachment_id": "attachment-1",
            "attempt_id": "attempt-1",
            "slot": "input",
            "protected_ref": claimed.row_id,
            "plaintext_hash": claimed.plaintext_hash,
        }
        assert await runs.materialize_legacy_conversation_turn(**base) == b"protected prompt"  # type: ignore[arg-type]
        for changed in (
            {"owner_principal_id": "foreign"},
            {"conversation_id": "other"},
            {"attachment_id": "other"},
            {"attempt_id": "other"},
            {"slot": "terminal"},
            {"protected_ref": "random"},
            {"plaintext_hash": "f" * 64},
        ):
            with pytest.raises(ProtectedTurnUnavailable, match=r"^protected turn unavailable$"):
                await runs.materialize_legacy_conversation_turn(**{**base, **changed})  # type: ignore[arg-type]

    asyncio.run(scenario())
    engine.dispose()


def _stamp() -> SourceAuthorityStamp:
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
        source_kind="m365-sharepoint", descriptor_schema_version=1, descriptor_hash="a" * 64
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


def _stores() -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore]:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32, "k2": b"z" * 32})
    )
    return engine, SqlAlchemySessionStore(engine), AgentRunStore(engine, cipher)


async def _bind_approval_finalizer_proof(
    engine: sa.Engine,
    runs: AgentRunStore,
    *,
    run_id: str,
    attempt_id: str,
    claim_event_id: str,
    approval_id: str,
    config_hash: str,
    tag: str,
    record_approval: bool = True,
) -> str:
    """Seed the server-authored target and exact execution/approval audit receipts."""

    with engine.begin() as conn:
        approval_event_id = _append_safe_event(
            conn,
            run_id=run_id,
            attempt_id=attempt_id,
            event_kind="run.approval.audit-observed",
            event_status="observed",
            safe_detail={"approval_id": approval_id},
            step_id="run-approval",
        )
    await runs.record_audit_receipt(
        run_id=run_id,
        attempt_id=attempt_id,
        run_event_id=claim_event_id,
        decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        audit_event_id=f"audit-execution-{tag}",
        audit_sequence=1,
        audit_record_hash=hashlib.sha256(f"audit-execution-{tag}".encode()).hexdigest(),
        config_hash=config_hash,
        owner_principal_id="alice",
        step_id="run-execution",
    )
    if record_approval:
        await runs.record_audit_receipt(
            run_id=run_id,
            attempt_id=attempt_id,
            run_event_id=approval_event_id,
            decision_kind=RunAuditDecisionKind.APPROVAL,
            audit_event_id=f"audit-approval-{tag}",
            audit_sequence=2,
            audit_record_hash=hashlib.sha256(f"audit-approval-{tag}".encode()).hexdigest(),
            config_hash=config_hash,
            owner_principal_id="alice",
            step_id="run-approval",
            approval_id=approval_id,
        )
    return approval_event_id


_LEGACY_RUN_TABLE_SQL = """
CREATE TABLE ne_agent_runs (
  run_id VARCHAR NOT NULL PRIMARY KEY,
  conversation_id VARCHAR NOT NULL,
  owner_principal_id VARCHAR NOT NULL,
  department VARCHAR NOT NULL,
  conversation_agent_name VARCHAR NOT NULL,
  agent_id VARCHAR NOT NULL,
  agent_version INTEGER NOT NULL,
  strategy VARCHAR NOT NULL,
  config_hash VARCHAR(64) NOT NULL,
  capability_snapshot_hash VARCHAR(64) NOT NULL,
  capability_snapshot_receipt_id VARCHAR NOT NULL,
  sequence INTEGER NOT NULL,
  display_label VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  version INTEGER DEFAULT 1 NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  terminal_at DATETIME,
  CONSTRAINT uq_agent_run_conversation_sequence UNIQUE (conversation_id, sequence),
  CONSTRAINT uq_agent_run_conversation_identity UNIQUE (run_id, conversation_id),
  CONSTRAINT uq_agent_run_owner_scope UNIQUE
    (run_id, owner_principal_id, conversation_id),
  CONSTRAINT uq_agent_run_owner_identity UNIQUE (run_id, owner_principal_id),
  CONSTRAINT fk_agent_run_conversation_owner_scope FOREIGN KEY
    (conversation_id, owner_principal_id, department, conversation_agent_name)
    REFERENCES ne_agent_conversations
    (conversation_id, owner_principal_id, department, agent_name),
  CONSTRAINT ck_agent_run_status CHECK
    (status IN ('submitted','queued','running','waiting_approval','cancelling',
                'completed','refused','failed','cancelled','expired')),
  CONSTRAINT ck_agent_run_agent_version CHECK (agent_version >= 1),
  CONSTRAINT ck_agent_run_sequence CHECK (sequence >= 1),
  CONSTRAINT ck_agent_run_config_hash CHECK (length(config_hash) = 64),
  CONSTRAINT ck_agent_run_capability_hash CHECK
    (length(capability_snapshot_hash) = 64),
  CONSTRAINT ck_agent_run_capability_receipt_nonempty CHECK
    (length(trim(capability_snapshot_receipt_id)) > 0),
  CONSTRAINT ck_agent_run_terminal_time CHECK (
    (status IN ('completed','refused','failed','cancelled','expired')
      AND terminal_at IS NOT NULL) OR
    (status NOT IN ('completed','refused','failed','cancelled','expired')
      AND terminal_at IS NULL)
  )
)
"""


def _legacy_run_lifecycle_engine(*, claim_bearing_refusal: bool = False) -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    ne_agent_conversations.create(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            ne_agent_conversations.insert().values(
                conversation_id="legacy-conversation",
                owner_principal_id="alice",
                agent_name="assistant",
                department="claims",
                created_at=now,
                updated_at=now,
                last_sequence=0,
            )
        )
        conn.exec_driver_sql(_LEGACY_RUN_TABLE_SQL)
        conn.exec_driver_sql(
            "CREATE INDEX ix_ne_agent_runs_conversation_id ON ne_agent_runs (conversation_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX ix_ne_agent_runs_owner_principal_id ON ne_agent_runs (owner_principal_id)"
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO ne_agent_runs (
                  run_id, conversation_id, owner_principal_id, department,
                  conversation_agent_name, agent_id, agent_version, strategy,
                  config_hash, capability_snapshot_hash,
                  capability_snapshot_receipt_id, sequence, display_label,
                  status, version, created_at, updated_at, terminal_at
                ) VALUES (
                  'legacy-refused', 'legacy-conversation', 'alice', 'claims',
                  'assistant', 'agt_legacy', 1, 'agentic', :digest, :digest,
                  'legacy-capability-receipt', 1, 'Legacy refusal',
                  'refused', 1, :now, :now, :now
                )
                """
            ),
            {"digest": "a" * 64, "now": now},
        )
        if claim_bearing_refusal:
            conn.exec_driver_sql(
                "CREATE TABLE ne_agent_run_claims ("
                "claim_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL)"
            )
            conn.exec_driver_sql(
                "INSERT INTO ne_agent_run_claims (claim_id, run_id) "
                "VALUES ('legacy-claim', 'legacy-refused')"
            )
    return engine


def test_sqlite_legacy_run_lifecycle_constraints_are_rebuilt() -> None:
    engine = _legacy_run_lifecycle_engine()
    ensure_run_schema(engine)
    ensure_run_schema(engine)

    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == "legacy-refused")
        ).one()
        assert row.status == RunState.COMPLETED.value
        assert row.output_verdict == RunOutputVerdict.REFUSED.value
        checks = {
            str(item["name"]): str(item["sqltext"])
            for item in sa.inspect(conn).get_check_constraints(ne_agent_runs.name)
        }
        assert "'refused'" not in checks["ck_agent_run_status"]
        assert "context_exhausted" in checks["ck_agent_run_status"]
        assert "budget_exhausted" in checks["ck_agent_run_terminal_time"]
        indexes = {item["name"] for item in sa.inspect(conn).get_indexes(ne_agent_runs.name)}
        assert {
            "ix_ne_agent_runs_conversation_id",
            "ix_ne_agent_runs_owner_principal_id",
        } <= indexes
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        terminal_values = {
            "run_id": "legacy-context",
            "conversation_id": "legacy-conversation",
            "owner_principal_id": "alice",
            "department": "claims",
            "conversation_agent_name": "assistant",
            "agent_id": "agt_legacy",
            "agent_version": 1,
            "strategy": "agentic",
            "config_hash": "a" * 64,
            "capability_snapshot_hash": "a" * 64,
            "capability_snapshot_receipt_id": "legacy-capability-receipt",
            "sequence": 2,
            "display_label": "Legacy exhaustion",
            "status": RunState.CONTEXT_EXHAUSTED.value,
            "output_verdict": RunOutputVerdict.UNAVAILABLE.value,
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "terminal_at": datetime.now(UTC),
        }
        conn.execute(ne_agent_runs.insert().values(**terminal_values))
        with pytest.raises(IntegrityError, match="ck_agent_run_status"):
            conn.execute(
                ne_agent_runs.insert().values(
                    **{
                        **terminal_values,
                        "run_id": "legacy-refused-reintroduced",
                        "sequence": 3,
                        "status": "refused",
                        "output_verdict": None,
                        "terminal_at": None,
                    }
                )
            )
    engine.dispose()


def test_sqlite_claim_bearing_legacy_refusal_fails_closed() -> None:
    engine = _legacy_run_lifecycle_engine(claim_bearing_refusal=True)
    with pytest.raises(RuntimeError, match="claim-bearing legacy refused Run"):
        ensure_run_schema(engine)
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT status FROM ne_agent_runs WHERE run_id = 'legacy-refused'"
        ).one()
        assert row.status == "refused"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    engine.dispose()


def test_run_approval_wait_is_atomic_idempotent_and_payload_free() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    sentinel = "ceo@example.test / secret body / source-item-991"

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        attempt = await runs.link_attempt(run_id=run.run_id)
        now = datetime.now(UTC)
        kwargs = {
            "run_id": run.run_id,
            "attempt_id": attempt.attempt_id,
            "step_id": "step-1",
            "tool_call_id": "tool-call-1",
            "action": "write.external.email.send",
            "action_hash": "a" * 64,
            "tier": 2,
            "principal_id": "alice",
            "policy_hash": "b" * 64,
            "invocation_plaintext": sentinel.encode(),
            "source_stamps": (_stamp(),),
            "stamp_receipt_id": "opaque-receipt-1",
            "stamp_receipt_hash": "c" * 64,
            "requested_at": now,
            "sla_deadline": now,
            "expires_at": now,
            "event": new_approval_event(
                approval_id="allocated-inside-transaction",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        }
        joined = await checkpoints.create_wait(**kwargs)  # type: ignore[arg-type]
        replay = await checkpoints.create_wait(**kwargs)  # type: ignore[arg-type]
        assert replay == joined
        changed_receipt = {**kwargs, "stamp_receipt_id": "opaque-receipt-foreign"}
        with pytest.raises(ValueError, match="changed its protected invocation"):
            await checkpoints.create_wait(**changed_receipt)  # type: ignore[arg-type]
        loaded = await checkpoints.load_checkpoint(
            joined.approval_id,
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
        )
        assert loaded.run_id == run.run_id
        with pytest.raises(LookupError, match="unavailable"):
            await checkpoints.load_checkpoint(
                joined.approval_id,
                expected_action_hash="d" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )
        assert (
            await checkpoints.reconcile_source_authority(
                joined.approval_id, decision="retryable_unknown", decided_at=now
            )
            is False
        )
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_workflow_claims.insert().values(
                    dispatch_id="approval-wait-dispatch",
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    attempt_id=attempt.attempt_id,
                    config_hash="1" * 64,
                    created_at=now,
                )
            )
        assert await checkpoints.decide(
            joined.approval_id,
            status="approved",
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            approver="approver:one",
            reason=None,
            decided_at=now,
            event=new_approval_event(
                approval_id="resolved-through-join",
                tier=2,
                decision=ApprovalDecision.approved,
                subject_ref="tool:write.external.email.send",
                actor="approver:one",
            ),
        )
        resume_request = await checkpoints.load_resume_request(joined.approval_id)
        assert (
            resume_request.run_id,
            resume_request.attempt_id,
            resume_request.dispatch_id,
            resume_request.owner_principal_id,
            resume_request.decision,
        ) == (
            run.run_id,
            attempt.attempt_id,
            "approval-wait-dispatch",
            "alice",
            "approved",
        )
        assert await checkpoints.require_resume_request(resume_request) == resume_request
        assert await checkpoints.load_pending_resume_requests() == (resume_request,)
        resumed = await checkpoints.resume(
            joined.approval_id,
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            resumed_at=now,
        )
        assert resumed.status == "resumed"
        # THE RECONCILER EXCLUSION THE REPAIR RUNBOOK LEANS ON.  Once the Run
        # is RUNNING with a resumed (or consumed) continuation, the API-boot
        # dispatch reconciler's sweep must NEVER re-enqueue its resume intent:
        # the query demands run status `waiting_approval` AND checkpoint status
        # == the intent's decision.  A stranded post-send run (running/resumed,
        # decision approved) is excluded on BOTH conjuncts — this is half of
        # the proof that nothing unattended re-executes the 2026-08-28
        # production strandings (docs/ops/approval-resume-repair.md).
        assert await checkpoints.load_pending_resume_requests() == ()
        await checkpoints.consume_resumed(joined.approval_id, consumed_at=now)
        assert await checkpoints.load_pending_resume_requests() == ()

    asyncio.run(scenario())
    with engine.connect() as conn:
        run_row = conn.execute(sa.select(ne_agent_runs)).one()
        approval = conn.execute(sa.select(ne_approval_requests)).one()
        checkpoint = conn.execute(sa.select(ne_agent_run_continuation_checkpoints)).one()
        outbox = conn.execute(sa.select(ne_agent_run_approval_audit_outbox)).one()
        assert run_row.status == RunState.RUNNING.value
        join_count = conn.execute(
            sa.select(sa.func.count()).select_from(ne_agent_run_approval_joins)
        ).scalar_one()
        assert join_count == 1
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_approval_resume_intents)
            ).scalar_one()
            == 1
        )
        clear_rows = json.dumps(
            {
                "approval": approval.payload,
                "checkpoint": dict(checkpoint._mapping),
                "outbox": dict(outbox._mapping),
            },
            default=str,
        )
        assert sentinel not in clear_rows
        assert "source-item-991" not in clear_rows
    engine.dispose()


def test_approved_join_cannot_resume_after_durable_cancel_wins() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    lifecycle = RunWorkflowStateStore(engine, run_store=runs)

    async def scenario() -> None:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        accepted = await runs.admit_start(
            _model_bound_start_command(
                idempotency_key="approval-cancel-start",
                request_hash="1" * 64,
                conversation_id=conversation.conversation_id,
                expected_version=0,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="2" * 64,
                capability_snapshot_hash="3" * 64,
                capability_snapshot_receipt_id="cap-receipt-approval-cancel",
                input_plaintext=b"protected approval input",
            )
        )
        claim = lifecycle.claim(
            dispatch_id="approval-cancel-dispatch",
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="2" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id="step-approval",
            tool_call_id="tool-approval",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"recipient":"protected"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id="stamp-approval-cancel",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        assert await checkpoints.decide(
            joined.approval_id,
            status="approved",
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            approver="approver:one",
            reason=None,
            decided_at=now,
            event=new_approval_event(
                approval_id="exact-approved",
                tier=2,
                decision=ApprovalDecision.approved,
                subject_ref="tool:write.external.email.send",
                actor="approver:one",
            ),
        )
        await _bind_approval_finalizer_proof(
            engine,
            runs,
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            claim_event_id=claim.claim_event_id,
            approval_id=joined.approval_id,
            config_hash="2" * 64,
            tag="approval-cancel",
        )
        waiting = await runs.get_run(run_id=accepted.run_id, owner_principal_id="alice")
        assert waiting is not None and waiting.status is RunState.WAITING_APPROVAL
        await runs.admit_cancel(
            SimpleNamespace(
                idempotency_key="approval-cancel-control",
                request_hash="4" * 64,
                run_id=accepted.run_id,
                expected_version=waiting.version,
                owner_principal_id="alice",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="2" * 64,
                capability_snapshot_hash="3" * 64,
                capability_snapshot_receipt_id="cap-receipt-approval-cancel",
            )
        )
        terminal = lifecycle.reconcile_approval_join(
            approval_id=joined.approval_id,
            action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
        )
        assert terminal is not None and terminal.status is RunState.CANCELLED
        with pytest.raises(LookupError, match="not resumable"):
            await checkpoints.resume(
                joined.approval_id,
                expected_action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
                resumed_at=now,
            )
        lifecycle.release(claim)

    asyncio.run(scenario())
    with engine.connect() as conn:
        run = conn.execute(sa.select(ne_agent_runs)).one()
        checkpoint = conn.execute(sa.select(ne_agent_run_continuation_checkpoints)).one()
        assert run.status == RunState.CANCELLED.value
        assert checkpoint.status == "approved"
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_lifecycle_outbox)
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_tier3_run_approval_keeps_checkpoint_parked_until_distinct_second_check() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        attempt = await runs.link_attempt(run_id=run.run_id)
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                ne_agent_run_workflow_claims.insert().values(
                    dispatch_id="tier3-dispatch",
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    attempt_id=attempt.attempt_id,
                    config_hash="1" * 64,
                    created_at=now,
                )
            )
        joined = await checkpoints.create_wait(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            step_id="step-tier3",
            tool_call_id="tool-tier3",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=3,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b"{}",
            source_stamps=(_stamp(),),
            stamp_receipt_id="receipt-tier3",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside-transaction",
                tier=3,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        assert await checkpoints.record_first_approval(
            joined.approval_id,
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            approver="approver:one",
            decided_at=now,
            event=new_approval_event(
                approval_id=joined.approval_id,
                tier=3,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="approver:one",
            ),
        )
        assert (
            await checkpoints.load_checkpoint(
                joined.approval_id,
                expected_action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )
        ).status == "pending"
        with pytest.raises(ValueError, match="two distinct"):
            await checkpoints.decide(
                joined.approval_id,
                status="approved",
                expected_action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
                approver="approver:one",
                second_approver="approver:one",
                reason=None,
                decided_at=now,
                event=new_approval_event(
                    approval_id=joined.approval_id,
                    tier=3,
                    decision=ApprovalDecision.approved,
                    subject_ref="tool:write.external.email.send",
                    actor="approver:one",
                ),
            )
        assert await checkpoints.decide(
            joined.approval_id,
            status="approved",
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            approver="approver:one",
            second_approver="approver:two",
            reason=None,
            decided_at=now,
            event=new_approval_event(
                approval_id=joined.approval_id,
                tier=3,
                decision=ApprovalDecision.approved,
                subject_ref="tool:write.external.email.send",
                actor="approver:two",
            ),
        )
        resume = await checkpoints.load_resume_request(joined.approval_id)
        assert resume.decision == "approved"

    asyncio.run(scenario())
    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_approval_requests)).one()
        assert (row.status, row.first_approver, row.second_approver) == (
            "approved",
            "approver:one",
            "approver:two",
        )
    engine.dispose()


def test_rejected_approval_finishes_as_content_free_refusal_through_finalizer() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    lifecycle = RunWorkflowStateStore(engine, run_store=runs)

    async def scenario() -> str:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        accepted = await runs.admit_start(
            _model_bound_start_command(
                idempotency_key="approval-refusal-start",
                request_hash="1" * 64,
                conversation_id=conversation.conversation_id,
                expected_version=0,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="2" * 64,
                capability_snapshot_hash="3" * 64,
                capability_snapshot_receipt_id="cap-receipt-approval-refusal",
                input_plaintext=b"protected approval input",
            )
        )
        claim = lifecycle.claim(
            dispatch_id="approval-refusal-dispatch",
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="2" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id="step-refusal",
            tool_call_id="tool-refusal",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"recipient":"protected"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id="stamp-refusal",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        assert await checkpoints.decide(
            joined.approval_id,
            status="rejected",
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            approver="approver:one",
            reason="policy refusal",
            decided_at=now,
            event=new_approval_event(
                approval_id="exact-rejected",
                tier=2,
                decision=ApprovalDecision.rejected,
                subject_ref="tool:write.external.email.send",
                actor="approver:one",
            ),
        )
        approval_event_id = await _bind_approval_finalizer_proof(
            engine,
            runs,
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            claim_event_id=claim.claim_event_id,
            approval_id=joined.approval_id,
            config_hash="2" * 64,
            tag="approval-refusal",
            record_approval=False,
        )
        with pytest.raises(ValueError, match="mandatory audit receipt"):
            lifecycle.reconcile_approval_join(
                approval_id=joined.approval_id,
                action_hash="a" * 64,
                principal_id="alice",
                policy_hash="b" * 64,
            )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_finalizations)
                ).scalar_one()
                == 0
            )
        await runs.record_audit_receipt(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            run_event_id=approval_event_id,
            decision_kind=RunAuditDecisionKind.APPROVAL,
            audit_event_id="audit-approval-approval-refusal",
            audit_sequence=2,
            audit_record_hash=hashlib.sha256(b"audit-approval-approval-refusal").hexdigest(),
            config_hash="2" * 64,
            owner_principal_id="alice",
            step_id="run-approval",
            approval_id=joined.approval_id,
        )
        terminal = lifecycle.reconcile_approval_join(
            approval_id=joined.approval_id,
            action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
        )
        assert terminal is not None
        assert terminal.status is RunState.COMPLETED
        assert terminal.output_verdict is RunOutputVerdict.REFUSED
        assert terminal.outcome == "refused"
        lifecycle.release(claim)
        return accepted.run_id

    run_id = asyncio.run(scenario())
    with engine.connect() as conn:
        finalization = conn.execute(
            sa.select(ne_agent_run_finalizations).where(
                ne_agent_run_finalizations.c.run_id == run_id
            )
        ).one()
        assert finalization.mapping_state == "unavailable"
        assert finalization.protected_output_kind == "unavailable"
        assert finalization.protected_output_ref is None
        assert finalization.output_verdict == RunOutputVerdict.REFUSED.value
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_claims)
                .where(ne_agent_run_claims.c.run_id == run_id)
            ).scalar_one()
            == 0
        )
    engine.dispose()


def test_approval_terminal_paths_have_no_secondary_terminal_writer() -> None:
    checkpoint_source = inspect.getsource(RunApprovalCheckpointStore._withhold_sync)
    reconcile_source = inspect.getsource(RunWorkflowStateStore.reconcile_approval_join)
    for source in (checkpoint_source, reconcile_source):
        assert "commit_run_terminal_in_transaction" not in source
        assert "finalize_unavailable_in_transaction" in source


def test_source_withheld_terminal_state_event_and_outbox_are_one_transaction() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    lifecycle = RunWorkflowStateStore(engine, run_store=runs)

    async def scenario() -> tuple[str, str, str, str]:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        accepted = await runs.admit_start(
            _model_bound_start_command(
                idempotency_key="source-withheld-start",
                request_hash="1" * 64,
                conversation_id=conversation.conversation_id,
                expected_version=0,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="2" * 64,
                capability_snapshot_hash="3" * 64,
                capability_snapshot_receipt_id="cap-receipt-source-withheld",
                input_plaintext=b"protected approval input",
            )
        )
        claim = lifecycle.claim(
            dispatch_id="source-withheld-dispatch",
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="2" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id="step-source-withheld",
            tool_call_id="tool-source-withheld",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"recipient":"protected"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id="stamp-source-withheld",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        await _bind_approval_finalizer_proof(
            engine,
            runs,
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            claim_event_id=claim.claim_event_id,
            approval_id=joined.approval_id,
            config_hash="2" * 64,
            tag="source-withheld",
        )

        def fail_terminal_outbox(
            _conn: Any,
            clause: Any,
            _multiparams: Any,
            _params: Any,
            _options: Any,
        ) -> None:
            if (
                isinstance(clause, Insert)
                and clause.table.name == ne_agent_run_lifecycle_outbox.name
            ):
                raise RuntimeError("injected source-withheld publication failure")

        event.listen(engine, "before_execute", fail_terminal_outbox)
        try:
            with pytest.raises(RuntimeError, match="injected source-withheld publication failure"):
                await checkpoints.reconcile_source_authority(
                    joined.approval_id,
                    decision="permanent_withheld",
                    decided_at=now,
                )
        finally:
            event.remove(engine, "before_execute", fail_terminal_outbox)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(ne_approval_requests.c.status).where(
                        ne_approval_requests.c.request_id == joined.approval_id
                    )
                ).scalar_one()
                == "pending"
            )
            assert (
                conn.execute(
                    sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                        ne_agent_run_continuation_checkpoints.c.checkpoint_id
                        == joined.checkpoint_id
                    )
                ).scalar_one()
                == "pending"
            )
            assert (
                conn.execute(
                    sa.select(ne_agent_runs.c.status).where(
                        ne_agent_runs.c.run_id == accepted.run_id
                    )
                ).scalar_one()
                == RunState.WAITING_APPROVAL.value
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_events)
                    .where(
                        ne_agent_run_events.c.run_id == accepted.run_id,
                        ne_agent_run_events.c.event_kind == "run.terminal",
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_lifecycle_outbox)
                    .where(ne_agent_run_lifecycle_outbox.c.run_id == accepted.run_id)
                ).scalar_one()
                == 0
            )

        assert await checkpoints.reconcile_source_authority(
            joined.approval_id,
            decision="permanent_withheld",
            decided_at=now,
        )
        lifecycle.release(claim)
        return (
            accepted.run_id,
            accepted.attempt_id,
            joined.approval_id,
            joined.checkpoint_id,
        )

    run_id, attempt_id, approval_id, checkpoint_id = asyncio.run(scenario())
    with engine.connect() as conn:
        run_row = conn.execute(
            sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == run_id)
        ).one()
        assert run_row.status == RunState.FAILED.value
        assert run_row.output_verdict == RunOutputVerdict.UNAVAILABLE.value
        assert (
            conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id == approval_id
                )
            ).scalar_one()
            == "source_withheld"
        )
        assert (
            conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                    ne_agent_run_continuation_checkpoints.c.checkpoint_id == checkpoint_id
                )
            ).scalar_one()
            == "source_withheld"
        )
        terminal = conn.execute(
            sa.select(ne_agent_run_events).where(
                ne_agent_run_events.c.run_id == run_id,
                ne_agent_run_events.c.attempt_id == attempt_id,
                ne_agent_run_events.c.event_kind == "run.terminal",
            )
        ).one()
        outbox = conn.execute(
            sa.select(ne_agent_run_lifecycle_outbox).where(
                ne_agent_run_lifecycle_outbox.c.run_id == run_id
            )
        ).one()
        assert terminal.event_id == outbox.event_id
        assert outbox.terminal_status == RunState.FAILED.value
        assert outbox.output_verdict == RunOutputVerdict.UNAVAILABLE.value
    engine.dispose()


def test_current_source_denial_terminalizes_approved_and_resumed_joins() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    lifecycle = RunWorkflowStateStore(engine)

    async def create_approved(tag: str):  # type: ignore[no-untyped-def]
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        accepted = await runs.admit_start(
            _model_bound_start_command(
                idempotency_key=f"source-approved-{tag}",
                request_hash=hashlib.sha256(tag.encode()).hexdigest(),
                conversation_id=conversation.conversation_id,
                expected_version=0,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="2" * 64,
                capability_snapshot_hash="3" * 64,
                capability_snapshot_receipt_id=f"cap-receipt-{tag}",
                input_plaintext=b"protected approval input",
            )
        )
        claim = lifecycle.claim(
            dispatch_id=f"source-approved-dispatch-{tag}",
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="2" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id=f"step-{tag}",
            tool_call_id=f"tool-{tag}",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"recipient":"protected"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id=f"stamp-{tag}",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        assert await checkpoints.decide(
            joined.approval_id,
            status="approved",
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            approver="approver:one",
            reason=None,
            decided_at=now,
            event=new_approval_event(
                approval_id="exact-approved",
                tier=2,
                decision=ApprovalDecision.approved,
                subject_ref="tool:write.external.email.send",
                actor="approver:one",
            ),
        )
        await _bind_approval_finalizer_proof(
            engine,
            runs,
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            claim_event_id=claim.claim_event_id,
            approval_id=joined.approval_id,
            config_hash="2" * 64,
            tag=f"source-{tag}",
        )
        return accepted, claim, joined, now

    async def scenario() -> tuple[str, str]:
        approved, approved_claim, approved_join, now = await create_approved("waiting")
        assert await checkpoints.reconcile_source_authority(
            approved_join.approval_id,
            decision="permanent_withheld",
            decided_at=now,
        )
        lifecycle.release(approved_claim)

        resumed, resumed_claim, resumed_join, now = await create_approved("resumed")
        checkpoint = await checkpoints.resume(
            resumed_join.approval_id,
            expected_action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
            resumed_at=now,
        )
        assert checkpoint.status == "resumed"
        assert await checkpoints.reconcile_source_authority(
            resumed_join.approval_id,
            decision="permanent_withheld",
            decided_at=now,
        )
        lifecycle.release(resumed_claim)
        return approved.run_id, resumed.run_id

    run_ids = asyncio.run(scenario())
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                ne_agent_runs.c.run_id,
                ne_agent_runs.c.status,
                ne_agent_runs.c.output_verdict,
            ).where(ne_agent_runs.c.run_id.in_(run_ids))
        ).all()
        assert {(row.run_id, row.status, row.output_verdict) for row in rows} == {
            (run_ids[0], RunState.FAILED.value, RunOutputVerdict.UNAVAILABLE.value),
            (run_ids[1], RunState.FAILED.value, RunOutputVerdict.UNAVAILABLE.value),
        }
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_lifecycle_outbox)
                .where(ne_agent_run_lifecycle_outbox.c.run_id.in_(run_ids))
            ).scalar_one()
            == 2
        )
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_events)
                .where(
                    ne_agent_run_events.c.run_id.in_(run_ids),
                    ne_agent_run_events.c.event_kind == "run.terminal",
                )
            ).scalar_one()
            == 2
        )
        assert set(
            conn.execute(
                sa.select(ne_approval_requests.c.status).where(
                    ne_approval_requests.c.request_id.in_(
                        sa.select(ne_agent_run_approval_joins.c.approval_id).where(
                            ne_agent_run_approval_joins.c.run_id.in_(run_ids)
                        )
                    )
                )
            ).scalars()
        ) == {"source_withheld"}
    engine.dispose()


def test_durable_approval_expiry_reconciles_exactly_against_cancel(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'run-approval-expiry.db'}"
    engine = sa.create_engine(database_url)
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_approval_schema(engine)
    ensure_run_workflow_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(engine, cipher)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)
    lifecycle = RunWorkflowStateStore(engine, run_store=runs)

    async def create_wait(tag: str):  # type: ignore[no-untyped-def]
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        accepted = await runs.admit_start(
            _model_bound_start_command(
                idempotency_key=f"expiry-start-{tag}",
                request_hash=hashlib.sha256(tag.encode()).hexdigest(),
                conversation_id=conversation.conversation_id,
                expected_version=0,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id=f"cap-receipt-{tag}",
                input_plaintext=b"protected approval input",
            )
        )
        claim = lifecycle.claim(
            dispatch_id=f"expiry-dispatch-{tag}",
            run_id=accepted.run_id,
            owner_principal_id="alice",
            config_hash="1" * 64,
        )
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            step_id=f"step-{tag}",
            tool_call_id=f"tool-{tag}",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"recipient":"protected"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id=f"stamp-{tag}",
            stamp_receipt_hash="c" * 64,
            requested_at=now,
            sla_deadline=now,
            expires_at=now,
            event=new_approval_event(
                approval_id="allocated-inside",
                tier=2,
                decision=ApprovalDecision.requested,
                subject_ref="tool:write.external.email.send",
                actor="alice",
            ),
        )
        await _bind_approval_finalizer_proof(
            engine,
            runs,
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            claim_event_id=claim.claim_event_id,
            approval_id=joined.approval_id,
            config_hash="1" * 64,
            tag=f"expiry-{tag}",
        )
        return accepted, claim, joined

    async def cancel(accepted: Any, tag: str) -> None:
        current = await runs.get_run(run_id=accepted.run_id, owner_principal_id="alice")
        assert current is not None
        await runs.admit_cancel(
            SimpleNamespace(
                idempotency_key=f"expiry-cancel-{tag}",
                request_hash=hashlib.sha256(f"cancel-{tag}".encode()).hexdigest(),
                run_id=accepted.run_id,
                expected_version=current.version,
                owner_principal_id="alice",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id=f"cap-receipt-{tag}",
            )
        )

    async def scenario() -> tuple[str, str]:
        expiry_first, expiry_claim, expiry_join = await create_wait("expiry-first")
        assert expire_approval_step(database_url, expiry_join.approval_id) == "expired"
        expiry_publication = lifecycle.reconcile_approval_join(
            approval_id=expiry_join.approval_id,
            action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
        )
        assert expiry_publication is not None
        assert expiry_publication.status is RunState.EXPIRED
        lifecycle.release(expiry_claim)
        with pytest.raises(RunSubmissionConflictError, match="cannot be cancelled"):
            await cancel(expiry_first, "expiry-first")

        cancel_first, cancel_claim, cancel_join = await create_wait("cancel-first")
        await cancel(cancel_first, "cancel-first")
        assert expire_approval_step(database_url, cancel_join.approval_id) == "expired"
        cancel_publication = lifecycle.reconcile_approval_join(
            approval_id=cancel_join.approval_id,
            action_hash="a" * 64,
            principal_id="alice",
            policy_hash="b" * 64,
        )
        assert cancel_publication is not None
        assert cancel_publication.status is RunState.CANCELLED
        lifecycle.release(cancel_claim)
        return expiry_first.run_id, cancel_first.run_id

    run_ids = asyncio.run(scenario())
    with engine.connect() as conn:
        assert set(
            conn.execute(
                sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id.in_(run_ids))
            ).scalars()
        ) == {RunState.EXPIRED.value, RunState.CANCELLED.value}
        assert set(
            conn.execute(
                sa.select(ne_agent_run_continuation_checkpoints.c.status).where(
                    ne_agent_run_continuation_checkpoints.c.run_id.in_(run_ids)
                )
            ).scalars()
        ) == {"expired"}
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(ne_agent_run_lifecycle_outbox)
                .where(ne_agent_run_lifecycle_outbox.c.run_id.in_(run_ids))
            ).scalar_one()
            == 2
        )
    engine.dispose()


def test_run_approval_outbox_failure_rolls_back_every_row() -> None:
    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)

    async def scenario() -> str:
        _, run = await _create_run(sessions, runs)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        attempt = await runs.link_attempt(run_id=run.run_id)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TRIGGER fail_run_approval_outbox BEFORE INSERT ON "
                "ne_agent_run_approval_audit_outbox BEGIN SELECT RAISE(ABORT, 'probe'); END"
            )
        now = datetime.now(UTC)
        with pytest.raises(IntegrityError, match="probe"):
            await checkpoints.create_wait(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                step_id="step-1",
                tool_call_id="tool-call-1",
                action="write.external.email.send",
                action_hash="a" * 64,
                tier=2,
                principal_id="alice",
                policy_hash="b" * 64,
                invocation_plaintext=b"secret",
                source_stamps=(_stamp(),),
                stamp_receipt_id="receipt",
                stamp_receipt_hash="c" * 64,
                requested_at=now,
                sla_deadline=now,
                expires_at=now,
                event=new_approval_event(
                    approval_id="allocated-inside",
                    tier=2,
                    decision=ApprovalDecision.requested,
                    subject_ref="tool:write.external.email.send",
                    actor="alice",
                ),
            )
        return run.run_id

    run_id = asyncio.run(scenario())
    with engine.connect() as conn:
        for table in (
            ne_approval_requests,
            ne_agent_protected_source_invocations,
            ne_agent_run_continuation_checkpoints,
            ne_agent_run_approval_joins,
            ne_agent_run_approval_audit_outbox,
        ):
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
        status = conn.execute(
            sa.select(ne_agent_runs.c.status).where(ne_agent_runs.c.run_id == run_id)
        ).scalar_one()
        assert status == RunState.RUNNING.value
    engine.dispose()


async def _create_run(
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
    *,
    owner: str = "alice",
    capability_hash: str = "2" * 64,
):
    conversation = await sessions.create_conversation("assistant", "claims", owner)
    run = await runs.create_run(
        conversation_id=conversation.conversation_id,
        owner_principal_id=owner,
        department="claims",
        conversation_agent_name="assistant",
        agent_id="agt_01",
        agent_version=7,
        strategy="agentic",
        config_hash="1" * 64,
        capability_snapshot_hash=capability_hash,
        capability_snapshot_receipt_id="cap-receipt-1",
        input_plaintext="RunInput prompt sentinel — private".encode(),
    )
    return conversation, run


def test_accepted_submission_is_atomic_idempotent_encrypted_and_versioned() -> None:
    engine, sessions, runs = _stores()
    identities: dict[str, str] = {}

    async def scenario() -> None:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        start = _model_bound_start_command(
            idempotency_key="start-1",
            request_hash="a" * 64,
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id="alice",
            department="claims",
            conversation_agent_name="assistant",
            agent_id="agt_01",
            agent_version=7,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            requested_model="frontier-primary",
            requested_model_lane="frontier-api",
            input_plaintext=b"PRIVATE accepted submission sentinel",
        )
        accepted = await runs.admit_start(start)
        replay = await runs.admit_start(start)
        assert accepted.status == "submitted"
        assert replay == replace(accepted, replayed=True)
        assert (
            await runs.load_authenticated_context(
                run_id=accepted.run_id, owner_principal_id="alice"
            )
            == start.authenticated_context
        )
        workflow_request = await runs.load_workflow_request(
            enqueue_id=accepted.enqueue_id,
            run_id=accepted.run_id,
            attempt_id=accepted.attempt_id,
            owner_principal_id="alice",
        )
        assert workflow_request.authenticated_context == start.authenticated_context
        assert workflow_request.target.run_id == accepted.run_id
        assert workflow_request.target.attempt_id == accepted.attempt_id
        pending = await runs.load_pending_workflow_requests()
        assert pending == (workflow_request,)
        with pytest.raises(RunSubmissionConflictError):
            await runs.admit_start(SimpleNamespace(**{**vars(start), "request_hash": "b" * 64}))

        cancel = SimpleNamespace(
            idempotency_key="cancel-1",
            request_hash="c" * 64,
            run_id=accepted.run_id,
            expected_version=1,
            owner_principal_id="alice",
            agent_id="agt_01",
            agent_version=7,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
        )
        cancelled = await runs.admit_cancel(cancel)
        assert (cancelled.status, cancelled.version) == ("cancelling", 2)
        assert (await runs.admit_cancel(cancel)).replayed is True
        pending = await runs.load_pending_workflow_requests()
        assert tuple(request.dispatch_id for request in pending) == (accepted.enqueue_id,)
        with pytest.raises(RunSubmissionNotFoundError):
            await runs.admit_cancel(
                SimpleNamespace(
                    **{**vars(cancel), "owner_principal_id": "foreign", "idempotency_key": "x"}
                )
            )

        with engine.begin() as conn:
            commit_run_terminal_in_transaction(
                conn,
                run_id=accepted.run_id,
                attempt_id=accepted.attempt_id,
                status=RunState.CANCELLED,
                outcome="cancelled",
                output_verdict=RunOutputVerdict.UNAVAILABLE,
                allowed_from={RunState.CANCELLING: frozenset({RunState.CANCELLED})},
                step_id="test-submission",
                now=datetime.now(UTC),
            )
        retry = SimpleNamespace(
            **{
                **vars(cancel),
                "idempotency_key": "retry-1",
                "request_hash": "d" * 64,
                "expected_version": 3,
                "authenticated_context": _model_bound_start_command(
                    owner_principal_id="alice", department="claims"
                ).authenticated_context,
            }
        )
        retried = await runs.admit_retry(retry)
        assert (await runs.admit_retry(retry)).run_id == retried.run_id
        retried_request = await runs.load_workflow_request(
            enqueue_id=retried.enqueue_id,
            run_id=retried.run_id,
            attempt_id=retried.attempt_id,
            owner_principal_id="alice",
        )
        assert retried_request.authenticated_context == retry.authenticated_context
        pending = await runs.load_pending_workflow_requests()
        assert tuple(request.dispatch_id for request in pending) == (retried.enqueue_id,)
        identities.update(predecessor=accepted.run_id, successor=retried.run_id)
        assert (retried.status, retried.version) == ("queued", 1)
        assert retried.run_id != accepted.run_id
        assert retried.attempt_id != accepted.attempt_id
        predecessor = await runs.get_run(run_id=accepted.run_id, owner_principal_id="alice")
        assert predecessor is not None
        assert predecessor.status is RunState.CANCELLED
        assert predecessor.terminal_at is not None
        assert (predecessor.requested_model, predecessor.requested_model_lane) == (
            "frontier-primary",
            "frontier-api",
        )
        successor = await runs.get_run(run_id=retried.run_id, owner_principal_id="alice")
        assert successor is not None
        assert successor.predecessor_run_id == accepted.run_id
        assert successor.root_run_id == accepted.run_id
        assert successor.retry_ordinal == 1
        assert successor.context_snapshot_hash is not None
        assert (successor.requested_model, successor.requested_model_lane) == (
            "frontier-primary",
            "frontier-api",
        )
        await runs.transition(
            run_id=successor.run_id,
            expected=RunState.QUEUED,
            target=RunState.RUNNING,
        )
        with engine.begin() as conn:
            commit_run_terminal_in_transaction(
                conn,
                run_id=successor.run_id,
                attempt_id=retried.attempt_id,
                status=RunState.FAILED,
                outcome="failed",
                output_verdict=RunOutputVerdict.UNAVAILABLE,
                allowed_from={RunState.RUNNING: frozenset({RunState.FAILED})},
                step_id="test-submission",
                now=datetime.now(UTC),
            )
        successor = await runs.get_run(run_id=successor.run_id, owner_principal_id="alice")
        assert successor is not None
        retry_two = await runs.admit_retry(
            SimpleNamespace(
                **{
                    **vars(retry),
                    "run_id": successor.run_id,
                    "expected_version": successor.version,
                    "idempotency_key": "retry-2",
                    "request_hash": "e" * 64,
                }
            )
        )
        second_successor = await runs.get_run(run_id=retry_two.run_id, owner_principal_id="alice")
        assert second_successor is not None
        assert second_successor.predecessor_run_id == successor.run_id
        assert second_successor.root_run_id == accepted.run_id
        assert second_successor.retry_ordinal == 2
        identities["second_successor"] = second_successor.run_id

    asyncio.run(scenario())
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_submissions)
            ).scalar_one()
            == 4
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_enqueue_outbox)
            ).scalar_one()
            == 4
        )
        raw = "|".join(
            str(value) for row in conn.execute(sa.select(ne_agent_run_submissions)) for value in row
        )
        raw += "|" + "|".join(
            str(value)
            for row in conn.execute(sa.select(ne_agent_run_enqueue_outbox))
            for value in row
        )
        assert "PRIVATE accepted submission sentinel" not in raw
        input_rows = conn.execute(sa.select(ne_agent_run_inputs)).all()
        assert len(input_rows) == 3
        assert all(
            "PRIVATE accepted submission sentinel" not in "|".join(map(str, row))
            for row in input_rows
        )
        lineages = conn.execute(
            sa.select(ne_agent_run_retry_lineage).order_by(
                ne_agent_run_retry_lineage.c.retry_ordinal
            )
        ).all()
        assert [row.predecessor_run_id for row in lineages] == [
            identities["predecessor"],
            identities["successor"],
        ]
        assert [row.successor_run_id for row in lineages] == [
            identities["successor"],
            identities["second_successor"],
        ]
        retry_id = str(lineages[0].retry_id)
    with (
        engine.begin() as conn,
        pytest.raises(IntegrityError, match="Run retry lineage is immutable"),
    ):
        conn.execute(
            ne_agent_run_retry_lineage.update()
            .where(ne_agent_run_retry_lineage.c.retry_id == retry_id)
            .values(retry_ordinal=99)
        )
    with (
        engine.begin() as conn,
        pytest.raises(IntegrityError, match="Run retry lineage is immutable"),
    ):
        conn.execute(
            ne_agent_run_retry_lineage.delete().where(
                ne_agent_run_retry_lineage.c.retry_id == retry_id
            )
        )
    engine.dispose()


def test_hardship_canonical_inputs_fit_exactly_and_one_extra_byte_rolls_back() -> None:
    engine, sessions, runs = _stores()
    exact_budget = CONTINUITY_PROMPT_RESERVE_TOKENS + len(b'{"max_subjects":1}')
    assert exact_budget == 8210

    async def scenario() -> None:
        for max_subjects in range(1, 6):
            conversation = await sessions.create_conversation(
                "agt_01k3hardshipreview0000000000",
                "operations",
                "user:ops",
            )
            input_plaintext = f'{{"max_subjects":{max_subjects}}}'.encode()
            accepted = await runs.admit_start(
                _model_bound_start_command(
                    idempotency_key=f"hardship-{max_subjects}",
                    request_hash=hashlib.sha256(input_plaintext).hexdigest(),
                    conversation_id=conversation.conversation_id,
                    expected_version=0,
                    owner_principal_id="user:ops",
                    department="operations",
                    conversation_agent_name="agt_01k3hardshipreview0000000000",
                    agent_id="agt_01k3hardshipreview0000000000",
                    agent_version=4,
                    strategy="deterministic",
                    config_hash="1" * 64,
                    capability_snapshot_hash="2" * 64,
                    capability_snapshot_receipt_id="capability-hardship",
                    model_policy_id="agent.meridian-hardship-review.v3",
                    target_input_budget=exact_budget,
                    input_plaintext=input_plaintext,
                )
            )
            with engine.connect() as conn:
                receipt = conn.execute(
                    sa.select(ne_agent_model_boundary_receipts).where(
                        ne_agent_model_boundary_receipts.c.run_id == accepted.run_id
                    )
                ).one()
            assert receipt.estimated_input_tokens == receipt.target_input_budget == exact_budget

        rejected_conversation = await sessions.create_conversation(
            "agt_01k3hardshipreview0000000000",
            "operations",
            "user:ops",
        )
        over_budget = b'{"max_subjects":10}'
        with pytest.raises(
            RunSubmissionConflictError,
            match="unavailable governed compaction",
        ):
            await runs.admit_start(
                _model_bound_start_command(
                    idempotency_key="hardship-over-budget",
                    request_hash=hashlib.sha256(over_budget).hexdigest(),
                    conversation_id=rejected_conversation.conversation_id,
                    expected_version=0,
                    owner_principal_id="user:ops",
                    department="operations",
                    conversation_agent_name="agt_01k3hardshipreview0000000000",
                    agent_id="agt_01k3hardshipreview0000000000",
                    agent_version=4,
                    strategy="deterministic",
                    config_hash="1" * 64,
                    capability_snapshot_hash="2" * 64,
                    capability_snapshot_receipt_id="capability-hardship",
                    model_policy_id="agent.meridian-hardship-review.v3",
                    target_input_budget=exact_budget,
                    input_plaintext=over_budget,
                )
            )

    asyncio.run(scenario())
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(ne_agent_runs)).scalar_one() == 5
    engine.dispose()


def _target(
    conversation_id: str,
    run_id: str,
    attempt_id: str,
    *,
    turn_id: str = "turn-1",
    turn_sequence: int = 1,
    attachment_id: str = "attachment-1",
) -> CanonicalTargetTuple:
    return CanonicalTargetTuple(
        conversation_id=conversation_id,
        run_id=run_id,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        attachment_id=attachment_id,
        attempt_id=attempt_id,
    )


async def _claim_turn_content(
    runs: AgentRunStore,
    target: CanonicalTargetTuple,
    *,
    owner: str = "alice",
    plaintext: bytes = b"the assistant said this",
    source_stamps: tuple[SourceAuthorityStamp, ...] = (),
    content_status: ProtectedContentStatus | None = None,
    run_id: str | None = None,
):
    """A TUPLED agent_run_turn protected row addressed by the canonical tuple.

    ``content_status`` defaults to the only status legal for the stamps supplied,
    so a caller must go out of its way to build an illegal pair.
    """
    if content_status is None:
        content_status = (
            ProtectedContentStatus.SOURCE_BOUND_CONTAINER
            if source_stamps
            else ProtectedContentStatus.SOURCE_FREE
        )
    return await runs.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=target.run_id if run_id is None else run_id,
        owner_principal_id=owner,
        target_kind=AGENT_RUN_TURN_TARGET_KIND,
        target=target,
        content_status=content_status,
        plaintext=plaintext,
        source_stamps=source_stamps,
    )


def test_create_run_is_owner_bound_requires_real_snapshot_and_encrypts_input() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        assert run.sequence == 1
        assert run.display_label.startswith("Run 1 · Agentic ·")
        assert run.capability_snapshot_hash == "2" * 64
        assert await runs.get_run(run_id=run.run_id, owner_principal_id="alice") == run
        assert await runs.get_run(run_id=run.run_id, owner_principal_id="bob") is None
        assert (
            await runs.materialize_run_input(run_id=run.run_id, owner_principal_id="alice")
            == "RunInput prompt sentinel — private".encode()
        )
        with pytest.raises(LookupError, match="unavailable"):
            await runs.materialize_run_input(run_id=run.run_id, owner_principal_id="bob")

        with pytest.raises(ValueError, match="capability_snapshot_hash"):
            await runs.create_run(
                conversation_id=conversation.conversation_id,
                owner_principal_id="alice",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="placeholder",
                capability_snapshot_receipt_id="cap-receipt-1",
                input_plaintext=b"prompt",
            )
        with pytest.raises(LookupError, match="unknown Conversation"):
            await runs.create_run(
                conversation_id=conversation.conversation_id,
                owner_principal_id="bob",
                department="claims",
                conversation_agent_name="assistant",
                agent_id="agt_01",
                agent_version=7,
                strategy="agentic",
                config_hash="1" * 64,
                capability_snapshot_hash="2" * 64,
                capability_snapshot_receipt_id="cap-receipt-1",
                input_plaintext=b"prompt",
            )

    asyncio.run(scenario())
    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_agent_run_inputs)).one()
        raw = "|".join(str(value) for value in row)
        assert "RunInput prompt sentinel" not in raw
        assert len(str(row.plaintext_hash)) == 64
    engine.dispose()


def test_state_machine_terminal_immutability_and_retry_new_attempt_identity() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        first = await runs.link_attempt(run_id=run.run_id)
        second = await runs.link_attempt(run_id=run.run_id, predecessor_attempt_id=first.attempt_id)
        assert second.attempt_id != first.attempt_id
        assert second.predecessor_attempt_id == first.attempt_id
        assert second.attempt_number == 2
        assert (
            await runs.get_attempt(
                run_id=run.run_id,
                attempt_id=second.attempt_id,
                owner_principal_id="alice",
            )
            == second
        )
        assert (
            await runs.get_attempt(
                run_id=run.run_id,
                attempt_id=second.attempt_id,
                owner_principal_id="bob",
            )
            is None
        )
        with pytest.raises(ValueError, match="exact current"):
            await runs.link_attempt(run_id=run.run_id, predecessor_attempt_id=first.attempt_id)

        queued = await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        running = await runs.transition(
            run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING
        )
        assert (queued.status, running.status) == (RunState.QUEUED, RunState.RUNNING)
        with pytest.raises(ValueError, match="lifecycle/finalizer writer"):
            await runs.transition(
                run_id=run.run_id,
                expected=RunState.RUNNING,
                target=RunState.COMPLETED,
            )
        with engine.begin() as conn:
            commit_run_terminal_in_transaction(
                conn,
                run_id=run.run_id,
                attempt_id=second.attempt_id,
                status=RunState.COMPLETED,
                outcome="execution_complete",
                output_verdict=RunOutputVerdict.UNAVAILABLE,
                allowed_from={RunState.RUNNING: frozenset({RunState.COMPLETED})},
                step_id="test-terminal",
                now=datetime.now(UTC),
            )
        completed = await runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert completed is not None
        assert completed.terminal_at is not None
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="terminal Run is immutable"),
        ):
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == run.run_id)
                .values(display_label="overwritten")
            )
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="Run deletion is forbidden"),
        ):
            conn.execute(ne_agent_runs.delete().where(ne_agent_runs.c.run_id == run.run_id))
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="lifecycle outbox intent is immutable"),
        ):
            conn.execute(
                ne_agent_run_lifecycle_outbox.update()
                .where(ne_agent_run_lifecycle_outbox.c.run_id == run.run_id)
                .values(outcome="refused")
            )
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="lifecycle outbox deletion is forbidden"),
        ):
            conn.execute(
                ne_agent_run_lifecycle_outbox.delete().where(
                    ne_agent_run_lifecycle_outbox.c.run_id == run.run_id
                )
            )
        with pytest.raises(ValueError, match="illegal Run transition"):
            await runs.transition(
                run_id=run.run_id, expected=RunState.COMPLETED, target=RunState.RUNNING
            )

        _, direct = await _create_run(sessions, runs)
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="illegal Run state transition"),
        ):
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == direct.run_id)
                .values(status=RunState.RUNNING.value)
            )
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="output verdict does not match lifecycle"),
        ):
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == direct.run_id)
                .values(output_verdict="refused")
            )
        await runs.transition(
            run_id=direct.run_id,
            expected=RunState.SUBMITTED,
            target=RunState.QUEUED,
        )
        await runs.transition(
            run_id=direct.run_id,
            expected=RunState.QUEUED,
            target=RunState.RUNNING,
        )
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="requires atomic lifecycle outbox"),
        ):
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == direct.run_id)
                .values(
                    status=RunState.FAILED.value,
                    output_verdict=RunOutputVerdict.UNAVAILABLE.value,
                    terminal_at=datetime.now(UTC),
                )
            )

    asyncio.run(scenario())
    engine.dispose()


def test_attempt_lineage_rows_reject_database_update_and_delete() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        where = ne_agent_run_attempt_links.c.attempt_id == attempt.attempt_id
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="Run attempt lineage is immutable"),
        ):
            conn.execute(ne_agent_run_attempt_links.update().where(where).values(attempt_number=2))
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="Run attempt lineage is immutable"),
        ):
            conn.execute(ne_agent_run_attempt_links.delete().where(where))
        assert (
            await runs.get_attempt(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                owner_principal_id="alice",
            )
            == attempt
        )

    asyncio.run(scenario())
    engine.dispose()


def test_sequential_evidence_replay_returns_the_winner_and_locator_unavailable_is_explicit() -> (
    None
):
    """Sequential replay and locator-status shape ONLY — not atomicity.

    The previous name claimed the claim path was atomic, but this body replays on
    in-memory SQLite, whose global writer lock serializes the two transactions
    whose interleaving is the actual hazard: the fixture cannot produce the race
    it named. The concurrency claim now lives where it can fail —
    ``test_run_store_postgres.py``'s barrier-released same-invocation proof, with
    the unlocked ``max + 1`` red control beside it.
    """
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        declaration = EvidenceDeclaration(
            turn_id="turn-1",
            step_id="step-1",
            tool_call_id="tool-call-1",
            read_ordinal=0,
            content_hash="3" * 64,
            safe_event_detail={"evidence_status": "located"},
            locator_plaintext=b"locator source sentinel and page coordinates",
            pending_result_plaintext=b"protected tool result sentinel",
            source_stamps=(_stamp(),),
        )
        first = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=declaration,
        )
        replay = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=declaration,
        )
        assert replay == first
        assert first.locator_status == "located"
        stored = await runs.load_pending_source_tool_result(
            reference_id=first.pending_result_id, owner_principal_id="alice"
        )
        assert stored.plaintext == b"protected tool result sentinel"
        assert stored.evidence_ids == ()

        unavailable = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=EvidenceDeclaration(
                turn_id="turn-1",
                step_id="step-1",
                tool_call_id="tool-call-2",
                read_ordinal=1,
                content_hash="4" * 64,
                safe_event_detail={"evidence_status": "evidence_locator_unavailable"},
                locator_plaintext=None,
                pending_result_plaintext=b"protected result without locator",
                source_stamps=(_stamp(),),
            ),
        )
        assert unavailable.locator_status == "evidence_locator_unavailable"
        assert unavailable.locator_id is None
        page = await runs.list_events(run_id=run.run_id, owner_principal_id="alice", limit=1)
        assert len(page.events) == 1
        assert page.next_sequence == 1
        second_page = await runs.list_events(
            run_id=run.run_id,
            owner_principal_id="alice",
            after_sequence=page.next_sequence,
            limit=10,
        )
        assert [event.sequence for event in second_page.events] == [2]
        assert not (await runs.list_events(run_id=run.run_id, owner_principal_id="bob")).events
        audit_receipt = await runs.record_audit_receipt(
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            run_event_id=first.producing_event_id,
            decision_kind=RunAuditDecisionKind.EVIDENCE,
            audit_event_id="audit-event-1",
            audit_sequence=42,
            audit_record_hash="e" * 64,
            config_hash="1" * 64,
            owner_principal_id="alice",
            step_id="step-1",
            tool_call_id="tool-call-1",
        )
        assert (
            await runs.record_audit_receipt(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                run_event_id=first.producing_event_id,
                decision_kind=RunAuditDecisionKind.EVIDENCE,
                audit_event_id="audit-event-1",
                audit_sequence=42,
                audit_record_hash="e" * 64,
                config_hash="1" * 64,
                owner_principal_id="alice",
                step_id="step-1",
                tool_call_id="tool-call-1",
            )
            == audit_receipt
        )

    asyncio.run(scenario())
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_evidence_records)
            ).scalar_one()
            == 2
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_audit_receipts)
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_evidence_locators)
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_pending_source_tool_results)
            ).scalar_one()
            == 2
        )
        raw = "|".join(
            str(value)
            for row in conn.execute(sa.select(ne_agent_evidence_locators))
            for value in row
        )
        assert "locator source sentinel" not in raw
    engine.dispose()


def test_run_claim_evidence_loader_and_verifier_are_exact_and_replay_safe() -> None:
    engine, sessions, runs = _stores()
    protected = (
        b"[showing 1 of 1 retrieved chunks]\nchunk-a: Meridian hardship support is available"
    )
    locator = json.dumps(
        {
            "kind": "knowledge_search",
            "contributors": [{"source": "m365", "revision": "revision-1"}],
            "chunk_ids": ["chunk-a"],
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        evidence = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=EvidenceDeclaration(
                turn_id="turn-1",
                step_id="step-1",
                tool_call_id="tool-call-1",
                read_ordinal=0,
                content_hash=hashlib.sha256(protected).hexdigest(),
                safe_event_detail={
                    "read_kind": "search",
                    "evidence_status": "located",
                    "contributor_count": 1,
                    "evidence_assurance": "direct",
                },
                locator_plaintext=locator,
                pending_result_plaintext=protected,
                source_stamps=(_stamp(),),
            ),
        )
        await runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref=evidence.pending_result_id,
            protected_hash=hashlib.sha256(protected).hexdigest(),
            source_stamps=(_stamp(),),
            receiving_attempt_id=attempt.attempt_id,
        )
        protected_ref = encode_protected_reference(
            SourceBoundToolResultRef(
                reference_id=evidence.pending_result_id,
                result_hash=hashlib.sha256(protected).hexdigest(),
                stamps=canonical_source_authority_stamps((_stamp(),)),
            )
        )
        observation = await runs.load_run_claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            turn_id="turn-1",
            protected_ref=protected_ref,
        )
        # A single-record read stays a single record: the load is shaped by the
        # server's own evidence-set row, and this pending result has none.
        assert isinstance(observation, RunClaimEvidenceObservation)
        assert observation.evidence_id == evidence.evidence_id
        assert observation.protected_rendering == protected
        assert observation.locator == locator
        assert observation.read_kind == "search"
        assert observation.assurance == "direct"
        # NE-REF-WIDTH: the observation carries the reference's ID, not the
        # reference. `protected_ref` means the id everywhere else in this family
        # — `claim_delivery` above was called with `evidence.pending_result_id`,
        # and `run_finalizer` uses that value as the `pending_result_id` key —
        # and this site used to echo the caller's argument instead.
        assert observation.protected_ref == evidence.pending_result_id
        assert observation.protected_ref != protected_ref
        # It round-trips into the typed model that consumes it. The full
        # reference does not fit and never could: that field is `OpaqueId`, the
        # same bound `_StampedReference.reference_id` already declares.
        typed = DirectSearchEvidenceObservation(
            protected_ref=observation.protected_ref,
            protected_result_hash=observation.protected_result_hash,
            evidence_id=observation.evidence_id,
            locator_id=observation.locator_id,
            locator_hash=observation.locator_hash,
            producing_event_id=observation.producing_event_id,
            delivery_receipt_id=observation.delivery_receipt_id,
            delivery_receipt_hash=observation.delivery_receipt_hash,
            protected_rendering=observation.protected_rendering,
            locator=observation.locator,
        )
        assert typed.protected_ref == evidence.pending_result_id
        assert len(protected_ref) > 512  # the reference this run actually served
        # A reference that does not decode is still refused, and a TRUNCATED one
        # is exactly that — never silently bound to whatever it prefixes.
        for broken in (protected_ref[:-40], "ne-source-ref:v1:not-a-reference"):
            with pytest.raises((LookupError, MalformedProtectedReferenceError)):
                await runs.load_run_claim_evidence(
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    attempt_id=attempt.attempt_id,
                    turn_id="turn-1",
                    protected_ref=broken,
                )
        with pytest.raises(LookupError, match="unavailable"):
            await runs.load_run_claim_evidence(
                run_id=run.run_id,
                owner_principal_id="mallory",
                attempt_id=attempt.attempt_id,
                turn_id="turn-1",
                protected_ref=protected_ref,
            )

        verifier = await runs.claim_run_claim_verifier(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            claim_key="claim-local-1",
            claim_content_hash="4" * 64,
            served_output_hash="5" * 64,
            span_start_byte=0,
            span_end_byte=17,
            verifier_method="ne.grounding.claim",
            verifier_version="v1/ng2",
            result_hash="6" * 64,
        )
        assert (
            await runs.claim_run_claim_verifier(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                claim_key="claim-local-1",
                claim_content_hash="4" * 64,
                served_output_hash="5" * 64,
                span_start_byte=0,
                span_end_byte=17,
                verifier_method="ne.grounding.claim",
                verifier_version="v1/ng2",
                result_hash="6" * 64,
            )
            == verifier
        )
        with pytest.raises(ValueError, match="changed its exact intent"):
            await runs.claim_run_claim_verifier(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                claim_key="claim-local-1",
                claim_content_hash="4" * 64,
                served_output_hash="5" * 64,
                span_start_byte=1,
                span_end_byte=17,
                verifier_method="ne.grounding.claim",
                verifier_version="v1/ng2",
                result_hash="6" * 64,
            )

    asyncio.run(scenario())
    engine.dispose()


def test_terminal_claim_transaction_stores_no_display_text_and_unavailable_is_forced() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        protected = await _claim_turn_content(runs, target)
        claim = ClaimDeclaration(
            claim_key="claim-local-1",
            claim_index=0,
            claim_verdict="unavailable",
            display_state="protected_source_slice",
            protected_ref=protected.row_id,
            served_output_hash="5" * 64,
            span_start_byte=7,
            span_end_byte=29,
            claim_content_hash="6" * 64,
            association_status="association_unavailable",
        )
        ids = await runs.commit_terminal_claims(
            run_id=run.run_id,
            owner_principal_id="alice",
            turn_id="turn-1",
            attempt_id=attempt.attempt_id,
            target=target,
            claims=(claim,),
            terminal_status=RunState.COMPLETED,
        )
        assert len(ids) == 1
        assert (
            await runs.commit_terminal_claims(
                run_id=run.run_id,
                owner_principal_id="alice",
                turn_id="turn-1",
                attempt_id=attempt.attempt_id,
                target=target,
                claims=(claim,),
                terminal_status=RunState.COMPLETED,
            )
            == ids
        )

        bad = ClaimDeclaration(**{**claim.__dict__, "claim_verdict": "grounded"})
        other_conversation, other = await _create_run(sessions, runs)
        other_attempt = await runs.link_attempt(run_id=other.run_id)
        other_target = _target(
            other_conversation.conversation_id,
            other.run_id,
            other_attempt.attempt_id,
            turn_id="turn-2",
        )
        with pytest.raises(ValueError, match="unassociated claim"):
            await runs.commit_terminal_claims(
                run_id=other.run_id,
                owner_principal_id="alice",
                turn_id="turn-2",
                attempt_id=other_attempt.attempt_id,
                target=other_target,
                claims=(bad,),
                terminal_status=RunState.FAILED,
            )

    asyncio.run(scenario())
    with engine.connect() as conn:
        columns = {
            column["name"] for column in sa.inspect(conn).get_columns(ne_agent_run_claims.name)
        }
        assert "display" not in columns
        assert "claim_text" not in columns
        assert (
            conn.execute(sa.select(ne_agent_run_claims.c.claim_verdict)).scalar_one()
            == "unavailable"
        )
    engine.dispose()


def test_two_valid_same_run_evidence_rows_cannot_be_swapped_between_claims() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        protected = await _claim_turn_content(runs, target)
        records = []
        for ordinal, label in enumerate(("a", "b")):
            records.append(
                await runs.claim_evidence(
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    attempt_id=attempt.attempt_id,
                    declaration=EvidenceDeclaration(
                        turn_id="turn-1",
                        step_id="step-1",
                        tool_call_id=f"tool-{label}",
                        read_ordinal=ordinal,
                        content_hash=("7" if label == "a" else "8") * 64,
                        safe_event_detail={"evidence_status": "located", "ordinal": ordinal},
                        locator_plaintext=f"locator-{label}".encode(),
                        pending_result_plaintext=f"result-{label}".encode(),
                        source_stamps=(_stamp(),),
                    ),
                )
            )
        locator_hashes: dict[str, str] = {}
        with engine.connect() as conn:
            for record in records:
                assert record.locator_id is not None
                locator_hashes[record.evidence_id] = str(
                    conn.execute(
                        sa.select(ne_agent_evidence_locators.c.locator_hash).where(
                            ne_agent_evidence_locators.c.locator_id == record.locator_id
                        )
                    ).scalar_one()
                )
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        claims = tuple(
            ClaimDeclaration(
                claim_key=f"claim-{label}",
                claim_index=index,
                claim_verdict="grounded",
                display_state="protected_source_slice",
                protected_ref=protected.row_id,
                served_output_hash="9" * 64,
                span_start_byte=index * 8,
                span_end_byte=index * 8 + 7,
                claim_content_hash=("a" if label == "a" else "b") * 64,
                association_status="associated",
            )
            for index, label in enumerate(("a", "b"))
        )
        swapped = (
            ClaimEvidenceDeclaration(
                claim_key="claim-a",
                evidence_id=records[0].evidence_id,
                locator_hash=locator_hashes[records[1].evidence_id],
                served_citation_ref_hash="c" * 64,
                producing_event_id=records[0].producing_event_id,
            ),
            ClaimEvidenceDeclaration(
                claim_key="claim-b",
                evidence_id=records[1].evidence_id,
                locator_hash=locator_hashes[records[0].evidence_id],
                served_citation_ref_hash="d" * 64,
                producing_event_id=records[1].producing_event_id,
            ),
        )
        with pytest.raises(ValueError, match="exact same-Run locator/event"):
            await runs.commit_terminal_claims(
                run_id=run.run_id,
                owner_principal_id="alice",
                turn_id="turn-1",
                attempt_id=attempt.attempt_id,
                target=target,
                claims=claims,
                associations=swapped,
                terminal_status=RunState.COMPLETED,
            )
        exact = tuple(
            ClaimEvidenceDeclaration(
                claim_key=f"claim-{label}",
                evidence_id=record.evidence_id,
                locator_hash=locator_hashes[record.evidence_id],
                served_citation_ref_hash=("c" if label == "a" else "d") * 64,
                producing_event_id=record.producing_event_id,
            )
            for label, record in zip(("a", "b"), records, strict=True)
        )
        assert (
            len(
                await runs.commit_terminal_claims(
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    turn_id="turn-1",
                    attempt_id=attempt.attempt_id,
                    target=target,
                    claims=claims,
                    associations=exact,
                    terminal_status=RunState.COMPLETED,
                )
            )
            == 2
        )

    asyncio.run(scenario())
    engine.dispose()


def test_exact_target_protected_rows_claim_load_and_rotation_cover_all_seven_domains() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        evidence = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=EvidenceDeclaration(
                turn_id="turn-1",
                step_id="step-1",
                tool_call_id="tool-1",
                read_ordinal=0,
                content_hash="7" * 64,
                safe_event_detail={"evidence_status": "located"},
                locator_plaintext=b"locator-seven-domain-sentinel",
                pending_result_plaintext=b"pending-seven-domain-sentinel",
                source_stamps=(_stamp(),),
            ),
        )
        assert evidence.locator_id is not None
        target_cases = (
            (
                PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                "conversation_turn",
                (run.conversation_id, run.run_id, "turn-1", attempt.attempt_id),
                b"turn-content-seven-domain-sentinel",
                (),
            ),
            (
                PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
                "assistant_turn",
                (run.run_id, "turn-1", "model-call-1"),
                b"source-output-seven-domain-sentinel",
                (_stamp(),),
            ),
            (
                PrivatePayloadDomain.PROTECTED_SOURCE_INVOCATION,
                "approval_action",
                (run.run_id, attempt.attempt_id, "step-1", "tool-1", "approval-1"),
                b"approval-args-seven-domain-sentinel",
                (_stamp(),),
            ),
            (
                PrivatePayloadDomain.PROTECTED_WORKFLOW_STATE,
                "agent_run",
                (run.run_id, "workflow-1", "step-1", "publication-1"),
                b"workflow-state-seven-domain-sentinel",
                (_stamp(),),
            ),
        )
        refs = []
        for domain, target_kind, binding, plaintext, stamps in target_cases:
            # Only the ProtectedTurnContent table carries an authenticated status.
            status = (
                ProtectedContentStatus.SOURCE_FREE
                if domain is PrivatePayloadDomain.PROTECTED_TURN_CONTENT
                else None
            )
            claim_kwargs: dict[str, object] = {
                "domain": domain,
                "run_id": run.run_id,
                "owner_principal_id": "alice",
                "target_kind": target_kind,
                "target_binding": binding,
                "content_status": status,
                "plaintext": plaintext,
                "source_stamps": stamps,
            }
            ref = await runs.claim_protected_payload(**claim_kwargs)  # type: ignore[arg-type]
            replay = await runs.claim_protected_payload(**claim_kwargs)  # type: ignore[arg-type]
            assert replay == ref
            refs.append(ref)

        before = {}
        tables = (
            ne_agent_run_inputs,
            ne_agent_evidence_locators,
            ne_agent_pending_source_tool_results,
            ne_agent_protected_turn_contents,
            ne_agent_protected_source_outputs,
            ne_agent_protected_source_invocations,
            ne_agent_protected_workflow_states,
        )
        with engine.connect() as conn:
            for table in tables:
                row = conn.execute(
                    sa.select(table.c.payload_ciphertext, table.c.plaintext_hash, table.c.key_id)
                ).one()
                before[table.name] = tuple(row)
        assert await runs.key_reference_counts() == {"k1": 7}
        # The SYNCHRONOUS readiness seam reports the same population.
        before_rotation = runs.current_key_readiness()
        assert before_rotation.current_key_id == "k1"
        assert before_rotation.rows_under_current_key == 7
        assert before_rotation.rows_under_other_keys == 0
        assert before_rotation.ready is True
        assert (
            await runs.rewrap_private_payload_keys(expected_old_key_id="k1", target_key_id="k2")
            == 7
        )
        assert await runs.key_reference_counts() == {"k2": 7}
        # The cipher still mints under k1, so with the STORE rotated to k2 the seam
        # must report NOT ready. A probe that cannot say "the deployment's current
        # key is not the one the rows are under" is useless for readiness.
        after_rotation = runs.current_key_readiness()
        assert after_rotation.current_key_id == "k1"
        assert after_rotation.rows_under_current_key == 0
        assert after_rotation.rows_under_other_keys == 7
        assert after_rotation.ready is False
        assert after_rotation.reference_counts == {"k2": 7}
        with engine.connect() as conn:
            for table in tables:
                row = conn.execute(
                    sa.select(table.c.payload_ciphertext, table.c.plaintext_hash, table.c.key_id)
                ).one()
                assert tuple(row[:2]) == before[table.name][:2]
                assert row.key_id == "k2"
        # Rotation is a REWRAP: the ciphertext and plaintext hash comparisons above
        # are byte-identical, which is what proves the payload survived without this
        # test ever needing a generic decryptor to look inside it.
        assert len(refs) == len(target_cases)

    asyncio.run(scenario())
    engine.dispose()


async def _seed_legacy_turn(
    sessions: SqlAlchemySessionStore, runs: AgentRunStore
) -> tuple[str, str, str, dict[str, str]]:
    """Legacy plaintext turn + the two protected rows a lawful migration needs."""
    conversation, run = await _create_run(sessions, runs)
    attempt = await runs.link_attempt(run_id=run.run_id)
    refs: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for slot, plaintext in (
        ("input", b"PRIVATE-CONTEXT-SENTINEL"),
        ("terminal", b"SERVED-OUTPUT-SENTINEL and CITATION-SENTINEL"),
    ):
        claimed = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=run.run_id,
            owner_principal_id="alice",
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            target_binding=conversation_turn_content_binding(
                conversation_id=conversation.conversation_id,
                attachment_id="attachment-1",
                attempt_id=attempt.attempt_id,
                slot=slot,  # type: ignore[arg-type]
            ),
            plaintext=plaintext,
        )
        refs[slot] = claimed.row_id
        hashes[slot] = claimed.plaintext_hash
    await sessions.append_conversation_turn(
        conversation.conversation_id,
        "alice",
        "claims",
        "assistant",
        [ModelRequest(parts=[UserPromptPart(content="PRIVATE-CONTEXT-SENTINEL")])],
        served_output="SERVED-OUTPUT-SENTINEL",
        trace_id="trace-1",
        attachment_id="attachment-1",
        attempt_id=attempt.attempt_id,
        citation_payloads=({"ref": "CITATION-SENTINEL", "title": "secret"},),
    )
    return (
        conversation.conversation_id,
        attempt.attempt_id,
        run.run_id,
        {**refs, **{f"{slot}_hash": value for slot, value in hashes.items()}},
    )


def _turn_migration_kwargs(
    conversation_id: str, attempt_id: str, claimed: dict[str, str], run_id: str
) -> dict[str, str]:
    return {
        "conversation_id": conversation_id,
        "owner_principal_id": "alice",
        "run_id": run_id,
        "attachment_id": "attachment-1",
        "attempt_id": attempt_id,
        "input_status": "source_free",
        "protected_input_ref": claimed["input"],
        "protected_input_hash": claimed["input_hash"],
        "terminal_status": "source_free",
        "protected_terminal_ref": claimed["terminal"],
        "protected_terminal_hash": claimed["terminal_hash"],
    }


def _turn_plaintext(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_agent_conversation_turns)).one()
    return {
        "private_context": str(row.private_context),
        "served_output": str(row.served_output),
        "citation_refs": str(row.citation_refs),
        "input_status": "" if row.input_status is None else str(row.input_status),
        "protected_input_ref": (
            "" if row.protected_input_ref is None else str(row.protected_input_ref)
        ),
    }


def test_conversation_turn_migration_erases_plaintext_and_is_retry_idempotent() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        kwargs = _turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        before = _turn_plaintext(engine)
        assert before["private_context"] != ""
        await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        migrated = _turn_plaintext(engine)
        # Retrying the SAME lawful migration converges on the same row, byte for byte.
        await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        assert _turn_plaintext(engine) == migrated

    asyncio.run(scenario())
    with engine.connect() as conn:
        turn = conn.execute(sa.select(ne_agent_conversation_turns)).one()
        assert turn.input_status == "source_free"
        assert turn.terminal_status == "source_free"
        assert turn.private_context == ""
        assert turn.served_output == ""
        assert turn.citation_refs == "[]"
        assert turn.served_citations == "[]"
        assert turn.served_verified == ""
        assert turn.served_grounded == ""
        raw = "|".join(str(value) for value in turn)
        assert "PRIVATE-CONTEXT-SENTINEL" not in raw
        assert "SERVED-OUTPUT-SENTINEL" not in raw
        assert "CITATION-SENTINEL" not in raw
    engine.dispose()


def test_aborted_migrated_claim_retries_under_a_fresh_unprotected_epoch() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        await runs.migrate_conversation_turn_to_protected_refs(  # type: ignore[arg-type]
            **_turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        )
        with engine.begin() as conn:
            row = conn.execute(
                sa.select(
                    ne_agent_conversation_turns.c.request_fingerprint,
                    ne_agent_conversation_turns.c.trace_id,
                ).where(ne_agent_conversation_turns.c.conversation_id == conversation_id)
            ).one()
            conn.execute(
                ne_agent_conversation_turns.update()
                .where(ne_agent_conversation_turns.c.conversation_id == conversation_id)
                .values(publication_state="aborted")
            )
        retry = await sessions.claim_conversation_attachment(
            conversation_id,
            "alice",
            "claims",
            "assistant",
            "attachment-1",
            str(row.request_fingerprint),
            str(row.trace_id),
        )
        assert retry.disposition == "owned"
        assert retry.attempt_id != attempt_id

    asyncio.run(scenario())
    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_agent_conversation_turns)).one()
    assert row.private_context == "[]"
    assert row.input_status is None and row.protected_input_ref is None
    assert row.terminal_status is None and row.protected_terminal_ref is None
    engine.dispose()


@pytest.mark.parametrize(
    ("defect", "overrides", "expected"),
    [
        # Refused by the PROTECTED-ROW verifier (reachable legacy row, wrong row).
        ("dangling_input_ref", {"protected_input_ref": "no-such-row"}, LookupError),
        ("dangling_terminal_ref", {"protected_terminal_ref": "no-such-row"}, LookupError),
        ("hash_mismatch_input", {"protected_input_hash": "0" * 64}, ValueError),
        ("hash_mismatch_terminal", {"protected_terminal_hash": "0" * 64}, ValueError),
        ("foreign_run", {"run_id": "run-that-is-not-this-turns"}, ValueError),
        # Refused by the LEGACY-ROW lock, whose WHERE filters owner/attachment/
        # attempt and so never reaches the verifier. Both routes preserve plaintext;
        # naming which guard fires keeps the next reader honest.
        ("foreign_owner", {"owner_principal_id": "mallory"}, LookupError),
        ("cross_target_attachment", {"attachment_id": "attachment-2"}, LookupError),
    ],
)
def test_conversation_turn_migration_rolls_back_and_preserves_plaintext(
    defect: str, overrides: dict[str, str], expected: type[Exception]
) -> None:
    """A migration that cannot prove its protected rows must not erase anything.

    Each case names a distinct rejected shape: a reference to nothing, to another
    principal's row, to a row addressing a different turn, and to a row holding
    different bytes. Every case additionally runs the unmutated call afterwards, so
    a passing row cannot be a fixture that never had plaintext to lose.
    """
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        kwargs = _turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        before = _turn_plaintext(engine)
        with pytest.raises(expected):
            await runs.migrate_conversation_turn_to_protected_refs(
                **{**kwargs, **overrides}  # type: ignore[arg-type]
            )
        assert _turn_plaintext(engine) == before
        assert before["private_context"] != ""
        # Positive control: the unmutated call on the SAME row DOES migrate, so the
        # preservation above is the verifier refusing, not a fixture that never wrote.
        await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        assert _turn_plaintext(engine)["private_context"] == ""

    asyncio.run(scenario())
    engine.dispose()


def test_conversation_turn_migration_rejects_a_row_claimed_for_the_other_slot() -> None:
    """The two slots of one turn are distinct targets, not one interchangeable ref."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        kwargs = _turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        swapped = {
            **kwargs,
            "protected_input_ref": claimed["terminal"],
            "protected_input_hash": claimed["terminal_hash"],
        }
        before = _turn_plaintext(engine)
        with pytest.raises(ValueError, match="different migration target"):
            await runs.migrate_conversation_turn_to_protected_refs(**swapped)  # type: ignore[arg-type]
        assert _turn_plaintext(engine) == before

    asyncio.run(scenario())
    engine.dispose()


async def _seed_legacy_session(
    sessions: SqlAlchemySessionStore, runs: AgentRunStore, *, owner: str = "alice"
) -> tuple[str, str, str]:
    session_id = await sessions.open_session("assistant", "legacy")
    await sessions.append_turn(
        session_id,
        [ModelRequest(parts=[UserPromptPart(content="LEGACY-PAYLOAD-SENTINEL")])],
    )
    claimed = await runs.claim_protected_payload(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        run_id=None,
        owner_principal_id=owner,
        target_kind=SESSION_MESSAGE_TARGET_KIND,
        content_status=ProtectedContentStatus.LEGACY_UNAVAILABLE,
        target_binding=session_message_content_binding(session_id=session_id, sequence=1),
        plaintext=b"LEGACY-PAYLOAD-SENTINEL",
    )
    return session_id, claimed.row_id, claimed.plaintext_hash


def _session_payload(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.select(ne_agent_session_messages.c.payload)).scalar_one())


def test_session_message_migration_erases_plaintext_once_its_protected_row_is_proven() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        session_id, ref, content_hash = await _seed_legacy_session(sessions, runs)
        assert "LEGACY-PAYLOAD-SENTINEL" in _session_payload(engine)
        await runs.migrate_session_message_to_protected_ref(
            session_id=session_id,
            sequence=1,
            owner_principal_id="alice",
            content_status="legacy_unavailable",
            protected_content_ref=ref,
            protected_content_hash=content_hash,
        )
        assert _session_payload(engine) == ""

    asyncio.run(scenario())
    with engine.connect() as conn:
        session = conn.execute(sa.select(ne_agent_session_messages)).one()
        assert session.content_status == "legacy_unavailable"
        assert "LEGACY-PAYLOAD-SENTINEL" not in "|".join(str(value) for value in session)
    engine.dispose()


@pytest.mark.parametrize(
    ("defect", "overrides", "expected"),
    [
        ("dangling_ref", {"protected_content_ref": "no-such-row"}, LookupError),
        ("hash_mismatch", {"protected_content_hash": "0" * 64}, ValueError),
        # Refused before the verifier: no such (session, seq) row to lock.
        ("cross_target_sequence", {"sequence": 2}, LookupError),
        ("foreign_owner", {"owner_principal_id": "mallory"}, ValueError),
    ],
)
def test_session_message_migration_rolls_back_and_preserves_plaintext(
    defect: str, overrides: dict[str, object], expected: type[Exception]
) -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        session_id, ref, content_hash = await _seed_legacy_session(sessions, runs)
        kwargs: dict[str, object] = {
            "session_id": session_id,
            "sequence": 1,
            "owner_principal_id": "alice",
            "content_status": "legacy_unavailable",
            "protected_content_ref": ref,
            "protected_content_hash": content_hash,
        }
        before = _session_payload(engine)
        with pytest.raises(expected):
            await runs.migrate_session_message_to_protected_ref(**{**kwargs, **overrides})  # type: ignore[arg-type]
        assert _session_payload(engine) == before
        assert "LEGACY-PAYLOAD-SENTINEL" in before
        await runs.migrate_session_message_to_protected_ref(**kwargs)  # type: ignore[arg-type]
        assert _session_payload(engine) == ""

    asyncio.run(scenario())
    engine.dispose()


def test_session_message_migration_refuses_a_message_attributed_to_another_principal() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        session_id = await sessions.open_session("assistant", "legacy")
        await sessions.append_turn(
            session_id,
            [ModelRequest(parts=[UserPromptPart(content="LEGACY-PAYLOAD-SENTINEL")])],
            principal_id="user:bob",
            trace_id="trace-bob",
        )
        claimed = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id="alice",
            target_kind=SESSION_MESSAGE_TARGET_KIND,
            content_status=ProtectedContentStatus.LEGACY_UNAVAILABLE,
            target_binding=session_message_content_binding(session_id=session_id, sequence=1),
            plaintext=b"LEGACY-PAYLOAD-SENTINEL",
        )
        with pytest.raises(ValueError, match="attributed to a different principal"):
            await runs.migrate_session_message_to_protected_ref(
                session_id=session_id,
                sequence=1,
                owner_principal_id="alice",
                content_status="legacy_unavailable",
                protected_content_ref=claimed.row_id,
                protected_content_hash=claimed.plaintext_hash,
            )
        assert "LEGACY-PAYLOAD-SENTINEL" in _session_payload(engine)

    asyncio.run(scenario())
    engine.dispose()


def test_ownerless_session_retirement_erases_payload_without_authoring_identity() -> None:
    engine, sessions, runs = _stores()
    fence = "7" * 64

    async def scenario() -> None:
        session_id = await sessions.open_session("assistant", "legacy")
        await sessions.append_turn(
            session_id,
            [ModelRequest(parts=[UserPromptPart(content="OWNERLESS-SENTINEL")])],
        )
        kwargs = {
            "session_id": session_id,
            "sequence": 1,
            "generation": LEGACY_TRANSPORT_GENERATION,
            "reason": OWNERLESS_RETIREMENT_REASON,
            "retirement_fence": fence,
        }
        with engine.connect() as conn:
            before_protected = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
            ).scalar_one()
            before_runs = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_runs)
            ).scalar_one()
            before_events = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_events)
            ).scalar_one()
        await runs.retire_ownerless_session_message(**kwargs)  # type: ignore[arg-type]
        await runs.retire_ownerless_session_message(**kwargs)  # exact replay
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_session_messages).where(
                    ne_agent_session_messages.c.session_id == session_id,
                    ne_agent_session_messages.c.seq == 1,
                )
            ).one()
            after_protected = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
            ).scalar_one()
            after_runs = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_runs)
            ).scalar_one()
            after_events = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_events)
            ).scalar_one()
        assert row.payload == ""
        assert row.principal_id is None and row.trace_id is None
        assert row.content_status is None
        assert row.protected_content_ref is None
        assert row.protected_content_hash is None
        assert (
            row.legacy_transport_retired,
            row.legacy_transport_generation,
            row.legacy_transport_reason,
            row.legacy_transport_fence,
        ) == (True, LEGACY_TRANSPORT_GENERATION, OWNERLESS_RETIREMENT_REASON, fence)
        assert (after_protected, after_runs, after_events) == (
            before_protected,
            before_runs,
            before_events,
        )

    asyncio.run(scenario())
    engine.dispose()


def test_ownerless_session_retirement_refuses_foreign_partial_and_changed_receipts() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        attributed = await sessions.open_session("assistant", "attributed")
        await sessions.append_turn(
            attributed,
            [ModelRequest(parts=[UserPromptPart(content="ATTRIBUTED-SENTINEL")])],
            principal_id="user:alice",
            trace_id="trace-alice",
        )
        base = {
            "sequence": 1,
            "generation": LEGACY_TRANSPORT_GENERATION,
            "reason": OWNERLESS_RETIREMENT_REASON,
            "retirement_fence": "8" * 64,
        }
        with pytest.raises(ValueError, match="attributed principal"):
            await runs.retire_ownerless_session_message(
                session_id=attributed,
                **base,  # type: ignore[arg-type]
            )
        assert "ATTRIBUTED-SENTINEL" in _session_payload(engine)

        ownerless = await sessions.open_session("assistant-2", "ownerless")
        await sessions.append_turn(
            ownerless,
            [ModelRequest(parts=[UserPromptPart(content="OWNERLESS-SENTINEL")])],
        )
        with engine.begin() as conn:
            conn.execute(
                ne_agent_session_messages.update()
                .where(ne_agent_session_messages.c.session_id == ownerless)
                .values(
                    payload="",
                    content_status="legacy_unavailable",
                    protected_content_ref="foreign-ref",
                    protected_content_hash="6" * 64,
                )
            )
        with pytest.raises(ValueError, match="partial or foreign protected tuple"):
            await runs.retire_ownerless_session_message(
                session_id=ownerless,
                **base,  # type: ignore[arg-type]
            )
        with engine.begin() as conn:
            conn.execute(
                ne_agent_session_messages.update()
                .where(ne_agent_session_messages.c.session_id == ownerless)
                .values(
                    content_status=None,
                    protected_content_ref=None,
                    protected_content_hash=None,
                )
            )
        await runs.retire_ownerless_session_message(
            session_id=ownerless,
            **base,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="different retirement receipt"):
            await runs.retire_ownerless_session_message(
                session_id=ownerless,
                **{**base, "retirement_fence": "9" * 64},  # type: ignore[arg-type]
            )

    asyncio.run(scenario())
    with (
        engine.begin() as conn,
        pytest.raises(IntegrityError, match="legacy retirement receipt is immutable"),
    ):
        conn.execute(
            ne_agent_session_messages.update()
            .where(ne_agent_session_messages.c.legacy_transport_retired.is_(True))
            .values(legacy_transport_fence="a" * 64)
        )
    engine.dispose()


def test_database_guard_rejects_partial_ownerless_retirement_receipt() -> None:
    engine, sessions, _runs = _stores()

    async def seed() -> str:
        session_id = await sessions.open_session("assistant", "ownerless")
        await sessions.append_turn(
            session_id,
            [ModelRequest(parts=[UserPromptPart(content="OWNERLESS-SENTINEL")])],
        )
        return session_id

    session_id = asyncio.run(seed())
    with (
        engine.begin() as conn,
        pytest.raises(IntegrityError, match="partial legacy retirement tuple"),
    ):
        conn.execute(
            ne_agent_session_messages.update()
            .where(ne_agent_session_messages.c.session_id == session_id)
            .values(legacy_transport_generation=LEGACY_TRANSPORT_GENERATION)
        )
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_session_messages).where(
                ne_agent_session_messages.c.session_id == session_id
            )
        ).one()
    assert "OWNERLESS-SENTINEL" in row.payload
    assert row.legacy_transport_retired is None
    engine.dispose()


@pytest.mark.parametrize(
    "invalid",
    [
        {"legacy_transport_retired": False},
        {"legacy_transport_generation": "agent-transport-v0"},
        {"legacy_transport_reason": "invented_reason"},
        {"legacy_transport_fence": "a" * 63},
        {"legacy_transport_fence": "A" * 64},
        {"legacy_transport_fence": "z" * 64},
        {"payload": "PLAINTEXT-MUST-NOT-SURVIVE"},
        {"principal_id": "user:invented"},
        {"trace_id": "trace-invented"},
        {"content_status": "legacy_unavailable"},
        {"protected_content_ref": "foreign-ref"},
        {"protected_content_hash": "b" * 64},
    ],
    ids=lambda invalid: next(iter(invalid)),
)
def test_sqlite_retirement_receipt_requires_every_exact_truth_clause(
    invalid: dict[str, object],
) -> None:
    engine, sessions, _runs = _stores()

    async def seed() -> str:
        session_id = await sessions.open_session("assistant", "ownerless")
        await sessions.append_turn(
            session_id,
            [ModelRequest(parts=[UserPromptPart(content="OWNERLESS-SENTINEL")])],
        )
        return session_id

    session_id = asyncio.run(seed())
    receipt: dict[str, object] = {
        "legacy_transport_retired": True,
        "legacy_transport_generation": LEGACY_TRANSPORT_GENERATION,
        "legacy_transport_reason": OWNERLESS_RETIREMENT_REASON,
        "legacy_transport_fence": "a" * 64,
        "payload": "",
        "principal_id": None,
        "trace_id": None,
        "content_status": None,
        "protected_content_ref": None,
        "protected_content_hash": None,
    }
    receipt.update(invalid)
    with (
        engine.begin() as conn,
        pytest.raises(IntegrityError, match="invalid legacy retirement receipt"),
    ):
        conn.execute(
            ne_agent_session_messages.update()
            .where(ne_agent_session_messages.c.session_id == session_id)
            .values(**receipt)
        )
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_session_messages).where(
                ne_agent_session_messages.c.session_id == session_id
            )
        ).one()
    assert "OWNERLESS-SENTINEL" in row.payload
    assert row.legacy_transport_retired is None
    engine.dispose()


def test_schema_is_idempotent_and_capability_snapshot_is_database_immutable() -> None:
    engine, sessions, runs = _stores()
    ensure_run_schema(engine)

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        with (
            engine.begin() as conn,
            pytest.raises(IntegrityError, match="Run capability snapshot is immutable"),
        ):
            # The product store exposes no capability-update method.  Database shape
            # still pins a valid non-null hash, while 011E is the sole producer.
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == run.run_id)
                .values(capability_snapshot_hash="f" * 64)
            )

    asyncio.run(scenario())
    engine.dispose()


def _evidence_declaration(**overrides: object) -> EvidenceDeclaration:
    base: dict[str, object] = {
        "turn_id": "turn-1",
        "step_id": "step-1",
        "tool_call_id": "tool-call-1",
        "read_ordinal": 0,
        "content_hash": "3" * 64,
        "safe_event_detail": {"evidence_status": "located"},
        "locator_plaintext": b"locator source sentinel",
        "pending_result_plaintext": b"protected tool result sentinel",
        "source_stamps": (_stamp(),),
    }
    return EvidenceDeclaration(**{**base, **overrides})  # type: ignore[arg-type]


def _other_stamp() -> SourceAuthorityStamp:
    original = _stamp()
    return SourceAuthorityStamp(
        **{**original.__dict__, "selected_head_revision": "etag-b"}
        if False
        else {
            "source_identity": original.source_identity,
            "artifact_identity": original.artifact_identity,
            "selected_head_revision": "etag-b",
            "acl_snapshot_hash": original.acl_snapshot_hash,
            "source_contract": original.source_contract,
            "contract_epoch": original.contract_epoch,
            "authority_receipt_id": original.authority_receipt_id,
            "authority_receipt_hash": original.authority_receipt_hash,
        }
    )


# One entry per NON-ADDRESS intent-bearing field of an evidence declaration.  A
# field dropped from the store's intent map makes exactly its row here stop
# raising, so this table is the replay contract's mutation surface.
_EVIDENCE_INTENT_CHANGES: tuple[tuple[str, dict[str, object]], ...] = (
    ("turn_id", {"turn_id": "turn-2"}),
    ("content_hash", {"content_hash": "9" * 64}),
    ("safe_event_detail", {"safe_event_detail": {"evidence_status": "located", "extra": 1}}),
    ("locator_plaintext", {"locator_plaintext": b"a different locator"}),
    ("locator_status", {"locator_plaintext": None}),
    ("pending_result_plaintext", {"pending_result_plaintext": b"a different result"}),
    ("source_stamps", {"source_stamps": (_other_stamp(),)}),
)


@pytest.mark.parametrize(
    ("field", "overrides"), _EVIDENCE_INTENT_CHANGES, ids=[n for n, _ in _EVIDENCE_INTENT_CHANGES]
)
def test_evidence_replay_changing_one_intent_field_fails_closed(
    field: str, overrides: dict[str, object]
) -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        first = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_evidence_declaration(),
        )
        with pytest.raises(ValueError, match="different declared intent"):
            await runs.claim_evidence(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                declaration=_evidence_declaration(**overrides),
            )
        # Positive control: the UNCHANGED replay still returns the winner, so the
        # raise above is this field discriminating, not a claim path that now
        # rejects every second call.
        assert (
            await runs.claim_evidence(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                declaration=_evidence_declaration(),
            )
            == first
        )

    asyncio.run(scenario())
    engine.dispose()


def test_evidence_replay_under_a_different_owner_fails_closed() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_evidence_declaration(),
        )
        with pytest.raises(ValueError, match="different declared intent"):
            await runs.claim_evidence(
                run_id=run.run_id,
                owner_principal_id="mallory",
                attempt_id=attempt.attempt_id,
                declaration=_evidence_declaration(),
            )

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("step_id", {"step_id": "step-2"}),
        ("tool_call_id", {"tool_call_id": "tool-call-2"}),
        ("read_ordinal", {"read_ordinal": 1}),
    ],
)
def test_evidence_claims_differing_in_one_address_field_are_separate_records(
    field: str, overrides: dict[str, object]
) -> None:
    """An address change is a NEW read, not a conflicting replay of the old one."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        first = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_evidence_declaration(),
        )
        second = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_evidence_declaration(**overrides),
        )
        assert second.evidence_id != first.evidence_id
        assert second.invocation_key != first.invocation_key
        assert second.producing_event_id != first.producing_event_id

    asyncio.run(scenario())
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_evidence_records)
            ).scalar_one()
            == 2
        )
    engine.dispose()


def test_delivery_receipt_binds_a_run_event_of_its_own_run_attempt_and_sequence() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        receipt_id = await runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref="protected-row-1",
            protected_hash="9" * 64,
            source_stamps=(_stamp(),),
            receiving_attempt_id=attempt.attempt_id,
        )
        second = await runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref="protected-row-2",
            protected_hash="8" * 64,
            source_stamps=(_stamp(),),
            receiving_attempt_id=attempt.attempt_id,
        )
        assert second != receipt_id
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(
                    ne_agent_delivery_receipts.c.delivery_receipt_id,
                    ne_agent_delivery_receipts.c.event_id,
                    ne_agent_delivery_receipts.c.run_id,
                    ne_agent_delivery_receipts.c.receiving_attempt_id,
                    ne_agent_run_events.c.run_id.label("event_run_id"),
                    ne_agent_run_events.c.attempt_id.label("event_attempt_id"),
                    ne_agent_run_events.c.sequence,
                    ne_agent_run_events.c.event_kind,
                ).join(
                    ne_agent_run_events,
                    ne_agent_run_events.c.event_id == ne_agent_delivery_receipts.c.event_id,
                )
            ).all()
        # The join itself is the proof that neither receipt names a phantom event.
        assert {str(row.delivery_receipt_id) for row in rows} == {receipt_id, second}
        for row in rows:
            assert str(row.event_run_id) == run.run_id == str(row.run_id)
            assert str(row.event_attempt_id) == attempt.attempt_id
            assert str(row.event_attempt_id) == str(row.receiving_attempt_id)
            assert str(row.event_kind) == "delivery.decided"
        assert sorted(int(row.sequence) for row in rows) == [1, 2]

    asyncio.run(scenario())
    engine.dispose()


def test_delivery_receipt_rolls_back_its_run_event_when_the_receipt_side_fails() -> None:
    """Either-side failure leaves NEITHER row: the event side committing alone
    would be exactly the orphan the foreign key exists to forbid.

    Scope, stated because it is not obvious: this proves the ROLLBACK, not that the
    two writes share one transaction. An in-memory ``sqlite://`` engine hands every
    ``connect()`` the same pooled connection, so a mutant that commits the event on
    its own connection is invisible here. The detector for THAT is
    ``test_run_store_postgres.py``'s surviving-event count under the delivery race,
    which reads 80 instead of 10 when the event is split out.
    """
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        # Receipt side fails: the attempt does not belong to this Run, so
        # fk_delivery_receiving_attempt rejects the receipt AFTER its event insert.
        with pytest.raises(IntegrityError):
            await runs.claim_delivery(
                run_id=run.run_id,
                owner_principal_id="alice",
                delivery_kind="tool_result_served",
                protected_ref="protected-row-1",
                protected_hash="9" * 64,
                source_stamps=(_stamp(),),
                receiving_attempt_id="attempt-that-is-not-this-runs",
            )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_events)
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_delivery_receipts)
                ).scalar_one()
                == 0
            )
        # Event side fails: an unknown Run cannot be locked, so it can produce no
        # event, and no receipt may survive that either.
        with pytest.raises(LookupError, match="unknown Run"):
            await runs.claim_delivery(
                run_id="run-that-does-not-exist",
                owner_principal_id="alice",
                delivery_kind="tool_result_served",
                protected_ref="protected-row-1",
                protected_hash="9" * 64,
                source_stamps=(_stamp(),),
                receiving_attempt_id=attempt.attempt_id,
            )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_delivery_receipts)
                ).scalar_one()
                == 0
            )
        # Positive control: the lawful call on the SAME Run writes exactly one of each.
        await runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref="protected-row-1",
            protected_hash="9" * 64,
            source_stamps=(_stamp(),),
            receiving_attempt_id=attempt.attempt_id,
        )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_events)
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_delivery_receipts)
                ).scalar_one()
                == 1
            )

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("delivery_kind", {"delivery_kind": "history_output_served"}),
        ("decision", {"delivery_kind": "withheld"}),
        ("protected_hash", {"protected_hash": "1" * 64}),
        ("source_stamps", {"source_stamps": (_other_stamp(),)}),
    ],
)
def test_delivery_replay_changing_one_intent_field_fails_closed(
    field: str, overrides: dict[str, object]
) -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        base: dict[str, object] = {
            "run_id": run.run_id,
            "owner_principal_id": "alice",
            "delivery_kind": "tool_result_served",
            "protected_ref": "protected-row-1",
            "protected_hash": "9" * 64,
            "source_stamps": (_stamp(),),
            "receiving_attempt_id": attempt.attempt_id,
        }
        first = await runs.claim_delivery(**base)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="different decided intent"):
            await runs.claim_delivery(**{**base, **overrides})  # type: ignore[arg-type]
        assert await runs.claim_delivery(**base) == first  # type: ignore[arg-type]

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("audit_event_id", {"audit_event_id": "audit-event-2"}),
        ("audit_sequence", {"audit_sequence": 43}),
        ("audit_record_hash", {"audit_record_hash": "d" * 64}),
        ("config_hash", {"config_hash": "2" * 64}),
    ],
)
def test_audit_receipt_replay_changing_one_recorded_fact_fails_closed(
    field: str, overrides: dict[str, object]
) -> None:
    """The recorded audit fact is not part of the address, so it MUST be compared."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        evidence = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_evidence_declaration(),
        )
        base: dict[str, object] = {
            "run_id": run.run_id,
            "attempt_id": attempt.attempt_id,
            "run_event_id": evidence.producing_event_id,
            "decision_kind": RunAuditDecisionKind.EVIDENCE,
            "audit_event_id": "audit-event-1",
            "audit_sequence": 42,
            "audit_record_hash": "e" * 64,
            "config_hash": "1" * 64,
            "owner_principal_id": "alice",
            "step_id": "step-1",
            "tool_call_id": "tool-call-1",
        }
        first = await runs.record_audit_receipt(**base)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="different recorded intent"):
            await runs.record_audit_receipt(**{**base, **overrides})  # type: ignore[arg-type]
        assert await runs.record_audit_receipt(**base) == first  # type: ignore[arg-type]

    asyncio.run(scenario())
    engine.dispose()


def test_audit_receipt_is_typed_owner_scoped_and_exactly_attempt_event_bound() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        first_attempt = await runs.link_attempt(run_id=run.run_id)
        evidence = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=first_attempt.attempt_id,
            declaration=_evidence_declaration(),
        )
        second_attempt = await runs.link_attempt(
            run_id=run.run_id, predecessor_attempt_id=first_attempt.attempt_id
        )
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                ne_agent_run_audit_receipts.insert().values(
                    run_audit_receipt_id="mismatched-attempt-event",
                    intent_key="a" * 64,
                    intent_hash="b" * 64,
                    run_id=run.run_id,
                    attempt_id=second_attempt.attempt_id,
                    run_event_id=evidence.producing_event_id,
                    step_id="step-1",
                    tool_call_id="tool-call-1",
                    approval_id=None,
                    decision_kind=RunAuditDecisionKind.EVIDENCE.value,
                    audit_event_id="audit-mismatched-attempt-event",
                    audit_sequence=41,
                    audit_record_hash="c" * 64,
                    config_hash="d" * 64,
                    created_at=datetime.now(UTC),
                )
            )
        recorded = await runs.record_audit_receipt(
            run_id=run.run_id,
            attempt_id=first_attempt.attempt_id,
            run_event_id=evidence.producing_event_id,
            decision_kind=RunAuditDecisionKind.EVIDENCE,
            audit_event_id="audit-exact-attempt-event",
            audit_sequence=42,
            audit_record_hash="e" * 64,
            config_hash="1" * 64,
            owner_principal_id="alice",
            step_id="step-1",
            tool_call_id="tool-call-1",
        )
        receipt_id = recorded.run_audit_receipt_id
        receipt = await runs.get_audit_receipt(
            run_id=run.run_id,
            run_audit_receipt_id=receipt_id,
            owner_principal_id="alice",
        )
        assert receipt is not None
        assert receipt == recorded
        assert receipt.run_audit_receipt_id == receipt_id
        assert receipt.run_id == run.run_id
        assert receipt.attempt_id == first_attempt.attempt_id
        assert receipt.run_event_id == evidence.producing_event_id
        assert receipt.decision_kind is RunAuditDecisionKind.EVIDENCE
        assert receipt.audit_event_id == "audit-exact-attempt-event"
        assert receipt.audit_sequence == 42
        assert (
            await runs.get_audit_receipt(
                run_id=run.run_id,
                run_audit_receipt_id=receipt_id,
                owner_principal_id="bob",
            )
            is None
        )

    asyncio.run(scenario())
    engine.dispose()


def test_audit_receipt_for_a_second_decision_kind_on_one_event_fails_closed() -> None:
    """A different address colliding on uq_run_audit_exact_event must not leak the
    driver error, and must not answer with the other decision's receipt."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        evidence = await runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_evidence_declaration(),
        )
        base: dict[str, object] = {
            "run_id": run.run_id,
            "attempt_id": attempt.attempt_id,
            "run_event_id": evidence.producing_event_id,
            "audit_event_id": "audit-event-1",
            "audit_sequence": 42,
            "audit_record_hash": "e" * 64,
            "config_hash": "1" * 64,
            "owner_principal_id": "alice",
        }
        first = await runs.record_audit_receipt(
            decision_kind=RunAuditDecisionKind.EVIDENCE,
            **base,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="different recorded decision"):
            await runs.record_audit_receipt(
                decision_kind=RunAuditDecisionKind.DELIVERY,
                **{**base, "audit_event_id": "audit-event-2"},  # type: ignore[arg-type]
            )
        assert (
            await runs.record_audit_receipt(
                decision_kind=RunAuditDecisionKind.EVIDENCE,
                **base,  # type: ignore[arg-type]
            )
            == first
        )

    asyncio.run(scenario())
    with engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_audit_receipts)
            ).scalar_one()
            == 1
        )
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("plaintext", {"plaintext": b"different protected bytes"}),
        ("run_id", {"run_id": None}),
        ("source_stamps", {"source_stamps": (_other_stamp(),)}),
    ],
)
def test_protected_payload_replay_changing_one_intent_field_fails_closed(
    field: str, overrides: dict[str, object]
) -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        base: dict[str, object] = {
            "domain": PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
            "run_id": run.run_id,
            "owner_principal_id": "alice",
            "target_kind": "assistant_turn",
            "target_binding": ("turn-1", "attempt-1"),
            "plaintext": b"one exact protected payload",
            "source_stamps": (_stamp(),),
        }
        first = await runs.claim_protected_payload(**base)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="different declared intent"):
            await runs.claim_protected_payload(**{**base, **overrides})  # type: ignore[arg-type]
        assert await runs.claim_protected_payload(**base) == first  # type: ignore[arg-type]

    asyncio.run(scenario())
    engine.dispose()


REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PROMOTED_NODE = "packages/ne-runtime/tests/test_run_store_postgres.py"
AGENT_CATALOG_NODE = "packages/ne-api/tests/test_agent_catalog_postgres.py"


def _ci_jobs() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name: str) -> str:
    for step in _ci_jobs()[job]["steps"]:
        if step.get("name") == name:
            return str(step["run"])
    raise AssertionError(f"{job!r} has no step named {name!r}")


def _collected(module: Path, *additional: Path) -> int:
    """How many tests pytest itself collects from ``module``.

    Derived from pytest's OWN collection rather than a hand count, so deleting or
    renaming a promoted proof moves the floor with it instead of leaving slack.
    """
    result = subprocess.run(  # fixed argv, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            str(module),
            *(str(item) for item in additional),
            "--collect-only",
            "-q",
            "-p",
            "no:randomly",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match is not None, result.stdout + result.stderr
    return int(match.group(1))


REQUIRED_GATE_JOB = "check"
REQUIRED_GATE_STEP = "Agent Run storage PostgreSQL proofs (REQUIRED gate — exact floor, must run)"
PINNED_POSTGRES_IMAGE = (
    "postgres:18.6-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"
)


def test_required_check_job_provides_the_pinned_postgres_service() -> None:
    """The REQUIRED job owns the database, or the required gate cannot run at all."""
    services = _ci_jobs()[REQUIRED_GATE_JOB].get("services", {})
    assert "postgres" in services
    assert services["postgres"]["image"] == PINNED_POSTGRES_IMAGE
    assert "5432:5432" in [str(port) for port in services["postgres"]["ports"]]


def test_required_check_gate_runs_the_run_store_postgres_proofs() -> None:
    """The gate that blocks a merge is in `check`, not in a nonrequired lane.

    `live-postgres` and `live-postgres-pooled` are not required contexts, so a node
    promoted only into them gates nothing. This control names the REQUIRED job
    explicitly; moving the gate back out of `check` fails here.
    """
    run = _step(REQUIRED_GATE_JOB, REQUIRED_GATE_STEP)
    assert PROMOTED_NODE in run
    invocation = run.split("uv run pytest", 1)[1].split("-q -rs", 1)[0]
    assert PROMOTED_NODE in invocation
    match = re.search(r"RUN_STORE_MIN_PASSED=(\d+)", run)
    assert match is not None
    assert int(match.group(1)) == _collected(
        REPO_ROOT / "packages" / "ne-runtime" / "tests" / "test_run_store_postgres.py"
    )
    assert "no tests ran" in run and "skipped" in run
    assert "NE_TEST_POSTGRES_URL" in yaml.safe_dump(
        [
            step
            for step in _ci_jobs()[REQUIRED_GATE_JOB]["steps"]
            if step.get("name") == REQUIRED_GATE_STEP
        ]
    )


def test_required_protected_transport_floor_matches_pytest_collection() -> None:
    run = _step(
        REQUIRED_GATE_JOB,
        "Protected transport migration + DBOS proof (REQUIRED gate — exact floor, must run)",
    )
    match = re.search(r"PROTECTED_TRANSPORT_MIN_PASSED=(\d+)", run)
    assert match is not None
    assert int(match.group(1)) == _collected(
        REPO_ROOT
        / (
            "packages/ne-runtime/tests/test_run_store_postgres.py::"
            "test_legacy_protection_guards_reject_half_migrated_shapes_on_postgres"
        ),
        REPO_ROOT / "packages/ne-runtime/tests/test_legacy_transport_migration_postgres.py",
        REPO_ROOT / "packages/ne-worker/tests/test_worker_dbos_live.py",
    )


def test_required_check_runs_fixed_pilot_postgres_adapter_without_skips() -> None:
    run = _step(
        REQUIRED_GATE_JOB,
        "Fixed Pilot protected-transport PostgreSQL proof (REQUIRED)",
    )
    assert "tools/ne-cli/tests/test_agent_transport_postgres.py" in run
    assert "skipped|no tests ran" in run


def test_required_check_job_keeps_its_existing_executing_gates() -> None:
    """Adding the Run-store gate must not displace a control already required."""
    names = {step.get("name") for step in _ci_jobs()[REQUIRED_GATE_JOB]["steps"]}
    assert {
        "Test",
        "Checkpoint-C executing gate (must run, not skip)",
        "Rego executing gate (C6 — must run, not skip)",
        "Import contracts (C3)",
        "Lint",
        "Format check",
    } <= names


@pytest.mark.parametrize(
    ("job", "step_name"),
    [
        ("live-postgres", "Live NE_TEST_POSTGRES_URL suites (must run, not skip)"),
        ("live-postgres-pooled", "Pooled NE_TEST_POSTGRES_URL suites (must run, not skip)"),
    ],
)
def test_promoted_run_store_postgres_node_runs_in_both_live_lanes(job: str, step_name: str) -> None:
    """The proof is promoted into the direct AND pooled lanes, under their no-skip gate."""
    run = _step(job, step_name)
    assert PROMOTED_NODE in run
    # The node is inside the pytest invocation, not merely mentioned in a comment.
    invocation = run.split("uv run pytest", 1)[1].split("-q -rs", 1)[0]
    assert PROMOTED_NODE in invocation
    assert "no tests ran" in run and "skipped" in run
    assert "[0-9]+ passed" in run


def test_promoted_run_store_postgres_node_is_gated_in_ci() -> None:
    """The dedicated floor equals what pytest actually collects — no slack."""
    run = _step("live-postgres", "Agent Run storage PostgreSQL proofs (exact floor, must run)")
    match = re.search(r"RUN_STORE_MIN_PASSED=(\d+)", run)
    assert match is not None
    assert int(match.group(1)) == _collected(
        REPO_ROOT / "packages" / "ne-runtime" / "tests" / "test_run_store_postgres.py"
    )
    assert PROMOTED_NODE in run
    assert "no tests ran" in run and "skipped" in run


def test_agent_catalog_postgres_controls_survive_the_run_store_promotion() -> None:
    """Adding the Run-store node must not weaken the control it was added beside."""
    direct = _step("live-postgres", "Live NE_TEST_POSTGRES_URL suites (must run, not skip)")
    assert AGENT_CATALOG_NODE in direct
    catalog_floor = _step(
        "live-postgres", "Agent catalog PostgreSQL proofs (exact floor, must run)"
    )
    assert "AGENT_CATALOG_MIN_PASSED=3" in catalog_floor
    assert "no tests ran" in catalog_floor and "skipped" in catalog_floor


def test_migration_refuses_a_protected_row_whose_ciphertext_cannot_be_opened() -> None:
    """Metadata that merely READS correct is not proof the plaintext is safe to destroy."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        kwargs = _turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        # Tamper ONLY the wrapped DEK: every metadata column the verifier compares
        # (owner, Run, target kind, target hash, plaintext hash) still matches.
        with engine.begin() as conn:
            conn.execute(
                ne_agent_protected_turn_contents.update()
                .where(ne_agent_protected_turn_contents.c.turn_content_id == claimed["input"])
                .values(encrypted_dek="A" * 64)
            )
        before = _turn_plaintext(engine)
        with pytest.raises(ValueError, match="authentically decryptable"):
            await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        assert _turn_plaintext(engine) == before
        assert before["private_context"] != ""

    asyncio.run(scenario())
    engine.dispose()


def test_migration_replay_is_idempotent_but_a_changed_binding_never_overwrites() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        kwargs = _turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        migrated = _turn_plaintext(engine)
        await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        assert _turn_plaintext(engine) == migrated
        # A DIFFERENT ref for an already-migrated turn must not overwrite: the
        # plaintext that would be needed to re-derive it is already gone.
        other = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=run_id,
            owner_principal_id="alice",
            target_kind=CONVERSATION_TURN_TARGET_KIND,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            target_binding=conversation_turn_content_binding(
                conversation_id=conversation_id,
                attachment_id="attachment-9",
                attempt_id=attempt_id,
                slot="input",
            ),
            plaintext=b"a different protected payload",
        )
        with pytest.raises(ValueError, match="already migrated"):
            await runs.migrate_conversation_turn_to_protected_refs(
                **{
                    **kwargs,
                    "protected_input_ref": other.row_id,
                    "protected_input_hash": other.plaintext_hash,
                }  # type: ignore[arg-type]
            )
        assert _turn_plaintext(engine) == migrated

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    ("defect", "values"),
    [
        # Each case clears the plaintext it would otherwise strand, so the ONLY
        # guard that can fire is the partial-tuple one under test.
        (
            "input_ref_without_status",
            {"private_context": "", "protected_input_ref": "r", "protected_input_hash": "a" * 64},
        ),
        ("input_status_without_ref", {"private_context": "", "input_status": "source_free"}),
        (
            "terminal_ref_without_hash",
            {"served_output": "", "terminal_status": "source_free", "protected_terminal_ref": "r"},
        ),
    ],
)
def test_database_guard_rejects_a_partial_protected_tuple_on_a_conversation_turn(
    defect: str, values: dict[str, str]
) -> None:
    """A crashed migration's half-state must not be storable at all."""
    engine, sessions, runs = _stores()

    async def scenario() -> str:
        conversation_id, attempt_id, run_id, _claimed = await _seed_legacy_turn(sessions, runs)
        del run_id, attempt_id
        return conversation_id

    conversation_id = asyncio.run(scenario())
    with engine.begin() as conn, pytest.raises(IntegrityError, match="partial protected tuple"):
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(ne_agent_conversation_turns.c.conversation_id == conversation_id)
            .values(**values)
        )
    # Positive control: the COMPLETE trio on the same row is accepted, so the
    # rejection above is the shape guard, not a blanket refusal to update.
    with engine.begin() as conn:
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(ne_agent_conversation_turns.c.conversation_id == conversation_id)
            .values(
                private_context="",
                input_status="source_free",
                protected_input_ref="r",
                protected_input_hash="a" * 64,
            )
        )
    engine.dispose()


def test_database_guard_rejects_a_protected_reference_beside_surviving_plaintext() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> str:
        conversation_id, _attempt, _run, _claimed = await _seed_legacy_turn(sessions, runs)
        return conversation_id

    conversation_id = asyncio.run(scenario())
    with (
        engine.begin() as conn,
        pytest.raises(IntegrityError, match="protected row still carries plaintext"),
    ):
        conn.execute(
            ne_agent_conversation_turns.update()
            .where(ne_agent_conversation_turns.c.conversation_id == conversation_id)
            .values(
                input_status="source_free",
                protected_input_ref="r",
                protected_input_hash="a" * 64,
            )  # private_context deliberately left populated
        )
    engine.dispose()


def test_database_guard_still_admits_an_untouched_legacy_row() -> None:
    """The guards forbid half-migrated shapes, never ordinary un-migrated rows."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, _attempt, _run, _claimed = await _seed_legacy_turn(sessions, runs)
        session_id = await sessions.open_session("assistant", "legacy")
        await sessions.append_turn(
            session_id, [ModelRequest(parts=[UserPromptPart(content="LEGACY-PAYLOAD-SENTINEL")])]
        )
        with engine.connect() as conn:
            turn = conn.execute(
                sa.select(ne_agent_conversation_turns).where(
                    ne_agent_conversation_turns.c.conversation_id == conversation_id
                )
            ).one()
            message = conn.execute(sa.select(ne_agent_session_messages)).one()
        assert turn.input_status is None and turn.protected_input_ref is None
        assert turn.private_context != ""
        assert message.content_status is None and message.protected_content_ref is None
        assert "LEGACY-PAYLOAD-SENTINEL" in str(message.payload)

    asyncio.run(scenario())
    engine.dispose()


def test_standalone_delivery_receipt_stores_no_run_attempt_or_event() -> None:
    engine, _sessions, runs = _stores()

    async def scenario() -> None:
        receipt_id = await runs.claim_delivery(
            run_id=None,
            owner_principal_id="alice",
            delivery_kind="direct_result_served",
            protected_ref="standalone-protected-row",
            protected_hash="9" * 64,
            source_stamps=(_stamp(),),
            receiving_attempt_id=None,
        )
        assert receipt_id
        with engine.connect() as conn:
            row = conn.execute(sa.select(ne_agent_delivery_receipts)).one()
            events = conn.execute(
                sa.select(sa.func.count()).select_from(ne_agent_run_events)
            ).scalar_one()
        assert row.run_id is None
        assert row.receiving_attempt_id is None
        assert row.event_id is None
        assert events == 0
        # Replaying the same standalone address returns the same receipt.
        assert (
            await runs.claim_delivery(
                run_id=None,
                owner_principal_id="alice",
                delivery_kind="direct_result_served",
                protected_ref="standalone-protected-row",
                protected_hash="9" * 64,
                source_stamps=(_stamp(),),
                receiving_attempt_id=None,
            )
            == receipt_id
        )

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    ("defect", "kwargs"),
    [
        ("run_without_attempt", {"run_id": "r", "receiving_attempt_id": None}),
        ("attempt_without_run", {"run_id": None, "receiving_attempt_id": "a"}),
    ],
)
def test_delivery_refuses_a_half_filled_run_bound_shape(
    defect: str, kwargs: dict[str, str | None]
) -> None:
    engine, _sessions, runs = _stores()

    async def scenario() -> None:
        with pytest.raises(ValueError, match=r"Run-bound .* or standalone"):
            await runs.claim_delivery(
                owner_principal_id="alice",
                delivery_kind="direct_result_served",
                protected_ref="protected-row-1",
                protected_hash="9" * 64,
                source_stamps=(_stamp(),),
                **kwargs,  # type: ignore[arg-type]
            )

    asyncio.run(scenario())
    engine.dispose()


def test_terminal_replay_with_a_different_zero_claim_turn_fails_closed() -> None:
    """claim_count alone cannot tell two zero-claim terminal intents apart."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        one = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        two = _target(
            conversation.conversation_id, run.run_id, attempt.attempt_id, turn_id="turn-2"
        )
        base: dict[str, object] = {
            "run_id": run.run_id,
            "owner_principal_id": "alice",
            "attempt_id": attempt.attempt_id,
            "claims": (),
            "terminal_status": RunState.COMPLETED,
        }
        first = await runs.commit_terminal_claims(turn_id="turn-1", target=one, **base)  # type: ignore[arg-type]
        assert first == ()
        assert (
            await runs.commit_terminal_claims(turn_id="turn-1", target=one, **base) == first  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="different terminal intent"):
            await runs.commit_terminal_claims(turn_id="turn-2", target=two, **base)  # type: ignore[arg-type]
        with engine.connect() as conn:
            detail = conn.execute(
                sa.select(ne_agent_run_events.c.safe_detail).where(
                    ne_agent_run_events.c.event_kind == "run.terminal"
                )
            ).scalar_one()
        assert json.loads(str(detail))["turn_id"] == "turn-1"

    asyncio.run(scenario())
    engine.dispose()


# --- A1: canonical target tuple, RunClaim binding, narrow claim slice -----------


def test_tupled_protected_row_persists_every_canonical_component_and_no_content() -> None:
    """The tuple is stored as QUERYABLE columns, not folded away into target_hash."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(
            conversation.conversation_id,
            run.run_id,
            attempt.attempt_id,
            turn_id="turn-7",
            turn_sequence=3,
            attachment_id="attachment-9",
        )
        ref = await _claim_turn_content(runs, target, plaintext=b"PLAINTEXT-SENTINEL here")
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_protected_turn_contents).where(
                    ne_agent_protected_turn_contents.c.turn_content_id == ref.row_id
                )
            ).one()
        assert row.conversation_id == conversation.conversation_id
        assert row.run_id == run.run_id
        assert row.turn_id == "turn-7"
        assert row.turn_sequence == 3
        assert row.attachment_id == "attachment-9"
        assert row.attempt_id == attempt.attempt_id
        assert row.target_kind == AGENT_RUN_TURN_TARGET_KIND
        assert "PLAINTEXT-SENTINEL" not in "|".join(str(value) for value in row)

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize("component", ["turn_id", "turn_sequence", "attachment_id"])
def test_changing_one_canonical_component_yields_a_different_protected_row(
    component: str,
) -> None:
    """Every component participates in the address; none is decorative."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        base = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        first = await _claim_turn_content(runs, base)
        value = 9 if component == "turn_sequence" else f"other-{component}"
        changed = CanonicalTargetTuple(**{**base.__dict__, component: value})
        second = await _claim_turn_content(runs, changed)
        assert second.row_id != first.row_id
        assert second.target_hash != first.target_hash

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    "component",
    ["conversation_id", "run_id", "turn_id", "turn_sequence", "attachment_id", "attempt_id"],
)
def test_every_canonical_component_changes_the_derived_address(component: str) -> None:
    """Leaf-level: dropping any component from the binding would collide two targets."""
    base = _target("conv-1", "run-1", "attempt-1")
    value = 9 if component == "turn_sequence" else f"other-{component}"
    changed = CanonicalTargetTuple(**{**base.__dict__, component: value})
    assert changed.binding() != base.binding()
    domain = PrivatePayloadDomain.PROTECTED_TURN_CONTENT
    assert protected_target_hash(
        domain, "alice", AGENT_RUN_TURN_TARGET_KIND, base.binding()
    ) != protected_target_hash(domain, "alice", AGENT_RUN_TURN_TARGET_KIND, changed.binding())


@pytest.mark.parametrize(
    "legacy_kind", [CONVERSATION_TURN_TARGET_KIND, SESSION_MESSAGE_TARGET_KIND]
)
def test_legacy_discriminators_stay_distinct_and_nonclaimable(legacy_kind: str) -> None:
    assert legacy_kind in LEGACY_TARGET_KINDS
    assert is_claimable_target_kind(legacy_kind) is False
    assert CONVERSATION_TURN_TARGET_KIND != SESSION_MESSAGE_TARGET_KIND
    # Positive control: the tupled kind IS claimable, so the two assertions above
    # are the legacy rule and not a predicate that returns False for everything.
    assert is_claimable_target_kind(AGENT_RUN_TURN_TARGET_KIND) is True


def test_other_run_scoped_kinds_are_also_nonclaimable() -> None:
    """Claimable means TUPLED, not merely 'not legacy'."""
    for kind in ("agent_run_tool_call", "assistant_turn", "approval_action", "agent_run"):
        assert is_claimable_target_kind(kind) is False


def test_a_legacy_target_cannot_carry_a_tuple_and_a_tupled_one_must() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        with pytest.raises(ValueError, match="non-tupled protected target has no canonical tuple"):
            await runs.claim_protected_payload(
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=run.run_id,
                owner_principal_id="alice",
                target_kind=CONVERSATION_TURN_TARGET_KIND,
                target=target,
                plaintext=b"x",
            )
        with pytest.raises(ValueError, match="requires its canonical tuple"):
            await runs.claim_protected_payload(
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=run.run_id,
                owner_principal_id="alice",
                target_kind=AGENT_RUN_TURN_TARGET_KIND,
                target_binding=("turn-1", "attempt-1"),
                plaintext=b"x",
            )
        # Positive control: each kind with its OWN correct shape is accepted.
        assert await _claim_turn_content(runs, target)
        assert await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=None,
            owner_principal_id="alice",
            target_kind=SESSION_MESSAGE_TARGET_KIND,
            content_status=ProtectedContentStatus.LEGACY_UNAVAILABLE,
            target_binding=session_message_content_binding(session_id="s-1", sequence=1),
            plaintext=b"x",
        )

    asyncio.run(scenario())
    engine.dispose()


async def _seed_claim(
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
    *,
    plaintext: bytes = b"the assistant said this exactly",
    span: tuple[int, int] = (4, 13),
    display_state: str = "protected_source_free_slice",
    source_stamps: tuple[SourceAuthorityStamp, ...] = (),
):
    """One terminal Run with a single claim over a real protected turn-content row."""
    conversation, run = await _create_run(sessions, runs)
    attempt = await runs.link_attempt(run_id=run.run_id)
    await runs.transition(run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED)
    await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
    target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
    protected = await _claim_turn_content(
        runs, target, plaintext=plaintext, source_stamps=source_stamps
    )
    # A source-BOUND row is stored packed with its stamp set, so the claim's span
    # and content hash must be taken over the STORED bytes. Without this the span
    # simply fails its content-hash check and the stamp gate is never reached —
    # which would leave that gate unpinned by this control.
    stored = _pack_source_bound_payload(plaintext, source_stamps) if source_stamps else plaintext
    start, end = span
    claim = ClaimDeclaration(
        claim_key="claim-1",
        claim_index=0,
        claim_verdict="unavailable",
        display_state=display_state,  # type: ignore[arg-type]
        protected_ref=protected.row_id,
        served_output_hash="5" * 64,
        span_start_byte=start,
        span_end_byte=end,
        claim_content_hash=hashlib.sha256(stored[start:end]).hexdigest(),
        association_status="association_unavailable",
    )
    (claim_id,) = await runs.commit_terminal_claims(
        run_id=run.run_id,
        owner_principal_id="alice",
        turn_id="turn-1",
        attempt_id=attempt.attempt_id,
        target=target,
        claims=(claim,),
        terminal_status=RunState.COMPLETED,
    )
    return claim_id, target, protected, stored[start:end]


def test_source_free_claim_slice_returns_only_the_stored_span() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        claim_id, target, protected, expected = await _seed_claim(sessions, runs)
        got = await runs.materialize_source_free_claim_slice(
            claim_id=claim_id,
            owner_principal_id="alice",
            target=target,
            protected_ref=protected.row_id,
            served_output_hash="5" * 64,
        )
        assert got == expected
        assert got != b"the assistant said this exactly"  # a WINDOW, not the payload
        # The API has NO span parameter: the caller cannot widen the window.
        assert set(inspect.signature(runs.materialize_source_free_claim_slice).parameters) == {
            "claim_id",
            "owner_principal_id",
            "target",
            "protected_ref",
            "served_output_hash",
        }

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    ("defect", "seed"),
    [
        ("claim_displayed_source_bound", {"display_state": "protected_source_slice"}),
        ("row_carries_authority_stamps", {"source_stamps": "stamped"}),
    ],
)
def test_source_bound_content_can_never_pass_the_claim_slice(
    defect: str, seed: dict[str, object]
) -> None:
    """Releasing source-derived bytes is the authority resolver's call, not this store's."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        kwargs = dict(seed)
        if kwargs.get("source_stamps") == "stamped":
            kwargs["source_stamps"] = (_stamp(),)
        claim_id, target, protected, _ = await _seed_claim(sessions, runs, **kwargs)  # type: ignore[arg-type]
        with pytest.raises(ProtectedSliceUnavailable):
            await runs.materialize_source_free_claim_slice(
                claim_id=claim_id,
                owner_principal_id="alice",
                target=target,
                protected_ref=protected.row_id,
                served_output_hash="5" * 64,
            )

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.parametrize(
    "defect",
    [
        "unknown_claim",
        "foreign_owner",
        "wrong_conversation_id",
        "wrong_turn_id",
        "wrong_turn_sequence",
        "wrong_attachment_id",
        "wrong_attempt_id",
        "wrong_ref",
        "wrong_output_hash",
    ],
)
def test_every_refused_claim_slice_shares_one_indistinguishable_outcome(defect: str) -> None:
    """No existence oracle: nine distinct defects, one error type and one message."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        claim_id, target, protected, expected = await _seed_claim(sessions, runs)
        call: dict[str, object] = {
            "claim_id": claim_id,
            "owner_principal_id": "alice",
            "target": target,
            "protected_ref": protected.row_id,
            "served_output_hash": "5" * 64,
        }
        if defect == "unknown_claim":
            call["claim_id"] = "claim-that-does-not-exist"
        elif defect == "foreign_owner":
            call["owner_principal_id"] = "mallory"
        elif defect == "wrong_ref":
            call["protected_ref"] = "row-that-does-not-exist"
        elif defect == "wrong_output_hash":
            call["served_output_hash"] = "0" * 64
        else:
            component = defect.removeprefix("wrong_")
            value = 9 if component == "turn_sequence" else f"other-{component}"
            call["target"] = CanonicalTargetTuple(**{**target.__dict__, component: value})
        with pytest.raises(ProtectedSliceUnavailable) as raised:
            await runs.materialize_source_free_claim_slice(**call)  # type: ignore[arg-type]
        assert type(raised.value) is ProtectedSliceUnavailable
        assert str(raised.value) == "protected claim slice unavailable"
        # Positive control: the unmutated call still returns the slice, so the
        # refusal above is this defect and not a surface that refuses everything.
        assert (
            await runs.materialize_source_free_claim_slice(
                claim_id=claim_id,
                owner_principal_id="alice",
                target=target,
                protected_ref=protected.row_id,
                served_output_hash="5" * 64,
            )
            == expected
        )

    asyncio.run(scenario())
    engine.dispose()


def test_a_run_claim_cannot_name_a_protected_row_of_another_turn() -> None:
    """The RunClaim/protected-row binding is a database constraint, not a convention."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        await runs.transition(
            run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
        mine = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        other_turn = _target(
            conversation.conversation_id, run.run_id, attempt.attempt_id, turn_id="turn-2"
        )
        await _claim_turn_content(runs, mine)
        foreign = await _claim_turn_content(runs, other_turn, plaintext=b"a different turn")
        claim = ClaimDeclaration(
            claim_key="claim-1",
            claim_index=0,
            claim_verdict="unavailable",
            display_state="protected_source_free_slice",
            protected_ref=foreign.row_id,  # belongs to turn-2, claim is for turn-1
            served_output_hash="5" * 64,
            span_start_byte=0,
            span_end_byte=4,
            claim_content_hash="6" * 64,
            association_status="association_unavailable",
        )
        with pytest.raises(IntegrityError):
            await runs.commit_terminal_claims(
                run_id=run.run_id,
                owner_principal_id="alice",
                turn_id="turn-1",
                attempt_id=attempt.attempt_id,
                target=mine,
                claims=(claim,),
                terminal_status=RunState.COMPLETED,
            )

    asyncio.run(scenario())
    engine.dispose()


def test_a_terminal_commit_refuses_a_tuple_for_a_different_run_turn() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        wrong = _target(conversation.conversation_id, run.run_id, attempt.attempt_id, turn_id="x")
        with pytest.raises(ValueError, match="does not address this Run turn"):
            await runs.commit_terminal_claims(
                run_id=run.run_id,
                owner_principal_id="alice",
                turn_id="turn-1",
                attempt_id=attempt.attempt_id,
                target=wrong,
                claims=(),
                terminal_status=RunState.COMPLETED,
            )

    asyncio.run(scenario())
    engine.dispose()


def test_the_store_exposes_no_generic_protected_payload_decryptor() -> None:
    """AES stays private: every exposed materializer is target-discriminated."""
    engine, _sessions, runs = _stores()
    public = {name for name in dir(runs) if not name.startswith("_")}
    assert {name for name in public if "materialize" in name or "decrypt" in name} == {
        "materialize_run_input",
        "materialize_conversation_history",
        "materialize_legacy_conversation_turn",
        "materialize_source_free_claim_slice",
        "materialize_source_free_turn",
    }
    assert not hasattr(runs, "materialize_protected_payload")
    engine.dispose()


def test_run_store_source_resolver_requires_exact_output_target_binding() -> None:
    _engine, _sessions, runs = _stores()
    expected_context = AuthenticatedRequestContext(
        principal_id="alice",
        authorization_groups=("analysts",),
        issuer="https://issuer.example",
        expires_at=4_102_444_800,
    )

    async def evaluate(stamp, context):
        del stamp
        assert context is expected_context
        return SourceAuthorityDecision.AUTHORIZED

    async def exercise() -> None:
        writer = RunStoreProtectedSourceOutputWriter(runs)
        resolver = RunStoreSourceBoundResultResolver(runs, evaluate)
        payload = b"current protected answer"
        with authenticated_request_scope(expected_context):
            reference = await writer.claim_output(
                target_id="run-a",
                attempt_id="attempt-a",
                output=payload,
                output_hash=hashlib.sha256(payload).hexdigest(),
                stamps=(_stamp(),),
            )
            allowed = await resolver.for_model_send(
                reference,
                principal_id=expected_context.principal_id,
                access_context_hash=expected_context.context_hash,
                run_id="run-a",
                model_call_attempt_id="attempt-a",
            )
            assert allowed.decision is SourceAuthorityDecision.AUTHORIZED
            assert allowed.materialized_text == payload.decode()

            rebound = await resolver.for_model_send(
                reference,
                principal_id=expected_context.principal_id,
                access_context_hash=expected_context.context_hash,
                run_id="run-a",
                model_call_attempt_id="attempt-b",
            )
            assert rebound.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
            assert rebound.materialized_text is None

    asyncio.run(exercise())
    _engine.dispose()


def _verified_evidence_set(stamps: tuple[SourceAuthorityStamp, ...]) -> EvidenceSetDeclaration:
    contributors = tuple(
        EvidenceContributorDeclaration(
            locator_plaintext=json.dumps(
                {
                    "evidence_assurance": "direct",
                    "contributor_index": index,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            source_stamp=stamp,
            assurance=EvidenceAssurance.DIRECT,
            safe_event_detail={
                "read_kind": "verified_t3",
                "evidence_status": "located",
                "contributor_count": 1,
                "evidence_assurance": "direct",
                "row_anchor": "a" * 64,
                "receipt_hash": "b" * 64,
            },
        )
        for index, stamp in enumerate(stamps)
    )
    return EvidenceSetDeclaration(
        turn_id="turn-1",
        step_id="step-1",
        tool_call_id="tool-call-verified",
        read_ordinal=0,
        content_hash=hashlib.sha256(b"verified protected rows").hexdigest(),
        pending_result_plaintext=b"verified protected rows",
        rows=(
            EvidenceRowDeclaration(
                row_anchor="a" * 64,
                receipt_hash="b" * 64,
                contributors=contributors,
            ),
        ),
    )


def test_verified_evidence_set_is_atomic_ordered_and_exactly_resolved() -> None:
    engine, sessions, runs = _stores()
    second = _stamp().model_copy(
        update={"authority_receipt_id": "receipt-b", "authority_receipt_hash": "e" * 64}
    )

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        declaration = _verified_evidence_set((_stamp(), second))
        claimed = await runs.claim_evidence_set(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=declaration,
        )
        assert (
            await runs.claim_evidence_set(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                declaration=declaration,
            )
            == claimed
        )
        row = declaration.rows[0]
        for changed_row in (
            replace(row, contributors=row.contributors[:1]),
            replace(row, contributors=tuple(reversed(row.contributors))),
            replace(row, receipt_hash="c" * 64),
        ):
            with pytest.raises(ValueError, match="different declared intent"):
                await runs.claim_evidence_set(
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    attempt_id=attempt.attempt_id,
                    declaration=replace(declaration, rows=(changed_row,)),
                )
        evidence_ids = tuple(item.evidence_id for item in claimed.evidence)
        assert len(evidence_ids) == 2
        assert len({item.locator_id for item in claimed.evidence}) == 2
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_evidence_sets)
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_evidence_records)
                ).scalar_one()
                == 2
            )
            row_event = conn.execute(
                sa.select(ne_agent_run_events.c.safe_detail).where(
                    ne_agent_run_events.c.event_id == claimed.row_event_ids[0]
                )
            ).scalar_one()
        assert json.loads(row_event)["evidence_ids"] == list(evidence_ids)

        context = AuthenticatedRequestContext(
            principal_id="alice",
            authorization_groups=("analysts",),
            issuer="https://issuer.example",
            expires_at=4_102_444_800,
        )

        async def evaluate(
            stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
        ) -> SourceAuthorityDecision:
            del stamp, context
            return SourceAuthorityDecision.AUTHORIZED

        resolver = RunStoreSourceBoundResultResolver(runs, evaluate)
        guard = ProtectedSourceResultGuard(resolver)
        reference = SourceBoundToolResultSetRef(
            reference_id=claimed.pending_result_id,
            result_hash=declaration.content_hash,
            stamps=canonical_source_authority_stamps((_stamp(), second)),
            evidence_ids=evidence_ids,
        )
        stored = await runs.load_pending_source_tool_result(
            reference_id=claimed.pending_result_id, owner_principal_id="alice"
        )
        assert stored.plaintext_hash == reference.result_hash
        assert stored.stamps == reference.stamps
        assert stored.evidence_ids == reference.evidence_ids
        with authenticated_request_scope(context):
            allowed = await resolver.for_model_send(
                reference,
                principal_id="alice",
                access_context_hash=context.context_hash,
                run_id=run.run_id,
                model_call_attempt_id=attempt.attempt_id,
            )
            swapped = await resolver.for_model_send(
                reference.model_copy(update={"evidence_ids": tuple(reversed(evidence_ids))}),
                principal_id="alice",
                access_context_hash=context.context_hash,
                run_id=run.run_id,
                model_call_attempt_id=attempt.attempt_id,
            )
            with (
                run_delivery_observation_scope(
                    run_id=run.run_id, attempt_id=attempt.attempt_id
                ) as observer,
                model_call_scope(run_id=run.run_id, model_call_attempt_id=attempt.attempt_id),
                direct_publication_scope(
                    workflow_execution_id=run.run_id,
                    publication_attempt_id=attempt.attempt_id,
                ),
            ):
                direct = await guard.materialize_direct_text(encode_protected_reference(reference))
                complete_run_delivery_observation()
                observed = observer.snapshot()
        assert allowed.materialized_text == "verified protected rows"
        assert swapped.decision is SourceAuthorityDecision.RETRYABLE_UNKNOWN
        assert direct is not None and direct.authorized
        assert direct.text == "verified protected rows"
        assert len(observed.delivery_receipt_ids) == 1
        with engine.connect() as conn:
            delivery = conn.execute(
                sa.select(ne_agent_delivery_receipts).where(
                    ne_agent_delivery_receipts.c.delivery_receipt_id
                    == observed.delivery_receipt_ids[0]
                )
            ).one()
        assert delivery.delivery_kind == "direct_result_served"
        assert delivery.run_id == run.run_id
        assert delivery.receiving_attempt_id == attempt.attempt_id
        assert delivery.protected_ref == reference.reference_id

    asyncio.run(scenario())
    engine.dispose()


def test_verified_evidence_set_rolls_back_every_partial_effect(monkeypatch) -> None:
    engine, sessions, runs = _stores()
    original = runs._append_event  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def fail_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("controlled atomicity failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(runs, "_append_event", fail_after_first)

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        with pytest.raises(RuntimeError, match="controlled atomicity failure"):
            await runs.claim_evidence_set(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                declaration=_verified_evidence_set((_stamp(),)),
            )

    asyncio.run(scenario())
    with engine.connect() as conn:
        for table in (
            ne_agent_evidence_sets,
            ne_agent_evidence_records,
            ne_agent_evidence_locators,
            ne_agent_pending_source_tool_results,
        ):
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
    engine.dispose()


def test_verified_evidence_set_rejects_locator_assurance_mismatch() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        declaration = _verified_evidence_set((_stamp(),))
        contributor = declaration.rows[0].contributors[0]
        bad = EvidenceContributorDeclaration(
            locator_plaintext=contributor.locator_plaintext,
            source_stamp=contributor.source_stamp,
            assurance=EvidenceAssurance.INFERRED,
            safe_event_detail=contributor.safe_event_detail,
        )
        with pytest.raises(ValueError, match="assurance does not match"):
            await runs.claim_evidence_set(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                declaration=EvidenceSetDeclaration(
                    **{
                        **declaration.__dict__,
                        "rows": (
                            EvidenceRowDeclaration(
                                row_anchor="a" * 64,
                                receipt_hash="b" * 64,
                                contributors=(bad,),
                            ),
                        ),
                    }
                ),
            )

    asyncio.run(scenario())
    engine.dispose()


def test_current_key_readiness_is_synchronous_and_reports_no_key_material() -> None:
    engine, sessions, runs = _stores()
    assert not inspect.iscoroutinefunction(runs.current_key_readiness)

    async def scenario() -> None:
        await _create_run(sessions, runs)

    asyncio.run(scenario())
    readiness = runs.current_key_readiness()  # no running event loop
    assert readiness.current_key_id == "k1"
    assert readiness.rows_under_current_key == 1
    assert readiness.rows_under_other_keys == 0
    assert readiness.ready is True
    rendered = repr(readiness)
    assert "k" * 32 not in rendered
    assert "z" * 32 not in rendered
    engine.dispose()


# --- P1-1: contradictory Run identity must fail closed before any write ---------


def test_a_protected_claim_whose_run_id_disagrees_with_its_tuple_writes_nothing() -> None:
    """The AAD/hash bind target.run_id while the row PERSISTS the run_id argument.

    Accepting a disagreement would let the authenticated identity and the queryable
    identity name different Runs. The refusal must happen before any write — an
    exception alone would not rule out a half-written row, event or claim.
    """
    engine, sessions, runs = _stores()

    def counts() -> tuple[int, int, int]:
        with engine.connect() as conn:
            return (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
                ).scalar_one(),
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_events)
                ).scalar_one(),
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_claims)
                ).scalar_one(),
            )

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        other_conversation, other_run = await _create_run(sessions, runs)
        del other_conversation
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        before = counts()
        assert before == (0, 0, 0)
        with pytest.raises(ValueError, match="disagrees with its canonical target tuple"):
            # A REAL other Run id, so this is a genuine contradiction rather than a
            # value that would have failed a foreign-key check anyway.
            await _claim_turn_content(runs, target, run_id=other_run.run_id)
        assert counts() == before
        # Positive control: the agreeing call on the SAME target writes exactly one
        # protected row, so the zero above is the guard and not an inert fixture.
        await _claim_turn_content(runs, target)
        assert counts() == (1, 0, 0)

    asyncio.run(scenario())
    engine.dispose()


# --- P1-2: the authenticated content status ------------------------------------


@pytest.mark.parametrize(
    ("case", "status", "stamps", "expected"),
    [
        ("source_free", ProtectedContentStatus.SOURCE_FREE, False, None),
        (
            "source_bearing_with_its_stamps",
            ProtectedContentStatus.SOURCE_BOUND_CONTAINER,
            True,
            None,
        ),
        (
            "source_bearing_missing_stamps",
            ProtectedContentStatus.SOURCE_BOUND_CONTAINER,
            False,
            "source-bound container requires its authority stamp set",
        ),
        (
            "source_free_with_extra_stamps",
            ProtectedContentStatus.SOURCE_FREE,
            True,
            "only a source-bound container may carry authority stamps",
        ),
        (
            "legacy_unavailable_with_extra_stamps",
            ProtectedContentStatus.LEGACY_UNAVAILABLE,
            True,
            "only a source-bound container may carry authority stamps",
        ),
    ],
)
def test_the_legal_status_to_stamp_relationship_is_enforced(
    case: str,
    status: ProtectedContentStatus,
    stamps: bool,
    expected: str | None,
) -> None:
    """A model or client cannot declare source-free content that carries stamps,
    nor hide a source-bearing payload behind a status that forbids them."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        source_stamps = (_stamp(),) if stamps else ()
        if expected is None:
            ref = await _claim_turn_content(
                runs, target, content_status=status, source_stamps=source_stamps
            )
            with engine.connect() as conn:
                stored = conn.execute(
                    sa.select(ne_agent_protected_turn_contents.c.content_status).where(
                        ne_agent_protected_turn_contents.c.turn_content_id == ref.row_id
                    )
                ).scalar_one()
            assert stored == status.value
            return
        with pytest.raises(ValueError, match=expected):
            await _claim_turn_content(
                runs, target, content_status=status, source_stamps=source_stamps
            )

    asyncio.run(scenario())
    engine.dispose()


def test_a_replay_declaring_a_different_content_status_fails_closed() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        first = await _claim_turn_content(
            runs, target, content_status=ProtectedContentStatus.SOURCE_FREE
        )
        with pytest.raises(ValueError, match="different declared intent"):
            await _claim_turn_content(
                runs, target, content_status=ProtectedContentStatus.LEGACY_UNAVAILABLE
            )
        # Positive control: the unchanged status still replays to the same winner.
        assert (
            await _claim_turn_content(
                runs, target, content_status=ProtectedContentStatus.SOURCE_FREE
            )
            == first
        )

    asyncio.run(scenario())
    engine.dispose()


async def _seed_claim_over(
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
    *,
    content_status: ProtectedContentStatus,
    display_state: str,
    plaintext: bytes = b"the assistant said this exactly",
    span: tuple[int, int] = (4, 13),
):
    """A claim pointing at a row of the given AUTHENTICATED status."""
    conversation, run = await _create_run(sessions, runs)
    attempt = await runs.link_attempt(run_id=run.run_id)
    await runs.transition(run_id=run.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED)
    await runs.transition(run_id=run.run_id, expected=RunState.QUEUED, target=RunState.RUNNING)
    target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
    stamps = (_stamp(),) if content_status is ProtectedContentStatus.SOURCE_BOUND_CONTAINER else ()
    protected = await _claim_turn_content(
        runs, target, plaintext=plaintext, content_status=content_status, source_stamps=stamps
    )
    stored = _pack_source_bound_payload(plaintext, stamps) if stamps else plaintext
    start, end = span
    claim = ClaimDeclaration(
        claim_key="claim-1",
        claim_index=0,
        claim_verdict="unavailable",
        display_state=display_state,  # type: ignore[arg-type]
        protected_ref=protected.row_id,
        served_output_hash="5" * 64,
        span_start_byte=start,
        span_end_byte=end,
        claim_content_hash=hashlib.sha256(stored[start:end]).hexdigest(),
        association_status="association_unavailable",
    )
    (claim_id,) = await runs.commit_terminal_claims(
        run_id=run.run_id,
        owner_principal_id="alice",
        turn_id="turn-1",
        attempt_id=attempt.attempt_id,
        target=target,
        claims=(claim,),
        terminal_status=RunState.COMPLETED,
    )
    return claim_id, target, protected, stored[start:end]


def test_source_bearing_content_cannot_be_materialized_as_a_source_free_slice() -> None:
    """Even with a caller-authored source-free display_state and a span whose hash
    matches, the AUTHENTICATED status refuses — the substitution is the attack."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        claim_id, target, protected, _ = await _seed_claim_over(
            sessions,
            runs,
            content_status=ProtectedContentStatus.SOURCE_BOUND_CONTAINER,
            display_state="protected_source_free_slice",
        )
        with pytest.raises(ProtectedSliceUnavailable):
            await runs.materialize_source_free_claim_slice(
                claim_id=claim_id,
                owner_principal_id="alice",
                target=target,
                protected_ref=protected.row_id,
                served_output_hash="5" * 64,
            )
        # Positive control: an otherwise identical SOURCE-FREE row DOES resolve, so
        # the refusal is the status and not a slice path that refuses everything.
        free_id, free_target, free_ref, expected = await _seed_claim_over(
            sessions,
            runs,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            display_state="protected_source_free_slice",
        )
        assert (
            await runs.materialize_source_free_claim_slice(
                claim_id=free_id,
                owner_principal_id="alice",
                target=free_target,
                protected_ref=free_ref.row_id,
                served_output_hash="5" * 64,
            )
            == expected
        )

    asyncio.run(scenario())
    engine.dispose()


def test_only_the_aead_refuses_a_relabelled_source_bound_row() -> None:
    """content_status is in the AEAD AAD, not merely a column and a semantic gate.

    Every earlier gate is deliberately satisfied on this input: the row is
    relabelled to ``source_free`` AND its stamp set cleared in ONE write, so the
    schema constraint, the stored-status authority check, the display-state check
    and the stamp check all pass, and the target hash is unchanged because
    content_status is deliberately NOT part of the address. The ONLY thing left
    that can refuse is that the ciphertext was sealed under source-bound AAD.

    An earlier version of this test relabelled ``source_free -> legacy_unavailable``
    and passed without the ciphertext ever being opened -- the pre-decrypt semantic
    gate refused first. That is the same "passing for the wrong reason" shape as the
    stamp gate, and it proved nothing about the AAD.
    """
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        claim_id, target, protected, _ = await _seed_claim_over(
            sessions,
            runs,
            content_status=ProtectedContentStatus.SOURCE_BOUND_CONTAINER,
            display_state="protected_source_free_slice",
        )
        with engine.begin() as conn:
            conn.execute(
                ne_agent_protected_turn_contents.update()
                .where(ne_agent_protected_turn_contents.c.turn_content_id == protected.row_id)
                .values(
                    content_status=ProtectedContentStatus.SOURCE_FREE.value,
                    stamp_set_hash=None,
                )
            )
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    ne_agent_protected_turn_contents.c.content_status,
                    ne_agent_protected_turn_contents.c.stamp_set_hash,
                    ne_agent_protected_turn_contents.c.target_hash,
                ).where(ne_agent_protected_turn_contents.c.turn_content_id == protected.row_id)
            ).one()
        # The relabelled row now looks source-free to every pre-decrypt gate...
        assert row.content_status == ProtectedContentStatus.SOURCE_FREE.value
        assert row.stamp_set_hash is None
        # ...and its ADDRESS is untouched, because content_status is not in it.
        assert row.target_hash == protected_target_hash(
            PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            "alice",
            AGENT_RUN_TURN_TARGET_KIND,
            target.binding(),
        )
        with pytest.raises(ProtectedSliceUnavailable):
            await runs.materialize_source_free_claim_slice(
                claim_id=claim_id,
                owner_principal_id="alice",
                target=target,
                protected_ref=protected.row_id,
                served_output_hash="5" * 64,
            )
        # Positive control: a GENUINE source-free row resolves through the same path.
        free_id, free_target, free_ref, expected = await _seed_claim_over(
            sessions,
            runs,
            content_status=ProtectedContentStatus.SOURCE_FREE,
            display_state="protected_source_free_slice",
        )
        assert (
            await runs.materialize_source_free_claim_slice(
                claim_id=free_id,
                owner_principal_id="alice",
                target=free_target,
                protected_ref=free_ref.row_id,
                served_output_hash="5" * 64,
            )
            == expected
        )

    asyncio.run(scenario())
    engine.dispose()


def test_the_migration_verifier_authenticates_the_declared_content_status() -> None:
    """A migration cannot declare a status the protected row does not authenticate."""
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation_id, attempt_id, run_id, claimed = await _seed_legacy_turn(sessions, runs)
        kwargs = _turn_migration_kwargs(conversation_id, attempt_id, claimed, run_id)
        before = _turn_plaintext(engine)
        with pytest.raises(ValueError, match="different authenticated content status"):
            await runs.migrate_conversation_turn_to_protected_refs(
                **{**kwargs, "input_status": "legacy_unavailable"}  # type: ignore[arg-type]
            )
        assert _turn_plaintext(engine) == before
        assert before["private_context"] != ""
        await runs.migrate_conversation_turn_to_protected_refs(**kwargs)  # type: ignore[arg-type]
        assert _turn_plaintext(engine)["private_context"] == ""

    asyncio.run(scenario())
    engine.dispose()


def test_a_malformed_at_birth_envelope_still_yields_the_single_slice_outcome() -> None:
    """The envelope CONSTRUCTOR raises ValueError, not KeyEnvelopeError.

    `_row_envelope` builds a `PrivatePayloadEnvelope` inside the slice's try block,
    and its `__post_init__` rejects a malformed column. A row written
    malformed-at-birth — which the append-only payload trigger cannot prevent,
    because it only blocks post-hoc UPDATEs — must still be indistinguishable from
    every other refusal, or the parser error becomes an existence oracle.
    """
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        conversation, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        target = _target(conversation.conversation_id, run.run_id, attempt.attempt_id)
        # A SELF-CONSISTENT row: correct owner, tuple and target hash, so every
        # earlier gate passes and the envelope construction is genuinely reached.
        target_hash = protected_target_hash(
            PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            "alice",
            AGENT_RUN_TURN_TARGET_KIND,
            target.binding(),
        )
        with engine.begin() as conn:
            conn.execute(
                ne_agent_protected_turn_contents.insert().values(
                    turn_content_id="malformed-row",
                    run_id=run.run_id,
                    owner_principal_id="alice",
                    target_kind=AGENT_RUN_TURN_TARGET_KIND,
                    target_hash=target_hash,
                    content_status=ProtectedContentStatus.SOURCE_FREE.value,
                    conversation_id=target.conversation_id,
                    turn_id=target.turn_id,
                    turn_sequence=target.turn_sequence,
                    attachment_id=target.attachment_id,
                    attempt_id=target.attempt_id,
                    stamp_set_hash=None,
                    created_at=datetime.now(UTC),
                    cipher_version=1,
                    plaintext_hash="b" * 64,
                    binding_hash="c" * 64,
                    key_id="k1",
                    payload_nonce="n",  # malformed: not a 96-bit base64url nonce
                    payload_ciphertext="ct",
                    key_nonce="kn",
                    encrypted_dek="dek",
                )
            )
            conn.execute(
                ne_agent_run_claims.insert().values(
                    claim_id="malformed-claim",
                    run_id=run.run_id,
                    turn_id=target.turn_id,
                    owner_principal_id="alice",
                    agent_version=7,
                    conversation_id=target.conversation_id,
                    turn_sequence=target.turn_sequence,
                    attachment_id=target.attachment_id,
                    attempt_id=target.attempt_id,
                    claim_key="claim-1",
                    claim_index=0,
                    claim_verdict="unavailable",
                    display_state="protected_source_free_slice",
                    protected_ref="malformed-row",
                    served_output_hash="5" * 64,
                    span_start_byte=0,
                    span_end_byte=4,
                    claim_content_hash="6" * 64,
                    association_status="association_unavailable",
                    created_at=datetime.now(UTC),
                )
            )
        with pytest.raises(ProtectedSliceUnavailable) as raised:
            await runs.materialize_source_free_claim_slice(
                claim_id="malformed-claim",
                owner_principal_id="alice",
                target=target,
                protected_ref="malformed-row",
                served_output_hash="5" * 64,
            )
        assert type(raised.value) is ProtectedSliceUnavailable
        assert str(raised.value) == "protected claim slice unavailable"

    asyncio.run(scenario())
    engine.dispose()


def _model_bound_start_command(**values: object) -> SimpleNamespace:
    owner = str(values.get("owner_principal_id", "alice"))
    department = str(values.get("department", "claims"))
    defaults: dict[str, object] = dict(
        agent_definition_hash="d" * 64,
        model_policy_id="agent.meridian-analyst.v2",
        authorized_model="private-primary",
        authorized_model_lane="private-vllm",
        model_authorization_receipt_hash="e" * 64,
        target_input_budget=16_384,
        requested_model=None,
        requested_model_lane=None,
        authenticated_context=AuthenticatedRequestContext(
            principal_id=owner,
            authorization_groups=(),
            department=department,
            issuer="https://issuer.example.test",
            expires_at=4_102_444_800,
        ),
    )
    defaults.update(values)
    return SimpleNamespace(**defaults)


# --- NE-CLAIM-VERIFIED-SET ------------------------------------------------------
# A `read.kg.*` verified (T3) read serves ONE protected result backed by an evidence
# SET — one record per (row, contributor). The loader used to require exactly one
# located record per pending result and died on the set with a BARE `LookupError`,
# which published as `Recorded failure class: LookupError`. These pin that the set
# now resolves whole and verifies end to end, and that a genuine store condition
# still fails with its own honest reason rather than being relabelled a bad answer.

_T3_SET_COLUMNS = ["customer", "provisional_credit_amount", "status"]
_T3_SET_ROWS = [
    ["Meridian Bank", "412.55", "posted"],
    ["Northwind Trust", "88.20", "pending"],
]


def _t3_set_rendering() -> bytes:
    lines = [
        f"[showing {len(_T3_SET_ROWS)} of {len(_T3_SET_ROWS)} rows]",
        " | ".join(_T3_SET_COLUMNS),
        "-+-".join("-" * len(column) for column in _T3_SET_COLUMNS),
        *(" | ".join(row) for row in _T3_SET_ROWS),
    ]
    return "\n".join(lines).encode("utf-8")


def _t3_set_declaration(
    stamps: tuple[SourceAuthorityStamp, ...],
) -> EvidenceSetDeclaration:
    """A declaration shaped exactly as `KnowledgeSourceEvidenceAuthor.claim_verified_set`
    shapes one: the row anchor is `sha256(domain + receipt_hash)` and the receipt's
    `canonical_row_hash` is the one `ne.brain_ontology.verified.answers` mints."""

    rendering = _t3_set_rendering()
    rows = []
    for row_index, row in enumerate(_T3_SET_ROWS):
        receipt_hash = hashlib.sha256(f"receipt-{row_index}".encode()).hexdigest()
        row_anchor = hashlib.sha256(
            b"ne.verified-evidence-row-anchor.v1\x00" + receipt_hash.encode("ascii")
        ).hexdigest()
        receipt = {
            "receipt_schema_version": 1,
            "template_id": "reg_e_provisional_credits",
            "template_version": 1,
            "canonical_row_hash": hashlib.sha256(
                json.dumps([_T3_SET_COLUMNS, row], separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "contributor_schema_version": 1,
            "contributors": [stamp.model_dump(mode="json") for stamp in stamps],
            "contributor_count": len(stamps),
            "contributor_set_hash": "1" * 64,
            "receipt_hash": receipt_hash,
        }
        rows.append(
            EvidenceRowDeclaration(
                row_anchor=row_anchor,
                receipt_hash=receipt_hash,
                contributors=tuple(
                    EvidenceContributorDeclaration(
                        locator_plaintext=json.dumps(
                            {
                                "kind": "verified_t3",
                                "row_anchor": row_anchor,
                                "receipt": receipt,
                                "contributor_index": index,
                                "contributor": stamp.model_dump(mode="json"),
                                "evidence_assurance": "direct",
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode(),
                        source_stamp=stamp,
                        assurance=EvidenceAssurance.DIRECT,
                        safe_event_detail={
                            "read_kind": "verified_t3",
                            "evidence_status": "located",
                            "contributor_count": 1,
                            "evidence_assurance": "direct",
                            "row_anchor": row_anchor,
                            "receipt_hash": receipt_hash,
                        },
                    )
                    for index, stamp in enumerate(stamps)
                ),
            )
        )
    return EvidenceSetDeclaration(
        turn_id="turn-1",
        step_id="step-1",
        tool_call_id="tool-call-verified",
        read_ordinal=0,
        content_hash=hashlib.sha256(rendering).hexdigest(),
        pending_result_plaintext=rendering,
        rows=tuple(rows),
    )


def test_verified_evidence_set_binds_a_run_claim_end_to_end() -> None:
    engine, sessions, runs = _stores()
    second = _stamp().model_copy(
        update={"authority_receipt_id": "receipt-b", "authority_receipt_hash": "e" * 64}
    )
    stamps = (_stamp(), second)
    rendering = _t3_set_rendering()
    content_hash = hashlib.sha256(rendering).hexdigest()

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        claimed = await runs.claim_evidence_set(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=_t3_set_declaration(stamps),
        )
        await runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref=claimed.pending_result_id,
            protected_hash=content_hash,
            # The delivery is claimed with the reference's own canonical stamps,
            # exactly as `ne.runtime.source_results` claims it in the live path.
            source_stamps=canonical_source_authority_stamps(stamps),
            receiving_attempt_id=attempt.attempt_id,
        )
        evidence_ids = tuple(item.evidence_id for item in claimed.evidence)
        assert len(evidence_ids) == 4  # two rows x two contributors
        set_ref = encode_protected_reference(
            SourceBoundToolResultSetRef(
                reference_id=claimed.pending_result_id,
                result_hash=content_hash,
                stamps=canonical_source_authority_stamps(stamps),
                evidence_ids=evidence_ids,
            )
        )

        observation = await runs.load_run_claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            turn_id="turn-1",
            protected_ref=set_ref,
        )
        assert isinstance(observation, RunClaimEvidenceSetObservation)
        # WHOLE, and in the set's own server-minted order — never one member.
        assert tuple(member.evidence_id for member in observation.members) == evidence_ids
        assert observation.protected_rendering == rendering
        assert observation.protected_ref == claimed.pending_result_id
        assert observation.evidence_set_id == claimed.evidence_set_id
        assert observation.read_kind == "verified_t3"

        # ... and it verifies a claim over the served table end to end. This is the
        # exact path that published `Recorded failure class: LookupError`.
        answer = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
        raw = answer.encode()
        verified = verify_claim(
            ClaimSlice(
                claim_key="claim-local-1",
                claim_content_hash=hashlib.sha256(raw).hexdigest(),
                span_start_byte=0,
                span_end_byte=len(raw),
                served_output_hash=hashlib.sha256(raw).hexdigest(),
                served_output=raw,
            ),
            VerifiedRowSetEvidenceObservation(
                protected_ref=observation.protected_ref,
                protected_result_hash=observation.protected_result_hash,
                evidence_set_id=observation.evidence_set_id,
                set_key=observation.set_key,
                delivery_receipt_id=observation.delivery_receipt_id,
                delivery_receipt_hash=observation.delivery_receipt_hash,
                protected_rendering=observation.protected_rendering,
                members=tuple(
                    VerifiedRowSetMemberObservation(
                        evidence_id=member.evidence_id,
                        locator_id=member.locator_id,
                        locator_hash=member.locator_hash,
                        producing_event_id=member.producing_event_id,
                        locator=member.locator,
                    )
                    for member in observation.members
                ),
            ),
        )
        assert verified.member_evidence_ids == evidence_ids
        assert verified.evidence_set_id == claimed.evidence_set_id
        assert verified.evidence_id == evidence_ids[0]

        # A reference naming only PART of the set is a contract refusal: a claim is
        # never verified against a subset of the table the model was shown.
        subset = encode_protected_reference(
            SourceBoundToolResultSetRef(
                reference_id=claimed.pending_result_id,
                result_hash=content_hash,
                stamps=canonical_source_authority_stamps(stamps),
                evidence_ids=evidence_ids[:2],
            )
        )
        with pytest.raises(RunClaimEvidenceUnavailable) as subset_error:
            await runs.load_run_claim_evidence(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                turn_id="turn-1",
                protected_ref=subset,
            )
        assert subset_error.value.cause == "claim_contract"

        # A SET result cited as a single record is refused too — that request is
        # exactly the collapse to one arbitrary member.
        single = encode_protected_reference(
            SourceBoundToolResultRef(
                reference_id=claimed.pending_result_id,
                result_hash=content_hash,
                stamps=canonical_source_authority_stamps(stamps),
            )
        )
        with pytest.raises(RunClaimEvidenceUnavailable) as single_error:
            await runs.load_run_claim_evidence(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                turn_id="turn-1",
                protected_ref=single,
            )
        assert single_error.value.cause == "claim_contract"

        # A GENUINE store condition keeps its OWN reason. One member's locator is
        # corrupted in place: nothing about the answer changed, so reporting this
        # as a contract error would blame the model for a system fault.
        with engine.begin() as conn:
            conn.execute(
                ne_agent_evidence_locators.update()
                .where(ne_agent_evidence_locators.c.locator_id == claimed.evidence[2].locator_id)
                .values(locator_hash="0" * 64)
            )
        with pytest.raises(RunClaimEvidenceUnavailable) as store_error:
            await runs.load_run_claim_evidence(
                run_id=run.run_id,
                owner_principal_id="alice",
                attempt_id=attempt.attempt_id,
                turn_id="turn-1",
                protected_ref=set_ref,
            )
        assert store_error.value.cause == "evidence_store"
        assert store_error.value.safe_reason != subset_error.value.safe_reason
        assert "store condition" in store_error.value.safe_reason

    asyncio.run(scenario())
    engine.dispose()


def test_run_claim_evidence_failures_publish_a_reason_and_name_their_cause() -> None:
    """A bare `LookupError` must never escape: every refusal carries a C12-safe
    `safe_reason` that `run_workflow` publishes as the run's `error_reason`."""

    for cause in ("claim_contract", "evidence_store"):
        error = RunClaimEvidenceUnavailable(cause)  # type: ignore[arg-type]
        assert isinstance(error, LookupError)
        assert error.cause == cause
        assert error.safe_reason
        assert len(error.safe_reason) <= 256
        assert all(character.isprintable() for character in error.safe_reason)
        assert (
            published_failure_reason(
                safe_reason=error.safe_reason, failure_class=type(error).__name__
            )
            == error.safe_reason
        )
    assert RunClaimEvidenceUnavailable("claim_contract").safe_reason != (
        RunClaimEvidenceUnavailable("evidence_store").safe_reason
    )


def test_a_partial_multikey_set_with_coverage_member_loads_and_verifies() -> None:
    """NE-KG-MULTIKEY partial coverage, end to end through the STORE.

    The set holds two data rows PLUS one empty-member coverage entry (shaped
    exactly as `KnowledgeSourceEvidenceAuthor.claim_verified_set` now declares it:
    `verified_t3_empty` detail with no row anchor, receipt-only locator). The
    loader must admit the mixed set, and a claim quoting a served row must still
    verify — the coverage entry attests absence, it never displaces a row.
    """

    _engine, sessions, runs = _stores()
    second = _stamp().model_copy(
        update={"authority_receipt_id": "receipt-b", "authority_receipt_hash": "e" * 64}
    )
    stamps = (_stamp(), second)
    note = (
        "\n[note: 1 of 3 requested q_key values returned no rows in the "
        "caller-chosen window 2026-07-25..2026-08-25: key-3 — a fact about "
        "this query's result only, not evidence that the data does not exist]"
    )
    rendering = _t3_set_rendering() + note.encode("utf-8")
    content_hash = hashlib.sha256(rendering).hexdigest()

    base = _t3_set_declaration(stamps)
    empty_receipt_hash = hashlib.sha256(b"receipt-empty-member").hexdigest()
    empty_receipt = {
        "receipt_schema_version": 1,
        "template_id": "reg_e_provisional_credits",
        "template_version": 1,
        "canonical_args_hash": "a" * 64,
        "dataset_graphs_hash": "b" * 64,
        "contributor_schema_version": 1,
        "contributors": [stamps[0].model_dump(mode="json")],
        "contributor_count": 1,
        "contributor_set_hash": "1" * 64,
        "receipt_hash": empty_receipt_hash,
    }
    empty_anchor = hashlib.sha256(
        b"ne.verified-evidence-empty-member-anchor.v1\x00" + empty_receipt_hash.encode("ascii")
    ).hexdigest()
    empty_locator = json.dumps(
        {"kind": "verified_t3_empty", "receipt": empty_receipt, "evidence_assurance": "direct"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    coverage_row = EvidenceRowDeclaration(
        row_anchor=empty_anchor,
        receipt_hash=empty_receipt_hash,
        contributors=(
            EvidenceContributorDeclaration(
                locator_plaintext=empty_locator,
                source_stamp=stamps[0],
                assurance=EvidenceAssurance.DIRECT,
                safe_event_detail={
                    "read_kind": "verified_t3_empty",
                    "evidence_status": "located",
                    "contributor_count": 1,
                    "evidence_assurance": "direct",
                    "receipt_hash": empty_receipt_hash,
                },
            ),
        ),
    )
    declaration = EvidenceSetDeclaration(
        turn_id=base.turn_id,
        step_id=base.step_id,
        tool_call_id=base.tool_call_id,
        read_ordinal=base.read_ordinal,
        content_hash=content_hash,
        pending_result_plaintext=rendering,
        rows=(*base.rows, coverage_row),
    )

    async def scenario() -> None:
        _, run = await _create_run(sessions, runs)
        attempt = await runs.link_attempt(run_id=run.run_id)
        claimed = await runs.claim_evidence_set(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            declaration=declaration,
        )
        await runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="tool_result_served",
            protected_ref=claimed.pending_result_id,
            protected_hash=content_hash,
            source_stamps=canonical_source_authority_stamps(stamps),
            receiving_attempt_id=attempt.attempt_id,
        )
        evidence_ids = tuple(item.evidence_id for item in claimed.evidence)
        assert len(evidence_ids) == 5  # two rows x two contributors + coverage entry
        set_ref = encode_protected_reference(
            SourceBoundToolResultSetRef(
                reference_id=claimed.pending_result_id,
                result_hash=content_hash,
                stamps=canonical_source_authority_stamps(stamps),
                evidence_ids=evidence_ids,
            )
        )

        observation = await runs.load_run_claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=attempt.attempt_id,
            turn_id="turn-1",
            protected_ref=set_ref,
        )
        assert isinstance(observation, RunClaimEvidenceSetObservation)
        assert tuple(member.evidence_id for member in observation.members) == evidence_ids

        answer = "Meridian Bank 412.55 posted and Northwind Trust 88.20 pending"
        raw = answer.encode()
        verified = verify_claim(
            ClaimSlice(
                claim_key="claim-partial-1",
                claim_content_hash=hashlib.sha256(raw).hexdigest(),
                span_start_byte=0,
                span_end_byte=len(raw),
                served_output_hash=hashlib.sha256(raw).hexdigest(),
                served_output=raw,
            ),
            VerifiedRowSetEvidenceObservation(
                protected_ref=observation.protected_ref,
                protected_result_hash=observation.protected_result_hash,
                evidence_set_id=observation.evidence_set_id,
                set_key=observation.set_key,
                delivery_receipt_id=observation.delivery_receipt_id,
                delivery_receipt_hash=observation.delivery_receipt_hash,
                protected_rendering=observation.protected_rendering,
                members=tuple(
                    VerifiedRowSetMemberObservation(
                        evidence_id=member.evidence_id,
                        locator_id=member.locator_id,
                        locator_hash=member.locator_hash,
                        producing_event_id=member.producing_event_id,
                        locator=member.locator,
                    )
                    for member in observation.members
                ),
            ),
        )
        assert verified.member_evidence_ids == evidence_ids
        assert verified.evidence_set_id == claimed.evidence_set_id

    asyncio.run(scenario())


def test_sqlite_pre_consume_continuation_constraint_is_migrated_at_boot() -> None:
    """An existing DB whose status CHECK predates ``consumed`` migrates at boot.

    ``metadata.create_all`` never replaces an existing check constraint, so a
    database created before the consume fence still refuses ``consumed`` — and
    the FIRST approval resume after deploy would die on its own fence CAS.
    ``ensure_run_schema`` must repair it (rows preserved), not leave a manual
    runbook DDL as the only line of defense.
    """

    engine, sessions, runs = _stores()
    ensure_approval_schema(engine)
    approvals = SqlAlchemyApprovalStore(engine)
    checkpoints = RunApprovalCheckpointStore(runs, approvals)

    async def seed() -> str:
        _, run_row = await _create_run(sessions, runs)
        await runs.transition(
            run_id=run_row.run_id, expected=RunState.SUBMITTED, target=RunState.QUEUED
        )
        await runs.transition(
            run_id=run_row.run_id, expected=RunState.QUEUED, target=RunState.RUNNING
        )
        attempt = await runs.link_attempt(run_id=run_row.run_id)
        now = datetime.now(UTC)
        joined = await checkpoints.create_wait(
            run_id=run_row.run_id,
            attempt_id=attempt.attempt_id,
            step_id="step-1",
            tool_call_id="tool-call-1",
            action="write.external.email.send",
            action_hash="a" * 64,
            tier=2,
            principal_id="alice",
            policy_hash="b" * 64,
            invocation_plaintext=b'{"recipient":"x"}',
            source_stamps=(_stamp(),),
            stamp_receipt_id="opaque-receipt-1",
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
        return joined.checkpoint_id

    checkpoint_id = asyncio.run(seed())

    # Regress the table to its exact PRE-consume DDL, preserving the row —
    # the shape every database created before the fix is in right now.
    table = ne_agent_run_continuation_checkpoints
    with engine.connect() as conn:
        driver = conn.connection.driver_connection
        assert driver is not None
        cursor = driver.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()
        try:
            with conn.begin():
                ddl = str(
                    conn.execute(
                        sa.text(
                            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"
                        ),
                        {"name": table.name},
                    ).scalar_one()
                )
                assert "'consumed'" in ddl
                rows = [dict(row._mapping) for row in conn.execute(sa.select(table))]
                assert len(rows) == 1
                conn.exec_driver_sql(f"DROP TABLE {table.name}")
                conn.exec_driver_sql(ddl.replace("'consumed',", ""))
                for row in rows:
                    conn.execute(table.insert().values(**row))
        finally:
            if conn.in_transaction():
                conn.rollback()
            cursor = driver.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    # Precondition: the regressed database refuses `consumed` outright.
    with engine.connect() as conn, pytest.raises(IntegrityError), conn.begin():
        conn.execute(
            table.update().where(table.c.checkpoint_id == checkpoint_id).values(status="consumed")
        )

    # Boot. The compatibility migration must rebuild the constraint in place.
    ensure_run_schema(engine)

    with engine.connect() as conn:
        checks = {
            str(item.get("name")): "".join(str(item.get("sqltext") or "").lower().split())
            for item in sa.inspect(conn).get_check_constraints(table.name)
        }
        assert "'consumed'" in checks["ck_run_continuation_status"], (
            "boot must admit the consume fence on an existing database; the first "
            "approval resume dies on its own CAS otherwise"
        )
        survivor = conn.execute(sa.select(table)).one()
        assert (survivor.checkpoint_id, survivor.status) == (checkpoint_id, "pending")
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    # ...and the fence value itself is now writable (schema probe, reverted).
    with engine.begin() as conn:
        conn.execute(
            table.update().where(table.c.checkpoint_id == checkpoint_id).values(status="consumed")
        )
        conn.execute(
            table.update().where(table.c.checkpoint_id == checkpoint_id).values(status="pending")
        )
    # Idempotent on the repaired database.
    ensure_run_schema(engine)
    engine.dispose()
