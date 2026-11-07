# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chain verification + tamper detection.

Test design informed by ABI versionstore test suite (MIT, Copyright (C) 2025
Naas.ai) — shapes only, no code copied.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import sqlalchemy as sa

from ne.audit import compute_record_hash, to_audit_verification, verified_extension, verify_chain
from ne.audit.store import append_record, ne_audit_head, ne_audit_log, read_head
from ne.contracts.governance import AuditVerification
from ne.schema import AuditEvent


def _drop_triggers(conn: sa.Connection) -> None:
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_update")
    conn.exec_driver_sql("DROP TRIGGER IF EXISTS ne_audit_log_no_delete")


def _seed(engine: sa.Engine, audit: Callable[..., AuditEvent], n: int = 6) -> list[str]:
    ids = []
    with engine.begin() as conn:
        for i in range(n):
            ev = audit(
                event_id=f"e{i}",
                occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC),
            )
            append_record(conn, ev)
            ids.append(f"e{i}")
    return ids


def test_valid_chain_verifies(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    cv = verify_chain(engine)
    assert cv.valid
    assert cv.checked == 6


def test_tampered_payload_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 3)
            .values(payload={"event_id": "e2", "tampered": True})
        )
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.first_break_event_id == "e2"
    assert cv.reason == "record tampered"
    assert "e2" in cv.detail


def test_broken_link_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    # Rewrite seq 4's prev_hash to a bogus value AND recompute its own record_hash
    # so the record is self-consistent — isolating the "chain link broken" check.
    with engine.connect() as conn:
        row = conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == 4)).one()
    bogus_prev = "f" * 64
    new_hash = compute_record_hash(bogus_prev, dict(row.payload))
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 4)
            .values(prev_hash=bogus_prev, record_hash=new_hash)
        )
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 4
    assert cv.reason == "chain link broken"


def test_missing_record_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 3))
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == "missing record (sequence gap)"


def test_head_mismatch_after_forged_tail(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    # Delete the tail row but leave the head pointing past it.
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 6))
    cv = verify_chain(engine)
    assert not cv.valid
    assert "head mismatch" in cv.reason  # type: ignore[operator]


def test_windowed_verify_intact(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    cv = verify_chain(
        engine,
        start=datetime(2026, 3, 2, tzinfo=UTC),
        end=datetime(2026, 3, 4, tzinfo=UTC),
    )
    assert cv.valid
    assert cv.checked == 3  # seqs 2,3,4


def test_windowed_verify_catches_tamper_in_window(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 3)
            .values(payload={"event_id": "e2", "x": 1})
        )
    cv = verify_chain(
        engine,
        start=datetime(2026, 3, 2, tzinfo=UTC),
        end=datetime(2026, 3, 4, tzinfo=UTC),
    )
    assert not cv.valid
    assert cv.first_break_seq == 3
    assert cv.reason == "record tampered"


def test_to_audit_verification_maps(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=3)
    av = to_audit_verification(verify_chain(engine))
    assert isinstance(av, AuditVerification)
    assert av.valid
    assert av.checked == 3
    assert av.detail


# ---------------------------------------------------------------------------
# Bounded-verify anchoring: a windowed verify must be as sound as a full verify
# for every deletion / reorder / gap attack (no trivial pass, both ends anchored).
# ---------------------------------------------------------------------------
_MAR = lambda d: datetime(2026, 3, d, tzinfo=UTC)  # noqa: E731 - seed occurred_at helper


def _self_consistent_rewrite(engine: sa.Engine, seq: int, **col_overrides: object) -> None:
    """Rewrite a row's prev_hash (and/or columns) AND recompute its own record_hash
    so the row is internally self-consistent — isolating a specific anchor check."""
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


def test_bounded_verify_detects_missing_predecessor(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    # The exact Codex P2: delete an interior record, then verify a LATER window.
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 2))
    cv = verify_chain(engine, start=_MAR(3), end=_MAR(4))
    assert not cv.valid
    assert cv.first_break_seq == 2
    assert "missing record" in cv.reason  # type: ignore[operator]


