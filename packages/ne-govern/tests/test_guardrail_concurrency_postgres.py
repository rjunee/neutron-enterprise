# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-Postgres concurrency proof for the guardrail reservation (NE-070H, race 2).

Env-gated on ``NE_TEST_POSTGRES_URL``. THE FAIL-OPEN PRIORITY: the guardrail
check-limit-then-charge TOCTOU is the ONE race here that is fail-OPEN (over-spend /
over-rate), so it is proven first and hardest. K=16 genuinely concurrent workers —
each its OWN GuardrailEngine + RecordingSink but ONE SHARED durable ledger/counter
over live Postgres (the real multi-worker shape) — cannot overshoot a cap C:

* :func:`test_concurrent_charges_never_exceed_cap_live_pg` — cap $10, 16 workers
  charging $1: EXACTLY 10 succeed, 6 raise an AUDITED ``GuardrailViolation``, the
  ledger total is EXACTLY $10.
* :func:`test_concurrent_rate_reservations_never_exceed_max_calls_live_pg` — max 5,
  16 concurrent reservations: EXACTLY 5 admitted.
* :func:`test_check_then_record_pattern_overshoots` — red control: the pre-fix
  check-then-unconditional-record sequence deterministically OVERSHOOTS (two-phase
  barrier) under the identical harness.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from decimal import Decimal

import pytest
import sqlalchemy as sa

from ne.govern.config import RateLimitRule, RateLimitSettings, SpendCapSettings
from ne.govern.errors import GuardrailViolation
from ne.govern.guardrails import (
    GuardrailEngine,
    RateLimitGuardrail,
    SpendCapGuardrail,
    build_tool_call_payload,
)
from ne.govern.guardrails.durable import (
    SqlAlchemySlidingWindowCounter,
    SqlAlchemySpendLedger,
    ensure_guardrail_state_schema,
    metadata,
    ne_guardrail_spend,
)

from .conftest import RecordingAuditSink, make_principal

_URL = os.environ.get("NE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="live Postgres not configured (set NE_TEST_POSTGRES_URL); "
    "pending-live-verification ledger",
)

_K = 16


@pytest.fixture
def pg_engine() -> Iterator[sa.Engine]:
    assert _URL is not None
    engine = sa.create_engine(_URL, pool_size=24, max_overflow=24)
    metadata.drop_all(engine)
    ensure_guardrail_state_schema(engine)
    try:
        yield engine
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def test_concurrent_charges_never_exceed_cap_live_pg(pg_engine: sa.Engine) -> None:
    ledger = SqlAlchemySpendLedger(pg_engine)  # ONE shared durable ledger
    barrier = threading.Barrier(_K)
    lock = threading.Lock()
    outcomes: list[tuple[bool, RecordingAuditSink]] = []

    def worker() -> None:
        sink = RecordingAuditSink()  # each worker its OWN engine + sink
        engine = GuardrailEngine(
            [SpendCapGuardrail(SpendCapSettings(default_cap_usd="10"), ledger)],
            sink,
            spend_ledger=ledger,
        )
        payload = build_tool_call_payload(
            principal=make_principal("user:1"), tool="send", estimated_cost_usd="1"
        )
        barrier.wait()
        try:
            asyncio.run(engine.enforce(payload))
            ok = True
        except GuardrailViolation:
            ok = False
        with lock:
            outcomes.append((ok, sink))

    threads = [threading.Thread(target=worker) for _ in range(_K)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    passed = [o for o in outcomes if o[0]]
    denied = [o for o in outcomes if not o[0]]
    assert len(passed) == 10  # cap $10 / $1 each ⇒ exactly 10 admitted
    assert len(denied) == 6
    # Every loser carries an audited guardrail.block (C12 fail-closed at the new deny point).
    for _ok, sink in denied:
        actions = [getattr(e, "action", "") for e in sink.events]
        assert "guardrail.block" in actions

    with pg_engine.connect() as conn:
        total_nano = conn.execute(
            sa.select(ne_guardrail_spend.c.total_nanousd).where(
                ne_guardrail_spend.c.scope == "principal:user:1"
            )
        ).scalar_one()
    assert Decimal(int(total_nano)) / (Decimal(10) ** 9) == Decimal("10")  # exactly $10


def test_concurrent_rate_reservations_never_exceed_max_calls_live_pg(pg_engine: sa.Engine) -> None:
    counter = SqlAlchemySlidingWindowCounter(pg_engine)  # ONE shared durable counter
    barrier = threading.Barrier(_K)
    lock = threading.Lock()
    results: list[bool] = []

    def worker() -> None:
        sink = RecordingAuditSink()
        engine = GuardrailEngine(
            [
                RateLimitGuardrail(
                    RateLimitSettings(default=RateLimitRule(max_calls=5, window_s=600)), counter
                )
            ],
            sink,
            rate_counter=counter,
        )
        payload = build_tool_call_payload(principal=make_principal("user:1"), tool="send")
        barrier.wait()
        try:
            asyncio.run(engine.enforce(payload))
            ok = True
        except GuardrailViolation:
            ok = False
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(_K)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 5  # exactly max_calls admitted


def test_check_then_record_pattern_overshoots(pg_engine: sa.Engine) -> None:
    # RED CONTROL (negative contrast): the pre-fix check-then-unconditional-record
    # sequence — read recorded_spend, and if within cap, record — OVERSHOOTS the cap
    # under the same 16-worker harness, documenting the exact fail-open the atomic
    # charge closes. This is ONLY a negative control: the durable atomic charge()
    # invariant (spend == cap) is asserted separately by
    # test_concurrent_charges_never_exceed_cap_live_pg. With only a START barrier the
    # OS could serialise whole check+record iterations, so some runs landed at exactly
    # $10 and the strict `> cap` assertion FLAKED. A SECOND barrier between the check
    # and record phases guarantees all 16 workers complete the racy read while
    # spend == $0 BEFORE any worker records → total $16 > $10 EVERY run. The strict
    # `> cap` assertion is kept verbatim — determinism comes only from the barrier.
    ledger = SqlAlchemySpendLedger(pg_engine)
    cap = Decimal("10")
    est = Decimal("1")
    scope = "principal:user:1"
    check_barrier = threading.Barrier(_K)
    record_barrier = threading.Barrier(_K)

    def worker() -> None:
        check_barrier.wait()
        # the racy check — all 16 workers read while spend == $0
        within_cap = ledger.recorded_spend(scope) + est <= cap
        record_barrier.wait()  # every worker finishes the check phase BEFORE any record
        if within_cap:
            ledger.record(scope, est)  # ... then the unconditional charge

    threads = [threading.Thread(target=worker) for _ in range(_K)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ledger.recorded_spend(scope) > cap  # the cap WAS overshot pre-fix
