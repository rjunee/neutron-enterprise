# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Timestamped model-health observations emitted only for provider attempts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import (
    ChatMessage,
    CompletionChunk,
    CompletionRequest,
    ContextBudgetExceededError,
    Lane,
)
from ne.gateway import FrontierProviderError, LiteLLMGateway, ModelCallFailedError, TaskRoute
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_ACTION = "ne.gateway.model.observed"


def _private(name: str, *, mock_response: Any = "ok", context_window: int = 32768) -> Any:
    return make_model(
        name,
        mock_response=mock_response,
        lane=Lane.PRIVATE_VLLM,
        litellm_model=f"hosted_vllm/{name}",
        provider="hosted_vllm",
        api_base="http://vllm.internal:8000/v1",
        context_window=context_window,
    )


def _request(
    *,
    requested_model: str = "primary",
    content: str = "hello",
) -> CompletionRequest:
    return CompletionRequest(
        messages=(ChatMessage(role="user", content=content),),
        task="agent",
        requested_model=requested_model,
        trace_id="trace-model-15",
    )


def _observations(events: Sequence[object]) -> list[AuditEvent]:
    return [event for event in events if isinstance(event, AuditEvent) and event.action == _ACTION]


async def _drain(stream: AsyncIterator[CompletionChunk]) -> list[CompletionChunk]:
    return [chunk async for chunk in stream]


def test_complete_observes_failed_requested_primary_then_successful_actual_fallback(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [
            _private("primary", mock_response=RuntimeError("down")),
            _private("fallback", mock_response="recovered"),
        ],
        {"agent": TaskRoute(model="primary", fallbacks=("fallback",))},
        num_retries=0,
    )
    gateway = LiteLLMGateway(settings, sink, audit_sink=audit_sink)

    result = asyncio.run(gateway.complete(_request()))

    assert result.model_id == "fallback"
    observations = _observations(audit_sink.events)
    assert [(event.provenance["model_name"], event.outcome) for event in observations] == [
        ("primary", AuditOutcome.error),
        ("fallback", AuditOutcome.success),
    ]
    assert all(event.provenance["requested_model"] == "primary" for event in observations)
    assert all(event.provenance["operation"] == "complete" for event in observations)
    assert all(event.trace_id == "trace-model-15" for event in observations)
    assert all(event.occurred_at.tzinfo is not None for event in observations)
    assert observations[0].occurred_at <= observations[1].occurred_at


def test_unusable_complete_response_is_costed_observed_and_falls_back(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    gateway = LiteLLMGateway(
        make_settings(
            [_private("primary"), _private("fallback")],
            {"agent": TaskRoute(model="primary", fallbacks=("fallback",))},
            num_retries=0,
        ),
        sink,
        audit_sink=audit_sink,
    )
    calls: list[str] = []

    async def malformed_then_fallback(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        if model == "primary":
            return SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
                _hidden_params={"response_cost": 0.25},
                model="provider/primary",
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="recovered"))],
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
            _hidden_params={"response_cost": 0.5},
            model="provider/fallback",
        )

    monkeypatch.setattr(gateway._router, "acompletion", malformed_then_fallback)

    result = asyncio.run(gateway.complete(_request()))

    assert result.model_id == "fallback"
    assert result.text == "recovered"
    assert calls == ["primary", "fallback"]
    # Both transports happened and remain billable, including the malformed
    # response that could not satisfy the Completion contract.
    assert [(event.model, event.cost_usd) for event in sink.events] == [
        ("hosted_vllm/primary", 0.25),
        ("hosted_vllm/fallback", 0.5),
    ]
    observations = _observations(audit_sink.events)
    assert [(event.provenance["model_name"], event.outcome) for event in observations] == [
        ("primary", AuditOutcome.error),
        ("fallback", AuditOutcome.success),
    ]
    completion = next(
        event
        for event in audit_sink.events
        if isinstance(event, AuditEvent)
        and event.action == "ne.gateway.complete"
        and event.outcome == AuditOutcome.success
    )
    assert completion.provenance["route_position"] == "fallback"
    assert completion.provenance["fallback_from"] == "primary"


