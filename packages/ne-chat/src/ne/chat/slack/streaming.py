# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack response streaming — the orchestrator-mandated post-then-update pattern.

:class:`SlackStreamer` posts a placeholder message immediately (so the user sees
an instant ack), then edits it in place with ``chat.update`` at most once per
``min_update_interval_s`` (Slack's ~1 msg/s/channel write limit), and ALWAYS
flushes a final update with the full text plus any citations.

Slack's newer native streaming trio (``chat.startStream`` /
``chat.appendStream`` / ``chat.stopStream``, 2025-10 changelog) is threads-only
and newer; it is recorded as the v1.1 enhancement path. This unit uses the
portable post-then-update pattern so the same class backs Teams v1.1.

The ``AsyncIterator[str]`` seam is real: today the runtime yields a single final
chunk (``AgentSession.run`` is not token-streaming), but tests drive multi-chunk
iterators and a future token-level runtime plugs in here unchanged.

Placeholder/stream split: :meth:`start` posts the placeholder and returns its
``ts`` so a caller (the ConversationService) can run the agent turn AFTER the
user already sees an ack and can :meth:`fail` that exact message if the turn dies.
:meth:`stream` is the self-contained convenience (start + stream_into).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from ne.chat.slack.blocks import message_blocks, placeholder_blocks
from ne.chat.slack.client import SlackChatClient
from ne.contracts.types import Citation

__all__ = ["SlackStreamer"]

_GENERIC_FAILURE = "Something went wrong — the team has been notified."


class SlackStreamer:
    """Post-then-throttled-``chat.update`` streamer over a :class:`SlackChatClient`."""

    def __init__(
        self,
        client: SlackChatClient,
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
        """Post the streaming placeholder immediately; return its ``ts``."""
        posted = await self._client.call(
            "chat.postMessage", {"channel": channel, "blocks": placeholder_blocks()}
        )
        return str(posted["ts"])

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
        # Final flush is UNCONDITIONAL (even if the interval has not elapsed) so the
        # full text + citations always land, even when only one chunk ever arrived.
        await self._update(channel, ts, accumulated, citations=citations)

    async def stream(
        self,
        channel: str,
        chunks: AsyncIterator[str],
        *,
        citations: Sequence[Citation] = (),
    ) -> str:
        """Self-contained: post a placeholder, stream chunks in, return the ``ts``."""
        ts = await self.start(channel)
        await self.stream_into(channel, ts, chunks, citations=citations)
        return ts

    async def fail(self, channel: str, ts: str) -> None:
        """Replace the placeholder with a generic failure message (C12: no internals)."""
        await self._update(channel, ts, _GENERIC_FAILURE)

    async def ephemeral(self, channel: str, user: str, text: str) -> None:
        """Send an ephemeral message visible only to ``user`` (denials/notices)."""
        await self._client.call(
            "chat.postEphemeral",
            {"channel": channel, "user": user, "blocks": message_blocks(text)},
        )

    async def _update(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        citations: Sequence[Citation] = (),
    ) -> None:
        await self._client.call(
            "chat.update",
            {"channel": channel, "ts": ts, "blocks": message_blocks(text, citations)},
        )
