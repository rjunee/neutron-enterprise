# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackStreamer — placeholder + throttled chat.update + unconditional final flush."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from ne.chat.slack.streaming import SlackStreamer
from ne.contracts.types import Citation

from .conftest import recording_client


async def _chunks(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


def _methods(recorder: list[httpx.Request]) -> list[str]:
    return [r.url.path.rsplit("/", 1)[-1] for r in recorder]


def _last_body(recorder: list[httpx.Request]) -> dict[str, object]:
    return json.loads(recorder[-1].content)


def test_posts_placeholder_then_incremental_updates() -> None:
    recorder: list[httpx.Request] = []
    # A monotonic-ish clock that advances 10s per read so every chunk triggers an update.
    ticks = iter(range(0, 10_000, 10))
    streamer = SlackStreamer(
        recording_client(recorder), min_update_interval_s=1.0, clock=lambda: float(next(ticks))
    )

    async def scenario() -> str:
        return await streamer.stream("C1", _chunks("Hello ", "world", "!"))

    ts = asyncio.run(scenario())
    methods = _methods(recorder)
    assert methods[0] == "chat.postMessage"  # placeholder
    assert methods.count("chat.update") >= 2  # interim(s) + final
    assert ts == "1720000000.000100"
    # every update targets the SAME ts.
    for req in recorder[1:]:
        assert json.loads(req.content)["ts"] == ts


def test_updates_throttled_to_min_interval() -> None:
    recorder: list[httpx.Request] = []
    # A frozen clock: the interval never elapses, so only the final flush updates.
    streamer = SlackStreamer(
        recording_client(recorder), min_update_interval_s=1.0, clock=lambda: 100.0
    )

    async def scenario() -> None:
        await streamer.stream("C1", _chunks("a", "b", "c", "d"))

    asyncio.run(scenario())
    methods = _methods(recorder)
    assert methods == ["chat.postMessage", "chat.update"]  # 1 placeholder + 1 final only


def test_final_flush_always_sent_with_citations() -> None:
    recorder: list[httpx.Request] = []
    streamer = SlackStreamer(
        recording_client(recorder), min_update_interval_s=1.0, clock=lambda: 100.0
    )
    citation = Citation(
        chunk_id="c1", document_id="d1", source_system="confluence", source_ref="PAGE-42"
    )

    async def scenario() -> None:
        await streamer.stream("C1", _chunks("Answer."), citations=(citation,))

    asyncio.run(scenario())
    body = _last_body(recorder)
    blocks = body["blocks"]
    assert isinstance(blocks, list)
    rendered = json.dumps(blocks)
    assert "Answer." in rendered
    assert "confluence:PAGE-42" in rendered  # context block from Citation fields


def test_fail_update_is_generic() -> None:
    recorder: list[httpx.Request] = []
    streamer = SlackStreamer(recording_client(recorder), min_update_interval_s=1.0)

    async def scenario() -> None:
        ts = await streamer.start("C1")
        await streamer.fail("C1", ts)

    asyncio.run(scenario())
    rendered = json.dumps(_last_body(recorder))
    assert "went wrong" in rendered
    assert "Traceback" not in rendered and "Error" not in rendered
