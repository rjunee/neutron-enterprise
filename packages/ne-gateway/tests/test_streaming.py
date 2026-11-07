# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Streaming — sync-returning AsyncIterator, deltas, fallback, and cost."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

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


def _request(task: str) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task)


async def _drain(gen: object) -> list[CompletionChunk]:
    out: list[CompletionChunk] = []
    async for chunk in gen:  # type: ignore[attr-defined]
        out.append(chunk)
    return out


def test_stream_yields_deltas(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    chunks = asyncio.run(_drain(gw.stream(_request("draft"))))
    assert "".join(c.delta for c in chunks) == "hello world"
    assert all(c.model_id == "primary" for c in chunks)


def test_stream_is_sync_returning_async_iterator(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="hi")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    assert not asyncio.iscoroutinefunction(gw.stream)
    obj = gw.stream(_request("draft"))
    assert hasattr(obj, "__aiter__")


def test_stream_emits_cost_event_after_exhaustion(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="abc")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    asyncio.run(_drain(gw.stream(_request("draft"))))
    assert len(sink.events) == 1
    event = sink.events[0]
    # Mock streams do not surface usage -> None-guarded token fields.
    assert event.input_tokens is None
    assert event.output_tokens is None
    assert event.model == "gpt-3.5-turbo"


def test_stream_fallback_before_first_chunk(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("no-stream")),
            make_model("backup", mock_response="fallback-stream"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    chunks = asyncio.run(_drain(gw.stream(_request("draft"))))
    assert "".join(c.delta for c in chunks) == "fallback-stream"
    assert all(c.model_id == "backup" for c in chunks)


def test_stream_handles_usage_only_final_chunk(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal usage-only chunk (empty ``choices``, e.g. stream_options
    include_usage) must not crash the drain loop, and its usage must feed the
    post-exhaustion cost event."""
    from types import SimpleNamespace

    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_stream() -> object:
        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
            yield SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=7),
            )

        return gen()

    async def fake_acompletion(*, model: str, messages: object, **kwargs: object) -> object:
        return await fake_stream()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    chunks = asyncio.run(_drain(gw.stream(_request("draft"))))
    assert [c.delta for c in chunks] == ["hi"]
    assert len(sink.events) == 1
    assert sink.events[0].input_tokens == 3
    assert sink.events[0].output_tokens == 7


# -- NE-CKC3 streaming audit gap: abnormal terminations still finalize --------


def test_stream_midstream_error_emits_cost_and_error_audit(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider failure mid-drain (after data has egressed) must still emit
    exactly one CostEvent + one error AuditEvent, and re-raise the original
    exception unwrapped."""
    settings = make_settings(
        [make_model("primary")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def fake_acompletion(*, model: str, messages: object, **kwargs: object) -> object:
        async def gen() -> AsyncIterator[object]:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
            raise RuntimeError("provider exploded mid-stream")

        return gen()

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    with pytest.raises(RuntimeError):
        asyncio.run(_drain(gw.stream(_request("draft"))))

    assert len(sink.events) == 1
    # Redaction + terminal stream error + one actual-attempt observation.
    assert len(audit_sink.events) == 3
    event = next(
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == "ne.gateway.stream"
    )
    assert event.outcome == AuditOutcome.error
    assert event.detail is not None
    assert "mid-stream failure" in event.detail


def test_stream_consumer_abandon_emits_cost_and_interrupted_audit(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """A consumer that abandons the stream after one chunk (aclose() → the async
    generator is thrown GeneratorExit) must still finalize: one CostEvent + one
    error AuditEvent whose detail marks the consumer interruption."""
    settings = make_settings(
        [make_model("primary", mock_response="hello world")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(settings, sink, audit_sink=audit_sink, redactor=PASSTHROUGH_REDACTOR)

    async def consume_one_then_abandon() -> None:
        agen = gw.stream(_request("draft"))
        await agen.__anext__()  # type: ignore[attr-defined]
        await agen.aclose()  # type: ignore[attr-defined]

    asyncio.run(consume_one_then_abandon())

    assert len(sink.events) == 1
    # NE-162: one redact.pre_egress success event ahead of the interrupted-stream event.
    assert len(audit_sink.events) == 2
    event = next(
        e
        for e in audit_sink.events
        if isinstance(e, AuditEvent) and e.action == "ne.gateway.stream"
    )
    assert event.outcome == AuditOutcome.error
    assert event.detail is not None
    assert "interrupted by consumer" in event.detail


def test_stream_guard_exception_propagates_unwrapped(
    sink: RecordingCostSink, audit_sink: RecordingAuditSink, frozen_config: str
) -> None:
    """An egress-guard denial on the stream path propagates UNWRAPPED (not a
    ModelCallFailedError) with zero cost/audit events — the audit-#9 fail-closed
    proof, mirroring the existing complete()-side guard test."""
    settings = make_settings(
        [make_model("primary", mock_response="x")],
        {"draft": TaskRoute(model="primary")},
    )

    def guard(_model: object) -> None:
        raise RuntimeError("egress denied")

    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=guard,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
    )
    with pytest.raises(RuntimeError):
        asyncio.run(_drain(gw.stream(_request("draft"))))

    assert sink.events == []
    assert audit_sink.events == []


def test_stream_all_fail_raises(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom-1")),
            make_model("backup", mock_response=Exception("boom-2")),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(ModelCallFailedError):
        asyncio.run(_drain(gw.stream(_request("draft"))))
    assert sink.events == []
