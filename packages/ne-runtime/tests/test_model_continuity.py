# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-014C atomic between-Run context continuity and restart proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.contracts.identity import AuthenticatedRequestContext
from ne.runtime.model_continuity import (
    ne_agent_model_boundary_receipts,
    ne_agent_run_model_steps,
)
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
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
    RunSubmissionConflictError,
    ensure_run_schema,
    ne_agent_run_events,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.run_workflow import RunWorkflowStateStore, ensure_run_workflow_schema
from ne.runtime.source_results import (
    complete_run_delivery_observation,
    model_call_scope,
    run_delivery_observation_scope,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

_AGENT = "agt_01k3meridiananalyst000000000"
_POLICY = "agent.meridian-analyst.v2"
_OWNER = "user:continuity"
_DEPARTMENT = "operations"
_CONFIG_HASH = "1" * 64
_DEFINITION_HASH = "2" * 64
_CAPABILITY_HASH = "3" * 64
_AUTH_HASH_A = "a" * 64
_AUTH_HASH_B = "b" * 64


def _cipher() -> RunPrivatePayloadCipher:
    return RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="continuity-k1", keys={"continuity-k1": b"k" * 32})
    )


def _start(
    *,
    conversation_id: str,
    expected_version: int,
    tag: str,
    model: str,
    lane: str,
    authorization_hash: str,
    prompt: bytes,
    target_input_budget: int = 16_384,
) -> SimpleNamespace:
    return SimpleNamespace(
        idempotency_key=f"continuity-{tag}",
        request_hash=hashlib.sha256(f"continuity-{tag}".encode()).hexdigest(),
        conversation_id=conversation_id,
        expected_version=expected_version,
        owner_principal_id=_OWNER,
        department=_DEPARTMENT,
        conversation_agent_name=_AGENT,
        agent_id=_AGENT,
        agent_version=2,
        strategy="agentic",
        config_hash=_CONFIG_HASH,
        capability_snapshot_hash=_CAPABILITY_HASH,
        capability_snapshot_receipt_id=f"capability-{tag}",
        agent_definition_hash=_DEFINITION_HASH,
        model_policy_id=_POLICY,
        authorized_model=model,
        authorized_model_lane=lane,
        model_authorization_receipt_hash=authorization_hash,
        target_input_budget=target_input_budget,
        input_plaintext=prompt,
        requested_model=model,
        requested_model_lane=lane,
        authenticated_context=AuthenticatedRequestContext(
            principal_id=_OWNER,
            authorization_groups=(),
            department=_DEPARTMENT,
            issuer="https://issuer.example.test",
            expires_at=4_102_444_800,
        ),
    )


