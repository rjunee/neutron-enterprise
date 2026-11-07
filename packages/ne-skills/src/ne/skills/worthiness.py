# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Worthiness scoring + the rate cap + the never-drop ranked queue (NE-SKILL-2).

The FLOOR stays where NE-SKILL-1b put it (``succeeded ∧ ≥2 distinct actions`` —
enforced in ``forge.py``). This module adds the RANK and the CAP:

- **Rank** (plan §4.2 + §4.7): repetition-in-30d PRIMARY (the strongest recur
  signal — the same ``workflow_signature`` seen 2+ times over the sightings history),
  then the §4.7 generalizability term (distinct principals), then step count
  (efficiency — a longer collapsed workflow is worth more), then recency, then a
  deterministic signature tie-break. Worthiness RANKS; it never DROPS a worthy
  candidate — excess over the cap persists in ``ne_skill_forge_queue`` and is
  authored on a later run.
- **Rate cap** (plan §4.2): ≤2 OPEN ``skill_candidate``/``skill_update_candidate``
  proposals (status folded from the review log, MIRRORING
  ``ne.api.steward_metrics.folded_status_map`` — ne-skills may NOT import ne-api)
  and ~1 new/day (the last skill-lane proposal's ``created_at`` inside a
  cron-jitter-tolerant 20h window).

Schema style mirrors ``registry.py``/``forge.py``: BigInteger-with-sqlite-Integer
Identity PKs, tz-aware ``DateTime``, ``sa.JSON`` with a Postgres JSONB variant.
All writes ride the forge's ONE transaction (conn-level helpers); the reads are
engine-level folds. ``ensure_birth_schema`` is idempotent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from ne.contracts.skills import WorkflowSnapshot
from ne.flywheel.proposals import ProposalKind, SqlAlchemyProposalStore, ne_fix_proposals
from ne.flywheel.review import ne_proposal_reviews

#: The window over which repeated sightings of a signature count as recurrence.
REPETITION_WINDOW_DAYS = 30
#: The default rate cap: at most this many OPEN skill proposals at once.
DEFAULT_OPEN_CAP = 2
#: The "~1/day" cap window. 20h (not 24) is cron-jitter tolerant: a daily cron
#: firing a little early on consecutive days must not be starved to every-other-day.
DAILY_WINDOW = timedelta(hours=20)

#: The two skill proposal kinds the rate cap + already-proposed check fold over.
_SKILL_KINDS = (ProposalKind.SKILL_CANDIDATE.value, ProposalKind.SKILL_UPDATE_CANDIDATE.value)
#: The folded statuses that count as an OPEN proposal (never a terminal one).
_OPEN_STATUSES = frozenset({"proposed", "approved"})
#: Documented pilot-scale cap on the proposal folds (R10 precedent, mirrors the
#: steward router's ``read_proposals(limit=500)``).
_FOLD_LIMIT = 500


birth_metadata = sa.MetaData()


def _big_id() -> sa.Column[int]:
    return sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.Identity(),
        primary_key=True,
    )


def _json() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


