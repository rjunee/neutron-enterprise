# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-Postgres concurrency proof for the session store (NE-070H, race 1).

Env-gated on ``NE_TEST_POSTGRES_URL``. The pilot tier is the first deployment that
runs MULTIPLE workers against ONE Postgres, so these tests exercise the exact
check-then-act races the fix targets with genuinely concurrent workers on separate
connections/stores:

* :func:`test_concurrent_open_session_yields_one_session` — 8 workers opening the
  same (agent, department) all receive ONE identical session id, exactly one row.
* :func:`test_concurrent_append_turn_gapless_unique_contiguous` — 8 workers x 25
  appends to ONE session produce seq exactly {1..200}: contiguous, gapless, unique.
* :func:`test_old_max_plus_one_pattern_loses_where_fix_wins` — red control: the
  pre-fix SELECT-max+1 pattern DUPLICATES seqs under the identical harness (proves
  the harness is sensitive to the race the fix eliminates).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelRequest, UserPromptPart

import ne.runtime.store as store_module
from ne.runtime.store import (
    SqlAlchemySessionStore,
    conversation_request_fingerprint,
    ensure_runtime_schema,
    metadata,
    ne_agent_conversation_turns,
    ne_agent_conversations,
    ne_agent_session_messages,
    ne_agent_session_seq,
    ne_agent_sessions,
)

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL, pool_size=20, max_overflow=20)
    metadata.drop_all(engine)
    ensure_runtime_schema(engine)
    try:
        yield engine
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def test_concurrent_open_session_yields_one_session(pg_engine: sa.Engine) -> None:
    n = 8
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        store = SqlAlchemySessionStore(pg_engine)
        barrier.wait()
        sid = asyncio.run(store.open_session("assistant", "eng"))
        with lock:
            results.append(sid)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1  # all 8 workers got the SAME id
    with pg_engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(ne_agent_sessions)).scalar_one()
    assert count == 1


def test_concurrent_append_turn_gapless_unique_contiguous(pg_engine: sa.Engine) -> None:
    store = SqlAlchemySessionStore(pg_engine)
    session_id = asyncio.run(store.open_session("assistant", "eng"))

    workers = 8
    per_worker = 25
    barrier = threading.Barrier(workers)

    def worker() -> None:
        w = SqlAlchemySessionStore(pg_engine)
        barrier.wait()

        async def appends() -> None:
            for _ in range(per_worker):
                await w.append_turn(session_id, [ModelRequest(parts=[UserPromptPart(content="x")])])

        asyncio.run(appends())

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with pg_engine.connect() as conn:
        seqs = [
            int(r.seq)
            for r in conn.execute(
                sa.select(ne_agent_session_messages.c.seq)
                .where(ne_agent_session_messages.c.session_id == session_id)
                .order_by(ne_agent_session_messages.c.seq)
            ).all()
        ]
    assert seqs == list(range(1, workers * per_worker + 1))  # gapless, contiguous, unique


def test_owned_conversation_concurrent_append_is_gapless_and_owner_filtered(
    pg_engine: sa.Engine,
) -> None:
    store = SqlAlchemySessionStore(pg_engine)
    conversation = asyncio.run(store.create_conversation("assistant", "eng", "user:ada"))
    workers = 8
    per_worker = 25
    barrier = threading.Barrier(workers)

    def worker() -> None:
        local = SqlAlchemySessionStore(pg_engine)
        barrier.wait()

        async def appends() -> None:
            for _ in range(per_worker):
                await local.append_conversation_turn(
                    conversation.conversation_id,
                    "user:ada",
                    "eng",
                    "assistant",
                    [ModelRequest(parts=[UserPromptPart(content="x")])],
                    served_output="served",
                    trace_id="trace",
                    attachment_id=f"turn-{threading.get_ident()}-{_}",
                    attempt_id=f"attempt-{threading.get_ident()}-{_}",
                )

        asyncio.run(appends())

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with pg_engine.connect() as conn:
        sequences = list(
            conn.execute(
                sa.select(ne_agent_conversation_turns.c.sequence)
                .where(
                    ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id
                )
                .order_by(ne_agent_conversation_turns.c.sequence)
            ).scalars()
        )
    assert sequences == list(range(1, workers * per_worker + 1))
    assert (
        asyncio.run(store.get_conversation(conversation.conversation_id, "user:bob", "eng")) is None
    )


