# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""RateLimitGuardrail (C12): deterministic sliding window with an injectable clock."""

from __future__ import annotations

import threading

import pytest

from ne.govern.config import RateLimitRule, RateLimitSettings
from ne.govern.errors import GuardrailConfigError
from ne.govern.guardrails import RateLimitGuardrail, SlidingWindowCounter, build_tool_call_payload

from .conftest import make_principal


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _payload(tool: str = "search"):
    return build_tool_call_payload(principal=make_principal("user:1"), tool=tool)


def test_under_limit_passes_and_at_limit_denies() -> None:
    clock = FakeClock()
    counter = SlidingWindowCounter(clock=clock)
    g = RateLimitGuardrail(
        RateLimitSettings(default=RateLimitRule(max_calls=2, window_s=10)), counter
    )
    key = "user:1:search"

    assert g.check(_payload()).passed
    counter.record(key)
    assert g.check(_payload()).passed
    counter.record(key)
    verdict = g.check(_payload())
    assert not verdict.passed
    assert verdict.rule == "rate.limit-exceeded"


def test_window_expiry_reallows_deterministically() -> None:
    clock = FakeClock()
    counter = SlidingWindowCounter(clock=clock)
    g = RateLimitGuardrail(
        RateLimitSettings(default=RateLimitRule(max_calls=1, window_s=10)), counter
    )
    key = "user:1:search"

    counter.record(key)
    assert not g.check(_payload()).passed
    clock.t = 11.0  # slide past the window
    assert g.check(_payload()).passed


def test_per_tool_rule_beats_default() -> None:
    counter = SlidingWindowCounter(clock=FakeClock())
    settings = RateLimitSettings(
        per_tool={"search": RateLimitRule(max_calls=1, window_s=10)},
        default=RateLimitRule(max_calls=100, window_s=10),
    )
    g = RateLimitGuardrail(settings, counter)
    counter.record("user:1:search")
    assert not g.check(_payload("search")).passed
    # a different tool falls through to the generous default
    assert g.check(_payload("other")).passed


def test_no_rule_passes() -> None:
    counter = SlidingWindowCounter(clock=FakeClock())
    g = RateLimitGuardrail(RateLimitSettings(), counter)
    for _ in range(100):
        counter.record("user:1:search")
    assert g.check(_payload()).passed


def test_check_never_increments() -> None:
    counter = SlidingWindowCounter(clock=FakeClock())
    g = RateLimitGuardrail(
        RateLimitSettings(default=RateLimitRule(max_calls=1, window_s=10)), counter
    )
    for _ in range(5):
        assert g.check(_payload()).passed
    assert counter.count("user:1:search", 10) == 0


def test_concurrent_record_is_thread_safe() -> None:
    counter = SlidingWindowCounter()

    def worker() -> None:
        for _ in range(200):
            counter.record("k")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter.count("k", 1_000_000) == 8 * 200


# --- fail-closed: malformed rate rule rejected at construction (NE-032 symmetry) ---
# Mirrors test_spend_cap.py's construction-validation section: a malformed rate
# rule must never silently DISABLE rate limiting (a non-positive window prunes
# every event; count is always 0). GuardrailConfigError is NOT a ValueError, so
# pydantic propagates it raw from both direct and nested/ne.toml construction.


def test_zero_window_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        RateLimitRule(max_calls=1, window_s=0)
    assert "window_s" in str(excinfo.value)


def test_negative_window_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        RateLimitRule(max_calls=1, window_s=-5)
    assert "window_s" in str(excinfo.value)


def test_nan_window_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        RateLimitRule(max_calls=1, window_s=float("nan"))
    assert "window_s" in str(excinfo.value)


def test_infinite_window_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        RateLimitRule(max_calls=1, window_s=float("inf"))
    assert "window_s" in str(excinfo.value)


def test_negative_max_calls_rejected_at_construction() -> None:
    with pytest.raises(GuardrailConfigError) as excinfo:
        RateLimitRule(max_calls=-1, window_s=10)
    assert "max_calls" in str(excinfo.value)


def test_malformed_rate_rule_rejected_via_nested_settings() -> None:
    # The ne.toml / dict construction path (nested model_validate) must ALSO fail
    # at construction, not silently disable — proves the validator lives on the
    # rule itself, not only on direct construction.
    with pytest.raises(GuardrailConfigError) as excinfo:
        RateLimitSettings.model_validate({"per_tool": {"search": {"max_calls": 1, "window_s": 0}}})
    assert "window_s" in str(excinfo.value)


def test_zero_max_calls_blocks_every_call() -> None:
    # max_calls == 0 is a LEGAL fail-closed kill switch: it blocks the FIRST call.
    counter = SlidingWindowCounter(clock=FakeClock())
    g = RateLimitGuardrail(
        RateLimitSettings(default=RateLimitRule(max_calls=0, window_s=10)), counter
    )
    verdict = g.check(_payload())
    assert not verdict.passed
    assert verdict.rule == "rate.limit-exceeded"


def test_well_formed_rate_rule_still_works() -> None:
    # Regression guard: the validator must not reject legitimate rules.
    counter = SlidingWindowCounter(clock=FakeClock())
    g = RateLimitGuardrail(
        RateLimitSettings(default=RateLimitRule(max_calls=2, window_s=10)), counter
    )
    assert g.check(_payload()).passed
