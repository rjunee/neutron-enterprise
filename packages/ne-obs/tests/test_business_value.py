# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""compute_business_value: pure-function exactness on the hours-saved FLOOR (NE-068)."""

from __future__ import annotations

from ne.obs.collect import compute_business_value
from ne.obs.kpi import ProficiencyMetrics, UtilizationMetrics


def _utilization(agent_turns: int) -> UtilizationMetrics:
    return UtilizationMetrics(
        agent_turns=agent_turns,
        tool_calls=0,
        active_users=0,
        sessions=None,
        events_total=agent_turns,
    )


def _proficiency(negative_signals: int) -> ProficiencyMetrics:
    return ProficiencyMetrics(
        eval_runs=0,
        eval_pass_rate=None,
        correction_signals_total=negative_signals,
        positive_signals=0,
        negative_signals=negative_signals,
        negative_signal_rate=None,
    )


def test_documented_formula_exact() -> None:
    metrics = compute_business_value(
        _utilization(100), _proficiency(10), minutes_saved_per_turn=2.0, floor_discount=0.5
    )

    assert metrics.assisted_turns == 100
    assert metrics.discounted_turns == 90
    assert metrics.hours_saved_floor == 1.5


def test_negative_signals_exceeding_turns_floors_at_zero() -> None:
    metrics = compute_business_value(
        _utilization(5), _proficiency(10), minutes_saved_per_turn=2.0, floor_discount=0.5
    )

    assert metrics.discounted_turns == 0
    assert metrics.hours_saved_floor == 0.0


def test_floor_scales_linearly_with_discount() -> None:
    # 300 turns keeps every intermediate value an exact multiple of 0.01, so
    # rounding cannot introduce non-linearity across this assertion.
    half = compute_business_value(
        _utilization(300), _proficiency(0), minutes_saved_per_turn=2.0, floor_discount=0.5
    )
    full = compute_business_value(
        _utilization(300), _proficiency(0), minutes_saved_per_turn=2.0, floor_discount=1.0
    )

    assert half.hours_saved_floor == 5.0
    assert full.hours_saved_floor == half.hours_saved_floor * 2


def test_floor_scales_linearly_with_minutes() -> None:
    two_min = compute_business_value(
        _utilization(300), _proficiency(0), minutes_saved_per_turn=2.0, floor_discount=0.5
    )
    four_min = compute_business_value(
        _utilization(300), _proficiency(0), minutes_saved_per_turn=4.0, floor_discount=0.5
    )

    assert two_min.hours_saved_floor == 5.0
    assert four_min.hours_saved_floor == two_min.hours_saved_floor * 2


def test_result_rounded_to_two_decimals() -> None:
    metrics = compute_business_value(
        _utilization(7), _proficiency(0), minutes_saved_per_turn=2.0, floor_discount=0.5
    )

    # 7 * 2.0 * 0.5 / 60 = 0.11666... -> rounded to 0.12
    assert metrics.hours_saved_floor == 0.12