def test_frontier_unusable_response_error_is_class_only_and_observed(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    sentinel = "SENTINEL-PROMPT-123-45-6789"
    gateway = LiteLLMGateway(
        make_settings([make_model("frontier")], {"agent": TaskRoute(model="frontier")}),
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
    )

    class UnusableResponse:
        def __init__(self) -> None:
            self.usage = SimpleNamespace(prompt_tokens=2, completion_tokens=1)
            self._hidden_params = {"response_cost": 0.25}
            self.model = "provider/frontier"

        @property
        def choices(self) -> list[Any]:
            raise RuntimeError(sentinel)

    async def unusable(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return UnusableResponse()

    monkeypatch.setattr(gateway._router, "acompletion", unusable)

    with pytest.raises(ModelCallFailedError) as excinfo:
        asyncio.run(gateway.complete(_request(requested_model="frontier")))

    assert len(sink.events) == 1
    [(model_name, attempt_error)] = excinfo.value.attempts
    assert model_name == "frontier"
    assert isinstance(attempt_error, FrontierProviderError)
    assert str(attempt_error) == "RuntimeError"
    assert sentinel not in str(excinfo.value)
    observations = _observations(audit_sink.events)
    assert [(event.provenance["model_name"], event.outcome) for event in observations] == [
        ("frontier", AuditOutcome.error)
    ]
    assert all(
        sentinel not in (event.detail or "")
        for event in audit_sink.events
        if isinstance(event, AuditEvent)
    )


def test_stream_observes_establish_failure_then_clean_fallback_exhaustion(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [
            _private("primary", mock_response=RuntimeError("down")),
            _private("fallback", mock_response="stream recovered"),
        ],
        {"agent": TaskRoute(model="primary", fallbacks=("fallback",))},
        num_retries=0,
    )
    gateway = LiteLLMGateway(settings, sink, audit_sink=audit_sink)

    chunks = asyncio.run(_drain(gateway.stream(_request())))

    assert "".join(chunk.delta for chunk in chunks) == "stream recovered"
    observations = _observations(audit_sink.events)
    assert [(event.provenance["model_name"], event.outcome) for event in observations] == [
        ("primary", AuditOutcome.error),
        ("fallback", AuditOutcome.success),
    ]
    assert all(event.provenance["operation"] == "stream" for event in observations)


def test_reasoning_exhaustion_is_an_error_observation_of_the_attempted_model(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    gateway = LiteLLMGateway(
        make_settings(
            [_private("primary")],
            {"agent": TaskRoute(model="primary")},
            num_retries=0,
        ),
        sink,
        audit_sink=audit_sink,
    )

    async def reasoning_only(*, model: str, messages: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, reasoning_content="thinking"),
                    finish_reason="length",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=16),
            _hidden_params={"response_cost": 0.0},
            model=model,
        )

    monkeypatch.setattr(gateway._router, "acompletion", reasoning_only)

    with pytest.raises(ModelCallFailedError):
        asyncio.run(gateway.complete(_request()))

    observations = _observations(audit_sink.events)
    assert len(observations) == 1
    assert observations[0].outcome == AuditOutcome.error
    assert observations[0].provenance["model_name"] == "primary"


def test_budget_skip_guard_denial_and_redaction_denial_are_not_model_observations(
    sink: RecordingCostSink,
    frozen_config: str,
) -> None:
    budget_audit = RecordingAuditSink()
    budget_gateway = LiteLLMGateway(
        make_settings(
            [_private("primary", context_window=2048)],
            {"agent": TaskRoute(model="primary")},
        ),
        sink,
        audit_sink=budget_audit,
    )
    with pytest.raises(ContextBudgetExceededError):
        asyncio.run(budget_gateway.complete(_request(content="x" * 2000)))
    assert _observations(budget_audit.events) == []

    guard_audit = RecordingAuditSink()

    def deny(_model: object) -> None:
        raise RuntimeError("egress denied")

    guard_gateway = LiteLLMGateway(
        make_settings([_private("primary")], {"agent": TaskRoute(model="primary")}),
        sink,
        audit_sink=guard_audit,
        egress_guard=deny,
    )
    with pytest.raises(RuntimeError, match="egress denied"):
        asyncio.run(guard_gateway.complete(_request()))
    assert _observations(guard_audit.events) == []

    class RaisingRedactor:
        def redact(self, text: str, *, categories: Sequence[str] | None = None) -> Any:
            raise RuntimeError("redaction denied")

    redaction_audit = RecordingAuditSink()
    redaction_gateway = LiteLLMGateway(
        make_settings([make_model("primary")], {"agent": TaskRoute(model="primary")}),
        sink,
        audit_sink=redaction_audit,
        redactor=RaisingRedactor(),
    )
    with pytest.raises(RuntimeError, match="redaction denied"):
        asyncio.run(redaction_gateway.complete(_request()))
    assert _observations(redaction_audit.events) == []


def test_midstream_provider_failure_is_observed_but_consumer_abandon_and_cancel_are_not(
    sink: RecordingCostSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    async def exercise(mode: str) -> list[AuditEvent]:
        audit = RecordingAuditSink()
        gateway = LiteLLMGateway(
            make_settings([_private("primary")], {"agent": TaskRoute(model="primary")}),
            sink,
            audit_sink=audit,
        )

        async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
            async def chunks() -> AsyncIterator[object]:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="one"))]
                )
                if mode == "failure":
                    raise RuntimeError("provider failed mid-drain")
                if mode == "cancel":
                    raise asyncio.CancelledError
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="two"))]
                )

            return chunks()

        monkeypatch.setattr(gateway._router, "acompletion", fake_acompletion)
        if mode == "abandon":
            stream = gateway.stream(_request())
            await stream.__anext__()
            await stream.aclose()  # type: ignore[attr-defined]
        elif mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await _drain(gateway.stream(_request()))
        else:
            with pytest.raises(RuntimeError, match="provider failed"):
                await _drain(gateway.stream(_request()))
        return _observations(audit.events)

    failed = asyncio.run(exercise("failure"))
    abandoned = asyncio.run(exercise("abandon"))
    cancelled = asyncio.run(exercise("cancel"))

    assert [(event.provenance["model_name"], event.outcome) for event in failed] == [
        ("primary", AuditOutcome.error)
    ]
    assert abandoned == []
    assert cancelled == []


class _FailObservationSink(RecordingAuditSink):
    async def append(self, event: Any) -> None:
        if isinstance(event, AuditEvent) and event.action == _ACTION:
            raise RuntimeError("observation audit unavailable")
        await super().append(event)


def test_unrecordable_failed_attempt_stops_before_fallback_transport(
    sink: RecordingCostSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    audit = _FailObservationSink()
    gateway = LiteLLMGateway(
        make_settings(
            [_private("primary"), _private("fallback")],
            {"agent": TaskRoute(model="primary", fallbacks=("fallback",))},
            num_retries=0,
        ),
        sink,
        audit_sink=audit,
    )
    calls: list[str] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        if model == "primary":
            raise RuntimeError("provider down")
        raise AssertionError("fallback must not run without a recorded failed attempt")

    monkeypatch.setattr(gateway._router, "acompletion", fake_acompletion)

    with pytest.raises(RuntimeError, match="observation audit unavailable"):
        asyncio.run(gateway.complete(_request()))

    assert calls == ["primary"]
