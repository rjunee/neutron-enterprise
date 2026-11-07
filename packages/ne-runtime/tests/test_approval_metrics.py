# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Approval-latency histogram asserted via an in-memory OTel MeterProvider."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from ne.contracts import get_config_hash
from ne.contracts.governance import ApprovalStatus
from ne.runtime.approval_metrics import (
    ATTR_NE_APPROVAL_DECISION,
    ATTR_NE_APPROVAL_TIER,
    METRIC_APPROVAL_LATENCY,
    ApprovalMetrics,
)
from ne.runtime.approvals import DurableApprovalGate

from .conftest import FakeClock, RecordingAuditSink, approval_request, approval_settings


def _latency_points(reader: InMemoryMetricReader) -> list[Any]:
    data = reader.get_metrics_data()
    assert data is not None
    points: list[Any] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == METRIC_APPROVAL_LATENCY:
                    points.extend(metric.data.data_points)
    return points


def _make(
    reader: InMemoryMetricReader, store: Any, *, clock: Any = None, **settings_kwargs: Any
) -> DurableApprovalGate:
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("test.ne.runtime.approvals")
    return DurableApprovalGate(
        store=store,
        sink=RecordingAuditSink(),
        settings=approval_settings(**settings_kwargs),
        metrics=ApprovalMetrics(meter=meter),
        clock=clock if clock is not None else (lambda: datetime.now(UTC)),
    )


def test_latency_histogram_recorded_with_tier_decision_confighash(approval_store: Any) -> None:
    reader = InMemoryMetricReader()
    gate = _make(reader, approval_store)

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        outcome = await gate.approve(rid, approver="carol")
        assert outcome is not None and outcome.status is ApprovalStatus.APPROVED

    asyncio.run(scenario())

    points = _latency_points(reader)
    assert len(points) == 1
    attrs = dict(points[0].attributes)
    assert attrs[ATTR_NE_APPROVAL_TIER] == 2
    assert attrs[ATTR_NE_APPROVAL_DECISION] == "approved"
    assert attrs["ne.config_hash"] == get_config_hash()


def test_expired_records_latency_metric(approval_store: Any) -> None:
    reader = InMemoryMetricReader()
    clock = FakeClock()
    gate = _make(reader, approval_store, expiry_s=1000.0, poll_interval_s=0.0, clock=clock)

    async def scenario() -> None:
        rid = await gate.submit(approval_request("write.external.email.send", 2))
        clock.advance(2000.0)
        final = await asyncio.wait_for(gate.await_outcome(rid), timeout=10)
        assert final.status is ApprovalStatus.EXPIRED

    asyncio.run(scenario())

    points = _latency_points(reader)
    assert len(points) == 1
    assert dict(points[0].attributes)[ATTR_NE_APPROVAL_DECISION] == "expired"
