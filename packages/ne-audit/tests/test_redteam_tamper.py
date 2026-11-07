# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-REDTEAM — tamper attacks against the append-only, hash-chained audit log.

Runs against a REAL in-memory SQLite chain (the SAME SQLAlchemy statements +
append-only triggers as production). The existing suites
(``test_chain_verify.py`` / ``test_column_tamper.py`` / ``test_chain_append.py``)
already cover payload tamper, broken link, missing/deleted record, seq-swap
reorder, per-column tamper, head-anchor and idempotent replay — those are mapped in
the red-team report. This file adds only GENUINELY-NEW adversarial framings:

* backdate-then-rehash a single record so it is INTERNALLY self-consistent — caught
  only because its SUCCESSOR's forward link now dangles;
* a FORGED out-of-sequence INSERT — caught by the dense-seq check.

Each test states its MUTATION-CHECK proving non-vacuity.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import sqlalchemy as sa

from ne.audit import compute_record_hash, verify_chain
from ne.audit.store import append_record, ne_audit_log
from ne.schema import AuditEvent


def _drop_triggers(conn: sa.Connection) -> None:
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int = 6) -> None:
    with engine.begin() as conn:
        for i in range(n):
            append_record(
                conn, audit(event_id=f"e{i}", occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC))
            )


# --- (a) backdate + self-rehash a record: caught by the SUCCESSOR's link -------


def test_backdate_then_rehash_single_record_caught_by_successor_link(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """An attacker backdates a middle record's ``occurred_at`` in BOTH the payload
    JSON AND its denormalized column, then recomputes THAT record's own hash so it
    is internally self-consistent (self-hash ok, prev-link ok, columns ok). The
    forgery is still caught: the SUCCESSOR's ``prev_hash`` now references the
    record's OLD hash, so the forward link breaks at ``seq+1``.

    MUTATION-CHECK: a per-record-only integrity check (recompute each row's own hash
    but NOT verify each ``prev_hash`` equals the predecessor's ``record_hash`` — i.e.
    dropping the "chain link broken" check in ne/audit/verify.py:329-330) would miss
    this entirely.
    """
    _seed(engine, audit, n=6)

    # A self-consistent, backdated replacement for the seq-3 record (event_id "e2").
    backdated = audit(event_id="e2", occurred_at=datetime(2020, 1, 1, tzinfo=UTC))
    new_payload = backdated.model_dump(mode="json")

    with engine.connect() as conn:
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 3)).one()
    prev_hash = row.prev_hash
    new_hash = compute_record_hash(prev_hash, new_payload)

    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 3)
            .values(
                payload=new_payload,
                occurred_at=backdated.occurred_at,
                record_hash=new_hash,
                prev_hash=prev_hash,
            )
        )

    cv = verify_chain(engine)
    assert not cv.valid
    # The seq-3 record itself is internally consistent; the break surfaces at its
    # successor whose prev_hash still points at the pre-backdate hash.
    assert cv.first_break_seq == 4
    assert cv.first_break_event_id == "e3"
    assert cv.reason == "chain link broken"


# --- (b) forged out-of-sequence INSERT: caught by the dense-seq check ----------


def test_forged_record_insertion_breaks_dense_seq(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """The append-only triggers block UPDATE/DELETE but not INSERT, so an attacker
    with table privileges can raw-INSERT a fabricated, self-consistent record. Doing
    so out of dense sequence (seq 8 while the head is at 6, leaving a gap at 7) is
    caught.

    NE-AUDIT-VERIFY-RACE reporting delta: pre-fix, ``verify_chain`` walked the
    WHOLE table unbounded (no head anchor on the aggregate) and its dense-seq
    check fired first, reporting ``seq 7`` / ``missing record (sequence gap)``.
    Post-fix, the walk is bounded to ``seq <= head_seq`` (head read first, as-of-
    head semantics — see ``ne/audit/verify.py`` module docstring), so the walk
    over 1..6 completes cleanly (the forged seq-8 row, being beyond the head
    snapshot, is invisible to it) and the race-free beyond-head epilogue
    (:func:`ne.audit.verify._beyond_head_break`) catches it instead, reporting
    the row it actually found: ``seq 8`` / ``event_id forged-injected`` /
    ``head mismatch (records beyond head)``. The row is still found because the
    epilogue's ``max(seq)`` scan is over the WHOLE table (unbounded), same as
    before — only the ANCHOR for the primary walk changed.

    MUTATION-CHECK: neutering :func:`ne.audit.verify._beyond_head_break` (making
    it return ``None`` unconditionally — mutation M3 in the unit's SPEC §6.1
    evidence) admits the forged insert SILENTLY: the bounded walk over 1..6 is
    clean, and with the epilogue gone nothing else looks past the head, so
    ``verify_chain`` returns ``valid`` (this assertion goes red). The
    dense-sequence check ITSELF (``seq != expected_seq`` in
    ``ne.audit.verify._walk_rows``) remains pinned by
    ``test_chain_verify.py::test_missing_record_detected`` — a real interior
    gap, not a beyond-head insert, is what exercises that branch now.
    """
    _seed(engine, audit, n=6)

    # Chain the forgery onto the real head (seq 6) so the missing seq 7 is the sole
    # defect: no chain-link break, no tamper, no head mismatch to mask it.
    with engine.begin() as conn:
        head_hash = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 6)
        ).scalar_one()

    forged = audit(event_id="forged-injected", occurred_at=datetime(2026, 3, 20, tzinfo=UTC))
    payload = forged.model_dump(mode="json")
    prev = head_hash
    record_hash = compute_record_hash(prev, payload)

    with engine.begin() as conn:
        conn.execute(
            ne_audit_log.insert().values(
                seq=8,  # skips seq 7 -> not a dense 1..head run
                event_id="forged-injected",
                event_type=payload["event_type"],
                config_hash=payload["config_hash"],
                actor=payload["actor"],
                occurred_at=forged.occurred_at,
                action=payload.get("action"),
                resource=payload.get("resource"),
                outcome=payload.get("outcome"),
                payload=payload,
                prev_hash=prev,
                record_hash=record_hash,
            )
        )

    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 8
    assert cv.first_break_event_id == "forged-injected"
    assert cv.reason == "head mismatch (records beyond head)"