def test_concurrent_same_owner_tuple_creates_distinct_conversations(pg_engine: sa.Engine) -> None:
    workers = 16
    barrier = threading.Barrier(workers)
    conversation_ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local = SqlAlchemySessionStore(pg_engine)
        barrier.wait()
        row = asyncio.run(local.create_conversation("assistant", "eng", "user:ada"))
        with lock:
            conversation_ids.append(row.conversation_id)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(conversation_ids) == workers
    assert len(set(conversation_ids)) == workers
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_agent_conversations.c.conversation_id).where(
                ne_agent_conversations.c.owner_principal_id == "user:ada",
                ne_agent_conversations.c.department == "eng",
                ne_agent_conversations.c.agent_name == "assistant",
            )
        ).scalars()
        assert set(rows) == set(conversation_ids)


def test_concurrent_same_attachment_has_one_execution_owner(pg_engine: sa.Engine) -> None:
    store = SqlAlchemySessionStore(pg_engine)
    conversation = asyncio.run(store.create_conversation("assistant", "eng", "user:ada"))
    fingerprint = conversation_request_fingerprint(
        conversation.conversation_id,
        "user:ada",
        "eng",
        "assistant",
        "same prompt",
        None,
    )
    workers = 8
    barrier = threading.Barrier(workers)
    dispositions: list[tuple[str, str | None]] = []
    lock = threading.Lock()

    def worker() -> None:
        local = SqlAlchemySessionStore(pg_engine)
        barrier.wait()
        claim = asyncio.run(
            local.claim_conversation_attachment(
                conversation.conversation_id,
                "user:ada",
                "eng",
                "assistant",
                "same-attachment",
                fingerprint,
                "same-trace",
            )
        )
        with lock:
            dispositions.append((claim.disposition, claim.attempt_id))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    owners = [attempt for disposition, attempt in dispositions if disposition == "owned"]
    assert len(owners) == 1
    assert owners[0] is not None
    assert [disposition for disposition, _attempt in dispositions].count("inflight") == workers - 1
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_agent_conversation_turns).where(
                ne_agent_conversation_turns.c.conversation_id == conversation.conversation_id,
                ne_agent_conversation_turns.c.attachment_id == "same-attachment",
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].sequence == 1
    assert rows[0].publication_state == "claimed"


def test_concurrent_first_schema_startup_serializes_legacy_unique_index_creation(
    pg_engine: sa.Engine,
) -> None:
    metadata.drop_all(pg_engine)
    workers = 8
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            ensure_runtime_schema(pg_engine)
        except BaseException as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    inspector = sa.inspect(pg_engine)
    assert inspector.has_table(ne_agent_conversations.name)
    assert {item["name"] for item in inspector.get_indexes(ne_agent_session_messages.name)} >= {
        "uq_agent_session_messages_seq"
    }


def test_postgres_restart_quarantines_mixed_legacy_rows_without_assignment(
    pg_engine: sa.Engine,
) -> None:
    store = SqlAlchemySessionStore(pg_engine)

    async def seed() -> tuple[str, list[tuple[object, ...]]]:
        legacy_id = await store.open_session("assistant", "eng")
        for index, principal in enumerate((None, "user:ada", "user:bob"), start=1):
            await store.append_turn(
                legacy_id,
                [ModelRequest(parts=[UserPromptPart(content=f"legacy-{index}")])],
                principal_id=principal,
                trace_id=None if principal is None else f"legacy-trace-{index}",
            )
        with pg_engine.connect() as conn:
            before = [tuple(row) for row in conn.execute(sa.select(ne_agent_session_messages))]
        return legacy_id, before

    legacy_id, before = asyncio.run(seed())
    pg_engine.dispose()
    assert _URL is not None
    restarted_engine = sa.create_engine(_URL, pool_size=20, max_overflow=20)
    try:
        ensure_runtime_schema(restarted_engine)
        restarted = SqlAlchemySessionStore(restarted_engine)
        with restarted_engine.connect() as conn:
            after = [tuple(row) for row in conn.execute(sa.select(ne_agent_session_messages))]
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_conversations)
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(ne_agent_session_seq)
                ).scalar_one()
                == 1
            )
        assert after == before
        page = asyncio.run(restarted.list_conversation_turns(limit=10))
        assert {turn.principal_id for turn in page.turns} == {None, "user:ada", "user:bob"}
        for owner in ("user:ada", "user:bob"):
            assert asyncio.run(restarted.list_conversations(owner, "eng")) == ()
            assert asyncio.run(restarted.get_conversation(legacy_id, owner, "eng")) is None
            assert (
                asyncio.run(
                    restarted.load_conversation_history(legacy_id, owner, "eng", "assistant")
                )
                == []
            )
    finally:
        restarted_engine.dispose()


