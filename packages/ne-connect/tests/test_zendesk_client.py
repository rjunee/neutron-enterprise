# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""ZendeskClient: air-gap/SSRF refusal, path guard, Basic auth, cursor safety (NE-093)."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError
from ne.connect.errors import ZendeskSyncError
from ne.connect.zendesk.client import _RETRYABLE, ZendeskClient

from .zendesk_transport import build_zendesk_transport, recording_transport, zendesk_settings


def test_air_gap_aborts_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    recorder: list[httpx.Request] = []
    with pytest.raises(AirGapViolationError):
        ZendeskClient(zendesk_settings(), transport=build_zendesk_transport(recorder=recorder))
    assert recorder == []


@pytest.mark.parametrize(
    "hostile",
    [
        "http://acme.zendesk.com",
        "https://169.254.169.254",
        "https://2130706433",
        "https://acme.zendesk.com.evil.example.com",
        "https://evil.example.com",
    ],
)
def test_construction_refuses_hostile_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        ZendeskClient(zendesk_settings(base_url=hostile))


def test_rejected_endpoint_never_emits_a_request() -> None:
    recorder: list[httpx.Request] = []
    transport = httpx.MockTransport(recording_transport(recorder))
    with pytest.raises(UnsafeEndpointError):
        ZendeskClient(zendesk_settings(base_url="https://169.254.169.254"), transport=transport)
    assert recorder == []


def test_basic_auth_header_uses_email_slash_token() -> None:
    settings = zendesk_settings(email="svc@acme.test", api_token="tok-123")
    recorder: list[httpx.Request] = []
    client = ZendeskClient(settings, transport=build_zendesk_transport(recorder=recorder))

    async def scenario() -> None:
        async with client.session() as http:
            await client.incremental_tickets(http, start_time=1, cursor=None)

    asyncio.run(scenario())
    auth = recorder[0].headers["Authorization"]
    assert auth.startswith("Basic ")
    assert base64.b64decode(auth.removeprefix("Basic ")).decode() == "svc@acme.test/token:tok-123"


def test_path_guard_refuses_off_prefix_path() -> None:
    recorder: list[httpx.Request] = []
    client = ZendeskClient(zendesk_settings(), transport=build_zendesk_transport(recorder=recorder))

    async def scenario() -> None:
        async with client.session() as http:
            await client._get_json(http, "/evil/path")

    with pytest.raises(ZendeskSyncError):
        asyncio.run(scenario())
    assert recorder == []


def test_hostile_after_url_and_next_page_are_never_fetched() -> None:
    recorder: list[httpx.Request] = []
    client = ZendeskClient(zendesk_settings(), transport=build_zendesk_transport(recorder=recorder))

    async def scenario() -> bool:
        async with client.session() as http:
            _tickets, after, end = await client.incremental_tickets(http, start_time=1, cursor=None)
            assert after == "curs2"  # the opaque cursor, NOT the after_url
            assert end is False
            _, _, end2 = await client.incremental_tickets(http, start_time=None, cursor=after)
            return end2

    assert asyncio.run(scenario()) is True
    # Every request went to OUR pinned base; the response-embedded hostile
    # after_url / next_page (evil.example.com) was NEVER requested.
    assert all(r.url.host == "acme.zendesk.com" for r in recorder)
    assert not any("evil" in str(r.url) for r in recorder)


def test_malformed_cursor_is_refused() -> None:
    client = ZendeskClient(zendesk_settings(), transport=build_zendesk_transport())

    async def scenario() -> None:
        async with client.session() as http:
            await client.incremental_tickets(http, start_time=None, cursor="bad cursor!!")

    with pytest.raises(ZendeskSyncError):
        asyncio.run(scenario())


def test_429_honors_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = ZendeskClient(zendesk_settings(), transport=build_zendesk_transport(one_shot_429=True))

    async def scenario() -> bool:
        async with client.session() as http:
            _, _, end = await client.incremental_tickets(http, start_time=1, cursor=None)
            return end

    asyncio.run(scenario())
    assert slept == [1.0]


def test_transient_5xx_are_retryable() -> None:
    assert {429, 500, 502, 503, 504} <= _RETRYABLE
    assert 501 not in _RETRYABLE


def test_retry_exhaustion_raises_status_only_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = zendesk_settings(max_attempts=2, retry_after_cap_seconds=1)
    client = ZendeskClient(settings, transport=build_zendesk_transport(fail_after=0))

    async def scenario() -> None:
        async with client.session() as http:
            await client.incremental_tickets(http, start_time=1, cursor=None)

    with pytest.raises(ZendeskSyncError) as exc:
        asyncio.run(scenario())
    assert exc.value.status == 500


def test_error_message_excludes_secret() -> None:
    settings = zendesk_settings(max_attempts=1, api_token="super-secret-zendesk-token")
    client = ZendeskClient(settings, transport=build_zendesk_transport(fail_after=0))

    async def scenario() -> None:
        async with client.session() as http:
            await client.incremental_tickets(http, start_time=1, cursor=None)

    with pytest.raises(ZendeskSyncError) as exc:
        asyncio.run(scenario())
    assert "super-secret-zendesk-token" not in str(exc.value)
