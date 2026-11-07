# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Durable/atomic guardrail state (NE-070H) — SQLite parity for races 2 and 4.

The genuinely-concurrent multi-worker proofs run on LIVE Postgres
(``test_guardrail_concurrency_postgres.py``); this module pins the atomicity,
all-or-nothing rollback, nano-USD exactness, window semantics, and restart
durability deterministically on SQLite file engines.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from ne.govern.guardrails.durable import (
    SqlAlchemySlidingWindowCounter,
    SqlAlchemySpendLedger,
    ensure_guardrail_state_schema,
)
from ne.govern.guardrails.rate import SlidingWindowCounter
from ne.govern.guardrails.spend import InMemorySpendLedger

_NANO = Decimal(10) ** 9


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'guardrail_state.db'}")
    ensure_guardrail_state_schema(eng)
    ensure_guardrail_state_schema(eng)  # idempotent
    try:
        yield eng
    finally:
        eng.dispose()


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --- Race 2: atomic conditional spend charge -------------------------------


def test_charge_within_and_over_cap(engine: sa.Engine) -> None:
    ledger = SqlAlchemySpendLedger(engine)
    assert ledger.charge([("principal:p", Decimal("5"), Decimal("10"))]) is True
    assert ledger.recorded_spend("principal:p") == Decimal("5")
    # A charge that would breach the cap loses and leaves the total unchanged.
    assert ledger.charge([("principal:p", Decimal("6"), Decimal("10"))]) is False
    assert ledger.recorded_spend("principal:p") == Decimal("5")
    # Exactly-to-the-cap is admitted (<= cap).
    assert ledger.charge([("principal:p", Decimal("5"), Decimal("10"))]) is True
    assert ledger.recorded_spend("principal:p") == Decimal("10")


def test_multi_scope_charge_is_all_or_nothing(engine: sa.Engine) -> None:
    ledger = SqlAlchemySpendLedger(engine)
    # Principal scope fits (5<=100) but department scope busts (5>3): NOTHING charged.
    ok = ledger.charge(
        [
            ("principal:p", Decimal("5"), Decimal("100")),
            ("department:d", Decimal("5"), Decimal("3")),
        ]
    )
    assert ok is False
    assert ledger.recorded_spend("principal:p") == Decimal("0")
    assert ledger.recorded_spend("department:d") == Decimal("0")


def test_nanousd_rounding_is_pessimistic(engine: sa.Engine) -> None:
    ledger = SqlAlchemySpendLedger(engine)
    # A sub-nano charge rounds UP to 1 nano-USD (never lost to float underflow).
    assert ledger.charge([("s", Decimal("0.0000000001"), Decimal("1"))]) is True
    assert ledger.recorded_spend("s") == Decimal(1) / _NANO
    # A cap below 1 nano floors DOWN to 0, so a 1-nano charge is denied.
    assert ledger.charge([("t", Decimal("0.0000000001"), Decimal("0.0000000009"))]) is False
    assert ledger.recorded_spend("t") == Decimal("0")


# --- Race 4: durable sliding-window rate counter ---------------------------


def test_record_if_within_boundary_admits_exactly_max_calls(engine: sa.Engine) -> None:
    clock = _Clock()
    counter = SqlAlchemySlidingWindowCounter(engine, clock=clock)
    admitted = [counter.record_if_within("k", 60.0, 3) for _ in range(5)]
    assert admitted == [True, True, True, False, False]
    assert counter.count("k", 60.0) == 3


def test_record_if_within_prunes_expired_events(engine: sa.Engine) -> None:
    clock = _Clock()
    counter = SqlAlchemySlidingWindowCounter(engine, clock=clock)
    assert counter.record_if_within("k", 60.0, 1) is True
    assert counter.record_if_within("k", 60.0, 1) is False  # window full
    clock.t += 120.0  # advance past the window: the old event prunes away
    assert counter.record_if_within("k", 60.0, 1) is True
    assert counter.count("k", 60.0) == 1


def test_rate_counter_survives_restart(tmp_path: Path) -> None:
    # RACE-4 durability proof: the durable counter persists events across an engine
    # dispose/recreate (worker-restart shape) and KEEPS denying.
    db = tmp_path / "rate.db"
    clock = _Clock()
    eng_a = sa.create_engine(f"sqlite:///{db}")
    ensure_guardrail_state_schema(eng_a)
    counter_a = SqlAlchemySlidingWindowCounter(eng_a, clock=clock)
    assert counter_a.record_if_within("k", 600.0, 2) is True
    assert counter_a.record_if_within("k", 600.0, 2) is True
    eng_a.dispose()  # "worker restart"

    eng_b = sa.create_engine(f"sqlite:///{db}")
    ensure_guardrail_state_schema(eng_b)
    counter_b = SqlAlchemySlidingWindowCounter(eng_b, clock=clock)
    assert counter_b.count("k", 600.0) == 2  # events survived
    assert counter_b.record_if_within("k", 600.0, 2) is False  # still at the cap
    eng_b.dispose()

    # RED CONTROL: the in-memory counter FORGETS on restart (a fresh instance is empty).
    forgetful = SlidingWindowCounter(clock=clock)
    assert forgetful.count("k", 600.0) == 0


def test_in_memory_charge_atomic_under_threads() -> None:
    # The base-class atomic charge holds under thread contention: 16 threads each
    # charging $1 against a $10 cap admit EXACTLY 10 (the loser finds total+1 > cap).
    ledger = InMemorySpendLedger()
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        ok = ledger.charge([("principal:p", Decimal("1"), Decimal("10"))])
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 10
    assert ledger.recorded_spend("principal:p") == Decimal("10")
