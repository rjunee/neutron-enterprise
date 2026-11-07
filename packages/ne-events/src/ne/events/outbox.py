# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The Postgres outbox: the v1 typed-event transport (ADR-001 §4.1, ADR-0002).

Two SQLAlchemy-Core tables — an append-only ``ne_outbox_events`` log and a
per-consumer ``ne_outbox_cursors`` table — plus three seam operations:

* :func:`append` — write an event in the CALLER's business transaction. That the
  event row and the business write commit atomically IS the outbox pattern: no
  event is emitted for a rolled-back write, and no write is lost while its event
  is emitted. Re-appending the same ``event_id`` is idempotent (a SAVEPOINT keeps
  a duplicate from aborting the caller's transaction).
* :func:`read_batch` / :func:`ack` — a consumer reads a window of events after
  its cursor and, only after durably processing them, advances the cursor.
* :func:`drain_once` — one read → process → ack tick for the exporter seam
  (ne-worker owns the loop, a later unit).
* :func:`prune_acked` — bounded retention: delete rows at/below EVERY consumer's
  cursor that are older than a retention window, so the table cannot grow
  unbounded once events have been delivered (P2-21). Zero consumers ⇒ prune
  NOTHING (an undelivered event is never dropped).

At-least-once under CONCURRENT Postgres producers (NE-070)
----------------------------------------------------------
The pilot tier is the first deployment that runs MULTIPLE workers against ONE
Postgres, so the outbox must survive genuinely concurrent producers. The hazard:
a producer that has been ASSIGNED a low id ``N`` (via the identity sequence) but
has NOT yet committed leaves a *gap* — a higher id ``N+1`` from a sibling
producer can be committed and visible first. A naive "ack up to the max visible
id" would advance the cursor past ``N``, and when ``N`` finally commits it would
be **permanently skipped** — a lost event, violating at-least-once.

The fix is a **contiguous-visible walk** past the cursor, plus a **dead-hole
fence** and a **cursor claim**:

* :func:`append` forces the producer's xid to be assigned BEFORE the identity
  ``nextval`` (``SELECT pg_current_xact_id()`` on Postgres) so every dispensed id
  is backed by an xid that already existed when the id was handed out.
* :func:`read_batch` walks ids contiguously upward from the cursor over the
  VISIBLE rows. It **stops at the first gap that is not proven dead** — an
  invisible lower id whose producer might still commit is never crossed, so the
  classic multi-writer sequence gap can never be skipped. Delivery of committed
  rows is prompt: a committed row is visible immediately, no fence wait.
* A gap is **proven dead** (a permanent rollback hole, safe to skip so the cursor
  stays live past aborted producers) only when it is below a *matured fence*. The
  fence is anchored to the READER's OWN xid, captured AFTER reading the sequence
  ``last_value`` and AFTER the ``FOR UPDATE`` claim assigns that xid: this anchor
  strictly exceeds every producer xid that dispensed an id ``<= last_value``
  (each such producer assigned its xid before its ``nextval``, before we read
  ``last_value``, before this anchor). When a later tick's ``pg_snapshot_xmin``
  reaches the stored anchor, every xid below it — hence every such producer — has
  finished, so an id ``<= fence_last_id`` that is still invisible is aborted.
* ``pg_snapshot_xmax`` is deliberately NOT used as the anchor: it is one past the
  highest *completed* xid, so an in-flight producer's xid sits AT/ABOVE it
  (``xmin == xmax`` does **not** imply "no in-flight producer"). Only a
  reader-assigned anchor bounds in-flight producers from above.
* Competing consumers claim the cursor row with ``SELECT ... FOR UPDATE SKIP
  LOCKED``; a worker that cannot obtain the row reads nothing (it does not block
  and does not double-deliver a window a sibling already holds).

SOUNDNESS INVARIANT (stated where it is relied on in :func:`read_batch`): the
walk only advances over a VISIBLE row (delivered) or a hole below a matured fence
(provably aborted). Requires the identity sequence to use ``CACHE 1`` (the
default) so ``last_value`` reflects the true dispensed high-water (a crash can
over-report — safe direction: over-reported ids are never-dispensed dead holes).
SQLite is a single-writer test dialect where the concurrent gap cannot arise, so
its path stays the simple id-ordered scan.

Cursor edge-case discipline (translated from ABI's SQLite/Dagster event-cursor
teardown, §3.5 — the discipline is lifted, not the code):

* **cursor-advance-then-drop hazard** — the cursor advances ONLY after the
  handler returns successfully (:func:`drain_once` acks in a *second*
  transaction, after ``handler`` runs outside any transaction). A crash between
  read and ack redelivers the identical window (at-least-once).
* **filter-superset invariant** — a consumer's ``event_types`` filter is recorded
  at first registration and is IMMUTABLE. Widening it would silently skip rows
  the cursor already advanced past, so a changed filter fails loud
  (:class:`ConsumerFilterMismatchError`) — register a new consumer instead. The
  scan window carries NO type predicate in SQL; the filter only selects which
  scanned rows are returned, and the cursor advances over the whole window.
* **fail-open-on-transient-error** — a handler exception yields a
  :class:`DrainResult` with ``error`` set and the cursor UNMOVED, never an
  exception that stalls the drain loop; the events redeliver next tick.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from ne.contracts import EventLike

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
metadata = sa.MetaData()

ne_outbox_events = sa.Table(
    "ne_outbox_events",
    metadata,
    # BigInteger on Postgres (Identity); Integer on SQLite so it aliases the
    # rowid and autoincrements (SQLite only autoincrements INTEGER PRIMARY KEY).
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    sa.Column("event_id", sa.String, nullable=False, unique=True),
    sa.Column("event_type", sa.String, nullable=False, index=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("kpi_tag", sa.String, nullable=True),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

ne_outbox_cursors = sa.Table(
    "ne_outbox_cursors",
    metadata,
    sa.Column("consumer", sa.String, primary_key=True),
    sa.Column("position", sa.BigInteger, nullable=False, default=0),
    # NULL = all event types; else the sorted list recorded at registration
    # (immutable — see the filter-superset invariant).
    sa.Column("event_types", sa.JSON, nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True)),
    # Dead-hole fence (Postgres concurrent-producer safety, NE-070). Both NULL on
    # SQLite and before the first Postgres tick. ``fence_xmax`` is the READER's
    # own xid captured (after reading ``last_value``) at the tick that recorded
    # ``fence_last_id`` (that sequence last_value); once a later tick's
    # ``pg_snapshot_xmin`` reaches ``fence_xmax`` the fence matures and an
    # invisible id <= ``fence_last_id`` is a proven-dead rollback hole. The
    # column keeps the ``fence_xmax`` name for schema stability. See the module
    # docstring for the soundness argument.
    sa.Column("fence_xmax", sa.BigInteger, nullable=True),
    sa.Column("fence_last_id", sa.BigInteger, nullable=True),
)


# ---------------------------------------------------------------------------
# Errors, records
# ---------------------------------------------------------------------------
class OutboxError(Exception):
    """Base class for outbox seam faults."""


class ConsumerFilterMismatchError(OutboxError):
    """A consumer read with a different ``event_types`` filter than it registered."""


class CursorRegressionError(OutboxError):
    """An ack tried to move a consumer's cursor backwards."""


@dataclass(frozen=True)
class OutboxRecord:
    """One event as read back from the outbox (the id is the cursor coordinate)."""

    id: int
    event_id: str
    event_type: str
    occurred_at: datetime
    config_hash: str
    kpi_tag: str | None
    actor: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class DrainResult:
    """Outcome of one :func:`drain_once` tick."""

    processed: int
    acked_to: int
    error: str | None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
#: Advisory-lock key serializing this bootstrap across roots on ONE Postgres.
#: Spells "NEOUTB" in hex; distinct from the lineage schema key
#: (``ne.brain.lineage._POSTGRES_SCHEMA_LOCK_KEY``) so the two bootstraps do not
#: serialize against each other — and, more importantly, cannot deadlock by
#: taking the same two locks in different orders.
_POSTGRES_SCHEMA_LOCK_KEY = 0x4E45_4F55_5442


def ensure_outbox_schema(engine: sa.Engine) -> None:
    """Create the outbox tables if absent (v1 bootstrap) and upgrade legacy shapes.

    The real migration runner is NE-069's; this is the create-all seam tests and
    early bootstrap use. Package schemas are package-owned v0 (see m0001), so the
    NE-070 settled-horizon fence columns are added HERE, idempotently: a
    pre-existing ``ne_outbox_cursors`` from before NE-070 is ALTERed to add the
    two nullable ``fence_*`` columns (a no-op on a freshly created table). Plain
    ``ADD COLUMN ... BIGINT`` works on both SQLite and Postgres.

    RE-RUNNING THIS CONCURRENTLY MUST BE A NO-OP, NOT A CRASH. ``create_all`` is
    a check-then-create: it asks whether each table exists and creates the ones
    that do not, which is idempotent only against ITSELF. On the pilot tier it is
    not alone — ne-api, ne-worker's ``*/15`` connector cron, and a host-side
    ``ne connect sync`` all call it against the SAME database, on every run
    (``ne.connect.runner.build_ingest_stack``). Two of them interleaving between
    the check and the create is not exotic: the loser collides on a system
    catalog's unique index, or the two abort with ``40P01 deadlock_detected`` —
    which psycopg raises as ``DeadlockDetected``, a subclass of
    ``OperationalError``, so a re-run collision arrives wearing the class of a
    connection fault. So the whole check-and-create now runs under ONE
    transaction-scoped advisory lock on Postgres, exactly as
    ``ne.brain.lineage.ensure_document_lineage_schema`` already does and for the
    same stated reason. The loser BLOCKS, then finds everything present and does
    nothing.

    Nothing is swallowed to buy that: no ``except`` was added, so a genuine
    constraint violation still raises. SQLite has no advisory locks and needs
    none (single writer), so that path is unchanged.
    """
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _POSTGRES_SCHEMA_LOCK_KEY},
            )
        metadata.create_all(conn)
        existing = {col["name"] for col in sa.inspect(conn).get_columns(ne_outbox_cursors.name)}
        for name in ("fence_xmax", "fence_last_id"):
            if name not in existing:
                conn.execute(
                    sa.text(f"ALTER TABLE {ne_outbox_cursors.name} ADD COLUMN {name} BIGINT")
                )


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_filter(event_types: Sequence[str] | None) -> list[str] | None:
    """Canonicalize a filter to a sorted unique list, or ``None`` for all types."""
    if event_types is None:
        return None
    return sorted(set(event_types))


