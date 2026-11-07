# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKABLE Postgres acceptance for the audit chain.

Env-gated on ``NE_TEST_POSTGRES_URL``. The build host has no Docker daemon, so
this module auto-skips (pending-live-verification ledger). When a real Postgres
endpoint is configured it re-runs the behaviors whose Postgres semantics differ
from SQLite: DB-level append-only enforcement (UPDATE/DELETE/TRUNCATE), FOR
UPDATE head-lock serialization under real threads, and JSONB canonical-hash
stability. pgAudit proofs have been PROMOTED out of this module: they now live
in ``test_pgaudit_live.py``, gated on ``NE_TEST_PGAUDIT_URL``, and must-run
(not nested-skip) in CI's live-postgres job against a pgAudit-capable image.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DatabaseError

import ne.audit.engine
from ne.audit import append_record, ensure_transactional_sqlite, verify_chain
from ne.audit.replay import replay
from ne.audit.store import ensure_audit_schema, metadata, ne_audit_log
from ne.schema import AuditEvent, AuditOutcome, CostEvent

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
        provenance={"source": "pg-test"},
        occurred_at=datetime.now(UTC),
        action="a",
        resource="r",
        outcome=AuditOutcome.success,
    )


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL)
    metadata.drop_all(engine)
    ensure_audit_schema(engine)
    try:
        yield engine
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def test_append_only_update_delete_truncate(pg_engine: sa.Engine) -> None:
    with pg_engine.begin() as conn:
        append_record(conn, _audit(event_id="pg-1"))
    for stmt in (
        ne_audit_log.update().values(actor="attacker"),
        ne_audit_log.delete(),
    ):
        with pytest.raises(DatabaseError), pg_engine.begin() as conn:
            conn.execute(stmt)
    with pytest.raises(DatabaseError), pg_engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE ne_audit_log")


def test_head_lock_serializes_concurrent_threads(pg_engine: sa.Engine) -> None:
    barrier = threading.Barrier(4)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(10):
            with pg_engine.begin() as conn:
                append_record(conn, _audit(event_id=f"t{tid}-{i}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with pg_engine.connect() as conn:
        seqs = (
            conn.execute(sa.select(ne_audit_log.c.seq).order_by(ne_audit_log.c.seq)).scalars().all()
        )
    assert seqs == list(range(1, 41))
    assert verify_chain(pg_engine).valid


def test_jsonb_canonical_hash_stability(pg_engine: sa.Engine) -> None:
    cost = CostEvent(
        event_id="pg-cost",
        event_type="cost",
        config_hash=_HASH,
        actor="sys",
        provenance={},
        occurred_at=datetime.now(UTC),
        model="m",
        lane="frontier-api",
        cost_usd=1.5,
    )
    with pg_engine.begin() as conn:
        append_record(conn, cost)
    assert verify_chain(pg_engine).valid


def test_column_tamper_detected_and_replay_authoritative(pg_engine: sa.Engine) -> None:
    for i in range(3):
        with pg_engine.begin() as conn:
            append_record(conn, _audit(event_id=f"col-{i}"))
    # Drop the append-only trigger so we can forge a denormalized column directly.
    with pg_engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_append_only ON ne_audit_log")
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.event_id == "col-1")
            .values(config_hash="d" * 64)
        )
    cv = verify_chain(pg_engine)
    assert not cv.valid
    assert cv.reason == "column/payload mismatch: config_hash"
    # Replay returns the ORIGINAL payload config_hash, never the forged column.
    result = replay(pg_engine, event_id="col-1")
    rec = {r.event_id: r for r in result.events}["col-1"]
    assert rec.config_hash == _HASH


def test_transactional_sqlite_helper_noop_on_postgres(pg_engine: sa.Engine) -> None:
    """NE-CLEAN2: the pysqlite recipe is a hard no-op on a REAL Postgres engine.

    The helper must neither track nor patch a Postgres engine (dialect guard), and
    Postgres' native nested transactions already undo a released SAVEPOINT's writes
    on an outer rollback — so the stranding gap never existed here.
    """
    ensure_transactional_sqlite(pg_engine)
    assert pg_engine not in ne.audit.engine._patched_engines

    # Native savepoint + outer-rollback semantics: the inner write is undone.
    probe = sa.Table(
        "ne_clean2_pg_probe",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
    )
    probe.create(pg_engine)
    try:

        class _Boom(Exception):
            pass

        try:
            with pg_engine.begin() as conn:
                with conn.begin_nested():
                    conn.execute(probe.insert().values(id=1))
                raise _Boom()
        except _Boom:
            pass
        with pg_engine.connect() as conn:
            count = conn.execute(sa.select(sa.func.count()).select_from(probe)).scalar_one()
        assert count == 0
    finally:
        probe.drop(pg_engine)
