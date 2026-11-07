# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KPI summary — POPULATED from the real ne-obs collect_window, + documented-empty."""

from __future__ import annotations

from datetime import UTC, datetime

from ne.eval.store import EvalRunRecord
from ne.events import new_audit_event
from ne.schema import AuditOutcome

from .conftest import AppBuilder, get_state, run

_ENTRY_KEYS = {
    "kpi_tag",
    "value",
    "measurement_status",
    "unit",
    "dimensions",
    "window_start",
    "window_end",
}


def test_kpi_summary_populated_from_ne_obs(make_app: AppBuilder) -> None:
    h = make_app()  # eval + signal configured by default ⇒ kpi_sources present
    state = get_state(h.app)
    now = datetime.now(UTC)

    async def _seed() -> None:
        for _ in range(2):
            await state.sink.append(
                new_audit_event(
                    action="ne.gateway.complete",
                    resource="gateway",
                    outcome=AuditOutcome.success,
                    actor="user:ada",
                    occurred_at=now,
                )
            )

    run(_seed())
    state.eval_store.append(  # type: ignore[union-attr]
        EvalRunRecord(
            run_id="r1",
            suite="s",
            config_hash="c",
            golden_set_hash="g",
            judge_model_id=None,
            judge_family=None,
            rubric_versions={},
            metrics={"recall": 0.9},
            thresholds={"recall": 0.8},
            statuses={"recall": "pass"},
            judge_error_rate=0.0,
            passed=True,
            created_at=now,
        )
    )

    period_label = f"{now.year:04d}-{now.month:02d}"

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/kpi/summary", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"kpis", "generated_at"}
            kpis = body["kpis"]
            assert kpis
            for entry in kpis:
                assert set(entry) == _ENTRY_KEYS
            by_tag = {e["kpi_tag"]: e for e in kpis}
            turns = by_tag["utilization.agent_turns"]
            assert turns["value"] == 2.0
            assert turns["measurement_status"] == "measured"
            assert turns["dimensions"] == {"period": period_label}
            # The denominator is agent turns, not correction signals. A served
            # 0.0 is a real measured rate and must never be suppressed.
            assert by_tag["proficiency.negative_signal_rate"]["value"] == 0.0
            assert by_tag["proficiency.negative_signal_rate"]["measurement_status"] == "measured"

    run(_t())


def test_kpi_summary_distinguishes_undefined_ratios_from_uninstrumented_tags(
    make_app: AppBuilder,
) -> None:
    """Configured collectors with zero denominators ran; session provenance did not."""
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/kpi/summary", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            by_tag = {entry["kpi_tag"]: entry for entry in r.json()["kpis"]}

            assert by_tag["proficiency.eval_pass_rate"]["value"] is None
            assert by_tag["proficiency.eval_pass_rate"]["measurement_status"] == "undefined"
            assert by_tag["proficiency.negative_signal_rate"]["value"] is None
            assert by_tag["proficiency.negative_signal_rate"]["measurement_status"] == "undefined"
            assert "utilization.sessions" not in by_tag

    run(_t())


def test_kpi_summary_unconfigured_empty(make_app: AppBuilder) -> None:
    h = make_app(kpi_configured=False)  # no signal URL ⇒ kpi_sources None

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/kpi/summary", headers=h.bearer("u", groups=[]))
            assert r.status_code == 200
            body = r.json()
            assert body["kpis"] == []
            assert isinstance(body["generated_at"], str)

    run(_t())


def test_kpi_requires_auth_only(make_app: AppBuilder) -> None:
    h = make_app()

    async def _t() -> None:
        async with h.client() as client:
            r = await client.get("/api/v1/kpi/summary", headers=h.bearer("nobody", groups=[]))
            assert r.status_code == 200

    run(_t())
