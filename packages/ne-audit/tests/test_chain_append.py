# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chain append semantics: genesis, dense seq, idempotency, denormalized columns."""

from __future__ import annotations

import itertools
from collections.abc import Callable

import pytest
import sqlalchemy as sa

from ne.audit import GENESIS_HASH, append_record, canonical_bytes, compute_record_hash
from ne.audit.store import ne_audit_head, ne_audit_log
from ne.schema import AuditEvent, CostEvent


def test_genesis_head_row(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        head = conn.execute(
            sa.select(ne_audit_head.c.seq, ne_audit_head.c.record_hash).where(
                ne_audit_head.c.id == 1
            )
        ).one()
    assert head.seq == 0
    assert head.record_hash == GENESIS_HASH


def test_first_append_links_to_genesis(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn:
        rec = append_record(conn, audit(event_id="first"))
    assert rec.seq == 1
    assert rec.prev_hash == GENESIS_HASH
    assert rec.record_hash == compute_record_hash(GENESIS_HASH, rec.payload)


def test_dense_chain_links(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    recs = []
    with engine.begin() as conn:
        for i in range(10):
            recs.append(append_record(conn, audit(event_id=f"e{i}")))
    for i, rec in enumerate(recs, start=1):
        assert rec.seq == i
    for prev, cur in itertools.pairwise(recs):
        assert cur.prev_hash == prev.record_hash


def test_canonical_bytes_key_order_insensitive() -> None:
    a = {"z": 1, "a": 2, "m": {"y": 3, "x": 4}}
    b = {"a": 2, "m": {"x": 4, "y": 3}, "z": 1}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert compute_record_hash(GENESIS_HASH, a) == compute_record_hash(GENESIS_HASH, b)


def test_duplicate_event_id_is_idempotent(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    with engine.begin() as conn:
        first = append_record(conn, audit(event_id="dup"))
        append_record(conn, audit(event_id="other"))
        again = append_record(conn, audit(event_id="dup"))
    assert again.seq == first.seq
    assert again.record_hash == first.record_hash
    with engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(ne_audit_log)).scalar_one()
        head_seq = conn.execute(
            sa.select(ne_audit_head.c.seq).where(ne_audit_head.c.id == 1)
        ).scalar_one()
    assert count == 2
    assert head_seq == 2


def test_non_basemodel_raises(engine: sa.Engine) -> None:
    with engine.begin() as conn, pytest.raises(TypeError):
        append_record(conn, {"event_id": "x"})  # type: ignore[arg-type]


def test_empty_event_id_raises(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    with engine.begin() as conn, pytest.raises(TypeError):
        append_record(conn, audit(event_id=""))


def test_denormalized_columns_for_audit(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    with engine.begin() as conn:
        append_record(
            conn,
            audit(event_id="a1", kpi_tag="kpi-x", trace_id="tr-1"),
        )
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.event_id == "a1")).one()
    assert row.event_type == "audit"
    assert row.config_hash == "a" * 64
    assert row.actor == "sys"
    assert row.action == "tool.invoke"
    assert row.resource == "urn:tool:search"
    assert row.outcome == "success"
    assert row.kpi_tag == "kpi-x"
    assert row.trace_id == "tr-1"


def test_cost_event_appends_with_null_action_columns(
    engine: sa.Engine, cost: Callable[..., CostEvent]
) -> None:
    with engine.begin() as conn:
        rec = append_record(conn, cost(event_id="c1"))
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.event_id == "c1")).one()
    assert rec.event_type == "cost"
    assert row.action is None
    assert row.resource is None
    assert row.outcome is None
