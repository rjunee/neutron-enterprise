# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-AUDIT-VERIFY-RACE — the verifier must not race itself under concurrent writes.

**The bug:** ``verify_chain``'s unbounded path read the chain head AFTER scanning
rows. If an ``append_record`` landed in the gap between the row scan and that
final head read, ``head_seq`` exceeded the scanned max and the verifier reported
a false ``head mismatch (truncated or forged tail)`` — a genuine tamper alarm
firing on nothing but ordinary concurrent write traffic. The bounded path had the
identical race class in its density pre-check.

**The vacuity risk, stated plainly:** a test that appends a record and THEN calls
``verify_chain`` proves nothing about this bug — that is simply "verify after a
write", which was never broken. The race needs a REAL interleaving: an append
that lands *between* the verifier's own statements, while the verifier is
mid-flight. Four of the tests below achieve that with a deterministic seam — a
SQLAlchemy ``before_cursor_execute`` listener on the VERIFIER's own engine that
performs a genuine ``append_record`` (via a second engine) immediately before a
specific statement the verifier itself is about to run (the head read, the
bounded density scan, or the beyond-head epilogue's own ``max(seq)`` read).
That is a true mid-verify interleaving, not append-before-verify, and
``assert fired`` on each test proves the interleaving actually happened — a
seam test that never fires proves nothing either. A fifth test
(``test_full_verify_stays_valid_under_true_concurrent_appends``) adds a real
background writer thread as probabilistic belt-and-braces.

**Why these tests build PLAIN engines, never the ``conftest.file_engine``
fixture:** ``file_engine`` (via ``_make_serialized``) installs a SQLAlchemy
``"begin"`` listener that issues ``BEGIN IMMEDIATE`` on every transaction, taking
SQLite's write lock up front and holding a WAL snapshot for the *whole*
transaction. If the VERIFIER ran on such an engine, its entire ``verify_chain``
call would execute inside one locked, snapshotted transaction — no concurrent
writer could ever land a commit "mid-verify" from the verifier's point of view,
and every seam/threaded test below would pass even against the UNPATCHED,
racy code. That would be a masked race: green tests, real bug. Production
Postgres runs READ COMMITTED, where each statement on a connection sees the
latest committed data as of THAT statement — that is what actually reproduced
this bug live, and it is what the plain engines below (WAL + ``busy_timeout``
only, no ``BEGIN IMMEDIATE``, no ``isolation_level=None``) emulate: every SELECT
issued by the verifier sees whatever the writer has committed by that instant,
not a snapshot frozen at transaction start. The negative-control tests further
down deliberately use the ordinary ``engine``/``audit`` fixtures instead, since
they are not testing concurrency at all.

Per STEP 4 of the build brief, this file's seam and threaded tests were run
against the UNPATCHED ``verify_chain`` before the fix was written, and the
verbatim RED output is recorded in the builder report — that observation is the
load-bearing proof the guard actually detects the race it claims to detect.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event

from ne.audit import append_record, compute_record_hash, ensure_audit_schema, verify_chain
from ne.audit.store import GENESIS_HASH, ne_audit_head, ne_audit_log
from ne.schema import AuditEvent


def _race_engine(url: str) -> sa.Engine:
    """A plain file-backed SQLite engine — WAL + busy_timeout, NO write-lock hook.

    Deliberately NOT ``conftest._make_serialized``/``file_engine``: no
    ``isolation_level = None`` override and no ``"begin"`` -> ``BEGIN IMMEDIATE``
    listener. Every statement on this engine autocommits its own implicit
    transaction (pysqlite's default), so each SELECT sees whatever the OTHER
    engine has committed as of that instant — the per-statement READ COMMITTED
    visibility this bug needs to reproduce (see module docstring).
    """
    eng = sa.create_engine(url)

    @event.listens_for(eng, "connect")
    def _on_connect(dbapi_conn: Any, _rec: Any) -> None:  # pragma: no cover - driver glue
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return eng


def _drop_triggers(conn: sa.Connection) -> None:
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


def _seed(writer_eng: sa.Engine, audit: Callable[..., AuditEvent], n: int) -> list[str]:
    ids = []
    for i in range(n):
        with writer_eng.begin() as conn:
            append_record(conn, audit(event_id=f"e{i}"))
        ids.append(f"e{i}")
    return ids


def _seed_distinct_days(
    writer_eng: sa.Engine, audit: Callable[..., AuditEvent], n: int
) -> list[str]:
    """Like :func:`_seed`, but each record gets its own ``occurred_at`` day.

    Lets a test build a genuinely narrow-but-nonempty ``occurred_at`` window
    (e.g. "day 1 only" -> seq 1 alone) instead of one that merely happens to
    be empty (every :func:`_seed` row shares the same ``occurred_at``, so any
    sub-range narrower than the whole set matches nothing).
    """
    ids = []
    for i in range(n):
        with writer_eng.begin() as conn:
            append_record(
                conn,
                audit(event_id=f"e{i}", occurred_at=datetime(2026, 1, i + 1, 12, 0, tzinfo=UTC)),
            )
        ids.append(f"e{i}")
    return ids


def _self_consistent_rewrite(engine: sa.Engine, seq: int, **col_overrides: object) -> None:
    """Rewrite a row's prev_hash (and/or columns) AND recompute its own record_hash
    so the row is internally self-consistent — isolating a specific anchor check.
    (Copied from ``test_chain_verify.py``'s recipe of the same name.)
    """
    with engine.connect() as conn:
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == seq)).one()
    prev_hash = str(col_overrides.pop("prev_hash", row.prev_hash))
    new_hash = compute_record_hash(prev_hash, dict(row.payload))
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == seq)
            .values(prev_hash=prev_hash, record_hash=new_hash, **col_overrides)
        )