# ---------------------------------------------------------------------------
# Append (producer side)
# ---------------------------------------------------------------------------
def append(conn: sa.Connection, event: EventLike) -> int:
    """Append ``event`` to the outbox on the caller's connection; return its id.

    MUST be called on the caller's business-write connection/transaction so the
    event row commits atomically with the business write — that atomicity is the
    whole point of the outbox pattern. ``event`` must be a pydantic model (the
    generated ``ne.schema`` events are); anything else raises :class:`TypeError`.

    Re-appending an already-present ``event_id`` is idempotent: the INSERT runs in
    a SAVEPOINT, a unique-violation rolls the SAVEPOINT back (leaving the caller's
    enclosing transaction usable — critical on Postgres, which otherwise aborts
    the whole transaction), and the existing row's id is returned.
    """
    if not isinstance(event, BaseModel):
        raise TypeError(f"append() requires a pydantic BaseModel event; got {type(event).__name__}")
    payload = event.model_dump(mode="json")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise TypeError("append() requires an event carrying a non-empty string 'event_id'")

    ev = cast(EventLike, event)
    # Settled-horizon ordering invariant (NE-070, Postgres only): force the
    # transaction's xid to be assigned BEFORE the identity ``nextval`` runs, so
    # every id this transaction dispenses is backed by an xid that already
    # existed when the id was handed out. Without this a razor-thin
    # nextval-before-xid window would let a committed higher id carry an xid
    # LOWER than an uncommitted lower id, defeating the fence. No-op on SQLite
    # (single writer; the concurrent gap cannot arise).
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT pg_current_xact_id()"))
    stmt = ne_outbox_events.insert().values(
        event_id=event_id,
        event_type=ev.event_type,
        occurred_at=ev.occurred_at,
        config_hash=ev.config_hash,
        kpi_tag=ev.kpi_tag,
        actor=ev.actor,
        payload=payload,
    )
    try:
        with conn.begin_nested():
            result = conn.execute(stmt)
        pk = result.inserted_primary_key
        if pk is None:  # pragma: no cover - a single-row insert always yields a pk
            raise RuntimeError("outbox insert returned no primary key")
        return int(pk[0])
    except IntegrityError:
        existing = conn.execute(
            sa.select(ne_outbox_events.c.id).where(ne_outbox_events.c.event_id == event_id)
        ).scalar_one()
        return int(existing)


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _CursorState:
    position: int
    stored_filter: list[str] | None


