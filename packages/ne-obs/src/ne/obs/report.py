# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The deterministic monthly U-P-B report generator + renderers (NE-068).

``generate_monthly_report`` is a PURE function of two already-collected
:class:`~ne.obs.kpi.WindowMetrics` (plus optional named KPI leads) — no wall
clock, no randomness, no set/dict iteration order anywhere in its output, so
the SAME inputs always produce a byte-identical report (SPEC §2.1
determinism). ``render_markdown``/``render_json`` are equally pure functions
of the resulting :class:`~ne.obs.kpi.MonthlyUpbReport`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple

from ne.contracts import get_config_hash
from ne.obs.kpi import KpiLead, KpiValue, MonthlyUpbReport, WindowMetrics

#: Verbatim hours-saved-floor methodology (also mirrored in README.md).
METHODOLOGY = (
    "Hours-saved floor methodology (conservative, defensible): (1) count only "
    "SUCCESSFUL assisted agent turns in the window (audit outcome=success on "
    "the configured turn actions); (2) subtract every negative correction "
    "signal in the window (thumbs-down, wrong-source, re-query, "
    "approval-rejection) one-for-one, floored at zero — a turn a user pushed "
    "back on claims zero savings; (3) multiply by a per-turn minutes-saved "
    "assumption (default 2.0 minutes) and a conservatism discount (default "
    "0.5); (4) divide by 60 and round to 2 decimals. Every assumption is "
    "chosen to UNDER-claim: this is a floor, not an estimate. Per SPEC C8, "
    "hours-saved is a supporting metric only; the named business KPIs lead "
    "the report."
)


class _KpiDef(NamedTuple):
    name: str
    unit: str
    direction: Literal["higher_is_better", "lower_is_better"]
    #: Decimal places used when rounding baseline/current/delta for this KPI.
    decimals: int


#: Canonical KPI registry, in report order (C8: business KPIs lead; the
#: supporting hours-saved-floor metric is deliberately LAST).
KPI_DEFS: tuple[_KpiDef, ...] = (
    _KpiDef("utilization.agent_turns", "turns", "higher_is_better", 2),
    _KpiDef("utilization.active_users", "users", "higher_is_better", 2),
    _KpiDef("utilization.tool_calls", "calls", "higher_is_better", 2),
    _KpiDef("proficiency.eval_pass_rate", "ratio", "higher_is_better", 4),
    _KpiDef("proficiency.negative_signal_rate", "signals/turn", "lower_is_better", 4),
    _KpiDef("business_value.hours_saved_floor", "hours (floor)", "higher_is_better", 2),
)

_SESSIONS_GAP = (
    "sessions: no session_id provenance found in audit events — session "
    "instrumentation gap (emitters do not yet stamp session_id); sessions KPI "
    "omitted"
)
_EVAL_WINDOW_GAP = (
    "ne-eval list_runs has no time-window filter; window applied in-memory "
    "over the newest EVAL_SCAN_LIMIT runs"
)


def _get_kpi_value(window: WindowMetrics, name: str) -> float | None:
    group, field = name.split(".", 1)
    value = getattr(getattr(window, group), field)
    return float(value) if value is not None else None


