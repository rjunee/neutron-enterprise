# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
from ne.runtime.run_store import (
    AgentRunStore,
    ensure_run_schema,
    ne_agent_run_skill_activations,
)
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema


def _stores() -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore]:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    return engine, SqlAlchemySessionStore(engine), AgentRunStore(engine, cipher)


def _postgres_stores(url: str) -> tuple[sa.Engine, SqlAlchemySessionStore, AgentRunStore]:
    engine = sa.create_engine(url)
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": b"k" * 32})
    )
    return engine, SqlAlchemySessionStore(engine), AgentRunStore(engine, cipher)


async def _run_and_attempt(
    sessions: SqlAlchemySessionStore, runs: AgentRunStore, *, strategy: str = "agentic"
) -> tuple[str, str]:
    conversation = await sessions.create_conversation("assistant", "claims", "alice")
    run = await runs.create_run(
        conversation_id=conversation.conversation_id,
        owner_principal_id="alice",
        department="claims",
        conversation_agent_name="assistant",
        agent_id="agt_01",
        agent_version=7,
        strategy=strategy,
        config_hash="1" * 64,
        capability_snapshot_hash="2" * 64,
        capability_snapshot_receipt_id="cap-receipt-1",
        input_plaintext=b"private prompt",
    )
    attempt = await runs.link_attempt(run_id=run.run_id)
    return run.run_id, attempt.attempt_id


def test_skill_activation_receipt_is_content_free_immutable_and_exactly_replayable() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        run_id, attempt_id = await _run_and_attempt(sessions, runs)
        values = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "agent_id": "agt_01",
            "agent_version": 7,
            "surface_key": f"run:{run_id}:attempt:{attempt_id}:skills",
            "pins": (("meridian-hardship-draft", 1, "a" * 64),),
            "index_hash": "b" * 64,
            "surface_hash": "c" * 64,
        }
        first = await runs.record_skill_activation(**values)
        replay = await runs.record_skill_activation(**values)
        assert replay == first
        assert (
            await runs.load_skill_activation(
                run_id=run_id, attempt_id=attempt_id, owner_principal_id="alice"
            )
            == first
        )
        assert (
            await runs.load_skill_activation(
                run_id=run_id, attempt_id=attempt_id, owner_principal_id="foreign"
            )
            is None
        )
        assert first.pins == values["pins"]

        for changed in (
            {"index_hash": "d" * 64},
            {"surface_hash": "e" * 64},
            {"pins": (("meridian-hardship-draft", 1, "f" * 64),)},
            {"surface_key": f"run:{run_id}:attempt:{attempt_id}:other"},
        ):
            with pytest.raises(RuntimeError, match="different Skill activation"):
                await runs.record_skill_activation(**{**values, **changed})

        with pytest.raises(RuntimeError, match="immutable Run Agent pin"):
            await runs.record_skill_activation(**{**values, "agent_version": 8})

        with engine.connect() as conn:
            row = conn.execute(sa.select(ne_agent_run_skill_activations)).one()
            assert set(row._mapping) == {
                "activation_receipt_id",
                "run_id",
                "attempt_id",
                "agent_id",
                "agent_version",
                "surface_key",
                "pins_json",
                "index_hash",
                "surface_hash",
                "receipt_hash",
                "created_at",
            }
            serialized = str(row.pins_json)
            assert "private prompt" not in serialized
            assert "procedure" not in serialized

        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(
                ne_agent_run_skill_activations.update()
                .where(ne_agent_run_skill_activations.c.run_id == run_id)
                .values(index_hash="d" * 64)
            )

    asyncio.run(scenario())
    engine.dispose()


def test_deterministic_execution_profile_receipt_is_frozen_and_owner_scoped() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        run_id, attempt_id = await _run_and_attempt(sessions, runs, strategy="deterministic")
        values = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "workflow_id": "meridian.hardship.review",
            "profile_version": 4,
            "profile_hash": "a" * 64,
            "input_schema": {
                "type": "object",
                "properties": {"max_subjects": {"type": "integer"}},
                "required": ["max_subjects"],
            },
            "ordered_plan": (
                {"step_id": "select", "operation": "select_subjects"},
                {"step_id": "draft", "operation": "draft_reviews"},
            ),
        }
        first = await runs.record_execution_profile(**values)
        assert await runs.record_execution_profile(**values) == first
        assert (
            await runs.load_execution_profile(
                run_id=run_id, attempt_id=attempt_id, owner_principal_id="alice"
            )
            == first
        )
        assert (
            await runs.load_execution_profile(
                run_id=run_id, attempt_id=attempt_id, owner_principal_id="foreign"
            )
            is None
        )
        with pytest.raises(RuntimeError, match="different profile"):
            await runs.record_execution_profile(
                **{**values, "ordered_plan": ({"step_id": "changed"},)}
            )

    asyncio.run(scenario())
    engine.dispose()


