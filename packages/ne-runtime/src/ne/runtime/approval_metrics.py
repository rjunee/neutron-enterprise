# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Approval-latency metrics — an OTel histogram, ne-obs-pattern (NE-035).

Mirrors ``ne.gateway.instrumentation``: import ONLY the OpenTelemetry metrics
*API* (never ``opentelemetry.semconv`` — that stays exclusive to ``ne.obs``, C3)
and reuse the ``ne.obs`` config-hash attribute constant rather than redefining it.
Only the two approval-local, NON-semconv attribute keys and the metric name are
declared here.

The ``Meter`` is injectable so tests can bind an in-memory ``MeterProvider`` (an
``InMemoryMetricReader``) without touching global provider state; the default is
the module-scoped meter ``ne.runtime.approvals``.
"""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from ne.obs import ATTR_NE_CONFIG_HASH

#: Histogram of decided approval latency, in seconds (requested -> terminal).
METRIC_APPROVAL_LATENCY = "ne.approval.latency"
#: Approval-local, NON-semconv attribute keys.
ATTR_NE_APPROVAL_TIER = "ne.approval.tier"
ATTR_NE_APPROVAL_DECISION = "ne.approval.decision"


class ApprovalMetrics:
    """Records approval-latency points on an OTel histogram (injectable meter)."""

    def __init__(self, meter: Meter | None = None) -> None:
        m = meter if meter is not None else metrics.get_meter("ne.runtime.approvals")
        self._latency = m.create_histogram(
            METRIC_APPROVAL_LATENCY,
            unit="s",
            description="Latency from approval request to terminal decision.",
        )

    def record_latency(self, *, tier: int, decision: str, seconds: float, config_hash: str) -> None:
        """Record one decided-latency point tagged with tier / decision / config_hash."""
        self._latency.record(
            seconds,
            {
                ATTR_NE_APPROVAL_TIER: tier,
                ATTR_NE_APPROVAL_DECISION: decision,
                ATTR_NE_CONFIG_HASH: config_hash,
            },
        )
