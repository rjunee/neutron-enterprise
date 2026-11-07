# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""REGRESSION — the second turn of a canonical Ask Conversation must be admitted.

Production symptom (found 2026-08-28, present since canonical Ask activation):
every second ``POST /api/v1/chat/sessions/<id>/messages`` in ANY Conversation
returned 409 ``canonical default Ask conflict`` — with a model swap, with the
same model, and with no ``requested_model`` at all. Only fresh Conversations
worked, which is why every scripted demo beat hid it.

Root cause: the finalizer stores every evidence-serving answer as a
source-bound container (``source_bound_container`` in
``ne_agent_protected_source_outputs`` — ``run_finalizer._protect_output``),
but ``run_store._materialize_prior_turns_in_transaction`` accepted only the
source-free shape and raised ``ProtectedTurnUnavailable`` for everything else.
``_insert_model_boundary_receipt`` runs on every canonical admission (the Ask
Agent always has a model policy) and mapped that into
``RunSubmissionConflictError("context-window fit is unknown")`` → HTTP 409.
So the first cited answer made every later turn of its Conversation
inadmissible, and the same helper made worker history-carry impossible.

These tests drive the real chat route against the REAL runtime stores: turn 1
is finalized exactly the way production finalizes a cited answer (a genuinely
served verified read, packed and stamped), and the second turn must then be
admitted — same model or swapped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage

