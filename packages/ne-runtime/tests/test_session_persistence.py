# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Session persistence — cross-turn state, per-department scoping, mismatch rejection."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.runtime.agent import DepartmentAgentSession
from ne.runtime.config import RuntimeSettings
from ne.runtime.errors import SessionDepartmentMismatchError
from ne.runtime.middleware import GovernedToolExecutor
from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

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


@pytest.fixture
def store() -> Iterator[SqlAlchemySessionStore]:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    try:
        yield SqlAlchemySessionStore(engine)
    finally:
        engine.dispose()


def _executor() -> GovernedToolExecutor:
    return make_executor(
        standard_bindings(ToolLog()), RecordingAuditSink(), approval_gate=NamedApproverGate()
    )


async def _fresh_session(
    store: SqlAlchemySessionStore,
    department: str,
    gateway: ScriptedGateway,
    executor: GovernedToolExecutor,
    *,
    conversation_id: str | None = None,
) -> DepartmentAgentSession:
    session_id = conversation_id
    if session_id is None:
        session_id = (
            await store.create_conversation("assistant", department, "user:ada")
        ).conversation_id
    return DepartmentAgentSession(
        session_id=session_id,
        agent_name="assistant",
        department=department,
        owner_principal_id="user:ada",
        store=store,
        executor=executor,
        gateway=gateway,
        settings=RuntimeSettings(),
    )


def test_state_persists_across_runs_and_instances(store: SqlAlchemySessionStore) -> None:
    executor = _executor()

    async def scenario() -> None:
        gw1 = ScriptedGateway([text_completion("answer one")])
        session1 = await _fresh_session(store, "eng", gw1, executor)
        await session1.run("first question", principal=make_principal(department="eng"))

        # A brand-new session object rehydrates the exact conversation id.
        gw2 = ScriptedGateway([text_completion("answer two")])
        session2 = await _fresh_session(
            store, "eng", gw2, executor, conversation_id=session1.session_id
        )
        assert session2.session_id == session1.session_id
        await session2.run("second question", principal=make_principal(department="eng"))

        # The second run's first gateway request carries the first turn's messages.
        first_request = gw2.requests[0]
        blob = " ".join(m.content for m in first_request.messages)
        assert "first question" in blob
        assert "answer one" in blob

    asyncio.run(scenario())


def test_department_mismatch_rejected_before_any_activity(store: SqlAlchemySessionStore) -> None:
    executor = _executor()

    async def scenario() -> None:
        gw = ScriptedGateway([text_completion("should not run")])
        session = await _fresh_session(store, "eng", gw, executor)
        with pytest.raises(SessionDepartmentMismatchError):
            await session.run("hi", principal=make_principal(department="sales"))
        # No model or tool activity happened.
        assert gw.requests == []

    asyncio.run(scenario())


def test_owner_mismatch_rejected_before_history_or_gateway_activity(
    store: SqlAlchemySessionStore,
) -> None:
    executor = _executor()

    async def scenario() -> None:
        gw = ScriptedGateway([text_completion("should not run")])
        session = await _fresh_session(store, "eng", gw, executor)
        with pytest.raises(SessionDepartmentMismatchError):
            await session.run(
                "bob must not enter Ada's conversation",
                principal=make_principal("user:bob", department="eng"),
            )
        assert gw.requests == []
        assert (
            await store.load_conversation_history(
                session.session_id, "user:bob", "eng", "assistant"
            )
            == []
        )
        with pytest.raises(LookupError):
            await store.append_conversation_turn(
                session.session_id,
                "user:bob",
                "eng",
                "assistant",
                [],
                served_output="must not write",
                trace_id="bob-trace",
                attachment_id="bob-turn",
                attempt_id="attempt-bob-turn",
            )

    asyncio.run(scenario())


def test_departments_do_not_share_history(store: SqlAlchemySessionStore) -> None:
    executor = _executor()

    async def scenario() -> None:
        gw_eng = ScriptedGateway([text_completion("eng answer")])
        eng = await _fresh_session(store, "eng", gw_eng, executor)
        await eng.run("eng secret question", principal=make_principal(department="eng"))

        gw_sales = ScriptedGateway([text_completion("sales answer")])
        sales = await _fresh_session(store, "sales", gw_sales, executor)
        assert sales.session_id != eng.session_id
        await sales.run("sales question", principal=make_principal(department="sales"))

        blob = " ".join(m.content for m in gw_sales.requests[0].messages)
        assert "eng secret question" not in blob

    asyncio.run(scenario())