def test_bounded_verify_detects_tail_of_window_deletion(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 4))
    cv = verify_chain(engine, start=_MAR(2), end=_MAR(4))
    assert not cv.valid


def test_bounded_verify_detects_emptied_window(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq.in_([2, 3, 4])))
    cv = verify_chain(engine, start=_MAR(2), end=_MAR(4))
    assert not cv.valid


def test_bounded_verify_detects_successor_link_tamper(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    # Self-consistently rewrite the row AFTER the window; the end anchor catches it.
    _self_consistent_rewrite(engine, seq=5, prev_hash="f" * 64)
    cv = verify_chain(engine, start=_MAR(2), end=_MAR(4))
    assert not cv.valid
    assert cv.first_break_seq == 5


def test_bounded_verify_tail_window_head_anchor(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        conn.execute(
            ne_audit_head.update().where(ne_audit_head.c.id == 1).values(record_hash="f" * 64)
        )
    cv = verify_chain(engine, start=_MAR(5), end=_MAR(6))
    assert not cv.valid
    assert "head mismatch" in cv.reason  # type: ignore[operator]


def test_full_verify_detects_head_record_deletion(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == 1))
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 1


def test_full_verify_detects_prefix_deletion(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq.in_([1, 2, 3])))
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 3


def test_full_verify_detects_emptied_table(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete())
    cv = verify_chain(engine)
    assert not cv.valid
    assert "head mismatch" in cv.reason  # type: ignore[operator]


def test_genesis_prev_hash_tamper_detected(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    # Rewrite seq 1's prev_hash off the genesis, self-consistently.
    _self_consistent_rewrite(engine, seq=1, prev_hash="f" * 64)
    full = verify_chain(engine)
    assert not full.valid
    assert full.first_break_seq == 1
    assert full.reason == "chain link broken"
    bounded = verify_chain(engine, start=_MAR(1), end=_MAR(2))
    assert not bounded.valid


def test_seq_swap_reordering_detected(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    # Swap the seq values of rows 2 and 3 via a temp seq to dodge the unique index.
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == 2).values(seq=9999))
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == 3).values(seq=2))
        conn.execute(ne_audit_log.update().where(ne_audit_log.c.seq == 9999).values(seq=3))
    assert not verify_chain(engine).valid
    assert not verify_chain(engine, start=_MAR(2), end=_MAR(4)).valid


def test_head_row_deleted_fails_closed(engine: sa.Engine, audit: Callable[..., AuditEvent]) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        conn.execute(ne_audit_head.delete())
    full = verify_chain(engine)
    assert not full.valid
    assert "head" in full.reason  # type: ignore[operator]
    bounded = verify_chain(engine, start=_MAR(2), end=_MAR(4))
    assert not bounded.valid
    assert "head" in bounded.reason  # type: ignore[operator]


def test_bounded_verify_out_of_order_occurred_at_intact(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    # Non-monotonic occurred_at vs seq must NOT trip the seq-based density/anchor
    # checks — guards against false positives on an intact chain.
    order = [5, 1, 4, 2, 6, 3]
    with engine.begin() as conn:
        for i, day in enumerate(order):
            append_record(
                conn, audit(event_id=f"o{i}", occurred_at=datetime(2026, 3, day, tzinfo=UTC))
            )
    assert verify_chain(engine).valid
    for lo, hi in ((1, 3), (2, 5), (4, 6), (1, 6)):
        cv = verify_chain(engine, start=_MAR(lo), end=_MAR(hi))
        assert cv.valid, f"window {lo}-{hi} false-failed: {cv.detail}"


# --- NE-CHAIN-LOCATED: an invalid verdict ALWAYS names an entry ---------------
#
# The console leans on this and could not check it. Its break rendering — the
# `role="alert"` band, the entry pill, the dimmed tail, the severed spine and
# the `/audit?seq=` deep link — is all keyed on `first_break_seq`, so a verdict
# that said "broken" without saying where would put the word BREAK on screen
# over a ledger drawn as whole.
#
# The property is real but was expressed NOWHERE: `first_break_seq` is
# `int | None` on the dataclass, and it must be, because `_ok` sets it to None.
# The correlation lives only in `_break`'s signature — `seq: int`, required —
# and a `_break`-shaped constructor that named no entry would typecheck today.
# Twenty-three call sites currently pass one; nothing stopped the twenty-fourth
# from not doing so.
#
# ⚠️ This assertion exists because the console shipped a guard against the
# seq-less break and its author (me) recorded it as a defect REPRODUCED and
# FIXED. It was neither: the shape is unreachable. The guard is kept as a
# defensive one, and this test is what makes that label true rather than
# hopeful — the console's comment cites `verify.py:180` for a promise this
# package now actually enforces.
def test_an_invalid_verdict_always_names_the_entry_it_broke_at(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=6)
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 3)
            .values(payload={"event_id": "e2", "tampered": True})
        )
    verdict = verify_chain(engine)
    assert verdict.valid is False
    # The load-bearing half: not merely that a seq is present on THIS verdict,
    # but that `valid is False` and `first_break_seq is None` never co-occur.
    assert verdict.first_break_seq is not None, (
        "an invalid verdict named no entry; the console renders its whole break "
        "state off first_break_seq and would draw an unbroken chain under the "
        "word BREAK"
    )
    assert isinstance(verdict.first_break_seq, int)