from ne.api.default_ask_submission import compose_default_ask_submission_service
from ne.api.routers import chat
from ne.api.wire import ChatMessageRequest
from ne.contracts.identity import AuthenticatedRequestContext
from ne.contracts.source_authority import (
    CurrentSourceAuthorityReceipt,
    M365SourcePoint,
    ProtectedSourceOutputRef,
    SourceAuthorityDecision,
    SourceAuthorityStamp,
    SourceAuthorityState,
    SourceContentArtifactIdentity,
    SourceContractIdentity,
    SourceIdentity,
)
from ne.runtime.private_payloads import (
    PrivatePayloadDomain,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
)
from ne.runtime.run_finalizer import (
    RunFinalizer,
    RunFinalizerLease,
    RunTerminalCandidate,
    RunTurnTarget,
    StructuredRunOutputEnvelope,
    claim_set_hash,
)
from ne.runtime.run_store import (
    AgentRunStore,
    RunAuditDecisionKind,
    ensure_run_schema,
    ne_agent_delivery_receipts,
    ne_agent_run_history_attachments,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_workflow import RunWorkflowStateStore, ensure_run_workflow_schema
from ne.runtime.source_results import (
    PROTECTED_REFERENCE_PREFIX,
    SOURCE_WITHHELD_TEXT,
    ProtectedSourceResultGuard,
    RunStoreSourceBoundResultResolver,
    authenticated_request_scope,
    complete_run_delivery_observation,
    decode_protected_reference,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

from . import test_default_ask_submission as doubles
from .test_default_ask_submission import (  # the exact canonical-Ask API doubles
    AGENT_ID,
    CONFIG_HASH,
    OWNER,
    _activated_route_state,
    _authenticated_context,
    _pin,
    _run_submissions,
    _RunEnqueuer,
)

_DEPARTMENT = "operations"
WIRE_GENERATION = "default-ask/canonical-run/v1"


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


def _real_stores(tmp_path: Path) -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'multi-turn.db'}")
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
    runs.activate_default_ask_at_boot(WIRE_GENERATION)
    return engine, sessions, runs


async def _send(state: Any, conversation_id: str, body: ChatMessageRequest) -> dict[str, Any]:
    """One real chat-route turn; returns the parsed ``accepted`` SSE payload."""
    with authenticated_request_scope(_authenticated_context()):
        response = await chat.send_message(conversation_id, body, OWNER, cast(Any, state), None)
    chunks = [chunk async for chunk in response.body_iterator]
    raw = b"".join(chunk.encode() if isinstance(chunk, str) else bytes(chunk) for chunk in chunks)
    return cast(dict[str, Any], json.loads(raw.split(b"data: ", 1)[1].strip()))


async def _finalize_turn(
    engine: sa.Engine,
    runs: AgentRunStore,
    state: RunWorkflowStateStore,
    *,
    run_id: str,
    attempt_id: str,
    tag: str,
    output: bytes,
    cited: bool,
) -> None:
    """Finalize one admitted Run the way production does.

    ``cited=True`` records a genuinely SERVED verified read first, so the
    finalizer stores the source-bound container shape every evidence-backed
    production answer gets (the shape that used to poison the Conversation).
    ``cited=False`` finalizes source-free.
    """
    with engine.connect() as conn:
        authored = conn.execute(
            sa.select(ne_agent_run_turn_targets).where(ne_agent_run_turn_targets.c.run_id == run_id)
        ).one()
    target = RunTurnTarget(
        conversation_id=str(authored.conversation_id),
        run_id=str(authored.run_id),
        turn_id=str(authored.turn_id),
        turn_sequence=int(authored.turn_sequence),
        attachment_id=str(authored.attachment_id),
        attempt_id=str(authored.attempt_id),
    )
    claim = state.claim(
        dispatch_id=f"dispatch-{tag}",
        run_id=run_id,
        owner_principal_id=OWNER.id,
        config_hash=CONFIG_HASH,
    )
    await runs.record_audit_receipt(
        run_id=run_id,
        attempt_id=attempt_id,
        run_event_id=claim.claim_event_id,
        decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        audit_event_id=f"audit-{tag}",
        audit_sequence=1,
        audit_record_hash=hashlib.sha256(f"audit-{tag}".encode()).hexdigest(),
        config_hash=CONFIG_HASH,
        owner_principal_id=OWNER.id,
        step_id="run-execution",
    )
    receipt_ids: list[str] = []
    if cited:
        stamp = _stamp()
        tool_result = b'{"records": ["fee posting 42"]}'
        pending = await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PENDING_SOURCE_TOOL_RESULT,
            run_id=run_id,
            owner_principal_id=OWNER.id,
            target_kind="agent_run_tool_call",
            target_binding=(run_id, attempt_id, "step-1", "tool-1", f"evidence-{tag}"),
            plaintext=tool_result,
            source_stamps=(stamp,),
        )
        receipt_id = await runs.claim_delivery(
            run_id=run_id,
            owner_principal_id=OWNER.id,
            delivery_kind="tool_result_served",
            protected_ref=pending.row_id,
            protected_hash=hashlib.sha256(tool_result).hexdigest(),
            source_stamps=(stamp,),
            receiving_attempt_id=attempt_id,
        )
        receipt_ids.append(receipt_id)
        with engine.connect() as conn:
            delivery_event_id = conn.execute(
                sa.select(ne_agent_delivery_receipts.c.event_id).where(
                    ne_agent_delivery_receipts.c.delivery_receipt_id == receipt_id
                )
            ).scalar_one()
        await runs.record_audit_receipt(
            run_id=run_id,
            attempt_id=attempt_id,
            run_event_id=str(delivery_event_id),
            decision_kind=RunAuditDecisionKind.DELIVERY,
            audit_event_id=f"audit-delivery-{tag}",
            audit_sequence=2,
            audit_record_hash=hashlib.sha256(f"audit-delivery-{tag}".encode()).hexdigest(),
            config_hash=CONFIG_HASH,
            owner_principal_id=OWNER.id,
        )
    with (
        run_delivery_observation_scope(run_id=run_id, attempt_id=attempt_id) as observer,
        model_call_scope(run_id=run_id, model_call_attempt_id=attempt_id),
    ):
        for receipt_id in receipt_ids:
            observer.record_receipt(run_id=run_id, attempt_id=attempt_id, receipt_id=receipt_id)
        complete_run_delivery_observation()
        delivery = observer.snapshot()
    finalizer = RunFinalizer(runs)
    observations = await finalizer.observe(target=target, delivery=delivery)
    output_hash = hashlib.sha256(output).hexdigest()
    await finalizer.finalize(
        RunTerminalCandidate(
            observations=observations,
            envelope=StructuredRunOutputEnvelope(
                discriminator="structured_run_output",
                served_output_hash=output_hash,
                claim_count=0,
                claim_set_hash=claim_set_hash(output_hash, ()),
                claims=(),
            ),
            output_bytes=output,
        ),
        lease=RunFinalizerLease(
            dispatch_id=claim.dispatch_id,
            run_id=claim.run_id,
            attempt_id=claim.attempt_id,
            lease_token=claim.lease_token,
            lease_epoch=claim.lease_epoch,
        ),
    )
    state.release(claim)


