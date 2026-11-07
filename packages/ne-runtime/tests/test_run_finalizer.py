# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy import event
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
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_finalizer import (
    _MAX_SAFE_REASON_CHARS,
    RunClaimEnvelopeDeclaration,
    RunEvidenceBinding,
    RunFinalizer,
    RunFinalizerLease,
    RunFinalizerObservations,
    RunTerminalCandidate,
    RunTurnTarget,
    RunVerifierBinding,
    StructuredRunOutputEnvelope,
    claim_set_hash,
    delivery_set_hash,
    derived_failure_reason,
    published_failure_reason,
    safe_failure_class,
    served_evidence_binding_hash,
)
from ne.runtime.run_store import (
    AgentRunStore,
    EvidenceDeclaration,
    EvidenceProjectionUnavailable,
    RunAuditDecisionKind,
    RunState,
    ensure_run_schema,
    ne_agent_delivery_receipts,
    ne_agent_evidence_locators,
    ne_agent_protected_source_outputs,
    ne_agent_protected_turn_contents,
    ne_agent_run_claim_evidence,
    ne_agent_run_claims,
    ne_agent_run_events,
    ne_agent_run_finalizations,
    ne_agent_run_history_attachments,
    ne_agent_run_lifecycle_outbox,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_workflow import (
    RunWorkflowClaim,
    RunWorkflowStateStore,
    ensure_run_workflow_schema,
)
from ne.runtime.source_results import (
    RunStoreSourceBoundResultResolver,
    authenticated_request_scope,
    complete_run_delivery_observation,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema


@dataclass(frozen=True)
class _ReadyRun:
    engine: sa.Engine
    runs: AgentRunStore
    state: RunWorkflowStateStore
    target: RunTurnTarget
    claim: RunWorkflowClaim
    config_hash: str


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


async def _ready_run(tmp_path: Path, tag: str, *, engine: sa.Engine | None = None) -> _ReadyRun:
    engine = engine or sa.create_engine(f"sqlite:///{tmp_path}/{tag}.db")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    config_hash = "1" * 64
    conversation = await sessions.create_conversation("assistant", "claims", "alice")
    accepted = await runs.admit_start(
        SimpleNamespace(
            idempotency_key=f"start-{tag}",
            request_hash=hashlib.sha256(tag.encode()).hexdigest(),
            conversation_id=conversation.conversation_id,
            expected_version=0,
            owner_principal_id="alice",
            department="claims",
            conversation_agent_name="assistant",
            agent_id="agt_01",
            agent_version=7,
            strategy="agentic",
            config_hash=config_hash,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-receipt-1",
            input_plaintext=b"private input sentinel",
            authenticated_context=AuthenticatedRequestContext(
                principal_id="alice",
                authorization_groups=(),
                department="claims",
                issuer="https://issuer.example.test",
                expires_at=4_102_444_800,
            ),
        )
    )
    with engine.connect() as conn:
        authored = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(
                ne_agent_run_turn_targets.c.run_id == accepted.run_id
            )
        ).one()
    target = RunTurnTarget(
        conversation_id=str(authored.conversation_id),
        run_id=str(authored.run_id),
        turn_id=str(authored.turn_id),
        turn_sequence=int(authored.turn_sequence),
        attachment_id=str(authored.attachment_id),
        attempt_id=str(authored.attempt_id),
    )
    state = RunWorkflowStateStore(engine)
    claim = state.claim(
        dispatch_id=f"dispatch-{tag}",
        run_id=accepted.run_id,
        owner_principal_id="alice",
        config_hash=config_hash,
    )
    await _audit(
        runs,
        run_id=target.run_id,
        attempt_id=target.attempt_id,
        event_id=claim.claim_event_id,
        config_hash=config_hash,
        decision=RunAuditDecisionKind.STATE_TRANSITION,
        step_id="run-execution",
        ordinal=1,
    )
    return _ReadyRun(engine, runs, state, target, claim, config_hash)


async def _audit(
    runs: AgentRunStore,
    *,
    run_id: str,
    attempt_id: str,
    event_id: str,
    config_hash: str,
    decision: RunAuditDecisionKind,
    step_id: str,
    ordinal: int,
) -> None:
    await runs.record_audit_receipt(
        run_id=run_id,
        attempt_id=attempt_id,
        run_event_id=event_id,
        decision_kind=decision,
        audit_event_id=f"audit-{ordinal}-{uuid.uuid4()}",
        audit_sequence=ordinal,
        audit_record_hash=hashlib.sha256(f"audit-{ordinal}".encode()).hexdigest(),
        config_hash=config_hash,
        owner_principal_id="alice",
        step_id=step_id,
    )


def _lease(ready: _ReadyRun) -> RunFinalizerLease:
    return RunFinalizerLease(
        dispatch_id=ready.claim.dispatch_id,
        run_id=ready.claim.run_id,
        attempt_id=ready.claim.attempt_id,
        lease_token=ready.claim.lease_token,
        lease_epoch=ready.claim.lease_epoch,
    )


def _empty_delivery(target: RunTurnTarget):  # type: ignore[no-untyped-def]
    with (
        run_delivery_observation_scope(
            run_id=target.run_id, attempt_id=target.attempt_id
        ) as observer,
        model_call_scope(run_id=target.run_id, model_call_attempt_id=target.attempt_id),
    ):
        complete_run_delivery_observation()
        return observer.snapshot()


def _envelope(output: bytes, claims: tuple[RunClaimEnvelopeDeclaration, ...] = ()):
    output_hash = hashlib.sha256(output).hexdigest()
    return StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=len(claims),
        claim_set_hash=claim_set_hash(output_hash, claims),
        claims=claims,
    )