def generate_monthly_report(
    *,
    baseline: WindowMetrics,
    current: WindowMetrics,
    leads: Sequence[KpiLead] = (),
) -> MonthlyUpbReport:
    """Build the deterministic monthly U-P-B report from two collected windows.

    Stamps the report with the FROZEN process config_hash
    (:func:`ne.contracts.get_config_hash`); ``ConfigHashNotFrozenError``
    propagates rather than an unstamped report being produced (mirrors
    ``ne.obs.genai.genai_attributes``).
    """
    leads_tuple = tuple(leads)

    kpi_values: list[KpiValue] = []
    for kdef in KPI_DEFS:
        baseline_raw = _get_kpi_value(baseline, kdef.name)
        current_raw = _get_kpi_value(current, kdef.name)
        baseline_value = round(baseline_raw, kdef.decimals) if baseline_raw is not None else None
        current_value = round(current_raw, kdef.decimals) if current_raw is not None else None
        delta = (
            round(current_raw - baseline_raw, kdef.decimals)
            if (baseline_raw is not None and current_raw is not None)
            else None
        )
        lead = next((lead for lead in leads_tuple if lead.kpi == kdef.name), None)
        kpi_values.append(
            KpiValue(
                name=kdef.name,
                unit=kdef.unit,
                baseline=baseline_value,
                current=current_value,
                delta=delta,
                direction=kdef.direction,
                lead=lead,
            )
        )

    gaps: list[str] = []
    if baseline.utilization.sessions is None or current.utilization.sessions is None:
        gaps.append(_SESSIONS_GAP)
    gaps.append(_EVAL_WINDOW_GAP)

    return MonthlyUpbReport(
        baseline=baseline,
        current=current,
        kpis=tuple(kpi_values),
        leads=leads_tuple,
        config_hash=get_config_hash(),
        methodology=METHODOLOGY,
        gaps=tuple(gaps),
    )


def _fmt_optional(value: float | None) -> str:
    return "n/a" if value is None else str(value)


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value}"


def render_markdown(report: MonthlyUpbReport) -> str:
    """Render ``report`` as deterministic markdown (no charting dep)."""
    lines: list[str] = []
    lines.append(
        f"# U-P-B Monthly Report — {report.current.period.label} "
        f"(baseline {report.baseline.period.label})"
    )
    lines.append("")

    lines.append("## KPI leads")
    lines.append("")
    lines.append("| kpi | lead | email |")
    lines.append("| --- | --- | --- |")
    for lead in report.leads:
        lines.append(f"| {lead.kpi} | {lead.name} | {lead.email} |")
    lines.append("")

    lines.append("## KPIs: baseline → current")
    lines.append("")
    lines.append("| kpi | unit | baseline | current | delta | direction |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for kpi in report.kpis:
        lines.append(
            f"| {kpi.name} | {kpi.unit} | {_fmt_optional(kpi.baseline)} | "
            f"{_fmt_optional(kpi.current)} | {_fmt_delta(kpi.delta)} | {kpi.direction} |"
        )
    lines.append("")

    lines.append("## Utilization")
    lines.append("")
    for field_name in ("agent_turns", "tool_calls", "active_users", "sessions", "events_total"):
        b = getattr(report.baseline.utilization, field_name)
        c = getattr(report.current.utilization, field_name)
        lines.append(f"- {field_name}: {_fmt_optional(b)} → {_fmt_optional(c)}")
    lines.append("")

    lines.append("## Proficiency")
    lines.append("")
    for field_name in (
        "eval_runs",
        "eval_pass_rate",
        "correction_signals_total",
        "positive_signals",
        "negative_signals",
        "negative_signal_rate",
    ):
        b = getattr(report.baseline.proficiency, field_name)
        c = getattr(report.current.proficiency, field_name)
        lines.append(f"- {field_name}: {_fmt_optional(b)} → {_fmt_optional(c)}")
    lines.append("")

    lines.append("## Business value")
    lines.append("")
    for field_name in (
        "assisted_turns",
        "discounted_turns",
        "minutes_saved_per_turn",
        "floor_discount",
        "hours_saved_floor",
    ):
        b = getattr(report.baseline.business_value, field_name)
        c = getattr(report.current.business_value, field_name)
        lines.append(f"- {field_name}: {_fmt_optional(b)} → {_fmt_optional(c)}")
    lines.append("")

    lines.append("## Methodology")
    lines.append("")
    lines.append(report.methodology)
    lines.append("")

    lines.append("## Data gaps")
    lines.append("")
    for gap in report.gaps:
        lines.append(f"- {gap}")
    lines.append("")

    lines.append(f"config_hash: {report.config_hash}")

    return "\n".join(lines) + "\n"


def render_json(report: MonthlyUpbReport) -> str:
    """Render ``report`` as deterministic, pretty-printed JSON."""
    return report.model_dump_json(indent=2)