def _texts(messages: list[ModelMessage]) -> list[str]:
    """Every string part content, in order — what the model would read."""
    collected: list[str] = []
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                collected.append(content)
    return collected


def _constant_evaluator(decision: SourceAuthorityDecision) -> Any:
    """A current-source authority that answers ``decision`` for every stamp."""

    async def evaluate(
        stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
    ) -> SourceAuthorityDecision:
        return decision

    return evaluate


async def _deliver_history(
    runs: AgentRunStore,
    *,
    run_id: str,
    attempt_id: str,
    decision: SourceAuthorityDecision | None = None,
    evaluator: Any = None,
    trim: bool = False,
) -> tuple[list[ModelMessage], list[str]]:
    """Assemble history-carry and deliver it exactly as the worker's model send does.

    BOTH production passes are crossed, mirroring ``model.GatewayModel.request``:
    the provisional pre-trim :meth:`materialize_history` AND the pre-send
    :meth:`reauthorize_history` over the (possibly trimmed) survivors — the
    second authorization barrier is where the durable receipt truth lives.
    ``decision`` is what the CURRENT source authority answers for every stamp —
    ``AUTHORIZED`` is a still-permitted source, ``PERMANENT_WITHHELD`` a revoked
    one — or pass a stateful ``evaluator`` to drift between the passes.
    ``trim=True`` drops EVERY message between the passes, the way the context
    budget can. Returns the durable assembled history and the ephemerally
    delivered text parts of the final send.
    """
    if (decision is None) == (evaluator is None):
        raise ValueError("pass exactly one of decision/evaluator")
    evaluate = _constant_evaluator(decision) if decision is not None else evaluator
    history = await runs.materialize_conversation_history(
        run_id=run_id, owner_principal_id=OWNER.id
    )
    guard = ProtectedSourceResultGuard(RunStoreSourceBoundResultResolver(runs, evaluate))
    with (
        authenticated_request_scope(_authenticated_context()),
        model_call_scope(run_id=run_id, model_call_attempt_id=attempt_id),
        run_delivery_observation_scope(run_id=run_id, attempt_id=attempt_id),
    ):
        first_pass = await guard.materialize_history(history)
        survivors: list[ModelMessage] = [] if trim else list(first_pass.messages)
        delivered = await guard.reauthorize_history(survivors, first_pass)
    return history, _texts(list(delivered.messages))


