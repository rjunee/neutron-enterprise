# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Denormalized-column tamper detection + payload-authoritative reads.

The chain hash covers only the JSON ``payload``, NOT the denormalized query columns
(``config_hash``, ``actor``, ``occurred_at``, …) that exist for fast indexed WHERE
filtering. Swapping a column value WITHOUT touching the payload therefore leaves the
per-record hash and the chain links intact — the hash-only checks miss it. These
tests pin the cross-check that closes that gap: :func:`verify_chain` now compares
every denormalized column against the hash-covered payload and names the tampered
column, and replay / append's idempotent return read every value FROM the payload,
so a tampered column can never surface forged content.

Test design informed by ABI versionstore test suite (MIT, Copyright (C) 2025
Naas.ai) — shapes only, no code copied.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.audit import verify_chain
from ne.audit.replay import replay
from ne.audit.store import append_record, ne_audit_log
from ne.schema import AuditEvent

# make_audit's fixed values (conftest.HEX_HASH / actor / outcome), asserted as the
# authoritative payload values that survive a column tamper.
_PAYLOAD_CONFIG_HASH = "a" * 64
_PAYLOAD_ACTOR = "sys"
_PAYLOAD_OUTCOME = "success"


def _drop_triggers(conn: sa.Connection) -> None:
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int = 6) -> list[str]:
    """Seed ``n`` events on one shared trace so replay-by-trace pivots across them."""
    ids = []
    with engine.begin() as conn:
        for i in range(n):
            ev = audit(
                event_id=f"e{i}",
                trace_id="tr-1",
                occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC),
            )
            append_record(conn, ev)
            ids.append(f"e{i}")
    return ids


def _tamper(engine: sa.Engine, *, seq: int, **values: object) -> None:
    """Direct column UPDATE (payload untouched) after dropping the append-only triggers."""
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == seq).values(**values))


def test_config_hash_column_tamper_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    _tamper(engine, seq=3, config_hash="b" * 64)
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.first_break_event_id == "e2"
    # NOT "record tampered" — proves the hash-only check missed the column swap.
    assert cv.reason == "column/payload mismatch: config_hash"
    assert "config_hash" in cv.detail
    assert "e2" in cv.detail


def test_actor_column_tamper_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    _tamper(engine, seq=3, actor="mallory")
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == "column/payload mismatch: actor"
    assert "actor" in cv.detail


def test_occurred_at_column_tamper_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    # Exercises naive-SQLite-column vs 'Z'-suffixed-payload normalization.
    _tamper(engine, seq=3, occurred_at=datetime(2027, 1, 1, tzinfo=UTC))
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == "column/payload mismatch: occurred_at"
    assert "occurred_at" in cv.detail


@pytest.mark.parametrize(
    ("column", "forged"),
    [
        ("event_id", "forged-id"),  # unique constraint — must stay unique
        ("event_type", "forged-value"),
        ("config_hash", "forged-value"),
        ("actor", "forged-value"),
        ("kpi_tag", "forged-value"),  # NULL column / None payload → value tamper
        ("trace_id", "forged-value"),
        ("action", "forged-value"),
        ("resource", "forged-value"),
        ("outcome", "forged-value"),
    ],
)
def test_every_text_column_tamper_detected(
    engine: sa.Engine,
    audit: Callable[..., AuditEvent],
    column: str,
    forged: str,
) -> None:
    _seed(engine, audit, n=6)
    _tamper(engine, seq=3, **{column: forged})
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == f"column/payload mismatch: {column}"
    assert column in cv.detail


def test_replay_returns_payload_values_despite_column_tamper(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    _tamper(
        engine,
        seq=3,
        config_hash="b" * 64,
        actor="mallory",
        outcome="failure",
    )

    for result in (replay(engine, trace_id="tr-1"), replay(engine, event_id="e2")):
        by_id = {r.event_id: r for r in result.events}
        rec = by_id["e2"]
        # Returned values derive from the hash-covered payload, never the columns.
        assert rec.config_hash == _PAYLOAD_CONFIG_HASH
        assert rec.actor == _PAYLOAD_ACTOR
        assert rec.outcome == _PAYLOAD_OUTCOME
        assert result.config_hashes == (_PAYLOAD_CONFIG_HASH,)


def test_replay_occurred_at_is_tz_aware_datetime(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    result = replay(engine, event_id="e2")
    rec = {r.event_id: r for r in result.events}["e2"]
    assert isinstance(rec.occurred_at, datetime)
    # e2 was seeded at 2026-03-3 (i+1 for i=2).
    assert rec.occurred_at == datetime(2026, 3, 3, tzinfo=UTC)
    assert rec.occurred_at.isoformat()  # protects the ne-cli replay printer


def test_append_idempotent_return_is_payload_authoritative(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    _tamper(engine, seq=3, actor="mallory")
    # Re-append the SAME event_id → idempotent return of the existing (tampered) row.
    with engine.begin() as conn:
        rec = append_record(
            conn,
            audit(
                event_id="e2",
                trace_id="tr-1",
                occurred_at=datetime(2026, 3, 3, tzinfo=UTC),
            ),
        )
    assert rec.actor == _PAYLOAD_ACTOR  # from payload, not the forged column


def test_windowed_verify_detects_column_tamper(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    _tamper(engine, seq=3, resource="forged-value")
    cv = verify_chain(
        engine,
        start=datetime(2026, 3, 2, tzinfo=UTC),
        end=datetime(2026, 3, 4, tzinfo=UTC),
    )
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == "column/payload mismatch: resource"