def test_source_free_finalizer_commits_one_protected_atomic_terminal_and_replays(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "source-free")
        finalizer = RunFinalizer(ready.runs)
        output = b"private terminal answer"
        observations = await finalizer.observe(
            target=ready.target, delivery=_empty_delivery(ready.target)
        )
        candidate = RunTerminalCandidate(
            observations=observations, envelope=_envelope(output), output_bytes=output
        )
        receipt = await finalizer.finalize(candidate, lease=_lease(ready))
        ready.state.release(ready.claim)
        replay = await finalizer.finalize(
            candidate,
            lease=_lease(ready).model_copy(update={"lease_token": "stale-replay-token"}),
        )
        assert replay.replayed and replay == receipt.model_copy(update={"replayed": True})
        assert receipt.output_verdict == "unavailable"
        assert receipt.mapping_state == "unavailable"
        assert receipt.protected_output_kind == "protected_turn_content"
        assert (
            await ready.runs.materialize_source_free_turn(
                owner_principal_id="alice",
                target=ready.target.canonical(),
                protected_ref=str(receipt.protected_output_ref),
                plaintext_hash=str(receipt.protected_output_hash),
            )
            == output
        )
        with ready.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(ne_agent_runs.c.status).where(
                        ne_agent_runs.c.run_id == ready.target.run_id
                    )
                ).scalar_one()
                == RunState.COMPLETED.value
            )
            for table in (
                ne_agent_protected_turn_contents,
                ne_agent_run_history_attachments,
                ne_agent_run_finalizations,
                ne_agent_run_lifecycle_outbox,
            ):
                assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 1
            clear = json.dumps(
                {
                    "protected": dict(
                        conn.execute(sa.select(ne_agent_protected_turn_contents)).one()._mapping
                    ),
                    "history": dict(
                        conn.execute(sa.select(ne_agent_run_history_attachments)).one()._mapping
                    ),
                    "events": [
                        dict(row._mapping) for row in conn.execute(sa.select(ne_agent_run_events))
                    ],
                    "claims": [
                        dict(row._mapping) for row in conn.execute(sa.select(ne_agent_run_claims))
                    ],
                    "finalization": dict(
                        conn.execute(sa.select(ne_agent_run_finalizations)).one()._mapping
                    ),
                    "outbox": dict(
                        conn.execute(sa.select(ne_agent_run_lifecycle_outbox)).one()._mapping
                    ),
                },
                default=str,
            )
            assert "private terminal answer" not in clear

    asyncio.run(scenario())


def test_postgres_finalizer_rollback_concurrency_and_stale_lease_replay(
    tmp_path: Path,
) -> None:
    url = os.environ.get("NE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("NE_TEST_POSTGRES_URL is required for the PRE-010F PostgreSQL proof")
    admin = sa.create_engine(url)
    schema = f"test_run_finalizer_{uuid.uuid4().hex}"
    try:
        with admin.begin() as conn:
            conn.execute(sa.schema.CreateSchema(schema))
    except Exception as exc:
        admin.dispose()
        pytest.skip(f"NE_TEST_POSTGRES_URL is not reachable: {type(exc).__name__}")
    engine = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema}"})

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "postgres-atomic", engine=engine)
        finalizer = RunFinalizer(ready.runs)
        output = b"postgres private terminal answer"
        claim = RunClaimEnvelopeDeclaration(
            claim_key="postgres-local-claim",
            span_start_byte=0,
            span_end_byte=len(output),
            claim_content_hash=hashlib.sha256(output).hexdigest(),
        )
        candidate = RunTerminalCandidate(
            observations=await finalizer.observe(
                target=ready.target, delivery=_empty_delivery(ready.target)
            ),
            envelope=_envelope(output, (claim,)),
            output_bytes=output,
        )

        def fail_history(
            _conn: Any,
            clause: Any,
            _multiparams: Any,
            _params: Any,
            _options: Any,
        ) -> None:
            if isinstance(clause, Insert) and clause.table is ne_agent_run_history_attachments:
                raise RuntimeError("injected postgres protected-history rollback")

        event.listen(engine, "before_execute", fail_history)
        try:
            with pytest.raises(RuntimeError, match="protected-history rollback"):
                await finalizer.finalize(candidate, lease=_lease(ready))
        finally:
            event.remove(engine, "before_execute", fail_history)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_finalizations)
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    sa.select(ne_agent_runs.c.status).where(
                        ne_agent_runs.c.run_id == ready.target.run_id
                    )
                ).scalar_one()
                == RunState.RUNNING.value
            )

        first, second = await asyncio.gather(
            finalizer.finalize(candidate, lease=_lease(ready)),
            finalizer.finalize(candidate, lease=_lease(ready)),
        )
        assert first.model_copy(update={"replayed": False}) == second.model_copy(
            update={"replayed": False}
        )
        assert {first.replayed, second.replayed} == {False, True}
        ready.state.release(ready.claim)
        replay = await finalizer.finalize(
            candidate,
            lease=_lease(ready).model_copy(update={"lease_token": "stale-postgres-replay"}),
        )
        assert replay.replayed
        with engine.connect() as conn:
            for table in (
                ne_agent_protected_turn_contents,
                ne_agent_run_claims,
                ne_agent_run_history_attachments,
                ne_agent_run_finalizations,
                ne_agent_run_lifecycle_outbox,
            ):
                assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 1

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.schema.DropSchema(schema, cascade=True))
        admin.dispose()