def _history_receipts(engine: sa.Engine, run_id: str) -> list[tuple[str, str, str]]:
    """(delivery_kind, decision, protected_ref) for every receipt on ``run_id``."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                ne_agent_delivery_receipts.c.delivery_kind,
                ne_agent_delivery_receipts.c.decision,
                ne_agent_delivery_receipts.c.protected_ref,
            ).where(ne_agent_delivery_receipts.c.run_id == run_id)
        ).all()
    return [(str(kind), str(decision), str(ref)) for kind, decision, ref in rows]


def _container_ref(engine: sa.Engine, run_id: str) -> str:
    """The protected container id a finalized cited turn attached as history."""
    with engine.connect() as conn:
        return str(
            conn.execute(
                sa.select(ne_agent_run_history_attachments.c.protected_ref).where(
                    ne_agent_run_history_attachments.c.run_id == run_id
                )
            ).scalar_one()
        )


def test_second_and_third_turns_are_admitted_after_a_cited_answer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """THE two-turn regression, through the real chat route and real stores.

    Turn 1 completes as a cited (source-bound) answer — the production shape.
    Turn 2 (no model choice) must be admitted, not 409. Turn 3 swaps the model
    mid-Conversation (demo Beat 1b) over the mixed source-bound + source-free
    history and must also be admitted, with the prior turns carried.
    """
    # The shared doubles pin a 4096-token budget, below the 8192-token
    # continuity reserve every REAL admission carries; production agents run
    # far larger budgets, so give this scenario a realistic one.
    monkeypatch.setattr(doubles._Catalog.definition.budgets, "max_input_tokens", 200_000)
    monkeypatch.setattr(doubles._ModelSelection, "input_budget", lambda self, _model: 200_000)
    engine, sessions, runs = _real_stores(tmp_path)
    service = compose_default_ask_submission_service(
        run_submissions=_run_submissions(),
        transactions=runs,
        activation=_pin(),
    )
    assert service is not None
    enqueuer = _RunEnqueuer()
    state = _activated_route_state(service, cast(Any, sessions), enqueuer, runs)
    workflow_state = RunWorkflowStateStore(engine, run_store=runs)

    async def scenario() -> None:
        conversation = await sessions.create_conversation(AGENT_ID, _DEPARTMENT, OWNER.id)
        conversation_id = conversation.conversation_id

        first = await _send(
            state, conversation_id, ChatMessageRequest(prompt="how many fee postings are open?")
        )
        assert first["type"] == "accepted"
        await _finalize_turn(
            engine,
            runs,
            workflow_state,
            run_id=first["run_id"],
            attempt_id=first["attempt_id"],
            tag="one",
            output=b"There are 42 open fee postings.",
            cited=True,
        )
        # The finalized shape IS the production shape that used to poison the
        # Conversation: a stamped source-bound container, not source-free text.
        with engine.connect() as conn:
            attachment = conn.execute(
                sa.select(
                    ne_agent_run_history_attachments.c.content_status,
                    ne_agent_run_history_attachments.c.protected_domain,
                ).where(ne_agent_run_history_attachments.c.run_id == first["run_id"])
            ).one()
            assert tuple(attachment) == ("source_bound_container", "protected_source_output")

        # THE regression: the follow-up question in the SAME Conversation.
        second = await _send(state, conversation_id, ChatMessageRequest(prompt="and yesterday?"))
        assert second["type"] == "accepted"
        assert second["conversation_id"] == conversation_id
        assert second["run_id"] != first["run_id"]

        await _finalize_turn(
            engine,
            runs,
            workflow_state,
            run_id=second["run_id"],
            attempt_id=second["attempt_id"],
            tag="two",
            output=b"Yesterday there were 40.",
            cited=False,
        )

        # Beat 1b: an explicit mid-Conversation model swap over the mixed
        # source-bound + source-free history must also be admitted.
        third = await _send(
            state,
            conversation_id,
            ChatMessageRequest(prompt="summarize the trend", requested_model="frontier-default"),
        )
        assert third["type"] == "accepted"
        assert third["run_id"] not in {first["run_id"], second["run_id"]}
        with engine.connect() as conn:
            requested = conn.execute(
                sa.select(ne_agent_runs.c.requested_model).where(
                    ne_agent_runs.c.run_id == third["run_id"]
                )
            ).scalar_one()
            assert requested == "frontier-default"

        # History-carry for the swapped run: BOTH prior turns, in order. The
        # cited answer is NOT carried as its stale plaintext — the durable
        # assembly holds only its opaque protected reference; the plaintext is
        # reauthorized per model send under the current principal.
        history = await runs.materialize_conversation_history(
            run_id=third["run_id"], owner_principal_id=OWNER.id
        )
        durable = _texts(history)
        assert durable[0] == "how many fee postings are open?"
        assert durable[1].startswith(PROTECTED_REFERENCE_PREFIX)
        assert durable[2:] == ["and yesterday?", "Yesterday there were 40."]
        # Under a still-PRESENT source authority the model send materializes the
        # exact prior text ephemerally...
        _, delivered = await _deliver_history(
            runs,
            run_id=third["run_id"],
            attempt_id=third["attempt_id"],
            decision=SourceAuthorityDecision.AUTHORIZED,
        )
        assert delivered == [
            "how many fee postings are open?",
            "There are 42 open fee postings.",
            "and yesterday?",
            "Yesterday there were 40.",
        ]
        # ...and the delivery is RECEIPTED: turn 3's delivery set names the
        # cited container, so its answer is never misclassified source-free.
        assert (
            "history_output_served",
            "served",
            _container_ref(engine, first["run_id"]),
        ) in _history_receipts(engine, third["run_id"])
        assert len(enqueuer.requests) == 3

    asyncio.run(scenario())
    engine.dispose()


def test_revoked_source_authority_withholds_carried_history_and_claims_receipt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """SPEC "Conversation continuity" acceptance scenario, executed end to end.

    Turn 1: the principal asks; the agent reads a governed source under their
    ACL; the cited answer is stored as a stamped source-bound container. The
    principal's access to that source is then REVOKED (the current-authority
    evaluator answers ``PERMANENT_WITHHELD`` for its stamp). Turn 2 in the same
    Conversation must:

    (a) never carry the stale original answer text in durable history assembly,
    (b) inject the ONE fixed source-withheld assistant message into the model
        context — never the original bytes, and
    (c) claim a ``withheld`` delivery receipt naming the cited container, so
        turn 2's answer is attributed to the protected source rather than
        misclassified source-free.

    Red before this fix: history assembly returned the container's plaintext
    unconditionally (integrity-only read) and claimed no receipt.
    """
    monkeypatch.setattr(doubles._Catalog.definition.budgets, "max_input_tokens", 200_000)
    monkeypatch.setattr(doubles._ModelSelection, "input_budget", lambda self, _model: 200_000)
    engine, sessions, runs = _real_stores(tmp_path)
    service = compose_default_ask_submission_service(
        run_submissions=_run_submissions(),
        transactions=runs,
        activation=_pin(),
    )
    assert service is not None
    enqueuer = _RunEnqueuer()
    state = _activated_route_state(service, cast(Any, sessions), enqueuer, runs)
    workflow_state = RunWorkflowStateStore(engine, run_store=runs)
    cited_answer = "There are 42 open fee postings."

    async def scenario() -> None:
        conversation = await sessions.create_conversation(AGENT_ID, _DEPARTMENT, OWNER.id)
        conversation_id = conversation.conversation_id

        first = await _send(
            state, conversation_id, ChatMessageRequest(prompt="how many fee postings are open?")
        )
        assert first["type"] == "accepted"
        await _finalize_turn(
            engine,
            runs,
            workflow_state,
            run_id=first["run_id"],
            attempt_id=first["attempt_id"],
            tag="one",
            output=cited_answer.encode(),
            cited=True,
        )

        second = await _send(state, conversation_id, ChatMessageRequest(prompt="and yesterday?"))
        assert second["type"] == "accepted"

        # THE REVOCATION, between turn 1 and turn 2's model send: the current
        # source authority no longer permits the principal to read the source
        # the turn-1 answer was derived from.
        history, delivered = await _deliver_history(
            runs,
            run_id=second["run_id"],
            attempt_id=second["attempt_id"],
            decision=SourceAuthorityDecision.PERMANENT_WITHHELD,
        )

        # (a) The durable carry never holds the stale answer's plaintext.
        assert all(cited_answer not in text for text in _texts(history))
        # (b) The model context gets the ONE fixed source-withheld message —
        # never the original TextPart, even though it was authorized when
        # produced.
        assert delivered == ["how many fee postings are open?", SOURCE_WITHHELD_TEXT]
        assert all(cited_answer not in text for text in delivered)
        # (c) The withheld decision is receipted on turn 2, naming the cited
        # container: the source appears in turn 2's audit chain.
        assert (
            "withheld",
            "withheld",
            _container_ref(engine, first["run_id"]),
        ) in _history_receipts(engine, second["run_id"])

    asyncio.run(scenario())
    engine.dispose()


def _conversation_fixture(
    tmp_path: Path, monkeypatch: Any
) -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore, Any, RunWorkflowStateStore]:
    """The real-store canonical-Ask rig every governance scenario below shares."""
    monkeypatch.setattr(doubles._Catalog.definition.budgets, "max_input_tokens", 200_000)
    monkeypatch.setattr(doubles._ModelSelection, "input_budget", lambda self, _model: 200_000)
    engine, sessions, runs = _real_stores(tmp_path)
    service = compose_default_ask_submission_service(
        run_submissions=_run_submissions(),
        transactions=runs,
        activation=_pin(),
    )
    assert service is not None
    state = _activated_route_state(service, cast(Any, sessions), _RunEnqueuer(), runs)
    return engine, sessions, runs, state, RunWorkflowStateStore(engine, run_store=runs)


async def _cited_then_followup(
    engine: sa.Engine,
    sessions: SqlAlchemySessionStore,
    runs: AgentRunStore,
    state: Any,
    workflow_state: RunWorkflowStateStore,
    *,
    cited_answer: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn 1 finalized as a cited source-bound answer; turn 2 admitted."""
    conversation = await sessions.create_conversation(AGENT_ID, _DEPARTMENT, OWNER.id)
    first = await _send(
        state,
        conversation.conversation_id,
        ChatMessageRequest(prompt="how many fee postings are open?"),
    )
    assert first["type"] == "accepted"
    await _finalize_turn(
        engine,
        runs,
        workflow_state,
        run_id=first["run_id"],
        attempt_id=first["attempt_id"],
        tag="one",
        output=cited_answer.encode(),
        cited=True,
    )
    second = await _send(
        state, conversation.conversation_id, ChatMessageRequest(prompt="and yesterday?")
    )
    assert second["type"] == "accepted"
    return first, second