def _current_cursor(conn: sa.Connection, consumer: str) -> _CursorState | None:
    row = conn.execute(
        sa.select(ne_outbox_cursors.c.position, ne_outbox_cursors.c.event_types).where(
            ne_outbox_cursors.c.consumer == consumer
        )
    ).first()
    if row is None:
        return None
    stored = cast("list[str] | None", row.event_types)
    return _CursorState(position=int(row.position), stored_filter=_normalize_filter(stored))


# ---------------------------------------------------------------------------
# Read / ack (consumer side)
# ---------------------------------------------------------------------------
def _row_to_record(row: Any) -> OutboxRecord:
    return OutboxRecord(
        id=int(row.id),
        event_id=row.event_id,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        config_hash=row.config_hash,
        kpi_tag=row.kpi_tag,
        actor=row.actor,
        payload=dict(row.payload),
    )


def _check_filter(consumer: str, stored: list[str] | None, requested: list[str] | None) -> None:
    if stored != requested:
        raise ConsumerFilterMismatchError(
            f"outbox consumer {consumer!r} registered filter "
            f"{stored!r} but read requested {requested!r}; the "
            "consumer filter is immutable — register a new consumer name"
        )


def read_batch(
    conn: sa.Connection,
    consumer: str,
    *,
    limit: int = 100,
    event_types: Sequence[str] | None = None,
) -> tuple[list[OutboxRecord], int]:
    """Read the next window of events for ``consumer``; return (records, high_water).

    Registers the consumer's cursor (at position 0, filter ``event_types``) on
    first sight — the filter is then IMMUTABLE. A subsequent read with a different
    filter raises :class:`ConsumerFilterMismatchError`.

    ``records`` are the scanned rows that match the filter; acking ``high_water``
    advances the cursor over the WHOLE window, including rows filtered out (they
    never redeliver to this consumer — the deliberate filtered-drain semantics).

    **SQLite** (single-writer test dialect): the scan is ``WHERE id > position
    ORDER BY id LIMIT limit``; ``high_water`` is the max scanned id (equal to the
    current position when the window is empty). No concurrent-producer gap can
    arise, so no fence is needed.

    **Postgres** (competing multi-worker consumers, NE-070): the cursor row is
    claimed with ``SELECT ... FOR UPDATE SKIP LOCKED`` — a worker that cannot
    obtain it reads nothing (returns ``([], position)`` from a non-locking read;
    a caller MUST NOT ack a batch it did not receive, and acking that unchanged
    position is a no-op). The scan then walks ids contiguously upward from the
    cursor over the VISIBLE rows and STOPS at the first gap that is not proven
    dead, so an uncommitted lower id is never crossed (``high_water`` never
    advances past it). A gap below a *matured fence* is a permanent rollback hole
    and is skipped so the cursor stays live past aborted producers. See the
    module docstring for the soundness invariant.
    """
    requested = _normalize_filter(event_types)
    if conn.dialect.name == "postgresql":
        return _read_batch_postgres(conn, consumer, requested, limit)

    state = _current_cursor(conn, consumer)
    if state is None:
        conn.execute(
            ne_outbox_cursors.insert().values(
                consumer=consumer,
                position=0,
                event_types=requested,
                updated_at=_now(),
            )
        )
        position = 0
    else:
        _check_filter(consumer, state.stored_filter, requested)
        position = state.position

    rows = conn.execute(
        sa.select(ne_outbox_events)
        .where(ne_outbox_events.c.id > position)
        .order_by(ne_outbox_events.c.id)
        .limit(limit)
    ).all()

    high_water = position
    records: list[OutboxRecord] = []
    for row in rows:
        high_water = max(high_water, int(row.id))
        if requested is not None and row.event_type not in requested:
            continue
        records.append(_row_to_record(row))
    return records, high_water


