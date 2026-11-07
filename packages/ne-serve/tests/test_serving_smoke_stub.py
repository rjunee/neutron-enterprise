# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CI serving-path smoke: client -> real localhost HTTP -> guided JSON.

Exercises ServeClient against the sanctioned in-process fake OpenAI-compatible
server (conftest ``fake_openai_server``) over REAL HTTP — the GPU-free CI
serving path while real-GPU throughput is live-parked. No mocking past the seam
under test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ne.serve.client import ServeClient

from .conftest import FakeServer

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "tier": {"type": "integer"}},
    "required": ["name", "tier"],
}


def test_health_models_and_guided_json(fake_openai_server: FakeServer) -> None:
    client = ServeClient(fake_openai_server.base_url, model=fake_openai_server.served_name)

    async def scenario() -> dict[str, Any]:
        await client.health()
        served = await client.models()
        assert fake_openai_server.served_name in served
        result = await client.generate_json(
            [{"role": "user", "content": "produce a thing"}],
            schema=_SCHEMA,
            schema_name="thing",
        )
        await client.aclose()
        return result

    result = asyncio.run(scenario())

    # Returned dict has the required keys with the right types.
    assert set(_SCHEMA["required"]) <= set(result)
    assert isinstance(result["name"], str)
    assert isinstance(result["tier"], int)

    # The server recorded a request carrying the json_schema response_format.
    chat_requests = [r for r in fake_openai_server.recorder.requests if "response_format" in r]
    assert chat_requests, "no chat completion with response_format was recorded"
    rf = chat_requests[-1]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
