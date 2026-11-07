# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-010B focused durable Run, restart and audit-receipt proofs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.sql.dml import Delete, Insert, Update

import ne.runtime.run_runner as run_runner_module
from ne.audit import PostgresAuditSink, ensure_audit_schema, records_by_event_ids
from ne.contracts import freeze_config_hash
from ne.contracts.events import AuditEventLike
from ne.contracts.governance import AuditVerification
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.contracts.source_authority import (
    EvidenceAssurance,
    M365SourcePoint,
    SourceAuthorityStamp,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.contracts.types import ChatMessage, Principal
from ne.events import new_audit_event
from ne.gateway.errors import AirGapViolationError
from ne.runtime.errors import AgentOutputContractError, RuntimeAuditError
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_audit_receipts import (
    RunAuditReceiptSink,
    RunAuditTarget,
    current_run_tool_call,
)
from ne.runtime.run_checkpoint import RunApprovalJoin, RunApprovalWaiting
from ne.runtime.run_finalizer import (
    RunTurnTarget,
    StructuredRunOutputEnvelope,
    claim_set_hash,
    derived_failure_reason,
)
from ne.runtime.run_runner import (
    ReceiptGatedGateway,
    ReceiptGatedToolExecutor,
    RunExecutionContext,
    RunExecutionResult,
    RunLeaseLost,
)
from ne.runtime.run_store import (
    AgentRunStore,
    EvidenceDeclaration,
    RunOutputVerdict,
    RunState,
    RunSubmissionConflictError,
    ensure_run_schema,
    ne_agent_protected_source_outputs,
    ne_agent_protected_turn_contents,
    ne_agent_run_attempt_links,
    ne_agent_run_audit_receipts,
    ne_agent_run_claims,
    ne_agent_run_enqueue_outbox,
    ne_agent_run_events,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_workflow import (
    RunWorkflowRequest,
    RunWorkflowService,
    RunWorkflowStateStore,
    _LeaseExecutionBoundary,
    ensure_run_workflow_schema,
    ne_agent_run_execution_checkpoints,
    ne_agent_run_lifecycle_outbox,
    ne_agent_run_workflow_leases,
)
from ne.runtime.source_results import complete_run_delivery_observation
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema
from ne.schema import AuditOutcome


class _Config(BaseModel):
    generation: str = "pre-010b-test"


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        return Completion(
            text="protected elsewhere",
            model_id=request.requested_model or "private",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    def stream(self, request: CompletionRequest) -> Any:
        raise AssertionError(f"unexpected stream for {request.task}")


def _authenticated_context() -> AuthenticatedRequestContext:
    return AuthenticatedRequestContext(
        principal_id="alice",
        authorization_groups=(),
        department="finance",
        issuer="https://issuer.example.test",
        expires_at=4_102_444_800,
    )


def _source_stamp() -> SourceAuthorityStamp:
    source = SourceIdentity(
        deployment_id="deployment-a",
        source_kind="m365-sharepoint",
        connector_instance_id="connector-a",
        point=M365SourcePoint(
            graph_tenant_id="tenant-a",
            site_id="site-a",
            drive_id="drive-a",
            item_id="item-a",
        ),
        generation=1,
    )
    contract = SourceContractIdentity(
        source_kind="m365-sharepoint",
        descriptor_schema_version=1,
        descriptor_hash="a" * 64,
    )
    return SourceAuthorityStamp(
        source_identity=source,
        artifact_identity=SourceContentArtifactIdentity(
            source_identity=source,
            artifact_schema_version=1,
            source_snapshot_hash="b" * 64,
            content_snapshot_hash="c" * 64,
            acl_snapshot_hash="d" * 64,
            source_contract=contract,
        ),
        selected_head_revision="head-a",
        acl_snapshot_hash="d" * 64,
        source_contract=contract,
        contract_epoch=1,
        authority_receipt_id="authority-a",
        authority_receipt_hash="e" * 64,
    )


class _Executor:
    @property
    def tool_specs(self) -> tuple[Any, ...]:
        return ()

    async def execute(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("test adapter invokes no Tool")


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.recovery_flags: list[bool] = []

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        self.calls += 1
        self.recovery_flags.append(context.recovered_uncertain_model_attempt)
        await context.gateway.complete(
            CompletionRequest(
                messages=(ChatMessage(role="user", content=context.input_bytes.decode("utf-8")),),
                task="agent-run",
                principal_id=context.run.owner_principal_id,
            )
        )
        complete_run_delivery_observation()
        return _complete_result()


class _RaisingAdapter:
    """Fails execution with an arbitrary exception the caller supplies."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        del context
        raise self._error


class _MustNotRunAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        del context
        self.calls += 1
        raise AssertionError("cancellation scheduled new execution work")


class _BlockingGateway(_Gateway):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return Completion(
            text="protected elsewhere",
            model_id=request.requested_model or "private",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )


class _BlockingExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def tool_specs(self) -> tuple[Any, ...]:
        return ()

    async def execute(self, *_args: Any, **_kwargs: Any) -> str:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "protected elsewhere"


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def tool_specs(self) -> tuple[Any, ...]:
        return ()

    async def execute(self, *_args: Any, **_kwargs: Any) -> str:
        self.calls += 1
        return "protected elsewhere"


class _TwoEffectAdapter:
    def __init__(self, effect: str) -> None:
        self.effect = effect

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        if self.effect == "model":
            request = CompletionRequest(
                messages=(ChatMessage(role="user", content="protected"),),
                task="agent-run",
                principal_id="alice",
            )
            await context.gateway.complete(request)
            await context.gateway.complete(request)
        else:
            principal = Principal(id="alice")
            await context.executor.execute("read.knowledge.search", {}, principal=principal)
            await context.executor.execute("read.knowledge.search", {}, principal=principal)
        complete_run_delivery_observation()
        return _complete_result()


class _OutcomeAdapter:
    def __init__(self, outcome: str, exhausted_reason: str | None = None) -> None:
        self.outcome = outcome
        self.exhausted_reason = exhausted_reason

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        del context
        return RunExecutionResult(
            outcome=self.outcome,  # type: ignore[arg-type]
            exhausted_reason=self.exhausted_reason,
        )


class _WaitingApprovalAdapter:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
        with self._engine.begin() as conn:
            changed = conn.execute(
                ne_agent_runs.update()
                .where(
                    ne_agent_runs.c.run_id == context.run.run_id,
                    ne_agent_runs.c.status == RunState.RUNNING.value,
                )
                .values(
                    status=RunState.WAITING_APPROVAL.value,
                    version=ne_agent_runs.c.version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            assert changed.rowcount == 1
        raise RunApprovalWaiting(
            RunApprovalJoin(
                approval_id="approval-waiting-a",
                run_id=context.run.run_id,
                attempt_id=context.attempt_id,
                checkpoint_id="checkpoint-waiting-a",
                action_hash="a" * 64,
                principal_id=context.run.owner_principal_id,
                policy_hash="b" * 64,
                protected_invocation_id="invocation-waiting-a",
                protected_invocation_hash="c" * 64,
                tier=2,
                action="external.write",
            )
        )


def _complete_result() -> RunExecutionResult:
    output = b"private final output"
    output_hash = hashlib.sha256(output).hexdigest()
    claims = ()
    return RunExecutionResult(
        outcome="execution_complete",
        output_bytes=output,
        structured_envelope=StructuredRunOutputEnvelope(
            discriminator="structured_run_output",
            served_output_hash=output_hash,
            claim_count=0,
            claim_set_hash=claim_set_hash(output_hash, claims),
            claims=claims,
        ),
    )


class _TerminalFailOnceSink:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.failed = False

    async def append(self, event: AuditEventLike) -> None:
        if getattr(event, "action", None) == "run.lifecycle.terminal" and not self.failed:
            self.failed = True
            raise RuntimeError("injected terminal audit outage")
        await self.inner.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return await self.inner.verify(start=start, end=end)


class _BlockingAuditSink:
    def __init__(self, inner: Any, *, action: str) -> None:
        self.inner = inner
        self.action = action
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, event: AuditEventLike) -> None:
        if getattr(event, "action", None) == self.action:
            self.started.set()
            await self.release.wait()
        await self.inner.append(event)

    async def verify(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> AuditVerification:
        return await self.inner.verify(start=start, end=end)


def _stall_event_loop_past_test_lease() -> None:
    """Model scheduler starvation longer than the focused 0.15 second lease."""
    time.sleep(0.2)


@dataclass
class _Harness:
    runtime: sa.Engine
    audit: sa.Engine
    sessions: SqlAlchemySessionStore
    runs: AgentRunStore
    sink: PostgresAuditSink
    config_hash: str


def _harness(tmp_path: Path) -> _Harness:
    config_hash = freeze_config_hash(_Config())
    runtime = sa.create_engine(f"sqlite:///{tmp_path}/runtime.db")
    audit = sa.create_engine(f"sqlite:///{tmp_path}/audit.db")
    ensure_runtime_schema(runtime)
    ensure_run_schema(runtime)
    ensure_run_workflow_schema(runtime)
    ensure_audit_schema(audit)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    return _Harness(
        runtime=runtime,
        audit=audit,
        sessions=SqlAlchemySessionStore(runtime),
        runs=AgentRunStore(runtime, cipher),
        sink=PostgresAuditSink(audit),
        config_hash=config_hash,
    )


def _engine_harness(runtime: sa.Engine, audit: sa.Engine) -> _Harness:
    config_hash = freeze_config_hash(_Config())
    ensure_runtime_schema(runtime)
    ensure_run_schema(runtime)
    ensure_run_workflow_schema(runtime)
    ensure_audit_schema(audit)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    return _Harness(
        runtime=runtime,
        audit=audit,
        sessions=SqlAlchemySessionStore(runtime),
        runs=AgentRunStore(runtime, cipher),
        sink=PostgresAuditSink(audit),
        config_hash=config_hash,
    )


async def _admitted_run(harness: _Harness):  # type: ignore[no-untyped-def]
    conversation = await harness.sessions.create_conversation("agent-a", "finance", "alice")
    accepted = await harness.runs.admit_start(
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
            config_hash=harness.config_hash,
            capability_snapshot_hash="a" * 64,
            capability_snapshot_receipt_id="snapshot-receipt",
            authenticated_context=_authenticated_context(),
        )
    )
    run = await harness.runs.get_run(run_id=accepted.run_id, owner_principal_id="alice")
    assert run is not None
    return run, accepted


async def _queued_run(harness: _Harness):  # type: ignore[no-untyped-def]
    run, _ = await _admitted_run(harness)
    return run


def test_source_observations_receive_exact_audit_receipts_before_finalization(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run, accepted = await _admitted_run(harness)
        protected = b"protected source result"
        evidence = await harness.runs.claim_evidence(
            run_id=run.run_id,
            owner_principal_id="alice",
            attempt_id=accepted.attempt_id,
            declaration=EvidenceDeclaration(
                turn_id="turn-a",
                step_id="run-tool",
                tool_call_id="run-tool:tool:0",
                read_ordinal=0,
                content_hash=hashlib.sha256(protected).hexdigest(),
                safe_event_detail={
                    "read_kind": "search",
                    "evidence_status": "located",
                    "contributor_count": 1,
                    "evidence_assurance": "direct",
                },
                locator_plaintext=b"{}",
                pending_result_plaintext=protected,
                source_stamps=(_source_stamp(),),
                assurance=EvidenceAssurance.DIRECT,
            ),
        )
        delivery_id = await harness.runs.claim_delivery(
            run_id=run.run_id,
            owner_principal_id="alice",
            delivery_kind="direct_result_served",
            protected_ref=evidence.pending_result_id,
            protected_hash=hashlib.sha256(protected).hexdigest(),
            source_stamps=(_source_stamp(),),
            receiving_attempt_id=accepted.attempt_id,
        )
        sink = RunAuditReceiptSink(
            harness.sink,
            audit_engine=harness.audit,
            runtime_engine=harness.runtime,
            run_store=harness.runs,
            target=RunAuditTarget(
                run_id=run.run_id,
                attempt_id=accepted.attempt_id,
                step_id="run-execution",
            ),
            config_hash=harness.config_hash,
        )
        evidence_receipt = await sink.append_evidence_receipt(
            producing_event_id=evidence.producing_event_id,
            actor="alice",
        )
        delivery_receipt = await sink.append_delivery_receipt(
            delivery_receipt_id=delivery_id,
            actor="alice",
        )
        assert evidence_receipt.decision_kind.value == "evidence"
        assert evidence_receipt.run_event_id == evidence.producing_event_id
        assert delivery_receipt.decision_kind.value == "delivery"
        with harness.runtime.connect() as conn:
            delivery_event_id = str(
                conn.execute(
                    sa.text(
                        "SELECT event_id FROM ne_agent_delivery_receipts "
                        "WHERE delivery_receipt_id = :receipt_id"
                    ),
                    {"receipt_id": delivery_id},
                ).scalar_one()
            )
            kinds = list(
                conn.execute(
                    sa.text(
                        "SELECT decision_kind FROM ne_agent_run_audit_receipts "
                        "WHERE run_id = :run_id ORDER BY decision_kind"
                    ),
                    {"run_id": run.run_id},
                ).scalars()
            )
        assert delivery_receipt.run_event_id == delivery_event_id
        assert kinds == ["delivery", "evidence"]
        assert (
            len(
                records_by_event_ids(
                    harness.audit,
                    [evidence_receipt.audit_event_id, delivery_receipt.audit_event_id],
                )
            )
            == 2
        )
        assert (
            await sink.append_evidence_receipt(
                producing_event_id=evidence.producing_event_id,
                actor="alice",
            )
        ).run_audit_receipt_id == evidence_receipt.run_audit_receipt_id
        assert (
            await sink.append_delivery_receipt(
                delivery_receipt_id=delivery_id,
                actor="alice",
            )
        ).run_audit_receipt_id == delivery_receipt.run_audit_receipt_id

    asyncio.run(scenario())


def _workflow_request(harness: _Harness, run: Any, *, dispatch_id: str) -> RunWorkflowRequest:
    with harness.runtime.begin() as conn:
        attempt_id = str(
            conn.execute(
                sa.select(ne_agent_run_attempt_links.c.attempt_id).where(
                    ne_agent_run_attempt_links.c.run_id == run.run_id
                )
            ).scalar_one()
        )
        existing = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == run.run_id
            )
        ).one_or_none()
        if existing is None:
            target = RunTurnTarget(
                conversation_id=run.conversation_id,
                run_id=run.run_id,
                turn_id=f"turn-{run.run_id}",
                turn_sequence=1,
                attachment_id=f"attachment-{run.run_id}",
                attempt_id=attempt_id,
            )
            conn.execute(
                ne_agent_run_turn_targets.insert().values(
                    **target.model_dump(), created_at=datetime.now(UTC)
                )
            )
        else:
            target = RunTurnTarget(
                conversation_id=str(existing.conversation_id),
                run_id=str(existing.run_id),
                turn_id=str(existing.turn_id),
                turn_sequence=int(existing.turn_sequence),
                attachment_id=str(existing.attachment_id),
                attempt_id=str(existing.attempt_id),
            )
    return RunWorkflowRequest(
        dispatch_id=dispatch_id,
        run_id=run.run_id,
        owner_principal_id="alice",
        config_hash=harness.config_hash,
        target=target,
        authenticated_context=_authenticated_context(),
    )


async def _cancel_run(harness: _Harness, run: Any):  # type: ignore[no-untyped-def]
    current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
    assert current is not None
    return await harness.runs.admit_cancel(
        SimpleNamespace(
            idempotency_key=f"cancel-{uuid.uuid4()}",
            request_hash=uuid.uuid4().hex * 2,
            run_id=run.run_id,
            expected_version=current.version,
            owner_principal_id="alice",
            agent_id="agent-a",
            agent_version=1,
            strategy="agentic",
            config_hash=harness.config_hash,
            capability_snapshot_hash="a" * 64,
            capability_snapshot_receipt_id="snapshot-receipt",
        )
    )


async def _legacy_unadmitted_run(harness: _Harness):  # type: ignore[no-untyped-def]
    conversation = await harness.sessions.create_conversation("agent-a", "finance", "alice")
    return await harness.runs.create_run(
        conversation_id=conversation.conversation_id,
        owner_principal_id="alice",
        department="finance",
        conversation_agent_name="agent-a",
        agent_id="agent-a",
        agent_version=1,
        strategy="agentic",
        config_hash=harness.config_hash,
        capability_snapshot_hash="a" * 64,
        capability_snapshot_receipt_id="snapshot-receipt",
        input_plaintext=b"protected prompt",
    )


def test_restart_renews_only_the_joined_lease_and_records_model_uncertainty(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    gateway = _Gateway()
    adapter = _RecordingAdapter()
    state = RunWorkflowStateStore(harness.runtime)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=gateway,
        executor=_Executor(),
        adapter=adapter,
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run, accepted = await _admitted_run(harness)
        request = _workflow_request(harness, run, dispatch_id="dispatch-a")
        first = state.claim(
            dispatch_id=request.dispatch_id,
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        assert first.attempt_id == accepted.attempt_id
        state.checkpoint(first, stage="execution_started", safe_detail={"status": "started"})
        with harness.runtime.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE ne_agent_run_workflow_leases SET expires_at = :expired "
                    "WHERE run_id = :run_id"
                ),
                {"expired": datetime(2000, 1, 1, tzinfo=UTC), "run_id": run.run_id},
            )
        result = await service.execute(request)
        assert result.outcome == "execution_complete"
        assert result.protected_output_kind == "protected_turn_content"
        assert result.protected_result_ref is not None
        assert result.target_hash is not None
        assert result.delivery_set_hash is not None
        assert result.audit_receipt_set_hash is not None
        assert adapter.recovery_flags == [True]
        assert gateway.calls == 1

        with pytest.raises(ValueError, match="terminal Run workflow"):
            state.claim(
                dispatch_id="foreign-dispatch",
                run_id=run.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
        events = await harness.runs.list_events(
            run_id=run.run_id, owner_principal_id="alice", limit=100
        )
        assert sum(event.event_kind == "run.execution.claimed" for event in events.events) == 1
        assert sum(event.event_kind == "run.execution.recovered" for event in events.events) == 1
        assert any(
            event.event_kind == "run.audit.intent" and event.step_id == "run-execution-recovery"
            for event in events.events
        )
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None and current.status is RunState.COMPLETED
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_lifecycle_outbox)
                ).scalar_one()
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("effect", "step_id", "audit_action"),
    [
        ("model", "run-model", "run.model.pre_effect"),
        ("tool", "run-tool", "run.tool.pre_effect"),
    ],
)
def test_crash_after_effect_issue_rotates_lease_without_reissuing_effect(
    tmp_path: Path,
    effect: str,
    step_id: str,
    audit_action: str,
) -> None:
    harness = _harness(tmp_path)
    gateway = _Gateway()
    executor = _CountingExecutor()
    state = RunWorkflowStateStore(harness.runtime, lease_seconds=0.15)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=gateway,
        executor=executor,
        adapter=_TwoEffectAdapter(effect),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id=f"dispatch-crash-{effect}")
        first = state.claim(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            owner_principal_id=request.owner_principal_id,
            config_hash=request.config_hash,
        )
        guard = state.begin_effect_settlement(first)
        try:
            state.checkpoint(first, stage="execution_started", safe_detail={"status": "started"})
            await RunAuditReceiptSink(
                harness.sink,
                audit_engine=harness.audit,
                runtime_engine=harness.runtime,
                run_store=harness.runs,
                target=RunAuditTarget(
                    run_id=run.run_id,
                    attempt_id=first.attempt_id,
                    step_id="run-execution",
                    event_ordinal=0,
                ),
                config_hash=harness.config_hash,
            ).append(
                new_audit_event(
                    action="run.execution.pre_effect",
                    resource=f"run:{run.run_id}",
                    outcome=AuditOutcome.success,
                    actor="alice",
                    config_hash=harness.config_hash,
                )
            )
            await RunAuditReceiptSink(
                harness.sink,
                audit_engine=harness.audit,
                runtime_engine=harness.runtime,
                run_store=harness.runs,
                target=RunAuditTarget(
                    run_id=run.run_id,
                    attempt_id=first.attempt_id,
                    step_id=step_id,
                    tool_call_id="run-tool:tool:0" if effect == "tool" else None,
                    event_ordinal=0,
                ),
                config_hash=harness.config_hash,
            ).append(
                new_audit_event(
                    action=audit_action,
                    resource=f"crashed-{effect}",
                    outcome=AuditOutcome.success,
                    actor="alice",
                    config_hash=harness.config_hash,
                )
            )
            state.checkpoint(
                first,
                stage=f"effect_issued:crashed-{effect}",
                safe_detail={"effect_stage": f"run-{effect}:wait:0", "settlement": "issued"},
                required_run_status=RunState.RUNNING,
            )
        finally:
            # Model a process/session death: the OS/DB guard disappears while
            # the durable SETTLING sentinel and issued checkpoint remain.
            guard.close()

        result = await service.execute(request)
        assert result.outcome == "failed"
        assert gateway.calls == 0
        assert executor.calls == 0
        # THE FLOOR. This path checkpoints nothing of its own, so the sole
        # terminal writer is the only thing that keeps the Run from failing in
        # silence: the reader is told the class, and told nothing else.
        settlement = state.get_checkpoint(
            run_id=run.run_id,
            attempt_id=first.attempt_id,
            stage="execution_failed",
        )
        assert settlement is not None
        assert settlement.safe_detail == {
            "error_class": "RunEffectSettlementUncertain",
            "error_reason": derived_failure_reason("RunEffectSettlementUncertain"),
        }
        with harness.runtime.connect() as conn:
            lease = conn.execute(
                sa.select(ne_agent_run_workflow_leases).where(
                    ne_agent_run_workflow_leases.c.run_id == run.run_id
                )
            ).one()
            assert lease.lease_epoch == first.lease_epoch + 1
            assert lease.lease_token != first.lease_token
            assert lease.state == "released"

    asyncio.run(scenario())


def test_execution_complete_uses_the_atomic_source_free_finalizer(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime),
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_RecordingAdapter(),
        config_hash=harness.config_hash,
    )
    writes: list[str] = []

    def observe_writes(
        _conn: Any,
        clause: Any,
        _multiparams: Any,
        _params: Any,
        _options: Any,
    ) -> None:
        if isinstance(clause, (Insert, Update, Delete)):
            table = clause.table
            writes.append(table.name if isinstance(table, sa.Table) else str(table))

    async def scenario() -> None:
        run = await _queued_run(harness)
        event.listen(harness.runtime, "before_execute", observe_writes)
        try:
            result = await service.execute(
                _workflow_request(harness, run, dispatch_id="dispatch-no-finalizer-writes")
            )
        finally:
            event.remove(harness.runtime, "before_execute", observe_writes)
        assert result.outcome == "execution_complete"
        # POST_DEMO item 4's CONTROL. An ordinary run — no approval, no resume —
        # must keep the exact word it always had, in the PERSISTED row and not
        # merely in the returned result. Without this, widening the outcome
        # vocabulary could silently re-label every completed run in the system and
        # every test above would still pass.
        with harness.runtime.connect() as control_conn:
            assert (
                control_conn.execute(
                    sa.select(ne_agent_run_lifecycle_outbox.c.outcome).where(
                        ne_agent_run_lifecycle_outbox.c.run_id == run.run_id
                    )
                ).scalar_one()
                == "execution_complete"
            )
        assert ne_agent_protected_turn_contents.name in writes
        assert ne_agent_run_lifecycle_outbox.name in writes
        assert ne_agent_protected_source_outputs.name not in writes
        assert ne_agent_run_claims.name not in writes
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
                ).scalar_one()
                == 1
            )
            for table in (ne_agent_run_claims, ne_agent_protected_source_outputs):
                assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0

    asyncio.run(scenario())


def test_execution_complete_passes_exact_evidence_and_verifier_bindings_to_finalizer(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    evidence = SimpleNamespace(evidence_key="evidence-local-1")
    verifier = SimpleNamespace(verifier_key="verifier-local-1")

    class _BindingAdapter:
        async def execute(self, context: RunExecutionContext) -> RunExecutionResult:
            await context.gateway.complete(
                CompletionRequest(
                    messages=(ChatMessage(role="user", content="protected"),),
                    task="agent-run",
                    principal_id=context.run.owner_principal_id,
                )
            )
            complete_run_delivery_observation()
            complete = _complete_result()
            return replace(
                complete,
                evidence_bindings=(evidence,),  # type: ignore[arg-type]
                verifier_bindings=(verifier,),  # type: ignore[arg-type]
            )

    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime),
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_BindingAdapter(),
        config_hash=harness.config_hash,
    )
    inner = service._finalizer
    observed: dict[str, object] = {}

    class _RecordingFinalizer:
        async def observe(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            observed["evidence"] = kwargs["evidence"]
            observed["verifiers"] = kwargs["verifiers"]
            return await inner.observe(target=kwargs["target"], delivery=kwargs["delivery"])

        async def finalize(self, candidate: Any, *, lease: Any):  # type: ignore[no-untyped-def]
            return await inner.finalize(candidate, lease=lease)

    service._finalizer = _RecordingFinalizer()  # type: ignore[assignment]

    async def scenario() -> None:
        run = await _queued_run(harness)
        result = await service.execute(_workflow_request(harness, run, dispatch_id="dispatch-bind"))
        assert result.outcome == "execution_complete"

    asyncio.run(scenario())
    assert observed == {"evidence": (evidence,), "verifiers": (verifier,)}


def test_expired_lease_takeover_reuses_the_exact_dispatch_attempt_join(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run, accepted = await _admitted_run(harness)
        state = RunWorkflowStateStore(harness.runtime)
        first = state.claim(
            dispatch_id="dispatch-a",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        assert first.attempt_id == accepted.attempt_id
        with harness.runtime.connect() as conn:
            attempts = conn.execute(
                sa.select(ne_agent_run_attempt_links).where(
                    ne_agent_run_attempt_links.c.run_id == run.run_id
                )
            ).all()
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].predecessor_attempt_id is None
            assert (
                conn.execute(
                    sa.select(ne_agent_run_enqueue_outbox.c.claimed_at).where(
                        ne_agent_run_enqueue_outbox.c.enqueue_id == accepted.enqueue_id
                    )
                ).scalar_one()
                is not None
            )
        prior_expiry = state.probe(first).expires_at
        renewed = state.renew(first)
        assert renewed.expires_at >= prior_expiry
        state.checkpoint(first, stage="execution_started", safe_detail={"status": "started"})
        with pytest.raises(RuntimeError, match="active worker"):
            state.claim(
                dispatch_id="dispatch-a",
                run_id=run.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
        with harness.runtime.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE ne_agent_run_workflow_leases "
                    "SET expires_at = :expired WHERE run_id = :run_id"
                ),
                {"expired": datetime(2000, 1, 1, tzinfo=UTC), "run_id": run.run_id},
            )
        recovered = state.claim(
            dispatch_id="dispatch-a",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        assert recovered.attempt_id == first.attempt_id
        assert recovered.lease_epoch == first.lease_epoch + 1
        assert recovered.recovered is True
        assert recovered.lease_token != first.lease_token
        stale_calls = (
            lambda: state.probe(first),
            lambda: state.renew(first),
            lambda: state.checkpoint(first, stage="stale", safe_detail={"status": "stale"}),
            lambda: state.terminalize(first, status=RunState.FAILED, outcome="failed"),
            lambda: state.release(first),
        )
        for call in stale_calls:
            with pytest.raises(RuntimeError, match=r"stale|lost"):
                call()
        with harness.runtime.connect() as conn:
            lease = conn.execute(
                sa.select(ne_agent_run_workflow_leases).where(
                    ne_agent_run_workflow_leases.c.run_id == run.run_id
                )
            ).one()
            assert lease.lease_token == recovered.lease_token

    asyncio.run(scenario())


def test_waiting_approval_cannot_replace_or_strand_its_execution_lease(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        run, _accepted = await _admitted_run(harness)
        first = state.claim(
            dispatch_id="dispatch-waiting",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        with harness.runtime.begin() as conn:
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == run.run_id)
                .values(
                    status=RunState.WAITING_APPROVAL.value,
                    version=ne_agent_runs.c.version + 1,
                )
            )
            conn.execute(
                ne_agent_run_workflow_leases.update()
                .where(ne_agent_run_workflow_leases.c.run_id == run.run_id)
                .values(expires_at=datetime(2000, 1, 1, tzinfo=UTC))
            )
        with pytest.raises(RuntimeError, match="waiting approval"):
            state.claim(
                dispatch_id="dispatch-waiting",
                run_id=run.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
        with harness.runtime.connect() as conn:
            lease = conn.execute(
                sa.select(ne_agent_run_workflow_leases).where(
                    ne_agent_run_workflow_leases.c.run_id == run.run_id
                )
            ).one()
            assert lease.lease_token == first.lease_token
            assert lease.lease_epoch == first.lease_epoch

    asyncio.run(scenario())


def test_typed_approval_wait_is_not_terminalized_as_execution_failure(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_WaitingApprovalAdapter(harness.runtime),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        result = await service.execute(
            _workflow_request(harness, run, dispatch_id="dispatch-waiting-control")
        )
        assert result.outcome == "waiting_approval"
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None and current.status is RunState.WAITING_APPROVAL
        events = await harness.runs.list_events(
            run_id=run.run_id, owner_principal_id="alice", limit=100
        )
        assert not any(event.event_kind == "run.execution.failed" for event in events.events)
        assert (
            state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=result.attempt_id,
                stage="execution_failed",
            )
            is None
        )
        with harness.runtime.connect() as conn:
            lease = conn.execute(
                sa.select(ne_agent_run_workflow_leases).where(
                    ne_agent_run_workflow_leases.c.run_id == run.run_id
                )
            ).one()
            assert lease.state == "released"

    asyncio.run(scenario())


def test_workflow_refuses_a_forged_delayed_authentication_snapshot(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    adapter = _RecordingAdapter()
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime),
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=adapter,
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        admitted = _workflow_request(harness, run, dispatch_id="dispatch-auth-context")
        forged = admitted.model_copy(
            update={
                "authenticated_context": AuthenticatedRequestContext(
                    principal_id="alice",
                    authorization_groups=("forged-admin",),
                    department="finance",
                    issuer="https://issuer.example.test",
                    expires_at=4_102_444_800,
                )
            }
        )
        with pytest.raises(ValueError, match="differs from admission"):
            await service.execute(forged)
        assert adapter.calls == 0

    asyncio.run(scenario())


def test_success_checkpoint_requires_running_so_durable_cancel_wins(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = state.claim(
            dispatch_id="dispatch-success-cancel-race",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        await _cancel_run(harness, run)
        with pytest.raises(RuntimeError, match="required lifecycle state"):
            state.checkpoint(
                claim,
                stage="execution_succeeded",
                safe_detail={"outcome": "execution_complete"},
                required_run_status=RunState.RUNNING,
            )
        assert (
            state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                stage="execution_succeeded",
            )
            is None
        )
        state.checkpoint(
            claim,
            stage="cancellation_reconciled",
            safe_detail={"scheduled_new_work": False},
        )
        state.terminalize(claim, status=RunState.CANCELLED, outcome="cancelled")
        state.release(claim)

    asyncio.run(scenario())


def test_cancelling_lease_requires_exact_cancel_admission(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = state.claim(
            dispatch_id="dispatch-forged-cancel",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        with harness.runtime.begin() as conn:
            conn.execute(
                ne_agent_runs.update()
                .where(ne_agent_runs.c.run_id == run.run_id)
                .values(
                    status=RunState.CANCELLING.value,
                    version=ne_agent_runs.c.version + 1,
                )
            )
        with pytest.raises(RuntimeError, match="exact durable cancel admission"):
            state.renew(claim)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "detail",
    [
        {
            "outcome": "execution_complete",
            "protected_result_ref": "protected-ref",
            "protected_result_hash": None,
        },
        {"outcome": "cancelled"},
    ],
)
def test_execution_checkpoint_replay_rejects_malformed_or_nonexecution_result(
    tmp_path: Path,
    detail: dict[str, object],
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)
    adapter = _MustNotRunAdapter()
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=adapter,
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = state.claim(
            dispatch_id="dispatch-poisoned-checkpoint",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        state.checkpoint(claim, stage="execution_succeeded", safe_detail=detail)
        state.release(claim)
        with pytest.raises(RuntimeError, match="execution checkpoint carries"):
            await service.execute(
                _workflow_request(harness, run, dispatch_id="dispatch-poisoned-checkpoint")
            )
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None and current.status is RunState.FAILED

    asyncio.run(scenario())


def test_initial_claim_refuses_zero_or_multiple_admission_attempts(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        unadmitted = await _legacy_unadmitted_run(harness)
        with pytest.raises(ValueError, match="exactly one admission-created attempt"):
            state.claim(
                dispatch_id="dispatch-zero",
                run_id=unadmitted.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )

        admitted, accepted = await _admitted_run(harness)
        extra = await harness.runs.link_attempt(
            run_id=admitted.run_id,
            predecessor_attempt_id=accepted.attempt_id,
        )
        assert extra.attempt_number == 2
        with pytest.raises(ValueError, match="exactly one admission-created attempt"):
            state.claim(
                dispatch_id="dispatch-many",
                run_id=admitted.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM ne_agent_run_workflow_claims "
                        "WHERE run_id IN (:zero, :many)"
                    ),
                    {"zero": unadmitted.run_id, "many": admitted.run_id},
                ).scalar_one()
                == 0
            )

    asyncio.run(scenario())


def test_cancellation_reconciles_without_scheduling_and_terminal_publication_is_once(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)
    adapter = _MustNotRunAdapter()
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=adapter,
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        await _cancel_run(harness, run)
        request = _workflow_request(harness, run, dispatch_id="dispatch-cancel")
        result = await service.execute(request)
        assert result.outcome == "cancelled"
        assert adapter.calls == 0
        terminal = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert terminal is not None
        assert terminal.status is RunState.CANCELLED
        events = await harness.runs.list_events(
            run_id=run.run_id, owner_principal_id="alice", limit=100
        )
        assert sum(event.event_kind == "run.terminal" for event in events.events) == 1
        with harness.runtime.connect() as conn:
            publication = conn.execute(sa.select(ne_agent_run_lifecycle_outbox)).one()
            assert publication.published_at is not None
            assert publication.audit_event_id is not None
            assert (
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM ne_agent_run_audit_receipts "
                        "WHERE run_id = :run_id AND decision_kind = 'terminal'"
                    ),
                    {"run_id": run.run_id},
                ).scalar_one()
                == 1
            )
            for table in (
                ne_agent_run_claims,
                ne_agent_protected_source_outputs,
                ne_agent_protected_turn_contents,
            ):
                assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
        with (
            harness.runtime.begin() as conn,
            pytest.raises(IntegrityError, match="Run receipt row is append-only"),
        ):
            conn.execute(
                ne_agent_run_audit_receipts.delete().where(
                    ne_agent_run_audit_receipts.c.audit_event_id == publication.audit_event_id
                )
            )
        assert (await service.execute(request)).outcome == "cancelled"
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_lifecycle_outbox)
                ).scalar_one()
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("effect", "audit_action"),
    [("model", "run.model.pre_effect"), ("tool", "run.tool.pre_effect")],
)
def test_cancel_during_receipt_persistence_blocks_the_external_effect(
    tmp_path: Path,
    effect: str,
    audit_action: str,
) -> None:
    harness = _harness(tmp_path)
    gateway = _Gateway()
    executor = _CountingExecutor()
    sink = _BlockingAuditSink(harness.sink, action=audit_action)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime, lease_seconds=0.15),
        run_store=harness.runs,
        sink=sink,
        audit_engine=harness.audit,
        gateway=gateway,
        executor=executor,
        adapter=_TwoEffectAdapter(effect),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        task = asyncio.create_task(
            service.execute(
                _workflow_request(harness, run, dispatch_id=f"dispatch-receipt-{effect}")
            )
        )
        await asyncio.wait_for(sink.started.wait(), timeout=2)
        await _cancel_run(harness, run)
        _stall_event_loop_past_test_lease()
        sink.release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result.outcome == "cancelled"
        assert gateway.calls == 0
        assert executor.calls == 0

    asyncio.run(scenario())


def test_caller_cancellation_settles_admitted_effect_before_releasing_lease(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    gateway = _BlockingGateway()
    state = RunWorkflowStateStore(harness.runtime, lease_seconds=0.15)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=gateway,
        executor=_Executor(),
        adapter=_TwoEffectAdapter("model"),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id="dispatch-caller-cancel")
        task = asyncio.create_task(service.execute(request))
        await asyncio.wait_for(gateway.started.wait(), timeout=2)
        task.cancel()
        _stall_event_loop_past_test_lease()
        await asyncio.sleep(0.08)
        assert not task.done()
        with pytest.raises(RuntimeError, match="active worker"):
            state.claim(
                dispatch_id=request.dispatch_id,
                run_id=request.run_id,
                owner_principal_id=request.owner_principal_id,
                config_hash=request.config_hash,
            )
        gateway.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        recovered = state.claim(
            dispatch_id=request.dispatch_id,
            run_id=request.run_id,
            owner_principal_id=request.owner_principal_id,
            config_hash=request.config_hash,
        )
        assert recovered.recovered is True
        assert gateway.calls == 1
        state.release(recovered)

    asyncio.run(scenario())


def test_terminal_audit_outage_leaves_retryable_outbox_then_publishes_once(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    sink = _TerminalFailOnceSink(harness.sink)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime),
        run_store=harness.runs,
        sink=sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_OutcomeAdapter("context_exhausted"),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id="dispatch-terminal-audit-retry")
        with pytest.raises(RuntimeAuditError, match="effect remains blocked"):
            await service.execute(request)
        terminal = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert terminal is not None and terminal.status is RunState.CONTEXT_EXHAUSTED
        with harness.runtime.connect() as conn:
            pending = conn.execute(sa.select(ne_agent_run_lifecycle_outbox)).one()
            assert pending.claimed_at is None
            assert pending.published_at is None
            assert pending.audit_event_id is None

        assert (await service.execute(request)).outcome == "context_exhausted"
        with harness.runtime.connect() as conn:
            published = conn.execute(sa.select(ne_agent_run_lifecycle_outbox)).one()
            assert published.published_at is not None
            assert published.audit_event_id is not None
            assert (
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM ne_agent_run_audit_receipts "
                        "WHERE run_id = :run_id AND decision_kind = 'terminal'"
                    ),
                    {"run_id": run.run_id},
                ).scalar_one()
                == 1
            )
        events = await harness.runs.list_events(
            run_id=run.run_id, owner_principal_id="alice", limit=100
        )
        assert sum(item.event_kind == "run.terminal" for item in events.events) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("effect", ["model", "tool"])
def test_cancel_during_inflight_effect_settles_once_and_schedules_no_next_effect(
    tmp_path: Path,
    effect: str,
) -> None:
    harness = _harness(tmp_path)
    gateway = _BlockingGateway() if effect == "model" else _Gateway()
    executor = _BlockingExecutor() if effect == "tool" else _Executor()
    state = RunWorkflowStateStore(harness.runtime, lease_seconds=0.15)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=gateway,
        executor=executor,
        adapter=_TwoEffectAdapter(effect),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id=f"dispatch-{effect}")
        task = asyncio.create_task(service.execute(request))
        blocking: Any = gateway if effect == "model" else executor
        await asyncio.wait_for(blocking.started.wait(), timeout=2)
        await _cancel_run(harness, run)
        # Starve the event loop beyond the lease, then prove the unchanged
        # exact holder can renew before the admitted effect settles.
        _stall_event_loop_past_test_lease()
        blocking.release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result.outcome == "cancelled"
        assert blocking.calls == 1
        terminal = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert terminal is not None
        assert terminal.status is RunState.CANCELLED
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_lifecycle_outbox)
                ).scalar_one()
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("context_exhausted", RunState.CONTEXT_EXHAUSTED),
        ("budget_exhausted", RunState.BUDGET_EXHAUSTED),
    ],
)
def test_exhaustion_is_a_truthful_terminal_state(
    tmp_path: Path,
    outcome: str,
    expected_status: RunState,
) -> None:
    harness = _harness(tmp_path)
    service = RunWorkflowService(
        state=RunWorkflowStateStore(harness.runtime),
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_OutcomeAdapter(outcome),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id=f"dispatch-{outcome}")
        assert (await service.execute(request)).outcome == outcome
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None
        assert current.status is expected_status
        assert current.output_verdict == "unavailable"
        assert (await service.execute(request)).outcome == outcome

    asyncio.run(scenario())


_TOKEN_CEILING_REASON = "stopped: the turn's 96,650-token budget was exhausted"


@pytest.mark.parametrize(
    ("reason", "expected_detail"),
    [
        # A run that died on a ceiling states WHICH ceiling. `budget_exhausted` is
        # one word for five different ceilings, so the status word alone leaves the
        # reader with nothing to act on -- which is exactly what the live demo run
        # showed: a terminal, and a blank where the account belongs.
        (_TOKEN_CEILING_REASON, {"error_reason": _TOKEN_CEILING_REASON}),
        # THE CONTROL. Where the producer named no ceiling, NOTHING is published:
        # no checkpoint, no invented sentence, and the reader gets the status word.
        # Without this the pin above would pass against a seam that manufactures a
        # plausible cause for every exhausted run.
        (None, None),
    ],
)
def test_a_budget_terminal_publishes_only_the_ceiling_its_producer_named(
    tmp_path: Path,
    reason: str | None,
    expected_detail: dict[str, str] | None,
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_OutcomeAdapter("budget_exhausted", reason),
        config_hash=harness.config_hash,
    )

    async def scenario() -> None:
        run = await _queued_run(harness)
        request = _workflow_request(harness, run, dispatch_id=f"dispatch-reason-{reason}")
        result = await service.execute(request)
        assert result.outcome == "budget_exhausted"
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None
        assert current.status is RunState.BUDGET_EXHAUSTED
        checkpoint = state.get_checkpoint(
            run_id=run.run_id,
            attempt_id=result.attempt_id,
            stage="execution_failed",
        )
        if expected_detail is None:
            assert checkpoint is None
        else:
            assert checkpoint is not None
            assert dict(checkpoint.safe_detail) == expected_detail

    asyncio.run(scenario())


def test_refusal_is_completed_zero_claim_disposition_and_terminal_intent_is_exact(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = state.claim(
            dispatch_id="dispatch-refused",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        publication = state.terminalize(
            claim,
            status=RunState.COMPLETED,
            output_verdict=RunOutputVerdict.REFUSED,
            outcome="refused",
        )
        assert (
            state.terminalize(
                claim,
                status=RunState.COMPLETED,
                output_verdict=RunOutputVerdict.REFUSED,
                outcome="refused",
            )
            == publication
        )
        with pytest.raises(ValueError, match="outcome does not match"):
            state.terminalize(
                claim,
                status=RunState.COMPLETED,
                output_verdict=RunOutputVerdict.REFUSED,
                outcome="different",
            )
        state.release(claim)
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None
        assert current.status is RunState.COMPLETED
        assert current.output_verdict == "refused"
        assert "REFUSED" not in RunState.__members__
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_claims)
                    .where(ne_agent_run_claims.c.run_id == run.run_id)
                ).scalar_one()
                == 0
            )

    asyncio.run(scenario())


def test_terminal_event_and_audit_outbox_rollback_together(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = state.claim(
            dispatch_id="dispatch-terminal-rollback",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )

        def fail_outbox(
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
                raise RuntimeError("injected lifecycle outbox failure")

        event.listen(harness.runtime, "before_execute", fail_outbox)
        try:
            with pytest.raises(RuntimeError, match="injected lifecycle outbox failure"):
                state.terminalize(claim, status=RunState.FAILED, outcome="failed")
        finally:
            event.remove(harness.runtime, "before_execute", fail_outbox)
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None and current.status is RunState.RUNNING
        with harness.runtime.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_events)
                    .where(
                        ne_agent_run_events.c.run_id == run.run_id,
                        ne_agent_run_events.c.event_kind == "run.terminal",
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count())
                    .select_from(ne_agent_run_lifecycle_outbox)
                    .where(ne_agent_run_lifecycle_outbox.c.run_id == run.run_id)
                ).scalar_one()
                == 0
            )
        state.release(claim)

    asyncio.run(scenario())


def test_terminal_outbox_rejects_fabricated_publication_and_missing_receipt(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = state.claim(
            dispatch_id="dispatch-terminal-guard",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        event_id = str(uuid.uuid4())
        detail = "{}"
        now = datetime.now(UTC)
        with (
            pytest.raises(IntegrityError, match="outbox insert is not canonical"),
            harness.runtime.begin() as conn,
        ):
            sequence = (
                int(
                    conn.execute(
                        sa.select(sa.func.max(ne_agent_run_events.c.sequence)).where(
                            ne_agent_run_events.c.run_id == run.run_id
                        )
                    ).scalar_one()
                )
                + 1
            )
            conn.execute(
                ne_agent_run_events.insert().values(
                    event_id=event_id,
                    run_id=run.run_id,
                    attempt_id=claim.attempt_id,
                    sequence=sequence,
                    event_kind="run.terminal",
                    event_status=RunState.FAILED.value,
                    parent_event_id=None,
                    step_id="forged",
                    safe_detail=detail,
                    detail_hash=hashlib.sha256(detail.encode()).hexdigest(),
                    created_at=now,
                )
            )
            conn.execute(
                ne_agent_run_lifecycle_outbox.insert().values(
                    publication_id=str(uuid.uuid4()),
                    run_id=run.run_id,
                    attempt_id=claim.attempt_id,
                    event_id=event_id,
                    terminal_status=RunState.FAILED.value,
                    output_verdict=RunOutputVerdict.UNAVAILABLE.value,
                    outcome="failed",
                    terminal_intent_hash="f" * 64,
                    created_at=now,
                    claimed_at=now,
                    published_at=now,
                    audit_event_id="fabricated-audit-event",
                )
            )
        publication_id = state.terminalize(
            claim,
            status=RunState.FAILED,
            outcome="failed",
        )
        publication = state.claim_terminal_publication(publication_id)
        with pytest.raises(RuntimeError, match="lacks its exact audit receipt"):
            state.complete_terminal_publication(
                publication,
                audit_event_id="fabricated-audit-event",
                config_hash=harness.config_hash,
            )
        state.release_terminal_publication(publication)
        state.release(claim)

    asyncio.run(scenario())


def test_expiry_and_cancel_race_preserves_the_first_primary_transition(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)

    async def scenario() -> None:
        expiry_first = await _queued_run(harness)
        expiry_claim = state.claim(
            dispatch_id="dispatch-expiry-first",
            run_id=expiry_first.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        state.terminalize(
            expiry_claim,
            status=RunState.EXPIRED,
            outcome="expired",
        )
        state.release(expiry_claim)
        with pytest.raises(RunSubmissionConflictError, match="cannot be cancelled"):
            await _cancel_run(harness, expiry_first)

        cancel_first = await _queued_run(harness)
        cancel_claim = state.claim(
            dispatch_id="dispatch-cancel-first",
            run_id=cancel_first.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        await _cancel_run(harness, cancel_first)
        with pytest.raises(RuntimeError, match="cancelling->expired"):
            state.terminalize(
                cancel_claim,
                status=RunState.EXPIRED,
                outcome="expired",
            )
        state.terminalize(
            cancel_claim,
            status=RunState.CANCELLED,
            outcome="cancelled",
        )
        state.release(cancel_claim)

        first = await harness.runs.get_run(run_id=expiry_first.run_id, owner_principal_id="alice")
        second = await harness.runs.get_run(run_id=cancel_first.run_id, owner_principal_id="alice")
        assert first is not None and first.status is RunState.EXPIRED
        assert second is not None and second.status is RunState.CANCELLED
        with harness.runtime.connect() as conn:
            rows = conn.execute(
                sa.select(ne_agent_run_lifecycle_outbox.c.terminal_status).order_by(
                    ne_agent_run_lifecycle_outbox.c.terminal_status
                )
            ).scalars()
            assert sorted(rows) == ["cancelled", "expired"]

    asyncio.run(scenario())


def test_exact_target_provenance_is_hash_covered_and_caller_collision_is_rejected(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = RunWorkflowStateStore(harness.runtime).claim(
            dispatch_id="dispatch-a",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        sink = RunAuditReceiptSink(
            harness.sink,
            audit_engine=harness.audit,
            runtime_engine=harness.runtime,
            run_store=harness.runs,
            target=RunAuditTarget(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                step_id="step-a",
                tool_call_id="tool-call-a",
            ),
            config_hash=harness.config_hash,
        )
        event = new_audit_event(
            action="run.tool.pre_effect",
            resource="tool:read.knowledge",
            outcome=AuditOutcome.success,
            actor="alice",
            config_hash=harness.config_hash,
        )
        await sink.append(event)
        # The wrapper replaces the caller's random event id with its durable,
        # target-derived id; select the sole row and prove the stamps are payload.
        with harness.audit.connect() as conn:
            event_id = str(conn.execute(sa.text("SELECT event_id FROM ne_audit_log")).scalar_one())
        record = records_by_event_ids(harness.audit, [event_id])[0]
        provenance = record.payload["provenance"]
        assert provenance["run_id"] == run.run_id
        assert provenance["attempt_id"] == claim.attempt_id
        assert provenance["step_id"] == "step-a"
        assert provenance["tool_call_id"] == "tool-call-a"
        with harness.runtime.connect() as conn:
            receipt_id = str(
                conn.execute(
                    sa.text("SELECT run_audit_receipt_id FROM ne_agent_run_audit_receipts")
                ).scalar_one()
            )
        receipt = await harness.runs.get_audit_receipt(
            run_id=run.run_id,
            run_audit_receipt_id=receipt_id,
            owner_principal_id="alice",
        )
        assert receipt is not None
        assert receipt.audit_record_hash == record.record_hash
        assert receipt.run_event_id == provenance["run_event_id"]

        collision = new_audit_event(
            action="run.tool.pre_effect",
            resource="tool:read.knowledge",
            outcome=AuditOutcome.success,
            actor="alice",
            provenance={"run_id": "caller-chosen"},
            config_hash=harness.config_hash,
        )
        with pytest.raises(RuntimeAuditError, match="server-owned target"):
            await sink.append(collision)

    asyncio.run(scenario())


def test_receipt_gated_gateway_preserves_air_gap_fail_closed_error(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    class _AirGapGateway(_Gateway):
        async def complete(self, request: CompletionRequest) -> Completion:
            self.calls += 1
            raise AirGapViolationError(f"AIR_GAP blocked {request.task}")

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = RunWorkflowStateStore(harness.runtime).claim(
            dispatch_id="dispatch-a",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        sink = RunAuditReceiptSink(
            harness.sink,
            audit_engine=harness.audit,
            runtime_engine=harness.runtime,
            run_store=harness.runs,
            target=RunAuditTarget(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                step_id="model-a",
            ),
            config_hash=harness.config_hash,
        )
        raw = _AirGapGateway()
        gated = ReceiptGatedGateway(
            raw,
            audit=sink,
            owner_principal_id="alice",
            step_id="model-a",
        )
        with pytest.raises(AirGapViolationError, match="AIR_GAP blocked agent-run"):
            await gated.complete(
                CompletionRequest(
                    messages=(ChatMessage(role="user", content="protected"),),
                    task="agent-run",
                    principal_id="alice",
                )
            )
        assert raw.calls == 1

    asyncio.run(scenario())


def test_missing_pre_effect_receipt_blocks_gateway_before_air_gap_capability(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    class _ReceiptFailure:
        async def record_audit_receipt(self, **_kwargs: Any) -> str:
            raise OSError("runtime DB unavailable")

    async def scenario() -> None:
        run = await _queued_run(harness)
        state = RunWorkflowStateStore(harness.runtime)
        claim = state.claim(
            dispatch_id="dispatch-a",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        sink = RunAuditReceiptSink(
            harness.sink,
            audit_engine=harness.audit,
            runtime_engine=harness.runtime,
            run_store=_ReceiptFailure(),  # type: ignore[arg-type]
            target=RunAuditTarget(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                step_id="model-a",
            ),
            config_hash=harness.config_hash,
        )
        gateway = _Gateway()
        gated = ReceiptGatedGateway(
            gateway,
            audit=sink,
            owner_principal_id="alice",
            step_id="model-a",
        )
        with pytest.raises(RuntimeAuditError, match="effect remains blocked"):
            await gated.complete(
                CompletionRequest(
                    messages=(ChatMessage(role="user", content="protected"),),
                    task="agent-run",
                    principal_id="alice",
                )
            )
        # This is the AIR_GAP/provider capability boundary: the raw composed
        # gateway is never reached when its mandatory receipt is unavailable.
        assert gateway.calls == 0

    asyncio.run(scenario())


def test_receipt_gated_tool_executor_is_the_only_path_to_the_raw_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path)
    protected_result = "ne-source-ref:v1:server-minted-result"

    @dataclass(frozen=True)
    class _SourceRef:
        reference_id: str

    monkeypatch.setattr(run_runner_module, "SourceBoundToolResultRef", _SourceRef)
    monkeypatch.setattr(
        run_runner_module,
        "decode_protected_reference",
        lambda value: _SourceRef("protected-result-a") if value == protected_result else None,
    )
    monkeypatch.setattr(
        run_runner_module,
        "encode_protected_reference",
        lambda _reference: protected_result,
    )

    class _RawExecutor:
        def __init__(self) -> None:
            self.calls = 0
            self.contexts: list[Any] = []

        @property
        def tool_specs(self) -> tuple[Any, ...]:
            return ()

        async def execute(self, tool: str, args: Any, *, principal: Principal) -> str:
            del args
            self.calls += 1
            context = current_run_tool_call()
            assert context is not None
            self.contexts.append(context)
            if tool == "write.external.fail":
                raise ValueError("injected handler failure")
            if tool == "read.knowledge.search":
                return protected_result
            return f"{tool}:{principal.id}"

    async def scenario() -> None:
        run = await _queued_run(harness)
        claim = RunWorkflowStateStore(harness.runtime).claim(
            dispatch_id="dispatch-tool",
            run_id=run.run_id,
            owner_principal_id="alice",
            config_hash=harness.config_hash,
        )
        sink = RunAuditReceiptSink(
            harness.sink,
            audit_engine=harness.audit,
            runtime_engine=harness.runtime,
            run_store=harness.runs,
            target=RunAuditTarget(
                run_id=run.run_id,
                attempt_id=claim.attempt_id,
                step_id="run-tool",
            ),
            config_hash=harness.config_hash,
        )
        raw = _RawExecutor()
        request = _workflow_request(harness, run, dispatch_id="dispatch-tool")
        gated = ReceiptGatedToolExecutor(
            raw,
            audit=sink,
            run=run,
            target=request.target,
            owner_principal_id="alice",
            step_id="run-tool",
        )
        result = await gated.execute("read.knowledge.search", {}, principal=Principal(id="alice"))
        assert result == protected_result
        assert raw.calls == 1
        assert raw.contexts[0].run == run
        assert raw.contexts[0].receipt.attempt_id == claim.attempt_id
        assert raw.contexts[0].receipt.tool_call_id == "run-tool:tool:0"
        assert current_run_tool_call() is None
        with harness.runtime.connect() as conn:
            receipt = conn.execute(
                sa.text(
                    "SELECT step_id, tool_call_id FROM ne_agent_run_audit_receipts "
                    "WHERE run_id = :run_id"
                ),
                {"run_id": run.run_id},
            ).one()
        assert receipt.step_id == "run-tool"
        assert receipt.tool_call_id == "run-tool:tool:0"

        with pytest.raises(PermissionError, match="effective identity"):
            await gated.execute("read.knowledge.search", {}, principal=Principal(id="mallory"))
        assert raw.calls == 1
        with pytest.raises(ValueError, match="injected handler failure"):
            await gated.execute("write.external.fail", {}, principal=Principal(id="alice"))
        assert current_run_tool_call() is None
        assert raw.calls == 2
        assert raw.contexts[1].served_protected_refs == (protected_result,)

        original_append_receipt = RunAuditReceiptSink.append_receipt

        async def mismatched_receipt(
            receipt_sink: RunAuditReceiptSink, event: AuditEventLike
        ) -> Any:
            receipt = await original_append_receipt(receipt_sink, event)
            return replace(receipt, attempt_id="attempt-foreign")

        monkeypatch.setattr(RunAuditReceiptSink, "append_receipt", mismatched_receipt)
        mismatched = ReceiptGatedToolExecutor(
            raw,
            audit=sink,
            run=run,
            target=request.target,
            owner_principal_id="alice",
            step_id="run-tool-mismatch",
        )
        with pytest.raises(RuntimeError, match="differs from its canonical execution target"):
            await mismatched.execute("read.knowledge.search", {}, principal=Principal(id="alice"))
        assert raw.calls == 2

    asyncio.run(scenario())


def test_concurrent_same_trace_runs_keep_exact_receipts_separate(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    async def scenario() -> None:
        runs = [await _queued_run(harness), await _queued_run(harness)]
        sinks: list[RunAuditReceiptSink] = []
        for index, run in enumerate(runs):
            claim = RunWorkflowStateStore(harness.runtime).claim(
                dispatch_id=f"dispatch-{index}",
                run_id=run.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
            sinks.append(
                RunAuditReceiptSink(
                    harness.sink,
                    audit_engine=harness.audit,
                    runtime_engine=harness.runtime,
                    run_store=harness.runs,
                    target=RunAuditTarget(
                        run_id=run.run_id,
                        attempt_id=claim.attempt_id,
                        step_id="same-step",
                        tool_call_id="same-tool-call",
                    ),
                    config_hash=harness.config_hash,
                )
            )

        async def append(sink: RunAuditReceiptSink) -> None:
            await sink.append(
                new_audit_event(
                    action="run.tool.pre_effect",
                    resource="tool:read.knowledge.search",
                    outcome=AuditOutcome.success,
                    actor="alice",
                    trace_id="same-trace",
                    config_hash=harness.config_hash,
                )
            )

        await asyncio.gather(*(append(sink) for sink in sinks))
        with harness.runtime.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT run_id, attempt_id, run_event_id, audit_event_id, "
                    "audit_record_hash FROM ne_agent_run_audit_receipts "
                    "WHERE run_id IN (:run_a, :run_b)"
                ),
                {"run_a": runs[0].run_id, "run_b": runs[1].run_id},
            ).all()
        assert {row.run_id for row in rows} == {run.run_id for run in runs}
        assert len({row.attempt_id for row in rows}) == 2
        assert len({row.run_event_id for row in rows}) == 2
        assert len({row.audit_event_id for row in rows}) == 2
        assert len({row.audit_record_hash for row in rows}) == 2

    asyncio.run(scenario())


def test_postgres_receipt_and_expired_lease_takeover() -> None:
    url = os.environ.get("NE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("NE_TEST_POSTGRES_URL is required for the PRE-010B PostgreSQL proof")

    admin = sa.create_engine(url)
    runtime_schema = f"pre010b_runtime_{uuid.uuid4().hex}"
    audit_schema = f"pre010b_audit_{uuid.uuid4().hex}"
    with admin.begin() as conn:
        conn.execute(sa.schema.CreateSchema(runtime_schema))
        conn.execute(sa.schema.CreateSchema(audit_schema))
    runtime = sa.create_engine(url, connect_args={"options": f"-csearch_path={runtime_schema}"})
    audit = sa.create_engine(url, connect_args={"options": f"-csearch_path={audit_schema}"})
    try:
        harness = _engine_harness(runtime, audit)

        async def scenario() -> None:
            run = await _queued_run(harness)
            state = RunWorkflowStateStore(runtime)
            first = state.claim(
                dispatch_id="postgres-dispatch",
                run_id=run.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
            intent_detail = {
                "ordinal": 0,
                "tool": "read.knowledge.search",
                "step_intent": "Find current governed evidence.",
            }
            first_boundary = _LeaseExecutionBoundary(state, first)
            await first_boundary.checkpoint(
                stage="agentic-intent:0",
                safe_detail=intent_detail,
            )
            intent_checkpoint = state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=first.attempt_id,
                stage="agentic-intent:0",
            )
            assert intent_checkpoint is not None
            await first_boundary.checkpoint(
                stage="agentic-intent:0",
                safe_detail=intent_detail,
            )
            replayed = state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=first.attempt_id,
                stage="agentic-intent:0",
            )
            assert replayed is not None
            assert replayed.checkpoint_id == intent_checkpoint.checkpoint_id
            with pytest.raises(ValueError, match="different intent"):
                await first_boundary.checkpoint(
                    stage="agentic-intent:0",
                    safe_detail={**intent_detail, "tool": "write.internal.note"},
                )
            sink = RunAuditReceiptSink(
                harness.sink,
                audit_engine=audit,
                runtime_engine=runtime,
                run_store=harness.runs,
                target=RunAuditTarget(
                    run_id=run.run_id,
                    attempt_id=first.attempt_id,
                    step_id="postgres-step",
                ),
                config_hash=harness.config_hash,
            )
            await sink.append(
                new_audit_event(
                    action="run.execution.pre_effect",
                    resource=f"run:{run.run_id}",
                    outcome=AuditOutcome.success,
                    actor="alice",
                    config_hash=harness.config_hash,
                )
            )
            with runtime.begin() as conn:
                conn.execute(
                    sa.text(
                        "UPDATE ne_agent_run_workflow_leases SET expires_at = :expired "
                        "WHERE run_id = :run_id"
                    ),
                    {"expired": datetime(2000, 1, 1, tzinfo=UTC), "run_id": run.run_id},
                )
            recovered = state.claim(
                dispatch_id="postgres-dispatch",
                run_id=run.run_id,
                owner_principal_id="alice",
                config_hash=harness.config_hash,
            )
            assert recovered.attempt_id == first.attempt_id
            assert recovered.lease_epoch == first.lease_epoch + 1
            with pytest.raises(RunLeaseLost, match="safe checkpoint"):
                await first_boundary.checkpoint(
                    stage="agentic-intent:1",
                    safe_detail={
                        "ordinal": 1,
                        "tool": "write.internal.note",
                        "step_intent": "Park the governed result.",
                    },
                )
            for stale_operation in (
                lambda: state.probe(first),
                lambda: state.renew(first),
                lambda: state.terminalize(
                    first,
                    status=RunState.FAILED,
                    outcome="failed",
                ),
            ):
                with pytest.raises(RuntimeError, match=r"stale|lost its lease"):
                    stale_operation()
            recovered_boundary = _LeaseExecutionBoundary(state, recovered)
            await recovered_boundary.checkpoint(
                stage="agentic-intent:0",
                safe_detail=intent_detail,
            )
            takeover_replay = state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=recovered.attempt_id,
                stage="agentic-intent:0",
            )
            assert takeover_replay is not None
            assert takeover_replay.checkpoint_id == intent_checkpoint.checkpoint_id
            with pytest.raises(ValueError, match="different intent"):
                await recovered_boundary.checkpoint(
                    stage="agentic-intent:0",
                    safe_detail={**intent_detail, "step_intent": "Changed after takeover."},
                )
            await recovered_boundary.checkpoint(
                stage="agentic-intent:1",
                safe_detail={
                    "ordinal": 1,
                    "tool": "write.internal.note",
                    "step_intent": "Park the governed result.",
                },
            )
            second_checkpoint = state.get_checkpoint(
                run_id=run.run_id,
                attempt_id=recovered.attempt_id,
                stage="agentic-intent:1",
            )
            assert second_checkpoint is not None
            assert second_checkpoint.checkpoint_id != intent_checkpoint.checkpoint_id
            with runtime.connect() as conn:
                rows = list(
                    conn.execute(
                        sa.select(
                            ne_agent_run_execution_checkpoints.c.stage,
                            ne_agent_run_execution_checkpoints.c.safe_detail,
                        )
                        .where(
                            ne_agent_run_execution_checkpoints.c.run_id == run.run_id,
                            ne_agent_run_execution_checkpoints.c.attempt_id == first.attempt_id,
                            ne_agent_run_execution_checkpoints.c.stage.like("agentic-intent:%"),
                        )
                        .order_by(ne_agent_run_execution_checkpoints.c.stage)
                    ).all()
                )
            assert [(row.stage, json.loads(row.safe_detail)) for row in rows] == [
                ("agentic-intent:0", intent_detail),
                (
                    "agentic-intent:1",
                    {
                        "ordinal": 1,
                        "step_intent": "Park the governed result.",
                        "tool": "write.internal.note",
                    },
                ),
            ]
            publication_id = state.terminalize(
                recovered,
                status=RunState.FAILED,
                outcome="failed",
            )
            service = RunWorkflowService(
                state=state,
                run_store=harness.runs,
                sink=harness.sink,
                audit_engine=audit,
                gateway=_Gateway(),
                executor=_Executor(),
                adapter=_MustNotRunAdapter(),
                config_hash=harness.config_hash,
            )
            await service.publish_terminal(state.get_terminal_publication(publication_id))
            with runtime.connect() as conn:
                assert (
                    conn.execute(
                        sa.text(
                            "SELECT count(*) FROM ne_agent_run_audit_receipts "
                            "WHERE run_id = :run_id AND attempt_id = :attempt_id"
                        ),
                        {"run_id": run.run_id, "attempt_id": first.attempt_id},
                    ).scalar_one()
                    == 2
                )
                terminal_event_id = conn.execute(
                    sa.text(
                        "SELECT event_id FROM ne_agent_run_lifecycle_outbox "
                        "WHERE run_id = :run_id AND published_at IS NOT NULL"
                    ),
                    {"run_id": run.run_id},
                ).scalar_one()
                assert (
                    conn.execute(
                        sa.text(
                            "SELECT count(*) FROM ne_agent_run_events "
                            "WHERE event_id = :event_id AND run_id = :run_id "
                            "AND event_kind = 'run.terminal' AND event_status = 'failed'"
                        ),
                        {"event_id": terminal_event_id, "run_id": run.run_id},
                    ).scalar_one()
                    == 1
                )
                terminal_audit_event_id = conn.execute(
                    sa.text(
                        "SELECT audit_event_id FROM ne_agent_run_lifecycle_outbox "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": run.run_id},
                ).scalar_one()
            with (
                pytest.raises(DBAPIError, match="append-only"),
                runtime.begin() as conn,
            ):
                conn.execute(
                    sa.text(
                        "DELETE FROM ne_agent_run_audit_receipts "
                        "WHERE audit_event_id = :audit_event_id"
                    ),
                    {"audit_event_id": terminal_audit_event_id},
                )

        asyncio.run(scenario())
    finally:
        runtime.dispose()
        audit.dispose()
        with admin.begin() as conn:
            conn.execute(sa.schema.DropSchema(runtime_schema, cascade=True))
            conn.execute(sa.schema.DropSchema(audit_schema, cascade=True))
        admin.dispose()


# --- NE-OUTPUT-CONTRACT: a failed Run states its cause ----------------------


def _execute_failing(tmp_path: Path, error: Exception) -> dict[str, object]:
    harness = _harness(tmp_path)
    state = RunWorkflowStateStore(harness.runtime)
    service = RunWorkflowService(
        state=state,
        run_store=harness.runs,
        sink=harness.sink,
        audit_engine=harness.audit,
        gateway=_Gateway(),
        executor=_Executor(),
        adapter=_RaisingAdapter(error),
        config_hash=harness.config_hash,
    )
    captured: dict[str, object] = {}

    async def scenario() -> None:
        run = await _queued_run(harness)
        with pytest.raises(type(error)):
            await service.execute(
                _workflow_request(harness, run, dispatch_id="dispatch-output-contract")
            )
        current = await harness.runs.get_run(run_id=run.run_id, owner_principal_id="alice")
        assert current is not None and current.status is RunState.FAILED
        with harness.runtime.connect() as conn:
            row = conn.execute(
                sa.select(ne_agent_run_execution_checkpoints.c.safe_detail).where(
                    ne_agent_run_execution_checkpoints.c.run_id == run.run_id,
                    ne_agent_run_execution_checkpoints.c.stage == "execution_failed",
                )
            ).one()
        captured.update(json.loads(str(row.safe_detail)))

    asyncio.run(scenario())
    return captured


def test_an_unvalidatable_final_reply_publishes_its_reason_not_just_a_class(
    tmp_path: Path,
) -> None:
    """The run that used to die with `failure_reason: null` now says why.

    `run_model_planned_agent` raises `AgentOutputContractError` when pydantic-ai
    exhausts its output-retry budget; this pins the rest of the wire —
    `run_workflow` lifting `safe_reason` onto the `execution_failed` checkpoint as
    `error_reason`, which is what `RunProjection._failure_reason` serves and
    `TurnFailureReadout` renders.
    """
    reason = "The model's final reply did not match this Run's required answer format."

    detail = _execute_failing(tmp_path, AgentOutputContractError(reason))

    assert detail["error_class"] == "AgentOutputContractError"
    assert detail["error_reason"] == reason


def test_a_failure_that_marked_no_reason_names_its_class_and_invents_no_prose(
    tmp_path: Path,
) -> None:
    """The other half, DELIBERATELY CHANGED: silence was not the honest answer.

    This control previously pinned `"error_reason" not in detail` - no
    `safe_reason` at the raise site meant nothing was published at all. That is
    what shipped a FAILED run to the console as an alert reading only "the run
    ended before it produced an answer", permanently, with nothing for the
    reader to report. Not manufacturing prose was right; publishing NOTHING was
    not, because `error_class` is an OpaqueId the reader is never shown.

    So the floor names the CLASS and stops. It is not an explanation and does
    not pretend to be one - it says outright that no explanation was published.
    The half of this control that must never weaken is unchanged and widened:
    the exception's own message is a value, and no part of it may cross.
    """
    leaked = "internal detail nobody may read: the DR-4830 uplift is 18.5% of the 2026 base"
    detail = _execute_failing(tmp_path, RuntimeError(leaked))

    assert detail["error_class"] == "RuntimeError"
    assert detail["error_reason"] == derived_failure_reason("RuntimeError")
    serialized = json.dumps(detail)
    assert leaked not in serialized
    for fragment in ("internal detail nobody may read", "DR-4830", "18.5", "2026 base"):
        assert fragment not in serialized

    # STILL BOUNDED, STILL SERVABLE - the projection drops a reason past the cap
    # or carrying a non-printable, which would be `failure_reason: null` again.
    reason = detail["error_reason"]
    assert isinstance(reason, str)
    assert reason.strip() and len(reason) <= 256
    assert all(character.isprintable() for character in reason)