@pytest.mark.parametrize(
    "changed_literal", ["PREPARED", "pre pared", 'pre"pared', "prepared::text"]
)
def test_postgres_startup_rejects_malformed_publication_check_literals(
    pg_engine: sa.Engine, changed_literal: str
) -> None:
    expression = (
        "publication_state IN ('claimed', 'staged', 'prepared', 'published', 'aborted')"
    ).replace("prepared", changed_literal)
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE ne_agent_conversation_turns "
            "DROP CONSTRAINT ck_agent_conversation_turn_publication"
        )
        conn.exec_driver_sql(
            "ALTER TABLE ne_agent_conversation_turns "
            f"ADD CONSTRAINT ck_agent_conversation_turn_publication CHECK ({expression})"
        )
    with pytest.raises(RuntimeError, match="checks"):
        ensure_runtime_schema(pg_engine)


@pytest.mark.parametrize(
    "changed_literal", ["PREPARED", "pre pared", 'pre"pared', "prepared::text"]
)
def test_postgres_startup_rejects_malformed_partial_index_literals(
    pg_engine: sa.Engine, changed_literal: str
) -> None:
    predicate = ("publication_state IN ('claimed', 'staged', 'prepared', 'published')").replace(
        "prepared", changed_literal
    )
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX uq_agent_conversation_turn_active_attachment")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_agent_conversation_turn_active_attachment "
            "ON ne_agent_conversation_turns (conversation_id, attachment_id) "
            f"WHERE {predicate}"
        )
    with pytest.raises(RuntimeError, match="indexes"):
        ensure_runtime_schema(pg_engine)


@pytest.mark.parametrize(
    ("table", "constraint", "expression", "function_result"),
    [
        (
            "ne_agent_conversations",
            "ck_agent_conversation_owner_nonempty",
            "ne_test_lengthtrim(owner_principal_id) > 0",
            1,
        ),
        (
            "ne_agent_conversation_turns",
            "ck_agent_conversation_turn_fingerprint_shape",
            "ne_test_lengthtrim(request_fingerprint) = 64",
            64,
        ),
    ],
)
def test_postgres_startup_rejects_lengthtrim_function_collision(
    pg_engine: sa.Engine,
    table: str,
    constraint: str,
    expression: str,
    function_result: int,
) -> None:
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE OR REPLACE FUNCTION ne_test_lengthtrim(text) RETURNS integer "
            f"LANGUAGE SQL IMMUTABLE AS $$ SELECT {function_result} $$"
        )
        conn.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        conn.exec_driver_sql(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({expression})"
        )
    try:
        with pytest.raises(RuntimeError, match="checks"):
            ensure_runtime_schema(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            conn.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
            conn.exec_driver_sql("DROP FUNCTION ne_test_lengthtrim(text)")


def test_postgres_startup_rejects_extra_owner_foreign_key(pg_engine: sa.Engine) -> None:
    with pg_engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE ne_agent_conversation_turns ADD CONSTRAINT fk_unexpected_extra "
            "FOREIGN KEY (conversation_id) REFERENCES ne_agent_conversations (conversation_id)"
        )
    with pytest.raises(RuntimeError, match="foreign key"):
        ensure_runtime_schema(pg_engine)