def test_trimmed_history_is_never_receipted_as_served(tmp_path: Path, monkeypatch: Any) -> None:
    """A receipt describes the FINAL send, never the provisional pre-trim pass.

    Production materializes protected history BEFORE trimming (for an honest
    byte estimate) and reauthorizes the survivors immediately before the send.
    When the trim drops every message, nothing was model-visible — so nothing
    may be audited as served. Red before this fix: the first pass claimed a
    ``history_output_served``/served receipt for content the model never saw.
    """
    engine, sessions, runs, state, workflow_state = _conversation_fixture(tmp_path, monkeypatch)

    async def scenario() -> None:
        _first, second = await _cited_then_followup(
            engine,
            sessions,
            runs,
            state,
            workflow_state,
            cited_answer="There are 42 open fee postings.",
        )
        _, delivered = await _deliver_history(
            runs,
            run_id=second["run_id"],
            attempt_id=second["attempt_id"],
            decision=SourceAuthorityDecision.AUTHORIZED,
            trim=True,
        )
        assert delivered == []
        assert _history_receipts(engine, second["run_id"]) == []

    asyncio.run(scenario())
    engine.dispose()


def test_authority_drift_between_the_two_passes_withholds_instead_of_raising(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Revocation BETWEEN the pre-trim pass and the pre-send pass must withhold.

    The current-source authority answers ``AUTHORIZED`` on the provisional
    first pass and ``PERMANENT_WITHHELD`` on the pre-send reauthorization —
    exactly the drift window the second barrier exists for. The send must
    substitute the fixed source-withheld message and claim ONE ``withheld``
    receipt. Red before this fix: the first pass had already committed a
    ``served`` receipt, so the second pass raised ``ValueError: delivery
    address was replayed under a different decided intent`` and the stale
    ``served`` receipt remained — a crash instead of a withholding, and a
    false audit row underneath it.
    """
    engine, sessions, runs, state, workflow_state = _conversation_fixture(tmp_path, monkeypatch)
    cited_answer = "There are 42 open fee postings."

    async def scenario() -> None:
        first, second = await _cited_then_followup(
            engine, sessions, runs, state, workflow_state, cited_answer=cited_answer
        )
        evaluations = {"count": 0}

        async def drifting(
            stamp: SourceAuthorityStamp, context: AuthenticatedRequestContext
        ) -> SourceAuthorityDecision:
            evaluations["count"] += 1
            if evaluations["count"] == 1:
                return SourceAuthorityDecision.AUTHORIZED
            return SourceAuthorityDecision.PERMANENT_WITHHELD

        _, delivered = await _deliver_history(
            runs,
            run_id=second["run_id"],
            attempt_id=second["attempt_id"],
            evaluator=drifting,
        )
        # Both passes really ran the evaluator (the memo is per pass).
        assert evaluations["count"] == 2
        # The send withholds — fixed message, never the stale answer.
        assert delivered == ["how many fee postings are open?", SOURCE_WITHHELD_TEXT]
        assert all(cited_answer not in text for text in delivered)
        # Exactly one receipt: the FINAL decision. No served row anywhere.
        assert _history_receipts(engine, second["run_id"]) == [
            ("withheld", "withheld", _container_ref(engine, first["run_id"]))
        ]

    asyncio.run(scenario())
    engine.dispose()


def test_history_container_refuses_foreign_attempts_and_wrong_resolver_targets(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The carried container authorizes ONLY for the Run's own model send.

    Two refusals, both red before this fix: (a) ``for_model_send`` under an
    attempt id the current Run never linked authorized anyway — so a receipt
    could bind to a fabricated attempt; (b) ``for_workflow_projection`` — the
    wrong resolver target entirely — authorized the Conversation container as
    if it were workflow state. Both must refuse without material; the genuine
    run/attempt pair is the positive control.
    """
    engine, sessions, runs, state, workflow_state = _conversation_fixture(tmp_path, monkeypatch)

    async def scenario() -> None:
        _first, second = await _cited_then_followup(
            engine,
            sessions,
            runs,
            state,
            workflow_state,
            cited_answer="There are 42 open fee postings.",
        )
        history = await runs.materialize_conversation_history(
            run_id=second["run_id"], owner_principal_id=OWNER.id
        )
        reference = decode_protected_reference(_texts(history)[1])
        assert isinstance(reference, ProtectedSourceOutputRef)
        resolver = RunStoreSourceBoundResultResolver(
            runs, _constant_evaluator(SourceAuthorityDecision.AUTHORIZED)
        )
        context = _authenticated_context()
        with authenticated_request_scope(context):
            foreign_attempt = await resolver.for_model_send(
                reference,
                principal_id=OWNER.id,
                access_context_hash=context.context_hash,
                run_id=second["run_id"],
                model_call_attempt_id="not-the-current-runs-attempt",
            )
            wrong_target = await resolver.for_workflow_projection(
                reference,
                principal_id=OWNER.id,
                access_context_hash=context.context_hash,
                workflow_execution_id=second["run_id"],
                projection_attempt_id="not-a-workflow-projection-attempt",
            )
            bound = await resolver.for_model_send(
                reference,
                principal_id=OWNER.id,
                access_context_hash=context.context_hash,
                run_id=second["run_id"],
                model_call_attempt_id=second["attempt_id"],
            )
        assert foreign_attempt.decision is not SourceAuthorityDecision.AUTHORIZED
        assert foreign_attempt.materialized_text is None
        assert wrong_target.decision is not SourceAuthorityDecision.AUTHORIZED
        assert wrong_target.materialized_text is None
        # Positive control: the Run's own linked attempt still authorizes, so
        # the refusals above are the bindings and not a loader that refuses all.
        assert bound.decision is SourceAuthorityDecision.AUTHORIZED
        assert bound.materialized_text == "There are 42 open fee postings."

    asyncio.run(scenario())
    engine.dispose()
