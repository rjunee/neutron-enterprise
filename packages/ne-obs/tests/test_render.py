# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""render_markdown/render_json content + round-trip checks (NE-068)."""

from __future__ import annotations

from ne.obs.kpi import (
    BusinessValueMetrics,
    KpiLead,
    MonthlyUpbReport,
    ProficiencyMetrics,
    ReportPeriod,
    UtilizationMetrics,
    WindowMetrics,
)
from ne.obs.report import METHODOLOGY, generate_monthly_report, render_json, render_markdown


def _window(period: ReportPeriod, *, sessions: int | None = None) -> WindowMetrics:
    return WindowMetrics(
        period=period,
        utilization=UtilizationMetrics(
            agent_turns=10, tool_calls=2, active_users=3, sessions=sessions, events_total=15
        ),
        proficiency=ProficiencyMetrics(
            eval_runs=0,
            eval_pass_rate=None,
            correction_signals_total=0,
            positive_signals=0,
            negative_signals=0,
            negative_signal_rate=None,
        ),
        business_value=BusinessValueMetrics(
            assisted_turns=10,
            discounted_turns=10,
            minutes_saved_per_turn=2.0,
            floor_discount=0.5,
            hours_saved_floor=0.17,
        ),
    )


def test_render_markdown_contains_expected_sections(frozen_config: str) -> None:
    baseline = _window(ReportPeriod(year=2026, month=5))
    current = _window(ReportPeriod(year=2026, month=6))
    leads = (
        KpiLead(kpi="business_value.hours_saved_floor", name="Ada Lovelace", email="ada@acme.com"),
    )

    report = generate_monthly_report(baseline=baseline, current=current, leads=leads)
    markdown = render_markdown(report)

    assert "# U-P-B Monthly Report — 2026-06 (baseline 2026-05)" in markdown

    # One table row per canonical KPI, in registry order, hours-saved LAST (C8).
    kpi_names = [
        "utilization.agent_turns",
        "utilization.active_users",
        "utilization.tool_calls",
        "proficiency.eval_pass_rate",
        "proficiency.negative_signal_rate",
        "business_value.hours_saved_floor",
    ]
    kpi_table = markdown.split("## KPIs: baseline")[1].split("## Utilization")[0]
    positions = [kpi_table.index(f"| {name} |") for name in kpi_names]
    assert positions == sorted(positions)
    assert kpi_names[-1] == "business_value.hours_saved_floor"

    # "n/a" for the None baseline/eval-free-window rates.
    assert "n/a" in markdown

    # Sign-prefixed deltas: eval_pass_rate/negative_signal_rate are both None here
    # (no eval runs seeded), so assert a KPI with a real numeric delta instead.
    assert "+0.0" in markdown or "+0" in markdown

    assert METHODOLOGY in markdown
    assert "## Data gaps" in markdown
    assert "sessions" in markdown
    assert "ne-eval list_runs has no time-window filter" in markdown
    assert f"config_hash: {report.config_hash}" in markdown


def test_render_markdown_deltas_are_sign_prefixed(frozen_config: str) -> None:
    baseline = _window(ReportPeriod(year=2026, month=5))
    current = WindowMetrics(
        period=ReportPeriod(year=2026, month=6),
        utilization=UtilizationMetrics(
            agent_turns=20, tool_calls=4, active_users=6, sessions=None, events_total=30
        ),
        proficiency=baseline.proficiency,
        business_value=BusinessValueMetrics(
            assisted_turns=20,
            discounted_turns=20,
            minutes_saved_per_turn=2.0,
            floor_discount=0.5,
            hours_saved_floor=0.33,
        ),
    )

    report = generate_monthly_report(baseline=baseline, current=current)
    markdown = render_markdown(report)

    # agent_turns: 10 -> 20, delta +10.0
    expected_row = "| utilization.agent_turns | turns | 10.0 | 20.0 | +10.0 | higher_is_better |"
    assert expected_row in markdown


def test_render_json_round_trips(frozen_config: str) -> None:
    baseline = _window(ReportPeriod(year=2026, month=5))
    current = _window(ReportPeriod(year=2026, month=6))
    report = generate_monthly_report(baseline=baseline, current=current)

    payload = render_json(report)

    assert MonthlyUpbReport.model_validate_json(payload) == report
