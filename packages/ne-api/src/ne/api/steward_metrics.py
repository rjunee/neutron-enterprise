# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne-api-computed steward digest + latency — READ-ONLY SQL aggregation (spec §1.7).

ne-flywheel carries no digest/latency computation (the NE-067 seam note: "the
Phase-6 ne-api seam note (ProposalReviewService/render_proposal_diff + digest/
latency SQL aggregations)"). This module implements those two aggregations as
READ-ONLY ``sa.select`` queries over ``ne_fix_proposals`` + ``ne_proposal_reviews``
on the flywheel engine. It NEVER writes (no INSERT / UPDATE / DELETE statement)
and imports no review-service / capture / curation write surface (a source-scan
guardian in test_no_bypass asserts this). It is a read-only consumer of the same
tables the governed review service owns — no chokepoint is modified.

Formulas (deterministic, encoded in test_steward_metrics.py):

* **Week window** — ``[week_start 00:00 UTC, +7d)`` — INCLUSIVE start (``>=``),
  EXCLUSIVE end (``<``). The default week_start is the current ISO-week Monday.
* **Digest** — ``proposed`` per lane = proposals whose ``created_at`` is in the
  window; ``approved`` / ``rejected`` / ``applied`` per lane = review rows (joined
  to their proposal for lane attribution) whose ``action`` is approve/reject/apply
  and whose ``created_at`` is in the window. ``by_lane`` lists ONLY lanes with a
  nonzero counter, in ``FixLane`` declaration order (content, retrieval, schema,
  policy) — an all-zero week yields ``by_lane=()`` so the console empty state
  renders. ``totals`` are the sums.
* **Latency** — per decided proposal ``wait = first_review.created_at -
  proposal.created_at`` (first review = MIN review ``created_at``). ``median_wait_s``
  = ``statistics.median(waits)``; ``p90_wait_s`` = NEAREST-RANK
  ``sorted_waits[ceil(0.9 * n) - 1]``; both ``None`` when ``decided_count == 0``.
  ``pending_count`` = proposals with NO review row (folded status ``proposed``);
  ``oldest_pending_age_s`` = ``now - oldest_pending.created_at`` (``None`` when zero
  pending). ``generated_at`` = ``now``.

SQLite parity: every ``created_at`` is read back naive, so every wait/age
computation normalizes BOTH operands through :func:`_as_utc` before subtraction
(mixed aware/naive subtraction raises). Window bounds are bound as UTC-aware
datetimes (all library writes are UTC-aware — the replay.py precedent).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import sqlalchemy as sa

from ne.flywheel.lanes import FixLane
from ne.flywheel.proposals import ne_fix_proposals
from ne.flywheel.review import ne_proposal_reviews

#: Review ``action`` value -> the digest counter it increments.
_ACTION_TO_COUNTER = {"approve": "approved", "reject": "rejected", "apply": "applied"}


def _as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC (SQLite reads back naive)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def iso_week_start(now: datetime) -> date:
    """Return the Monday of ``now``'s ISO week (current-week default window start)."""
    d = now.astimezone(UTC).date()
    return d - timedelta(days=d.weekday())


def folded_status_map(engine: sa.Engine) -> dict[str, str]:
    """Return ``{proposal_id: folded_status}`` via ONE aggregated query (no N+1).

    The live status is the ``to_status`` of the highest-``id`` review row per
    proposal (the append-only log's last word). A proposal absent from the map
    has no review rows and is therefore ``proposed`` (``to_status`` is never
    ``proposed``). This is the single aggregated fold the R10 note calls for —
    never a per-proposal ``status_of`` loop.
    """
    latest = (
        sa.select(sa.func.max(ne_proposal_reviews.c.id).label("mid"))
        .group_by(ne_proposal_reviews.c.proposal_id)
        .subquery()
    )
    stmt = sa.select(ne_proposal_reviews.c.proposal_id, ne_proposal_reviews.c.to_status).where(
        ne_proposal_reviews.c.id.in_(sa.select(latest.c.mid))
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {str(row.proposal_id): str(row.to_status) for row in rows}


@dataclass(frozen=True)
class LaneDigest:
    """Per-lane proposal/review counts over the digest window."""

    lane: str
    proposed: int
    approved: int
    rejected: int
    applied: int


# -- NE-SKILL-4 rot digest section (READ-ONLY parse of the latest gardener run) --


@dataclass(frozen=True)
class SkillsRetirementRow:
    name: str
    reason: str
    fired_count: int


@dataclass(frozen=True)
class SkillsFailureRow:
    name: str
    known_fires: int
    failed: int
    ratio: float


@dataclass(frozen=True)
class SkillsOverlapRow:
    phrase: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class SkillsNearDupRow:
    a: str
    b: str
    method: str
    score: float


@dataclass(frozen=True)
class SkillsUnknownToolsRow:
    name: str
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class SkillsStaleProposalRow:
    proposal_id: str
    kind: str
    name: str
    age_days: int


@dataclass(frozen=True)
class SkillsGardenerSection:
    """The latest Skill Gardener run's digest sections (one card a month)."""

    ran_at: str
    retirement_candidates: tuple[SkillsRetirementRow, ...]
    failure_flags: tuple[SkillsFailureRow, ...]
    trigger_overlaps: tuple[SkillsOverlapRow, ...]
    near_dups: tuple[SkillsNearDupRow, ...]
    unknown_requires_tools: tuple[SkillsUnknownToolsRow, ...]
    stale_proposals: tuple[SkillsStaleProposalRow, ...]


@dataclass(frozen=True)
class WeeklyDigest:
    """The steward weekly digest — deterministic given (engine state, week, now)."""

    week_start: date
    week_end: date
    generated_at: datetime
    proposed: int
    approved: int
    rejected: int
    applied: int
    by_lane: tuple[LaneDigest, ...]
    #: The optional Skill Gardener rot card (NE-SKILL-4), read from the latest
    #: persisted gardener run when a skills engine is supplied. Defaulted so every
    #: existing ``WeeklyDigest`` construction stays valid.
    skills: SkillsGardenerSection | None = None


@dataclass(frozen=True)
class LatencySummary:
    """The steward review-latency summary (nulls when the relevant set is empty)."""

    median_wait_s: float | None
    p90_wait_s: float | None
    decided_count: int
    pending_count: int
    oldest_pending_age_s: float | None
    generated_at: datetime


def _window_bounds(week_start: date) -> tuple[datetime, datetime]:
    """Return ``[start, end)`` UTC-aware bounds: ``[week_start 00:00, +7d)``."""
    start = datetime.combine(week_start, time.min, tzinfo=UTC)
    return start, start + timedelta(days=7)


def _read_skills_section(
    skills_engine: sa.Engine,
    *,
    visible_proposal_ids: frozenset[str] | None = None,
) -> SkillsGardenerSection | None:
    """Parse the LATEST persisted gardener run into the digest section (read-only).

    Reads ``ne_skill_gardener_runs`` (max id) via the registry's read-only
    ``latest_gardener_report`` accessor — this module NEVER writes. Absent / no
    rows ⇒ ``None`` (the console omits the card until the first monthly run)."""
    from ne.skills.registry import SqlAlchemySkillRegistry

    report = SqlAlchemySkillRegistry(skills_engine).latest_gardener_report()
    if report is None:
        return None

    def _rows(key: str) -> list[dict]:
        raw = report.get(key)
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []

    return SkillsGardenerSection(
        ran_at=str(report.get("ran_at", "")),
        retirement_candidates=tuple(
            SkillsRetirementRow(
                name=str(r.get("name", "")),
                reason=str(r.get("reason", "")),
                fired_count=int(r.get("fired_count", 0)),
            )
            for r in _rows("retirement_candidates")
        ),
        failure_flags=tuple(
            SkillsFailureRow(
                name=str(r.get("name", "")),
                known_fires=int(r.get("known_fires", 0)),
                failed=int(r.get("failed", 0)),
                ratio=float(r.get("ratio", 0.0)),
            )
            for r in _rows("failure_flags")
        ),
        trigger_overlaps=tuple(
            SkillsOverlapRow(
                phrase=str(r.get("phrase", "")),
                names=tuple(str(n) for n in (r.get("names") or [])),
            )
            for r in _rows("trigger_overlaps")
        ),
        near_dups=tuple(
            SkillsNearDupRow(
                a=str(r.get("a", "")),
                b=str(r.get("b", "")),
                method=str(r.get("method", "")),
                score=float(r.get("score", 0.0)),
            )
            for r in _rows("near_dups")
        ),
        unknown_requires_tools=tuple(
            SkillsUnknownToolsRow(
                name=str(r.get("name", "")),
                unknown=tuple(str(t) for t in (r.get("unknown") or [])),
            )
            for r in _rows("unknown_requires_tools")
        ),
        stale_proposals=tuple(
            SkillsStaleProposalRow(
                proposal_id=str(r.get("proposal_id", "")),
                kind=str(r.get("kind", "")),
                name=str(r.get("name", "")),
                age_days=int(r.get("age_days", 0)),
            )
            for r in _rows("stale_proposals")
            if visible_proposal_ids is None or str(r.get("proposal_id", "")) in visible_proposal_ids
        ),
    )


def compute_digest(
    engine: sa.Engine,
    *,
    week_start: date,
    now: datetime,
    skills_engine: sa.Engine | None = None,
    visible_proposal_ids: frozenset[str] | None = None,
) -> WeeklyDigest:
    """Compute the weekly digest over ``[week_start 00:00 UTC, +7d)`` (read-only).

    When ``skills_engine`` is supplied, the NE-SKILL-4 rot card is read from the
    latest persisted gardener run and attached as ``skills`` (``None`` before the
    first run)."""
    start, end = _window_bounds(week_start)

    proposed_by_lane: dict[str, int] = {}
    approved_by_lane: dict[str, int] = {}
    rejected_by_lane: dict[str, int] = {}
    applied_by_lane: dict[str, int] = {}
    _counters = {
        "proposed": proposed_by_lane,
        "approved": approved_by_lane,
        "rejected": rejected_by_lane,
        "applied": applied_by_lane,
    }

    proposed_stmt = (
        sa.select(ne_fix_proposals.c.lane, sa.func.count())
        .where(ne_fix_proposals.c.created_at >= start, ne_fix_proposals.c.created_at < end)
        .group_by(ne_fix_proposals.c.lane)
    )
    review_stmt = (
        sa.select(ne_fix_proposals.c.lane, ne_proposal_reviews.c.action, sa.func.count())
        .select_from(
            ne_proposal_reviews.join(
                ne_fix_proposals,
                ne_fix_proposals.c.proposal_id == ne_proposal_reviews.c.proposal_id,
            )
        )
        .where(ne_proposal_reviews.c.created_at >= start, ne_proposal_reviews.c.created_at < end)
        .group_by(ne_fix_proposals.c.lane, ne_proposal_reviews.c.action)
    )
    if visible_proposal_ids is not None:
        visible = (
            ne_fix_proposals.c.proposal_id.in_(visible_proposal_ids)
            if visible_proposal_ids
            else sa.false()
        )
        proposed_stmt = proposed_stmt.where(visible)
        review_stmt = review_stmt.where(visible)

    with engine.connect() as conn:
        for lane, count in conn.execute(proposed_stmt).all():
            proposed_by_lane[str(lane)] = int(count)
        for lane, action, count in conn.execute(review_stmt).all():
            counter = _ACTION_TO_COUNTER.get(str(action))
            if counter is not None:
                _counters[counter][str(lane)] = int(count)

    by_lane: list[LaneDigest] = []
    for lane in FixLane:
        proposed = proposed_by_lane.get(lane.value, 0)
        approved = approved_by_lane.get(lane.value, 0)
        rejected = rejected_by_lane.get(lane.value, 0)
        applied = applied_by_lane.get(lane.value, 0)
        if proposed or approved or rejected or applied:
            by_lane.append(
                LaneDigest(
                    lane=lane.value,
                    proposed=proposed,
                    approved=approved,
                    rejected=rejected,
                    applied=applied,
                )
            )

    skills = (
        _read_skills_section(
            skills_engine,
            visible_proposal_ids=visible_proposal_ids,
        )
        if skills_engine is not None
        else None
    )

    return WeeklyDigest(
        week_start=week_start,
        week_end=week_start + timedelta(days=7),
        generated_at=now,
        proposed=sum(proposed_by_lane.values()),
        approved=sum(approved_by_lane.values()),
        rejected=sum(rejected_by_lane.values()),
        applied=sum(applied_by_lane.values()),
        by_lane=tuple(by_lane),
        skills=skills,
    )


def compute_latency(
    engine: sa.Engine,
    *,
    now: datetime,
    visible_proposal_ids: frozenset[str] | None = None,
) -> LatencySummary:
    """Compute review-wait median/p90 + pending age over the whole store (read-only)."""
    decided_stmt = (
        sa.select(
            ne_fix_proposals.c.proposal_id,
            ne_fix_proposals.c.created_at,
            sa.func.min(ne_proposal_reviews.c.created_at).label("first_review"),
        )
        .select_from(
            ne_fix_proposals.join(
                ne_proposal_reviews,
                ne_fix_proposals.c.proposal_id == ne_proposal_reviews.c.proposal_id,
            )
        )
        .group_by(ne_fix_proposals.c.proposal_id, ne_fix_proposals.c.created_at)
    )
    no_reviews = sa.not_(
        sa.exists().where(ne_proposal_reviews.c.proposal_id == ne_fix_proposals.c.proposal_id)
    )
    pending_stmt = sa.select(sa.func.count(), sa.func.min(ne_fix_proposals.c.created_at)).where(
        no_reviews
    )
    if visible_proposal_ids is not None:
        visible = (
            ne_fix_proposals.c.proposal_id.in_(visible_proposal_ids)
            if visible_proposal_ids
            else sa.false()
        )
        decided_stmt = decided_stmt.where(visible)
        pending_stmt = pending_stmt.where(visible)

    with engine.connect() as conn:
        decided_rows = conn.execute(decided_stmt).all()
        pending_count, oldest_created = conn.execute(pending_stmt).one()

    waits = sorted(
        (_as_utc(row.first_review) - _as_utc(row.created_at)).total_seconds()
        for row in decided_rows
    )
    decided_count = len(waits)
    if decided_count == 0:
        median_wait_s: float | None = None
        p90_wait_s: float | None = None
    else:
        median_wait_s = float(statistics.median(waits))
        # Nearest-rank p90: the ceil(0.9 * n)-th smallest wait (1-indexed).
        p90_wait_s = float(waits[math.ceil(0.9 * decided_count) - 1])

    pending_count = int(pending_count)
    if pending_count == 0:
        oldest_pending_age_s: float | None = None
    else:
        oldest_pending_age_s = (_as_utc(now) - _as_utc(oldest_created)).total_seconds()

    return LatencySummary(
        median_wait_s=median_wait_s,
        p90_wait_s=p90_wait_s,
        decided_count=decided_count,
        pending_count=pending_count,
        oldest_pending_age_s=oldest_pending_age_s,
        generated_at=now,
    )


__all__ = [
    "LaneDigest",
    "LatencySummary",
    "SkillsFailureRow",
    "SkillsGardenerSection",
    "SkillsNearDupRow",
    "SkillsOverlapRow",
    "SkillsRetirementRow",
    "SkillsStaleProposalRow",
    "SkillsUnknownToolsRow",
    "WeeklyDigest",
    "compute_digest",
    "compute_latency",
    "folded_status_map",
    "iso_week_start",
]
