# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Credential rotation/cooldown pool.

Rotation/cooldown PATTERN lifted from NousResearch/hermes-agent ``credential_pool``
(MIT); independent Python implementation — no hermes-agent code is vendored or
imported. See ``packages/ne-identity/NOTICE``. The lifted semantics are:

* four selection strategies — ``fill_first`` / ``round_robin`` / ``random`` /
  ``least_used`` — over the not-cooling-down slice;
* per-failure-class cooldowns — 429 (rate limit, honoring a retry-after
  override), 402 (billing), 401 (auth);
* consecutive-failure parking — after ``max_consecutive_failures`` strikes a
  credential is parked for ``consecutive_cooldown_seconds``;
* success resets the failure counter and clears any cooldown.

An optional ``eligible`` predicate (added ON TOP of the lifted semantics)
restricts the candidate set by credential id: :func:`has_usable` and
:func:`select_entry` only ever consider entries the predicate admits. The service
passes an ACTIVE-only predicate so a NEW selection can never return a rotated-out
(GRACE) credential; the strategy/cooldown/park logic itself is unchanged.

The wall clock is INJECTED: every time-sensitive function takes ``now`` as an
epoch-seconds ``float`` (no ambient ``time.time()``), so the whole pool is
deterministically testable. ``select_entry`` returns ``None`` when every entry is
cooling down — callers MUST treat that as a hard failure (no credential to
dispatch with) rather than spinning.
"""

from __future__ import annotations

import secrets as _secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ne.identity.agents.config import AgentCredentialConfig, Strategy


class CooldownReason(StrEnum):
    """Why a pool entry is cooling down (attribution for pool churn)."""

    RATE_LIMIT_429 = "rate_limit_429"
    BILLING_402 = "billing_402"
    AUTH_401 = "auth_401"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    MANUAL = "manual"


@dataclass
class PoolEntry:
    """Mutable per-credential pool bookkeeping (no secret material)."""

    credential_id: str
    use_count: int = 0
    last_used_at: float | None = None
    cooldown_until: float | None = None
    cooldown_reason: CooldownReason | None = None
    consecutive_failures: int = 0

    def available(self, now: float) -> bool:
        """True iff this entry is not currently cooling down."""
        return self.cooldown_until is None or self.cooldown_until <= now


@dataclass
class CredentialPoolState:
    """A pool of credential entries for one agent + its selection strategy.

    Rejects duplicate credential ids at construction: the failure/success
    reporters look entries up by id, so a duplicate would silently corrupt the
    cooldown clock + failure counter.
    """

    entries: list[PoolEntry]
    strategy: Strategy
    cursor: int = -1

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for entry in self.entries:
            if entry.credential_id in seen:
                raise ValueError(f"duplicate credential id {entry.credential_id!r} in pool")
            seen.add(entry.credential_id)

    def find(self, credential_id: str) -> PoolEntry | None:
        for entry in self.entries:
            if entry.credential_id == credential_id:
                return entry
        return None


def has_usable(
    pool: CredentialPoolState,
    now: float,
    *,
    eligible: Callable[[str], bool] | None = None,
) -> bool:
    """Pure read: True iff at least one entry is selectable right now.

    Mirrors the ``available`` predicate ``select_entry`` uses but does NOT mutate
    the pool — no ``use_count`` bump, no ``last_used_at`` stamp, no cursor
    advance. ``eligible`` optionally restricts the candidate set by credential id;
    the service passes an ACTIVE-only predicate so a NEW selection can never
    return a GRACE credential. Defaults to ``None`` (every entry eligible).
    """
    return any(
        entry.available(now) and (eligible is None or eligible(entry.credential_id))
        for entry in pool.entries
    )


def soonest_cooldown_until(pool: CredentialPoolState, now: float) -> float | None:
    """Pure read: soonest epoch-seconds at which some entry leaves cooldown.

    Returns ``None`` when at least one entry is already available (nothing to
    wait for) or the pool is empty; otherwise the minimum ``cooldown_until``.
    """
    soonest: float | None = None
    for entry in pool.entries:
        if entry.available(now):
            return None
        assert entry.cooldown_until is not None  # not available => cooling down
        if soonest is None or entry.cooldown_until < soonest:
            soonest = entry.cooldown_until
    return soonest


def select_entry(
    pool: CredentialPoolState,
    now: float,
    *,
    eligible: Callable[[str], bool] | None = None,
) -> PoolEntry | None:
    """Select the next entry per the pool strategy, skipping cooling-down entries.

    Mutates the pool: bumps ``use_count``, stamps ``last_used_at``, advances the
    round-robin cursor. Returns ``None`` when every entry is cooling down.
    ``eligible`` optionally restricts the candidate set by credential id; the
    service passes an ACTIVE-only predicate so a NEW selection can never return a
    GRACE credential. Defaults to ``None`` (every entry eligible).
    """
    available = [
        entry
        for entry in pool.entries
        if entry.available(now) and (eligible is None or eligible(entry.credential_id))
    ]
    if not available:
        return None

    pick: PoolEntry
    if pool.strategy == "fill_first":
        pick = available[0]
    elif pool.strategy == "round_robin":
        # Advance the cursor inside the AVAILABLE slice, keyed by credential id so
        # ordering stays stable (an entry entering cooldown does not stall it).
        ids = [entry.credential_id for entry in available]
        last_id = (
            pool.entries[pool.cursor].credential_id
            if 0 <= pool.cursor < len(pool.entries)
            else None
        )
        start_idx = ids.index(last_id) if last_id in ids else -1
        pick = available[(start_idx + 1) % len(ids)]
        pool.cursor = pool.entries.index(pick)
    elif pool.strategy == "random":
        pick = available[_secrets.randbelow(len(available))]
    else:  # least_used
        pick = min(available, key=lambda entry: entry.use_count)

    pick.use_count += 1
    pick.last_used_at = now
    return pick


def report_failure(
    pool: CredentialPoolState,
    config: AgentCredentialConfig,
    credential_id: str,
    status: int,
    now: float,
    retry_after_seconds: float | None = None,
) -> None:
    """Record a non-2xx/connection failure: set the cooldown clock + strike count.

    ``retry_after_seconds`` (from an upstream ``Retry-After``) overrides the
    default 429 cooldown. After ``max_consecutive_failures`` strikes the entry is
    parked for ``consecutive_cooldown_seconds``.
    """
    entry = pool.find(credential_id)
    if entry is None:
        return
    entry.consecutive_failures += 1
    if status == 429:
        if retry_after_seconds is not None:
            cooldown = retry_after_seconds
        else:
            cooldown = config.cooldown_429_seconds
        entry.cooldown_until = now + cooldown
        entry.cooldown_reason = CooldownReason.RATE_LIMIT_429
    elif status == 402:
        entry.cooldown_until = now + config.cooldown_402_seconds
        entry.cooldown_reason = CooldownReason.BILLING_402
    elif status == 401:
        entry.cooldown_until = now + config.cooldown_401_seconds
        entry.cooldown_reason = CooldownReason.AUTH_401
    if entry.consecutive_failures >= config.max_consecutive_failures:
        entry.cooldown_until = now + config.consecutive_cooldown_seconds
        entry.cooldown_reason = CooldownReason.CONSECUTIVE_FAILURES


def report_success(pool: CredentialPoolState, credential_id: str) -> None:
    """Record a successful dispatch: reset the strike counter, clear any cooldown."""
    entry = pool.find(credential_id)
    if entry is None:
        return
    entry.consecutive_failures = 0
    entry.cooldown_until = None
    entry.cooldown_reason = None


def park(pool: CredentialPoolState, credential_id: str, now: float) -> None:
    """Manually park an entry indefinitely (used on revocation).

    Sets a cooldown far into the future with the ``MANUAL`` reason so the entry
    is never selected again while it remains in the pool.
    """
    entry = pool.find(credential_id)
    if entry is None:
        return
    # A very large offset keeps the entry unavailable for any realistic clock.
    entry.cooldown_until = now + 1.0e18
    entry.cooldown_reason = CooldownReason.MANUAL
