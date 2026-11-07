# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FIX-SYNC-INVOKERS (P2-21): the real outbox → OTel-logs exporter.

Closes the P2-21 gap from the 2026-07-10 bulletproofing review: the §7 day-one SIEM
export was a log-only stub (count + discard) and the outbox had no consumer, so
audit/cost/approval events reached NO external consumer. This module drains the
outbox to OTel LOG records shipped over OTLP/HTTP to the IN-CLUSTER otel-collector —
the C2-sanctioned sink the customer configures to forward to their SIEM. The platform
itself NEVER opens external egress: :func:`build_otlp_log_record_exporter` runs
``ne.net.validate_in_vpc_endpoint`` at construction, so a worker pointed at an
external SIEM URL fails BOOT loud (fail-closed).

DESIGN DECISION — synchronous DIRECT export, deliberately NO ``LoggerProvider`` /
``BatchLogRecordProcessor``: an async batch processor would let the outbox drain ack a
window BEFORE the records were delivered, breaking the outbox's at-least-once
contract. :class:`OutboxOtelLogHandler` exports synchronously and RAISES on a non-
SUCCESS result, so the drain leaves the cursor unmoved and the window redelivers next
tick (fail-open, at-least-once).

The 1.43 log API names (``ReadableLogRecord`` / ``InMemoryLogRecordExporter`` /
``LogRecordExportResult``) live under ``opentelemetry.sdk._logs`` — the underscore
package is the sanctioned import path in otel-python (the logs SDK has not graduated
out of ``_logs``), hence the narrowly-targeted private-import ignores below.
``ReadableLogRecord`` (NOT the mutable ``ReadWriteLogRecord`` used inside batch
processors) is the exact type ``LogRecordExporter.export(Sequence[ReadableLogRecord])``
accepts.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import ReadableLogRecord  # pyright: ignore[reportPrivateImportUsage]
from opentelemetry.sdk._logs.export import (  # pyright: ignore[reportPrivateImportUsage]
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.resources import Resource

from ne.net import validate_in_vpc_endpoint

if TYPE_CHECKING:
    from ne.events.outbox import OutboxRecord

__all__ = [
    "OutboxExportError",
    "OutboxOtelLogHandler",
    "build_otlp_log_record_exporter",
]


class OutboxExportError(Exception):
    """A batch of outbox records failed to export to the OTLP collector."""


def build_otlp_log_record_exporter(endpoint: str) -> LogRecordExporter:
    """Build the OTLP/HTTP log exporter for the IN-CLUSTER collector ``endpoint``.

    C2 GUARDIAN: ``ne.net.validate_in_vpc_endpoint`` runs FIRST — it accepts a dotless
    compose/service name (``otel-collector``), a private/loopback IP, or a
    ``.internal`` host, and REJECTS any external SIEM host. So the only sanctioned sink
    is the in-cluster collector the customer forwards FROM; a worker pointed at an
    external endpoint fails BOOT loud. The OTLPLogExporter import is lazy (the
    otlp-proto-http dep is only needed when the drain is actually enabled).

    The ``/v1/logs`` path suffix is required — OTLPLogExporter uses the explicit
    ``endpoint`` verbatim (no auto-suffixing when an endpoint is passed).
    """
    validated = validate_in_vpc_endpoint(endpoint)
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # pyright: ignore[reportMissingImports]
        OTLPLogExporter,
    )

    return OTLPLogExporter(endpoint=f"{validated}/v1/logs")


class OutboxOtelLogHandler:
    """A ``drain_once`` handler that exports outbox records as OTel log records.

    Synchronous DIRECT export (see the module docstring): each :class:`OutboxRecord`
    becomes one OTel ``LogRecord`` (body = the event payload; the outbox coordinates
    ride as ``ne.*`` attributes), and the whole batch is exported in one call. A
    non-SUCCESS result raises :class:`OutboxExportError` so the drain does NOT ack the
    window (at-least-once redelivery).
    """

    def __init__(self, exporter: LogRecordExporter, *, service_name: str = "ne-worker") -> None:
        self._exporter = exporter
        self._resource = Resource.create({"service.name": service_name})

    def __call__(self, records: list[OutboxRecord]) -> None:
        if not records:
            return
        batch: list[ReadableLogRecord] = []
        for r in records:
            attributes: dict[str, str | int] = {
                "ne.event_type": r.event_type,
                "ne.event_id": r.event_id,
                "ne.actor": r.actor,
                "ne.config_hash": r.config_hash,
                "ne.outbox_id": r.id,
            }
            if r.kpi_tag:
                attributes["ne.kpi_tag"] = r.kpi_tag
            log_record = LogRecord(
                timestamp=int(r.occurred_at.timestamp() * 1e9),
                observed_timestamp=time.time_ns(),
                severity_number=SeverityNumber.INFO,
                severity_text="INFO",
                body=r.payload,
                attributes=attributes,
            )
            batch.append(ReadableLogRecord(log_record=log_record, resource=self._resource))
        if self._exporter.export(batch) is not LogRecordExportResult.SUCCESS:
            raise OutboxExportError(
                f"OTLP export of {len(batch)} outbox record(s) returned a non-SUCCESS result"
            )
