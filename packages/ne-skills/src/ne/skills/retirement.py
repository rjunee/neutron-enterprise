# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The retirement lane — 90d-unfired ∧ <3-lifetime-fires, NEVER silent-delete.

Retirement is a LIFECYCLE-log fact, not a content change:

- A candidate is an ACTIVE pack that is unfired for ``days`` (90) AND has fewer
  than ``min_fires`` (3) lifetime fires. "unfired 90d" is measured against the
  MAX of (last_fired_at, the register instant, the last ``restored`` instant) so
  a just-registered or just-restored pack is never an instant candidate. CLOCK-
  INJECTED: ``now`` is always a parameter — retirement never reads a wall clock.
- ``retire_skill`` appends a ``ne_skill_lifecycle('retired')`` event (retire
  LOUDLY — the reason/date ride the ``reason`` field) and withdraws the name from
  EVERY materialized surface. The pack's content rows in ``ne_skill_versions``
  are NEVER touched — retire is reversible (``restore_skill`` is one lifecycle
  transition; surfaces repopulate on the next active-set recompute). This is the
  npm-deprecate / Homebrew-``disable!`` discipline: keep restorable, retire with
  a reason, never silent-delete.
- Failure flags (≥50% fail over ≥4 known-outcome fires) trigger REVIEW, never
  auto-retire; attribution is approximate (design §10-Q6, best-effort).

D-8: retired packs leave the active set AND the default ``read.skills.search``
STRUCTURALLY — ``verified_current_packs`` (and thus the active-set computer and
the search handler) folds out any non-active status. The steward library list
(GET /v1/steward/skills) is the explicit FULL-library surface and keeps showing
retired rows with a one-tap restore.

R-26: this module reads the materialized ``ne_skill_usage`` rollup (folded from
``ne_skill_invocations`` by :mod:`ne.skills.telemetry`), never a live aggregate
over the unbounded invocation log.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ne.skills.registry import (
    SqlAlchemySkillRegistry,
    _as_utc,
    ne_skill_usage,
)

_RESTORED_EVENT = "restored"


class RetirementCandidate(BaseModel):
    """One pack the Gardener surfaces as a retirement candidate in the digest."""

    model_config = ConfigDict(frozen=True)

    name: str
    fired_count: int
    last_fired_at: datetime | None
    registered_at: datetime | None
    reason: Literal["unfired-90d", "superseded"]


class FailureFlag(BaseModel):
    """One pack whose fired runs fail at/above the review threshold (never auto-retire)."""

    model_config = ConfigDict(frozen=True)

    name: str
    known_fires: int
    failed: int
    ratio: float


def _usage_map(engine: sa.Engine) -> dict[str, sa.Row]:  # type: ignore[type-arg]
    with engine.begin() as conn:
        rows = conn.execute(sa.select(ne_skill_usage)).all()
    return {str(r.skill_name): r for r in rows}


def retirement_candidates(
    engine: sa.Engine,
    *,
    now: datetime,
    days: int = 90,
    min_fires: int = 3,
) -> tuple[RetirementCandidate, ...]:
    """ACTIVE packs unfired for ``days`` AND with < ``min_fires`` lifetime fires.

    Clock-injected: ``now`` is always supplied. The "last activity" a pack is
    measured against is ``max(last_fired_at, registered_at, last_restored_at)`` —
    so a fresh register or a fresh restore resets the 90d window (never an instant
    candidate). Reason is always ``'unfired-90d'`` here; the ``'superseded'``
    reason is minted by the Gardener's near-dup sweep.
    """
    registry = SqlAlchemySkillRegistry(engine)
    status = registry.status_map()
    registered_at = registry.registered_at_map()
    usage = _usage_map(engine)
    threshold = now - timedelta(days=days)

    candidates: list[RetirementCandidate] = []
    for name, st in sorted(status.items()):
        if st != "active":
            continue
        row = usage.get(name)
        fired = int(row.fired_count) if row is not None else 0
        if fired >= min_fires:
            continue
        last_fired: datetime | None = None
        if row is not None and row.last_fired_at is not None:
            last_fired = _as_utc(row.last_fired_at)
        reg_at = registered_at.get(name)
        restored_at = registry.last_lifecycle_at(name, _RESTORED_EVENT)
        activity = [d for d in (last_fired, reg_at, restored_at) if d is not None]
        last_activity = max(activity) if activity else None
        # No activity instant at all ⇒ treat as stale (unlikely; a parent row
        # always carries created_at, so reg_at is normally present).
        if last_activity is not None and last_activity >= threshold:
            continue
        candidates.append(
            RetirementCandidate(
                name=name,
                fired_count=fired,
                last_fired_at=last_fired,
                registered_at=reg_at,
                reason="unfired-90d",
            )
        )
    return tuple(candidates)


def failure_flags(
    engine: sa.Engine,
    *,
    min_fires: int = 4,
    ratio: float = 0.5,
) -> tuple[FailureFlag, ...]:
    """ACTIVE packs with ≥ ``min_fires`` KNOWN-outcome fires failing at ≥ ``ratio``.

    ``known = success_count + fail_count`` (unknown-outcome fires are excluded
    from the ratio — approximate attribution must not manufacture a failure). A
    flag triggers steward REVIEW, NEVER an auto-retire (§6.2, C12).
    """
    registry = SqlAlchemySkillRegistry(engine)
    status = registry.status_map()
    usage = _usage_map(engine)

    flags: list[FailureFlag] = []
    for name, row in sorted(usage.items()):
        if status.get(name, "active") != "active":
            continue
        known = int(row.success_count) + int(row.fail_count)
        if known < min_fires:
            continue
        failed = int(row.fail_count)
        r = failed / known if known else 0.0
        if r >= ratio:
            flags.append(FailureFlag(name=name, known_fires=known, failed=failed, ratio=r))
    return tuple(flags)


def retire_skill(engine: sa.Engine, name: str, *, actor: str, reason: str) -> None:
    """Retire ``name``: append ``retired`` lifecycle + withdraw from every surface.

    Raises ``LookupError`` if ``name`` has no parent row, ``ValueError`` if its
    folded status is not ``active``. Content rows in ``ne_skill_versions`` are
    NEVER deleted — restore is one lifecycle transition away.
    """
    registry = SqlAlchemySkillRegistry(engine)
    if not registry.name_exists(name):
        raise LookupError(f"skill {name!r} has no parent row")
    if registry.current_status(name) != "active":
        raise ValueError(f"skill {name!r} is not active; cannot retire")
    # ONE transaction: the retired lifecycle event AND withdrawal from every
    # surface commit together, so a partial failure cannot leave a folded-retired
    # pack still advertised on a stale surface (Codex P2).
    registry.retire(name, actor=actor, reason=reason)


def restore_skill(engine: sa.Engine, name: str, *, actor: str, reason: str) -> None:
    """Restore a retired ``name``: append a ``restored`` lifecycle transition.

    Raises ``LookupError`` if absent, ``ValueError`` unless the folded status is
    ``retired``. Surfaces repopulate on the next active-set recompute.
    """
    registry = SqlAlchemySkillRegistry(engine)
    if not registry.name_exists(name):
        raise LookupError(f"skill {name!r} has no parent row")
    if registry.current_status(name) != "retired":
        raise ValueError(f"skill {name!r} is not retired; cannot restore")
    registry.append_lifecycle(name, _RESTORED_EVENT, actor=actor, reason=reason)
