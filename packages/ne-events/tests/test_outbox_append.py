# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""append(): monotonic ids, SAVEPOINT-idempotent duplicates, payload round-trip."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import ClassVar

import pytest
import sqlalchemy as sa

from ne.contracts import EventLike
from ne.events import append
from ne.events.outbox import ne_outbox_events


def test_append_returns_increasing_ids_in_order(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    with engine.begin() as conn:
        ids = [append(conn, audit()) for _ in range(5)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5


def test_duplicate_event_id_is_idempotent_and_tx_stays_usable(
    engine: sa.Engine, audit: Callable[..., EventLike]
) -> None:
    dup = audit(event_id="ev-dup")
    with engine.begin() as conn:
        first = append(conn, dup)
        again = append(conn, dup)  # unique violation -> SAVEPOINT rollback
        # The enclosing transaction must still be usable after the conflict:
        after = append(conn, audit(event_id="ev-after"))
    assert first == again
    assert after != first

    with engine.connect() as conn:
        rows = (
            conn.execute(sa.select(ne_outbox_events.c.event_id).order_by(ne_outbox_events.c.id))
            .scalars()
            .all()
        )
    assert rows.count("ev-dup") == 1  # exactly one row for the duplicated id
    assert "ev-after" in rows


def test_non_pydantic_event_raises_type_error(engine: sa.Engine) -> None:
    class PlainEvent:
        """Structurally EventLike but NOT a pydantic model."""

        event_id = "plain-1"
        event_type = "audit"
        config_hash = "a" * 64
        kpi_tag = None
        actor = "sys"
        provenance: ClassVar[dict[str, str]] = {}
        occurred_at = datetime.now(UTC)

    plain = PlainEvent()
    assert isinstance(plain, EventLike)
    with engine.begin() as conn, pytest.raises(TypeError):
        append(conn, plain)  # type: ignore[arg-type]


def test_payload_round_trips_all_fields(
    engine: sa.Engine, audit: Callable[..., EventLike], utc: Callable[[datetime], datetime]
) -> None:
    ev = audit(
        event_id="ev-rt",
        kpi_tag=None,
        provenance={"source": "runtime", "commit": "abc"},
    )
    with engine.begin() as conn:
        append(conn, ev)
        row = conn.execute(
            sa.select(ne_outbox_events).where(ne_outbox_events.c.event_id == "ev-rt")
        ).one()
    assert row.event_type == "audit"
    assert row.config_hash == "a" * 64
    assert row.kpi_tag is None
    assert row.actor == "sys"
    assert utc(row.occurred_at) == utc(ev.occurred_at)
    assert row.payload["provenance"] == {"source": "runtime", "commit": "abc"}
    assert row.payload["event_id"] == "ev-rt"
