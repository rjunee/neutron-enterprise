# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Fixture-first end-to-end ratchet for generate_monthly_report (NE-068).

Seeds a baseline month (2026-05) and a current month (2026-06) across all
three REAL sqlite stores (usage audit events, eval runs, correction signals),
collects both windows through the real collectors, and asserts the generated
report's per-KPI baseline->current values/deltas against hand-computed
expectations for all six canonical KPIs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from ne.contracts import ConfigHashNotFrozenError, reset_config_hash_for_testing
from ne.eval.store import EvalRunStore
from ne.flywheel import SqlAlchemyCorrectionSignalStore
from ne.obs.collect import collect_window
from ne.obs.kpi import KpiLead, ReportPeriod
from ne.obs.report import generate_monthly_report, render_markdown
from ne.schema import CorrectionKind

from .conftest import seed_audit_event, seed_eval_run, seed_signal

MAY = ReportPeriod(year=2026, month=5)
JUNE = ReportPeriod(year=2026, month=6)

MINUTES_SAVED_PER_TURN = 2.0
FLOOR_DISCOUNT = 0.5

LEADS = (
    KpiLead(kpi="business_value.hours_saved_floor", name="Ada Lovelace", email="ada@acme.com"),
)


def _seed_fixtures(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
) -> None:
    # --- Baseline month (May 2026): 30 turns / 2 users / 1 tool call / 6 negative signals.
    for i in range(30):
        actor = "user:ada@acme.com" if i < 15 else "user:grace@acme.com"
        seed_audit_event(
            audit_engine,
            action="ne.gateway.complete",
            actor=actor,
            occurred_at=datetime(2026, 5, 1 + (i % 28), tzinfo=UTC),
        )
    seed_audit_event(
        audit_engine,
        action="tool.execute",
        actor="agent://gateway",
        occurred_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    for i in range(6):
        seed_signal(
            signal_store,
            kind=CorrectionKind.thumbs_down,
            occurred_at=datetime(2026, 5, 1 + i, tzinfo=UTC),
        )
    seed_eval_run(
        eval_store, run_id="may-1", created_at=datetime(2026, 5, 10, tzinfo=UTC), passed=True
    )
    seed_eval_run(
        eval_store, run_id="may-2", created_at=datetime(2026, 5, 20, tzinfo=UTC), passed=False
    )

    # --- Current month (June 2026): 60 turns / 3 users / 2 tool calls / 6 negative signals.
    for i in range(60):
        if i < 20:
            actor = "user:ada@acme.com"
        elif i < 40:
            actor = "user:grace@acme.com"
        else:
            actor = "user:carol@acme.com"
        seed_audit_event(
            audit_engine,
            action="ne.gateway.complete",
            actor=actor,
            occurred_at=datetime(2026, 6, 1 + (i % 28), tzinfo=UTC),
        )
    for i in range(2):
        seed_audit_event(
            audit_engine,
            action="tool.execute",
            actor="agent://gateway",
            occurred_at=datetime(2026, 6, 15 + i, tzinfo=UTC),
        )
    for i in range(6):
        seed_signal(
            signal_store,
            kind=CorrectionKind.thumbs_down,
            occurred_at=datetime(2026, 6, 1 + i, tzinfo=UTC),
        )
    seed_eval_run(
        eval_store, run_id="jun-1", created_at=datetime(2026, 6, 10, tzinfo=UTC), passed=True
    )
    seed_eval_run(
        eval_store, run_id="jun-2", created_at=datetime(2026, 6, 20, tzinfo=UTC), passed=True
    )


def _collect_both(
    audit_engine: sa.Engine, eval_store: EvalRunStore, signal_store: SqlAlchemyCorrectionSignalStore
):
    baseline = collect_window(
        audit_engine=audit_engine,
        eval_store=eval_store,
        signal_store=signal_store,
        period=MAY,
        minutes_saved_per_turn=MINUTES_SAVED_PER_TURN,
        floor_discount=FLOOR_DISCOUNT,
    )
    current = collect_window(
        audit_engine=audit_engine,
        eval_store=eval_store,
        signal_store=signal_store,
        period=JUNE,
        minutes_saved_per_turn=MINUTES_SAVED_PER_TURN,
        floor_discount=FLOOR_DISCOUNT,
    )
    return baseline, current


def test_all_six_canonical_kpis_match_hand_computed_expectations(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    frozen_config: str,
) -> None:
    _seed_fixtures(audit_engine, eval_store, signal_store)
    baseline, current = _collect_both(audit_engine, eval_store, signal_store)

    report = generate_monthly_report(baseline=baseline, current=current, leads=LEADS)

    by_name = {kpi.name: kpi for kpi in report.kpis}

    assert by_name["utilization.agent_turns"].baseline == 30.0
    assert by_name["utilization.agent_turns"].current == 60.0
    assert by_name["utilization.agent_turns"].delta == 30.0

    assert by_name["utilization.active_users"].baseline == 2.0
    assert by_name["utilization.active_users"].current == 3.0
    assert by_name["utilization.active_users"].delta == 1.0

    assert by_name["utilization.tool_calls"].baseline == 1.0
    assert by_name["utilization.tool_calls"].current == 2.0
    assert by_name["utilization.tool_calls"].delta == 1.0

    assert by_name["proficiency.eval_pass_rate"].baseline == 0.5
    assert by_name["proficiency.eval_pass_rate"].current == 1.0
    assert by_name["proficiency.eval_pass_rate"].delta == 0.5

    assert by_name["proficiency.negative_signal_rate"].baseline == 0.2
    assert by_name["proficiency.negative_signal_rate"].current == 0.1
    assert by_name["proficiency.negative_signal_rate"].delta == -0.1

    assert by_name["business_value.hours_saved_floor"].baseline == 0.4
    assert by_name["business_value.hours_saved_floor"].current == 0.9
    assert by_name["business_value.hours_saved_floor"].delta == 0.5

    # Canonical order, hours-saved LAST (C8).
    assert [kpi.name for kpi in report.kpis] == [
        "utilization.agent_turns",
        "utilization.active_users",
        "utilization.tool_calls",
        "proficiency.eval_pass_rate",
        "proficiency.negative_signal_rate",
        "business_value.hours_saved_floor",
    ]


def test_hours_saved_floor_matches_documented_formula_exactly(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    frozen_config: str,
) -> None:
    _seed_fixtures(audit_engine, eval_store, signal_store)
    baseline, current = _collect_both(audit_engine, eval_store, signal_store)

    # baseline: successful_turns=30, negative_signals=6 -> discounted=24
    # 24 * 2.0 * 0.5 / 60 = 0.4
    assert baseline.business_value.hours_saved_floor == round(24 * 2.0 * 0.5 / 60.0, 2)
    # current: successful_turns=60, negative_signals=6 -> discounted=54
    # 54 * 2.0 * 0.5 / 60 = 0.9
    assert current.business_value.hours_saved_floor == round(54 * 2.0 * 0.5 / 60.0, 2)


def test_named_kpi_leads_attached_and_listed(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    frozen_config: str,
) -> None:
    _seed_fixtures(audit_engine, eval_store, signal_store)
    baseline, current = _collect_both(audit_engine, eval_store, signal_store)

    report = generate_monthly_report(baseline=baseline, current=current, leads=LEADS)

    assert report.leads == LEADS
    hours_kpi = next(k for k in report.kpis if k.name == "business_value.hours_saved_floor")
    assert hours_kpi.lead == LEADS[0]
    # Every OTHER kpi has no lead configured.
    for kpi in report.kpis:
        if kpi.name != "business_value.hours_saved_floor":
            assert kpi.lead is None


def test_config_hash_equals_frozen_digest(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    frozen_config: str,
) -> None:
    _seed_fixtures(audit_engine, eval_store, signal_store)
    baseline, current = _collect_both(audit_engine, eval_store, signal_store)

    report = generate_monthly_report(baseline=baseline, current=current, leads=LEADS)

    assert report.config_hash == frozen_config


def test_generating_without_frozen_config_hash_raises(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
) -> None:
    # Deliberately no frozen_config fixture: guarantee the unfrozen state.
    reset_config_hash_for_testing()

    period_a = ReportPeriod(year=2026, month=1)
    period_b = ReportPeriod(year=2026, month=2)
    empty_baseline = collect_window(
        audit_engine=audit_engine,
        eval_store=eval_store,
        signal_store=signal_store,
        period=period_a,
        minutes_saved_per_turn=MINUTES_SAVED_PER_TURN,
        floor_discount=FLOOR_DISCOUNT,
    )
    empty_current = collect_window(
        audit_engine=audit_engine,
        eval_store=eval_store,
        signal_store=signal_store,
        period=period_b,
        minutes_saved_per_turn=MINUTES_SAVED_PER_TURN,
        floor_discount=FLOOR_DISCOUNT,
    )

    with pytest.raises(ConfigHashNotFrozenError):
        generate_monthly_report(baseline=empty_baseline, current=empty_current)


def test_determinism_double_run_equal_and_byte_identical_markdown(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    frozen_config: str,
) -> None:
    _seed_fixtures(audit_engine, eval_store, signal_store)

    baseline_1, current_1 = _collect_both(audit_engine, eval_store, signal_store)
    report_1 = generate_monthly_report(baseline=baseline_1, current=current_1, leads=LEADS)
    markdown_1 = render_markdown(report_1)

    baseline_2, current_2 = _collect_both(audit_engine, eval_store, signal_store)
    report_2 = generate_monthly_report(baseline=baseline_2, current=current_2, leads=LEADS)
    markdown_2 = render_markdown(report_2)

    assert report_1.model_dump() == report_2.model_dump()
    assert markdown_1 == markdown_2


def test_gaps_names_sessions_gap_when_no_session_provenance_seeded(
    audit_engine: sa.Engine,
    eval_store: EvalRunStore,
    signal_store: SqlAlchemyCorrectionSignalStore,
    frozen_config: str,
) -> None:
    _seed_fixtures(audit_engine, eval_store, signal_store)
    baseline, current = _collect_both(audit_engine, eval_store, signal_store)

    assert baseline.utilization.sessions is None
    assert current.utilization.sessions is None

    report = generate_monthly_report(baseline=baseline, current=current, leads=LEADS)

    assert any("sessions" in gap for gap in report.gaps)
    assert any("ne-eval list_runs has no time-window filter" in gap for gap in report.gaps)
