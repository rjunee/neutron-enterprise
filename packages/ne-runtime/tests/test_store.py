# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SqlAlchemySessionStore — SQLite parity, session identity, turn round-trips."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from sqlalchemy.exc import IntegrityError

from ne.runtime.config import RuntimeSettings
from ne.runtime.store import (
    SqlAlchemySessionStore,
    ensure_runtime_schema,
    ne_agent_session_messages,
    ne_agent_session_seq,
    ne_agent_sessions,
)


def _batch(label: str) -> list[ModelRequest]:
    """A one-message turn batch whose text encodes ``label`` for windowing asserts."""
    return [ModelRequest(parts=[UserPromptPart(content=label)])]


def _first_text(message: ModelRequest) -> str:
    part = message.parts[0]
    assert isinstance(part, UserPromptPart)
    return str(part.content)


@pytest.fixture
def store() -> Iterator[SqlAlchemySessionStore]:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    # Idempotent second call is safe.
    ensure_runtime_schema(engine)
    try:
        yield SqlAlchemySessionStore(engine)
    finally:
        engine.dispose()


def test_open_session_is_stable_per_agent_department(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> None:
        a = await store.open_session("assistant", "eng")
        b = await store.open_session("assistant", "eng")
        assert a == b
        other = await store.open_session("assistant", "sales")
        assert other != a

    asyncio.run(scenario())


def test_append_and_load_history_roundtrip_in_seq_order(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> None:
        session_id = await store.open_session("assistant", "eng")
        turn1 = [ModelRequest(parts=[UserPromptPart(content="hi")])]
        turn2 = [
            ModelResponse(
                parts=[TextPart(content="hello")],
                usage=RequestUsage(input_tokens=1, output_tokens=1),
                model_name="m",
            )
        ]
        await store.append_turn(session_id, turn1)
        await store.append_turn(session_id, turn2)
        history = await store.load_history(session_id)
        assert len(history) == 2
        assert isinstance(history[0], ModelRequest)
        assert isinstance(history[1], ModelResponse)

    asyncio.run(scenario())


def test_conversation_turns_preserve_complete_body_principal_and_trace(
    store: SqlAlchemySessionStore,
) -> None:
    async def scenario() -> None:
        session_id = await store.open_session("assistant", "eng")
        messages = [
            ModelRequest(parts=[UserPromptPart(content="QUESTION-SENTINEL")]),
            ModelResponse(
                parts=[TextPart(content="ANSWER-SENTINEL")],
                model_name="private-model",
            ),
        ]
        await store.append_turn(
            session_id,
            messages,
            principal_id="user:ada",
            trace_id="trace-exact",
        )

        page = await store.list_conversation_turns(limit=10)
        assert page.total_turns == 1
        assert len(page.turns) == 1
        turn = page.turns[0]
        assert turn.session_id == session_id
        assert turn.agent_name == "assistant"
        assert turn.department == "eng"
        assert turn.principal_id == "user:ada"
        assert turn.trace_id == "trace-exact"
        assert "QUESTION-SENTINEL" in str(turn.messages)
        assert "ANSWER-SENTINEL" in str(turn.messages)

    asyncio.run(scenario())


def test_conversation_turns_keep_legacy_attribution_absent(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> None:
        session_id = await store.open_session("assistant", "eng")
        await store.append_turn(session_id, _batch("legacy"))
        turn = (await store.list_conversation_turns(limit=1)).turns[0]
        assert turn.principal_id is None
        assert turn.trace_id is None

    asyncio.run(scenario())


def test_append_rejects_half_attribution(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> None:
        session_id = await store.open_session("assistant", "eng")
        with pytest.raises(ValueError, match="supplied together"):
            await store.append_turn(session_id, _batch("bad"), principal_id="user:ada")

    asyncio.run(scenario())


def test_runtime_schema_adds_nullable_attribution_to_legacy_message_table() -> None:
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE TABLE ne_agent_sessions ("
                    "session_id VARCHAR PRIMARY KEY, agent_name VARCHAR NOT NULL, "
                    "department VARCHAR NOT NULL, created_at DATETIME NOT NULL, "
                    "CONSTRAINT uq_agent_department UNIQUE (agent_name, department))"
                )
            )
            conn.execute(
                sa.text(
                    "CREATE TABLE ne_agent_session_messages ("
                    "id INTEGER PRIMARY KEY, session_id VARCHAR NOT NULL, seq INTEGER NOT NULL, "
                    "payload TEXT NOT NULL, created_at DATETIME NOT NULL)"
                )
            )

        ensure_runtime_schema(engine)
        columns = {
            str(column["name"]): bool(column["nullable"])
            for column in sa.inspect(engine).get_columns("ne_agent_session_messages")
        }
        assert columns["principal_id"] is True
        assert columns["trace_id"] is True
    finally:
        engine.dispose()


def test_empty_history_is_empty(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> None:
        session_id = await store.open_session("assistant", "eng")
        assert await store.load_history(session_id) == []

    asyncio.run(scenario())


def test_unique_constraint_enforced(store: SqlAlchemySessionStore) -> None:
    with store.engine.begin() as conn:
        conn.execute(
            ne_agent_sessions.insert().values(
                session_id="s1",
                agent_name="assistant",
                department="eng",
                created_at=sa.func.now(),
            )
        )
    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(
            ne_agent_sessions.insert().values(
                session_id="s2",
                agent_name="assistant",
                department="eng",
                created_at=sa.func.now(),
            )
        )


def test_open_session_concurrent_insert_conflict_returns_existing(
    store: SqlAlchemySessionStore,
) -> None:
    # Simulate the concurrent-open loser's view: the (agent, department) row already
    # exists (a sibling worker inserted it). open_session must return the EXISTING
    # id via the ON CONFLICT DO NOTHING + read path, never mint a second row.
    with store.engine.begin() as conn:
        conn.execute(
            ne_agent_sessions.insert().values(
                session_id="pre-existing",
                agent_name="assistant",
                department="eng",
                created_at=datetime.now(UTC),
            )
        )

    async def scenario() -> str:
        return await store.open_session("assistant", "eng")

    got = asyncio.run(scenario())
    assert got == "pre-existing"
    with store.engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(ne_agent_sessions)).scalar_one()
    assert count == 1


def test_load_history_caps_at_most_recent_n_batches(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> list[ModelRequest]:
        sid = await store.open_session("assistant", "eng")
        for i in range(1, 8):  # seven batches: turn-1 .. turn-7
            await store.append_turn(sid, _batch(f"turn-{i}"))
        history = await store.load_history(sid, limit=3)
        return [m for m in history if isinstance(m, ModelRequest)]

    windowed = asyncio.run(scenario())
    # Exactly the last three batches — proving the SQL LIMIT capped the rows, not a
    # post-hoc slice of a full read (the first returned is batch 5, not batch 1).
    assert [_first_text(m) for m in windowed] == ["turn-5", "turn-6", "turn-7"]


def test_load_history_window_is_chronological(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> list[str]:
        sid = await store.open_session("assistant", "eng")
        for i in range(1, 8):
            await store.append_turn(sid, _batch(f"turn-{i}"))
        history = await store.load_history(sid, limit=3)
        return [_first_text(m) for m in history if isinstance(m, ModelRequest)]

    # Despite the DESC fetch, the model receives the window oldest-first.
    assert asyncio.run(scenario()) == ["turn-5", "turn-6", "turn-7"]


def test_load_history_without_limit_returns_all(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> list[str]:
        sid = await store.open_session("assistant", "eng")
        for i in range(1, 5):
            await store.append_turn(sid, _batch(f"turn-{i}"))
        history = await store.load_history(sid)  # limit=None back-compat
        return [_first_text(m) for m in history if isinstance(m, ModelRequest)]

    assert asyncio.run(scenario()) == ["turn-1", "turn-2", "turn-3", "turn-4"]


def test_reset_history_clears_messages_and_seq_counter(store: SqlAlchemySessionStore) -> None:
    async def scenario() -> tuple[list[str], str]:
        sid = await store.open_session("assistant", "eng")
        await store.append_turn(sid, _batch("turn-1"))
        await store.append_turn(sid, _batch("turn-2"))
        await store.reset_history(sid)
        # History is empty and the session id is stable.
        assert await store.load_history(sid) == []
        assert await store.open_session("assistant", "eng") == sid
        # A fresh append re-seeds the counter at seq 1.
        await store.append_turn(sid, _batch("turn-fresh"))
        history = await store.load_history(sid)
        return [_first_text(m) for m in history if isinstance(m, ModelRequest)], sid

    texts, sid = asyncio.run(scenario())
    assert texts == ["turn-fresh"]
    with store.engine.connect() as conn:
        # The old seq counter row was removed by reset_history, then re-seeded at 1.
        last_seq = conn.execute(
            sa.select(ne_agent_session_seq.c.last_seq).where(
                ne_agent_session_seq.c.session_id == sid
            )
        ).scalar_one()
        assert last_seq == 1
        max_seq = conn.execute(
            sa.select(sa.func.max(ne_agent_session_messages.c.seq)).where(
                ne_agent_session_messages.c.session_id == sid
            )
        ).scalar_one()
        assert max_seq == 1


def test_from_settings_builds_engine_with_timeout_connect_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    real = sa.create_engine

    def fake(url: str, **kwargs: object) -> sa.Engine:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return real(url, **kwargs)

    monkeypatch.setattr("ne.audit.engine.sa.create_engine", fake)
    store = SqlAlchemySessionStore.from_settings(RuntimeSettings(session_database_url="sqlite://"))
    try:
        assert captured["url"] == "sqlite://"
        assert captured["kwargs"] == {"connect_args": {"timeout": 10}}
    finally:
        store.engine.dispose()


def test_store_offload_does_not_block_event_loop() -> None:
    engine = sa.create_engine("sqlite://")
    ensure_runtime_schema(engine)
    store = SqlAlchemySessionStore(engine, offload_db_io=True)

    def _slow(
        _session_id: str,
        _messages: object,
        _principal_id: str | None,
        _trace_id: str | None,
    ) -> None:
        time.sleep(0.3)

    store._append_turn_sync = _slow  # type: ignore[method-assign]

    async def scenario() -> int:
        counter = {"n": 0}
        stop = asyncio.Event()

        async def ticker() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.01)
                counter["n"] += 1

        tick_task = asyncio.create_task(ticker())
        await store.append_turn("sid", _batch("x"))
        stop.set()
        await tick_task
        return counter["n"]

    try:
        ticks = asyncio.run(scenario())
    finally:
        engine.dispose()
    assert ticks > 5


def test_append_turn_claims_contiguous_seq_and_initializes_from_legacy_rows(
    store: SqlAlchemySessionStore,
) -> None:
    session_id = "legacy-session"
    with store.engine.begin() as conn:
        conn.execute(
            ne_agent_sessions.insert().values(
                session_id=session_id,
                agent_name="assistant",
                department="eng",
                created_at=datetime.now(UTC),
            )
        )
        # Legacy message rows seq 1..3 with NO counter row (pre-NE-070H DB shape).
        for seq in (1, 2, 3):
            conn.execute(
                ne_agent_session_messages.insert().values(
                    session_id=session_id,
                    seq=seq,
                    payload="[]",
                    created_at=datetime.now(UTC),
                )
            )

    async def scenario() -> None:
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        await store.append_turn(session_id, [ModelRequest(parts=[UserPromptPart(content="x")])])

    asyncio.run(scenario())

    with store.engine.connect() as conn:
        # The counter seeded from legacy max(seq)=3 and claimed 4.
        last_seq = conn.execute(
            sa.select(ne_agent_session_seq.c.last_seq).where(
                ne_agent_session_seq.c.session_id == session_id
            )
        ).scalar_one()
        assert last_seq == 4
        max_msg_seq = conn.execute(
            sa.select(sa.func.max(ne_agent_session_messages.c.seq)).where(
                ne_agent_session_messages.c.session_id == session_id
            )
        ).scalar_one()
        assert max_msg_seq == 4

    # A fresh session with no legacy rows starts its counter at 1.
    async def fresh() -> str:
        sid = await store.open_session("assistant", "sales")
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        await store.append_turn(sid, [ModelRequest(parts=[UserPromptPart(content="y")])])
        return sid

    fresh_sid = asyncio.run(fresh())
    with store.engine.connect() as conn:
        fresh_seq = conn.execute(
            sa.select(ne_agent_session_seq.c.last_seq).where(
                ne_agent_session_seq.c.session_id == fresh_sid
            )
        ).scalar_one()
    assert fresh_seq == 1
