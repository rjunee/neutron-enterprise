# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""prune_acked: bounded retention that never drops undelivered events (P2-21).

Deletes only rows at/below MIN(cursor position) AND older than the retention window;
an unacked row (beyond every cursor) or an acked-but-recent row both survive; zero
consumers ⇒ zero deletions; delivered/cursor semantics are unchanged after a prune.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from ne.contracts import EventLike
from ne.events import ack, append, drain_once, prune_acked, read_batch
from ne.events.outbox import ne_outbox_events

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
_OLD = _NOW - timedelta(days=10)
_RECENT = _NOW - timedelta(days=1)
_WINDOW = 604800.0  # 7 days


def _count(engine: sa.Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(sa.select(sa.func.count()).select_from(ne_outbox_events)).scalar_one()
        )


def _append(engine: sa.Engine, audit: Callable[..., EventLike], when: datetime) -> int:
    with engine.begin() as conn:
        return append(conn, audit(occurred_at=when))


# --- (1) prune removes acked rows past the retention window ------------------


def test_prune_removes_acked_rows_past_retention(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    for _ in range(3):
        _append(engine, audit, _OLD)
    drain_once(engine, "c", lambda _records: None)  # advance the cursor to 3

    deleted = prune_acked(engine, older_than_s=_WINDOW, now=_NOW)
    assert deleted == 3
    assert _count(engine) == 0


# --- (2) unacked (beyond cursor) and acked-but-recent rows survive -----------


def test_prune_keeps_unacked_and_recent(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _append(engine, audit, _OLD)  # id 1 — acked + old  → pruned
    _append(engine, audit, _RECENT)  # id 2 — acked + recent → kept
    _append(engine, audit, _OLD)  # id 3 — UNACKED (beyond cursor) → kept
    with engine.begin() as conn:
        ack(conn, "c", 2)  # cursor at 2 (registers the consumer)

    deleted = prune_acked(engine, older_than_s=_WINDOW, now=_NOW)
    assert deleted == 1
    assert _count(engine) == 2  # the recent + the unacked row both survive


# --- (3) zero consumers ⇒ prune nothing (never drop undelivered events) ------


def test_prune_noop_without_consumers(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    for _ in range(3):
        _append(engine, audit, _OLD)

    deleted = prune_acked(engine, older_than_s=_WINDOW, now=_NOW)
    assert deleted == 0
    assert _count(engine) == 3


# --- (4) pruned ids never redeliver; cursor semantics intact ----------------


def test_pruned_ids_never_redeliver(engine: sa.Engine, audit: Callable[..., EventLike]) -> None:
    _append(engine, audit, _OLD)  # id 1
    _append(engine, audit, _OLD)  # id 2
    drain_once(engine, "c", lambda _records: None)  # cursor → 2
    r3 = _append(engine, audit, _RECENT)  # id 3 (above the cursor)

    deleted = prune_acked(engine, older_than_s=_WINDOW, now=_NOW)
    assert deleted == 2

    with engine.begin() as conn:
        records, high_water = read_batch(conn, "c")
    assert [rec.id for rec in records] == [r3]
    assert high_water == r3
