# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-014 per-call instrumentation: OTel span + CostEvent + AuditEvent, all
sharing one frozen ``config_hash``; failure-path auditing; the None-default
audit sink no-op; and the real PostgresAuditSink round trip."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
import sqlalchemy as sa
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id

from ne.audit import PostgresAuditSink, ensure_audit_schema
from ne.contracts import ChatMessage, CompletionChunk, CompletionRequest, Lane
from ne.gateway import (
    ATTR_NE_LATENCY_MS,
    ATTR_NE_TASK,
    LiteLLMGateway,
    ModelCallFailedError,
    ModelDef,
    TaskRoute,
    annotate_span_after_call,
)
from ne.obs import (
    ATTR_INPUT_TOKENS,
    ATTR_NE_CONFIG_HASH,
    ATTR_NE_COST_USD,
    ATTR_OUTPUT_TOKENS,
    ATTR_REQUEST_MODEL,
    ATTR_RESPONSE_MODEL,
)
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


def _find_event(events: Sequence[object], action: str) -> AuditEvent:
    """Find the (narrowed) AuditEvent with the given action. NE-162 adds a
    `redact.pre_egress` event ahead of every frontier attempt's own
    ne.gateway.complete/stream event, so tests that used to index events[0]
    now filter by action instead."""
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
    """Poke a fresh in-memory-exporter-backed tracer onto the gateway without
    touching OTel's process-global TracerProvider (which cannot be safely reset
    mid-suite). Mirrors the private-attribute-poke pattern already used in
    test_fallbacks.py (``gw._router.num_retries``)."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    gw._tracer = provider.get_tracer("test")  # pyright: ignore[reportAttributeAccessIssue]
    return exporter


def test_complete_success_emits_span_cost_and_audit_with_matching_config_hash(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    exporter = _install_exporter(gw)

    asyncio.run(gw.complete(_request("draft")))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs[ATTR_NE_TASK] == "draft"
    assert ATTR_NE_LATENCY_MS in attrs
    latency = attrs[ATTR_NE_LATENCY_MS]
    assert isinstance(latency, (int, float))
    assert latency >= 0
    assert attrs[ATTR_REQUEST_MODEL] == "gpt-3.5-turbo"

    assert len(sink.events) == 1
    # Redaction + completion + one actual-attempt observation.
    assert len(audit_sink.events) == 3
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.outcome == AuditOutcome.success
    assert audit_event.action == "ne.gateway.complete"

    # The three-way config_hash alignment this unit mandates.
    assert attrs[ATTR_NE_CONFIG_HASH] == sink.events[0].config_hash
    assert sink.events[0].config_hash == audit_event.config_hash
    assert audit_event.config_hash == frozen_config


def test_stream_success_emits_span_cost_and_audit_with_matching_config_hash(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    exporter = _install_exporter(gw)

    asyncio.run(_drain(gw.stream(_request("draft"))))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs[ATTR_NE_TASK] == "draft"
    assert ATTR_NE_LATENCY_MS in attrs
    # Streaming establish-phase span cannot know token usage (documented limit).
    assert ATTR_INPUT_TOKENS not in attrs
    assert ATTR_OUTPUT_TOKENS not in attrs

    assert len(sink.events) == 1
    # Redaction + stream + one actual-attempt observation.
    assert len(audit_sink.events) == 3
    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert audit_event.action == "ne.gateway.stream"
    assert audit_event.outcome == AuditOutcome.success

    assert attrs[ATTR_NE_CONFIG_HASH] == sink.events[0].config_hash
    assert sink.events[0].config_hash == audit_event.config_hash
    assert audit_event.config_hash == frozen_config


def test_complete_all_candidates_fail_emits_error_audit_event_no_cost_event(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(ModelCallFailedError):
        asyncio.run(gw.complete(_request("draft")))

    assert sink.events == []
    # Each frontier attempt emits redaction + failed-attempt observation,
    # followed by the terminal all-candidates-failed event.
    assert len(audit_sink.events) == 5
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.outcome == AuditOutcome.error
    assert audit_event.action == "ne.gateway.complete"


def test_stream_all_candidates_fail_emits_error_audit_event_no_cost_event(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(ModelCallFailedError):
        asyncio.run(_drain(gw.stream(_request("draft"))))

    assert sink.events == []
    # Each frontier attempt emits redaction + failed-attempt observation,
    # followed by the terminal all-candidates-failed event.
    assert len(audit_sink.events) == 5
    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert audit_event.outcome == AuditOutcome.error
    assert audit_event.action == "ne.gateway.stream"


def test_default_audit_sink_is_noop_on_success(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(
        settings, sink, redactor=PASSTHROUGH_REDACTOR
    )  # no audit_sink kwarg -> defaults None
    result = asyncio.run(gw.complete(_request("draft")))
    assert result.model_id == "primary"
    assert len(sink.events) == 1


def test_complete_appends_through_real_postgres_audit_sink(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    engine = sa.create_engine("sqlite://")
    ensure_audit_schema(engine)
    audit_sink = PostgresAuditSink(engine)

    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    asyncio.run(gw.complete(_request("draft")))

    verification = asyncio.run(audit_sink.verify())
    assert verification.valid is True
    # Redaction + completion + one actual-attempt observation.
    assert verification.checked == 3


# -- NE-CKC3 P3: trace_id correlation on success paths ------------------------


def test_complete_stamps_trace_id_matching_span(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    exporter = _install_exporter(gw)

    asyncio.run(gw.complete(_request("draft")))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span_context = spans[0].get_span_context()
    assert span_context is not None
    expected = format_trace_id(span_context.trace_id)
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert sink.events[0].trace_id == expected
    assert audit_event.trace_id == expected


def test_stream_stamps_trace_id_matching_span(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)
    exporter = _install_exporter(gw)

    asyncio.run(_drain(gw.stream(_request("draft"))))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span_context = spans[0].get_span_context()
    assert span_context is not None
    expected = format_trace_id(span_context.trace_id)
    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert sink.events[0].trace_id == expected
    assert audit_event.trace_id == expected


# -- NE-CKC3 audit #12: failure-path audit details never leak credentials -----

_LEAK_URL = "https://admin:s3cr3tpass@vllm.internal/v1"


def _leaky_vllm_model(name: str = "primary") -> ModelDef:
    """A PRIVATE_VLLM model: NE-162 sanitizes FRONTIER_API provider errors down
    to the exception class name only (see test_frontier_provider_error_reduced_to
    _class_name in test_redaction_seam.py), which supersedes the older
    ``redact_url_for_logs`` credential-masking this test proves — so this
    lane-specific (still fully exception-text-preserving) NE-012 guarantee is
    now exercised on the private-vllm lane instead."""
    return ModelDef(
        name=name,
        litellm_model="hosted_vllm/x",
        provider="vllm",
        lane=Lane.PRIVATE_VLLM,
        api_base="http://vllm.internal/v1",
        extra_params={"api_key": "test"},
        context_window=32768,
    )


def test_complete_failure_audit_detail_never_leaks_credentialed_url(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [_leaky_vllm_model()],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink)

    async def fake_acompletion(*, model: str, messages: object, **kwargs: object) -> object:
        raise RuntimeError(f"connect to {_LEAK_URL} refused")

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gw.complete(_request("draft")))

    assert "s3cr3tpass" not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    audit_event = _find_event(audit_sink.events, "ne.gateway.complete")
    assert audit_event.detail is not None
    assert "s3cr3tpass" not in audit_event.detail
    assert "***" in audit_event.detail


def test_stream_establish_failure_audit_detail_never_leaks_credentialed_url(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        [_leaky_vllm_model()],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink)

    async def fake_acompletion(*, model: str, messages: object, **kwargs: object) -> object:
        raise RuntimeError(f"connect to {_LEAK_URL} refused")

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(_drain(gw.stream(_request("draft"))))

    assert "s3cr3tpass" not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    audit_event = _find_event(audit_sink.events, "ne.gateway.stream")
    assert audit_event.detail is not None
    assert "s3cr3tpass" not in audit_event.detail
    assert "***" in audit_event.detail


class _FakeSpan:
    """Minimal span stand-in recording ``set_attribute`` calls."""

    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value


def test_annotate_span_after_call_omits_none_fields() -> None:
    span = _FakeSpan()
    annotate_span_after_call(
        span,  # type: ignore[arg-type]
        task="draft",
        latency_ms=1.5,
    )
    assert span.attrs[ATTR_NE_TASK] == "draft"
    assert span.attrs[ATTR_NE_LATENCY_MS] == 1.5
    assert ATTR_RESPONSE_MODEL not in span.attrs
    assert ATTR_INPUT_TOKENS not in span.attrs
    assert ATTR_OUTPUT_TOKENS not in span.attrs
    assert ATTR_NE_COST_USD not in span.attrs
