# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The hash-chained, append-only audit log — the chain core (sync, dialect-neutral).

Two SQLAlchemy-Core tables:

* ``ne_audit_log`` — the append-only chain. Every row links to its predecessor
  through a hash chain: ``record_hash = SHA-256(prev_hash_hex_utf8 +
  canonical_json(payload))``, so mutating any record (or reordering/removing one)
  breaks the chain and :func:`ne.audit.verify.verify_chain` names the exact break.
  ``seq`` is a *dense* chain position assigned under the head lock — deliberately
  NOT the identity/rowid column, so a rolled-back INSERT cannot leave a gap that
  looks like a deletion. The denormalized query columns (``event_id``,
  ``event_type``, ``config_hash``, ``actor``, ``occurred_at``, ``kpi_tag``,
  ``trace_id``, ``action``, ``resource``, ``outcome``) exist ONLY for indexed
  WHERE filtering: every value read back derives from the hash-covered ``payload``
  (see :func:`_record_from_row`), and :func:`ne.audit.verify.verify_chain`
  cross-checks each column against the payload, so a tampered column can never
  surface forged content through replay or append's idempotent return.
* ``ne_audit_head`` — a single-row (``id = 1``) pointer at the chain head
  (``seq`` + ``record_hash``). Reading it ``FOR UPDATE`` serializes concurrent
  appends on Postgres; on SQLite writers serialize through the database write lock
  (configure the engine for ``BEGIN IMMEDIATE`` when appending concurrently).

Append-only is enforced BOTH app-level (append is the only write surface — there
is no update/delete API) AND database-level (BEFORE UPDATE/DELETE triggers on
``ne_audit_log`` on both dialects — SPEC C6, no bypass).

RELATIONSHIP TO ne-events (plan §1.4 event-spine rule): the audit sink is the
durable, tamper-evident AUTHORITATIVE store; the ne-events outbox is the EXPORT
transport. They are aligned, not duplicated: :func:`append_record` writes on the
CALLER's connection exactly like :func:`ne.events.append`, so an emitter can
append to BOTH stores in one transaction and they commit/roll back atomically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql

from ne.contracts.events import AuditEventLike

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: The prev_hash of the very first record (seq 1) and the genesis head record_hash.
GENESIS_HASH = "0" * 64

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
metadata = sa.MetaData()

