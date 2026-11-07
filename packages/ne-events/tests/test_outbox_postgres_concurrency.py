# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-Postgres concurrency proof for the outbox settled-horizon fix (NE-070).

Env-gated on ``NE_TEST_POSTGRES_URL`` (same gate as ``test_outbox_postgres.py``).
The pilot tier is the first deployment that runs MULTIPLE workers against ONE
Postgres, so these tests exercise the exact multi-writer sequence-gap the fix
targets, with genuinely concurrent producers/consumers on separate connections:

* :func:`test_uncommitted_lower_id_is_never_acked_past` — the SPEC sequence gap:
  an open-transaction lower id vs a committed higher sibling is never skipped.
* :func:`test_concurrent_producers_stress_no_event_lost` — 8 producers x 200
  events with randomized commit delays, 2 competing drain workers: delivered set
  equals appended set (at-least-once, zero loss).
* :func:`test_competing_consumers_skip_locked_claim` — a second reader of a held
  consumer gets an empty batch, never a block or a duplicate window.
* :func:`test_rollback_hole_does_not_stall_cursor` — liveness past a permanently
  dead (rolled-back) id.
* :func:`test_racing_acks_never_regress_cursor` — concurrent acks land monotone.
"""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from ne.events import append, drain_once, read_batch
from ne.events.outbox import (
    CursorRegressionError,
    OutboxRecord,
    ack,
    ensure_outbox_schema,
    metadata,
    ne_outbox_cursors,
)
from ne.schema import AuditEvent, AuditOutcome

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)

_HASH = "c" * 64


def _audit(event_id: str | None = None) -> AuditEvent:
    return AuditEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type="audit",
        config_hash=_HASH,
        actor="sys",
        provenance={"source": "pg-concurrency"},
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        action="a",
        resource="r",
        outcome=AuditOutcome.success,
    )


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    # A pool comfortably larger than the worst-case concurrent connection count
    # (8 producers + 2 consumers + held-open transactions).
    engine = sa.create_engine(_URL, pool_size=20, max_overflow=20)
    metadata.drop_all(engine)
    ensure_outbox_schema(engine)
    try:
        yield engine
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def _drain_collect(
    engine: sa.Engine, consumer: str, sink: set[str], *, max_ticks: int = 200
) -> int:
    """Drain ``consumer`` until a tick delivers nothing; record delivered ids.

    Returns the number of ticks that actually delivered records. Bounded so a
    logic bug cannot hang the suite.
    """
    delivering_ticks = 0
    empty_streak = 0
    for _ in range(max_ticks):
        captured: list[str] = []

        def handler(records: list[OutboxRecord], _c: list[str] = captured) -> None:
            _c.extend(r.event_id for r in records)

        drain_once(engine, consumer, handler)
        if captured:
            sink.update(captured)
            delivering_ticks += 1
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break
    return delivering_ticks


def test_uncommitted_lower_id_is_never_acked_past(pg_engine: sa.Engine) -> None:
    # Producer A takes the LOWER id and holds its transaction open (a gap).
    raw_a = pg_engine.connect()
    tx_a = raw_a.begin()
    id_a = append(raw_a, _audit(event_id="ev-a"))

    # Producer B takes the HIGHER id and COMMITS first.
    with pg_engine.begin() as conn_b:
        id_b = append(conn_b, _audit(event_id="ev-b"))
    assert id_a < id_b, "A must hold the lower id for this to be the gap case"

    # While A is open, draining must NEVER deliver B (that would skip the gap A).
    seen_while_open: set[str] = set()
    _drain_collect(pg_engine, "c", seen_while_open)
    assert seen_while_open == set(), (
        "delivered a higher committed id while a lower id was still uncommitted "
        "— the cursor would permanently skip the gap row"
    )
    # And the cursor must not have advanced past the gap.
    with pg_engine.connect() as conn:
        pos = conn.execute(
            sa.select(ne_outbox_cursors.c.position).where(ne_outbox_cursors.c.consumer == "c")
        ).scalar_one()
    assert pos < id_a

    # The straggler commits; now BOTH events must deliver (at-least-once holds).
    tx_a.commit()
    raw_a.close()

    delivered: set[str] = set()
    _drain_collect(pg_engine, "c", delivered)
    assert delivered == {"ev-a", "ev-b"}


def test_concurrent_producers_stress_no_event_lost(pg_engine: sa.Engine) -> None:
    n_producers = 8
    per_producer = 200
    appended: set[str] = set()
    appended_lock = threading.Lock()
    producers_done = threading.Event()

    def produce(tid: int) -> None:
        rng = random.Random(tid)
        local: list[str] = []
        for i in range(per_producer):
            eid = f"p{tid}-{i}"
            with pg_engine.begin() as conn:
                append(conn, _audit(event_id=eid))
                # Randomized pre-commit delay: opens real sequence gaps between
                # a dispensed id and its commit, across competing producers.
                time.sleep(rng.uniform(0, 0.008))
            local.append(eid)
        with appended_lock:
            appended.update(local)

    delivered: set[str] = set()
    delivered_lock = threading.Lock()

    def consume() -> None:
        # Two competing drain workers share ONE consumer name.
        while True:
            captured: list[str] = []

            def handler(records: list[OutboxRecord], _c: list[str] = captured) -> None:
                _c.extend(r.event_id for r in records)

            drain_once(pg_engine, "stress", handler)
            if captured:
                # ALWAYS record what a tick delivered before any termination
                # check — the tick already acked these ids in the DB, so dropping
                # them from the set would look like loss the cursor can't replay.
                with delivered_lock:
                    delivered.update(captured)
                continue
            # An empty tick: stop once producers are done (a transient empty tick
            # while the peer holds the cursor lock is swept by the main thread's
            # final _drain_collect after both consumers join).
            if producers_done.is_set():
                return
            time.sleep(0.005)

    producers = [threading.Thread(target=produce, args=(t,)) for t in range(n_producers)]
    consumers = [threading.Thread(target=consume) for _ in range(2)]
    for c in consumers:
        c.start()
    for p in producers:
        p.start()
    for p in producers:
        p.join()
    producers_done.set()
    for c in consumers:
        c.join(timeout=60)
        assert not c.is_alive()

    # Final settle pass on the main thread to sweep any tail window.
    _drain_collect(pg_engine, "stress", delivered)

    assert len(appended) == n_producers * per_producer
    missing = appended - delivered
    assert missing == set(), f"{len(missing)} event(s) lost — at-least-once violated"


def test_competing_consumers_skip_locked_claim(pg_engine: sa.Engine) -> None:
    with pg_engine.begin() as conn:
        for _ in range(5):
            append(conn, _audit())
    # Register the cursor row with a committed read so the concurrent claim races
    # a FOR UPDATE lock (not an uncommitted INSERT, which would block instead).
    with pg_engine.begin() as conn:
        read_batch(conn, "shared")

    # conn1 holds the read transaction (and therefore the cursor-row lock) open.
    raw1 = pg_engine.connect()
    tx1 = raw1.begin()
    recs1, _ = read_batch(raw1, "shared")
    assert recs1, "the holder must actually receive the window"

    # conn2 cannot obtain the locked cursor row: it reads NOTHING (SKIP LOCKED),
    # neither blocking nor double-delivering the window conn1 holds.
    raw2 = pg_engine.connect()
    tx2 = raw2.begin()
    recs2, _ = read_batch(raw2, "shared")
    assert recs2 == []
    tx2.rollback()
    raw2.close()

    tx1.rollback()
    raw1.close()


def test_rollback_hole_does_not_stall_cursor(pg_engine: sa.Engine) -> None:
    # A producer takes an id then ROLLS BACK — a permanently dead hole.
    raw = pg_engine.connect()
    tx = raw.begin()
    dead_id = append(raw, _audit(event_id="dead"))
    tx.rollback()
    raw.close()

    with pg_engine.begin() as conn:
        live1 = append(conn, _audit(event_id="live1"))
        live2 = append(conn, _audit(event_id="live2"))
    assert dead_id < live1, "the dead id must sit below the live ids to be a hole"

    delivered: set[str] = set()
    _drain_collect(pg_engine, "c", delivered)
    assert delivered == {"live1", "live2"}

    # The cursor advanced PAST the dead hole (it must not stall forever on it).
    with pg_engine.connect() as conn:
        pos = conn.execute(
            sa.select(ne_outbox_cursors.c.position).where(ne_outbox_cursors.c.consumer == "c")
        ).scalar_one()
    assert pos >= live2


def test_racing_acks_never_regress_cursor(pg_engine: sa.Engine) -> None:
    with pg_engine.begin() as conn:
        for _ in range(10):
            append(conn, _audit())
    # Register the cursor at position 0.
    with pg_engine.begin() as conn:
        read_batch(conn, "c")

    barrier = threading.Barrier(2)

    def ack_to(upto: int) -> None:
        barrier.wait()
        try:
            with pg_engine.begin() as conn:
                ack(conn, "c", upto)
        except CursorRegressionError:
            # An acceptable outcome: refusing to move the cursor backwards.
            pass

    t_hi = threading.Thread(target=ack_to, args=(10,))
    t_lo = threading.Thread(target=ack_to, args=(5,))
    t_hi.start()
    t_lo.start()
    t_hi.join()
    t_lo.join()

    with pg_engine.connect() as conn:
        pos = conn.execute(
            sa.select(ne_outbox_cursors.c.position).where(ne_outbox_cursors.c.consumer == "c")
        ).scalar_one()
    assert pos == 10