def test_finalizer_rolls_back_protected_output_claims_history_event_and_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "rollback")
        finalizer = RunFinalizer(ready.runs)
        output = b"rollback sentinel"
        candidate = RunTerminalCandidate(
            observations=await finalizer.observe(
                target=ready.target, delivery=_empty_delivery(ready.target)
            ),
            envelope=_envelope(output),
            output_bytes=output,
        )

        def fail_history(
            _conn: Any,
            clause: Any,
            _multiparams: Any,
            _params: Any,
            _options: Any,
        ) -> None:
            if isinstance(clause, Insert) and clause.table is ne_agent_run_history_attachments:
                raise RuntimeError("injected protected history failure")

        event.listen(ready.engine, "before_execute", fail_history)
        try:
            with pytest.raises(RuntimeError, match="injected protected history failure"):
                await finalizer.finalize(candidate, lease=_lease(ready))
        finally:
            event.remove(ready.engine, "before_execute", fail_history)
        with ready.engine.connect() as conn:
            for table in (
                ne_agent_protected_turn_contents,
                ne_agent_run_claims,
                ne_agent_run_claim_evidence,
                ne_agent_run_history_attachments,
                ne_agent_run_finalizations,
                ne_agent_run_lifecycle_outbox,
            ):
                assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
            assert (
                conn.execute(
                    sa.select(ne_agent_runs.c.status).where(
                        ne_agent_runs.c.run_id == ready.target.run_id
                    )
                ).scalar_one()
                == RunState.RUNNING.value
            )

    asyncio.run(scenario())


def test_envelope_rejects_canonical_claim_evidence_and_locator_authorship() -> None:
    declaration = {
        "claim_key": "local-1",
        "span_start_byte": 0,
        "span_end_byte": 1,
        "claim_content_hash": hashlib.sha256(b"a").hexdigest(),
    }
    for forbidden in (
        {"claim_id": "caller-claim"},
        {"evidence_id": "caller-evidence"},
        {"locator_hash": "a" * 64},
        {"association_hash": "b" * 64},
        {"verifier_receipt_id": "caller-verifier"},
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RunClaimEnvelopeDeclaration.model_validate({**declaration, **forbidden})


def test_structured_envelope_rejects_wrong_count_set_output_and_span_hashes() -> None:
    output = b"exact output"
    output_hash = hashlib.sha256(output).hexdigest()
    claim = RunClaimEnvelopeDeclaration(
        claim_key="claim-local",
        span_start_byte=0,
        span_end_byte=len(output),
        claim_content_hash=output_hash,
    )
    for changed in (
        {"claim_count": 0},
        {"claim_set_hash": "f" * 64},
    ):
        with pytest.raises(ValidationError):
            StructuredRunOutputEnvelope.model_validate(
                {
                    "discriminator": "structured_run_output",
                    "served_output_hash": output_hash,
                    "claim_count": 1,
                    "claim_set_hash": claim_set_hash(output_hash, (claim,)),
                    "claims": (claim,),
                    **changed,
                }
            )
    target = RunTurnTarget(
        conversation_id="conversation",
        run_id="run",
        turn_id="turn",
        turn_sequence=1,
        attachment_id="attachment",
        attempt_id="attempt",
    )
    observations = RunFinalizerObservations(
        target=target,
        delivery_set_id="delivery-set",
        delivery_set_hash=delivery_set_hash("delivery-set", ()),
        deliveries=(),
        evidence=(),
        verifiers=(),
        approvals=(),
        audit_receipts=(),
    )
    valid = _envelope(output, (claim,))
    with pytest.raises(ValidationError, match="terminal output bytes"):
        RunTerminalCandidate(
            observations=observations,
            envelope=valid,
            output_bytes=b"wrong output",
        )
    wrong_span = claim.model_copy(
        update={"span_end_byte": len(output) - 1, "claim_content_hash": output_hash}
    )
    with pytest.raises(ValidationError, match="claim content hash"):
        RunTerminalCandidate(
            observations=observations,
            envelope=_envelope(output, (wrong_span,)),
            output_bytes=output,
        )


def test_missing_delivery_collector_never_downgrades_output_to_source_free(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "unknown-delivery")
        finalizer = RunFinalizer(ready.runs)
        with run_delivery_observation_scope(
            run_id=ready.target.run_id, attempt_id=ready.target.attempt_id
        ) as observer:
            unknown = observer.snapshot()
        with pytest.raises(RuntimeError, match="observation is unknown"):
            await finalizer.observe(target=ready.target, delivery=unknown)
        with ready.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
                ).scalar_one()
                == 0
            )

    asyncio.run(scenario())


