# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-15 exact per-call model selection over the governed BD-13 route."""

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
from ne.gateway import (
    AirGapViolationError,
    EgressPolicy,
    EgressSettings,
    LaneMismatchError,
    LiteLLMGateway,
    ModelCallFailedError,
    TaskRoute,
    UnknownModelIdError,
)
from ne.schema import AuditEvent, AuditOutcome

from .conftest import (
    PASSTHROUGH_REDACTOR,
    RecordingAuditSink,
    RecordingCostSink,
    make_model,
    make_settings,
)

_MESSAGES = (ChatMessage(role="user", content="hello"),)


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
    requested_model: str | None,
    lane: Lane | None = None,
    task: str = "agent",
) -> CompletionRequest:
    return CompletionRequest(
        messages=_MESSAGES,
        task=task,
        requested_model=requested_model,
        lane=lane,
        trace_id="turn-15",
    )


async def _drain(stream: AsyncIterator[CompletionChunk]) -> list[CompletionChunk]:
    return [chunk async for chunk in stream]


def _events(events: Sequence[object], action: str) -> list[AuditEvent]:
    return [event for event in events if isinstance(event, AuditEvent) and event.action == action]


def test_requested_primary_outranks_live_route_and_never_uses_boot_fallback(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    policy = "agent.meridian-analyst.v2"
    settings = make_settings(
        [_private("boot"), _private("live"), _private("chosen"), _private("fallback")],
        {
            policy: TaskRoute(
                model="boot",
                fallbacks=("fallback",),
                params={"top_p": 0.5},
            )
        },
        num_retries=0,
    )
    reads = 0

    def routes() -> dict[str, TaskRoute]:
        nonlocal reads
        reads += 1
        # The live provider may replace only the primary. Its params/fallbacks
        # are hostile input that `_routes()` must continue to discard.
        return {
            policy: TaskRoute(
                model="live",
                fallbacks=(),
                params={"top_p": 0.99},
            )
        }

    gateway = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
        route_provider=routes,
    )
    calls: list[tuple[str, float]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append((model, kwargs["top_p"]))
        raise RuntimeError("chosen unavailable")

    monkeypatch.setattr(gateway._router, "acompletion", fake_acompletion)

    with pytest.raises(ModelCallFailedError):
        asyncio.run(gateway.complete(_request(requested_model="chosen", task=policy)))

    assert calls == [("chosen", 0.5)]
    assert reads == 1, "the BD-13 route and its boot params must be snapshotted once"
    failure = next(
        event
        for event in _events(audit_sink.events, "ne.gateway.complete")
        if event.outcome == AuditOutcome.error
    )
    assert failure.provenance["requested_model"] == "chosen"


def test_legacy_none_still_uses_the_live_route_and_omits_requested_provenance(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [_private("boot"), _private("live")],
        {"agent": TaskRoute(model="boot")},
    )
    gateway = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
        route_provider=lambda: {"agent": TaskRoute(model="live")},
    )

    result = asyncio.run(gateway.complete(_request(requested_model=None)))

    assert result.model_id == "live"
    relevant = [
        event
        for event in audit_sink.events
        if isinstance(event, AuditEvent)
        and event.action in {"ne.gateway.complete", "ne.gateway.model.observed"}
    ]
    assert relevant
    assert all("requested_model" not in event.provenance for event in relevant)