def test_skill_activation_requires_exact_attempt_and_canonical_pins() -> None:
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        run_id, attempt_id = await _run_and_attempt(sessions, runs)
        values = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "agent_id": "agt_01",
            "agent_version": 7,
            "surface_key": "surface",
            "pins": (("skill", 1, "a" * 64),),
            "index_hash": "b" * 64,
            "surface_hash": "c" * 64,
        }
        with pytest.raises(LookupError, match="unknown Run attempt"):
            await runs.record_skill_activation(**{**values, "attempt_id": "other"})
        with pytest.raises(ValueError, match="duplicate pins"):
            await runs.record_skill_activation(
                **{
                    **values,
                    "pins": (("skill", 1, "a" * 64), ("skill", 2, "b" * 64)),
                }
            )

    asyncio.run(scenario())
    engine.dispose()


def test_zero_pin_activation_records_a_valid_empty_receipt() -> None:
    """A zero-pin Agent's attempt records "activated exactly no Skills".

    THE LIVE REGRESSION: a skill-less agentic Agent (Meridian Analyst) could
    never run because the store refused an empty pin tuple with a bare
    ``ValueError`` before any model call. The empty receipt is a positive fact
    under the same replay/mismatch discipline as any other activation — absence
    of the row keeps exactly one meaning (the attempt died before activation).
    """
    engine, sessions, runs = _stores()

    async def scenario() -> None:
        run_id, attempt_id = await _run_and_attempt(sessions, runs)
        values = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "agent_id": "agt_01",
            "agent_version": 7,
            "surface_key": f"run:{run_id}:attempt:{attempt_id}:skills",
            "pins": (),
            "index_hash": "b" * 64,
            "surface_hash": "c" * 64,
        }
        first = await runs.record_skill_activation(**values)
        assert first.pins == ()
        # Exactly replayable, like any other receipt...
        assert await runs.record_skill_activation(**values) == first
        assert (
            await runs.load_skill_activation(
                run_id=run_id, attempt_id=attempt_id, owner_principal_id="alice"
            )
            == first
        )
        # ...and still fail-closed: the empty receipt does not relax the
        # replay-mismatch guard, in either direction.
        with pytest.raises(RuntimeError, match="different Skill activation"):
            await runs.record_skill_activation(**{**values, "surface_hash": "e" * 64})
        with pytest.raises(RuntimeError, match="different Skill activation"):
            await runs.record_skill_activation(**{**values, "pins": (("skill", 1, "a" * 64),)})
        with engine.connect() as conn:
            row = conn.execute(sa.select(ne_agent_run_skill_activations)).one()
            assert str(row.pins_json) == "[]"

    asyncio.run(scenario())
    engine.dispose()


def test_skill_activation_receipt_replay_and_mismatch_on_postgres() -> None:
    url = os.environ.get("NE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("NE_TEST_POSTGRES_URL is required for the Skill activation PostgreSQL proof")
    engine, sessions, runs = _postgres_stores(url)

    async def scenario() -> None:
        run_id, attempt_id = await _run_and_attempt(sessions, runs)
        values = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "agent_id": "agt_01",
            "agent_version": 7,
            "surface_key": f"run:{run_id}:attempt:{attempt_id}:skills",
            "pins": (("meridian-hardship-draft", 1, "a" * 64),),
            "index_hash": "b" * 64,
            "surface_hash": "c" * 64,
        }
        first = await runs.record_skill_activation(**values)
        assert await runs.record_skill_activation(**values) == first
        with pytest.raises(RuntimeError, match="different Skill activation"):
            await runs.record_skill_activation(**{**values, "surface_hash": "d" * 64})
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(
                ne_agent_run_skill_activations.delete().where(
                    ne_agent_run_skill_activations.c.activation_receipt_id
                    == first.activation_receipt_id
                )
            )

    asyncio.run(scenario())
    engine.dispose()