async def _deliver_one_source_evidence(ready: _ReadyRun) -> tuple[Any, Any, str, Any]:
    """Serve ONE evidence record to the model and audit both decisions.

    Extracted verbatim so a test about how many CLAIMS may cite one record
    does not restate ninety lines of evidence/delivery setup that has nothing
    to do with claims.
    """

    source_bytes = b"source passage"
    evidence = await ready.runs.claim_evidence(
        run_id=ready.target.run_id,
        owner_principal_id="alice",
        attempt_id=ready.target.attempt_id,
        declaration=EvidenceDeclaration(
            turn_id=ready.target.turn_id,
            step_id="knowledge-read",
            tool_call_id="tool-1",
            read_ordinal=0,
            content_hash=hashlib.sha256(source_bytes).hexdigest(),
            safe_event_detail={"evidence_status": "located"},
            locator_plaintext=b"immutable locator",
            pending_result_plaintext=source_bytes,
            source_stamps=(_stamp(),),
            assurance=EvidenceAssurance.DIRECT,
        ),
    )
    await _audit(
        ready.runs,
        run_id=ready.target.run_id,
        attempt_id=ready.target.attempt_id,
        event_id=evidence.producing_event_id,
        config_hash=ready.config_hash,
        decision=RunAuditDecisionKind.EVIDENCE,
        step_id="knowledge-read",
        ordinal=2,
    )
    context = AuthenticatedRequestContext(
        principal_id="alice",
        authorization_groups=("analysts",),
        issuer="https://issuer.example",
        expires_at=4_102_444_800,
    )

    async def authorize(
        stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
    ) -> SourceAuthorityDecision:
        del stamp, context
        return SourceAuthorityDecision.AUTHORIZED

    resolver = RunStoreSourceBoundResultResolver(ready.runs, authorize)
    reference = SourceBoundToolResultRef(
        reference_id=evidence.pending_result_id,
        result_hash=hashlib.sha256(source_bytes).hexdigest(),
        stamps=(_stamp(),),
    )
    with (
        authenticated_request_scope(context),
        run_delivery_observation_scope(
            run_id=ready.target.run_id, attempt_id=ready.target.attempt_id
        ) as observer,
        model_call_scope(
            run_id=ready.target.run_id,
            model_call_attempt_id=ready.target.attempt_id,
        ),
    ):
        resolution = await resolver.for_model_send(
            reference,
            principal_id="alice",
            access_context_hash=context.context_hash,
            run_id=ready.target.run_id,
            model_call_attempt_id=ready.target.attempt_id,
        )
        assert resolution.decision is SourceAuthorityDecision.AUTHORIZED
        complete_run_delivery_observation()
        delivery = observer.snapshot()
    with ready.engine.connect() as conn:
        delivered = conn.execute(
            sa.select(ne_agent_delivery_receipts).where(
                ne_agent_delivery_receipts.c.delivery_receipt_id == delivery.delivery_receipt_ids[0]
            )
        ).one()
        locator_hash = str(
            conn.execute(
                sa.select(ne_agent_evidence_locators.c.locator_hash).where(
                    ne_agent_evidence_locators.c.locator_id == evidence.locator_id
                )
            ).scalar_one()
        )
    await _audit(
        ready.runs,
        run_id=ready.target.run_id,
        attempt_id=ready.target.attempt_id,
        event_id=str(delivered.event_id),
        config_hash=ready.config_hash,
        decision=RunAuditDecisionKind.DELIVERY,
        step_id="run-execution",
        ordinal=3,
    )
    return evidence, delivered, locator_hash, delivery


