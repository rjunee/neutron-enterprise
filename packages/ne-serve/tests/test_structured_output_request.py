# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Below-seam unit tests for ServeClient via httpx.MockTransport.

Pattern: ne-brain/tests/test_tei_embedder.py. No real server — the HTTP shape
of the request is asserted (esp. the current structured-output surface, and the
ABSENCE of the removed guided_* fields). Async seams via asyncio.run.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from ne.serve.client import ServeClient
from ne.serve.errors import ServeHealthError, StructuredOutputViolationError

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "tier": {"type": "integer"}},
    "required": ["name", "tier"],
}


def _client(handler: Any) -> ServeClient:
    transport = httpx.MockTransport(handler)
    return ServeClient(
        "http://vllm.local",
        model="qwen3-0.6b",
        client=httpx.AsyncClient(transport=transport, base_url="http://vllm.local"),
    )


def test_generate_json_emits_current_response_format_surface() -> None:
    recorded: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        recorded.append(body)
        content = json.dumps({"name": "x", "tier": 3})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = _client(handler)
    result = asyncio.run(
        client.generate_json(
            [{"role": "user", "content": "go"}], schema=_SCHEMA, schema_name="thing"
        )
    )
    assert result == {"name": "x", "tier": 3}

    body = recorded[0]
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "thing"
    assert rf["json_schema"]["schema"] == _SCHEMA
    assert rf["json_schema"]["strict"] is True

    # The removed guided_* fields must never appear.
    for removed in ("guided_json", "guided_choice", "guided_regex", "guided_grammar"):
        assert removed not in body


def test_generate_json_raises_on_non_json_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json {"}}]})

    client = _client(handler)
    with pytest.raises(StructuredOutputViolationError):
        asyncio.run(client.generate_json([{"role": "user", "content": "go"}], schema=_SCHEMA))


def test_generate_json_raises_when_content_is_not_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "[1, 2, 3]"}}]})

    client = _client(handler)
    with pytest.raises(StructuredOutputViolationError):
        asyncio.run(client.generate_json([{"role": "user", "content": "go"}], schema=_SCHEMA))


def test_health_raises_on_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(500)

    client = _client(handler)
    with pytest.raises(ServeHealthError):
        asyncio.run(client.health())


def test_health_ok_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client = _client(handler)
    asyncio.run(client.health())  # no raise


def test_models_parses_data_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    client = _client(handler)
    assert asyncio.run(client.models()) == ["a", "b"]


def test_complete_returns_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi there"}}]})

    client = _client(handler)
    out = asyncio.run(client.complete([{"role": "user", "content": "hi"}]))
    assert out == "hi there"
