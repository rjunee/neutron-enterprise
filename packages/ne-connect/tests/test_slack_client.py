# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SlackClient — retry/Retry-After/ok:false semantics, method allowlist, pagination."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.connect.errors import SlackRateLimitedError, SlackSyncError
from ne.connect.slack.client import SlackClient

from .slack_transport import recording_transport, slack_settings


def _neutralize_throttles(client: SlackClient) -> None:
    """Make both token buckets free — these tests exercise retry/status logic,
    not bucket pacing (which the shared ``AdaptiveThrottle`` suite covers)."""

    async def _noop() -> None:
        return None

    client._history_throttle.acquire = _noop  # type: ignore[method-assign]
    client._general_throttle.acquire = _noop  # type: ignore[method-assign]


def _sequenced(responses: list[httpx.Response]) -> httpx.MockTransport:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        if queue:
            return queue.pop(0)
        return httpx.Response(404)  # pragma: no cover

    return httpx.MockTransport(handler)


def test_429_with_retry_after_is_honored_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = _sequenced(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429, headers={"Retry-After": "999"}),  # capped
            httpx.Response(200, json={"ok": True, "channels": []}),
        ]
    )
    client = SlackClient(slack_settings(retry_after_cap_seconds=60), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            data = await client.call(http, "conversations.list")
            assert data == {"ok": True, "channels": []}

    asyncio.run(scenario())
    assert slept == [3.0, 60.0]


def test_5xx_retried_then_raises_status_only_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = _sequenced([httpx.Response(503) for _ in range(5)])
    client = SlackClient(slack_settings(max_attempts=5), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackSyncError) as excinfo:
                await client.call(http, "conversations.list")
            assert excinfo.value.status == 503
            assert "503" in str(excinfo.value)

    asyncio.run(scenario())


def test_other_4xx_raises_status_only_error() -> None:
    transport = _sequenced([httpx.Response(403)])
    client = SlackClient(slack_settings(), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackSyncError) as excinfo:
                await client.call(http, "conversations.list")
            assert excinfo.value.status == 403

    asyncio.run(scenario())


def test_429_exhaustion_raises_rate_limited_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """NE-CLEAN3 item 2: HTTP 429 retry-budget exhaustion is a distinct,
    per-item RETRYABLE signal — ``SlackRateLimitedError`` (a ``SlackSyncError``
    subclass) — that the connector downgrades to a per-channel skip instead of
    aborting the whole run."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = _sequenced([httpx.Response(429, headers={"Retry-After": "1"}) for _ in range(5)])
    client = SlackClient(slack_settings(max_attempts=5), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackRateLimitedError) as excinfo:
                await client.call(http, "conversations.list")
            assert excinfo.value.status == 429
            assert isinstance(excinfo.value, SlackSyncError)

    asyncio.run(scenario())


def test_ok_false_ratelimited_exhaustion_raises_rate_limited_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = _sequenced(
        [httpx.Response(200, json={"ok": False, "error": "ratelimited"}) for _ in range(5)]
    )
    client = SlackClient(slack_settings(max_attempts=5), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackRateLimitedError) as excinfo:
                await client.call(http, "conversations.list")
            assert excinfo.value.error_code == "ratelimited"
            assert excinfo.value.status == 200
            assert isinstance(excinfo.value, SlackSyncError)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code", ["invalid_auth", "not_authed", "account_inactive", "missing_scope"]
)
def test_auth_errors_hard_raise_without_retry(code: str) -> None:
    """Genuine auth/permission failures keep the byte-identical, no-retry
    ``SlackSyncError`` raise — they must NEVER be classified as
    ``SlackRateLimitedError`` (that would let the connector skip-and-continue
    past an auth failure, which must instead abort the run)."""
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        return httpx.Response(200, json={"ok": False, "error": code})

    client = SlackClient(slack_settings(), transport=httpx.MockTransport(handler))
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackSyncError) as excinfo:
                await client.call(http, "conversations.list")
            assert not isinstance(excinfo.value, SlackRateLimitedError)
            assert excinfo.value.error_code == code

    asyncio.run(scenario())
    assert len(recorder) == 1  # no retry


def test_503_exhaustion_stays_plain_sync_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = _sequenced([httpx.Response(503) for _ in range(5)])
    client = SlackClient(slack_settings(max_attempts=5), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackSyncError) as excinfo:
                await client.call(http, "conversations.list")
            assert not isinstance(excinfo.value, SlackRateLimitedError)
            assert excinfo.value.status == 503

    asyncio.run(scenario())


def test_ok_false_raises_error_code_and_never_leaks_token() -> None:
    transport = _sequenced([httpx.Response(200, json={"ok": False, "error": "not_in_channel"})])
    client = SlackClient(slack_settings(), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackSyncError) as excinfo:
                await client.call(http, "conversations.history", {"channel": "C1"})
            assert excinfo.value.status == 200
            assert excinfo.value.error_code == "not_in_channel"
            assert "not_in_channel" in str(excinfo.value)
            assert "xoxb" not in str(excinfo.value)  # C12: never the token

    asyncio.run(scenario())


def test_ratelimited_ok_false_on_http_200_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    transport = _sequenced(
        [
            httpx.Response(
                200,
                json={"ok": False, "error": "ratelimited"},
                headers={"Retry-After": "2"},
            ),
            httpx.Response(200, json={"ok": True, "channels": []}),
        ]
    )
    client = SlackClient(slack_settings(), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            data = await client.call(http, "conversations.list")
            assert data["ok"] is True

    asyncio.run(scenario())
    assert slept == [2.0]


def test_disallowed_method_raises_before_any_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(recording_transport(recorder))
    client = SlackClient(slack_settings(), transport=transport)

    async def scenario() -> None:
        async with client.session() as http:
            with pytest.raises(SlackSyncError):
                await client.call(http, "chat.postMessage")

    asyncio.run(scenario())
    assert recorder == []


def test_paginate_follows_next_cursor_to_completion() -> None:
    pages = [
        httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": "a"}],
                "response_metadata": {"next_cursor": "P2"},
            },
        ),
        httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": "b"}],
                "response_metadata": {"next_cursor": ""},
            },
        ),
    ]
    transport = _sequenced(pages)
    client = SlackClient(slack_settings(), transport=transport)
    _neutralize_throttles(client)

    async def scenario() -> None:
        async with client.session() as http:
            items = await client.paginate(http, "conversations.list", {}, key="channels")
            assert items == [{"id": "a"}, {"id": "b"}]

    asyncio.run(scenario())


def test_history_methods_draw_from_history_bucket_not_general() -> None:
    client = SlackClient(
        slack_settings(history_requests_per_minute=30.0, requests_per_minute=15.0),
        transport=_sequenced([httpx.Response(200, json={"ok": True, "messages": []})]),
    )
    calls: list[str] = []
    original_history_acquire = client._history_throttle.acquire
    original_general_acquire = client._general_throttle.acquire

    async def tracked_history() -> None:
        calls.append("history")
        await original_history_acquire()

    async def tracked_general() -> None:
        calls.append("general")
        await original_general_acquire()

    client._history_throttle.acquire = tracked_history  # type: ignore[method-assign]
    client._general_throttle.acquire = tracked_general  # type: ignore[method-assign]

    async def scenario() -> None:
        async with client.session() as http:
            await client.call(http, "conversations.history", {"channel": "C1"})

    asyncio.run(scenario())
    assert calls == ["history"]


def test_replies_method_also_draws_from_history_bucket() -> None:
    client = SlackClient(
        slack_settings(),
        transport=_sequenced([httpx.Response(200, json={"ok": True, "messages": []})]),
    )
    calls: list[str] = []
    original = client._history_throttle.acquire

    async def tracked() -> None:
        calls.append("history")
        await original()

    client._history_throttle.acquire = tracked  # type: ignore[method-assign]

    async def scenario() -> None:
        async with client.session() as http:
            await client.call(http, "conversations.replies", {"channel": "C1", "ts": "1"})

    asyncio.run(scenario())
    assert calls == ["history"]


def test_list_and_members_methods_draw_from_general_bucket() -> None:
    client = SlackClient(
        slack_settings(),
        transport=_sequenced(
            [
                httpx.Response(200, json={"ok": True, "channels": []}),
                httpx.Response(200, json={"ok": True, "members": []}),
            ]
        ),
    )
    calls: list[str] = []
    original = client._general_throttle.acquire

    async def tracked() -> None:
        calls.append("general")
        await original()

    client._general_throttle.acquire = tracked  # type: ignore[method-assign]

    async def scenario() -> None:
        async with client.session() as http:
            await client.call(http, "conversations.list")
            await client.call(http, "conversations.members", {"channel": "C1"})

    asyncio.run(scenario())
    assert calls == ["general", "general"]
