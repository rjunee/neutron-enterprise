# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""read_batch / ack / drain_once: happy path, per-consumer cursors, guards."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlalchemy as sa

from ne.contracts import EventLike
from ne.events import ack, append, drain_once, read_batch
from ne.events.outbox import CursorRegressionError, OutboxRecord


def _seed(engine: sa.Engine, audit: Callable[..., EventLike], n: int) -> list[int]:
    with engine.begin() as conn:
        return [append(conn, audit()) for _ in range(n)]


def test_read_process_ack_happy_path(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _seed(engine, audit, 3)
    with engine.begin() as conn:
        records, high_water = read_batch(conn, "c1")
        assert [r.id for r in records] == [1, 2, 3]
        assert high_water == 3
        ack(conn, "c1", high_water)
    # After ack the window is empty.
    with engine.begin() as conn:
        records2, hw2 = read_batch(conn, "c1")
        assert records2 == []
        assert hw2 == 3


def test_records_carry_full_projection(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _seed(engine, audit, 1)
    with engine.begin() as conn:
        records, _ = read_batch(conn, "c")
    rec = records[0]
    assert isinstance(rec, OutboxRecord)
    assert rec.event_type == "audit"
    assert rec.config_hash == "a" * 64
    assert rec.payload["action"] == "tool.invoke"


def test_independent_per_consumer_cursors(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    _seed(engine, audit, 3)
    with engine.begin() as conn:
        recs_a, hw_a = read_batch(conn, "a")
        ack(conn, "a", hw_a)
    # Consumer b still sees the whole stream — cursors are independent.
    with engine.begin() as conn:
        recs_b, _hw_b = read_batch(conn, "b")
    assert [r.id for r in recs_a] == [1, 2, 3]
    assert [r.id for r in recs_b] == [1, 2, 3]


def test_ack_monotonic_guard(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _seed(engine, audit, 3)
    with engine.begin() as conn:
        _, hw = read_batch(conn, "c")
        ack(conn, "c", hw)  # -> 3
    with engine.begin() as conn, pytest.raises(CursorRegressionError):
        ack(conn, "c", 1)  # below current position


def test_ack_equal_is_noop(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _seed(engine, audit, 2)
    with engine.begin() as conn:
        _, hw = read_batch(conn, "c")
        ack(conn, "c", hw)
        ack(conn, "c", hw)  # equal -> no error, no movement
    with engine.begin() as conn:
        recs, _ = read_batch(conn, "c")
    assert recs == []


def test_empty_window_returns_position(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        records, hw = read_batch(conn, "fresh")
    assert records == []
    assert hw == 0


def test_limit_windows_the_scan(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _seed(engine, audit, 5)
    with engine.begin() as conn:
        first, hw1 = read_batch(conn, "c", limit=2)
        assert [r.id for r in first] == [1, 2]
        assert hw1 == 2
        ack(conn, "c", hw1)
        second, hw2 = read_batch(conn, "c", limit=2)
        assert [r.id for r in second] == [3, 4]
        assert hw2 == 4


def test_drain_once_advances_to_high_water(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    _seed(engine, audit, 4)
    seen: list[list[OutboxRecord]] = []
    result = drain_once(engine, "exp", seen.append)
    assert result.processed == 4
    assert result.acked_to == 4
    assert result.error is None
    # Cursor advanced exactly to high_water: a second drain sees nothing.
    result2 = drain_once(engine, "exp", seen.append)
    assert result2.processed == 0
    assert result2.acked_to == 4
    assert [r.id for r in seen[0]] == [1, 2, 3, 4]
