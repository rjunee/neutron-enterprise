# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Typed U-P-B (Utilization/Proficiency/Business-value) KPI models (NE-068).

Every model is a FROZEN pydantic model (immutable, hashable-by-value) so a
generated report is a stable, comparable value — determinism (SPEC §2.1) means
the SAME inputs always produce the SAME report, and tuples (never lists) are
used throughout so field order — and therefore equality/serialization — never
depends on set/dict iteration order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportPeriod(BaseModel):
    """One calendar month, the reporting unit for the monthly U-P-B report."""

    model_config = ConfigDict(frozen=True)

    year: int = Field(ge=2000, le=2200)
    month: int = Field(ge=1, le=12)

    @property
    def start(self) -> datetime:
        """The first instant of the month (UTC, inclusive)."""
        return datetime(self.year, self.month, 1, tzinfo=UTC)

    @property
    def end_exclusive(self) -> datetime:
        """The first instant of the NEXT month (UTC, exclusive upper bound)."""
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=UTC)
        return datetime(self.year, self.month + 1, 1, tzinfo=UTC)

    @property
    def end_inclusive_micro(self) -> datetime:
        """The LAST microsecond of the month — for INCLUSIVE ``since<=x<=until`` seams."""
        return self.end_exclusive - timedelta(microseconds=1)

    @property
    def label(self) -> str:
        """``YYYY-MM`` label for reports/markdown."""
        return f"{self.year:04d}-{self.month:02d}"

    def previous(self) -> ReportPeriod:
        """The calendar month immediately before this one (handles year rollover)."""
        if self.month == 1:
            return ReportPeriod(year=self.year - 1, month=12)
        return ReportPeriod(year=self.year, month=self.month - 1)


class KpiLead(BaseModel):
    """The named owner of a KPI (C8: business KPIs have a named lead)."""

    model_config = ConfigDict(frozen=True)

    kpi: str
    name: str
    email: str


class UtilizationMetrics(BaseModel):
    """Usage: agent turns/tool calls/active users/sessions over the window."""

    model_config = ConfigDict(frozen=True)

    agent_turns: int
    tool_calls: int
    active_users: int
    #: ``None`` means an instrumentation gap — no session_id provenance was found.
    sessions: int | None
    events_total: int


class ProficiencyMetrics(BaseModel):
    """Quality: eval pass-rate and correction-signal (thumbs/re-query/…) rates."""

    model_config = ConfigDict(frozen=True)

    eval_runs: int
    eval_pass_rate: float | None
    correction_signals_total: int
    positive_signals: int
    negative_signals: int
    negative_signal_rate: float | None


class BusinessValueMetrics(BaseModel):
    """The conservative, documented hours-saved FLOOR (see ``ne.obs.report.METHODOLOGY``)."""

    model_config = ConfigDict(frozen=True)

    assisted_turns: int
    discounted_turns: int
    minutes_saved_per_turn: float
    floor_discount: float
    hours_saved_floor: float


class KpiValue(BaseModel):
    """One named KPI row: baseline -> current, its delta, direction, and lead."""

    model_config = ConfigDict(frozen=True)

    name: str
    unit: str
    baseline: float | None
    current: float | None
    delta: float | None
    direction: Literal["higher_is_better", "lower_is_better"]
    lead: KpiLead | None


class SpendByOutcome(BaseModel):
    """Model spend recorded against ONE named business outcome (C8).

    ``cost_usd`` sums ONLY ``priced_calls`` — the calls whose price was actually
    recorded. ``unpriced_calls`` were attributed to this outcome just as firmly
    but carry no price, so they are counted (attribution is real even where the
    price is not) and summed nowhere (their cost is unknown, not zero).
    ``calls`` is the full attributed count, ``priced_calls + unpriced_calls``.
    """

    model_config = ConfigDict(frozen=True)

    kpi_tag: str
    cost_usd: float
    calls: int
    priced_calls: int
    unpriced_calls: int


class SpendAttribution(BaseModel):
    """Model spend over a window, split into named outcomes and what is untagged.

    The unattributed bucket is a FIRST-CLASS field, never a residual the caller
    is expected to derive: untagged spend is real spend that no outcome may
    claim. Nothing in this model imputes, estimates, or defaults a tag.
    """

    model_config = ConfigDict(frozen=True)

    period: ReportPeriod
    #: Per-outcome rows, ordered by ``kpi_tag`` so a report is byte-stable.
    outcomes: tuple[SpendByOutcome, ...]
    attributed_usd: float
    #: Calls carrying a declared outcome, PRICED OR NOT.
    attributed_calls: int
    #: Spend from calls that carried NO declared outcome tag. Shown as its own
    #: figure; folding it into a named outcome would be a false spend claim.
    unattributed_usd: float
    unattributed_calls: int
    total_usd: float
    total_calls: int
    #: ``attributed_usd / total_usd``, or ``None`` when ``total_usd`` is zero —
    #: no denominator is NOT "100% attributed" (SPEC §2.1 honest-gap rule).
    #:
    #: DOLLAR-WEIGHTED, and therefore NOT a coverage figure: on a deployment
    #: whose self-hosted lane prices every call at 0.0, ONE priced frontier call
    #: can carry this to 1.0 while hundreds of untagged calls sit at $0.00. Read
    #: it beside :attr:`attributed_call_share`, which is the coverage figure, and
    #: never on its own.
    attributed_share: float | None
    #: ``attributed_calls / total_calls`` — the CALL-weighted coverage figure, the
    #: one that answers "how much of what we ran is attributed at all". ``None``
    #: when no call was recorded.
    attributed_call_share: float | None
    #: Calls with no recorded price, across every bucket. Their cost is UNKNOWN,
    #: not zero, so they are excluded from every dollar sum above and reported
    #: here — a total that is SHORT says so rather than reading complete.
    calls_missing_cost: int


class WindowMetrics(BaseModel):
    """The full U-P-B metric set collected for one :class:`ReportPeriod`."""

    model_config = ConfigDict(frozen=True)

    period: ReportPeriod
    utilization: UtilizationMetrics
    proficiency: ProficiencyMetrics
    business_value: BusinessValueMetrics


class MonthlyUpbReport(BaseModel):
    """The deterministic monthly U-P-B report: baseline -> current per named KPI."""

    model_config = ConfigDict(frozen=True)

    baseline: WindowMetrics
    current: WindowMetrics
    kpis: tuple[KpiValue, ...]
    leads: tuple[KpiLead, ...]
    config_hash: str
    methodology: str
    gaps: tuple[str, ...]