# -- incremental extension proof (verified_extension) -------------------------


def test_an_appended_extension_is_proven_and_returned(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=3)
    with engine.connect() as conn:
        baseline_seq, baseline_hash = read_head(conn)
    with engine.begin() as conn:
        for i in range(3, 6):
            append_record(
                conn, audit(event_id=f"e{i}", occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC))
            )
    ext = verified_extension(engine, baseline_seq=baseline_seq, baseline_hash=baseline_hash)
    assert ext is not None
    head_seq, head_hash, rows = ext
    with engine.connect() as conn:
        assert (head_seq, head_hash) == read_head(conn)
    assert [r.seq for r in rows] == [4, 5, 6]
    assert [r.event_id for r in rows] == ["e3", "e4", "e5"]
    # Every returned field is payload-authoritative, same as replay's records.
    assert all(r.event_id == r.payload["event_id"] for r in rows)


def test_an_unmoved_head_proves_an_empty_extension(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=3)
    with engine.connect() as conn:
        baseline_seq, baseline_hash = read_head(conn)
    ext = verified_extension(engine, baseline_seq=baseline_seq, baseline_hash=baseline_hash)
    assert ext == (baseline_seq, baseline_hash, ())


def test_a_tampered_extension_row_is_not_proven(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=3)
    with engine.connect() as conn:
        baseline_seq, baseline_hash = read_head(conn)
    with engine.begin() as conn:
        for i in range(3, 6):
            append_record(
                conn, audit(event_id=f"e{i}", occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC))
            )
        _drop_triggers(conn)
        conn.execute(
            ne_audit_log.update()
            .where(ne_audit_log.c.seq == 5)
            .values(payload={"event_id": "e4", "forged": True})
        )
    assert (
        verified_extension(engine, baseline_seq=baseline_seq, baseline_hash=baseline_hash) is None
    )
    # The full verify still names it (the fallback the caller owes).
    cv = verify_chain(engine)
    assert not cv.valid
    assert cv.first_break_seq == 5