# Columns the Postgres cursor claim reads (position + filter + fence).
_CURSOR_CLAIM_COLS = (
    ne_outbox_cursors.c.position,
    ne_outbox_cursors.c.event_types,
    ne_outbox_cursors.c.fence_xmax,
    ne_outbox_cursors.c.fence_last_id,
)

# The identity sequence's last dispensed value. Read STRICTLY BEFORE the reader's
# own xid is assigned (by the FOR UPDATE cursor claim), so the fence anchor
# (``my_xid``) strictly exceeds every producer xid that dispensed an id
# ``<= last_now``. ``last_value`` is 0 before the first row is inserted.
_LAST_VALUE_SQL = sa.text(
    "SELECT COALESCE("
    "pg_sequence_last_value(pg_get_serial_sequence('ne_outbox_events', 'id')), 0"
    ")::bigint AS last_now"
)
# The reader transaction's OWN xid (assigned by the FOR UPDATE / INSERT above).
# This is the fence anchor: it is greater than every xid assigned before it,
# including every producer xid for an id <= last_now (each such producer assigned
# its xid before its nextval — see :func:`append` — before we read last_now,
# before this xid). ``pg_current_xact_id`` returns the already-assigned xid.
_MY_XID_SQL = sa.text("SELECT pg_current_xact_id()::text::bigint AS my_xid")
# The oldest still-running xid (own xid excluded). When it reaches a stored fence
# anchor, every xid below that anchor — hence every producer of an id <=
# fence_last_id — has finished, so a still-invisible id below the fence is a
# permanent rollback hole (dead, safe to skip).
_XMIN_SQL = sa.text("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint AS xmin_now")


