# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import (
    AgentRunStore,
    ensure_run_schema,
    ne_agent_run_turn_targets,
    ne_agent_runs,
)
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_conversation_turns,
    ne_agent_conversations,
)

_URL = os.environ.get("NE_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not _URL, reason="NE_TEST_POSTGRES_URL required")


def _command(conversation_id: str, *, expected_version: int = 0) -> SimpleNamespace:
    start = SimpleNamespace(
        idempotency_key="postgres-ask-once",
        request_hash=hashlib.sha256(b"postgres-ask-once").hexdigest(),
        conversation_id=conversation_id,
        expected_version=expected_version,
        owner_principal_id="alice",
        department="claims",
        conversation_agent_name="assistant",
        agent_id="agt_default",
        agent_version=1,
        strategy="agentic",
        config_hash="1" * 64,
        capability_snapshot_hash="2" * 64,
        capability_snapshot_receipt_id="capability-receipt",
        input_plaintext=b"private postgres ask",
    )
    return SimpleNamespace(
        start=start,
        activation_wire_generation="default-ask/canonical-run/v1",
        producer_snapshot_hash="2" * 64,
        turn_id="turn-postgres",
        attachment_id="turn-postgres",
        trace_id="trace-postgres",
    )


def test_default_ask_postgres_concurrency_rollback_and_legacy_fence() -> None:
    assert _URL is not None
    engine = sa.create_engine(_URL, pool_size=12, max_overflow=12)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    sessions = SqlAlchemySessionStore(engine)
    conversation = asyncio.run(sessions.create_conversation("assistant", "claims", "alice"))
    store = AgentRunStore(
        engine,
        RunPrivatePayloadCipher(
            StaticPrivatePayloadKeys(current_key_id="pg-k1", keys={"pg-k1": b"p" * 32})
        ),
    )
    asyncio.run(store.activate_default_ask("default-ask/canonical-run/v1"))

    def admit() -> tuple[str, str, str]:
        result = asyncio.run(store.admit_default_ask(_command(conversation.conversation_id)))
        return result.submission.run_id, result.submission.attempt_id, result.submission.enqueue_id

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            identities = list(pool.map(lambda _: admit(), range(8)))
        assert len(set(identities)) == 1
        with engine.connect() as conn:
            assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_runs)) == 1
            assert (
                conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_run_turn_targets)) == 1
            )
            assert conn.scalar(sa.select(ne_agent_conversations.c.last_sequence)) == 1
            assert (
                conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_conversation_turns))
                == 0
            )
        changed = _command(conversation.conversation_id)
        changed.start.request_hash = "e" * 64
        with pytest.raises(Exception, match="idempotency key changed request"):
            asyncio.run(store.admit_default_ask(changed))
        with pytest.raises(Exception, match="legacy Conversation attachment writer is retired"):
            asyncio.run(
                sessions.claim_conversation_attachment(
                    conversation.conversation_id,
                    "alice",
                    "claims",
                    "assistant",
                    "legacy-race",
                    "a" * 64,
                    "trace-legacy",
                )
            )
        bad = _command(conversation.conversation_id, expected_version=99)
        bad.start.idempotency_key = "forced-rollback"
        bad.start.request_hash = "f" * 64
        bad.turn_id = "turn-forced-rollback"
        bad.attachment_id = "turn-forced-rollback"
        with pytest.raises(Exception, match="stale Conversation version"):
            asyncio.run(store.admit_default_ask(bad))
        with engine.connect() as conn:
            assert conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_runs)) == 1
            assert (
                conn.scalar(sa.select(sa.func.count()).select_from(ne_agent_run_turn_targets)) == 1
            )
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
        engine.dispose()
