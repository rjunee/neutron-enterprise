# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-166: request.trace_id is the DATA carrier stamped onto CostEvent+AuditEvent,
taking precedence over the OTel-span enrichment fallback (``_current_trace_id``).

Existing span-fallback behavior (no explicit ``request.trace_id``) is proven
UNCHANGED by ``test_instrumentation.py::test_complete_stamps_trace_id_matching_span``
/ ``::test_stream_stamps_trace_id_matching_span``, which this module does not
touch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id

from ne.contracts import ChatMessage, CompletionChunk, CompletionRequest
from ne.gateway import LiteLLMGateway, ModelCallFailedError, TaskRoute
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str, *, trace_id: str | None = None) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task, trace_id=trace_id)


def _find_event(events: Sequence[object], action: str) -> AuditEvent:
    """NE-162 adds a `redact.pre_egress` event ahead of every frontier
    attempt's own ne.gateway.complete/stream event; filter by action instead
    of indexing events[0]."""
    for event in events:
        if isinstance(event, AuditEvent) and event.action == action:
            return event
    raise AssertionError(f"no AuditEvent with action={action!r} in {events!r}")


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def _install_exporter(gw: LiteLLMGateway) -> InMemorySpanExporter:
    """Same private-attribute-poke pattern as test_instrumentation.py."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    gw._tracer = provider.get_tracer("test")  # pyright: ignore[reportAttributeAccessIssue]
    return exporter


def test_complete_stamps_explicit_request_trace_id(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(gw.complete(_request("draft", trace_id="run-abc")))

    assert len(sink.events) == 1
    assert sink.events[0].trace_id == "run-abc"
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.trace_id == "run-abc"


def test_stream_stamps_explicit_request_trace_id(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    asyncio.run(_drain(gw.stream(_request("draft", trace_id="run-abc"))))

    assert len(sink.events) == 1
    assert sink.events[0].trace_id == "run-abc"
    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert audit_event.trace_id == "run-abc"


def test_explicit_empty_trace_id_is_preserved_not_overwritten(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """Codex P2 regression: an EXPLICIT empty-string trace_id must win over the
    span fallback (``is not None``, not truthiness) — the contract is "an explicit
    id always wins". A live span is installed to prove "" is not silently replaced."""
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    _install_exporter(gw)  # a real span is active; "" must still win over it

    asyncio.run(gw.complete(_request("draft", trace_id="")))

    assert sink.events[0].trace_id == ""
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.trace_id == ""


def test_explicit_trace_id_wins_over_live_span(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    exporter = _install_exporter(gw)

    asyncio.run(gw.complete(_request("draft", trace_id="explicit-wins")))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span_context = spans[0].get_span_context()
    assert span_context is not None
    span_trace_id = format_trace_id(span_context.trace_id)
    assert span_trace_id != "explicit-wins"

    assert sink.events[0].trace_id == "explicit-wins"
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.trace_id == "explicit-wins"


def test_failure_path_error_audit_carries_request_trace_id(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response=RuntimeError("boom"))],
        {"draft": TaskRoute(model="primary")},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    with pytest.raises(ModelCallFailedError):
        asyncio.run(gw.complete(_request("draft", trace_id="run-abc")))

    assert sink.events == []
    # Redaction + failed-attempt observation + final failure event.
    assert len(audit_sink.events) == 3
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.outcome == AuditOutcome.error
    assert audit_event.trace_id == "run-abc"