def _claim_cursor_row(conn: sa.Connection, consumer: str) -> Any | None:
    return conn.execute(
        sa.select(*_CURSOR_CLAIM_COLS)
        .where(ne_outbox_cursors.c.consumer == consumer)
        .with_for_update(skip_locked=True)
    ).first()


def _read_batch_postgres(
    conn: sa.Connection,
    consumer: str,
    requested: list[str] | None,
    limit: int,
) -> tuple[list[OutboxRecord], int]:
    # Read the sequence's last dispensed id BEFORE any xid is assigned to this
    # transaction (a plain SELECT assigns none). Every id <= last_now was
    # dispensed by a producer whose xid was assigned before its nextval (see
    # :func:`append`), hence before this read.
    last_now = int(conn.execute(_LAST_VALUE_SQL).scalar_one())

    claimed = _claim_cursor_row(conn, consumer)
    if claimed is None:
        # The row is either absent (register it) or held by a competing worker.
        existing = conn.execute(
            sa.select(ne_outbox_cursors.c.position).where(ne_outbox_cursors.c.consumer == consumer)
        ).first()
        if existing is not None:
            # Held by another worker: read nothing, do not block, do not
            # double-deliver. The returned position acks to a no-op.
            return [], int(existing.position)
        # First sight: register the cursor. A concurrent registrant may win the
        # unique key — fall back to re-claiming the now-existing row.
        try:
            with conn.begin_nested():
                conn.execute(
                    ne_outbox_cursors.insert().values(
                        consumer=consumer,
                        position=0,
                        event_types=requested,
                        updated_at=_now(),
                    )
                )
        except IntegrityError:
            pass
        claimed = _claim_cursor_row(conn, consumer)
        if claimed is None:
            existing = conn.execute(
                sa.select(ne_outbox_cursors.c.position).where(
                    ne_outbox_cursors.c.consumer == consumer
                )
            ).first()
            return [], int(existing.position) if existing is not None else 0

    _check_filter(consumer, _normalize_filter(claimed.event_types), requested)
    position = int(claimed.position)
    fence_xmax = None if claimed.fence_xmax is None else int(claimed.fence_xmax)
    fence_last_id = None if claimed.fence_last_id is None else int(claimed.fence_last_id)

    # my_xid (assigned by the FOR UPDATE / INSERT above) is the fence anchor;
    # xmin_now matures a PRIOR tick's fence. ``pg_snapshot_xmax`` is deliberately
    # NOT used: an in-flight producer's xid can sit AT/ABOVE xmax (xmax is one
    # past the highest *completed* xid), so xmax cannot bound in-flight producers
    # — only a reader-assigned anchor can.
    my_xid = int(conn.execute(_MY_XID_SQL).scalar_one())
    xmin_now = int(conn.execute(_XMIN_SQL).scalar_one())

    # Dead-hole horizon: the highest id we may SKIP a hole below (an invisible id
    # whose producer has provably finished == a permanent rollback hole). A
    # committed-but-not-yet-settled id is never skipped — the contiguous-visible
    # walk below stops at the first gap that is NOT proven dead.
    if fence_xmax is not None and xmin_now >= fence_xmax:
        dead_horizon = fence_last_id if fence_last_id is not None else position
        persist_fence = True  # matured: advance the fence for the next tick
    else:
        dead_horizon = position
        persist_fence = fence_xmax is None  # establish the fence on first sight
    if persist_fence:
        conn.execute(
            ne_outbox_cursors.update()
            .where(ne_outbox_cursors.c.consumer == consumer)
            .values(fence_xmax=my_xid, fence_last_id=last_now, updated_at=_now())
        )

    # Fetch the VISIBLE rows just above the cursor (committed rows, plus any this
    # very transaction wrote — the same-tx append+read case). Invisible ids
    # (uncommitted or aborted) simply do not appear.
    rows = conn.execute(
        sa.select(ne_outbox_events)
        .where(ne_outbox_events.c.id > position)
        .where(ne_outbox_events.c.id <= last_now)
        .order_by(ne_outbox_events.c.id)
        .limit(limit)
    ).all()
    present = {int(row.id): row for row in rows}
    # If the window filled the limit there may be visible rows beyond it, so we
    # must not advance past the last id we actually fetched; otherwise we have
    # seen every visible row up to last_now and may skip proven-dead holes up to
    # last_now.
    window_filled = len(rows) == limit
    scan_cap = max(present, default=position) if window_filled else last_now

    # Contiguous walk from the cursor: deliver each visible row, skip a hole only
    # when it is proven dead (<= dead_horizon), and STOP at the first gap that is
    # neither present nor proven dead — that is a lower id still in flight, which
    # at-least-once forbids us to cross.
    high_water = position
    records: list[OutboxRecord] = []
    expected = position + 1
    while expected <= scan_cap:
        row = present.get(expected)
        if row is not None:
            high_water = expected
            if requested is None or row.event_type in requested:
                records.append(_row_to_record(row))
        elif expected <= dead_horizon:
            high_water = expected  # permanent rollback hole — skip past it
        else:
            break  # uncommitted lower id — never skip it
        expected += 1

    return records, high_water


