# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ne.obs.otlp — the outbox → OTel-log handler + the C2 endpoint guardian.

Unit-level (no DB): an OutboxRecord maps to one OTel log record (body = payload,
outbox coordinates as ne.* attributes); a non-SUCCESS export raises OutboxExportError;
build_otlp_log_record_exporter rejects an external host and accepts the in-cluster
service name.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord  # pyright: ignore[reportPrivateImportUsage]
from opentelemetry.sdk._logs.export import (  # pyright: ignore[reportPrivateImportUsage]
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
)

from ne.events.outbox import OutboxRecord
from ne.net import UnsafeEndpointError
from ne.obs.otlp import (
    OutboxExportError,
    OutboxOtelLogHandler,
    build_otlp_log_record_exporter,
)


def _record(**over: object) -> OutboxRecord:
    fields: dict[str, object] = {
        "id": 7,
        "event_id": "evt-1",
        "event_type": "audit",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "config_hash": "a" * 64,
        "kpi_tag": None,
        "actor": "sys",
        "payload": {"action": "tool.invoke"},
    }
    fields.update(over)
    return OutboxRecord(**fields)  # type: ignore[arg-type]


class _Failing(LogRecordExporter):
    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        return LogRecordExportResult.FAILURE

    def shutdown(self) -> None:
        return None


def test_handler_maps_record_to_log_record() -> None:
    exporter = InMemoryLogRecordExporter()
    OutboxOtelLogHandler(exporter)([_record(kpi_tag="acme")])
    finished = exporter.get_finished_logs()
    assert len(finished) == 1
    lr = finished[0].log_record
    assert lr.body == {"action": "tool.invoke"}
    attrs = lr.attributes
    assert attrs is not None
    assert attrs["ne.event_type"] == "audit"
    assert attrs["ne.event_id"] == "evt-1"
    assert attrs["ne.config_hash"] == "a" * 64
    assert attrs["ne.outbox_id"] == 7
    assert attrs["ne.kpi_tag"] == "acme"


def test_empty_batch_is_a_noop() -> None:
    exporter = InMemoryLogRecordExporter()
    OutboxOtelLogHandler(exporter)([])
    assert not exporter.get_finished_logs()


def test_export_failure_raises() -> None:
    with pytest.raises(OutboxExportError):
        OutboxOtelLogHandler(_Failing())([_record()])


def test_external_endpoint_rejected_in_cluster_accepted() -> None:
    with pytest.raises(UnsafeEndpointError):
        build_otlp_log_record_exporter("https://siem.example.com")
    assert build_otlp_log_record_exporter("http://otel-collector:4318") is not None
