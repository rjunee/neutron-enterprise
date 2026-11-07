# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsChatAdapter — the Teams implementation of the FROZEN ChatAdapter Protocol.

Implements ``ne.contracts.runtime.ChatAdapter`` exactly (``send_message`` /
``send_approval_prompt`` / ``send_digest`` / ``on_message``), plus a
``dispatch_inbound`` seam the ingress uses to fan a verified inbound activity out
to the registered handlers — the SAME shape ``ne.chat.slack.adapter`` implements,
so the console/runtime code that talks to a ``ChatAdapter`` never learns which
channel it is.

``send_approval_prompt`` is VIEW-ONLY: actionable Adaptive-Card approval actioning
from Teams is deliberately OUT of v1 (no invoke route) — a Teams approval prompt
renders a read-only summary and, when the caller smuggles a durable id in
``request.payload['request_id']``, echoes it with an "action it from the console
or Slack" pointer. All outbound text shares the single
:func:`~ne.chat.teams.streaming.render_activity_text` renderer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ne.chat.teams.client import TeamsChatClient
from ne.chat.teams.streaming import TeamsStreamer, render_activity_text
from ne.contracts.governance import ApprovalRequest
from ne.contracts.runtime import ChatAdapter, InboundMessage, OutboundMessage

__all__ = ["TeamsChatAdapter"]

type InboundHandler = Callable[[InboundMessage], Awaitable[None]]


class TeamsChatAdapter:
    """Teams channel surface: messages in/out, approval prompts, digests."""

    def __init__(self, client: TeamsChatClient, streamer: TeamsStreamer) -> None:
        self._client = client
        self._streamer = streamer
        self._handlers: list[InboundHandler] = []

    async def send_message(self, message: OutboundMessage) -> None:
        await self._client.post_activity(
            message.channel,
            {"type": "message", "text": render_activity_text(message.text, message.citations)},
        )

    async def send_approval_prompt(self, channel: str, request: ApprovalRequest) -> None:
        lines = [
            f"**Approval required** (Tier {int(request.tier)})",
            f"**Action:** `{request.action}`",
            f"**Requested by:** `{request.principal.id}`",
        ]
        request_id = request.payload.get("request_id")
        if isinstance(request_id, str) and request_id:
            lines.append(f"Request id: `{request_id}` — action it from the console or Slack")
        await self._client.post_activity(channel, {"type": "message", "text": "\n".join(lines)})

    async def send_digest(self, channel: str, text: str) -> None:
        await self._client.post_activity(
            channel, {"type": "message", "text": f"**Daily digest**\n{text}"}
        )

    def on_message(self, handler: InboundHandler) -> None:
        self._handlers.append(handler)

    async def dispatch_inbound(self, message: InboundMessage) -> None:
        """Await every registered inbound handler (called by the verified ingress)."""
        for handler in self._handlers:
            await handler(message)


# Static conformance to the FROZEN ChatAdapter Protocol (pyright-checked) — the
# same conformance mechanism the Slack adapter uses (ne-contracts is untouchable
# here, and ships no ChatAdapter kit).
def _check_chat_adapter(a: TeamsChatAdapter) -> ChatAdapter:
    return a
