# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Teams response streaming — the portable post-then-update pattern (NE-080).

:class:`TeamsStreamer` posts a placeholder activity immediately (an instant ack),
then edits it in place via ``update_activity`` at most once per
``min_update_interval_s``, and ALWAYS flushes a final update with the full text
plus any citations. This is the SAME post-then-update pattern
``ne.chat.slack.streaming.SlackStreamer`` uses, so the channel-agnostic
ConversationService drives both unchanged.

Teams has no native ephemeral primitive, so :meth:`ephemeral` posts a plain
(NON-ephemeral) message — a documented v1 limitation. Citations render as plain
markdown (no Adaptive Cards / Block Kit): a ``**Sources**`` section with one
``` - `source_system:source_ref` ``` line each.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from ne.chat.teams.client import TeamsChatClient
from ne.contracts.types import Citation

__all__ = ["TeamsStreamer", "render_activity_text"]

_GENERIC_FAILURE = "Something went wrong handling this message."
_PLACEHOLDER = "_Thinking…_"


def render_activity_text(text: str, citations: Sequence[Citation] = ()) -> str:
    """Render answer text + a plain-markdown Sources section (the ONE shared renderer)."""
    body = text or " "
    if citations:
        lines = "\n".join(f"- `{c.source_system}:{c.source_ref}`" for c in citations)
        body = f"{body}\n\n**Sources**\n{lines}"
    return body


def _message(text: str) -> dict[str, object]:
    return {"type": "message", "text": text}


class TeamsStreamer:
    """Post-then-throttled-``update_activity`` streamer over a :class:`TeamsChatClient`."""

    def __init__(
        self,
        client: TeamsChatClient,
        *,
        min_update_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._min_interval = min_update_interval_s
        self._clock = clock
        self._sleep = sleep

    async def start(self, channel: str) -> str:
        """Post the streaming placeholder immediately; return its activity id."""
        return await self._client.post_activity(channel, _message(_PLACEHOLDER))

    async def stream_into(
        self,
        channel: str,
        ts: str,
        chunks: AsyncIterator[str],
        *,
        citations: Sequence[Citation] = (),
    ) -> None:
        """Edit ``ts`` as ``chunks`` arrive (throttled), then a final unconditional flush."""
        accumulated = ""
        last_update = self._clock()
        async for chunk in chunks:
            if not chunk:
                continue
            accumulated += chunk
            if self._clock() - last_update >= self._min_interval:
                await self._update(channel, ts, accumulated)
                last_update = self._clock()
        await self._update(channel, ts, accumulated, citations=citations)

    async def stream(
        self,
        channel: str,
        chunks: AsyncIterator[str],
        *,
        citations: Sequence[Citation] = (),
    ) -> str:
        """Self-contained: post a placeholder, stream chunks in, return the activity id."""
        ts = await self.start(channel)
        await self.stream_into(channel, ts, chunks, citations=citations)
        return ts

    async def fail(self, channel: str, ts: str) -> None:
        """Replace the placeholder with a generic failure message (C12: no internals)."""
        await self._update(channel, ts, _GENERIC_FAILURE)

    async def ephemeral(self, channel: str, user: str, text: str) -> None:
        """Post a plain message (Teams has no ephemeral primitive — v1 fallback)."""
        await self._client.post_activity(channel, _message(text))

    async def _update(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        citations: Sequence[Citation] = (),
    ) -> None:
        await self._client.update_activity(
            channel, ts, _message(render_activity_text(text, citations))
        )
