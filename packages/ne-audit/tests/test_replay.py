# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Replay query API — "why did the system say X on date D?"."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.audit import append_record, replay
from ne.schema import AuditEvent


def _seed_trace(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn:
        append_record(
            conn,
            audit(event_id="q1", trace_id="tr-1", resource="urn:answer:42"),
        )
        append_record(
            conn,
            audit(
                event_id="q2",
                trace_id="tr-1",
                resource="urn:answer:42",
                provenance={"context_ids": "chunk-1,chunk-7,chunk-9"},
            ),
        )
        append_record(
            conn,
            audit(event_id="q3", trace_id="tr-1", resource="urn:answer:42"),
        )
        # An unrelated trace that must NOT appear.
        append_record(conn, audit(event_id="z1", trace_id="tr-2"))


def test_replay_by_trace(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed_trace(engine, audit)
    rr = replay(engine, trace_id="tr-1")
    assert [e.event_id for e in rr.events] == ["q1", "q2", "q3"]
    assert rr.config_hashes == ("a" * 64,)
    assert rr.context_ids == ("chunk-1", "chunk-7", "chunk-9")


def test_replay_by_event_id_pivots_to_trace(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed_trace(engine, audit)
    rr = replay(engine, event_id="q1")
    assert [e.event_id for e in rr.events] == ["q1", "q2", "q3"]
    assert rr.context_ids == ("chunk-1", "chunk-7", "chunk-9")


def test_replay_event_id_without_trace(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn:
        append_record(conn, audit(event_id="solo", trace_id=None))
    rr = replay(engine, event_id="solo")
    assert [e.event_id for e in rr.events] == ["solo"]


def test_replay_by_resource_and_window(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn:
        for i in range(5):
            append_record(
                conn,
                audit(
                    event_id=f"r{i}",
                    resource="urn:doc:7",
                    occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC),
                ),
            )
    rr = replay(
        engine,
        resource="urn:doc:7",
        start=datetime(2026, 3, 2, tzinfo=UTC),
        end=datetime(2026, 3, 4, tzinfo=UTC),
    )
    assert [e.event_id for e in rr.events] == ["r1", "r2", "r3"]


def test_replay_reports_multiple_config_hashes(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    with engine.begin() as conn:
        append_record(conn, audit(event_id="c1", trace_id="tr", config_hash="b" * 64))
        append_record(conn, audit(event_id="c2", trace_id="tr", config_hash="a" * 64))
    rr = replay(engine, trace_id="tr")
    assert rr.config_hashes == ("a" * 64, "b" * 64)  # sorted-unique


def test_replay_parses_provenance_entry_shape(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    with engine.begin() as conn:
        append_record(
            conn,
            audit(
                event_id="pe1",
                trace_id="tr-pe",
                provenance={"context_ids": {"key": "context_ids", "value": "chunk-2,chunk-3"}},
            ),
        )
    rr = replay(engine, trace_id="tr-pe")
    assert rr.context_ids == ("chunk-2", "chunk-3")


def test_replay_no_selector_raises(engine: sa.Engine) -> None:
    with pytest.raises(ValueError, match="at least one selector"):
        replay(engine)


def test_replay_empty_selection(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed_trace(engine, audit)
    rr = replay(engine, trace_id="does-not-exist")
    assert rr.events == ()
    assert rr.config_hashes == ()
    assert rr.context_ids == ()
    # An unknown event_id also yields an empty result (not an error).
    assert replay(engine, event_id="nope").events == ()
