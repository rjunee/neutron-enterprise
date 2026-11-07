# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""KPI summary route (spec §1.6 #12) — read-only U-P-B metrics from ne-obs.

Populated from the EXISTING NE-068 ``ne.obs.collect_window`` read seam (which
calls ONLY ``replay`` / ``list_runs`` / ``read_signals`` — strictly read-only) over
the configured audit + eval + signal stores. The spec's #12 defaulted to an empty
list as the safe floor; ne-obs exists, so we wire it. When the KPI sources are
unconfigured (no eval/signal URLs) the route returns the DOCUMENTED-empty scaffold
(``{"kpis": [], "generated_at": now}``, 200) — never an invented KPI store.

WINDOW DECISION (documented, README): the current calendar month. A fresh
deployment therefore shows zeros (not an error) until the month accrues events.

Access: ANY authenticated principal (KPI aggregates carry no user data).
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends

from ne.api.deps import ApiState, KpiSources, api_state, current_principal
from ne.api.wire import KpiEntryView, KpiMeasurementStatus, KpiSummaryView
from ne.contracts.types import Principal
from ne.obs import ReportPeriod, collect_spend, collect_window
from ne.obs.kpi import SpendAttribution, WindowMetrics

router = APIRouter()

_Principal = Annotated[Principal, Depends(current_principal)]
_State = Annotated[ApiState, Depends(api_state)]


def _entries(window: WindowMetrics, period: ReportPeriod) -> list[KpiEntryView]:
    dims = {"period": period.label}

    def _entry(
        tag: str,
        value: float | int | None,
        unit: str,
        *,
        measurement_status: KpiMeasurementStatus = "measured",
    ) -> KpiEntryView:
        return KpiEntryView(
            kpi_tag=tag,
            value=None if value is None else float(value),
            measurement_status=measurement_status,
            unit=unit,
            dimensions=dict(dims),
            window_start=period.start,
            window_end=period.end_exclusive,
        )

    util = window.utilization
    prof = window.proficiency
    biz = window.business_value

    # Every numeric row is measured, including a zero. ``sessions`` is the sole
    # conditionally instrumented member of this set, so an unavailable
    # session-provenance seam omits that tag rather than claiming a measurement.
    entries = [
        _entry("utilization.agent_turns", util.agent_turns, "count"),
        _entry("utilization.tool_calls", util.tool_calls, "count"),
        _entry("utilization.active_users", util.active_users, "count"),
    ]
    if util.sessions is not None:
        entries.append(_entry("utilization.sessions", util.sessions, "count"))
    entries.extend(
        [
            _entry("utilization.events_total", util.events_total, "count"),
            _entry("proficiency.eval_runs", prof.eval_runs, "count"),
        ]
    )

    # These collectors are configured and ran. A missing denominator makes the
    # rate undefined; it does not make the instrumentation disappear. Keep the
    # tag on the wire with the server-authored status, and keep a genuine 0.0 as
    # a measured numeric value.
    entries.extend(
        [
            _entry(
                "proficiency.eval_pass_rate",
                prof.eval_pass_rate,
                "ratio",
                measurement_status=("undefined" if prof.eval_pass_rate is None else "measured"),
            ),
            _entry(
                "proficiency.correction_signals_total",
                prof.correction_signals_total,
                "count",
            ),
            _entry("proficiency.positive_signals", prof.positive_signals, "count"),
            _entry("proficiency.negative_signals", prof.negative_signals, "count"),
            _entry(
                "proficiency.negative_signal_rate",
                prof.negative_signal_rate,
                "ratio",
                measurement_status=(
                    "undefined" if prof.negative_signal_rate is None else "measured"
                ),
            ),
            _entry("business_value.hours_saved_floor", biz.hours_saved_floor, "hours"),
        ]
    )
    return entries


#: Wire prefix for a per-outcome spend row. The suffix is the DECLARED ``kpi_tag``
#: verbatim, so the console never has to know the customer's outcome names.
SPEND_OUTCOME_PREFIX = "spend.outcome."
#: The honestly-labelled bucket for spend NO outcome may claim.
SPEND_UNATTRIBUTED_TAG = "spend.unattributed"


