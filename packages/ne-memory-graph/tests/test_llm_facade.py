# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""llm_facade OpenAI-compat contract over a fake ModelGateway (R1 cost capture)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from ne.contracts.models import Completion, CompletionRequest, Usage
from ne.memory_graph.llm_facade import create_llm_facade_app

_TOKEN = "svc-token-123"


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text="extracted-json",
            model_id="frontier-model-x",
            usage=Usage(prompt_tokens=11, completion_tokens=5),
            cost_usd=0.0009,
        )

    def stream(self, request: CompletionRequest):
        raise NotImplementedError


def _client(gateway: FakeGateway) -> TestClient:
    app = create_llm_facade_app(gateway, service_token=_TOKEN, task="memory-extraction")  # type: ignore[arg-type]
    return TestClient(app)


def test_empty_token_refuses_to_build() -> None:
    with pytest.raises(ValueError, match="service_token"):
        create_llm_facade_app(FakeGateway(), service_token="")  # type: ignore[arg-type]


def test_ok_shape_usage_and_response_format_passthrough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = FakeGateway()
    client = _client(gateway)
    with caplog.at_level(logging.INFO, logger="ne.memory_graph.llm_facade"):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json={
                "messages": [{"role": "user", "content": "extract entities"}],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "frontier-model-x"
    assert body["choices"][0]["message"]["content"] == "extracted-json"
    assert body["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "total_tokens": 16,
    }
    # response_format passed through into the gateway request params.
    req = gateway.requests[0]
    assert req.task == "memory-extraction"
    assert req.params["response_format"] == {"type": "json_object"}
    # per-event cost logged.
    assert any("cost_usd=0.0009" in r.message for r in caplog.records)


def test_bad_bearer_is_401() -> None:
    client = _client(FakeGateway())
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong"},
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 401


def test_missing_bearer_is_401() -> None:
    client = _client(FakeGateway())
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 401


def test_stream_true_is_400() -> None:
    client = _client(FakeGateway())
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json={"messages": [{"role": "user", "content": "x"}], "stream": True},
    )
    assert resp.status_code == 400