# ---------------------------------------------------------------------------
# Deterministic seam tests — a real append injected between the verifier's own
# statements. These are the REQUIRED discriminators (deterministic, not timing
# dependent) proving the race is fixed.
# ---------------------------------------------------------------------------


def test_full_verify_valid_when_append_lands_before_head_read(
    tmp_path: Path, audit: Callable[..., AuditEvent]
) -> None:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    writer = _race_engine(url)
    ensure_audit_schema(writer)
    _seed(writer, audit, 6)

    verifier = _race_engine(url)
    fired = [False]

    @event.listens_for(verifier, "before_cursor_execute")
    def _inject_append(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if not fired[0] and "ne_audit_head" in statement:
            fired[0] = True
            with writer.begin() as wconn:
                append_record(wconn, audit(event_id="mid-verify-append"))

    try:
        cv = verify_chain(verifier)
    finally:
        event.remove(verifier, "before_cursor_execute", _inject_append)
        verifier.dispose()
        writer.dispose()

    assert fired[0], "the seam never fired — this test proved nothing about the race"
    assert cv.valid, cv.detail


def test_full_verify_valid_with_append_before_every_head_read(
    tmp_path: Path, audit: Callable[..., AuditEvent]
) -> None:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    writer = _race_engine(url)
    ensure_audit_schema(writer)
    _seed(writer, audit, 6)

    verifier = _race_engine(url)
    fired_count = [0]

    @event.listens_for(verifier, "before_cursor_execute")
    def _inject_append_every_time(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if "ne_audit_head" in statement:
            fired_count[0] += 1
            with writer.begin() as wconn:
                append_record(wconn, audit(event_id=f"mid-{fired_count[0]}"))

    try:
        cv = verify_chain(verifier)
    finally:
        event.remove(verifier, "before_cursor_execute", _inject_append_every_time)
        verifier.dispose()
        writer.dispose()

    assert fired_count[0] >= 1, "the seam never fired — this test proved nothing about the race"
    assert cv.valid, cv.detail


def test_bounded_verify_valid_when_append_lands_before_density_scan(
    tmp_path: Path, audit: Callable[..., AuditEvent]
) -> None:
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    writer = _race_engine(url)
    ensure_audit_schema(writer)
    _seed(writer, audit, 6)

    verifier = _race_engine(url)
    fired = [False]

    @event.listens_for(verifier, "before_cursor_execute")
    def _inject_append_before_density(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if not fired[0] and "count" in statement.lower() and "ne_audit_log" in statement:
            fired[0] = True
            with writer.begin() as wconn:
                append_record(wconn, audit(event_id="mid-verify-append"))

    try:
        # Seeded rows all carry the conftest default occurred_at (2026-01-01
        # 12:00), so this window covers them all.
        cv = verify_chain(
            verifier,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 12, 31, tzinfo=UTC),
        )
    finally:
        event.remove(verifier, "before_cursor_execute", _inject_append_before_density)
        verifier.dispose()
        writer.dispose()

    assert fired[0], "the seam never fired — this test proved nothing about the race"
    assert cv.valid, cv.detail


def test_beyond_head_epilogue_mx_read_before_head_reread_stays_valid(
    tmp_path: Path, audit: Callable[..., AuditEvent]
) -> None:
    """Pins ``_beyond_head_break``'s own internal read order, not just its call
    sites: ``max(seq)`` over the whole table must be read BEFORE the epilogue's
    fresh head re-read (see that function's docstring: "deliberate and the
    opposite of the bug this module fixes"). Round-2 Argus review flagged this
    as the module's newest, subtlest ordering invariant and noted it had NO
    deterministic pin -- swapping the two reads (head first, then max(seq))
    only turned the PROBABILISTIC threaded test red, which is timing-dependent
    by construction and explicitly not a required discriminator.

    The seam below fires on the epilogue's OWN ``max(seq)`` statement
    specifically -- ``"SELECT max(ne_audit_log.seq) AS max_1 FROM
    ne_audit_log"`` with no ``WHERE`` and no ``min(...)`` alongside it, which
    is unique to this one statement (the full-path prefix aggregate, the
    bounded density aggregate, and ``_seq_window`` all either filter with
    ``WHERE`` or pair ``max`` with ``min`` in the same SELECT) -- and injects a
    real append there. Under the SHIPPED (mx-then-head) order this is exactly
    the one point in the epilogue where a legitimate concurrent append can
    land without a false alarm: the append's row and its head-advance commit
    together (``append_record``, ``ne.audit.store``), so by the time the
    SUBSEQUENT head re-read runs it already reflects the new head too, and
    ``mx <= head_seq`` holds. Under the reversed (head-then-mx) order the
    IDENTICAL interleaving would false-fire: the head is snapshotted BEFORE
    the append, ``max(seq)`` read AFTER sees the new row, and a perfectly
    legitimate write gets reported as "records beyond head" -- the same bug
    class this unit exists to fix, relocated one function down.
    """
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    writer = _race_engine(url)
    ensure_audit_schema(writer)
    _seed(writer, audit, 6)

    verifier = _race_engine(url)
    fired = [False]

    @event.listens_for(verifier, "before_cursor_execute")
    def _inject_before_epilogue_mx(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if (
            not fired[0]
            and "max(ne_audit_log.seq)" in statement
            and "min(ne_audit_log.seq)" not in statement
            and "WHERE" not in statement
        ):
            fired[0] = True
            with writer.begin() as wconn:
                append_record(wconn, audit(event_id="mid-epilogue-append"))

    try:
        cv = verify_chain(verifier)
    finally:
        event.remove(verifier, "before_cursor_execute", _inject_before_epilogue_mx)
        verifier.dispose()
        writer.dispose()

    assert fired[0], "the seam never fired — this test proved nothing about the epilogue race"
    assert cv.valid, cv.detail


def test_full_verify_stays_valid_under_true_concurrent_appends(
    tmp_path: Path, audit: Callable[..., AuditEvent]
) -> None:
    """Real writer thread appending continuously while the main thread verifies.

    No timing assumptions about WHEN the interleaving lands — this belt-and-
    braces test does not itself prove the fix (a slow scheduler could
    serialize the interleaving away, which is exactly why the deterministic
    seam tests above exist as the REQUIRED discriminators), but post-fix it
    must be correct under ANY interleaving the OS/GIL happen to produce, so
    every one of the 20 iterations must stay valid. The writer loop sleeps a
    fraction of a millisecond between appends purely to bound SQLite
    ``busy_timeout`` contention on a loaded CI box (correctness here does not
    depend on the sleep's duration — only on there being SOME interleaving,
    which an unthrottled loop also provides).
    """
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    writer = _race_engine(url)
    ensure_audit_schema(writer)
    _seed(writer, audit, 6)

    verifier = _race_engine(url)
    stop = threading.Event()
    counter = [0]

    def _writer_loop() -> None:
        while not stop.is_set():
            with writer.begin() as conn:
                append_record(conn, audit(event_id=f"w-{counter[0]}"))
            counter[0] += 1
            time.sleep(0.0005)

    t = threading.Thread(target=_writer_loop, daemon=True)
    t.start()
    try:
        for _ in range(20):
            cv = verify_chain(verifier)
            assert cv.valid, cv.detail
    finally:
        stop.set()
        t.join()
        verifier.dispose()
        writer.dispose()


# ---------------------------------------------------------------------------
# Negative controls — every genuine tamper class must still break post-fix.
# A fix that makes everything pass is worse than the bug. Uses the plain
# in-memory ``engine``/``audit`` fixtures (no concurrency involved here).
# ---------------------------------------------------------------------------


def test_tampered_record_still_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, 6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 3)
            .values(payload={"event_id": "e2", "tampered": True})
        )
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.reason == "record tampered"


def test_broken_link_still_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, 6)
    _self_consistent_rewrite(engine, seq=4, prev_hash="f" * 64)
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.reason == "chain link broken"
    assert cv.first_break_seq == 4