@pytest.mark.parametrize("alias", ["not-registered", ""])
def test_unregistered_or_empty_requested_alias_fails_loud_before_transport(
    alias: str,
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    gateway = LiteLLMGateway(
        make_settings([_private("boot")], {"agent": TaskRoute(model="boot")}),
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
    )
    calls: list[str] = []

    async def never_called(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        raise AssertionError("unregistered selection must not reach transport")

    monkeypatch.setattr(gateway._router, "acompletion", never_called)

    with pytest.raises(UnknownModelIdError) as excinfo:
        asyncio.run(gateway.complete(_request(requested_model=alias)))

    assert excinfo.value.model_id == alias
    assert calls == []
    assert sink.events == []
    assert audit_sink.events == []


def test_explicit_lane_mismatch_refuses_before_a_matching_fallback_can_serve(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [make_model("frontier"), _private("private-fallback")],
        {"agent": TaskRoute(model="frontier", fallbacks=("private-fallback",))},
    )
    gateway = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
    )
    calls: list[str] = []

    async def never_called(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        raise AssertionError("lane mismatch must fail before fallback filtering")

    monkeypatch.setattr(gateway._router, "acompletion", never_called)

    with pytest.raises(LaneMismatchError) as excinfo:
        asyncio.run(gateway.complete(_request(requested_model="frontier", lane=Lane.PRIVATE_VLLM)))

    assert excinfo.value.candidates == [("frontier", Lane.FRONTIER_API.value)]
    assert calls == []
    assert sink.events == []
    assert audit_sink.events == []


def test_requested_frontier_under_air_gap_is_refused_without_private_fallback(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [_private("private"), make_model("frontier", provider="anthropic")],
        {"agent": TaskRoute(model="private", fallbacks=("private",))},
    )
    gateway = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
        egress_guard=EgressPolicy(
            EgressSettings(
                air_gap=True,
                allowed_frontier_hosts=("api.anthropic.com",),
            )
        ),
    )
    calls: list[str] = []

    async def never_called(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(model)
        raise AssertionError("AIR_GAP must refuse before transport")

    monkeypatch.setattr(gateway._router, "acompletion", never_called)

    with pytest.raises(AirGapViolationError):
        asyncio.run(gateway.complete(_request(requested_model="frontier")))

    assert calls == []
    assert sink.events == []
    assert audit_sink.events == []


def test_requested_private_model_remains_usable_under_air_gap(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [make_model("frontier", provider="anthropic"), _private("private")],
        {"agent": TaskRoute(model="frontier", fallbacks=("private",))},
    )
    gateway = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
        egress_guard=EgressPolicy(
            EgressSettings(
                air_gap=True,
                allowed_frontier_hosts=("api.anthropic.com",),
            ),
            host_resolver=lambda _host: ("10.77.0.2",),
        ),
    )

    result = asyncio.run(gateway.complete(_request(requested_model="private")))

    assert result.model_id == "private"


def test_stream_uses_requested_primary_and_records_requested_and_actual(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    frozen_config: str,
) -> None:
    settings = make_settings(
        [_private("boot"), _private("live"), _private("chosen", mock_response="streamed")],
        {"agent": TaskRoute(model="boot")},
    )
    gateway = LiteLLMGateway(
        settings,
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
        route_provider=lambda: {"agent": TaskRoute(model="live")},
    )

    chunks = asyncio.run(_drain(gateway.stream(_request(requested_model="chosen"))))

    assert chunks
    assert {chunk.model_id for chunk in chunks} == {"chosen"}
    terminal = _events(audit_sink.events, "ne.gateway.stream")
    assert len(terminal) == 1
    assert terminal[0].provenance["requested_model"] == "chosen"
    assert terminal[0].provenance["model_name"] == "chosen"


def _oversized_selected_request() -> CompletionRequest:
    # ~830 estimated tokens: above the selected 2048-window model's 768-token
    # input budget, comfortably inside either 32768-window boot candidate.
    return CompletionRequest(
        messages=(ChatMessage(role="user", content="x" * 1200),),
        task="agent",
        requested_model="selected-small",
        trace_id="turn-15-budget-refusal",
    )


def _budget_refusal_settings() -> Any:
    return make_settings(
        [
            _private("boot-large"),
            _private("selected-small", context_window=2048),
            _private("fallback-large"),
        ],
        {"agent": TaskRoute(model="boot-large", fallbacks=("fallback-large",))},
        num_retries=0,
    )


def _assert_budget_refusal_audit(audit_sink: RecordingAuditSink, action: str) -> None:
    terminal = _events(audit_sink.events, action)
    assert len(terminal) == 1
    assert terminal[0].outcome == AuditOutcome.error
    assert terminal[0].provenance["requested_model"] == "selected-small"
    assert _events(audit_sink.events, "ne.gateway.model.observed") == []


def test_explicit_requested_primary_budget_refusal_does_not_fall_back_complete(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    gateway = LiteLLMGateway(
        _budget_refusal_settings(),
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
    )
    provider_calls: list[str] = []

    async def fallback_would_succeed(*, model: str, messages: Any, **kwargs: Any) -> Any:
        provider_calls.append(model)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="wrong fallback"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            _hidden_params={"response_cost": 0.0},
            model="provider/fallback-large",
        )

    monkeypatch.setattr(gateway._router, "acompletion", fallback_would_succeed)

    with pytest.raises(ContextBudgetExceededError) as excinfo:
        asyncio.run(gateway.complete(_oversized_selected_request()))

    assert excinfo.value.stage == "gateway-preflight"
    assert excinfo.value.context_window == 2048
    assert excinfo.value.input_budget == 768
    assert provider_calls == []
    assert sink.events == []
    _assert_budget_refusal_audit(audit_sink, "ne.gateway.complete")


def test_explicit_requested_primary_budget_refusal_does_not_fall_back_stream(
    sink: RecordingCostSink,
    audit_sink: RecordingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    frozen_config: str,
) -> None:
    gateway = LiteLLMGateway(
        _budget_refusal_settings(),
        sink,
        audit_sink=audit_sink,
        redactor=PASSTHROUGH_REDACTOR,
    )
    provider_calls: list[str] = []

    async def fallback_would_stream(*, model: str, messages: Any, **kwargs: Any) -> Any:
        provider_calls.append(model)

        async def chunks() -> AsyncIterator[object]:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="wrong fallback"))]
            )

        return chunks()

    monkeypatch.setattr(gateway._router, "acompletion", fallback_would_stream)

    with pytest.raises(ContextBudgetExceededError) as excinfo:
        asyncio.run(_drain(gateway.stream(_oversized_selected_request())))

    assert excinfo.value.stage == "gateway-preflight"
    assert excinfo.value.context_window == 2048
    assert excinfo.value.input_budget == 768
    assert provider_calls == []
    assert sink.events == []
    _assert_budget_refusal_audit(audit_sink, "ne.gateway.stream")
