# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-14: durable session context is a model-independent, content-free object."""

from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import RuntimeAuditError
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_session_messages,
)

from .conftest import (
    NamedApproverGate,
    RecordingAuditSink,
    ScriptedGateway,
    ToolLog,
    make_executor,
    make_principal,
    standard_bindings,
    text_completion,
)


def _turn(prompt: str, answer: str) -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)], model_name="fixture"),
    ]


def test_snapshot_identity_is_stable_bounded_and_model_independent() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)

    async def scenario() -> None:
        session_id = await store.open_session("assistant", "engineering")
        empty = await store.context_snapshot(session_id, max_components=2)
        assert empty is not None
        assert empty.components == ()
        assert empty.total_components == 0
        assert not empty.truncated

        await store.append_turn(session_id, _turn("first", "one"))
        first = await store.context_snapshot(session_id, max_components=2)
        again = await store.context_snapshot(session_id, max_components=2)
        assert first == again
        assert first is not None
        assert len(first.components) == 1

        await store.append_turn(session_id, _turn("second", "two"))
        await store.append_turn(session_id, _turn("third", "three"))
        current = await store.context_snapshot(session_id, max_components=2)
        assert current is not None
        assert [component.seq for component in current.components] == [2, 3]
        assert current.total_components == 3
        assert current.truncated
        assert current.context_id != first.context_id

        other_session = await store.open_session("assistant", "sales")
        await store.append_turn(other_session, _turn("first", "one"))
        other = await store.context_snapshot(other_session, max_components=2)
        assert other is not None
        assert other.context_id != first.context_id
        assert other.components[0].component_id != first.components[0].component_id

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_component_identity_changes_when_only_the_persisted_payload_changes() -> None:
    """The mutation control: a seq-only/session-only digest must fail this test."""
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)

    async def scenario() -> None:
        session_id = await store.open_session("assistant", "engineering")
        await store.append_turn(session_id, _turn("first", "one"))
        before = await store.context_snapshot(session_id, max_components=40)
        assert before is not None

        # Hold session + seq fixed and mutate ONLY the serialized payload. This is
        # not a production write path; it is the discriminating proof that payload
        # bytes participate in both identities rather than seq presence alone.
        with engine.begin() as conn:
            conn.execute(
                ne_agent_session_messages.update()
                .where(ne_agent_session_messages.c.session_id == session_id)
                .values(payload='[{"kind":"different-persisted-payload"}]')
            )

        after = await store.context_snapshot(session_id, max_components=40)
        assert after is not None
        assert after.components[0].seq == before.components[0].seq
        assert after.components[0].component_id != before.components[0].component_id
        assert after.context_id != before.context_id

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_snapshot_of_unknown_session_is_absent() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)
    try:
        assert asyncio.run(store.context_snapshot("missing", max_components=40)) is None
    finally:
        engine.dispose()


def test_successful_session_terminal_stamps_persisted_context_identity() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine)
    sink = RecordingAuditSink()
    session = DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "engineering", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="engineering",
        owner_principal_id="user:ada",
        store=store,
        executor=make_executor(
            standard_bindings(ToolLog()), sink, approval_gate=NamedApproverGate()
        ),
        gateway=ScriptedGateway([text_completion("SECRET-ANSWER-NEVER-STAMP-12d8")]),
        settings=RuntimeSettings(session_history_max_turns=2),
        sink=sink,
    )

    try:
        asyncio.run(
            session.run(
                "SECRET-PROMPT-NEVER-STAMP-c332",
                principal=make_principal(department="engineering"),
            )
        )
        snapshot = asyncio.run(store.context_snapshot(session.session_id, max_components=2))
        assert snapshot is not None
        terminal = sink.with_action("session.run")[-1]
        assert terminal.outcome == "success"
        assert terminal.provenance == {
            "context_id": snapshot.context_id,
            "context_active_components": str(len(snapshot.components)),
            "context_total_components": str(snapshot.total_components),
        }
        serialized = json.dumps(terminal.model_dump(mode="json"))
        assert "SECRET-PROMPT-NEVER-STAMP-c332" not in serialized
        assert "SECRET-ANSWER-NEVER-STAMP-12d8" not in serialized
    finally:
        engine.dispose()


def test_successful_run_with_context_snapshot_failure_withholds_result() -> None:
    class _FailingSnapshotStore(SqlAlchemySessionStore):
        async def context_snapshot(  # type: ignore[override]
            self, session_id: str, *, max_components: int
        ) -> None:
            raise RuntimeError("context store unavailable")

    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = _FailingSnapshotStore(engine)
    sink = RecordingAuditSink()
    session = DepartmentAgentSession(
        session_id=asyncio.run(
            store.create_conversation("assistant", "engineering", "user:ada")
        ).conversation_id,
        agent_name="assistant",
        department="engineering",
        owner_principal_id="user:ada",
        store=store,
        executor=make_executor(
            standard_bindings(ToolLog()), sink, approval_gate=NamedApproverGate()
        ),
        gateway=ScriptedGateway([text_completion("withheld")]),
        settings=RuntimeSettings(),
        sink=sink,
    )

    try:
        with pytest.raises(RuntimeAuditError, match="context snapshot failed"):
            asyncio.run(session.run("prompt", principal=make_principal(department="engineering")))
        assert sink.with_action("session.run") == []
    finally:
        engine.dispose()
