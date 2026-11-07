# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Adaptive throttling for connector crawls (NE-044/045) — stdlib + asyncio only.

:class:`AdaptiveThrottle` paces outbound requests with a token bucket and
computes retry delays that honor a server ``Retry-After`` (capped) or fall back
to a deterministic capped exponential backoff. Both the clock and the sleep are
injectable so the test suite adds ZERO wall-clock sleeps and pacing is exact.

The syncs driving this throttle are durable (persisted cursor + dedup ledger
committed post-run), so a throttled-out or slept-through run never loses
progress — it resumes from the stored cursor and the ledger skips
already-written items.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

__all__ = ["AdaptiveThrottle"]


async def _default_sleeper(seconds: float) -> None:
    """Resolve ``asyncio.sleep`` at CALL time so tests may monkeypatch it."""
    await asyncio.sleep(seconds)


class AdaptiveThrottle:
    """A token-bucket rate limiter + retry-delay calculator with injected time."""

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = _default_sleeper,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._sleeper = sleeper
        self._tokens = float(burst)
        self._last = clock()

    async def acquire(self) -> None:
        """Consume one token, sleeping (via the injected sleeper) if the bucket is dry."""
        self._refill()
        if self._tokens < 1.0:
            deficit = 1.0 - self._tokens
            await self._sleeper(deficit / self._rate)
            self._refill()
        self._tokens -= 1.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last = now

    async def wait_retry(self, attempt: int, retry_after_header: str | None, cap: int) -> None:
        """Sleep (via the injected sleeper) for :meth:`retry_delay`."""
        await self._sleeper(self.retry_delay(attempt, retry_after_header, cap))

    def retry_delay(self, attempt: int, retry_after_header: str | None, cap: int) -> float:
        """Delay before retry ``attempt`` (1-based).

        A well-formed integer ``Retry-After`` wins (capped at ``cap``); a malformed
        header degrades to ``1`` (mirroring ``entra.py._retry_after``); absent, a
        deterministic capped exponential backoff ``min(cap, 2**(attempt-1))``.
        """
        if retry_after_header is not None:
            try:
                secs = int(retry_after_header)
            except ValueError:
                secs = 1
            return float(max(0, min(secs, cap)))
        return float(min(cap, 2 ** (attempt - 1)))
