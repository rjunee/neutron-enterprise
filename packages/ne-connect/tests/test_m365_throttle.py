# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""AdaptiveThrottle — token-bucket pacing + retry-delay, fully injected clock (NE-044).

No real sleeping: the clock is a mutable list and the sleeper advances it, so
pacing is asserted deterministically.
"""

from __future__ import annotations

import asyncio

from ne.connect.m365.throttle import AdaptiveThrottle


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_burst_acquires_do_not_sleep() -> None:
    ft = _FakeTime()
    throttle = AdaptiveThrottle(rate=4.0, burst=3, clock=ft.clock, sleeper=ft.sleep)

    async def scenario() -> None:
        for _ in range(3):
            await throttle.acquire()

    asyncio.run(scenario())
    assert ft.slept == []  # the full burst is free


def test_acquire_beyond_burst_sleeps_one_over_rate() -> None:
    ft = _FakeTime()
    throttle = AdaptiveThrottle(rate=2.0, burst=1, clock=ft.clock, sleeper=ft.sleep)

    async def scenario() -> None:
        await throttle.acquire()  # free (starts with 1 token)
        await throttle.acquire()  # bucket empty -> sleep 1/rate = 0.5

    asyncio.run(scenario())
    assert ft.slept == [0.5]


def test_retry_delay_honors_integer_retry_after_capped() -> None:
    throttle = AdaptiveThrottle(rate=1.0, burst=1)
    assert throttle.retry_delay(1, "10", cap=60) == 10.0
    assert throttle.retry_delay(1, "999", cap=60) == 60.0  # capped


def test_retry_delay_malformed_header_defaults_to_one() -> None:
    throttle = AdaptiveThrottle(rate=1.0, burst=1)
    assert throttle.retry_delay(3, "Wed, 21 Oct 2026 07:28:00 GMT", cap=60) == 1.0


def test_retry_delay_exponential_backoff_when_no_header() -> None:
    throttle = AdaptiveThrottle(rate=1.0, burst=1)
    assert [throttle.retry_delay(a, None, cap=60) for a in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert throttle.retry_delay(10, None, cap=60) == 60.0  # capped


def test_wait_retry_sleeps_via_injected_sleeper() -> None:
    ft = _FakeTime()
    throttle = AdaptiveThrottle(rate=1.0, burst=1, clock=ft.clock, sleeper=ft.sleep)

    async def scenario() -> None:
        await throttle.wait_retry(2, None, cap=60)  # exponential -> 2

    asyncio.run(scenario())
    assert ft.slept == [2.0]
