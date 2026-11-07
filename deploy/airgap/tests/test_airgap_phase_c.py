# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-073 AIR_GAP phase C — the OPEN-WEIGHT LANE ALIVE proof.

A real CPU-mode vLLM server (Qwen3-0.6B, weights pre-staged and
`ne.serve verify`-checked at the air-gap boundary, ``HF_HUB_OFFLINE=1``)
serves a genuine completion fully inside the sealed namespace, with zero
egress — proving governed open-weight inference keeps working under
AIR_GAP even though the frontier lane (phase A) is dead.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from ne.serve.client import ServeClient

from .conftest import AIRGAP_ACTIVE

if not AIRGAP_ACTIVE:
    pytest.skip(
        "AIR_GAP harness only runs inside the sealed netns (no-egress.yml)",
        allow_module_level=True,
    )

_BASE_URL = "http://127.0.0.1:8091"
_EXPECTED_MODEL = "qwen3-0.6b"


def test_open_weight_lane_serves_real_completion_fully_offline() -> None:
    client = ServeClient(_BASE_URL, timeout=120)

    async def _scenario() -> tuple[list[str], str]:
        await client.health()
        models = await client.models()
        text = await client.complete(
            [{"role": "user", "content": "Say hello in one word."}],
            model=_EXPECTED_MODEL,
            max_tokens=8,
        )
        await client.aclose()
        return models, text

    models, text = asyncio.run(_scenario())
    assert _EXPECTED_MODEL in models
    assert text.strip()


def test_raw_ip_egress_dead_while_serving() -> None:
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=5)


def test_frontier_dns_dead_while_serving() -> None:
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("api.anthropic.com", 443)
