# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CTXB-3: the governed refusal is a NAMED SSE error frame (design §3.4/§3.5-5).

The runtime trim guard converts an un-fittable prompt into an audited
``ContextBudgetExceededError`` refusal, and the SSE surface names it as the
error-frame ``detail`` — COUNTS never leak to the browser (C12). This is the exact
surface the parked live reproducer check asserts (a governed refusal, not a raw
provider 400).
"""

from __future__ import annotations

import asyncio
import json

from ne.api.sse import safe_error_detail, turn_event_stream
from ne.api.turn_wire import error_frame
from ne.contracts.errors import ContextBudgetExceededError
from ne.contracts.runtime import AgentRunResult
from ne.gateway.errors import AirGapViolationError, EgressBlockedError


def _err() -> ContextBudgetExceededError:
    return ContextBudgetExceededError(
        stage="runtime-trim",
        estimated_tokens=9,
        input_budget=1,
        context_window=16,
        dropped_turns=2,
    )


def test_safe_error_detail_names_the_class_only() -> None:
    detail = safe_error_detail(_err())
    assert detail == "ContextBudgetExceededError"
    # None of the numeric counts leak into the browser-visible detail (C12).
    for count in ("9", "16", "input_budget", "dropped_turns"):
        assert count not in detail


def test_turn_event_stream_emits_named_error_frame() -> None:
    async def _boom() -> AgentRunResult:
        raise _err()

    async def _collect() -> list[bytes]:
        frames: list[bytes] = []
        async for frame in turn_event_stream(_boom(), keepalive_s=0.0, timeout_s=5.0):
            frames.append(frame)
        return frames

    frames = asyncio.run(_collect())
    text = b"".join(frames).decode()
    assert "event: error" in text
    # Parse the error frame's data payload.
    error_line = next(
        line
        for block in text.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data:") and "error" in block
    )
    payload = json.loads(error_line[len("data:") :].strip())
    assert payload["type"] == "error"
    assert payload["detail"] == "ContextBudgetExceededError"
    # No leaked counts anywhere in the wire bytes.
    assert "dropped_turns" not in text
    assert "input_budget" not in text


def test_air_gap_detail_is_actionable_and_matches_durable_error_frame() -> None:
    sentinel = "https://user:SECRET@example.test/private"
    exc = AirGapViolationError(sentinel)

    async def _boom() -> AgentRunResult:
        raise exc

    async def _collect() -> list[bytes]:
        return [frame async for frame in turn_event_stream(_boom(), keepalive_s=0.0, timeout_s=5.0)]

    inline = b"".join(asyncio.run(_collect())).decode()
    payload = json.loads(
        next(line for line in inline.splitlines() if line.startswith("data:"))[len("data:") :]
    )
    durable = error_frame(0, exc).sse_payload()
    assert payload["detail"] == durable["detail"]
    assert payload["detail"] == (
        "AIR_GAP is engaged; frontier models are unavailable; select a private-vllm model"
    )
    assert sentinel not in payload["detail"]


def test_generic_egress_error_detail_remains_class_only() -> None:
    sentinel = "https://user:SECRET@example.test/private"
    detail = safe_error_detail(EgressBlockedError(sentinel))
    assert detail == "EgressBlockedError"
    assert sentinel not in detail