def ack(conn: sa.Connection, consumer: str, upto_id: int) -> None:
    """Advance ``consumer``'s cursor to ``upto_id`` (monotonic; never backwards).

    ``upto_id`` below the current position raises :class:`CursorRegressionError`;
    equal is a no-op; greater advances the stored position and ``updated_at``.

    The advance is a single **atomic conditional UPDATE** —
    ``SET position = :upto WHERE consumer = :c AND position < :upto`` — so two
    concurrent acks can never regress the cursor: whichever lands second is a
    no-op if it would move the cursor backwards. The non-locking pre-read only
    raises :class:`CursorRegressionError` for an unambiguously-backwards ack; a
    racing ack that the pre-read did not yet see is silently absorbed by the
    ``position < :upto`` guard rather than clobbering a higher committed cursor.
    Identical on SQLite and Postgres.
    """
    state = _current_cursor(conn, consumer)
    current = state.position if state is not None else 0
    if upto_id < current:
        raise CursorRegressionError(
            f"outbox consumer {consumer!r} cursor is at {current}; refused to ack "
            f"backwards to {upto_id}"
        )
    if upto_id == current:
        return
    if state is None:
        try:
            with conn.begin_nested():
                conn.execute(
                    ne_outbox_cursors.insert().values(
                        consumer=consumer, position=upto_id, event_types=None, updated_at=_now()
                    )
                )
            return
        except IntegrityError:
            # Concurrently registered between the pre-read and the insert; fall
            # through to the conditional UPDATE below.
            pass
    conn.execute(
        ne_outbox_cursors.update()
        .where(ne_outbox_cursors.c.consumer == consumer)
        .where(ne_outbox_cursors.c.position < upto_id)
        .values(position=upto_id, updated_at=_now())
    )


