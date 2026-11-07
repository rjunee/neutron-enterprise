# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""TeamsChatClient — air-gap/SSRF construction guards, token cache, JWKS discovery."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ne.chat.errors import ActivityAuthError, TeamsApiError
from ne.chat.teams.client import TeamsChatClient
from ne.chat.teams.refs import ConversationRefs

from .teams_support import (
    APP_ID,
    CONVERSATION_ID,
    JWKS,
    JWKS_URI,
    KID,
    METADATA_URL,
    SERVICE_URL,
    recording_teams_client,
    teams_chat_settings,
    teams_recording_transport,
)


def test_air_gap_refuses_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    recorder: list[httpx.Request] = []
    with pytest.raises(TeamsApiError):
        TeamsChatClient(
            teams_chat_settings(),
            ConversationRefs(max_size=10),
            transport=teams_recording_transport(recorder),
        )
    assert recorder == []  # construction fails before any I/O


def test_off_allowlist_login_url_rejected() -> None:
    with pytest.raises(TeamsApiError):
        TeamsChatClient(
            teams_chat_settings(login_base_url="https://login.evil.example.com"),
            ConversationRefs(max_size=10),
        )


def test_off_allowlist_metadata_url_rejected() -> None:
    with pytest.raises(TeamsApiError):
        TeamsChatClient(
            teams_chat_settings(
                openid_metadata_url="https://evil.example.com/.well-known/openidconfiguration"
            ),
            ConversationRefs(max_size=10),
        )


def test_post_activity_to_unknown_conversation_raises() -> None:
    recorder: list[httpx.Request] = []
    client, _refs = recording_teams_client(recorder, seed_conversation=False)

    async def scenario() -> None:
        with pytest.raises(TeamsApiError):
            await client.post_activity("never-seen", {"type": "message", "text": "hi"})

    asyncio.run(scenario())


def test_off_allowlist_service_url_at_call_time_rejected() -> None:
    recorder: list[httpx.Request] = []
    refs = ConversationRefs(max_size=10)
    refs.record("conv-bad", "https://evil.example.com/amer")  # forced, non-allowlisted
    client, _ = recording_teams_client(recorder, refs, seed_conversation=False)

    async def scenario() -> None:
        with pytest.raises(TeamsApiError):
            await client.post_activity("conv-bad", {"type": "message", "text": "hi"})

    asyncio.run(scenario())


def test_token_cached_across_two_calls() -> None:
    recorder: list[httpx.Request] = []
    client, _refs = recording_teams_client(recorder)

    async def scenario() -> None:
        await client.post_activity(CONVERSATION_ID, {"type": "message", "text": "one"})
        await client.post_activity(CONVERSATION_ID, {"type": "message", "text": "two"})

    asyncio.run(scenario())
    token_posts = [r for r in recorder if str(r.url).endswith("/oauth2/v2.0/token")]
    assert len(token_posts) == 1


def test_connector_error_status_and_host_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3599})
        return httpx.Response(500)

    refs = ConversationRefs(max_size=10)
    refs.record(CONVERSATION_ID, SERVICE_URL.rstrip("/"))
    client = TeamsChatClient(
        teams_chat_settings(requests_per_minute=6000.0, max_attempts=1),
        refs,
        transport=httpx.MockTransport(handler),
    )

    async def scenario() -> None:
        with pytest.raises(TeamsApiError) as exc:
            await client.post_activity(CONVERSATION_ID, {"type": "message", "text": "x"})
        text = str(exc.value)
        assert exc.value.status == 500
        assert exc.value.host == "smba.trafficmanager.net"
        assert teams_chat_settings().app_secret.get_secret_value() not in text

    asyncio.run(scenario())


def test_get_signing_key_happy_path_and_ttl_cache() -> None:
    recorder: list[httpx.Request] = []
    client, _refs = recording_teams_client(recorder)

    async def scenario() -> None:
        key1 = await client.get_signing_key(KID)
        key2 = await client.get_signing_key(KID)
        assert key1.key_id == KID
        assert key2.key_id == KID

    asyncio.run(scenario())
    metadata_gets = [r for r in recorder if str(r.url) == METADATA_URL]
    assert len(metadata_gets) == 1  # TTL cache: metadata fetched once


def test_get_signing_key_discovery_substitution_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == METADATA_URL:
            # A hostile metadata doc pointing jwks_uri off the allowlist host.
            return httpx.Response(200, json={"jwks_uri": "https://evil.example.com/keys"})
        if url == "https://evil.example.com/keys":
            raise AssertionError("the off-allowlist jwks_uri must never be fetched")
        if url == JWKS_URI:
            return httpx.Response(200, json=JWKS)
        return httpx.Response(404)  # pragma: no cover

    client = TeamsChatClient(
        teams_chat_settings(), ConversationRefs(max_size=10), transport=httpx.MockTransport(handler)
    )

    async def scenario() -> None:
        with pytest.raises(ActivityAuthError):
            await client.get_signing_key(KID)

    asyncio.run(scenario())


def test_get_signing_key_unknown_kid_rejected() -> None:
    recorder: list[httpx.Request] = []
    client, _refs = recording_teams_client(recorder)

    async def scenario() -> None:
        with pytest.raises(ActivityAuthError):
            await client.get_signing_key("no-such-kid")

    asyncio.run(scenario())


def test_valid_activity_token_audience_is_app_id() -> None:
    # sanity: the fixture app id matches the settings used across the suite.
    assert teams_chat_settings().app_id == APP_ID