def _spend_entries(spend: SpendAttribution, period: ReportPeriod) -> list[KpiEntryView]:
    """The spend-attribution rows (C8) for one window.

    Every figure here is the SERVER'S — including ``spend.attributed_share``,
    which is computed in ``ne.obs.collect_spend`` precisely so the console never
    divides one served number by another (C7 thin client).

    HONESTY INVARIANTS pinned by ``test_kpi_spend_rows.py``:

    * ``spend.unattributed`` is emitted WHENEVER there was any unattributed
      spend or any untagged call — it is never suppressed to make attribution
      look complete, and it is never merged into a named outcome.
    * ``spend.attributed_share`` is OMITTED (not zero, not 1.0) when the server
      could not compute it, exactly like every other ``None`` metric above.
    * ``spend.calls_missing_cost`` is emitted whenever any call's price was
      unrecorded, so a total that is SHORT says so rather than reading complete.
    """
    dims = {"period": period.label}

    def _entry(tag: str, value: float | int, unit: str, **extra: str) -> KpiEntryView:
        return KpiEntryView(
            kpi_tag=tag,
            value=float(value),
            measurement_status="measured",
            unit=unit,
            dimensions={**dims, **extra},
            window_start=period.start,
            window_end=period.end_exclusive,
        )

    entries: list[KpiEntryView] = [
        # The call count rides ``dimensions`` as a SERVED string so the console
        # can render "N calls" beside the money without counting anything itself.
        # ``unpriced`` says how many of those calls the money figure does NOT
        # cover, so a row whose dollar figure is short says so on its own row.
        _entry(
            f"{SPEND_OUTCOME_PREFIX}{outcome.kpi_tag}",
            outcome.cost_usd,
            "usd",
            calls=str(outcome.calls),
            unpriced=str(outcome.unpriced_calls),
        )
        for outcome in spend.outcomes
    ]

    if spend.unattributed_calls > 0 or spend.unattributed_usd != 0.0:
        entries.append(
            _entry(
                SPEND_UNATTRIBUTED_TAG,
                spend.unattributed_usd,
                "usd",
                calls=str(spend.unattributed_calls),
            )
        )

    entries.append(
        _entry(
            "spend.attributed_usd",
            spend.attributed_usd,
            "usd",
            calls=str(spend.attributed_calls),
        )
    )
    entries.append(_entry("spend.total_usd", spend.total_usd, "usd", calls=str(spend.total_calls)))
    # THE COVERAGE FIGURE. Served alongside the dollar share because the dollar
    # share is not one: where a self-hosted lane prices every call at $0.00, a
    # single priced frontier call can carry ``attributed_share`` to 1.0 while
    # hundreds of untagged calls sit at zero. A screen showing only the dollar
    # share would read "100% attributed" on exactly that deployment.
    if spend.attributed_call_share is not None:
        entries.append(_entry("spend.attributed_call_share", spend.attributed_call_share, "ratio"))
    if spend.attributed_share is not None:
        entries.append(_entry("spend.attributed_share", spend.attributed_share, "ratio"))
    if spend.calls_missing_cost > 0:
        entries.append(_entry("spend.calls_missing_cost", spend.calls_missing_cost, "count"))
    return entries


@router.get("/v1/kpi/summary")
async def kpi_summary(
    principal: _Principal,
    state: _State,
) -> KpiSummaryView:
    now = datetime.now(UTC)
    src: KpiSources | None = state.kpi_sources
    if src is None:
        # The spec §1.6 #12 documented safe floor: empty when the read seams are
        # unconfigured (no eval/signal URLs).
        return KpiSummaryView(kpis=[], generated_at=now)

    period = ReportPeriod(year=now.year, month=now.month)
    window = await anyio.to_thread.run_sync(
        functools.partial(
            collect_window,
            audit_engine=src.audit_engine,
            eval_store=src.eval_store,
            signal_store=src.signal_store,
            period=period,
            turn_actions=src.obs_settings.turn_actions,
            tool_actions=src.obs_settings.tool_actions,
            minutes_saved_per_turn=src.obs_settings.minutes_saved_per_turn,
            floor_discount=src.obs_settings.floor_discount,
        )
    )
    # C8 spend attribution — the same READ-ONLY audit engine, one more replay over
    # the same window. Kept a separate collector (not folded into WindowMetrics)
    # because U-P-B is a fixed metric set while the outcome rows are per-customer
    # and open-ended.
    spend = await anyio.to_thread.run_sync(
        functools.partial(collect_spend, src.audit_engine, period)
    )
    return KpiSummaryView(
        kpis=[*_entries(window, period), *_spend_entries(spend, period)],
        generated_at=now,
    )
