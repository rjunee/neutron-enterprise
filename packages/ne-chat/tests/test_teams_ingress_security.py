# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Teams ingress security — activity JWT verified BEFORE any processing (deliverable 1).

FastAPI TestClient over recorded fixtures. Proves: unsigned / forged activities are
rejected 401 status-only with NO dispatch, NO refs entry, and NO audit; a valid
signed activity acks 200 and dispatches ONE mention-stripped InboundMessage keyed
to the AAD object id; duplicate activity ids dedupe; a conversationUpdate is acked
but not dispatched; and a verified-but-off-allowlist serviceUrl is acked and
dropped (no refs, no dispatch).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient

from ne.chat.teams.adapter import TeamsChatAdapter
from ne.chat.teams.auth import StaticJwksKeySource, TeamsActivityAuthenticator
from ne.chat.teams.ingress import create_teams_ingress
from ne.chat.teams.refs import ConversationRefs
from ne.chat.teams.streaming import TeamsStreamer

from .conftest import RecordingAuditSink
from .teams_support import (
    APP_SECRET,
    CONVERSATION_ID,
    JWKS,
    SERVICE_URL,
    bearer,
    load_fixture,
    mint_activity_token,
    recording_teams_client,
    teams_chat_settings,
)

NOW = 1_720_000_000.0


def _build() -> SimpleNamespace:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    refs = ConversationRefs(max_size=100)
    client, _ = recording_teams_client(recorder, refs, seed_conversation=False)
    adapter = TeamsChatAdapter(client, TeamsStreamer(client, min_update_interval_s=0.0))
    received: list[Any] = []

    async def handler(msg: Any) -> None:
        received.append(msg)

    adapter.on_message(handler)
    authenticator = TeamsActivityAuthenticator(teams_chat_settings(), StaticJwksKeySource(JWKS))
    captured: list[Any] = []

    def spawn(coro: Any) -> Any:
        captured.append(coro)
        return coro

    app = create_teams_ingress(
        settings=teams_chat_settings(),
        adapter=adapter,
        authenticator=authenticator,
        refs=refs,
        clock=lambda: NOW,
        spawn=spawn,
    )
    return SimpleNamespace(
        client=TestClient(app),
        captured=captured,
        received=received,
        sink=sink,
        refs=refs,
    )


def _post(ctx: SimpleNamespace, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    raw = json.dumps(body).encode()
    return ctx.client.post(
        "/teams/messages", content=raw, headers={"content-type": "application/json", **headers}
    )


def _signed_headers(body: dict[str, Any]) -> dict[str, str]:
    token = mint_activity_token(serviceurl=str(body.get("serviceUrl", "")))
    return bearer(token)


def _drain(ctx: SimpleNamespace) -> None:
    for coro in ctx.captured:
        asyncio.run(coro)


def test_unsigned_activity_rejected_401_no_side_effects() -> None:
    ctx = _build()
    body = load_fixture("message_activity.json")
    resp = _post(ctx, body, {})  # no Authorization header
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid activity authorization"}
    assert ctx.captured == [] and ctx.received == []
    assert ctx.refs.lookup(CONVERSATION_ID) is None  # no refs entry
    assert ctx.sink.events == []  # a forgery is NOT audited
    # No secret / config echoed in the rejection body.
    assert APP_SECRET not in resp.text


def test_forged_token_rejected_401_unaudited() -> None:
    ctx = _build()
    body = load_fixture("message_activity.json")
    resp = _post(ctx, body, bearer("v0.forged.token"))
    assert resp.status_code == 401
    assert ctx.captured == [] and ctx.received == []
    assert ctx.sink.events == []


def test_valid_activity_dispatches_once_mention_stripped() -> None:
    ctx = _build()
    body = load_fixture("message_activity.json")
    resp = _post(ctx, body, _signed_headers(body))
    assert resp.status_code == 200
    assert len(ctx.captured) == 1
    assert ctx.refs.lookup(CONVERSATION_ID) == SERVICE_URL.rstrip("/")  # verified => recorded
    _drain(ctx)
    assert len(ctx.received) == 1
    msg = ctx.received[0]
    assert msg.channel == CONVERSATION_ID
    assert msg.sender == "aad-ada"  # the AAD object id, not the 29:... id
    assert msg.text == "what is the status?"  # <at>..</at> stripped


def test_duplicate_activity_id_dispatched_once() -> None:
    ctx = _build()
    body = load_fixture("message_activity.json")
    assert _post(ctx, body, _signed_headers(body)).status_code == 200
    assert _post(ctx, body, _signed_headers(body)).status_code == 200
    assert len(ctx.captured) == 1  # deduped by conversation:activity id
    _drain(ctx)
    assert len(ctx.received) == 1


def test_conversation_update_acked_not_dispatched() -> None:
    ctx = _build()
    body = load_fixture("conversation_update_activity.json")
    resp = _post(ctx, body, _signed_headers(body))
    assert resp.status_code == 200
    assert ctx.captured == []  # not a user message
    assert ctx.refs.lookup(CONVERSATION_ID) == SERVICE_URL.rstrip("/")  # still recorded


def test_verified_off_allowlist_service_url_acked_and_dropped() -> None:
    ctx = _build()
    body = load_fixture("message_activity.json")
    body["serviceUrl"] = "https://evil.example.com/amer"
    # A token whose serviceurl claim MATCHES the (hostile) activity serviceUrl —
    # auth passes, but the allowlist check drops it.
    headers = bearer(mint_activity_token(serviceurl="https://evil.example.com/amer"))
    resp = _post(ctx, body, headers)
    assert resp.status_code == 200  # acked, not 401
    assert ctx.captured == []  # dropped: no dispatch
    assert ctx.refs.lookup(CONVERSATION_ID) is None  # no refs entry (poisoning defense)


def test_undecodable_body_acked_and_ignored() -> None:
    ctx = _build()
    resp = ctx.client.post(
        "/teams/messages",
        content=b"\xff\xfe not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert ctx.captured == []
