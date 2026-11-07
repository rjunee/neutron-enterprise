# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Chain verification — walk the hash chain and name the exact break, if any.

:func:`verify_chain` NEVER raises on a broken chain (see :mod:`ne.audit.errors`):
it returns a :class:`ChainVerification` whose ``detail`` names the exact record
(seq + event_id + reason). That ``detail`` string is what ``ne audit verify``
prints, and :func:`to_audit_verification` maps the result onto the frozen
``ne.contracts.governance.AuditVerification`` model the ``AuditSink.verify``
Protocol returns. It also FAILS CLOSED: a missing ``ne_audit_head`` row, a
deleted predecessor, an empty table with a non-genesis head, or an empty window
that hides a global gap all return an invalid result — never a silent pass and
never an exception.

**Full (unbounded) verify** — AS-OF-HEAD semantics, head read FIRST:

The chain head (``ne_audit_head``) is read ONCE, at the very start, before
anything else. Every subsequent check verifies exactly the prefix that head
anchors — ``seq`` in ``1..head_seq`` — never a value re-derived from a later
read. A record appended by another writer *while this verify is running* lands
past the snapshot this call took and is, by construction, out of scope: it is
not a break, because this call promised to verify "the ledger as of the head I
observed", not "the ledger as of whenever I happen to finish". (This is the
NE-AUDIT-VERIFY-RACE fix — see the module-level rationale below.)

1. **missing head** — a missing ``ne_audit_head`` row fails closed immediately;
2. **missing record** — the walk anchors at ``min(seq)`` (bounded to
   ``seq <= head_seq``) and its predecessor (``seq-1``, or genesis for ``seq
   1``) must exist; a deleted head/prefix record surfaces as a missing
   predecessor, and ``seq`` must be dense within the walk;
3. **record tampered** — ``compute_record_hash(prev_hash, payload)`` must equal
   the stored ``record_hash``;
4. **chain link broken** — each row's ``prev_hash`` must equal the previous
   row's ``record_hash`` (the first row overall is cross-checked against the
   genesis);
5. **column/payload mismatch** — every denormalized query column (``event_id``,
   ``event_type``, ``config_hash``, ``actor``, ``occurred_at``, ``kpi_tag``,
   ``trace_id``, ``action``, ``resource``, ``outcome``) must equal the value
   derived from the hash-covered ``payload`` — proves the fast query columns
   were not swapped. (Replay additionally reads its returned values FROM the
   payload, so the columns are filter-only: a tampered column at worst
   mis-selects a row, which this check catches, and can never surface forged
   content through replay.)
6. **head mismatch (truncated or forged tail)** — the walked prefix's tail
   ``seq`` and final ``record_hash`` must equal the head snapshot taken in step
   1 — compared against that SAME snapshot, never a fresh re-read;
7. **head mismatch (records beyond head)** — a race-free epilogue (see
   :func:`_beyond_head_break`) that catches rows genuinely inserted past the
   head (a forged, chain-consistent tail) WITHOUT re-introducing the race: it
   reads ``max(seq)`` over the whole table BEFORE re-reading the head, so a
   concurrent, legitimate append can never false-fire it.

