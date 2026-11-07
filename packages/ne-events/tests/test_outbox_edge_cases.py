# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ABI event-cursor teardown §3.5 checklist, translated to the Postgres pattern.

(a) cursor-advance-then-drop hazard, (b) filter-superset invariant,
(c) filtered-drain coverage semantics, (d) fail-open-on-transient-error.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlalchemy as sa

from ne.contracts import EventLike
from ne.events import append, drain_once, read_batch
from ne.events.outbox import (
    ConsumerFilterMismatchError,
    CursorRegressionError,
    OutboxRecord,
    ack,
    ensure_outbox_schema,
    ne_outbox_cursors,
)


# --- (a) crash between read and ack redelivers the identical window ----------
def test_crash_window_redelivers_identical_records(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        for _ in range(3):
            append(conn, audit())

    seen: list[list[str]] = []

    def crashing(records: list[OutboxRecord]) -> None:
        seen.append([r.event_id for r in records])
        raise RuntimeError("exporter crashed mid-flight")

    first = drain_once(engine, "exp", crashing)
    assert first.processed == 0
    assert first.error is not None
    assert first.acked_to == 0  # cursor NOT advanced

    # A second drain sees the IDENTICAL records (at-least-once).
    second = drain_once(engine, "exp", crashing)
    assert second.acked_to == 0
    assert seen[0] == seen[1]


# --- (b) filter is immutable per consumer ------------------------------------
def test_widening_filter_raises_and_directs_to_new_consumer(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        append(conn, audit())
    with engine.begin() as conn:
        read_batch(conn, "c", event_types=["audit"])  # registers filter

    with engine.begin() as conn, pytest.raises(ConsumerFilterMismatchError) as ei:
        read_batch(conn, "c", event_types=["audit", "cost"])
    assert "new consumer" in str(ei.value)


def test_dropping_filter_to_none_also_raises(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        append(conn, audit())
    with engine.begin() as conn:
        read_batch(conn, "c", event_types=["audit"])
    with engine.begin() as conn, pytest.raises(ConsumerFilterMismatchError):
        read_batch(conn, "c", event_types=None)


def test_new_consumer_name_sees_full_stream_from_zero(
    engine: sa.Engine, audit: Callable[..., EventLike], cost: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        append(conn, audit())
        append(conn, cost())
        append(conn, audit())
    with engine.begin() as conn:
        recs, hw = read_batch(conn, "brand-new")
    assert [r.id for r in recs] == [1, 2, 3]
    assert hw == 3


# --- (c) filtered drain advances over non-matching rows ----------------------
def test_filtered_drain_skips_and_never_redelivers_nonmatching(
    engine: sa.Engine, audit: Callable[..., EventLike], cost: Callable[..., EventLike]
) -> None:
    # Interleave audit / cost rows: ids 1=audit 2=cost 3=audit 4=cost.
    with engine.begin() as conn:
        append(conn, audit())
        append(conn, cost())
        append(conn, audit())
        append(conn, cost())

    delivered: list[list[str]] = []

    def handler(records: list[OutboxRecord]) -> None:
        delivered.append([r.event_type for r in records])

    result = drain_once(engine, "audit-only", handler, event_types=["audit"])
    # Only audit rows delivered, but the cursor advanced over ALL scanned rows.
    assert delivered[0] == ["audit", "audit"]
    assert result.acked_to == 4  # past the non-matching cost rows

    # Add a fresh audit row (id 5): the earlier cost rows never redeliver.
    with engine.begin() as conn:
        append(conn, audit())
    result2 = drain_once(engine, "audit-only", handler, event_types=["audit"])
    assert result2.processed == 1
    assert delivered[1] == ["audit"]
    assert result2.acked_to == 5


# --- (d) fail-open: repeated failure then success loses/duplicates nothing ----
def test_fail_open_then_recover_advances_exactly_once(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        expected_ids = [append(conn, audit()) for _ in range(3)]

    attempts = {"n": 0}
    acked_batches: list[list[int]] = []

    def flaky(records: list[OutboxRecord]) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError(f"transient failure {attempts['n']}")
        acked_batches.append([r.id for r in records])

    r1 = drain_once(engine, "exp", flaky)
    r2 = drain_once(engine, "exp", flaky)
    r3 = drain_once(engine, "exp", flaky)

    assert r1.error is not None and r1.acked_to == 0
    assert r2.error is not None and r2.acked_to == 0
    assert r3.error is None and r3.processed == 3 and r3.acked_to == 3

    # Exactly one successful ack of all three ids; nothing lost, nothing doubled.
    assert acked_batches == [expected_ids]

    r4 = drain_once(engine, "exp", flaky)
    assert r4.processed == 0 and r4.acked_to == 3


# --- (e) ack is an atomic, monotonic conditional update (NE-070) --------------
def test_ack_is_conditional_and_monotonic(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        for _ in range(10):
            append(conn, audit())

    # Register + advance to 10.
    with engine.begin() as conn:
        read_batch(conn, "c")
        ack(conn, "c", 10)

    # Acking backwards to 5 is refused (unambiguous regression).
    with engine.begin() as conn, pytest.raises(CursorRegressionError):
        ack(conn, "c", 5)

    # Acking 10 again is a no-op (idempotent), not a regression.
    with engine.begin() as conn:
        ack(conn, "c", 10)
        position = conn.execute(
            sa.select(ne_outbox_cursors.c.position).where(ne_outbox_cursors.c.consumer == "c")
        ).scalar_one()
    assert position == 10


# --- (f) legacy cursor tables gain the fence columns additively (NE-070) ------
def test_ensure_outbox_schema_adds_fence_columns_to_legacy_db(
    audit: Callable[..., EventLike],
) -> None:
    engine = sa.create_engine("sqlite://")
    # Build the PRE-NE-070 cursor table shape by hand (no fence_* columns), plus
    # a minimal events table so append/read still work.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE ne_outbox_cursors ("
                "consumer TEXT PRIMARY KEY, position BIGINT NOT NULL DEFAULT 0, "
                "event_types JSON, updated_at TIMESTAMP)"
            )
        )
    before = {c["name"] for c in sa.inspect(engine).get_columns("ne_outbox_cursors")}
    assert "fence_xmax" not in before and "fence_last_id" not in before

    # The additive upgrade runs inside ensure_outbox_schema (idempotent).
    ensure_outbox_schema(engine)
    after = {c["name"] for c in sa.inspect(engine).get_columns("ne_outbox_cursors")}
    assert "fence_xmax" in after and "fence_last_id" in after

    # Idempotent second call is a no-op (no duplicate-column error).
    ensure_outbox_schema(engine)

    # And the upgraded schema still drains end-to-end.
    with engine.begin() as conn:
        expected = [append(conn, audit()) for _ in range(3)]
    seen: list[list[int]] = []
    drain_once(engine, "legacy", lambda recs: seen.append([r.id for r in recs]))
    assert seen == [expected]
    engine.dispose()