def test_a_mismatched_or_overtaken_baseline_is_not_proven(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    _seed(engine, audit, n=3)
    with engine.connect() as conn:
        baseline_seq, _ = read_head(conn)
    # Unmoved head, wrong anchor hash: nothing to extend FROM.
    assert verified_extension(engine, baseline_seq=baseline_seq, baseline_hash="0" * 64) is None
    # A baseline past the head (a rolled-back chain) is never provable.
    assert verified_extension(engine, baseline_seq=baseline_seq + 7, baseline_hash="0" * 64) is None


def test_an_extension_broken_off_its_anchor_is_not_proven(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """A wrong-but-plausible anchor: rows exist past the baseline seq, but
    they chain off the REAL head hash, not the caller's — the linkage check
    must refuse rather than fold rows onto a snapshot they do not extend."""
    _seed(engine, audit, n=3)
    with engine.connect() as conn:
        baseline_seq, _ = read_head(conn)
    with engine.begin() as conn:
        append_record(conn, audit(event_id="e3", occurred_at=datetime(2026, 3, 9, tzinfo=UTC)))
    assert verified_extension(engine, baseline_seq=baseline_seq, baseline_hash="1" * 64) is None


def _forge_beyond_head(engine: sa.Engine, audit: Callable[..., AuditEvent], seq: int) -> None:
    """Insert a chain-consistent forged row at ``seq`` WITHOUT advancing the head.

    The privileged-writer shape from ``test_verify_race.py``'s
    ``test_forged_record_beyond_head_detected``: the row chains correctly off
    ``seq - 1``'s record hash, so only the head comparison can catch it.
    """
    with engine.connect() as conn:
        prev = conn.execute(
            sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == seq - 1)
        ).scalar_one()
    forged = audit(event_id=f"forged-beyond-head-{seq}")
    payload = forged.model_dump(mode="json")
    record_hash = compute_record_hash(prev, payload)
    with engine.begin() as conn:
        conn.execute(
            ne_audit_log.insert().values(
                seq=seq,
                event_id=payload["event_id"],
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


def test_a_forged_row_beyond_the_head_is_never_proven_as_an_extension(
    engine: sa.Engine, audit: Callable[..., AuditEvent]
) -> None:
    """FAILS on 5c43ed84: a forged beyond-head row went UNDETECTED incrementally.

    A privileged writer bypasses ``append_record`` and inserts a
    chain-consistent row with ``seq > ne_audit_head.seq`` without advancing the
    head.  ``verify_chain`` explicitly detects and rejects this forged-tail
    class (its race-free beyond-head epilogue).  The incremental proof MUST NOT
    bless the same chain state: tamper-evidence is the product's core claim,
    and a proof that stays silent where full verification alarms lets model
    selection keep reusing evidence while the chain is invalid.  Detection
    means the proof refuses (returns ``None``) and the caller falls through to
    the full verified read, whose ``verify_chain`` names the break — not merely
    that the forged row is absent from the folded rows.

    Both success paths are covered: the unmoved-head early return (the empty
    extension — verify.py:705-706 on 5c43ed84) and the walked, non-empty
    extension whose tail lands exactly on the head.
    """
    _seed(engine, audit, n=3)
    with engine.connect() as conn:
        baseline_seq, baseline_hash = read_head(conn)

    # -- Shape 1: head unmoved, forged row at head+1 (the empty-extension
    # early return).  Full verification rejects; the proof must too.
    _forge_beyond_head(engine, audit, seq=baseline_seq + 1)
    assert not verify_chain(engine).valid
    assert (
        verified_extension(engine, baseline_seq=baseline_seq, baseline_hash=baseline_hash) is None
    ), (
        "the incremental proof reported a VALID EMPTY extension of a chain "
        "state verify_chain rejects — the forged beyond-head row went "
        "undetected and the caller would keep reusing evidence"
    )

    # -- Shape 2: two honest appends move the head, then a forged row sits
    # just past the NEW head.  The walked extension (baseline -> head) is
    # itself flawless, so only a beyond-head check can refuse it.
    with engine.begin() as conn:
        _drop_triggers(conn)
        conn.execute(ne_audit_log.delete().where(ne_audit_log.c.seq == baseline_seq + 1))
    with engine.begin() as conn:
        for i in (3, 4):
            append_record(
                conn, audit(event_id=f"e{i}", occurred_at=datetime(2026, 3, i + 1, tzinfo=UTC))
            )
    assert verify_chain(engine).valid, "precondition: the restored chain is honest again"
    _forge_beyond_head(engine, audit, seq=baseline_seq + 3)
    assert not verify_chain(engine).valid
    assert (
        verified_extension(engine, baseline_seq=baseline_seq, baseline_hash=baseline_hash) is None
    ), (
        "the walked non-empty extension was proven even though a forged row "
        "sits beyond the head — full verification rejects this chain state"
    )
