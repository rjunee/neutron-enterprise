# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack ingress security — signature verified BEFORE any processing (deliverable 1).

FastAPI TestClient over recorded fixtures. Proves: unsigned / forged / stale
requests are rejected 401 status-only before any handler or spawn runs; a valid
event acks 200 and dispatches an InboundMessage; duplicate event ids are deduped;
interaction routes verify signatures too.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi.testclient import TestClient

from ne.chat.approvals import ApprovalActionService
from ne.chat.slack.adapter import SlackChatAdapter
from ne.chat.slack.ingress import create_slack_ingress
from ne.chat.slack.mirror import SlackApprovalMirror
from ne.chat.slack.streaming import SlackStreamer

from .conftest import (
    TEST_BOT_TOKEN,
    TEST_SIGNING_SECRET,
    RecordingAuditSink,
    approver_linker,
    chat_settings,
    load_fixture,
    load_fixture_bytes,
    make_gate,
    recording_client,
    sign,
)

NOW = 1_720_000_000.0


def _build(clock: Any = lambda: NOW) -> SimpleNamespace:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    client = recording_client(recorder)
    adapter = SlackChatAdapter(client, SlackStreamer(client, min_update_interval_s=0.0))
    received: list[Any] = []

    async def handler(msg: Any) -> None:
        received.append(msg)

    adapter.on_message(handler)
    store, gate, _e = make_gate(sink)
    linker = asyncio.run(approver_linker(sink))
    approval_service = ApprovalActionService(gate, linker, ("role:approver",), sink)
    mirror = SlackApprovalMirror(client)
    captured: list[Any] = []

    def spawn(coro: Any) -> Any:
        captured.append(coro)
        return coro

    app = create_slack_ingress(
        settings=chat_settings(),
        adapter=adapter,
        approval_service=approval_service,
        mirror=mirror,
        clock=clock,
        spawn=spawn,
    )
    return SimpleNamespace(
        client=TestClient(app),
        captured=captured,
        received=received,
        sink=sink,
        recorder=recorder,
        gate=gate,
        store=store,
    )


def _post_event(ctx: SimpleNamespace, raw: bytes, headers: dict[str, str]) -> httpx.Response:
    hdrs = {"content-type": "application/json", **headers}
    return ctx.client.post("/slack/events", content=raw, headers=hdrs)


def _signed(raw: bytes, ts: int = int(NOW)) -> dict[str, str]:
    return sign(raw, ts)


def _drain(ctx: SimpleNamespace) -> None:
    for coro in ctx.captured:
        asyncio.run(coro)


# --- events: signature rejection BEFORE any processing ----------------------


def test_unsigned_event_rejected_401_before_any_processing() -> None:
    ctx = _build()
    raw = load_fixture_bytes("message_im_event.json")
    resp = _post_event(ctx, raw, {})  # no signature headers
    assert resp.status_code == 401
    assert ctx.captured == []  # spawn never ran
    assert ctx.received == []  # handler never reached
    assert ctx.sink.events == []  # a forgery is NOT audited


def test_forged_signature_rejected_401() -> None:
    ctx = _build()
    raw = load_fixture_bytes("message_im_event.json")
    forged = {"X-Slack-Request-Timestamp": str(int(NOW)), "X-Slack-Signature": "v0=" + "0" * 64}
    resp = _post_event(ctx, raw, forged)
    assert resp.status_code == 401
    assert ctx.captured == [] and ctx.received == []


def test_stale_signature_rejected_401() -> None:
    ctx = _build()
    raw = load_fixture_bytes("message_im_event.json")
    resp = _post_event(ctx, raw, _signed(raw, ts=int(NOW - 10_000)))  # valid sig, stale ts
    assert resp.status_code == 401
    assert ctx.captured == []


def test_rejection_body_is_status_only() -> None:
    ctx = _build()
    raw = load_fixture_bytes("message_im_event.json")
    resp = _post_event(ctx, raw, {})
    body = resp.json()
    assert body == {"detail": "invalid request signature"}
    # No secret / bot token / config echoed.
    assert TEST_SIGNING_SECRET not in resp.text
    assert TEST_BOT_TOKEN not in resp.text