def _prove_source_delivery_uses_exact_evidence_projection_readers(
    tmp_path: Path, *, engine: sa.Engine | None = None
) -> None:
    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "source-bound", engine=engine)
        evidence, delivered, locator_hash, delivery = await _deliver_one_source_evidence(ready)
        output = b"supported answer"
        output_hash = hashlib.sha256(output).hexdigest()
        claim = RunClaimEnvelopeDeclaration(
            claim_key="claim-local-1",
            span_start_byte=0,
            span_end_byte=len(output),
            claim_content_hash=output_hash,
            evidence_key="evidence-local-1",
            verifier_key="verifier-local-1",
        )
        binding = RunEvidenceBinding(
            evidence_id=evidence.evidence_id,
            evidence_key="evidence-local-1",
            claim_key=claim.claim_key,
            claim_content_hash=claim.claim_content_hash,
            served_output_hash=output_hash,
            span_start_byte=claim.span_start_byte,
            span_end_byte=claim.span_end_byte,
            producing_event_id=evidence.producing_event_id,
            locator_hash=locator_hash,
            served_citation_ref_hash=served_evidence_binding_hash(
                claim_key=claim.claim_key,
                claim_content_hash=claim.claim_content_hash,
                served_output_hash=output_hash,
                span_start_byte=claim.span_start_byte,
                span_end_byte=claim.span_end_byte,
                evidence_id=evidence.evidence_id,
                delivery_receipt_id=str(delivered.delivery_receipt_id),
                protected_ref=str(delivered.protected_ref),
                protected_hash=str(delivered.protected_hash),
            ),
            delivery_receipt_id=str(delivered.delivery_receipt_id),
        )
        verifier_method = "deterministic-span-check"
        verifier_version = "v1"
        result_hash = hashlib.sha256(b"verified").hexdigest()
        verifier_record = await ready.runs.claim_run_claim_verifier(
            run_id=ready.target.run_id,
            owner_principal_id="alice",
            attempt_id=ready.target.attempt_id,
            claim_key=claim.claim_key,
            claim_content_hash=claim.claim_content_hash,
            served_output_hash=output_hash,
            span_start_byte=claim.span_start_byte,
            span_end_byte=claim.span_end_byte,
            verifier_method=verifier_method,
            verifier_version=verifier_version,
            result_hash=result_hash,
        )
        verifier = RunVerifierBinding(
            verifier_receipt_id=verifier_record.verifier_receipt_id,
            verifier_key="verifier-local-1",
            claim_key=claim.claim_key,
            claim_content_hash=claim.claim_content_hash,
            served_output_hash=output_hash,
            span_start_byte=claim.span_start_byte,
            span_end_byte=claim.span_end_byte,
            verifier_method=verifier_method,
            verifier_version=verifier_version,
            result_hash=result_hash,
            event_id=verifier_record.event_id,
            receipt_hash=verifier_record.receipt_hash,
        )
        finalizer = RunFinalizer(ready.runs)
        with pytest.raises(ValueError, match="complete receipt set"):
            await finalizer.observe(
                target=ready.target,
                delivery=_empty_delivery(ready.target),
                evidence=(binding,),
                verifiers=(verifier,),
            )
        observations = await finalizer.observe(
            target=ready.target,
            delivery=delivery,
            evidence=(binding,),
            verifiers=(verifier,),
        )
        envelope = _envelope(output, (claim,))
        for changed, message in (
            ({"audit_receipts": ()}, "audit receipt set"),
            (
                {"evidence": (binding.model_copy(update={"evidence_id": "swapped-evidence"}),)},
                "same-Run served receipt set",
            ),
            (
                {"evidence": (binding.model_copy(update={"locator_hash": "f" * 64}),)},
                "same-Run served receipt set",
            ),
            (
                {"verifiers": (verifier.model_copy(update={"span_end_byte": len(output) - 1}),)},
                "verifier event",
            ),
        ):
            with pytest.raises(ValueError, match=message):
                await finalizer.finalize(
                    RunTerminalCandidate(
                        observations=observations.model_copy(update=changed),
                        envelope=envelope,
                        output_bytes=output,
                    ),
                    lease=_lease(ready),
                )
        receipt = await finalizer.finalize(
            RunTerminalCandidate(
                observations=observations,
                envelope=envelope,
                output_bytes=output,
            ),
            lease=_lease(ready),
        )
        assert receipt.output_verdict == "verified"
        assert receipt.mapping_state == "complete"
        assert receipt.protected_output_kind == "protected_source_output"
        assert len(receipt.claim_ids) == 1 and receipt.claim_ids[0] != claim.claim_key
        assert evidence.locator_id is not None
        assert receipt.protected_output_ref is not None
        locator = await ready.runs.load_evidence_projection_locator(
            owner_principal_id="alice",
            run_id=ready.target.run_id,
            locator_ref=evidence.locator_id,
            expected_hash=locator_hash,
        )
        stored_output = await ready.runs.load_evidence_projection_output(
            owner_principal_id="alice",
            run_id=ready.target.run_id,
            protected_ref=receipt.protected_output_ref,
            expected_hash=output_hash,
        )
        stored_claim = await ready.runs.load_evidence_projection_claim_slice(
            owner_principal_id="alice",
            run_id=ready.target.run_id,
            protected_ref=receipt.protected_output_ref,
            served_output_hash=output_hash,
            span_start_byte=claim.span_start_byte,
            span_end_byte=claim.span_end_byte,
            claim_content_hash=claim.claim_content_hash,
        )
        assert locator.plaintext == b"immutable locator"
        assert stored_output.plaintext == output
        assert stored_claim.plaintext == output
        assert locator.stamps == stored_output.stamps == stored_claim.stamps == (_stamp(),)

        locator_args = {
            "owner_principal_id": "alice",
            "run_id": ready.target.run_id,
            "locator_ref": evidence.locator_id,
            "expected_hash": locator_hash,
        }
        output_args = {
            "owner_principal_id": "alice",
            "run_id": ready.target.run_id,
            "protected_ref": receipt.protected_output_ref,
            "expected_hash": output_hash,
        }
        claim_args = {
            "owner_principal_id": "alice",
            "run_id": ready.target.run_id,
            "protected_ref": receipt.protected_output_ref,
            "served_output_hash": output_hash,
            "span_start_byte": claim.span_start_byte,
            "span_end_byte": claim.span_end_byte,
            "claim_content_hash": claim.claim_content_hash,
        }
        for method, base, changes in (
            (
                ready.runs.load_evidence_projection_locator,
                locator_args,
                (
                    {"owner_principal_id": "foreign"},
                    {"run_id": "other"},
                    {"expected_hash": "f" * 64},
                ),
            ),
            (
                ready.runs.load_evidence_projection_output,
                output_args,
                (
                    {"owner_principal_id": "foreign"},
                    {"run_id": "other"},
                    {"protected_ref": "random"},
                    {"expected_hash": "f" * 64},
                ),
            ),
            (
                ready.runs.load_evidence_projection_claim_slice,
                claim_args,
                (
                    {"owner_principal_id": "foreign"},
                    {"run_id": "other"},
                    {"protected_ref": "random"},
                    {"claim_content_hash": "f" * 64},
                ),
            ),
        ):
            for changed in changes:
                with pytest.raises(
                    EvidenceProjectionUnavailable,
                    match=r"^evidence projection payload unavailable$",
                ):
                    await cast(Any, method)(**{**base, **changed})
        with ready.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_source_outputs)
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
                ).scalar_one()
                == 0
            )
            stored_claim = conn.execute(sa.select(ne_agent_run_claims)).one()
            assert stored_claim.protected_domain == "protected_source_output"
            assert stored_claim.protected_ref == receipt.protected_output_ref
            assert stored_claim.claim_verdict == "verified"
            association = conn.execute(sa.select(ne_agent_run_claim_evidence)).one()
            assert association.evidence_id == evidence.evidence_id
            assert association.locator_hash == locator_hash

    asyncio.run(scenario())