def test_postgres_startup_rejects_same_table_name_in_wrong_schema(pg_engine: sa.Engine) -> None:
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA ne_wrong_owner")
        conn.exec_driver_sql(
            "CREATE TABLE ne_wrong_owner.ne_agent_conversations ("
            "conversation_id VARCHAR NOT NULL, owner_principal_id VARCHAR NOT NULL, "
            "department VARCHAR NOT NULL, agent_name VARCHAR NOT NULL, "
            "UNIQUE (conversation_id, owner_principal_id, department, agent_name))"
        )
        conn.exec_driver_sql(
            "ALTER TABLE ne_agent_conversation_turns "
            "DROP CONSTRAINT fk_agent_conversation_turn_owner_scope"
        )
        conn.exec_driver_sql(
            "ALTER TABLE ne_agent_conversation_turns "
            "ADD CONSTRAINT fk_agent_conversation_turn_owner_scope "
            "FOREIGN KEY (conversation_id, owner_principal_id, department, agent_name) "
            "REFERENCES ne_wrong_owner.ne_agent_conversations "
            "(conversation_id, owner_principal_id, department, agent_name)"
        )
    try:
        with pytest.raises(RuntimeError, match="foreign key"):
            ensure_runtime_schema(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE ne_agent_conversation_turns "
                "DROP CONSTRAINT fk_agent_conversation_turn_owner_scope"
            )
            conn.exec_driver_sql("DROP SCHEMA ne_wrong_owner CASCADE")


def test_postgres_claim_lease_uses_server_time_under_skewed_host_clock(
    pg_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SkewedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return super().now(tz) + timedelta(days=365)

    monkeypatch.setattr(store_module, "datetime", SkewedDateTime)
    store = SqlAlchemySessionStore(pg_engine)
    conversation = asyncio.run(store.create_conversation("assistant", "eng", "user:ada"))
    fingerprint = conversation_request_fingerprint(
        conversation.conversation_id, "user:ada", "eng", "assistant", "prompt", None
    )
    asyncio.run(
        store.claim_conversation_attachment(
            conversation.conversation_id,
            "user:ada",
            "eng",
            "assistant",
            "server-time-attachment",
            fingerprint,
            "server-time-trace",
            lease_seconds=5,
        )
    )
    with pg_engine.connect() as conn:
        remaining = conn.execute(
            sa.text(
                "SELECT EXTRACT(EPOCH FROM (lease_expires_at - clock_timestamp())) "
                "FROM ne_agent_conversation_turns "
                "WHERE attachment_id = 'server-time-attachment'"
            )
        ).scalar_one()
    assert 0 < float(remaining) <= 5


def test_old_max_plus_one_pattern_loses_where_fix_wins(pg_engine: sa.Engine) -> None:
    # RED CONTROL: reimplement the pre-fix SELECT-max(seq)+1-then-INSERT sequence
    # against a table WITHOUT the counter/unique index, under the identical 8-worker
    # harness, and prove it produces DUPLICATE seqs — the exact race the fix closes.
    red = sa.Table(
        "ne070h_red_control_messages",
        sa.MetaData(),
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("session_id", sa.String, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
    )
    red.metadata.drop_all(pg_engine)
    red.metadata.create_all(pg_engine)
    try:
        session_id = "red"
        workers = 8
        per_worker = 25
        barrier = threading.Barrier(workers)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_worker):
                with pg_engine.begin() as conn:
                    current_max = conn.execute(
                        sa.select(sa.func.max(red.c.seq)).where(red.c.session_id == session_id)
                    ).scalar()
                    next_seq = 1 if current_max is None else int(current_max) + 1
                    conn.execute(red.insert().values(session_id=session_id, seq=next_seq))

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with pg_engine.connect() as conn:
            seqs = [
                int(r.seq)
                for r in conn.execute(
                    sa.select(red.c.seq).where(red.c.session_id == session_id)
                ).all()
            ]
        assert len(seqs) > len(set(seqs))  # the pre-fix pattern DUPLICATES under the race
    finally:
        red.metadata.drop_all(pg_engine)
