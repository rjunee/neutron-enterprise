# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Hermes credential_pool PATTERN conformance (independent implementation, MIT).

Selection strategies, per-failure-class cooldowns (429/402/401), retry-after
override, 5-strike park, success reset, and pure availability probes.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ne.identity.agents.config import AgentCredentialConfig
from ne.identity.agents.pool import (
    CooldownReason,
    CredentialPoolState,
    PoolEntry,
    has_usable,
    park,
    report_failure,
    report_success,
    select_entry,
    soonest_cooldown_until,
)

CONFIG = AgentCredentialConfig()


def _pool(strategy: str, *ids: str) -> CredentialPoolState:
    return CredentialPoolState(
        entries=[PoolEntry(credential_id=i) for i in ids],
        strategy=strategy,  # type: ignore[arg-type]
    )


def _pick(
    pool: CredentialPoolState,
    now: float,
    *,
    eligible: Callable[[str], bool] | None = None,
) -> PoolEntry:
    entry = select_entry(pool, now=now, eligible=eligible)
    assert entry is not None
    return entry


def test_duplicate_ids_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="duplicate credential id"):
        CredentialPoolState(
            entries=[PoolEntry(credential_id="a"), PoolEntry(credential_id="a")],
            strategy="fill_first",
        )


def test_fill_first_always_picks_first_available() -> None:
    pool = _pool("fill_first", "a", "b", "c")
    for _ in range(3):
        assert _pick(pool, 100.0).credential_id == "a"


def test_round_robin_cycles_available_slice() -> None:
    pool = _pool("round_robin", "a", "b", "c")
    picks = [_pick(pool, 100.0).credential_id for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_round_robin_skips_cooling_entry() -> None:
    pool = _pool("round_robin", "a", "b", "c")
    report_failure(pool, CONFIG, "b", 429, now=100.0)  # b cools 60s
    picks = [_pick(pool, 100.0).credential_id for _ in range(4)]
    assert "b" not in picks
    assert set(picks) == {"a", "c"}


def test_least_used_picks_minimum_use_count() -> None:
    pool = _pool("least_used", "a", "b")
    pool.entries[0].use_count = 5
    pool.entries[1].use_count = 1
    assert _pick(pool, 100.0).credential_id == "b"


def test_random_returns_available_entry() -> None:
    pool = _pool("random", "a", "b", "c")
    for _ in range(20):
        assert _pick(pool, 100.0).credential_id in {"a", "b", "c"}


def test_429_cooldown_default_and_retry_after_override() -> None:
    pool = _pool("fill_first", "a")
    report_failure(pool, CONFIG, "a", 429, now=100.0)
    entry = pool.entries[0]
    assert entry.cooldown_until == pytest.approx(100.0 + 60.0)
    assert entry.cooldown_reason is CooldownReason.RATE_LIMIT_429

    report_success(pool, "a")
    report_failure(pool, CONFIG, "a", 429, now=200.0, retry_after_seconds=5.0)
    assert pool.entries[0].cooldown_until == pytest.approx(205.0)


def test_402_and_401_cooldowns() -> None:
    pool = _pool("fill_first", "a", "b")
    report_failure(pool, CONFIG, "a", 402, now=0.0)
    report_failure(pool, CONFIG, "b", 401, now=0.0)
    a, b = pool.entries[0], pool.entries[1]
    assert a.cooldown_until == pytest.approx(1800.0)
    assert a.cooldown_reason is CooldownReason.BILLING_402
    assert b.cooldown_until == pytest.approx(300.0)
    assert b.cooldown_reason is CooldownReason.AUTH_401


def test_fifth_consecutive_failure_parks_one_hour() -> None:
    pool = _pool("fill_first", "a")
    for _ in range(5):
        report_failure(pool, CONFIG, "a", 401, now=0.0)
    entry = pool.entries[0]
    assert entry.consecutive_failures == 5
    assert entry.cooldown_until == pytest.approx(3600.0)
    assert entry.cooldown_reason is CooldownReason.CONSECUTIVE_FAILURES


def test_report_success_resets_failures_and_clears_cooldown() -> None:
    pool = _pool("fill_first", "a")
    report_failure(pool, CONFIG, "a", 429, now=0.0)
    report_success(pool, "a")
    entry = pool.entries[0]
    assert entry.consecutive_failures == 0
    assert entry.cooldown_until is None
    assert entry.cooldown_reason is None


def test_select_returns_none_when_all_cooling() -> None:
    pool = _pool("fill_first", "a", "b")
    report_failure(pool, CONFIG, "a", 402, now=0.0)
    report_failure(pool, CONFIG, "b", 402, now=0.0)
    assert has_usable(pool, now=0.0) is False
    assert select_entry(pool, now=0.0) is None
    # ... but usable again once the cooldown elapses.
    assert has_usable(pool, now=1801.0) is True


def test_pure_probes_do_not_mutate() -> None:
    pool = _pool("round_robin", "a", "b")
    before = [(e.use_count, e.credential_id) for e in pool.entries]
    assert has_usable(pool, now=0.0) is True
    assert soonest_cooldown_until(pool, now=0.0) is None
    assert pool.cursor == -1
    assert [(e.use_count, e.credential_id) for e in pool.entries] == before


def test_soonest_cooldown_when_all_cooling() -> None:
    pool = _pool("fill_first", "a", "b")
    report_failure(pool, CONFIG, "a", 401, now=0.0)  # 300
    report_failure(pool, CONFIG, "b", 402, now=0.0)  # 1800
    assert soonest_cooldown_until(pool, now=0.0) == pytest.approx(300.0)


def test_manual_park_makes_entry_unusable() -> None:
    pool = _pool("fill_first", "a")
    park(pool, "a", now=0.0)
    assert pool.entries[0].cooldown_reason is CooldownReason.MANUAL
    assert has_usable(pool, now=0.0) is False
    assert has_usable(pool, now=1.0e12) is False


def test_eligible_predicate_filters_round_robin() -> None:
    def _not_b(cid: str) -> bool:
        return cid != "b"

    pool = _pool("round_robin", "a", "b", "c")
    picks = [_pick(pool, 100.0, eligible=_not_b).credential_id for _ in range(6)]
    assert picks == ["a", "c", "a", "c", "a", "c"]
    assert "b" not in picks


def test_eligible_predicate_filters_random() -> None:
    def _only_a(cid: str) -> bool:
        return cid == "a"

    pool = _pool("random", "a", "b", "c")
    picks = [_pick(pool, 100.0, eligible=_only_a).credential_id for _ in range(20)]
    assert set(picks) == {"a"}


def test_eligible_predicate_fail_closed_when_none_eligible() -> None:
    def _none(cid: str) -> bool:
        return False

    pool = _pool("round_robin", "a", "b", "c")
    assert has_usable(pool, now=0.0, eligible=_none) is False
    assert select_entry(pool, now=0.0, eligible=_none) is None