ne_skill_signature_sightings = sa.Table(
    "ne_skill_signature_sightings",
    birth_metadata,
    _big_id(),
    sa.Column("signature", sa.String, nullable=False, index=True),
    sa.Column("snapshot_id", sa.String, nullable=False, unique=True),
    sa.Column("principal_id", sa.String, nullable=False),
    sa.Column("session_id", sa.String, nullable=False, default=""),
    sa.Column("department", sa.String, nullable=False, default=""),
    sa.Column("step_count", sa.Integer, nullable=False),
    sa.Column("distinct_actions", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_ne_skill_sightings_signature_seen", "signature", "seen_at"),
)

ne_skill_forge_queue = sa.Table(
    "ne_skill_forge_queue",
    birth_metadata,
    # The signature IS the identity — one queued candidate per workflow shape.
    # The snapshot JSON is a WorkflowSnapshot model_dump; C12-safe by construction
    # (a WorkflowStep carries no argument values, only action names).
    sa.Column("signature", sa.String, primary_key=True),
    sa.Column("snapshot", _json(), nullable=False),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def ensure_birth_schema(engine: sa.Engine) -> None:
    """Create the sightings + queue tables. Idempotent (``create_all``)."""
    birth_metadata.create_all(engine)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SightingEntry(NamedTuple):
    """One floor-passing snapshot's recurrence-tracking row (names/counts only, C12)."""

    signature: str
    snapshot_id: str
    principal_id: str
    session_id: str
    department: str
    step_count: int
    distinct_actions: int
    finished_at: datetime


class CandidateStat(NamedTuple):
    """The ranking inputs for one candidate signature (a deterministic sort key)."""

    signature: str
    repetition_30d: int
    distinct_principals_30d: int
    step_count: int
    finished_at: datetime


# -- conn-level writes (ride the forge's ONE transaction) ----------------------


def record_sightings(conn: sa.Connection, entries: Iterable[SightingEntry], now: datetime) -> None:
    """Append one sighting per snapshot, idempotent on ``snapshot_id``.

    Each INSERT runs in a SAVEPOINT (``begin_nested``); a duplicate ``snapshot_id``
    unique-violation rolls back just that SAVEPOINT and is swallowed, leaving the
    caller's enclosing transaction usable (mirrors ``proposals.append``).
    """
    for entry in entries:
        stmt = ne_skill_signature_sightings.insert().values(
            signature=entry.signature,
            snapshot_id=entry.snapshot_id,
            principal_id=entry.principal_id,
            session_id=entry.session_id,
            department=entry.department,
            step_count=entry.step_count,
            distinct_actions=entry.distinct_actions,
            finished_at=entry.finished_at,
            seen_at=now,
        )
        try:
            with conn.begin_nested():
                conn.execute(stmt)
        except IntegrityError:
            continue


def upsert_queue(
    conn: sa.Connection, signature: str, snapshot_json: dict[str, Any], now: datetime
) -> None:
    """Insert-or-update the ranked queue row for ``signature`` (never dropped)."""
    result = conn.execute(
        ne_skill_forge_queue.update()
        .where(ne_skill_forge_queue.c.signature == signature)
        .values(snapshot=snapshot_json, updated_at=now)
    )
    if result.rowcount == 0:
        conn.execute(
            ne_skill_forge_queue.insert().values(
                signature=signature,
                snapshot=snapshot_json,
                queued_at=now,
                updated_at=now,
            )
        )


def delete_queued(conn: sa.Connection, signatures: Iterable[str]) -> None:
    """Remove ``signatures`` from the queue (authored / suppressed / dropped)."""
    sigs = list(dict.fromkeys(signatures))
    if not sigs:
        return
    conn.execute(ne_skill_forge_queue.delete().where(ne_skill_forge_queue.c.signature.in_(sigs)))


# -- engine-level reads (folds) ------------------------------------------------


def load_queue(engine: sa.Engine) -> list[tuple[str, WorkflowSnapshot]]:
    """Rehydrate the persisted queue into ``(signature, WorkflowSnapshot)`` pairs."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(ne_skill_forge_queue.c.signature, ne_skill_forge_queue.c.snapshot)
        ).all()
    out: list[tuple[str, WorkflowSnapshot]] = []
    for row in rows:
        out.append((str(row.signature), WorkflowSnapshot.model_validate(row.snapshot)))
    return out


def repetition_counts(
    engine: sa.Engine, signatures: Sequence[str], *, since: datetime
) -> dict[str, int]:
    """Count stored sightings per signature with ``seen_at >= since`` (30d window)."""
    if not signatures:
        return {}
    since = _as_utc(since)
    stmt = (
        sa.select(
            ne_skill_signature_sightings.c.signature,
            sa.func.count().label("n"),
        )
        .where(
            ne_skill_signature_sightings.c.signature.in_(list(signatures)),
            ne_skill_signature_sightings.c.seen_at >= since,
        )
        .group_by(ne_skill_signature_sightings.c.signature)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {str(sig): int(n) for sig, n in rows}


def distinct_principal_counts(
    engine: sa.Engine, signatures: Sequence[str], *, since: datetime
) -> dict[str, int]:
    """Count DISTINCT principals per signature with ``seen_at >= since`` (§4.7)."""
    if not signatures:
        return {}
    since = _as_utc(since)
    stmt = (
        sa.select(
            ne_skill_signature_sightings.c.signature,
            sa.func.count(sa.func.distinct(ne_skill_signature_sightings.c.principal_id)).label("n"),
        )
        .where(
            ne_skill_signature_sightings.c.signature.in_(list(signatures)),
            ne_skill_signature_sightings.c.seen_at >= since,
        )
        .group_by(ne_skill_signature_sightings.c.signature)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {str(sig): int(n) for sig, n in rows}


def growth_since(engine: sa.Engine, signature: str, *, after: datetime) -> int:
    """Count stored sightings for ``signature`` with ``seen_at > after``.

    The decline-suppression re-proposal trigger: a rejected signature whose
    recurrence GROWS by ≥ threshold after the rejection earns a fresh look.
    """
    after = _as_utc(after)
    stmt = sa.select(sa.func.count()).where(
        ne_skill_signature_sightings.c.signature == signature,
        ne_skill_signature_sightings.c.seen_at > after,
    )
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


# -- rate-cap folds (MIRROR steward_metrics.folded_status_map; no ne-api import) --


def _folded_skill_status_map(engine: sa.Engine, proposal_ids: Sequence[str]) -> dict[str, str]:
    """``{proposal_id: folded_status}`` for the given ids via ONE aggregated query.

    The live status is the ``to_status`` of the highest-``id`` review row per
    proposal (the append-only log's last word); an id absent from the map has no
    review rows and is therefore ``proposed``. Mirrors
    ``ne.api.steward_metrics.folded_status_map`` verbatim (ne-skills may NOT import
    ne-api, so the SQL is duplicated, not imported).
    """
    if not proposal_ids:
        return {}
    latest = (
        sa.select(sa.func.max(ne_proposal_reviews.c.id).label("mid"))
        .where(ne_proposal_reviews.c.proposal_id.in_(list(proposal_ids)))
        .group_by(ne_proposal_reviews.c.proposal_id)
        .subquery()
    )
    stmt = sa.select(ne_proposal_reviews.c.proposal_id, ne_proposal_reviews.c.to_status).where(
        ne_proposal_reviews.c.id.in_(sa.select(latest.c.mid))
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {str(row.proposal_id): str(row.to_status) for row in rows}


def open_skill_proposal_count(engine: sa.Engine, store: SqlAlchemyProposalStore) -> int:
    """Count OPEN (proposed|approved) skill(_update)_candidate proposals.

    Reads both kinds (pilot cap 500, R10) and folds each proposal's live status
    from the review log — never reads/writes a status column.
    """
    proposals = []
    for kind in _SKILL_KINDS:
        proposals.extend(store.read_proposals(kind=kind, limit=_FOLD_LIMIT))
    status_map = _folded_skill_status_map(engine, [p.proposal_id for p in proposals])
    return sum(1 for p in proposals if status_map.get(p.proposal_id, "proposed") in _OPEN_STATUSES)


def last_skill_proposal_at(engine: sa.Engine) -> datetime | None:
    """The MAX ``created_at`` over both skill kinds, regardless of folded status."""
    stmt = sa.select(sa.func.max(ne_fix_proposals.c.created_at)).where(
        ne_fix_proposals.c.kind.in_(list(_SKILL_KINDS))
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if row is None or row[0] is None:
        return None
    return _as_utc(row[0])


# -- ranking -------------------------------------------------------------------


def rank_candidates(stats: Sequence[CandidateStat]) -> list[CandidateStat]:
    """Order candidates by the deterministic §4.2+§4.7 composite key.

    Sort key (all descending except the final tie-break):
    ``(-repetition_30d, -distinct_principals_30d, -step_count,
    -finished_at.timestamp(), signature)``. Repetition stays PRIMARY per plan
    §4.2; distinct-principals (generalizability) and step-count (efficiency) are
    the §4.7 terms; recency then the signature give a total, stable order.
    """
    return sorted(
        stats,
        key=lambda s: (
            -s.repetition_30d,
            -s.distinct_principals_30d,
            -s.step_count,
            -_as_utc(s.finished_at).timestamp(),
            s.signature,
        ),
    )


__all__ = [
    "DAILY_WINDOW",
    "DEFAULT_OPEN_CAP",
    "REPETITION_WINDOW_DAYS",
    "CandidateStat",
    "SightingEntry",
    "birth_metadata",
    "delete_queued",
    "distinct_principal_counts",
    "ensure_birth_schema",
    "growth_since",
    "last_skill_proposal_at",
    "load_queue",
    "ne_skill_forge_queue",
    "ne_skill_signature_sightings",
    "open_skill_proposal_count",
    "rank_candidates",
    "record_sightings",
    "repetition_counts",
    "upsert_queue",
]