def test_source_delivery_uses_protected_source_output_and_server_mints_verified_claim(
    tmp_path: Path,
) -> None:
    _prove_source_delivery_uses_exact_evidence_projection_readers(tmp_path)


def test_two_claims_may_cite_one_evidence_record_and_each_keeps_its_own_verifier(
    tmp_path: Path,
) -> None:
    """NE-CLAIM-SHARED-EVIDENCE: N claims, one record, N verifier rows.

    One retrieved table naming two loans is ONE evidence record and TWO claims.
    The worker used to raise a bare ``RuntimeError`` on the second one and the
    finalizer's observation set rejected the duplicate ``evidence_id`` behind
    it. Neither was protecting anything: the persisted join makes ``claim_id``
    unique and deliberately leaves ``evidence_id`` unconstrained, and every
    per-claim identity below is derived from the CLAIM key.

    What this pins is the part that must not move — two claims, two distinct
    association rows, two distinct verifier receipts, two independently
    re-sliced spans — while both cite the same server-observed record.
    """

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "shared-evidence")
        evidence, delivered, locator_hash, delivery = await _deliver_one_source_evidence(ready)
        output = b"first fact and second fact"
        output_hash = hashlib.sha256(output).hexdigest()
        spans = ((0, 10), (15, 26))
        claims: list[RunClaimEnvelopeDeclaration] = []
        bindings: list[RunEvidenceBinding] = []
        verifiers: list[RunVerifierBinding] = []
        for index, (start, end) in enumerate(spans, start=1):
            content_hash = hashlib.sha256(output[start:end]).hexdigest()
            claim = RunClaimEnvelopeDeclaration(
                claim_key=f"claim-local-{index}",
                span_start_byte=start,
                span_end_byte=end,
                claim_content_hash=content_hash,
                evidence_key=f"evidence-local-{index}",
                verifier_key=f"verifier-local-{index}",
            )
            claims.append(claim)
            bindings.append(
                RunEvidenceBinding(
                    evidence_id=evidence.evidence_id,
                    evidence_key=f"evidence-local-{index}",
                    claim_key=claim.claim_key,
                    claim_content_hash=content_hash,
                    served_output_hash=output_hash,
                    span_start_byte=start,
                    span_end_byte=end,
                    producing_event_id=evidence.producing_event_id,
                    locator_hash=locator_hash,
                    served_citation_ref_hash=served_evidence_binding_hash(
                        claim_key=claim.claim_key,
                        claim_content_hash=content_hash,
                        served_output_hash=output_hash,
                        span_start_byte=start,
                        span_end_byte=end,
                        evidence_id=evidence.evidence_id,
                        delivery_receipt_id=str(delivered.delivery_receipt_id),
                        protected_ref=str(delivered.protected_ref),
                        protected_hash=str(delivered.protected_hash),
                    ),
                    delivery_receipt_id=str(delivered.delivery_receipt_id),
                )
            )
            result_hash = hashlib.sha256(f"verified-{index}".encode()).hexdigest()
            record = await ready.runs.claim_run_claim_verifier(
                run_id=ready.target.run_id,
                owner_principal_id="alice",
                attempt_id=ready.target.attempt_id,
                claim_key=claim.claim_key,
                claim_content_hash=content_hash,
                served_output_hash=output_hash,
                span_start_byte=start,
                span_end_byte=end,
                verifier_method="deterministic-span-check",
                verifier_version="v1",
                result_hash=result_hash,
            )
            verifiers.append(
                RunVerifierBinding(
                    verifier_receipt_id=record.verifier_receipt_id,
                    verifier_key=f"verifier-local-{index}",
                    claim_key=claim.claim_key,
                    claim_content_hash=content_hash,
                    served_output_hash=output_hash,
                    span_start_byte=start,
                    span_end_byte=end,
                    verifier_method="deterministic-span-check",
                    verifier_version="v1",
                    result_hash=result_hash,
                    event_id=record.event_id,
                    receipt_hash=record.receipt_hash,
                )
            )

        finalizer = RunFinalizer(ready.runs)
        # The observation set no longer rejects the shared record itself...
        observations = await finalizer.observe(
            target=ready.target,
            delivery=delivery,
            evidence=tuple(bindings),
            verifiers=tuple(verifiers),
        )
        envelope = _envelope(output, tuple(claims))

        # ...and every check that made the 1:1 rule look load-bearing still bites.
        with pytest.raises(ValueError, match="same-Run served receipt set"):
            await finalizer.finalize(
                RunTerminalCandidate(
                    observations=observations.model_copy(
                        update={
                            "evidence": (
                                bindings[0],
                                bindings[1].model_copy(update={"evidence_id": "swapped-evidence"}),
                            )
                        }
                    ),
                    envelope=envelope,
                    output_bytes=output,
                ),
                lease=_lease(ready),
            )
        with pytest.raises(ValueError, match="did not consume the exact observation set"):
            await finalizer.finalize(
                RunTerminalCandidate(
                    observations=observations,
                    envelope=_envelope(output, (claims[0],)),
                    output_bytes=output,
                ),
                lease=_lease(ready),
            )

        receipt = await finalizer.finalize(
            RunTerminalCandidate(observations=observations, envelope=envelope, output_bytes=output),
            lease=_lease(ready),
        )
        assert receipt.output_verdict == "verified"
        assert receipt.mapping_state == "complete"
        assert len(set(receipt.claim_ids)) == 2
        assert receipt.protected_output_ref is not None

        # The packed terminal payload names the SET of records; a repeat there
        # is refused on unpack, so this read is the dedupe's regression test.
        stored_output = await ready.runs.load_evidence_projection_output(
            owner_principal_id="alice",
            run_id=ready.target.run_id,
            protected_ref=receipt.protected_output_ref,
            expected_hash=output_hash,
        )
        assert stored_output.plaintext == output
        for claim in claims:
            slice_payload = await ready.runs.load_evidence_projection_claim_slice(
                owner_principal_id="alice",
                run_id=ready.target.run_id,
                protected_ref=receipt.protected_output_ref,
                served_output_hash=output_hash,
                span_start_byte=claim.span_start_byte,
                span_end_byte=claim.span_end_byte,
                claim_content_hash=claim.claim_content_hash,
            )
            # Each claim re-slices ITS OWN bytes out of the one served answer.
            assert slice_payload.plaintext == output[claim.span_start_byte : claim.span_end_byte]

        with ready.engine.connect() as conn:
            associations = conn.execute(sa.select(ne_agent_run_claim_evidence)).all()
            assert len(associations) == 2
            assert {str(row.evidence_id) for row in associations} == {evidence.evidence_id}
            assert len({str(row.claim_id) for row in associations}) == 2
            assert len({str(row.association_hash) for row in associations}) == 2
            stored_claims = conn.execute(sa.select(ne_agent_run_claims)).all()
            assert len(stored_claims) == 2
            assert {str(row.claim_verdict) for row in stored_claims} == {"verified"}
        assert len({item.verifier_receipt_id for item in verifiers}) == 2
        assert len({item.receipt_hash for item in verifiers}) == 2

    asyncio.run(scenario())


