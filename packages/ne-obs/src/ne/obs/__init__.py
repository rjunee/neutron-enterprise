# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Observability, signals, and KPI instrumentation.

The GenAI OpenTelemetry semconv wrapper (:mod:`ne.obs.genai`) is the SINGLE place
the platform touches ``opentelemetry.semconv`` — the package is exact-pinned and
wrapped because the GenAI conventions are still in development (SPEC §10).

The U-P-B (Utilization/Proficiency/Business-value) KPI reporting layer (NE-068,
:mod:`ne.obs.kpi`/:mod:`ne.obs.collect`/:mod:`ne.obs.report`) consumes the
public read APIs of ne-audit, ne-eval, and ne-flywheel to produce a
deterministic monthly report of named-KPI baseline-to-current deltas plus a
conservative, documented hours-saved FLOOR — read-only, with no writes back
into any consumed package.
"""

from __future__ import annotations

__version__ = "0.1.0"

from ne.obs.collect import (
    collect_proficiency,
    collect_spend,
    collect_utilization,
    collect_window,
    compute_business_value,
)
from ne.obs.config import ObsReportSettings, leads_from_settings
from ne.obs.errors import ObsError, ObsReportError
from ne.obs.factory import ReportDataSources, report_sources_from_settings
from ne.obs.genai import (
    ATTR_INPUT_TOKENS,
    ATTR_NE_CONFIG_HASH,
    ATTR_NE_COST_USD,
    ATTR_NE_KPI_TAG,
    ATTR_NE_LANE,
    ATTR_OPERATION_NAME,
    ATTR_OUTPUT_TOKENS,
    ATTR_PROVIDER_NAME,
    ATTR_REQUEST_MODEL,
    ATTR_RESPONSE_MODEL,
    genai_attributes,
    genai_span,
)
from ne.obs.kpi import (
    BusinessValueMetrics,
    KpiLead,
    KpiValue,
    MonthlyUpbReport,
    ProficiencyMetrics,
    ReportPeriod,
    SpendAttribution,
    SpendByOutcome,
    UtilizationMetrics,
    WindowMetrics,
)
from ne.obs.otlp import (
    OutboxExportError,
    OutboxOtelLogHandler,
    build_otlp_log_record_exporter,
)
from ne.obs.report import (
    KPI_DEFS,
    METHODOLOGY,
    generate_monthly_report,
    render_json,
    render_markdown,
)

__all__ = [
    "ATTR_INPUT_TOKENS",
    "ATTR_NE_CONFIG_HASH",
    "ATTR_NE_COST_USD",
    "ATTR_NE_KPI_TAG",
    "ATTR_NE_LANE",
    "ATTR_OPERATION_NAME",
    "ATTR_OUTPUT_TOKENS",
    "ATTR_PROVIDER_NAME",
    "ATTR_REQUEST_MODEL",
    "ATTR_RESPONSE_MODEL",
    "KPI_DEFS",
    "METHODOLOGY",
    "BusinessValueMetrics",
    "KpiLead",
    "KpiValue",
    "MonthlyUpbReport",
    "ObsError",
    "ObsReportError",
    "ObsReportSettings",
    "OutboxExportError",
    "OutboxOtelLogHandler",
    "ProficiencyMetrics",
    "ReportDataSources",
    "ReportPeriod",
    "SpendAttribution",
    "SpendByOutcome",
    "UtilizationMetrics",
    "WindowMetrics",
    "__version__",
    "build_otlp_log_record_exporter",
    "collect_proficiency",
    "collect_spend",
    "collect_utilization",
    "collect_window",
    "compute_business_value",
    "genai_attributes",
    "genai_span",
    "generate_monthly_report",
    "leads_from_settings",
    "render_json",
    "render_markdown",
    "report_sources_from_settings",
]