async def _finalize_source_free(
    engine: sa.Engine,
    runs: AgentRunStore,
    state: RunWorkflowStateStore,
    *,
    admission: Any,
    conversation_id: str,
    tag: str,
    output: bytes,
) -> None:
    run_id = str(admission.run_id)
    attempt_id = str(admission.attempt_id)
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
        owner_principal_id=_OWNER,
        config_hash=_CONFIG_HASH,
    )
    await runs.record_audit_receipt(
        run_id=run_id,
        attempt_id=attempt_id,
        run_event_id=claim.claim_event_id,
        decision_kind=RunAuditDecisionKind.STATE_TRANSITION,
        audit_event_id=f"audit-{tag}",
        audit_sequence=1,
        audit_record_hash=hashlib.sha256(f"audit-{tag}".encode()).hexdigest(),
        config_hash=_CONFIG_HASH,
        owner_principal_id=_OWNER,
        step_id="run-execution",
    )
    with (
        run_delivery_observation_scope(run_id=run_id, attempt_id=attempt_id) as observer,
        model_call_scope(run_id=run_id, model_call_attempt_id=attempt_id),
    ):
        complete_run_delivery_observation()
        delivery = observer.snapshot()
    finalizer = RunFinalizer(runs)
    observations = await finalizer.observe(target=target, delivery=delivery)
    output_hash = hashlib.sha256(output).hexdigest()
    envelope = StructuredRunOutputEnvelope(
        discriminator="structured_run_output",
        served_output_hash=output_hash,
        claim_count=0,
        claim_set_hash=claim_set_hash(output_hash, ()),
        claims=(),
    )
    await finalizer.finalize(
        RunTerminalCandidate(
            observations=observations,
            envelope=envelope,
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


def _history_text(history: Sequence[object]) -> tuple[str, ...]:
    text: list[str] = []
    for message in history:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    assert isinstance(part.content, str)
                    text.append(part.content)
        elif isinstance(message, ModelResponse):
            text.extend(part.content for part in message.parts if isinstance(part, TextPart))
    return tuple(text)


def test_model_step_admits_only_declared_names_as_the_route_it_fell_back_from(
    tmp_path: Path,
) -> None:
    """C12 -- the routing pair is names and positions, and the writer is the fence.

    ``fallback_from`` is projected all the way to the console, so it is the one
    routing field an attacker-influenced string could ride. A provider's error
    text can echo prompt substrings back and a candidate's ``api_base`` can
    carry a credential; neither is a model name, and neither gets in here.
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'route.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    sessions = SqlAlchemySessionStore(engine)

    async def scenario() -> None:
        conversation = await sessions.create_conversation(_AGENT, _DEPARTMENT, _OWNER)
        store = AgentRunStore(engine, _cipher())
        admitted = await store.admit_start(
            _start(
                conversation_id=conversation.conversation_id,
                expected_version=0,
                tag="route",
                model="private-default",
                lane="private-vllm",
                authorization_hash=_AUTH_HASH_A,
                prompt=b"Which model served this, and why that one?",
            )
        )

        async def record(route_position: str, fallback_from: str | None) -> None:
            await store.record_model_step(
                run_id=admitted.run_id,
                attempt_id=admitted.attempt_id,
                ordinal=0,
                model_policy_id=_POLICY,
                request_mode="explicit",
                requested_model="private-default",
                served_model="private-default",
                served_lane="private-vllm",
                route_position=route_position,  # type: ignore[arg-type]
                fallback_from=fallback_from,
                authorization_receipt_hash=_AUTH_HASH_A,
            )

        # The exact provider failure that made this fact necessary. It is a
        # sentence, not a model name, and it never becomes a row.
        with pytest.raises(ValueError, match="declared model names"):
            await record("fallback", "litellm.BadRequestError: invalid tool_call_id")
        # A candidate's api_base is not a model name either.
        with pytest.raises(ValueError, match="declared model names"):
            await record("fallback", "https://api.anthropic.com/v1?key=SECRET")
        # The position and the names are ONE fact and cannot disagree.
        with pytest.raises(ValueError, match="disagree"):
            await record("primary", "frontier-sonnet")
        with pytest.raises(ValueError, match="disagree"):
            await record("fallback", None)
        # There is no third position and there is no absent one.
        with pytest.raises(ValueError, match="route position is invalid"):
            await record("unavailable", None)

        # Nothing above was persisted: the fence is at the writer, so a rejected
        # routing claim leaves no step and no `model.served` behind.
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_run_model_steps)
                ).scalar_one()
                == 0
            )

        await record("fallback", "frontier-sonnet,frontier-haiku")
        with engine.connect() as conn:
            step = conn.execute(sa.select(ne_agent_run_model_steps)).one()
        assert (step.route_position, step.fallback_from) == (
            "fallback",
            "frontier-sonnet,frontier-haiku",
        )
        event = await _model_served_event(engine, admitted.run_id)
        assert event["route_position"] == "fallback"
        assert event["fallback_from"] == "frontier-sonnet,frontier-haiku"

    asyncio.run(scenario())
    engine.dispose()


async def _model_served_event(engine: sa.Engine, run_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(ne_agent_run_events.c.safe_detail).where(
                ne_agent_run_events.c.run_id == run_id,
                ne_agent_run_events.c.event_kind == "model.served",
            )
        ).one()
    detail = row.safe_detail
    return json.loads(detail) if isinstance(detail, str) else dict(detail)


def test_atomic_switch_replays_across_restart_and_preserves_exact_history(
    tmp_path: Path,
) -> None:
    sentinel = "RUN-A-RANDOM-SENTINEL-7f469cf4"
    database = tmp_path / "model-continuity.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    sessions = SqlAlchemySessionStore(engine)

    async def scenario() -> None:
        conversation = await sessions.create_conversation(_AGENT, _DEPARTMENT, _OWNER)
        runs_a = AgentRunStore(engine, _cipher())
        command_a = _start(
            conversation_id=conversation.conversation_id,
            expected_version=0,
            tag="a",
            model="private-default",
            lane="private-vllm",
            authorization_hash=_AUTH_HASH_A,
            prompt=b"Remember the exact sentinel for the next Run.",
        )
        admitted_a = await runs_a.admit_start(command_a)
        await runs_a.record_model_step(
            run_id=admitted_a.run_id,
            attempt_id=admitted_a.attempt_id,
            ordinal=0,
            model_policy_id=_POLICY,
            request_mode="explicit",
            requested_model="private-default",
            served_model="private-default",
            served_lane="private-vllm",
            route_position="primary",
            fallback_from=None,
            authorization_receipt_hash=_AUTH_HASH_A,
        )
        state = RunWorkflowStateStore(engine, run_store=runs_a)
        await _finalize_source_free(
            engine,
            runs_a,
            state,
            admission=admitted_a,
            conversation_id=conversation.conversation_id,
            tag="a",
            output=sentinel.encode(),
        )

        # A too-small target cannot silently compact or truncate; the whole Run
        # admission, Conversation sequence and boundary receipt roll back.
        with pytest.raises(
            RunSubmissionConflictError,
            match="unavailable governed compaction",
        ):
            await runs_a.admit_start(
                _start(
                    conversation_id=conversation.conversation_id,
                    expected_version=1,
                    tag="fit-refusal",
                    model="frontier-default",
                    lane="frontier-api",
                    authorization_hash=_AUTH_HASH_B,
                    prompt=b"This must not be admitted.",
                    target_input_budget=1,
                )
            )

        # Recreate the store/cipher to prove no process-local history participates.
        runs_b = AgentRunStore(engine, _cipher())
        command_b = _start(
            conversation_id=conversation.conversation_id,
            expected_version=1,
            tag="b",
            model="frontier-default",
            lane="frontier-api",
            authorization_hash=_AUTH_HASH_B,
            prompt=b"What sentinel did Run A preserve?",
        )
        admitted_b = await runs_b.admit_start(command_b)
        replay_b = await runs_b.admit_start(command_b)
        assert replay_b.run_id == admitted_b.run_id
        assert replay_b.replayed is True
        history = await runs_b.materialize_conversation_history(
            run_id=admitted_b.run_id,
            owner_principal_id=_OWNER,
        )
        assert _history_text(history) == (
            "Remember the exact sentinel for the next Run.",
            sentinel,
        )
        run_b = await runs_b.get_run(run_id=admitted_b.run_id, owner_principal_id=_OWNER)
        assert run_b is not None and run_b.context_snapshot_hash is not None
        await runs_b.verify_model_boundary(
            run_id=admitted_b.run_id,
            owner_principal_id=_OWNER,
            agent_definition_hash=_DEFINITION_HASH,
            model_policy_id=_POLICY,
            authorization_receipt_hash=_AUTH_HASH_B,
            context_snapshot_hash=run_b.context_snapshot_hash,
        )
        with pytest.raises(RuntimeError, match="receipt is unavailable or drifted"):
            await runs_b.verify_model_boundary(
                run_id=admitted_b.run_id,
                owner_principal_id=_OWNER,
                agent_definition_hash=_DEFINITION_HASH,
                model_policy_id=_POLICY,
                authorization_receipt_hash=_AUTH_HASH_B,
                context_snapshot_hash="f" * 64,
            )

        with engine.connect() as conn:
            receipt_b = conn.execute(
                sa.select(ne_agent_model_boundary_receipts).where(
                    ne_agent_model_boundary_receipts.c.run_id == admitted_b.run_id
                )
            ).one()
            assert receipt_b.previous_run_id == admitted_a.run_id
            assert receipt_b.conversation_id == conversation.conversation_id
            assert receipt_b.before_served_model == "private-default"
            assert receipt_b.after_authorized_model == "frontier-default"
            assert receipt_b.context_fit == "fits"
            assert receipt_b.estimated_input_tokens < receipt_b.target_input_budget
            assert receipt_b.compaction == "not_required"
            assert receipt_b.cache_impact == "reset_expected"
            changed = conn.execute(
                sa.select(ne_agent_run_events).where(
                    ne_agent_run_events.c.run_id == admitted_b.run_id,
                    ne_agent_run_events.c.event_kind == "model.changed",
                )
            ).one()
            clear = json.dumps(
                {"receipt": dict(receipt_b._mapping), "event": dict(changed._mapping)},
                default=str,
            )
            assert sentinel not in clear

        # Round 7 (SPEC PRE-DEMO-014 snapshot gate): the selection surfaces read
        # the newest boundary receipt's estimate as the conversation's current
        # snapshot size — the same durable counts admission itself froze, never
        # a second estimator. An unknown conversation answers None (no fit
        # data), never a fabricated zero.
        assert (
            await runs_b.latest_context_snapshot_estimate(
                conversation_id=conversation.conversation_id,
                owner_principal_id=_OWNER,
            )
            == receipt_b.estimated_input_tokens
        )
        assert (
            await runs_b.latest_context_snapshot_estimate(
                conversation_id="conv-that-never-ran",
                owner_principal_id=_OWNER,
            )
            is None
        )

        # Round 9 (SPEC PRE-DEMO-014 on the repoint surface): the routing gate
        # reads the LARGEST live conversation snapshot for a task — here one
        # conversation, whose NEWEST receipt is B's — from the same durable
        # counts, through the real admit flow. An unrouted policy answers None
        # (no receipt ⇒ nothing to fit), never a fabricated zero.
        assert (
            await runs_b.largest_live_context_snapshot_estimate(_POLICY)
            == receipt_b.estimated_input_tokens
        )
        assert runs_b.largest_live_context_snapshot_estimate_sync(_POLICY) == int(
            receipt_b.estimated_input_tokens
        )
        assert await runs_b.largest_live_context_snapshot_estimate("policy-that-never-ran") is None

        # A queued/running B cannot be relabelled by admitting C at a fake boundary.
        with pytest.raises(RunSubmissionConflictError, match="no safe between-Run boundary"):
            await runs_b.admit_start(
                _start(
                    conversation_id=conversation.conversation_id,
                    expected_version=2,
                    tag="active-relabel",
                    model="private-default",
                    lane="private-vllm",
                    authorization_hash=_AUTH_HASH_A,
                    prompt=b"Must not relabel active B.",
                )
            )
        with engine.connect() as conn:
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(ne_agent_runs)).scalar_one()
                == 2
            )

    asyncio.run(scenario())
    engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("NE_TEST_POSTGRES_URL"),
    reason="NE_TEST_POSTGRES_URL is required for the PRE-014C Postgres proof",
)
def test_postgres_switch_receipt_and_exact_history_survive_store_restart() -> None:
    url = os.environ["NE_TEST_POSTGRES_URL"]
    schema = f"ne_model_continuity_{uuid.uuid4().hex}"
    admin = sa.create_engine(url)
    with admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = sa.create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    ensure_run_workflow_schema(engine)
    sentinel = f"RUN-A-PG-SENTINEL-{uuid.uuid4().hex}"

    async def scenario() -> None:
        sessions = SqlAlchemySessionStore(engine)
        conversation = await sessions.create_conversation(_AGENT, _DEPARTMENT, _OWNER)
        first_store = AgentRunStore(engine, _cipher())
        admitted_a = await first_store.admit_start(
            _start(
                conversation_id=conversation.conversation_id,
                expected_version=0,
                tag="pg-a",
                model="private-default",
                lane="private-vllm",
                authorization_hash=_AUTH_HASH_A,
                prompt=b"Preserve the Postgres sentinel.",
            )
        )
        await first_store.record_model_step(
            run_id=admitted_a.run_id,
            attempt_id=admitted_a.attempt_id,
            ordinal=0,
            model_policy_id=_POLICY,
            request_mode="explicit",
            requested_model="private-default",
            served_model="private-default",
            served_lane="private-vllm",
            route_position="primary",
            fallback_from=None,
            authorization_receipt_hash=_AUTH_HASH_A,
        )
        await _finalize_source_free(
            engine,
            first_store,
            RunWorkflowStateStore(engine, run_store=first_store),
            admission=admitted_a,
            conversation_id=conversation.conversation_id,
            tag="a",
            output=sentinel.encode(),
        )

        restarted_store = AgentRunStore(engine, _cipher())
        admitted_b = await restarted_store.admit_start(
            _start(
                conversation_id=conversation.conversation_id,
                expected_version=1,
                tag="pg-b",
                model="frontier-default",
                lane="frontier-api",
                authorization_hash=_AUTH_HASH_B,
                prompt=b"Recall the Postgres sentinel.",
            )
        )
        history = await restarted_store.materialize_conversation_history(
            run_id=admitted_b.run_id,
            owner_principal_id=_OWNER,
        )
        assert sentinel in _history_text(history)
        with engine.connect() as conn:
            receipt = conn.execute(
                sa.select(ne_agent_model_boundary_receipts).where(
                    ne_agent_model_boundary_receipts.c.run_id == admitted_b.run_id
                )
            ).one()
            assert receipt.previous_run_id == admitted_a.run_id
            assert receipt.context_fit == "fits"

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def test_largest_live_snapshot_is_the_newest_receipt_per_conversation_maxed_across(
    tmp_path: Path,
) -> None:
    """NE-CTX-IN-FORCE round 9 — the exact aggregate the repoint gate reads.

    The rule, proven against rows whose geometry can tell its failure modes
    apart: per conversation only the NEWEST receipt counts (conversation one
    carries an OLDER, LARGER receipt from before its wide-model turn was
    superseded — a max-over-history would answer 900), and the answer is the
    MAX across conversations' newest receipts (700, conversation two), filtered
    to the one model policy (any other policy answers None — no receipt means
    nothing to fit, never a fabricated zero).
    """
    from datetime import UTC, datetime, timedelta

    from ne.runtime.model_continuity import ne_agent_model_boundary_receipts
    from ne.runtime.store import ne_agent_conversations

    engine = sa.create_engine(f"sqlite:///{tmp_path}/largest-live.db")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    now = datetime.now(UTC)

    def _conversation(conn: sa.Connection, conversation_id: str) -> None:
        conn.execute(
            ne_agent_conversations.insert().values(
                conversation_id=conversation_id,
                owner_principal_id=_OWNER,
                agent_name=_AGENT,
                department=_DEPARTMENT,
                created_at=now,
                updated_at=now,
                last_sequence=1,
            )
        )

    def _run(conn: sa.Connection, run_id: str, conversation_id: str, sequence: int) -> None:
        conn.execute(
            ne_agent_runs.insert().values(
                run_id=run_id,
                conversation_id=conversation_id,
                owner_principal_id=_OWNER,
                department=_DEPARTMENT,
                conversation_agent_name=_AGENT,
                agent_id=_AGENT,
                agent_version=2,
                strategy="agentic",
                config_hash=_CONFIG_HASH,
                capability_snapshot_hash=_CAPABILITY_HASH,
                capability_snapshot_receipt_id=f"capability-{run_id}",
                agent_definition_hash=_DEFINITION_HASH,
                model_policy_id=_POLICY,
                authorized_model="private-default",
                authorized_model_lane="private-vllm",
                model_authorization_receipt_hash=_AUTH_HASH_A,
                context_snapshot_hash="4" * 64,
                sequence=sequence,
                display_label=f"Run {sequence}",
                status="completed",
                version=1,
                created_at=now,
                updated_at=now,
                terminal_at=now,
            )
        )

    def _receipt(
        conn: sa.Connection,
        run_id: str,
        conversation_id: str,
        *,
        estimated: int,
        policy: str,
        age: timedelta,
    ) -> None:
        conn.execute(
            ne_agent_model_boundary_receipts.insert().values(
                receipt_id=f"receipt-{run_id}",
                run_id=run_id,
                previous_run_id=None,
                conversation_id=conversation_id,
                owner_principal_id=_OWNER,
                department=_DEPARTMENT,
                agent_id=_AGENT,
                agent_version=2,
                agent_definition_hash=_DEFINITION_HASH,
                model_policy_id=policy,
                after_authorized_model="private-default",
                after_authorized_lane="private-vllm",
                context_snapshot_hash="4" * 64,
                context_fit="fits",
                estimated_input_tokens=estimated,
                target_input_budget=16_384,
                compaction="not_required",
                cache_impact="unknown",
                authorization_receipt_hash=_AUTH_HASH_A,
                config_hash=_CONFIG_HASH,
                created_at=now - age,
            )
        )

    with engine.begin() as conn:
        _conversation(conn, "conv-superseded")
        _conversation(conn, "conv-largest")
        _run(conn, "run-old-wide", "conv-superseded", 1)
        _run(conn, "run-new-narrow", "conv-superseded", 2)
        _run(conn, "run-largest", "conv-largest", 1)
        # conv-superseded: the OLD receipt is the biggest number in the table
        # (900) but is not that conversation's snapshot any more; the NEWEST
        # says 500.
        _receipt(
            conn,
            "run-old-wide",
            "conv-superseded",
            estimated=900,
            policy=_POLICY,
            age=timedelta(minutes=10),
        )
        _receipt(
            conn,
            "run-new-narrow",
            "conv-superseded",
            estimated=500,
            policy=_POLICY,
            age=timedelta(minutes=1),
        )
        # conv-largest: one receipt, 700 — the honest largest LIVE snapshot.
        _receipt(
            conn,
            "run-largest",
            "conv-largest",
            estimated=700,
            policy=_POLICY,
            age=timedelta(minutes=2),
        )

    runs = AgentRunStore(engine, _cipher())
    assert runs.largest_live_context_snapshot_estimate_sync(_POLICY) == 700
    assert runs.largest_live_context_snapshot_estimate_sync("some-other-policy") is None
    engine.dispose()