def test_url_verification_challenge_requires_valid_signature() -> None:
    ctx = _build()
    raw = load_fixture_bytes("url_verification.json")
    # forged -> 401
    forged = {"X-Slack-Request-Timestamp": str(int(NOW)), "X-Slack-Signature": "v0=bad"}
    assert _post_event(ctx, raw, forged).status_code == 401
    # valid -> challenge echoed
    resp = _post_event(ctx, raw, _signed(raw))
    assert resp.status_code == 200
    assert resp.json()["challenge"] == load_fixture("url_verification.json")["challenge"]


def test_valid_event_acks_200_and_dispatches_inbound_message() -> None:
    ctx = _build()
    raw = load_fixture_bytes("message_im_event.json")
    resp = _post_event(ctx, raw, _signed(raw))
    assert resp.status_code == 200
    assert len(ctx.captured) == 1  # scheduled exactly one dispatch
    _drain(ctx)
    assert len(ctx.received) == 1
    fixture = load_fixture("message_im_event.json")["event"]
    msg = ctx.received[0]
    assert msg.channel == fixture["channel"]
    assert msg.sender == fixture["user"]
    assert msg.text == fixture["text"]


def test_app_mention_event_dispatches() -> None:
    ctx = _build()
    raw = load_fixture_bytes("app_mention_event.json")
    assert _post_event(ctx, raw, _signed(raw)).status_code == 200
    _drain(ctx)
    assert len(ctx.received) == 1


def test_duplicate_event_id_processed_once() -> None:
    ctx = _build()
    raw = load_fixture_bytes("message_im_event.json")
    assert _post_event(ctx, raw, _signed(raw)).status_code == 200
    assert _post_event(ctx, raw, _signed(raw)).status_code == 200  # same event_id
    assert len(ctx.captured) == 1  # deduped: only ONE dispatch scheduled
    _drain(ctx)
    assert len(ctx.received) == 1


def test_bot_message_ignored() -> None:
    ctx = _build()
    payload = load_fixture("message_im_event.json")
    payload["event"]["bot_id"] = "B0BOT00000"
    raw = json.dumps(payload).encode()
    assert _post_event(ctx, raw, _signed(raw)).status_code == 200
    assert ctx.captured == []  # a bot message is not routed to the agent


# --- interactions -----------------------------------------------------------


def _post_interaction(ctx: SimpleNamespace, payload: dict[str, Any]) -> httpx.Response:
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = {"content-type": "application/x-www-form-urlencoded", **_signed(body)}
    return ctx.client.post("/slack/interactions", content=body, headers=headers)


def test_interaction_unsigned_rejected_401() -> None:
    ctx = _build()
    body = urlencode({"payload": json.dumps(load_fixture("block_action_approve.json"))}).encode()
    resp = ctx.client.post(
        "/slack/interactions",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 401
    assert ctx.captured == []


def test_interaction_malformed_payload_acked_and_ignored() -> None:
    ctx = _build()
    resp = _post_interaction(ctx, {"type": "not_block_actions"})
    assert resp.status_code == 200
    assert ctx.captured == []  # unknown shape: ack and ignore, nothing scheduled


def test_interaction_undecodable_body_acked_and_ignored() -> None:
    ctx = _build()
    body = b"\xff\xfe not utf-8"
    headers = {"content-type": "application/x-www-form-urlencoded", **_signed(body)}
    resp = ctx.client.post("/slack/interactions", content=body, headers=headers)
    assert resp.status_code == 200
    assert ctx.captured == []


def test_interactions_route_never_uses_starlette_form_parsing() -> None:
    """Regression lock (Codex NE-063 P2): Starlette's ``request.form()`` requires
    the ``python-multipart`` package for ANY form parsing — including
    application/x-www-form-urlencoded — and ne-chat does not declare it. The
    interactivity route must parse the urlencoded body from the raw bytes with
    the stdlib only; ``.form(`` must never be reintroduced in ne.chat."""
    import inspect

    from ne.chat.slack import ingress as ingress_module

    source = inspect.getsource(ingress_module)
    assert ".form(" not in source
