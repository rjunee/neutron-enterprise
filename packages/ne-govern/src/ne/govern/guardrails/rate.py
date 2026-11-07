# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Rate-limit guardrail (C12) — per-principal, per-tool sliding-window ceilings.

Blocks a call once a principal has made ``max_calls`` calls to a tool within the
last ``window_s`` seconds. The counter uses an injectable monotonic clock so the
window behaviour is deterministic under test. Like every guardrail, ``check`` is
a side-effect-free predicate: it READS the count but never records — the engine
records on the all-pass path.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from typing import Any

from ne.contracts.governance import GuardrailVerdict
from ne.govern.config import RateLimitSettings
from ne.govern.guardrails._common import failed, get_principal, get_tool, passed


class SlidingWindowCounter:
    """Thread-safe per-key sliding-window event counter with an injectable clock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def count(self, key: str, window_s: float) -> int:
        """Return the number of events for ``key`` within the last ``window_s``."""
        now = self._clock()
        cutoff = now - window_s
        with self._lock:
            events = self._events.get(key)
            if events is None:
                return 0
            self._prune(events, cutoff)
            if not events:
                del self._events[key]
                return 0
            return len(events)

    def record(self, key: str) -> None:
        """Record one event for ``key`` at the current clock time."""
        now = self._clock()
        with self._lock:
            self._events[key].append(now)

    def record_if_within(self, key: str, window_s: float, max_calls: int) -> bool:
        """ATOMICALLY record one event iff ``key`` is under ``max_calls`` in the window.

        The authoritative, atomic reservation the engine's all-pass path calls
        (NE-070H): prune → count → conditionally append, all under the one lock, so
        the check-count-then-record TOCTOU is closed. Returns ``True`` when the
        event was recorded (admitted), ``False`` when the window is full (denied).
        Under K concurrent reservations against ``max_calls`` M, at most M admit.
        """
        now = self._clock()
        cutoff = now - window_s
        with self._lock:
            events = self._events[key]
            self._prune(events, cutoff)
            if len(events) >= max_calls:
                if not events:
                    del self._events[key]
                return False
            events.append(now)
            return True

    @staticmethod
    def _prune(events: deque[float], cutoff: float) -> None:
        while events and events[0] <= cutoff:
            events.popleft()


class RateLimitGuardrail:
    """Blocks a call once the per-principal/per-tool sliding window is full."""

    def __init__(self, settings: RateLimitSettings, counter: SlidingWindowCounter) -> None:
        self._settings = settings
        self._counter = counter

    def rule_for(self, tool: str) -> tuple[str, int, float] | None:
        """Return ``(key_base_unused, max_calls, window_s)`` for ``tool`` or ``None``.

        Kept separate so the engine can consult the same resolution to decide
        whether (and under which key) to record on the all-pass path.
        """
        rule = self._settings.per_tool.get(tool) or self._settings.default
        if rule is None:
            return None
        return (tool, rule.max_calls, rule.window_s)

    def reservation(self, payload: Mapping[str, Any]) -> tuple[str, float, int] | None:
        """Return ``(key, window_s, max_calls)`` for the engine's atomic reservation.

        ``None`` when no rate rule applies to the tool (nothing to reserve) or the
        payload lacks a principal/tool. The engine passes this straight to
        :meth:`SlidingWindowCounter.record_if_within`, whose conditional record is
        the atomic authoritative reservation (NE-070H): the limit is enforced by
        the record itself, not a separate check, so concurrent callers can't both
        pass and overshoot.
        """
        principal = get_principal(payload)
        tool = get_tool(payload)
        if principal is None or tool is None:
            return None
        resolved = self.rule_for(tool)
        if resolved is None:
            return None
        _, max_calls, window_s = resolved
        return (f"{principal.id}:{tool}", window_s, max_calls)

    def check(self, payload: Mapping[str, Any]) -> GuardrailVerdict:
        principal = get_principal(payload)
        if principal is None:
            return failed("rate.missing-principal", "no principal in payload")
        tool = get_tool(payload)
        if tool is None:
            return failed("rate.missing-tool", "no tool in payload")

        resolved = self.rule_for(tool)
        if resolved is None:
            return passed("rate.no-rule")
        _, max_calls, window_s = resolved
        key = f"{principal.id}:{tool}"
        if self._counter.count(key, window_s) >= max_calls:
            return failed("rate.limit-exceeded", "per-principal/tool rate limit reached")
        return passed("rate.within-limit")
