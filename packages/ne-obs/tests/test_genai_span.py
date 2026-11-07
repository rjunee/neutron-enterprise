# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""genai_span(): span kind/attributes on exported spans, and the error path."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import StatusCode

from ne.contracts import freeze_config_hash, reset_config_hash_for_testing
from ne.contracts.config import NeSettings
from ne.obs import genai_span
from ne.obs.genai import ATTR_NE_CONFIG_HASH, ATTR_OPERATION_NAME, ATTR_REQUEST_MODEL


class _DemoSettings(NeSettings):
    demo: int = 5


@pytest.fixture
def frozen_config() -> Iterator[str]:
    reset_config_hash_for_testing()
    digest = freeze_config_hash(_DemoSettings())
    try:
        yield digest
    finally:
        reset_config_hash_for_testing()


@pytest.fixture
def exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    return provider, exp


def test_span_kind_and_attributes_land_on_export(
    frozen_config: str, exporter: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    provider, exp = exporter
    tracer = provider.get_tracer("test")
    with genai_span(tracer, "chat.completion", operation="chat", request_model="claude-opus"):
        pass
    spans = exp.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "chat.completion"
    assert span.kind is SpanKind.CLIENT
    assert span.attributes is not None
    assert span.attributes[ATTR_OPERATION_NAME] == "chat"
    assert span.attributes[ATTR_REQUEST_MODEL] == "claude-opus"
    assert span.attributes[ATTR_NE_CONFIG_HASH] == frozen_config


def test_error_path_records_exception_and_sets_error_status(
    frozen_config: str, exporter: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    provider, exp = exporter
    tracer = provider.get_tracer("test")

    with (
        pytest.raises(RuntimeError, match="model exploded"),
        genai_span(tracer, "chat.completion", operation="chat"),
    ):
        raise RuntimeError("model exploded")

    spans = exp.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code is StatusCode.ERROR
    event_names = [e.name for e in span.events]
    assert "exception" in event_names
