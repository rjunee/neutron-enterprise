# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FIX-SYNC-INVOKERS (P2-21) — the real outbox → OTel-logs exporter.

Proves the exporter drains + acks and the in-memory (fixture) path: three appended
events export as three OTel log records whose bodies are the event payloads and whose
attributes carry the outbox coordinates; a second drain sees nothing (cursor
advanced). An export FAILURE leaves the cursor unmoved (at-least-once redelivery). And
the C2 guardian: an external endpoint is rejected, the in-cluster service name is
accepted.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from opentelemetry.sdk._logs import ReadableLogRecord  # pyright: ignore[reportPrivateImportUsage]
from opentelemetry.sdk._logs.export import (  # pyright: ignore[reportPrivateImportUsage]
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
)

from ne.events import append, drain_once, ensure_outbox_schema
from ne.net import UnsafeEndpointError
from ne.obs.otlp import OutboxOtelLogHandler, build_otlp_log_record_exporter
from ne.schema import AuditEvent, AuditOutcome

_HASH = "a" * 64
_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _audit(action: str = "tool.invoke") -> AuditEvent:
    return AuditEvent(
        event_id=str(uuid.uuid4()),
        event_type="audit",
        config_hash=_HASH,
        actor="sys",
        provenance={},
        occurred_at=_TS,
        action=action,
        resource="urn:tool:search",
        outcome=AuditOutcome.success,
    )


def _engine() -> sa.Engine:
    eng = sa.create_engine("sqlite://")
    ensure_outbox_schema(eng)
    return eng


def _seed(engine: sa.Engine, n: int) -> None:
    with engine.begin() as conn:
        for _ in range(n):
            append(conn, _audit())


class _FailingExporter(LogRecordExporter):
    """A LogRecordExporter whose export always FAILS (the fail-open proof)."""

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        return LogRecordExportResult.FAILURE

    def shutdown(self) -> None:
        return None


# --- (1) drain exports + acks; second drain sees nothing --------------------


def test_drain_exports_acks_and_stops_redelivering() -> None:
    engine = _engine()
    _seed(engine, 3)
    exporter = InMemoryLogRecordExporter()
    handler = OutboxOtelLogHandler(exporter)

    result = drain_once(engine, "siem-otel", handler)
    assert result.processed == 3
    assert result.error is None

    finished = exporter.get_finished_logs()
    assert len(finished) == 3
    for rec in finished:
        lr = rec.log_record
        # body is the event payload dict.
        assert isinstance(lr.body, dict)
        assert lr.body["action"] == "tool.invoke"
        assert lr.body["outcome"] == "success"
        # attributes carry the outbox coordinates.
        attrs = lr.attributes
        assert attrs is not None
        assert attrs["ne.event_type"] == "audit"
        assert attrs["ne.actor"] == "sys"
        assert attrs["ne.config_hash"] == _HASH
        assert isinstance(attrs["ne.event_id"], str)

    # Cursor advanced: a second drain sees nothing.
    result2 = drain_once(engine, "siem-otel", handler)
    assert result2.processed == 0
    assert len(exporter.get_finished_logs()) == 3  # unchanged


# --- (2) export failure leaves the cursor unmoved (at-least-once) ------------


def test_export_failure_leaves_cursor_unmoved() -> None:
    engine = _engine()
    _seed(engine, 2)

    failing = OutboxOtelLogHandler(_FailingExporter())
    result = drain_once(engine, "siem-otel", failing)
    assert result.error is not None
    assert result.processed == 0

    # The SAME window redelivers to a healthy exporter (cursor never advanced).
    exporter = InMemoryLogRecordExporter()
    result2 = drain_once(engine, "siem-otel", OutboxOtelLogHandler(exporter))
    assert result2.processed == 2
    assert len(exporter.get_finished_logs()) == 2


# --- (3) C2 guardian: external rejected, in-cluster accepted -----------------


def test_external_endpoint_rejected() -> None:
    with pytest.raises(UnsafeEndpointError):
        build_otlp_log_record_exporter("https://siem.example.com")
    # a dotless in-cluster service name is accepted.
    exporter = build_otlp_log_record_exporter("http://otel-collector:4318")
    assert exporter is not None


# --- (4) a consumer with NO OTLP endpoint fails worker boot (Codex P1) --------


def test_consumer_without_otlp_endpoint_fails_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured drain consumer with an empty endpoint must FAIL BOOT.

    Regression guard for the Codex P1: a count-only fallback would ack (advance the
    cursor) without exporting, and the P2-21 retention prune would then permanently
    delete never-exported audit/cost/approval events. `_build_outbox_handler` must
    instead refuse to build a sink-less handler.
    """
    from ne.worker.main import _build_outbox_handler
    from ne.worker.settings import WorkerSettings

    monkeypatch.setenv("NE_WORKER_OUTBOX_CONSUMER", "siem-otel")
    monkeypatch.delenv("NE_WORKER_OUTBOX_OTLP_ENDPOINT", raising=False)
    settings = WorkerSettings()
    assert settings.outbox_consumer == "siem-otel"
    assert settings.outbox_otlp_endpoint == ""
    with pytest.raises(ValueError, match="NE_WORKER_OUTBOX_OTLP_ENDPOINT"):
        _build_outbox_handler(settings)

    # With the in-cluster endpoint set, the real exporter-backed handler builds.
    monkeypatch.setenv("NE_WORKER_OUTBOX_OTLP_ENDPOINT", "http://otel-collector:4318")
    handler = _build_outbox_handler(WorkerSettings())
    assert isinstance(handler, OutboxOtelLogHandler)