def drain_once(
    engine: sa.Engine,
    consumer: str,
    handler: Callable[[list[OutboxRecord]], Any],
    *,
    limit: int = 100,
    event_types: Sequence[str] | None = None,
) -> DrainResult:
    """One read → process → ack tick. The exporter loop (ne-worker) calls this.

    Reads a window in one transaction, runs ``handler`` OUTSIDE any transaction,
    and only on success advances the cursor in a SECOND transaction. A handler
    exception is captured into ``DrainResult.error`` with the cursor UNMOVED
    (fail-open, at-least-once) — the loop keeps ticking and the events redeliver.
    Database faults (in the read or ack transactions) propagate to the caller.

    At-least-once holds under CONCURRENT Postgres producers: :func:`read_batch`
    never returns (and this tick therefore never acks) past the settled horizon,
    so an in-flight lower-id producer whose id sits in a gap is never skipped —
    when it commits, a later tick delivers it. Running MULTIPLE drain workers on
    one consumer is safe: the ``FOR UPDATE SKIP LOCKED`` cursor claim keeps two
    workers from delivering the same window in lockstep, and the read/ack split
    means at worst a window redelivers (at-least-once), never drops.
    """
    with engine.begin() as conn:
        records, high_water = read_batch(conn, consumer, limit=limit, event_types=event_types)
        state = _current_cursor(conn, consumer)
        old_position = state.position if state is not None else 0

    try:
        handler(records)
    except Exception as exc:
        return DrainResult(processed=0, acked_to=old_position, error=repr(exc))

    try:
        with engine.begin() as conn:
            ack(conn, consumer, high_water)
    except CursorRegressionError:
        # A COMPETING drainer of the same consumer already acked past this
        # window between our read and our ack (multi-worker at-least-once with
        # overlapping delivery — the events WERE processed, the shared cursor is
        # simply further ahead). Benign: the atomic conditional UPDATE would have
        # no-op'd anyway; we only reach here because the non-locking pre-read saw
        # the peer's higher position. Never a data loss.
        pass
    return DrainResult(processed=len(records), acked_to=high_water, error=None)


# ---------------------------------------------------------------------------
# Retention / pruning (P2-21 — bound ne_outbox_events)
# ---------------------------------------------------------------------------
def prune_acked(engine: sa.Engine, *, older_than_s: float, now: datetime | None = None) -> int:
    """Delete acked outbox rows older than ``older_than_s``; return the row count.

    Retention that keeps ``ne_outbox_events`` bounded (P2-21): the only rows deleted
    are those at/below ``MIN(position)`` across ALL registered consumer cursors AND
    older than the retention window. Returns the number of rows deleted.

    SAFETY (why this cannot affect at-least-once or the NE-070 dead-hole fence):
    :func:`read_batch` only ever scans ``id > position >= MIN(position)`` and the
    Postgres contiguous-visible walk starts strictly ABOVE the cursor, so a row at or
    below EVERY cursor is unreachable by any consumer's future read — deleting it can
    never skip an undelivered event nor perturb the settled-horizon fence (which only
    ever advances forward past the cursor). We use ``id <= MIN(position)``, never a
    per-consumer or above-any-cursor predicate — those WOULD break at-least-once for a
    lagging consumer.

    ZERO CONSUMERS ⇒ PRUNE NOTHING: if no cursor row exists (``MIN(position)`` is
    NULL) nothing has been delivered, so returning 0 avoids silently dropping
    undelivered events. A deployment that clears every consumer therefore re-opens
    unbounded growth and must treat that as an explicit operator choice (see the
    NE_WORKER_OUTBOX_CONSUMER posture in docs/DEPLOYMENT.md).

    ``occurred_at`` (client-written, tz-aware, dialect-consistent) is the retention
    clock — NOT ``created_at``, whose ``server_default`` string form differs between
    SQLite ``CURRENT_TIMESTAMP`` and bound datetimes. Identical on SQLite and Postgres.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=older_than_s)
    with engine.begin() as conn:
        min_pos = conn.execute(sa.select(sa.func.min(ne_outbox_cursors.c.position))).scalar()
        if min_pos is None:
            return 0  # no consumers ⇒ nothing delivered ⇒ prune nothing
        result = conn.execute(
            ne_outbox_events.delete()
            .where(ne_outbox_events.c.id <= int(min_pos))
            .where(ne_outbox_events.c.occurred_at < cutoff)
        )
        return result.rowcount
