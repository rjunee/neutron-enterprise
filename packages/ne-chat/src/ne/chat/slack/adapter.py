# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackChatAdapter — the Slack implementation of the FROZEN ChatAdapter Protocol.

Implements ``ne.contracts.runtime.ChatAdapter`` exactly (``send_message`` /
``send_approval_prompt`` / ``send_digest`` / ``on_message``), plus a
``dispatch_inbound`` seam the ingress uses to fan a verified inbound message out
to the registered handlers. Teams v1.1 implements the SAME Protocol behind a
parallel ``ne.chat.teams`` adapter, so the console/runtime code that talks to a
``ChatAdapter`` never learns which channel it is.

``send_approval_prompt`` contract nuance: the frozen ``ApprovalRequest`` carries
NO durable request id, so this method renders an ACTIONABLE card ONLY when the
caller smuggles one in ``request.payload["request_id"]``; otherwise it posts a
VIEW-ONLY prompt (no buttons). The first-class actionable mirror path is
``ne.chat.slack.mirror.SlackApprovalMirror.mirror_pending(PendingApproval)``,
which has the durable id. This nuance is a candidate additive ne-contracts
follow-up (reported, not fixed here — ne-contracts is frozen this unit).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ne.chat.slack.blocks import approval_blocks, message_blocks
from ne.chat.slack.client import SlackChatClient
from ne.chat.slack.streaming import SlackStreamer
from ne.contracts.governance import ApprovalRequest
from ne.contracts.runtime import ChatAdapter, InboundMessage, OutboundMessage

__all__ = ["SlackChatAdapter"]

type InboundHandler = Callable[[InboundMessage], Awaitable[None]]


class SlackChatAdapter:
    """Slack channel surface: messages in/out, approval prompts, digests."""

    def __init__(self, client: SlackChatClient, streamer: SlackStreamer) -> None:
        self._client = client
        self._streamer = streamer
        self._handlers: list[InboundHandler] = []

    async def send_message(self, message: OutboundMessage) -> None:
        await self._client.call(
            "chat.postMessage",
            {
                "channel": message.channel,
                "blocks": message_blocks(message.text, message.citations),
            },
        )

    async def send_approval_prompt(self, channel: str, request: ApprovalRequest) -> None:
        request_id = request.payload.get("request_id")
        if isinstance(request_id, str) and request_id:
            blocks = approval_blocks(
                request_id=request_id,
                action=request.action,
                tier=int(request.tier),
                principal_id=request.principal.id,
            )
        else:
            # View-only: the frozen ApprovalRequest has no durable id, so no buttons.
            text = (
                f"*Approval required* (Tier {int(request.tier)})\n"
                f"*Action:* `{request.action}`\n"
                f"*Requested by:* `{request.principal.id}`"
            )
            blocks = message_blocks(text)
        await self._client.call("chat.postMessage", {"channel": channel, "blocks": blocks})

    async def send_digest(self, channel: str, text: str) -> None:
        blocks = message_blocks(f"*Daily digest*\n{text}")
        await self._client.call("chat.postMessage", {"channel": channel, "blocks": blocks})

    def on_message(self, handler: InboundHandler) -> None:
        self._handlers.append(handler)

    async def dispatch_inbound(self, message: InboundMessage) -> None:
        """Await every registered inbound handler (called by the verified ingress)."""
        for handler in self._handlers:
            await handler(message)


# Static conformance to the FROZEN ChatAdapter Protocol (pyright-checked). This is
# the conformance mechanism for this unit: ne.contracts.testing ships no
# ChatAdapter kit and ne-contracts is untouchable here (kit gap reported as a
# candidate additive contracts-lane follow-up).
def _check_chat_adapter(a: SlackChatAdapter) -> ChatAdapter:
    return a
