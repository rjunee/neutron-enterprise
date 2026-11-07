# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE-PARKED acceptance test against a real vLLM OpenAI-compatible server.

Skipped (module-level) when NE_VLLM_URL is unreachable. GPU provisioning is
NE-000 (owner-deferred). To run for real:

    python -m ne.serve download Qwen/Qwen3.6-35B-A3B --dest ./models
    NE_VLLM_WEIGHTS_ROOT=./models \
      docker compose -f deploy/compose/serve/compose.yaml --profile gpu up
    NE_VLLM_URL=http://localhost:8090 uv run pytest packages/ne-serve -q
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

from ne.serve.client import ServeClient


def _vllm_url() -> str:
    return os.environ.get("NE_VLLM_URL", "http://localhost:8090")


def _vllm_reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


_URL = _vllm_url()

if not _vllm_reachable(_URL):
    pytest.skip(
        "LIVE-PARKED: real-GPU vLLM serving pending — pre-stage weights "
        "(python -m ne.serve download ...) and docker compose -f "
        "deploy/compose/serve/compose.yaml --profile gpu up "
        f"(NE_VLLM_URL={_URL} unreachable)",
        allow_module_level=True,
    )

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "tier": {"type": "integer"}},
    "required": ["name", "tier"],
}


def test_health_and_models() -> None:
    client = ServeClient(_URL)

    async def scenario() -> list[str]:
        await client.health()
        models = await client.models()
        await client.aclose()
        return models

    models = asyncio.run(scenario())
    assert models  # served model name present


def test_small_completion_non_empty() -> None:
    client = ServeClient(_URL)

    async def scenario() -> str:
        served = (await client.models())[0]
        out = await client.complete(
            [{"role": "user", "content": "Say hello in one word."}],
            model=served,
            max_tokens=16,
        )
        await client.aclose()
        return out

    assert asyncio.run(scenario()).strip()


def test_generate_json_conformant() -> None:
    client = ServeClient(_URL)

    async def scenario() -> dict[str, object]:
        served = (await client.models())[0]
        result = await client.generate_json(
            [{"role": "user", "content": "Return a name and integer tier."}],
            schema=_SCHEMA,
            model=served,
        )
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert set(_SCHEMA["required"]) <= set(result)
    assert isinstance(result["name"], str)
    assert isinstance(result["tier"], int)


def test_throughput_smoke_four_concurrent() -> None:
    client = ServeClient(_URL)

    async def scenario() -> None:
        served = (await client.models())[0]

        async def one() -> str:
            return await client.complete(
                [{"role": "user", "content": "Count to ten."}],
                model=served,
                max_tokens=64,
            )

        start = time.monotonic()
        outputs = await asyncio.gather(*[one() for _ in range(4)])
        elapsed = time.monotonic() - start
        await client.aclose()

        assert all(o.strip() for o in outputs)
        approx_tokens = 4 * 64
        print(f"throughput smoke: ~{approx_tokens / elapsed:.1f} tok/s (4x64, {elapsed:.2f}s)")

    asyncio.run(scenario())
