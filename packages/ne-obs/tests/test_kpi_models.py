# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ReportPeriod boundary math + frozen-model invariants (NE-068)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ne.obs.kpi import (
    BusinessValueMetrics,
    KpiLead,
    KpiValue,
    MonthlyUpbReport,
    ProficiencyMetrics,
    ReportPeriod,
    UtilizationMetrics,
    WindowMetrics,
)


def test_mid_year_month_boundaries() -> None:
    period = ReportPeriod(year=2026, month=6)
    assert period.start == datetime(2026, 6, 1, tzinfo=UTC)
    assert period.end_exclusive == datetime(2026, 7, 1, tzinfo=UTC)
    assert period.end_inclusive_micro == datetime(2026, 7, 1, tzinfo=UTC) - timedelta(
        microseconds=1
    )
    assert period.label == "2026-06"


def test_december_to_january_rollover() -> None:
    period = ReportPeriod(year=2026, month=12)
    assert period.start == datetime(2026, 12, 1, tzinfo=UTC)
    assert period.end_exclusive == datetime(2027, 1, 1, tzinfo=UTC)
    assert period.end_inclusive_micro == datetime(2027, 1, 1, tzinfo=UTC) - timedelta(
        microseconds=1
    )
    assert period.label == "2026-12"


def test_previous_within_year() -> None:
    period = ReportPeriod(year=2026, month=6)
    prev = period.previous()
    assert prev == ReportPeriod(year=2026, month=5)


def test_previous_across_year_boundary() -> None:
    period = ReportPeriod(year=2026, month=1)
    prev = period.previous()
    assert prev == ReportPeriod(year=2025, month=12)


def test_label_format_zero_padded() -> None:
    assert ReportPeriod(year=2026, month=1).label == "2026-01"
    assert ReportPeriod(year=2026, month=9).label == "2026-09"


@pytest.mark.parametrize("month", [0, 13])
def test_invalid_month_rejected(month: int) -> None:
    with pytest.raises(ValidationError):
        ReportPeriod(year=2026, month=month)


def _window(period: ReportPeriod) -> WindowMetrics:
    return WindowMetrics(
        period=period,
        utilization=UtilizationMetrics(
            agent_turns=1, tool_calls=1, active_users=1, sessions=None, events_total=1
        ),
        proficiency=ProficiencyMetrics(
            eval_runs=1,
            eval_pass_rate=1.0,
            correction_signals_total=0,
            positive_signals=0,
            negative_signals=0,
            negative_signal_rate=None,
        ),
        business_value=BusinessValueMetrics(
            assisted_turns=1,
            discounted_turns=1,
            minutes_saved_per_turn=2.0,
            floor_discount=0.5,
            hours_saved_floor=0.02,
        ),
    )


def test_report_period_is_frozen() -> None:
    period = ReportPeriod(year=2026, month=6)
    with pytest.raises(ValidationError):
        period.year = 2027  # type: ignore[misc]


def test_utilization_metrics_is_frozen() -> None:
    metrics = UtilizationMetrics(
        agent_turns=1, tool_calls=1, active_users=1, sessions=None, events_total=1
    )
    with pytest.raises(ValidationError):
        metrics.agent_turns = 2  # type: ignore[misc]


def test_monthly_upb_report_is_frozen() -> None:
    baseline = _window(ReportPeriod(year=2026, month=5))
    current = _window(ReportPeriod(year=2026, month=6))
    report = MonthlyUpbReport(
        baseline=baseline,
        current=current,
        kpis=(
            KpiValue(
                name="utilization.agent_turns",
                unit="turns",
                baseline=1.0,
                current=1.0,
                delta=0.0,
                direction="higher_is_better",
                lead=None,
            ),
        ),
        leads=(KpiLead(kpi="utilization.agent_turns", name="Ada Lovelace", email="ada@acme.com"),),
        config_hash="0" * 64,
        methodology="methodology text",
        gaps=("gap-1",),
    )
    with pytest.raises(ValidationError):
        report.config_hash = "1" * 64  # type: ignore[misc]
