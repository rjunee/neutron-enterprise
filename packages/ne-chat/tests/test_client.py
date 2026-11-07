# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackChatClient — air-gap/SSRF construction guards, method allowlist, C12."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.chat.errors import SlackApiError
from ne.chat.slack.client import SlackChatClient

from .conftest import chat_settings, recording_client, slack_recording_transport

_HOSTILE = [
    "http://slack.com/api",  # not https
    "https://169.254.169.254/api",  # metadata IP literal
    "https://2130706433/api",  # packed-integer IPv4
    "https://foo@slack.com/api",  # embedded credentials
    "https://slack.evil.example.com/api",  # https but off-allowlist
]


def test_air_gap_refuses_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    recorder: list[httpx.Request] = []
    with pytest.raises(SlackApiError):
        SlackChatClient(chat_settings(), transport=slack_recording_transport(recorder))
    assert recorder == []  # construction fails before any I/O


@pytest.mark.parametrize("hostile", _HOSTILE)
def test_rejects_hostile_base_url(hostile: str) -> None:
    recorder: list[httpx.Request] = []
    with pytest.raises(SlackApiError):
        SlackChatClient(
            chat_settings(api_base_url=hostile), transport=slack_recording_transport(recorder)
        )
    assert recorder == []  # the exfil-never-POSTs proof


def test_method_allowlist_enforced() -> None:
    recorder: list[httpx.Request] = []
    client = recording_client(recorder)

    async def scenario() -> None:
        with pytest.raises(SlackApiError, match="disallowed"):
            await client.call("conversations.history", {"channel": "C1"})

    asyncio.run(scenario())
    assert recorder == []  # never left the process


def test_bearer_auth_and_json_body_shape() -> None:
    recorder: list[httpx.Request] = []
    client = recording_client(recorder)

    async def scenario() -> dict[str, object]:
        return await client.call("chat.postMessage", {"channel": "C1", "text": "hi"})

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert len(recorder) == 1
    req = recorder[0]
    assert req.method == "POST"
    assert str(req.url) == "https://slack.com/api/chat.postMessage"
    assert req.headers["authorization"] == "Bearer xoxb-test"
    assert req.headers["content-type"] == "application/json"
    body = req.content.decode()
    assert '"channel"' in body and '"C1"' in body and '"hi"' in body


def test_retry_after_honored_and_capped() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True, "ts": "1.1"})

    client = SlackChatClient(chat_settings(), transport=httpx.MockTransport(handler))

    async def scenario() -> dict[str, object]:
        return await client.call("chat.update", {"channel": "C1", "ts": "1.1"})

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert calls["n"] == 2  # one retry after the 429


def test_ok_false_raises_code_only_and_c12() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = SlackChatClient(chat_settings(), transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        try:
            await client.call("chat.postMessage", {"channel": "C1", "text": "secret-body-text"})
        except SlackApiError as exc:
            assert exc.error_code == "channel_not_found"
            assert exc.host == "slack.com"
            # C12: token / params / body never leak into the error.
            text = str(exc)
            assert "xoxb-test" not in text
            assert "secret-body-text" not in text
        else:  # pragma: no cover
            raise AssertionError("expected SlackApiError")

    asyncio.run(scenario())


def test_http_error_carries_status_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = SlackChatClient(chat_settings(), transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        with pytest.raises(SlackApiError) as ei:
            await client.call("chat.postMessage", {"channel": "C1", "text": "x"})
        assert ei.value.status == 500
        assert "xoxb-test" not in str(ei.value)

    asyncio.run(scenario())
