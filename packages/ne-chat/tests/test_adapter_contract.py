# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackChatAdapter conformance to the frozen ChatAdapter Protocol (C5)."""

from __future__ import annotations

import asyncio
import json

import httpx

from ne.chat.slack.adapter import SlackChatAdapter, _check_chat_adapter
from ne.chat.slack.streaming import SlackStreamer
from ne.contracts.governance import ApprovalRequest
from ne.contracts.runtime import ChatAdapter, InboundMessage, OutboundMessage
from ne.contracts.types import ApprovalTier, Citation, Principal

from .conftest import recording_client


def _adapter(recorder: list[httpx.Request]) -> SlackChatAdapter:
    client = recording_client(recorder)
    return SlackChatAdapter(client, SlackStreamer(client, min_update_interval_s=1.0))


def _bodies(recorder: list[httpx.Request]) -> str:
    return json.dumps([json.loads(r.content) for r in recorder])


def test_static_conformance() -> None:
    # _check_chat_adapter is pyright-checked; assert it round-trips at runtime too.
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    result: ChatAdapter = _check_chat_adapter(adapter)
    assert result is adapter


def test_send_message_posts_blocks_with_citations() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    citation = Citation(chunk_id="c", document_id="d", source_system="jira", source_ref="NE-1")

    async def scenario() -> None:
        await adapter.send_message(
            OutboundMessage(channel="C1", text="the answer", citations=(citation,))
        )

    asyncio.run(scenario())
    rendered = _bodies(recorder)
    assert "the answer" in rendered
    assert "jira:NE-1" in rendered


def test_send_digest_posts() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    asyncio.run(adapter.send_digest("C1", "3 approvals pending"))
    rendered = _bodies(recorder)
    assert "Daily digest" in rendered
    assert "3 approvals pending" in rendered


def test_send_approval_prompt_view_only_without_request_id() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    request = ApprovalRequest(
        tier=ApprovalTier.TIER_2, action="write.external.email.send", principal=Principal(id="u")
    )
    asyncio.run(adapter.send_approval_prompt("C1", request))
    rendered = _bodies(recorder)
    # No actionable buttons when the frozen ApprovalRequest carries no request id.
    assert "ne_approve" not in rendered
    assert "write.external.email.send" in rendered


def test_send_approval_prompt_actionable_with_request_id() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    request = ApprovalRequest(
        tier=ApprovalTier.TIER_2,
        action="write.external.email.send",
        principal=Principal(id="u"),
        payload={"request_id": "REQ-9"},
    )
    asyncio.run(adapter.send_approval_prompt("C1", request))
    rendered = _bodies(recorder)
    assert "ne_approve" in rendered and "ne_deny" in rendered
    assert "REQ-9" in rendered


def test_on_message_handlers_receive_inbound() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    seen: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        seen.append(msg)

    adapter.on_message(handler)
    msg = InboundMessage(channel="C1", sender="U1", text="hello")
    asyncio.run(adapter.dispatch_inbound(msg))
    assert seen == [msg]