ne_audit_log = sa.Table(
    "ne_audit_log",
    metadata,
    # BigInteger on Postgres (Identity); Integer on SQLite so it aliases the rowid
    # and autoincrements (SQLite only autoincrements INTEGER PRIMARY KEY).
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    ),
    # Dense chain position, assigned under the head lock (NOT the identity column).
    sa.Column("seq", sa.BigInteger, nullable=False, unique=True),
    sa.Column("event_id", sa.String, nullable=False, unique=True),
    sa.Column("event_type", sa.String, nullable=False),
    sa.Column("config_hash", sa.String, nullable=False),
    sa.Column("actor", sa.String, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
    # Denormalized query columns (nullable — populated when present on the event).
    sa.Column("kpi_tag", sa.String, nullable=True),
    sa.Column("trace_id", sa.String, nullable=True, index=True),
    sa.Column("action", sa.String, nullable=True),
    sa.Column("resource", sa.String, nullable=True),
    sa.Column("outcome", sa.String, nullable=True),
    sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
    ),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("record_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

ne_audit_head = sa.Table(
    "ne_audit_head",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("seq", sa.BigInteger, nullable=False),
    sa.Column("record_hash", sa.String(64), nullable=False),
    sa.CheckConstraint("id = 1", name="ne_audit_head_singleton"),
)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuditRecord:
    """One chain row as appended/read back (a frozen mirror of ``ne_audit_log``)."""

    id: int
    seq: int
    event_id: str
    event_type: str
    config_hash: str
    actor: str
    occurred_at: datetime
    kpi_tag: str | None
    trace_id: str | None
    action: str | None
    resource: str | None
    outcome: str | None
    payload: dict[str, Any]
    prev_hash: str
    record_hash: str


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical JSON encoding of ``payload`` (sorted keys, no whitespace).

    Key-order-insensitive and whitespace-free so the digest is stable across the
    write path (``model_dump(mode="json")``) and the read-back path (JSON/JSONB
    deserialization), on both SQLite and Postgres.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_record_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """``SHA-256(prev_hash_hex_as_utf8 + canonical_json(payload))`` as lowercase hex."""
    h = hashlib.sha256()
    h.update(prev_hash.encode("ascii"))
    h.update(canonical_bytes(payload))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_SQLITE_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS ne_audit_log_no_update
    BEFORE UPDATE ON ne_audit_log
    BEGIN SELECT RAISE(ABORT, 'ne_audit_log is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ne_audit_log_no_delete
    BEFORE DELETE ON ne_audit_log
    BEGIN SELECT RAISE(ABORT, 'ne_audit_log is append-only'); END
    """,
)

_PG_TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION ne_audit_log_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'ne_audit_log is append-only (no UPDATE/DELETE)';
    END;
    $$ LANGUAGE plpgsql
"""

_PG_TRIGGER_DROP = "DROP TRIGGER IF EXISTS ne_audit_log_append_only ON ne_audit_log"

_PG_TRIGGER_CREATE = """
    CREATE TRIGGER ne_audit_log_append_only
    BEFORE UPDATE OR DELETE ON ne_audit_log
    FOR EACH ROW EXECUTE FUNCTION ne_audit_log_append_only()
"""

# TRUNCATE is a statement-level operation that row-level (FOR EACH ROW) triggers
# never fire for, so it needs its own FOR EACH STATEMENT trigger — otherwise a
# caller with table privileges could wipe the authoritative chain (SPEC C6
# no-bypass). The same unconditional-RAISE function is reused.
_PG_TRUNCATE_TRIGGER_DROP = "DROP TRIGGER IF EXISTS ne_audit_log_no_truncate ON ne_audit_log"

_PG_TRUNCATE_TRIGGER_CREATE = """
    CREATE TRIGGER ne_audit_log_no_truncate
    BEFORE TRUNCATE ON ne_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION ne_audit_log_append_only()
"""


def _install_append_only_triggers(conn: sa.Connection) -> None:
    dialect = conn.dialect.name
    if dialect == "sqlite":
        # SQLite has no TRUNCATE statement; UPDATE/DELETE triggers suffice.
        for stmt in _SQLITE_TRIGGERS:
            conn.exec_driver_sql(stmt)
    elif dialect == "postgresql":
        conn.exec_driver_sql(_PG_TRIGGER_FUNCTION)
        conn.exec_driver_sql(_PG_TRIGGER_DROP)
        conn.exec_driver_sql(_PG_TRIGGER_CREATE)
        conn.exec_driver_sql(_PG_TRUNCATE_TRIGGER_DROP)
        conn.exec_driver_sql(_PG_TRUNCATE_TRIGGER_CREATE)
    # Other dialects: app-level append-only still holds; no DB trigger installed.


def ensure_audit_schema(engine: sa.Engine) -> None:
    """Create the audit tables, seed the genesis head, and install the triggers.

    Idempotent: safe to run repeatedly. ``create_all`` skips existing tables, the
    genesis head is inserted only when absent, and the triggers use
    ``IF NOT EXISTS`` / ``CREATE OR REPLACE`` so re-runs never duplicate or fail.

    (The real migration runner is NE-069's; this is the create-all bootstrap seam
    tests and early deployments use — mirrors ``ne.events.ensure_outbox_schema``.)
    """
    metadata.create_all(engine)
    with engine.begin() as conn:
        exists = conn.execute(sa.select(ne_audit_head.c.id).where(ne_audit_head.c.id == 1)).first()
        if exists is None:
            conn.execute(ne_audit_head.insert().values(id=1, seq=0, record_hash=GENESIS_HASH))
        _install_append_only_triggers(conn)


# ---------------------------------------------------------------------------
# Append (the ONLY write surface)
# ---------------------------------------------------------------------------
def _payload_datetime(value: Any) -> datetime:
    """Coerce a payload ``occurred_at`` to a :class:`datetime`.

    The payload is ``model_dump(mode="json")`` output, so ``occurred_at`` is an ISO
    string (e.g. ``'2026-01-01T12:00:00Z'``) — but a live-constructed pydantic model
    round-trip can also leave a real :class:`datetime`. Both are accepted; anything
    else raises so a malformed ``occurred_at`` is loud, not silently stringified.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(
        f"payload 'occurred_at' must be a datetime or ISO string; got {type(value).__name__}"
    )


def _record_from_values(
    *, row_id: int, seq: int, payload: dict[str, Any], prev_hash: str, record_hash: str
) -> AuditRecord:
    return AuditRecord(
        id=row_id,
        seq=seq,
        event_id=cast(str, payload["event_id"]),
        event_type=cast(str, payload["event_type"]),
        config_hash=cast(str, payload["config_hash"]),
        actor=cast(str, payload["actor"]),
        occurred_at=_payload_datetime(payload["occurred_at"]),
        kpi_tag=payload.get("kpi_tag"),
        trace_id=payload.get("trace_id"),
        action=payload.get("action"),
        resource=payload.get("resource"),
        outcome=payload.get("outcome"),
        payload=payload,
        prev_hash=prev_hash,
        record_hash=record_hash,
    )


def _record_from_row(row: sa.Row[Any]) -> AuditRecord:
    """Build an :class:`AuditRecord` from a chain row — payload-authoritative.

    EVERY event field derives from the hash-covered ``payload``; the denormalized
    columns exist ONLY for indexed WHERE filtering, so a tampered column can never
    surface forged content through replay or append's idempotent return. (Column
    tampering is separately DETECTED and named by
    :func:`ne.audit.verify.verify_chain`.)
    """
    return _record_from_values(
        row_id=int(row.id),
        seq=int(row.seq),
        payload=dict(row.payload),
        prev_hash=row.prev_hash,
        record_hash=row.record_hash,
    )


def append_record(conn: sa.Connection, event: AuditEventLike) -> AuditRecord:
    """Append ``event`` to the chain on the CALLER's connection; return the row.

    MUST be called on the caller's transaction so the log row + head update commit
    atomically with any co-located business write / outbox append — that atomicity
    is what makes the sink the authoritative store aligned with the ne-events
    outbox (plan §1.4). ``event`` must be a pydantic model carrying a non-empty
    string ``event_id`` (the generated ``ne.schema`` events are); anything else
    raises :class:`TypeError`.

    The head row is read ``FOR UPDATE`` (serializes appends on Postgres). Under the
    lock, an already-present ``event_id`` returns the existing record UNCHANGED and
    does NOT extend the chain — appends are idempotent, so an emitter can retry a
    delivery without corrupting the chain (SPEC C6). Otherwise ``seq = head.seq +
    1``, ``prev_hash = head.record_hash``, ``record_hash`` is computed, the row is
    inserted, and the head advances — all on ``conn``.
    """
    if not isinstance(event, BaseModel):
        raise TypeError(
            f"append_record() requires a pydantic BaseModel event; got {type(event).__name__}"
        )
    payload = event.model_dump(mode="json")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise TypeError("append_record() requires an event carrying a non-empty string 'event_id'")

    # Serialize on the head row (FOR UPDATE — a no-op on SQLite, which serializes
    # writers through the database write lock).
    head = conn.execute(
        sa.select(ne_audit_head.c.seq, ne_audit_head.c.record_hash)
        .where(ne_audit_head.c.id == 1)
        .with_for_update()
    ).one()

    existing = conn.execute(
        ne_audit_log.select().where(ne_audit_log.c.event_id == event_id)
    ).first()
    if existing is not None:
        return _record_from_row(existing)

    seq = int(head.seq) + 1
    prev_hash = cast(str, head.record_hash)
    record_hash = compute_record_hash(prev_hash, payload)

    result = conn.execute(
        ne_audit_log.insert().values(
            seq=seq,
            event_id=event_id,
            event_type=payload["event_type"],
            config_hash=payload["config_hash"],
            actor=payload["actor"],
            occurred_at=event.occurred_at,
            kpi_tag=payload.get("kpi_tag"),
            trace_id=payload.get("trace_id"),
            action=payload.get("action"),
            resource=payload.get("resource"),
            outcome=payload.get("outcome"),
            payload=payload,
            prev_hash=prev_hash,
            record_hash=record_hash,
        )
    )
    pk = result.inserted_primary_key
    if pk is None:  # pragma: no cover - a single-row insert always yields a pk
        raise RuntimeError("audit insert returned no primary key")

    conn.execute(
        ne_audit_head.update()
        .where(ne_audit_head.c.id == 1)
        .values(seq=seq, record_hash=record_hash)
    )
    return _record_from_values(
        row_id=int(pk[0]),
        seq=seq,
        payload=payload,
        prev_hash=prev_hash,
        record_hash=record_hash,
    )


def read_head(conn: sa.Connection) -> tuple[int, str]:
    """Return the current chain head as ``(seq, record_hash)``."""
    row = conn.execute(
        sa.select(ne_audit_head.c.seq, ne_audit_head.c.record_hash).where(ne_audit_head.c.id == 1)
    ).one()
    return int(row.seq), cast(str, row.record_hash)
