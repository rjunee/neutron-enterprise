# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Slack background-task exception logging (NE-CLEAN3 item 1 / NE-063 P3).

Both ingress background dispatch sites (events -> ``adapter.dispatch_inbound``;
interactions -> ``_action_followup``) are wrapped by ``_log_failures``, which
logs an unhandled exception at ERROR via the package logger, with task name +
id-only context, then RE-RAISES unchanged — the exception still propagates to
whatever awaits the task; nothing is swallowed-then-continued. This also proves
no secret/token ever lands in the log text (C12).
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from ne.chat.approvals import ApprovalActionService
from ne.chat.slack.adapter import SlackChatAdapter
from ne.chat.slack.ingress import _default_spawn, _log_failures, create_slack_ingress
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
_LOGGER_NAME = "ne.chat.slack.ingress"


def _signed(raw: bytes, ts: int = int(NOW)) -> dict[str, str]:
    return sign(raw, ts)


class _RaisingApprovalService:
    """An approval-action double with the real signature that always raises."""

    async def act(
        self, *, external_user_id: str, request_id: str, decision: str
    ) -> Any:  # pragma: no cover - never returns
        raise RuntimeError("boom-approval")


def _build(*, approval_service: Any = None, raise_on_message: bool = False) -> SimpleNamespace:
    sink = RecordingAuditSink()
    recorder: list[httpx.Request] = []
    client = recording_client(recorder)
    adapter = SlackChatAdapter(client, SlackStreamer(client, min_update_interval_s=0.0))

    if raise_on_message:

        async def handler(msg: Any) -> None:
            raise RuntimeError("boom-inbound")

        adapter.on_message(handler)

    store, gate, _e = make_gate(sink)
    linker = asyncio.run(approver_linker(sink))
    svc = (
        approval_service
        if approval_service is not None
        else ApprovalActionService(gate, linker, ("role:approver",), sink)
    )
    mirror = SlackApprovalMirror(client)
    captured: list[Any] = []

    def spawn(coro: Any) -> Any:
        captured.append(coro)
        return coro

    app = create_slack_ingress(
        settings=chat_settings(),
        adapter=adapter,
        approval_service=svc,
        mirror=mirror,
        clock=lambda: NOW,
        spawn=spawn,
    )
    return SimpleNamespace(client=TestClient(app), captured=captured, sink=sink, store=store)


def test_raising_bg_task_exception_is_logged_with_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _build(raise_on_message=True)
    raw = load_fixture_bytes("message_im_event.json")
    resp = ctx.client.post(
        "/slack/events",
        content=raw,
        headers={"content-type": "application/json", **_signed(raw)},
    )
    assert resp.status_code == 200
    assert len(ctx.captured) == 1

    fixture_event = load_fixture("message_im_event.json")["event"]
    caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
    with pytest.raises(RuntimeError, match="boom-inbound"):
        asyncio.run(ctx.captured[0])

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert "slack.dispatch_inbound" in message
    assert fixture_event["channel"] in message
    assert fixture_event["user"] in message
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


def test_raising_approval_followup_is_logged_with_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = _build(approval_service=_RaisingApprovalService())
    payload = load_fixture("block_action_approve.json")
    request_value = payload["actions"][0]["value"]
    clicker = payload["user"]["id"]
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = {"content-type": "application/x-www-form-urlencoded", **_signed(body)}
    resp = ctx.client.post("/slack/interactions", content=body, headers=headers)
    assert resp.status_code == 200
    assert len(ctx.captured) == 1

    caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
    with pytest.raises(RuntimeError, match="boom-approval"):
        asyncio.run(ctx.captured[0])

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert "slack.approval_action" in message
    assert clicker in message
    assert request_value in message
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


def test_bg_task_log_never_echoes_secrets(caplog: pytest.LogCaptureFixture) -> None:
    ctx = _build(raise_on_message=True)
    raw = load_fixture_bytes("message_im_event.json")
    resp = ctx.client.post(
        "/slack/events",
        content=raw,
        headers={"content-type": "application/json", **_signed(raw)},
    )
    assert resp.status_code == 200

    caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
    with pytest.raises(RuntimeError):
        asyncio.run(ctx.captured[0])

    assert TEST_SIGNING_SECRET not in caplog.text
    assert TEST_BOT_TOKEN not in caplog.text


def test_default_spawn_task_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def raiser() -> None:
        raise RuntimeError("boom-default")

    async def scenario() -> None:
        caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)
        task = _default_spawn(_log_failures(raiser(), task_name="t"))
        assert isinstance(task, asyncio.Task)
        with pytest.raises(RuntimeError, match="boom-default"):
            await task

    asyncio.run(scenario())
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1
    assert "t" in records[0].getMessage()
