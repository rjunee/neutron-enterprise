# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsStreamer — placeholder POST + throttled update PUT + final flush + ephemeral."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from ne.chat.teams.streaming import TeamsStreamer
from ne.contracts.types import Citation

from .teams_support import CONVERSATION_ID, recording_teams_client


async def _chunks(*items: str) -> AsyncIterator[str]:
    for item in items:
        yield item


def _methods(recorder: list[httpx.Request]) -> list[str]:
    out: list[str] = []
    for r in recorder:
        url = str(r.url)
        if url.endswith("/activities"):
            out.append("POST")
        elif "/activities/" in url:
            out.append("PUT")
    return out


def _streamer(recorder: list[httpx.Request], *, clock=None) -> TeamsStreamer:
    client, _refs = recording_teams_client(recorder)
    kwargs = {"clock": clock} if clock is not None else {}
    return TeamsStreamer(client, min_update_interval_s=1.0, **kwargs)


def test_start_posts_placeholder_returns_id() -> None:
    recorder: list[httpx.Request] = []
    streamer = _streamer(recorder)
    ts = asyncio.run(streamer.start(CONVERSATION_ID))
    assert ts == "act-out-1"
    assert _methods(recorder) == ["POST"]


def test_stream_into_puts_final_text_and_citations() -> None:
    recorder: list[httpx.Request] = []
    streamer = _streamer(recorder, clock=lambda: 100.0)  # frozen: only the final flush
    citation = Citation(
        chunk_id="c1", document_id="d1", source_system="confluence", source_ref="PAGE-42"
    )

    async def scenario() -> None:
        await streamer.stream(CONVERSATION_ID, _chunks("Answer."), citations=(citation,))

    asyncio.run(scenario())
    assert _methods(recorder) == ["POST", "PUT"]  # placeholder + one final flush
    final = json.loads(recorder[-1].content)
    assert "Answer." in final["text"]
    assert "confluence:PAGE-42" in final["text"]


def test_updates_throttled_to_min_interval() -> None:
    recorder: list[httpx.Request] = []
    ticks = iter(range(0, 10_000, 10))
    streamer = _streamer(recorder, clock=lambda: float(next(ticks)))

    async def scenario() -> None:
        await streamer.stream(CONVERSATION_ID, _chunks("a", "b", "c"))

    asyncio.run(scenario())
    methods = _methods(recorder)
    assert methods[0] == "POST"
    assert methods.count("PUT") >= 2  # interim(s) + final


def test_fail_puts_generic_notice() -> None:
    recorder: list[httpx.Request] = []
    streamer = _streamer(recorder)

    async def scenario() -> None:
        ts = await streamer.start(CONVERSATION_ID)
        await streamer.fail(CONVERSATION_ID, ts)

    asyncio.run(scenario())
    final = json.loads(recorder[-1].content)
    assert "went wrong" in final["text"]
    assert "Traceback" not in final["text"]


def test_ephemeral_posts_plain_message() -> None:
    recorder: list[httpx.Request] = []
    streamer = _streamer(recorder)
    asyncio.run(streamer.ephemeral(CONVERSATION_ID, "aad-ada", "You are not linked."))
    assert _methods(recorder) == ["POST"]
    body = json.loads(recorder[-1].content)
    assert body["text"] == "You are not linked."
