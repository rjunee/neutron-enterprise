# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C2 no-egress proofs (NE-012): the REAL EgressPolicy plugged into NE-010's
egress_guard seam, driving the real litellm Router at its mock_response
boundary. These are the tests the NE-077 gate + NE-073 no-egress harness build
on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import (
    EgressPolicy,
    EgressSettings,
    LiteLLMGateway,
    ModelDef,
    TaskRoute,
)
from ne.gateway.errors import AirGapViolationError, EgressBlockedError

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str, lane: Lane | None = None) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task, lane=lane)


def _guard(**kwargs: Any) -> EgressPolicy:
    # The real integration models use the compose name ``vllm``. Resolve it
    # deterministically as the production container network would; no test may
    # inherit the developer machine's DNS state.
    return EgressPolicy(
        EgressSettings(**kwargs),
        host_resolver=lambda _host: ("10.0.0.8",),
    )


def _vllm_model(name: str = "vllm") -> ModelDef:
    return make_model(
        name,
        lane=Lane.PRIVATE_VLLM,
        litellm_model="hosted_vllm/x",
        provider="vllm",
        api_base="http://vllm:8000/v1",
    )


def test_frontier_blocked_when_host_not_allowlisted(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=_guard(allowed_frontier_hosts=()),
        redactor=PASSTHROUGH_REDACTOR,
    )
    with pytest.raises(EgressBlockedError):
        asyncio.run(gw.complete(_request("draft")))
    assert sink.events == []


def test_frontier_allowlisted_completes_and_emits_cost_event(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=_guard(allowed_frontier_hosts=("api.openai.com",)),
        redactor=PASSTHROUGH_REDACTOR,
    )
    result = asyncio.run(gw.complete(_request("draft")))
    assert result.text == "ok"
    assert len(sink.events) == 1


def test_air_gap_blocks_frontier_lane_completely(
    sink: RecordingCostSink, frozen_config: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=_guard(air_gap=True, allowed_frontier_hosts=("api.openai.com",)),
        redactor=PASSTHROUGH_REDACTOR,
    )
    calls: list[Any] = []

    async def _recorder(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("model"))
        raise AssertionError("litellm must never be invoked under AIR_GAP")

    monkeypatch.setattr(gw._router, "acompletion", _recorder)
    with pytest.raises(AirGapViolationError):
        asyncio.run(gw.complete(_request("draft")))
    assert calls == []
    assert sink.events == []


def test_air_gap_blocks_frontier_stream(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [make_model("primary", mock_response="ok")],
        {"draft": TaskRoute(model="primary")},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=_guard(air_gap=True, allowed_frontier_hosts=("api.openai.com",)),
        redactor=PASSTHROUGH_REDACTOR,
    )

    async def _consume() -> None:
        async for _ in gw.stream(_request("draft")):
            pass

    with pytest.raises(AirGapViolationError):
        asyncio.run(_consume())
    assert sink.events == []


def test_private_vllm_lane_works_under_air_gap(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [_vllm_model("vllm")],
        {"draft": TaskRoute(model="vllm")},
    )
    gw = LiteLLMGateway(settings, sink, egress_guard=_guard(air_gap=True))
    result = asyncio.run(gw.complete(_request("draft", lane=Lane.PRIVATE_VLLM)))
    assert result.text == "ok"
    assert len(sink.events) == 1


def test_public_dns_gpu_is_blocked_before_the_real_router_attempt(
    sink: RecordingCostSink,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = make_model(
        "gpu",
        lane=Lane.PRIVATE_VLLM,
        litellm_model="hosted_vllm/x",
        provider="vllm",
        api_base="http://gpu.private.example:8000/v1",
        mock_response="must-not-run",
    )
    settings = make_settings([model], {"draft": TaskRoute(model="gpu")})
    guard = EgressPolicy(
        EgressSettings(allowed_private_vllm_hosts=("gpu.private.example",)),
        host_resolver=lambda _host: ("10.77.0.2", "8.8.8.8"),
    )
    gw = LiteLLMGateway(settings, sink, egress_guard=guard)
    calls: list[Any] = []

    async def _recorder(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise AssertionError("provider router must not run for public/mixed DNS")

    monkeypatch.setattr(gw._router, "acompletion", _recorder)
    with pytest.raises(EgressBlockedError, match="resolve exclusively"):
        asyncio.run(gw.complete(_request("draft", lane=Lane.PRIVATE_VLLM)))
    assert calls == []
    assert sink.events == []


def test_no_silent_cross_lane_fallback_under_air_gap(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response="ok"),
            _vllm_model("backup"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=_guard(air_gap=True, allowed_frontier_hosts=("api.openai.com",)),
        redactor=PASSTHROUGH_REDACTOR,
    )
    # request.lane=None: primary is frontier, air-gapped -> AirGapViolationError
    # propagates UNWRAPPED (NE-010 seam), NO silent downgrade to the vllm backup.
    with pytest.raises(AirGapViolationError):
        asyncio.run(gw.complete(_request("draft")))
    assert sink.events == []


def test_explicit_private_lane_request_works_under_air_gap(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response="ok"),
            _vllm_model("backup"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
    )
    gw = LiteLLMGateway(
        settings,
        sink,
        egress_guard=_guard(air_gap=True, allowed_frontier_hosts=("api.openai.com",)),
        redactor=PASSTHROUGH_REDACTOR,
    )
    result = asyncio.run(gw.complete(_request("draft", lane=Lane.PRIVATE_VLLM)))
    assert result.text == "ok"
    assert len(sink.events) == 1


def test_real_guard_called_on_every_attempt(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response=Exception("boom")),
            make_model("backup", mock_response="ok"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
        num_retries=0,
    )
    calls: list[str] = []
    policy = EgressPolicy(EgressSettings(allowed_frontier_hosts=("api.openai.com",)))

    def guard(m: ModelDef) -> None:
        calls.append(m.name)
        policy(m)

    gw = LiteLLMGateway(settings, sink, egress_guard=guard, redactor=PASSTHROUGH_REDACTOR)
    result = asyncio.run(gw.complete(_request("draft")))
    assert calls == ["primary", "backup"]
    assert result.text == "ok"
