# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The invocation-telemetry fold — ``ne_skill_invocations`` → ``ne_skill_usage`` rollup.

R-26 NORMATIVE DECISION (plan §6.5, do NOT break it): for the in-process
``PydanticAiSkillSubstrate`` binding, ``read.skills.view`` is the SOLE writer of
``ne_skill_invocations`` ("this call IS the firing", NE-SKILL-1a §2.3.2). The
frozen ``SkillSubstrate.collect_invocations`` reads that SAME table, so a
telemetry job that drained ``collect_invocations(cursor) → ne_skill_invocations``
would RE-INSERT rows the view handler already wrote — inflating
``fired_count``/``fail_count`` and corrupting the retirement rule (unfired-90d ∧
<3-fires) and the failure flag (≥50%-of-≥4). **Therefore this module NEVER
references ``collect_invocations``; the fold reads ``ne_skill_invocations``
DIRECTLY and rot/retirement query the materialized ``ne_skill_usage`` rollup.**
``collect_invocations`` stays in the frozen Protocol because the out-of-band
Claude-Code transcript binding (NE-SKILL-5) needs it — but no drain-through-
substrate job exists in v1, and a source-level test asserts this file never
names ``collect_invocations``.

The fold is CURSOR-IDEMPOTENT: the cursor watermark and the counter upserts move
in ONE ``engine.begin()`` transaction, so re-running with no new rows is a no-op
and a crash before commit re-folds the same window exactly once (never twice).

VIEW-WRITE-FAILURE SEMANTICS (plan §6.5): the ``ne_skill_invocations`` row is a
BEST-EFFORT telemetry write (``tools.build_view_handler`` serves the body even
when the INSERT fails) and the hash-chained ``tool.execute`` audit event is the
CANONICAL firing record. A dropped telemetry row can only UNDERCOUNT — which
merely DELAYS retirement (the safe direction) — never corrupts a lifecycle
decision.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.skills.registry import (
    ne_skill_cursors,
    ne_skill_invocations,
    ne_skill_usage,
)

#: The default fold consumer name (its own cursor row over ne_skill_invocations).
_ROT_CONSUMER = "skill-rot"

#: The terminal audit actions attribute_outcomes joins a firing to (mirrors the
#: substrate's snapshot fold — a session/subagent run's success/failure outcome).
_TERMINAL_ACTIONS = ("session.run", "subagent.complete")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class FoldOutcome(BaseModel):
    """The counts-only result of one :func:`fold_invocations` pass."""

    model_config = ConfigDict(frozen=True)

    rows_folded: int
    cursor: int


def _read_cursor(conn: sa.Connection, consumer: str) -> int:
    row = conn.execute(
        sa.select(ne_skill_cursors.c.last_id).where(ne_skill_cursors.c.consumer == consumer)
    ).first()
    return int(row.last_id) if row is not None else 0


def _upsert_cursor(conn: sa.Connection, consumer: str, last_id: int) -> None:
    result = conn.execute(
        ne_skill_cursors.update()
        .where(ne_skill_cursors.c.consumer == consumer)
        .values(last_id=last_id)
    )
    if result.rowcount == 0:
        conn.execute(ne_skill_cursors.insert().values(consumer=consumer, last_id=last_id))


class _Delta:
    __slots__ = ("fail", "fired", "last_fired_at", "success", "unknown")

    def __init__(self) -> None:
        self.fired = 0
        self.success = 0
        self.fail = 0
        self.unknown = 0
        self.last_fired_at: datetime | None = None


def fold_invocations(engine: sa.Engine, *, consumer: str = _ROT_CONSUMER) -> FoldOutcome:
    """Fold new ``ne_skill_invocations`` rows into the ``ne_skill_usage`` rollup.

    In ONE transaction: read rows with ``id > cursor`` ordered by id, aggregate
    per ``skill_name`` (``fired += n``; ``success``/``fail``/``unknown`` split on
    ``run_succeeded`` True/False/None; ``last_fired_at`` = max), upsert the
    per-skill counters, then upsert the cursor to the max folded id. Because the
    cursor and the counters commit atomically, a re-run with no new rows folds
    nothing (idempotent) and a crash before commit re-folds the exact window.
    """
    with engine.begin() as conn:
        cursor = _read_cursor(conn, consumer)
        rows = conn.execute(
            sa.select(ne_skill_invocations)
            .where(ne_skill_invocations.c.id > cursor)
            .order_by(ne_skill_invocations.c.id)
        ).all()
        if not rows:
            return FoldOutcome(rows_folded=0, cursor=cursor)

        deltas: dict[str, _Delta] = {}
        max_id = cursor
        for row in rows:
            max_id = max(max_id, int(row.id))
            delta = deltas.setdefault(str(row.skill_name), _Delta())
            delta.fired += 1
            if row.run_succeeded is True:
                delta.success += 1
            elif row.run_succeeded is False:
                delta.fail += 1
            else:
                delta.unknown += 1
            fired_at = _as_utc(row.fired_at)
            if delta.last_fired_at is None or fired_at > delta.last_fired_at:
                delta.last_fired_at = fired_at

        now = datetime.now(UTC)
        for name, delta in deltas.items():
            existing = conn.execute(
                sa.select(ne_skill_usage).where(ne_skill_usage.c.skill_name == name)
            ).first()
            if existing is None:
                conn.execute(
                    ne_skill_usage.insert().values(
                        skill_name=name,
                        fired_count=delta.fired,
                        success_count=delta.success,
                        fail_count=delta.fail,
                        unknown_count=delta.unknown,
                        last_fired_at=delta.last_fired_at,
                        updated_at=now,
                    )
                )
            else:
                prev_last = (
                    _as_utc(existing.last_fired_at) if existing.last_fired_at is not None else None
                )
                new_last = delta.last_fired_at
                if prev_last is not None and (new_last is None or prev_last > new_last):
                    new_last = prev_last
                conn.execute(
                    ne_skill_usage.update()
                    .where(ne_skill_usage.c.skill_name == name)
                    .values(
                        fired_count=int(existing.fired_count) + delta.fired,
                        success_count=int(existing.success_count) + delta.success,
                        fail_count=int(existing.fail_count) + delta.fail,
                        unknown_count=int(existing.unknown_count) + delta.unknown,
                        last_fired_at=new_last,
                        updated_at=now,
                    )
                )

        _upsert_cursor(conn, consumer, max_id)
        return FoldOutcome(rows_folded=len(rows), cursor=max_id)