def test_sequence_gap_still_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, 6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 3))
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == "missing record (sequence gap)"


def test_missing_head_still_fails_closed(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, 6)
    with engine.begin() as conn:
        conn.execute(ne_audit_head.delete())
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.reason is not None and "head" in cv.reason


def test_truncated_tail_still_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, 6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 6))
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.reason == "head mismatch (truncated or forged tail)"


def test_forged_record_beyond_head_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A chain-consistent forged row at seq 7 chained onto row 6, head NOT advanced.

    Recipe copied from ``test_redteam_tamper.py``'s
    ``test_forged_record_insertion_breaks_dense_seq`` (lines 115-143), but at
    seq 7 (immediately following the real head, no gap) so this pins the NEW
    race-free beyond-head epilogue specifically, not the dense-seq gap check.
    """
    _seed(engine, audit, 6)

    with engine.begin() as conn:
        head_hash = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 6)
        ).scalar_one()

    forged = audit(event_id="forged-beyond-head")
    payload = forged.model_dump(mode="json")
    prev = head_hash
    record_hash = compute_record_hash(prev, payload)

    with engine.begin() as conn:
        conn.execute(
            ne_audit_log.insert().values(
                seq=7,
                event_id="forged-beyond-head",
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
    assert cv.first_break_seq == 7
    assert cv.first_break_event_id == "forged-beyond-head"
    assert cv.reason == "head mismatch (records beyond head)"


def test_truncation_with_forged_head_hash_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Pins the ``hi != head_seq`` clause of the post-walk consistency check.

    Delete the tail row AND forge the head's ``record_hash`` to match the new
    (shorter) tail's real hash, while leaving the head's ``seq`` unmoved. A
    hash-only comparison would NOT catch this (the forged hash matches exactly);
    only the ``seq`` comparison does. Without this test, that clause has no
    dedicated RED — a latent vacuity the brief calls out explicitly.
    """
    _seed(engine, audit, 6)
    with engine.connect() as conn:
        row5_hash = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 5)
        ).scalar_one()
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 6))
        conn.execute(
            ne_audit_head.update().where(ne_audit_head.c.id == 1).values(record_hash=row5_hash)
        )
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.reason == "head mismatch (truncated or forged tail)"


