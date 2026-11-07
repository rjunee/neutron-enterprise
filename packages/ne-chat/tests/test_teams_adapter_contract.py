# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsChatAdapter conformance to the frozen ChatAdapter Protocol (C5)."""

from __future__ import annotations

import asyncio
import json

import httpx

from ne.chat.teams.adapter import TeamsChatAdapter, _check_chat_adapter
from ne.chat.teams.streaming import TeamsStreamer
from ne.contracts.governance import ApprovalRequest
from ne.contracts.runtime import ChatAdapter, InboundMessage, OutboundMessage
from ne.contracts.types import ApprovalTier, Citation, Principal

from .teams_support import recording_teams_client


def _adapter(recorder: list[httpx.Request]) -> TeamsChatAdapter:
    client, _refs = recording_teams_client(recorder)
    return TeamsChatAdapter(client, TeamsStreamer(client, min_update_interval_s=1.0))


def _bodies(recorder: list[httpx.Request]) -> str:
    return json.dumps(
        [json.loads(r.content) for r in recorder if r.content and str(r.url).endswith("activities")]
    )


def test_static_conformance() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    result: ChatAdapter = _check_chat_adapter(adapter)
    assert result is adapter


def test_send_message_renders_text_and_citations() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    citation = Citation(chunk_id="c", document_id="d", source_system="jira", source_ref="NE-1")

    async def scenario() -> None:
        await adapter.send_message(
            OutboundMessage(channel="a:conv1", text="the answer", citations=(citation,))
        )

    asyncio.run(scenario())
    rendered = _bodies(recorder)
    assert "the answer" in rendered
    assert "Sources" in rendered
    assert "jira:NE-1" in rendered


def test_send_digest_renders() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    asyncio.run(adapter.send_digest("a:conv1", "3 approvals pending"))
    rendered = _bodies(recorder)
    assert "Daily digest" in rendered
    assert "3 approvals pending" in rendered


def test_send_approval_prompt_view_only() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    request = ApprovalRequest(
        tier=ApprovalTier.TIER_2, action="write.external.email.send", principal=Principal(id="u")
    )
    asyncio.run(adapter.send_approval_prompt("a:conv1", request))
    rendered = _bodies(recorder)
    assert "Approval required" in rendered
    assert "write.external.email.send" in rendered
    assert "Tier 2" in rendered
    # View-only: no actionable request-id pointer without a durable id.
    assert "Request id" not in rendered


def test_send_approval_prompt_echoes_request_id() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    request = ApprovalRequest(
        tier=ApprovalTier.TIER_2,
        action="write.external.email.send",
        principal=Principal(id="u"),
        payload={"request_id": "REQ-9"},
    )
    asyncio.run(adapter.send_approval_prompt("a:conv1", request))
    rendered = _bodies(recorder)
    assert "REQ-9" in rendered
    assert "console or Slack" in rendered


def test_on_message_handlers_receive_inbound() -> None:
    recorder: list[httpx.Request] = []
    adapter = _adapter(recorder)
    seen: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        seen.append(msg)

    adapter.on_message(handler)
    msg = InboundMessage(channel="a:conv1", sender="aad-ada", text="hello")
    asyncio.run(adapter.dispatch_inbound(msg))
    assert seen == [msg]