def _is_servable_reason(reason: object) -> bool:
    """The projection's exact acceptance test for a published failure reason.

    Copied deliberately, not imported: ne-runtime must not depend on ne-api, and
    what matters is that the derived floor satisfies the reader-facing rule
    INDEPENDENTLY of who wrote it (`ne.api.run_projection._failure_reason`).
    """

    return (
        isinstance(reason, str)
        and bool(reason.strip())
        and len(reason) <= 256
        and all(character.isprintable() for character in reason)
    )


def test_derived_failure_reason_is_always_servable_and_leaks_no_value() -> None:
    """A failure NOBODY explained still says something honest, safe and bounded.

    The floor may name a CLASS and nothing else. Every hostile input below is a
    value, not a class - a prompt fragment, an answer fragment, a control
    character, an unbounded string - and none of them may survive into a string
    that is published verbatim to a reader (C12).
    """

    hostile = (
        None,
        "",
        "   ",
        "\n\t",
        "A" * 4000,
        "the customer's salary is 184000 GBP",
        "answer: DR-4830 supersedes DR-1207",
        "Runtime\u0007Error",
        "'; DROP TABLE ne_agent_runs; --",
        "<script>alert(1)</script>",
        "класс",
    )
    for value in hostile:
        reason = derived_failure_reason(value)
        assert _is_servable_reason(reason), reason
        assert len(reason) <= _MAX_SAFE_REASON_CHARS
        assert "UnnamedRunFailure" in reason
        if isinstance(value, str) and value.strip():
            assert value.strip() not in reason

    # THE CAP IS ONE CAP. `ne.runtime.runner` clamps a raise-site `safe_reason`
    # against its own copy of this bound, and the projection drops anything past
    # 256 outright - a floor built past it would be no explanation and a
    # `failure_reason: null` all over again, which is the bug this fixes.
    from ne.runtime.runner import _MAX_SAFE_REASON_CHARS as _RUNNER_CAP

    assert _MAX_SAFE_REASON_CHARS == _RUNNER_CAP == 256

    # A real class IS named - that is the whole point of the floor.
    named = derived_failure_reason("RunEffectSettlementUncertain")
    assert _is_servable_reason(named)
    assert named.endswith("Recorded failure class: RunEffectSettlementUncertain.")
    assert safe_failure_class("RunEffectSettlementUncertain") == "RunEffectSettlementUncertain"