def attribute_outcomes(
    engine: sa.Engine,
    audit_engine: sa.Engine | None,
    *,
    consumer: str = _ROT_CONSUMER,
    limit: int = 1000,
) -> int:
    """Best-effort approximate attribution of un-folded firings (design §10-Q6).

    For not-yet-folded rows (``id > cursor``) whose ``run_succeeded IS NULL`` and
    whose ``session_id`` is non-empty, look up a TERMINAL audit event
    (``session.run``/``subagent.complete``) on ``audit_engine`` whose ``trace_id``
    equals the firing's ``session_id`` and UPDATE ``run_succeeded`` from that
    terminal outcome (``'success' → True`` else ``False``). Unresolvable rows stay
    NULL (counted ``unknown`` at fold). Returns the number of rows updated.

    ``limit`` bounds the per-batch working set (memory), NOT the total rows
    attributed: attribution PAGINATES over an ascending-id watermark across the
    ENTIRE pre-cursor window (``id > cursor``), so it must reach every attributable
    row before the caller's :func:`fold_invocations` advances the cursor past them.
    Otherwise, when more than ``limit`` un-folded rows accumulate between monthly
    Gardener runs, rows beyond the first batch would be folded as ``unknown`` even
    though a matching terminal outcome exists — permanently underreporting failures
    and potentially suppressing a failure flag (Codex P1). The watermark advances
    by the max id SEEN per batch (not by rows dropping out of the NULL filter), so
    unresolvable NULLs cannot stall the walk, and every pre-cursor row is examined
    exactly once.

    This is telemetry ENRICHMENT, not audit mutation: the hash-chained audit log
    is never written here and remains the canonical firing record. ``audit_engine
    is None`` ⇒ 0 (nothing to join). Attribution is APPROXIMATE — a firing and its
    session's terminal outcome are correlated by ``session_id``/``trace_id`` only,
    so flags derived from it are review-only and never auto-retire.
    """
    if audit_engine is None:
        return 0

    from ne.audit.store import ne_audit_log

    updated = 0
    with engine.begin() as conn, audit_engine.connect() as audit_conn:
        watermark = _read_cursor(conn, consumer)
        while True:
            rows = conn.execute(
                sa.select(
                    ne_skill_invocations.c.id,
                    ne_skill_invocations.c.session_id,
                )
                .where(
                    ne_skill_invocations.c.id > watermark,
                    ne_skill_invocations.c.run_succeeded.is_(None),
                    ne_skill_invocations.c.session_id != "",
                )
                .order_by(ne_skill_invocations.c.id)
                .limit(limit)
            ).all()
            if not rows:
                break
            # Advance by the max id SEEN so unresolvable NULLs (which stay NULL and
            # keep matching the filter) can never re-enter the next batch.
            watermark = max(int(r.id) for r in rows)

            sessions = {str(r.session_id) for r in rows}
            terminals = audit_conn.execute(
                sa.select(ne_audit_log.c.trace_id, ne_audit_log.c.outcome, ne_audit_log.c.id)
                .where(
                    ne_audit_log.c.action.in_(_TERMINAL_ACTIONS),
                    ne_audit_log.c.trace_id.in_(sessions),
                )
                .order_by(ne_audit_log.c.id)
            ).all()
            # Last terminal per trace wins (the session's final recorded outcome).
            outcome_by_trace: dict[str, str] = {}
            for trace_id, outcome, _id in terminals:
                outcome_by_trace[str(trace_id)] = str(outcome)

            for row in rows:
                outcome = outcome_by_trace.get(str(row.session_id))
                if outcome is None:
                    continue
                conn.execute(
                    ne_skill_invocations.update()
                    .where(ne_skill_invocations.c.id == int(row.id))
                    .values(run_succeeded=(outcome == "success"))
                )
                updated += 1
    return updated


__all__ = ["FoldOutcome", "attribute_outcomes", "fold_invocations"]