**Bounded (windowed) verify** — as sound as full verify for deletion, reorder
and gap attacks. Before the windowed walk it runs a GLOBAL, seq-based (never
occurred_at-based, so non-monotonic ``occurred_at`` cannot false-fire) density
pre-check — no leading gap (``min(seq)`` must be 1), no interior gap
(``count == max(seq)``), and ``max(seq)`` must equal the head seq — and that
aggregate is itself bounded to ``seq <= head_seq`` (the head is read first,
same as the full path) so a record appended mid-verify cannot push
``max(seq)`` past the snapshot and false-fire the density check. Bounding that
aggregate to the head-anchored prefix, however, makes it BLIND by construction
to any row genuinely inserted past the head, so EVERY success path in the
bounded verify also runs the same race-free :func:`_beyond_head_break`
epilogue the full path runs — not only when the queried window happens to
reach the head — so a forged row sitting outside the window entirely, or a
head rewound to fall short of the window, is still caught (see
:func:`_verify_bounded`'s docstring for the pinning tests). It then anchors
BOTH ends of the window: the predecessor of ``min(seq)`` must exist (a
deleted predecessor IS a break), and the window's tail links forward to its
successor row (``seq hi+1``) or, when the window ends at the head, cross-checks
the head hash.

**Why a tail that grew during verification is out of scope by construction:**
a hash chain is monotone — ``append_record`` (see :mod:`ne.audit.store`)
inserts the new row AND advances ``ne_audit_head`` in the SAME caller
transaction, so the head never decreases honestly and a row is never visible
before its head-advance has committed. Reading the head first and bounding
every check to that snapshot therefore verifies a real, self-consistent prior
state of the ledger — it can never be fooled by a write that is still in
flight, and it never needs a re-read whose staleness relative to a LATER read
could manufacture a mismatch. This is provable, not merely observed: see
``packages/ne-audit/tests/test_verify_race.py`` for the deterministic
mid-verify-interleaving proof (a real ``append_record`` injected between the
verifier's own statements) and the true-concurrent-threads belt-and-braces
test.

**Inherent limitation (mathematical, not a bug):** a *consistent* tail-truncation
(delete tail rows AND rewind ``ne_audit_head`` to a self-consistent earlier state)
or a full chain rewrite performed AFTER the last signed checkpoint is invisible to
:func:`verify_chain` alone — a forward hash chain cannot prove records once existed
past a self-consistent head. Signed WORM checkpoints
(:func:`ne.audit.checkpoint.verify_checkpoints`) close that gap by pinning the head
at signing time; running verify_chain AND verify_checkpoints together is the
operational requirement.
"""

from __future__ import annotations

import logging
import threading
import weakref
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import sqlalchemy as sa

from ne.audit.store import (
    GENESIS_HASH,
    AuditRecord,
    _payload_datetime,
    _record_from_values,
    compute_record_hash,
    ne_audit_head,
    ne_audit_log,
)
from ne.contracts.governance import AuditVerification

_LOG = logging.getLogger("ne.audit.verify")

#: Denormalized text columns that must equal their hash-covered payload value.
#: (``occurred_at`` is cross-checked separately with datetime normalization.)
_DENORMALIZED_TEXT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "event_type",
    "config_hash",
    "actor",
    "kpi_tag",
    "trace_id",
    "action",
    "resource",
    "outcome",
)


def _as_utc(dt: datetime) -> datetime:
    """Normalize to UTC — assume UTC for a naive datetime (SQLite round-trips naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _column_mismatch(row: sa.RowMapping, payload: dict[str, Any]) -> str | None:
    """Return the FIRST denormalized column whose value diverges from the payload.

    Proves the fast query columns were not swapped out from under the hash-covered
    payload. ``occurred_at`` is compared after UTC normalization because SQLite
    returns the column naive while the payload is a ``'Z'``-suffixed ISO string;
    any parse failure on that comparison is itself reported as an ``occurred_at``
    mismatch. Returns ``None`` when every column matches.
    """
    for name in _DENORMALIZED_TEXT_COLUMNS:
        if row[name] != payload.get(name):
            return name
    try:
        if _as_utc(row["occurred_at"]) != _as_utc(_payload_datetime(payload["occurred_at"])):
            return "occurred_at"
    except (KeyError, TypeError, ValueError):
        return "occurred_at"
    return None


@dataclass(frozen=True)
class ChainVerification:
    """Result of walking (a window of) the audit chain."""

    valid: bool
    checked: int
    first_break_seq: int | None
    first_break_event_id: str | None
    reason: str | None
    detail: str


def _ok(checked: int) -> ChainVerification:
    return ChainVerification(
        valid=True,
        checked=checked,
        first_break_seq=None,
        first_break_event_id=None,
        reason=None,
        detail=f"chain intact: {checked} record(s) verified",
    )


def _break(*, seq: int, event_id: str | None, reason: str, checked: int) -> ChainVerification:
    who = event_id if event_id is not None else "<unknown>"
    return ChainVerification(
        valid=False,
        checked=checked,
        first_break_seq=seq,
        first_break_event_id=event_id,
        reason=reason,
        detail=f"chain broken at seq {seq} (event_id={who}): {reason}",
    )


def _seq_window(
    conn: sa.Connection, start: datetime | None, end: datetime | None
) -> tuple[int | None, int | None]:
    """Map a ``[start, end]`` occurred_at window to a contiguous ``[min_seq, max_seq]``."""
    clauses = []
    if start is not None:
        clauses.append(ne_audit_log.c.occurred_at >= start)
    if end is not None:
        clauses.append(ne_audit_log.c.occurred_at <= end)
    stmt = sa.select(sa.func.min(ne_audit_log.c.seq), sa.func.max(ne_audit_log.c.seq))
    if clauses:
        stmt = stmt.where(sa.and_(*clauses))
    row = conn.execute(stmt).one()
    lo, hi = row[0], row[1]
    return (int(lo) if lo is not None else None, int(hi) if hi is not None else None)


def _prev_hash_before(conn: sa.Connection, seq: int) -> str | None:
    """The ``record_hash`` of ``seq-1`` (or genesis when ``seq == 1``).

    Returns ``None`` ONLY when ``seq > 1`` and row ``seq-1`` is absent — a deleted
    predecessor, which the caller turns into a named ``missing record`` break.
    """
    if seq <= 1:
        return GENESIS_HASH
    row = conn.execute(
        sa.select(ne_audit_log.c.record_hash).where(ne_audit_log.c.seq == seq - 1)
    ).first()
    return row.record_hash if row is not None else None


def read_head_or_none(conn: sa.Connection) -> tuple[int, str] | None:
    """The chain head as ``(seq, record_hash)``, or ``None`` if the row is absent.

    A deleted ``ne_audit_head`` row is a tamper class in its own right — no DB
    trigger guards that table — so verify reads it fail-closed rather than with
    ``.one()`` (which would raise).

    PUBLIC, and deliberately so: it is the accessor every *verification* path
    must use, including :mod:`ne.audit.anchor`'s, which is outside this module.
    ``ne.audit.store.read_head`` stays ``.one()``-raising for the paths where
    an absent head means the process genuinely cannot proceed: the WRITE paths
    (``append_record``, checkpointing), AND two READ-side BASELINE callers that
    have no verdict to give — ``_frontier_records_with_baseline`` (the frontier
    evidence cache) and ``ne.cli.audit``.  Neither is a verification path; do
    not read this as a census of writers.  An exception is the right answer
    there.  A verifier
    reaching for that one turns a named tamper verdict into a 500 — which is
    exactly the regression pinned by
    ``test_audit_api.py::test_full_verify_names_the_deleted_head_row_instead_of_raising``.

    Note what is NOT here: no ``try``/``except``.  ``.first()`` distinguishes
    "no row" from "could not read" by CONSTRUCTION, so a genuine fault — a
    refused connection, a missing table — still raises out of ``conn.execute``
    untouched, and only the absent row becomes a verdict.
    """
    row = conn.execute(
        sa.select(ne_audit_head.c.seq, ne_audit_head.c.record_hash).where(ne_audit_head.c.id == 1)
    ).first()
    if row is None:
        return None
    return int(row.seq), cast(str, row.record_hash)


_HEAD_MISSING_REASON = "audit head missing (ne_audit_head row absent)"

#: How many rows :func:`_walk_rows` holds in memory at a time (NE-VERIFY-STREAM).
#:
#: The walk is strictly sequential — each row is hashed, linked against its
#: predecessor and then DROPPED; nothing downstream indexes the batch. So the
#: whole result set never needed to exist at once, and materialising it was pure
#: peak cost. MEASURED on the demo box's live chain (82,281 rows / 109 MB table
#: / 50 MB of JSONB payload) BEFORE this bound: one ``verify_chain()`` allocated
#: a 283 MB tracemalloc peak and drove process RSS from 148 MB to 845 MB — for a
#: verdict that is a single boolean. ne-api's frontier-evidence refresher pays
#: that every 30s (``ne.api.model_selection.FRONTIER_EVIDENCE_REFRESH_INTERVAL_S``)
#: on an ``anyio`` worker thread, so each burst grew a fresh glibc arena that is
#: never returned to the OS — which is how the 2 GiB API container ratcheted to
#: its cap and needed four restarts on 2026-08-28.
#:
#: The verdict is UNCHANGED: same rows, same order, same per-record checks. Only
#: the number resident at once moves. It is deliberately NOT
#: :data:`ne.audit.replay.MAX_PAGE_ROWS` — that is a caller-visible page ceiling
#: whose size is a contract; this is a private buffering hint with no observable
#: effect but memory.
_VERIFY_STREAM_ROWS = 1000


def _beyond_head_break(conn: sa.Connection, checked: int) -> ChainVerification | None:
    """Race-free epilogue: catch rows genuinely inserted PAST the verified head.

    Reads ``max(seq)`` over the WHOLE table FIRST, and only afterwards re-reads
    the head. This ordering is deliberate and is the opposite of the bug this
    module fixes: ``append_record`` (``ne.audit.store``) inserts the new row and
    advances ``ne_audit_head`` in the SAME caller transaction, so the head is
    monotone and a row is never visible before its own head-advance has
    committed. Consequently, if a row with ``seq > head_seq`` is visible at the
    ``max(seq)`` read, ITS head-advance (to at least that ``seq``) must already
    be committed too — so a head read taken AFTERWARDS that is still smaller
    than that ``max(seq)`` cannot be explained by a race; it proves the row was
    inserted directly (bypassing ``append_record``), i.e. a forged tail. A
    concurrent, *legitimate* append can never false-fire this check, because by
    the time its row is visible here its head-advance is visible too.

    Pinned by ``test_verify_race.py::test_forged_record_beyond_head_detected``
    (a genuinely forged row IS still caught — the fix did not weaken detection),
    ``test_verify_race.py::test_head_rewound_to_genesis_with_rows_detected``, and
    ``test_redteam_tamper.py::test_forged_record_insertion_breaks_dense_seq``.
    Each of the THREE call sites in ``_verify_bounded`` is individually pinned
    too (a coarse mutation that neuters all three at once masks a per-site gap
    — see ``mutations[]`` M9 in the PR body): the empty-window branch by
    ``test_verify_race.py::test_bounded_verify_head_rewind_empty_window_detected``,
    the ``hi == head_seq`` branch by
    ``test_verify_race.py::test_bounded_verify_forged_row_beyond_head_outside_window_detected``,
    and the successor-link branch by
    ``test_verify_race.py::test_bounded_verify_head_rewind_narrow_window_detected``.
    Race-freedom of the CALL SITES (head read once, up front, by the caller) is
    pinned by
    ``test_verify_race.py::test_full_verify_valid_with_append_before_every_head_read``
    (append injected before EVERY ``ne_audit_head`` statement, including this
    epilogue's re-read, and the result must still be valid). Race-freedom of
    THIS FUNCTION'S OWN read order — ``max(seq)`` before the head re-read,
    never the reverse — is pinned separately by
    ``test_verify_race.py::test_beyond_head_epilogue_mx_read_before_head_reread_stays_valid``,
    which injects the append immediately before this function's ``max(seq)``
    statement specifically.
    """
    mx = conn.execute(sa.select(sa.func.max(ne_audit_log.c.seq))).scalar()
    if mx is None:
        return None
    mx = int(mx)

    head = read_head_or_none(conn)
    if head is None:
        return _break(seq=0, event_id=None, reason=_HEAD_MISSING_REASON, checked=checked)
    head_seq, _head_hash = head
    if mx <= head_seq:
        return None

    row = (
        conn.execute(
            sa.select(ne_audit_log.c.seq, ne_audit_log.c.event_id)
            .where(sa.and_(ne_audit_log.c.seq > head_seq, ne_audit_log.c.seq <= mx))
            .order_by(ne_audit_log.c.seq)
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is not None:
        return _break(
            seq=int(row["seq"]),
            event_id=row["event_id"],
            reason="head mismatch (records beyond head)",
            checked=checked,
        )
    return _break(
        seq=mx, event_id=None, reason="head mismatch (records beyond head)", checked=checked
    )


def _density_break(conn: sa.Connection, head_seq: int) -> ChainVerification | None:
    """Global, seq-based density pre-check for BOUNDED verifies (fail-closed head).

    Deliberately seq-based (never occurred_at-based) so a non-monotonic
    ``occurred_at`` chain cannot false-fire. Returns a named break if the table is
    not a dense ``1..head_seq`` run anchored at the head, else ``None``.

    The aggregate is bounded to ``seq <= head_seq`` — ``head_seq`` is a snapshot
    the CALLER already took before this function runs, never re-read here. This
    is what makes the check race-free: a record appended by another writer
    between the caller's head read and this aggregate has ``seq > head_seq`` and
    is excluded, so it cannot push ``max(seq)`` past the snapshot and false-fire
    the ``mx != head_seq`` branch below (NE-AUDIT-VERIFY-RACE — pinned by
    ``test_verify_race.py::test_bounded_verify_valid_when_append_lands_before_density_scan``).
    """
    agg = conn.execute(
        sa.select(
            sa.func.count().label("n"),
            sa.func.min(ne_audit_log.c.seq).label("mn"),
            sa.func.max(ne_audit_log.c.seq).label("mx"),
        ).where(ne_audit_log.c.seq <= head_seq)
    ).one()
    n = int(agg.n)
    if n == 0:
        if head_seq != 0:
            return _break(
                seq=head_seq,
                event_id=None,
                reason=f"head mismatch (table empty but head at seq {head_seq})",
                checked=0,
            )
        return None

    mn = int(agg.mn)
    mx = int(agg.mx)
    if mn > 1:
        return _break(seq=1, event_id=None, reason="missing record (sequence gap)", checked=0)
    if mx != head_seq:
        return _break(
            seq=head_seq,
            event_id=None,
            reason=f"head mismatch (max seq {mx} != head seq {head_seq})",
            checked=0,
        )
    if n != mx:
        # Dense iff n == mx (given mn == 1). Locate the first missing seq: the
        # smallest present seq (< mx) that has no seq+1 successor.
        a = ne_audit_log.alias("a")
        b = ne_audit_log.alias("b")
        gap = conn.execute(
            sa.select(sa.func.min(a.c.seq))
            .select_from(a.outerjoin(b, b.c.seq == a.c.seq + 1))
            .where(sa.and_(b.c.seq.is_(None), a.c.seq < mx))
        ).scalar()
        first_missing = (int(gap) + 1) if gap is not None else mx
        return _break(
            seq=first_missing,
            event_id=None,
            reason="missing record (sequence gap)",
            checked=0,
        )
    return None


def _walk_rows(
    conn: sa.Connection, lo: int, hi: int, prev_record_hash: str
) -> tuple[ChainVerification | None, str, int]:
    """Walk rows ``lo..hi`` inclusive, checking gap/tamper/link/column integrity.

    Returns ``(break_or_None, final_prev_record_hash, checked)``. Shared by both
    the full and bounded verify paths — the per-record checks are identical
    regardless of how ``lo``/``hi`` were anchored.
    """
    # Streamed, never materialised: see _VERIFY_STREAM_ROWS. `yield_per` implies
    # server-side results, so the driver fetches in bounded batches instead of
    # buffering the whole window (payload JSONB included) before the first check.
    rows = conn.execute(
        ne_audit_log.select()
        .where(sa.and_(ne_audit_log.c.seq >= lo, ne_audit_log.c.seq <= hi))
        .order_by(ne_audit_log.c.seq)
        .execution_options(yield_per=_VERIFY_STREAM_ROWS)
    ).mappings()

    with closing(rows):
        expected_seq = lo
        checked = 0
        for row in rows:
            seq = int(row["seq"])
            event_id = row["event_id"]
            payload = dict(row["payload"])

            if seq != expected_seq:
                brk = _break(
                    seq=expected_seq,
                    event_id=None,
                    reason="missing record (sequence gap)",
                    checked=checked,
                )
                return brk, prev_record_hash, checked
            recomputed = compute_record_hash(row["prev_hash"], payload)
            if recomputed != row["record_hash"]:
                brk = _break(seq=seq, event_id=event_id, reason="record tampered", checked=checked)
                return brk, prev_record_hash, checked
            if row["prev_hash"] != prev_record_hash:
                brk = _break(
                    seq=seq, event_id=event_id, reason="chain link broken", checked=checked
                )
                return brk, prev_record_hash, checked
            mismatch = _column_mismatch(row, payload)
            if mismatch is not None:
                brk = _break(
                    seq=seq,
                    event_id=event_id,
                    reason=f"column/payload mismatch: {mismatch}",
                    checked=checked,
                )
                return brk, prev_record_hash, checked

            prev_record_hash = row["record_hash"]
            expected_seq = seq + 1
            checked += 1

        return None, prev_record_hash, checked


def _verify_full(conn: sa.Connection) -> ChainVerification:
    """As-of-head full verify: the head is read ONCE, first, and anchors everything.

    See the module docstring for the full race-freedom argument.
    """
    head = read_head_or_none(conn)
    if head is None:
        return _break(seq=0, event_id=None, reason=_HEAD_MISSING_REASON, checked=0)
    head_seq, head_hash = head

    if head_seq == 0:
        if head_hash != GENESIS_HASH:
            return _break(
                seq=0,
                event_id=None,
                reason="head mismatch (empty chain head hash is not genesis)",
                checked=0,
            )
        return _beyond_head_break(conn, 0) or _ok(0)

    agg = conn.execute(
        sa.select(sa.func.min(ne_audit_log.c.seq), sa.func.max(ne_audit_log.c.seq)).where(
            ne_audit_log.c.seq <= head_seq
        )
    ).one()
    lo, hi = agg[0], agg[1]
    if lo is None or hi is None:
        return _break(
            seq=head_seq,
            event_id=None,
            reason=f"head mismatch (table empty but head at seq {head_seq})",
            checked=0,
        )
    lo, hi = int(lo), int(hi)

    prev_record_hash = _prev_hash_before(conn, lo)
    if prev_record_hash is None:
        return _break(
            seq=lo - 1,
            event_id=None,
            reason=f"missing record (predecessor seq {lo - 1} absent)",
            checked=0,
        )

    brk, prev_record_hash, checked = _walk_rows(conn, lo, hi, prev_record_hash)
    if brk is not None:
        return brk

    if hi != head_seq or prev_record_hash != head_hash:
        return _break(
            seq=head_seq,
            event_id=None,
            reason="head mismatch (truncated or forged tail)",
            checked=checked,
        )

    return _beyond_head_break(conn, checked) or _ok(checked)


def _verify_bounded(
    conn: sa.Connection, *, start: datetime | None, end: datetime | None
) -> ChainVerification:
    """Windowed verify — as sound as full verify for deletion/reorder/gap attacks.

    Two changes from the pre-fix bounded logic, both NE-AUDIT-VERIFY-RACE:

    1. :func:`_density_break`'s aggregate is bounded to ``seq <= head_seq`` (the
       bounded path had the same race class as the full path — pinned by
       ``test_verify_race.py::test_bounded_verify_valid_when_append_lands_before_density_scan``).
    2. Bounding that aggregate makes it BLIND, by construction, to any row past
       the head — so every success return in this function ALSO runs the same
       race-free :func:`_beyond_head_break` epilogue the full path runs, not
       only the branch where the window happens to reach the head. Skipping
       that call on the other success paths was a real regression caught in
       review (a forged row sitting outside the queried window, or beyond a
       head that was itself rewound to fall short of the window, went
       undetected) — this function has THREE distinct success returns, each
       individually pinned (a coarse mutation neutering all three at once
       masks a per-site gap, as round-2 review found — see ``mutations[]`` M9):
       the empty-window branch by
       ``test_verify_race.py::test_bounded_verify_head_rewind_empty_window_detected``,
       the ``hi == head_seq`` branch by
       ``test_verify_race.py::test_bounded_verify_forged_row_beyond_head_outside_window_detected``,
       and the successor-link branch (``hi < head_seq``, a genuine successor
       row exists) by
       ``test_verify_race.py::test_bounded_verify_head_rewind_narrow_window_detected``.
    """
    lo, hi = _seq_window(conn, start, end)

    if lo is None or hi is None:
        # No rows selected — an empty window or a wholly-emptied table.
        head = read_head_or_none(conn)
        if head is None:
            return _break(seq=0, event_id=None, reason=_HEAD_MISSING_REASON, checked=0)
        density = _density_break(conn, head[0])
        if density is not None:
            return density
        return _beyond_head_break(conn, 0) or _ok(0)

    head = read_head_or_none(conn)
    if head is None:
        return _break(seq=0, event_id=None, reason=_HEAD_MISSING_REASON, checked=0)
    head_seq, head_hash = head

    # Global density pre-check BEFORE the walk (deletion / gap / head anchor).
    density = _density_break(conn, head_seq)
    if density is not None:
        return density

    # Start anchor: the predecessor of the window's first row must exist.
    prev_record_hash = _prev_hash_before(conn, lo)
    if prev_record_hash is None:
        return _break(
            seq=lo - 1,
            event_id=None,
            reason=f"missing record (predecessor seq {lo - 1} absent)",
            checked=0,
        )

    brk, prev_record_hash, checked = _walk_rows(conn, lo, hi, prev_record_hash)
    if brk is not None:
        return brk

    # Bounded END anchor: tie the window's tail to the successor row or the head.
    if hi > head_seq:
        return _break(
            seq=hi,
            event_id=None,
            reason="head mismatch (records beyond head)",
            checked=checked,
        )
    if hi == head_seq:
        if prev_record_hash != head_hash:
            return _break(
                seq=hi,
                event_id=None,
                reason="head mismatch (window ends at head but hashes differ)",
                checked=checked,
            )
        return _beyond_head_break(conn, checked) or _ok(checked)

    successor = (
        conn.execute(ne_audit_log.select().where(ne_audit_log.c.seq == hi + 1)).mappings().first()
    )
    if successor is None:
        return _break(
            seq=hi + 1,
            event_id=None,
            reason=f"missing record (window successor seq {hi + 1} absent)",
            checked=checked,
        )
    if successor["prev_hash"] != prev_record_hash:
        return _break(
            seq=hi + 1,
            event_id=successor["event_id"],
            reason="chain link broken",
            checked=checked,
        )
    return _beyond_head_break(conn, checked) or _ok(checked)


class ChainWalkObserver(Protocol):
    """Something that must learn when a chain walk in this process finds a break.

    The one implementation is :class:`ne.audit.anchor.AnchoredChainVerifier` —
    see :func:`register_chain_walk_observer` for why the hook exists at all.
    """

    def observe_chain_walk(self, engine: sa.Engine, cv: ChainVerification) -> None:
        """Called with EVERY ``valid is False`` verdict :func:`verify_chain`
        produces, on any engine.  The implementation decides whether the engine
        is one it holds state about; it must not raise."""


#: Guards the WeakSet below.  A WeakSet raises if it is mutated while being
#: iterated, and these callbacks fire from request worker threads.
_WALK_OBSERVERS_LOCK = threading.Lock()

#: Registered observers, held WEAKLY: a verifier that goes out of scope (every
#: test builds its own) drops out with no unregister call, so registration can
#: never leak a process-lifetime reference to a dead engine.
_WALK_OBSERVERS: weakref.WeakSet[ChainWalkObserver] = weakref.WeakSet()


def register_chain_walk_observer(observer: ChainWalkObserver) -> None:
    """Learn about every BREAK any chain walk in this process finds.

    ⭐ **THIS IS THE CHOKE POINT FOR BREAK PROPAGATION, AND IT IS HERE RATHER
    THAN AT THE CALL SITES ON PURPOSE.**  A process that verifies the chain in
    more than one place had, five separate times, the same defect: a walk
    DETECTED a real break and the finding did not reach something that needed
    it.  The last two were a windowed ``verify_chain(start=…, end=…)`` on the
    audit page proving the chain broken while the process's shared
    :class:`~ne.audit.anchor.AnchoredChainVerifier` anchor — a standing claim
    that a full walk blessed the prefix — stayed installed, so the very next
    unfiltered poll served an ``anchored-extension`` PASS over a chain a walk in
    the same process had just disproved.

    Patching that call site fixes that call site.  There are six more walkers in
    the API process (the audit page's windowed read, the CSV export preamble,
    ``/trace``'s unbounded walk, the trace index, the context ledger, and the
    verifier's own full verify), every one of them reached
    :func:`verify_chain`, and nothing stopped a seventh being added tomorrow.
    So the rule is enforced where they all already pass through: **a walk that
    returns invalid tells every observer, and an observer whose trust root
    claims the chain was good withdraws that claim.**

    WHAT AN OBSERVER MAY DO WITH THIS, precisely — it is a one-way rule:

    * it may WITHDRAW a standing claim of goodness (that is what an anchor is);
    * it may NOT turn the reporting verdict into a stronger one, and it is not
      told about PASSING walks at all.  A windowed walk is a weaker instrument
      than a full one about what it did NOT look at, and exactly as strong
      about what it did.  It can therefore make a finding and may never
      withdraw one — which is the same asymmetry the console's
      ``BASIS_STRENGTH`` ordering encodes, arrived at from the other side.
    """
    with _WALK_OBSERVERS_LOCK:
        _WALK_OBSERVERS.add(observer)


def unregister_chain_walk_observer(observer: ChainWalkObserver) -> None:
    """Stop reporting breaks to ``observer``.  Idempotent."""
    with _WALK_OBSERVERS_LOCK:
        _WALK_OBSERVERS.discard(observer)


def _report_chain_walk(
    engine_or_conn: sa.Engine | sa.Connection, cv: ChainVerification
) -> ChainVerification:
    """Hand a BREAK to every observer, then return the verdict unchanged.

    Passing verdicts are not reported: an observer's job is to stop vouching for
    a chain a walk disproved, never to start vouching for one because some other
    walk liked it.  A window that saw ten rows may not bless a ledger.

    An observer that raises is logged and SKIPPED rather than allowed to turn a
    verification into a 500 — the audit page renders a 500 as "unavailable",
    which is strictly weaker than the break verdict this call is carrying.
    """
    if cv.valid:
        return cv
    with _WALK_OBSERVERS_LOCK:
        observers = tuple(_WALK_OBSERVERS)
    if not observers:
        return cv
    engine = engine_or_conn.engine if isinstance(engine_or_conn, sa.Connection) else engine_or_conn
    for observer in observers:
        try:
            observer.observe_chain_walk(engine, cv)
        except Exception:  # pragma: no cover - defensive; see docstring
            _LOG.exception(
                "a chain-walk observer raised while being told about a break; the "
                "verdict itself is unaffected and is being returned to its caller"
            )
    return cv


def verify_chain(
    engine_or_conn: sa.Engine | sa.Connection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> ChainVerification:
    """Verify the chain (optionally restricted to an ``occurred_at`` window).

    EVERY walk in this process comes through here — ``_verify_full`` and
    ``_verify_bounded`` are private and have no other caller, pinned by
    ``test_break_propagation.py::test_no_caller_bypasses_the_verify_chain_choke_point``
    — which is what makes :func:`register_chain_walk_observer` a total rule
    rather than a convention.  A break found by ANY of them, windowed or not, is
    reported before the verdict is returned.
    """
    if isinstance(engine_or_conn, sa.Connection):
        conn = engine_or_conn
        if start is None and end is None:
            return _report_chain_walk(conn, _verify_full(conn))
        return _report_chain_walk(conn, _verify_bounded(conn, start=start, end=end))
    with engine_or_conn.connect() as conn:
        if start is None and end is None:
            cv = _verify_full(conn)
        else:
            cv = _verify_bounded(conn, start=start, end=end)
    return _report_chain_walk(engine_or_conn, cv)


def to_audit_verification(cv: ChainVerification) -> AuditVerification:
    """Map a :class:`ChainVerification` onto the frozen contracts model."""
    return AuditVerification(valid=cv.valid, checked=cv.checked, detail=cv.detail)


def verified_extension(
    engine_or_conn: sa.Engine | sa.Connection,
    *,
    baseline_seq: int,
    baseline_hash: str,
) -> tuple[int, str, tuple[AuditRecord, ...]] | None:
    """Every row appended after a previously verified head, chain-verified.

    The incremental counterpart to :func:`verify_chain` (the "incremental
    verify from the last verified head" NE-SELECT-LATENCY carded as owed
    work): given a baseline ``(seq, record_hash)`` that an earlier full
    verify blessed, prove that the chain now EXTENDS that baseline — and
    hand back the extension rows so the caller can fold them into a snapshot
    taken at the baseline instead of re-reading and re-verifying the whole
    chain.

    Cost is O(extension), never O(chain): one head read plus one indexed
    ``seq > baseline`` range scan.  The per-row checks are exactly
    ``_walk_rows``'s — density (no gap), ``record_hash`` recomputed over the
    hash-covered payload, ``prev_hash`` linkage anchored at
    ``baseline_hash``, and the denormalized column/payload cross-check — and
    the walked tail must land exactly on the head snapshot read FIRST (the
    same as-of-head semantics as :func:`verify_chain`; rows appended while
    this call runs are out of scope and provable on the next call).  Every
    success path — the empty extension included — then runs the SAME
    race-free beyond-head epilogue as :func:`verify_chain`
    (:func:`_beyond_head_break`, ``max(seq)`` read before the head re-read,
    still O(1)): a chain-consistent row a privileged writer inserted PAST
    the head without advancing it is a forged tail the full verify rejects,
    so the incremental proof must refuse the same chain state rather than
    bless it as a valid (possibly empty) extension.

    The guarantee is the same CLASS as :func:`verify_chain` gives the full
    chain: the returned rows are provably the unique append-only extension
    of the verified baseline.  The hash chain carries no signing key, so
    neither function authenticates the WRITER of a well-formed append —
    append authenticity comes from the append-only triggers and DB access
    control, identically for both — and this function therefore weakens
    nothing verify_chain guarantees over the segment it covers.  The
    prefix at or before the baseline is deliberately NOT re-checked here —
    re-checking it IS ``verify_chain`` over the whole chain, the exact
    O(chain) cost this function exists to remove.  That makes prefix-tamper
    detection BOUNDED, not absent: the baseline is only ever a head an
    actual full ``verify_chain`` pass blessed, so a prefix tampered after
    that blessing goes undetected by this proof alone and is caught at the
    caller's NEXT FULL ``verify_chain``.  A caller reusing a snapshot
    therefore owes a bound on how long that can be.  The shipping caller
    (the frontier records cache in ``ne.api.model_selection``) enforces one
    — but note WHICH one.  The cache's own
    ``FRONTIER_RECORDS_CACHE_TTL_S`` (60s) still bounds an ENTRY's
    lifetime: a successful extension proof advances the entry's baseline
    but never its ``read_at``, so however many folds succeed, the entry
    dies within one TTL and the next admission pays a fresh ``replay``.
    That fresh read no longer implies a fresh ``verify_chain``, though: the
    miss path now verifies through
    :class:`ne.audit.AnchoredChainVerifier` as well
    (``ModelSelectionService._frontier_records``), which is what made the
    miss sub-second.  So the prefix-tamper bound is no longer the 60s cache
    TTL — but it is NOT ``ne.audit.CHAIN_ANCHOR_MAX_AGE_S`` (900s) either.
    **The two limits COMPOSE: the effective ceiling is ~960s.**  An entry
    cached just before the anchor ages out keeps being reused through this
    function, which does NOT consult the anchor's age, for up to a further
    ``FRONTIER_RECORDS_CACHE_TTL_S`` (60s).  900s is only the anchor's half
    of it.

    And it is a STALENESS CEILING ON REUSE, not a time-to-detection: the age
    test runs inside ``AnchoredChainVerifier.verify``, so it fires on the
    first verification after the threshold, not on the threshold itself.
    What is guaranteed is that no read past ~960s is served off the stale
    anchor; each such read pays the full ``verify_chain`` inline and a prefix
    tamper surfaces there.  Elapsed time alone detects nothing.

    Active detection comes from the background
    ``run_chain_anchor_refresher`` loop, which walks the ledger every
    ``CHAIN_ANCHOR_FULL_VERIFY_INTERVAL_S`` (300s) whether or not a request
    arrives — in the API process, where it is wired.  There, and only there,
    300s is a genuine time-to-detection, because the loop is itself a
    caller.  The worker composes no refresher, so there the ~960s figure is
    the reuse ceiling and nothing more: with no caller, time-to-detection is
    unbounded.

    The entry-TTL property and the age property are pinned by execution in
    ``packages/ne-api/tests/test_frontier_warm.py``, the latter by
    ``test_a_prefix_tamper_is_detected_when_the_chain_ANCHOR_ages_out``.
    ⚠️ Read that test for what it proves and what it does not.  It advances
    the clock by ``CHAIN_ANCHOR_MAX_AGE_S + 100`` and THEN calls ``verify``,
    landing past BOTH limits — so it pins that a stale anchor is not reused,
    and it does NOT pin the ~960s boundary itself, nor any
    time-to-detection.  It asserts the anchor age rather than the old 60s
    TTL because a test still asserting 60s would be the dangerous kind of
    green; but a test that advanced time and asserted detection with no call
    would be a different dangerous green, and this one deliberately is not
    that.

    Returns ``(head_seq, head_hash, rows)`` — ``rows`` empty when the head
    has not moved — or ``None`` when the extension CANNOT be proven (head
    behind the baseline, gap, tampered record, broken link, column mismatch,
    a forged row beyond the head, or a moved-nothing head whose hash no
    longer matches the baseline).
    ``None`` never names the break: the caller's contract is "fall back to a
    full verified read", and :func:`verify_chain` is the tool that names
    breaks.
    """
    if isinstance(engine_or_conn, sa.Connection):
        return _verified_extension(engine_or_conn, baseline_seq, baseline_hash)
    with engine_or_conn.connect() as conn:
        return _verified_extension(conn, baseline_seq, baseline_hash)


def _verified_extension(
    conn: sa.Connection, baseline_seq: int, baseline_hash: str
) -> tuple[int, str, tuple[AuditRecord, ...]] | None:
    head = read_head_or_none(conn)
    if head is None:
        return None
    head_seq, head_hash = head
    if head_seq < baseline_seq:
        return None
    if head_seq == baseline_seq:
        if head_hash != baseline_hash:
            return None
        if _beyond_head_break(conn, 0) is not None:
            return None
        return (head_seq, head_hash, ())
    rows = conn.execute(
        ne_audit_log.select()
        .where(sa.and_(ne_audit_log.c.seq > baseline_seq, ne_audit_log.c.seq <= head_seq))
        .order_by(ne_audit_log.c.seq)
        .execution_options(yield_per=_VERIFY_STREAM_ROWS)
    ).mappings()
    records: list[AuditRecord] = []
    prev_record_hash = baseline_hash
    expected_seq = baseline_seq + 1
    with closing(rows):
        for row in rows:
            payload = dict(row["payload"])
            if (
                int(row["seq"]) != expected_seq
                or compute_record_hash(row["prev_hash"], payload) != row["record_hash"]
                or row["prev_hash"] != prev_record_hash
                or _column_mismatch(row, payload) is not None
            ):
                return None
            records.append(
                _record_from_values(
                    row_id=int(row["id"]),
                    seq=expected_seq,
                    payload=payload,
                    prev_hash=cast(str, row["prev_hash"]),
                    record_hash=cast(str, row["record_hash"]),
                )
            )
            prev_record_hash = cast(str, row["record_hash"])
            expected_seq += 1
    if expected_seq != head_seq + 1 or prev_record_hash != head_hash:
        return None
    if _beyond_head_break(conn, 0) is not None:
        return None
    return head_seq, head_hash, tuple(records)