def test_published_failure_reason_serves_a_marked_reason_verbatim_and_never_nothing() -> None:
    """The opt-in reason is unchanged byte-for-byte; the empty case is gone.

    NE-SELECT-REPROBE's contract is preserved exactly: a raiser that marked a
    static `safe_reason` still has it published character for character. What is
    no longer possible is the third outcome - a failed Run with nothing to say.
    """

    marked = "model has no current serving evidence"
    assert (
        published_failure_reason(safe_reason=marked, failure_class="ModelSelectionError") == marked
    )

    floor = derived_failure_reason("ModelSelectionError")
    for unservable in (None, "", "   ", 17, ["a"], "x" * 257, "bad\u0000reason"):
        published = published_failure_reason(
            safe_reason=unservable, failure_class="ModelSelectionError"
        )
        assert published == floor
        assert _is_servable_reason(published)


def test_a_refused_terminal_with_served_deliveries_stays_readable_and_source_free(
    tmp_path: Path,
) -> None:
    """NE-GA-RUN1. A grounding-gate withhold refuses AFTER genuinely serving
    verified evidence, so its terminal arrives with served delivery receipts.
    The refusal text is a server-authored account of the decision (classes +
    counts, zero claims by the envelope's own validator) — it must be stored
    as source-free ``protected_turn_content`` so `run_projection._preview_target`
    can disclose it (the decline card the demo shows), while the served
    deliveries keep their receipts and evidence rows untouched. Before this,
    served deliveries forced the refusal prose into the source-bound container,
    which rendered the one beat where the system correctly declines as
    "no output was served"."""

    async def scenario() -> None:
        ready = await _ready_run(tmp_path, "refused-with-delivery")
        _evidence, _delivered, _locator_hash, delivery = await _deliver_one_source_evidence(ready)
        output = (
            b"Answer withheld by the grounding gate. The deterministic gate found "
            b"1 element of the draft it could not verify against the rows "
            b"retrieved during this run (finding classes: numeral-ungrounded)."
        )
        output_hash = hashlib.sha256(output).hexdigest()
        envelope = StructuredRunOutputEnvelope(
            discriminator="structured_run_output",
            served_output_hash=output_hash,
            claim_count=0,
            claim_set_hash=claim_set_hash(output_hash, ()),
            claims=(),
            disposition="refused",
        )
        finalizer = RunFinalizer(ready.runs)
        observations = await finalizer.observe(
            target=ready.target,
            delivery=delivery,
            evidence=(),
            verifiers=(),
        )
        assert any(item.decision == "served" for item in observations.deliveries)
        receipt = await finalizer.finalize(
            RunTerminalCandidate(
                observations=observations,
                envelope=envelope,
                output_bytes=output,
            ),
            lease=_lease(ready),
        )
        # The decision lane end-to-end: verdict refused, mapping unavailable,
        # run COMPLETED with outcome "refused" — a correct outcome, not a failure.
        assert receipt.output_verdict == "refused"
        assert receipt.mapping_state == "unavailable"
        assert receipt.protected_output_kind == "protected_turn_content"
        assert receipt.claim_ids == ()
        with ready.engine.connect() as conn:
            # The refusal prose is stored source-free and READABLE...
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_turn_contents)
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_protected_source_outputs)
                ).scalar_one()
                == 0
            )
            run_row = conn.execute(
                sa.select(ne_agent_runs).where(ne_agent_runs.c.run_id == ready.target.run_id)
            ).one()
            assert run_row.status == RunState.COMPLETED.value
            final_row = conn.execute(sa.select(ne_agent_run_finalizations)).one()
            assert final_row.output_verdict == "refused"
            assert final_row.protected_output_kind == "protected_turn_content"
            attachment = conn.execute(sa.select(ne_agent_run_history_attachments)).one()
            assert attachment.content_status == "source_free"
            # ...and the served delivery receipt survives untouched beside it.
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_delivery_receipts)
                ).scalar_one()
                >= 1
            )

    asyncio.run(scenario())