def test_bounded_verify_forged_row_beyond_head_outside_window_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Bounded-path counterpart of ``test_forged_record_beyond_head_detected``.

    A forged, chain-consistent row at seq 8 (head unmoved at 6) whose
    ``occurred_at`` falls OUTSIDE the queried window — the window's own
    ``[lo, hi]`` never touches the forged row, so it must be caught by the
    GLOBAL ``_beyond_head_break`` epilogue, not by anything window-local. This
    is the exact shape of the P0 the previous review round found: bounding
    ``_density_break``'s aggregate to ``seq <= head_seq`` closed the false-alarm
    race but, without an epilogue call on the bounded success paths too, also
    made a forged row sitting outside the window invisible — a SILENT pass,
    strictly worse than the false alarm this unit set out to fix.
    """
    ids = _seed(engine, audit, 6)
    assert len(ids) == 6
    with engine.begin() as conn:
        head_hash = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 6)
        ).scalar_one()

    forged = audit(event_id="forged-outside-window")
    payload = forged.model_dump(mode="json")
    record_hash = compute_record_hash(head_hash, payload)
    with engine.begin() as conn:
        conn.execute(
            ne_audit_log.insert().values(
                seq=7,
                event_id="forged-outside-window",
                event_type=payload["event_type"],
                config_hash=payload["config_hash"],
                actor=payload["actor"],
                # Deliberately outside the [start, end] window queried below —
                # the conftest default seed occurred_at is 2026-01-01.
                occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
                action=payload.get("action"),
                resource=payload.get("resource"),
                outcome=payload.get("outcome"),
                payload=payload,
                prev_hash=head_hash,
                record_hash=record_hash,
            )
        )

    cv = verify_chain(
        engine,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert not cv.valid, f"forged row beyond head, outside the window, went undetected: {cv}"
    assert cv.first_break_seq == 7
    assert cv.first_break_event_id == "forged-outside-window"
    assert cv.reason == "head mismatch (records beyond head)"


def test_bounded_verify_head_rewind_empty_window_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Bounded-path counterpart of ``test_head_rewound_to_genesis_with_rows_detected``.

    ``ne_audit_head`` is rewound to seq 3 while all 6 real rows remain
    physically present. All seeded rows share the conftest default
    ``occurred_at`` of 2026-01-01 **12:00**; querying the closed interval
    ``[2026-01-01 00:00, 2026-01-01 00:00]`` (midnight to midnight) therefore
    matches ZERO rows — ``_seq_window`` returns ``(None, None)`` and this
    exercises ``_verify_bounded``'s **empty-window** branch specifically
    (``lo is None or hi is None``), not a narrow-but-nonempty one (measured:
    a previous version of this docstring mislabeled it "narrow window", which
    docs/testing/vacuous-guards.md flags as a guard whose documented target
    does not match what it actually pins). A window-local check alone has no
    reason to look past ``head_seq``; only the global ``_beyond_head_break``
    epilogue, run unconditionally on every bounded success path including the
    empty-window one, catches the rows still sitting beyond the (rewound)
    head. See ``test_bounded_verify_head_rewind_narrow_window_detected`` below
    for the genuinely narrow-but-nonempty counterpart, which pins the
    DIFFERENT (successor-link) bounded success path.
    """
    _seed(engine, audit, 6)
    with engine.begin() as conn:
        row3_hash = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 3)
        ).scalar_one()
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=3, record_hash=row3_hash)
        )

    cv = verify_chain(
        engine,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert not cv.valid, f"rows beyond a rewound head, outside the window, went undetected: {cv}"
    assert cv.reason == "head mismatch (records beyond head)"


def test_bounded_verify_head_rewind_narrow_window_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Pins the THIRD, previously-unpinned, bounded success path: the window
    ends strictly BEFORE the head with a genuine (real, correctly-linked)
    successor row — i.e. ``_verify_bounded``'s final ``return
    _beyond_head_break(conn, checked) or _ok(checked)`` after the successor
    link check passes (neither the ``hi > head_seq`` nor the ``hi ==
    head_seq`` branch above it).

    Round-2 Argus review found this exact call site was a SURVIVING MUTANT:
    neutering the epilogue on ONLY this branch left the full suite green,
    because every other bounded negative control happened to route through
    one of the OTHER two success branches
    (``test_bounded_verify_head_rewind_empty_window_detected`` for the
    empty-window branch, ``test_bounded_verify_forged_row_beyond_head_outside_window_detected``
    for the ``hi == head_seq`` branch). Seeding rows with DISTINCT
    ``occurred_at`` values (one per day, per the review's own suggested fix)
    makes a window that is narrow *and nonempty* constructible: it covers only
    seq 1, strictly less than the (rewound) head at seq 3, with row 2 --
    real, unmodified, correctly linked -- as its successor, so the
    successor-link check passes and control reaches this third call site.
    """
    ids = _seed_distinct_days(engine, audit, 6)
    assert len(ids) == 6
    with engine.begin() as conn:
        row3_hash = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == 3)
        ).scalar_one()
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=3, record_hash=row3_hash)
        )

    cv = verify_chain(
        engine,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC),
    )
    assert not cv.valid, f"rows beyond a rewound head, past a narrow window, went undetected: {cv}"
    assert cv.reason == "head mismatch (records beyond head)"
    assert cv.first_break_seq == 4
    assert cv.first_break_event_id == "e3"


def test_head_rewound_to_genesis_with_rows_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """Head rewound to (seq=0, GENESIS) with 6 real rows present.

    Deliberately loose ('head mismatch' in reason, not an exact reason string):
    this is a TRUE bidirectional control, green on both the unpatched and the
    patched tree — it exists to prove the fix did not accidentally weaken this
    detection, not to pin the exact reason text (which legitimately differs
    between the pre-fix "final head vs walked tail" framing and the post-fix
    "records beyond head" epilogue).
    """
    _seed(engine, audit, 6)
    with engine.begin() as conn:
        conn.execute(
            ne_audit_head.update()
            .where(ne_audit_head.c.id == 1)
            .values(seq=0, record_hash=GENESIS_HASH)
        )
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.reason is not None and "head mismatch" in cv.reason
