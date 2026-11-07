# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""A verified claim anchored to a knowledge SEARCH read renders its citation.

WHY THIS FILE EXISTS, END TO END. Twice in one night a claim-path surface went
dark because every unit test hand-built the payload shape it expected while the
live writer wrote another: the answer path (evidence ids packed into the v2
payload met a resolver door that refused them) and then the citation path (the
search tool's `knowledge_search` locator met a parser with no arm for it, so
every claim anchored to a search read — on the live corpus, EVERY claim —
reported "Claim mapping unavailable" with its association, verifier, spans and
hashes all valid).

This test therefore drives the REAL pipeline: the real `AgentRunStore` with the
real cipher writes the evidence exactly as `ne.api.tools` writes a search read
(same locator dict, same `SearchEvidenceDetail` event shape), the real
source-results resolver delivers it, the real `RunFinalizer` commits the
verified claim, and the real `EvidenceProjector` + `SqlAlchemyEvidenceProjection
Reader` + `RunStoreCurrentEvidenceMaterializer` project it back. If the writer
and the reader ever drift apart again, this fails — no fake payloads anywhere.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace
from typing import cast

import sqlalchemy as sa

from ne.api.evidence_projection import (
    EvidenceProjector,
    RunStoreCurrentEvidenceMaterializer,
    SqlAlchemyEvidenceProjectionReader,
)
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    EvidenceAssurance,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceBoundToolResultRef,
)
from ne.contracts.types import Principal
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_finalizer import (
    RunClaimEnvelopeDeclaration,
    RunEvidenceBinding,
    RunFinalizer,
    RunFinalizerLease,
    RunTerminalCandidate,
    RunTurnTarget,
    RunVerifierBinding,
    StructuredRunOutputEnvelope,
    claim_set_hash,
    served_evidence_binding_hash,
)
from ne.runtime.run_store import (
    AgentRunStore,
    EvidenceDeclaration,
    RunAuditDecisionKind,
    ensure_run_schema,
    ne_agent_delivery_receipts,
    ne_agent_evidence_locators,
)
from ne.runtime.run_workflow import RunWorkflowStateStore, ensure_run_workflow_schema
from ne.runtime.source_results import (
    RunStoreSourceBoundResultResolver,
    authenticated_request_scope,
    complete_run_delivery_observation,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from .test_knowledge_source_evidence import _stamp

_CHUNK_IDS = ("chunk-reg-e-1", "chunk-reg-e-2")


def _search_locator_exactly_as_tools_writes_it(stamp: SourceAuthorityStamp) -> bytes:
    # The literal shape from ne.api.tools (the `evidence.author.claim(...)`
    # call): `KnowledgeSourceEvidenceAuthor.claim` appends `evidence_assurance`
    # and serializes with sort_keys + compact separators (`_canonical_bytes`).
    return json.dumps(
        {
            "kind": "knowledge_search",
            "contributors": [stamp.model_dump(mode="json")],
            "chunk_ids": list(_CHUNK_IDS),
            "evidence_assurance": "direct",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


async def _scenario() -> None:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
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
            idempotency_key="start-citation",
            request_hash=hashlib.sha256(b"citation").hexdigest(),
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
            sa.text("SELECT * FROM ne_agent_run_turn_targets WHERE run_id = :run"),
            {"run": accepted.run_id},
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
    workflow_claim = state.claim(
        dispatch_id="dispatch-citation",
        run_id=accepted.run_id,
        owner_principal_id="alice",
        config_hash=config_hash,
    )

    async def audit(
        event_id: str, decision: RunAuditDecisionKind, ordinal: int, step_id: str
    ) -> None:
        await runs.record_audit_receipt(
            run_id=target.run_id,
            attempt_id=target.attempt_id,
            run_event_id=event_id,
            decision_kind=decision,
            audit_event_id=f"audit-{ordinal}-{uuid.uuid4()}",
            audit_sequence=ordinal,
            audit_record_hash=hashlib.sha256(f"audit-{ordinal}".encode()).hexdigest(),
            config_hash=config_hash,
            owner_principal_id="alice",
            step_id=step_id,
        )

    await audit(
        workflow_claim.claim_event_id, RunAuditDecisionKind.STATE_TRANSITION, 1, "run-execution"
    )

    # --- the SEARCH read, written exactly as ne.api.tools writes it ----------
    stamp = _stamp()
    rendering = b"[showing 2 of 2 rows]\nchunk-reg-e-1: ten days\nchunk-reg-e-2: five days"
    evidence = await runs.claim_evidence(
        run_id=target.run_id,
        owner_principal_id="alice",
        attempt_id=target.attempt_id,
        declaration=EvidenceDeclaration(
            turn_id=target.turn_id,
            step_id="run-tool",
            tool_call_id="run-tool:tool:0",
            read_ordinal=0,
            content_hash=hashlib.sha256(rendering).hexdigest(),
            safe_event_detail={
                "read_kind": "search",
                "evidence_status": "located",
                "contributor_count": 1,
                "evidence_assurance": "direct",
            },
            locator_plaintext=_search_locator_exactly_as_tools_writes_it(stamp),
            pending_result_plaintext=rendering,
            source_stamps=(stamp,),
            assurance=EvidenceAssurance.DIRECT,
        ),
    )
    await audit(evidence.producing_event_id, RunAuditDecisionKind.EVIDENCE, 2, "run-tool")

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

    resolver = RunStoreSourceBoundResultResolver(runs, authorize)
    reference = SourceBoundToolResultRef(
        reference_id=evidence.pending_result_id,
        result_hash=hashlib.sha256(rendering).hexdigest(),
        stamps=(stamp,),
    )
    with (
        authenticated_request_scope(context),
        run_delivery_observation_scope(
            run_id=target.run_id, attempt_id=target.attempt_id
        ) as observer,
        model_call_scope(run_id=target.run_id, model_call_attempt_id=target.attempt_id),
    ):
        resolution = await resolver.for_model_send(
            reference,
            principal_id="alice",
            access_context_hash=context.context_hash,
            run_id=target.run_id,
            model_call_attempt_id=target.attempt_id,
        )
        assert resolution.decision is SourceAuthorityDecision.AUTHORIZED
        complete_run_delivery_observation()
        delivery = observer.snapshot()
    with engine.connect() as conn:
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
    await audit(str(delivered.event_id), RunAuditDecisionKind.DELIVERY, 3, "run-execution")

    # --- one VERIFIED claim over a sub-span of the answer --------------------
    output = b"Reg E requires provisional credit within ten days."
    start = output.index(b"ten days")
    end = start + len(b"ten days")
    output_hash = hashlib.sha256(output).hexdigest()
    span_hash = hashlib.sha256(output[start:end]).hexdigest()
    verifier_record = await runs.claim_run_claim_verifier(
        run_id=target.run_id,
        owner_principal_id="alice",
        attempt_id=target.attempt_id,
        claim_key="claim-local-1",
        claim_content_hash=span_hash,
        served_output_hash=output_hash,
        span_start_byte=start,
        span_end_byte=end,
        verifier_method="deterministic-span-check",
        verifier_version="v1",
        result_hash=hashlib.sha256(b"verified").hexdigest(),
    )
    binding = RunEvidenceBinding(
        evidence_id=evidence.evidence_id,
        evidence_key="evidence-local-1",
        claim_key="claim-local-1",
        claim_content_hash=span_hash,
        served_output_hash=output_hash,
        span_start_byte=start,
        span_end_byte=end,
        producing_event_id=evidence.producing_event_id,
        locator_hash=locator_hash,
        served_citation_ref_hash=served_evidence_binding_hash(
            claim_key="claim-local-1",
            claim_content_hash=span_hash,
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
    verifier = RunVerifierBinding(
        verifier_receipt_id=verifier_record.verifier_receipt_id,
        verifier_key="verifier-local-1",
        claim_key="claim-local-1",
        claim_content_hash=span_hash,
        served_output_hash=output_hash,
        span_start_byte=start,
        span_end_byte=end,
        verifier_method="deterministic-span-check",
        verifier_version="v1",
        result_hash=hashlib.sha256(b"verified").hexdigest(),
        event_id=verifier_record.event_id,
        receipt_hash=verifier_record.receipt_hash,
    )
    declaration = RunClaimEnvelopeDeclaration(
        claim_key="claim-local-1",
        span_start_byte=start,
        span_end_byte=end,
        claim_content_hash=span_hash,
        evidence_key="evidence-local-1",
        verifier_key="verifier-local-1",
    )
    finalizer = RunFinalizer(runs)
    observations = await finalizer.observe(
        target=target, delivery=delivery, evidence=(binding,), verifiers=(verifier,)
    )
    receipt = await finalizer.finalize(
        RunTerminalCandidate(
            observations=observations,
            envelope=StructuredRunOutputEnvelope(
                discriminator="structured_run_output",
                served_output_hash=output_hash,
                claim_count=1,
                claim_set_hash=claim_set_hash(output_hash, (declaration,)),
                claims=(declaration,),
            ),
            output_bytes=output,
        ),
        lease=RunFinalizerLease(
            dispatch_id=workflow_claim.dispatch_id,
            run_id=workflow_claim.run_id,
            attempt_id=workflow_claim.attempt_id,
            lease_token=workflow_claim.lease_token,
            lease_epoch=workflow_claim.lease_epoch,
        ),
    )
    assert receipt.output_verdict == "verified"
    assert receipt.mapping_state == "complete"

    # --- project it back through the REAL read stack -------------------------
    projector = EvidenceProjector(
        visible_agent=lambda agent_id, principal: True,
        reader=SqlAlchemyEvidenceProjectionReader(engine),
        materializer=RunStoreCurrentEvidenceMaterializer(runs, authorize),
    )
    with authenticated_request_scope(context):
        envelope = await projector.project(
            agent_id="agt_01",
            run_id=target.run_id,
            principal=cast(Principal, SimpleNamespace(id="alice")),
            access_context_hash=context.context_hash,
            projection_attempt_id=str(uuid.uuid4()),
        )

    assert envelope.output.state == "available"
    assert envelope.output.display == output.decode()

    item = envelope.evidence[0]
    assert item.state == "available"
    assert item.message == "Evidence available"
    assert item.locator is not None
    assert item.locator.kind == "knowledge_search"
    # The chunk ids the console resolves back into the knowledge store, in the
    # served order; the contributor stamps must NOT be projected (C12).
    assert item.locator.chunk_ids == _CHUNK_IDS
    disclosed = item.locator.model_dump(mode="json", exclude_none=True)
    assert "contributors" not in disclosed

    claim = envelope.claims[0]
    assert claim.claim_verdict == "verified"
    assert claim.state == "available"
    assert claim.message == "Claim available"
    assert claim.display == "ten days"
    assert claim.evidence_id == evidence.evidence_id
    assert claim.verifier_event_id == verifier_record.event_id
    engine.dispose()


def test_verified_search_read_claim_renders_its_citation_end_to_end() -> None:
    asyncio.run(_scenario())
