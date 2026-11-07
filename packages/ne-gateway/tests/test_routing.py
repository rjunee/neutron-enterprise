# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Task routing, param merging, and lane filtering — through the real Router."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ne.contracts import ChatMessage, CompletionRequest, Lane
from ne.gateway import LaneMismatchError, LiteLLMGateway, TaskRoute, UnknownTaskError

from .conftest import PASSTHROUGH_REDACTOR, RecordingCostSink, make_model, make_settings

_MSGS = (ChatMessage(role="user", content="hi"),)


def _request(task: str, **kw: Any) -> CompletionRequest:
    return CompletionRequest(messages=_MSGS, task=task, **kw)


def test_complete_routes_task_to_primary(sink: RecordingCostSink, frozen_config: str) -> None:
    settings = make_settings(
        [
            make_model("primary", mock_response="from-primary"),
            make_model("backup", mock_response="from-backup"),
        ],
        {"draft": TaskRoute(model="primary", fallbacks=("backup",))},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    result = asyncio.run(gw.complete(_request("draft")))
    assert result.model_id == "primary"
    assert result.text == "from-primary"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 20


def test_unknown_task_raises_listing_configured(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [make_model("a")],
        {"draft": TaskRoute(model="a"), "triage": TaskRoute(model="a")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(UnknownTaskError) as excinfo:
        asyncio.run(gw.complete(_request("summarize")))
    msg = str(excinfo.value)
    assert "summarize" in msg
    assert "draft" in msg
    assert "triage" in msg


def test_params_merge_precedence(
    sink: RecordingCostSink, monkeypatch: pytest.MonkeyPatch, frozen_config: str
) -> None:
    settings = make_settings(
        [make_model("a")],
        {"draft": TaskRoute(model="a", params={"temperature": 0.1, "top_p": 0.5})},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)

    captured: dict[str, Any] = {}

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["model"] = model
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            _hidden_params={"response_cost": 0.0},
        )

    monkeypatch.setattr(gw._router, "acompletion", fake_acompletion)
    # request.params.temperature (0.9) must win over route.params.temperature (0.1);
    # route-only top_p and request-only max_tokens both forwarded.
    asyncio.run(gw.complete(_request("draft", params={"temperature": 0.9, "max_tokens": 42})))
    assert captured["temperature"] == 0.9
    assert captured["top_p"] == 0.5
    assert captured["max_tokens"] == 42


def test_lane_filter_selects_matching_candidate(
    sink: RecordingCostSink, frozen_config: str
) -> None:
    settings = make_settings(
        [
            make_model("frontier", lane=Lane.FRONTIER_API, mock_response="frontier-said"),
            make_model("vllm", lane=Lane.PRIVATE_VLLM, mock_response="vllm-said"),
        ],
        {"draft": TaskRoute(model="frontier", fallbacks=("vllm",))},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    result = asyncio.run(gw.complete(_request("draft", lane=Lane.PRIVATE_VLLM)))
    assert result.model_id == "vllm"
    assert result.text == "vllm-said"


def test_lane_mismatch_raises(sink: RecordingCostSink) -> None:
    settings = make_settings(
        [make_model("frontier", lane=Lane.FRONTIER_API)],
        {"draft": TaskRoute(model="frontier")},
    )
    gw = LiteLLMGateway(settings, sink, redactor=PASSTHROUGH_REDACTOR)
    with pytest.raises(LaneMismatchError) as excinfo:
        asyncio.run(gw.complete(_request("draft", lane=Lane.PRIVATE_VLLM)))
    msg = str(excinfo.value)
    assert "draft" in msg
    assert "private-vllm" in msg
