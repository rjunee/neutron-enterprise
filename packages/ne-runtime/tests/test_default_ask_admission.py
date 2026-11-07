# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from ne.contracts.identity import AuthenticatedRequestContext
from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import (
    AgentRunStore,
    ensure_run_schema,
    ne_agent_run_enqueue_outbox,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
    ne_agent_conversations,
)


def _command(conversation_id: str) -> SimpleNamespace:
    start = SimpleNamespace(
        idempotency_key="ask-once",
        request_hash=hashlib.sha256(b"ask-once").hexdigest(),
        conversation_id=conversation_id,
        expected_version=0,
        owner_principal_id="alice",
        department="claims",
        conversation_agent_name="assistant",
        agent_id="agt_default",
        agent_version=1,
        strategy="agentic",
        config_hash="1" * 64,
        capability_snapshot_hash="2" * 64,
        capability_snapshot_receipt_id="capability-receipt",
        input_plaintext=b"private ask",
        authenticated_context=AuthenticatedRequestContext(
            principal_id="alice",
            authorization_groups=(),
            department="claims",
            issuer="https://issuer.example.test",
            expires_at=4_102_444_800,
        ),
    )
    return SimpleNamespace(
        start=start,
        activation_wire_generation="default-ask/canonical-run/v1",
        producer_snapshot_hash="2" * 64,
        turn_id="turn-1",
        attachment_id="turn-1",
        trace_id="trace-1",
    )


def test_default_ask_is_atomic_exactly_replayed_and_excludes_legacy(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'default-ask.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    conversation = asyncio.run(sessions.create_conversation("assistant", "claims", "alice"))
    store = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    asyncio.run(store.activate_default_ask("default-ask/canonical-run/v1"))
    first = asyncio.run(store.admit_default_ask(_command(conversation.conversation_id)))
    replay = asyncio.run(store.admit_default_ask(_command(conversation.conversation_id)))
    assert replay.submission.replayed is True
    assert replay.submission.run_id == first.submission.run_id
    assert replay.submission.attempt_id == first.submission.attempt_id
    assert replay.submission.enqueue_id == first.submission.enqueue_id
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_runs)) == 1
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_run_turn_targets)) == 1
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_run_enqueue_outbox)) == 1
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_conversation_turns)) == 0
        assert conn.scalar(sa.select(ne_agent_conversations.c.last_sequence)) == 1


def test_database_activation_fence_rejects_legacy_attachment_writer(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fence.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    conversation = asyncio.run(sessions.create_conversation("assistant", "claims", "alice"))
    store = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    asyncio.run(store.activate_default_ask("default-ask/canonical-run/v1"))
    with pytest.raises(Exception, match="legacy Conversation attachment writer is retired"):
        asyncio.run(
            sessions.claim_conversation_attachment(
                conversation.conversation_id,
                "alice",
                "claims",
                "assistant",
                "turn-legacy",
                "a" * 64,
                "trace-legacy",
            )
        )


def test_default_ask_forced_failure_rolls_back_every_row(tmp_path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    conversation = asyncio.run(sessions.create_conversation("assistant", "claims", "alice"))
    store = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
        ),
    )
    asyncio.run(store.activate_default_ask("default-ask/canonical-run/v1"))
    command = _command(conversation.conversation_id)
    command.start.expected_version = 99
    with pytest.raises(Exception, match="stale Conversation version"):
        asyncio.run(store.admit_default_ask(command))
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_runs)) == 0
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_run_turn_targets)) == 0
        assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_run_enqueue_outbox)) == 0
        assert conn.scalar(sa.select(ne_agent_conversations.c.last_sequence)) == 0
