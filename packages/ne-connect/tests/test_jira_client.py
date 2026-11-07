# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""JiraClient: air-gap/SSRF refusal, path guard, Basic auth, retries, C12 hygiene (NE-093)."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError
from ne.connect.errors import JiraSyncError
from ne.connect.jira.client import _RETRYABLE, JiraClient

from .jira_transport import build_jira_transport, jira_settings


def test_air_gap_aborts_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_GAP", "1")
    recorder: list[httpx.Request] = []
    with pytest.raises(AirGapViolationError):
        JiraClient(jira_settings(), transport=build_jira_transport(recorder=recorder))
    assert recorder == []


@pytest.mark.parametrize(
    "hostile",
    [
        "http://acme.atlassian.net",  # not https
        "https://169.254.169.254",  # metadata IP literal
        "https://2130706433",  # packed-integer IPv4
        "https://acme.atlassian.net.evil.example.com",  # dot-suffix bypass attempt
        "https://evil.example.com",  # https but off-allowlist
    ],
)
def test_construction_refuses_hostile_endpoint(hostile: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        JiraClient(jira_settings(base_url=hostile))


def test_basic_auth_header_carries_email_and_token() -> None:
    settings = jira_settings(email="svc@acme.test", api_token="tok-123")
    recorder: list[httpx.Request] = []
    client = JiraClient(settings, transport=build_jira_transport(recorder=recorder))

    async def scenario() -> None:
        async with client.session() as http:
            await client.project(http, "ACME")

    asyncio.run(scenario())
    assert recorder
    auth = recorder[0].headers["Authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
    assert decoded == "svc@acme.test:tok-123"


def test_guard_path_refuses_absolute_url_before_request() -> None:
    recorder: list[httpx.Request] = []
    client = JiraClient(jira_settings(), transport=build_jira_transport(recorder=recorder))

    async def scenario() -> None:
        async with client.session() as http:
            await client.get_json(http, "https://evil.example.com/rest/api/3/project/ACME")

    with pytest.raises(JiraSyncError):
        asyncio.run(scenario())
    assert recorder == []


def test_guard_path_refuses_off_prefix_path() -> None:
    recorder: list[httpx.Request] = []
    client = JiraClient(jira_settings(), transport=build_jira_transport(recorder=recorder))

    async def scenario() -> None:
        async with client.session() as http:
            await client.get_json(http, "/evil/path")

    with pytest.raises(JiraSyncError):
        asyncio.run(scenario())
    assert recorder == []


def test_malformed_issue_key_refused_before_request() -> None:
    recorder: list[httpx.Request] = []
    client = JiraClient(jira_settings(), transport=build_jira_transport(recorder=recorder))

    async def scenario() -> None:
        async with client.session() as http:
            await client.issue_comments(http, "ACME-1/../../secret")

    with pytest.raises(JiraSyncError):
        asyncio.run(scenario())
    assert recorder == []


def test_429_honors_retry_after_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = JiraClient(jira_settings(), transport=build_jira_transport(one_shot_429=True))

    async def scenario() -> dict[str, object]:
        async with client.session() as http:
            return await client.project(http, "ACME")

    result = asyncio.run(scenario())
    assert result["key"] == "ACME"
    assert slept == [1.0]


def test_transient_5xx_are_retryable() -> None:
    assert {429, 500, 502, 503, 504} <= _RETRYABLE
    assert 501 not in _RETRYABLE


def test_retry_exhaustion_raises_status_only_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    settings = jira_settings(max_attempts=2, retry_after_cap_seconds=1)
    client = JiraClient(settings, transport=build_jira_transport(fail_after=0))

    async def scenario() -> None:
        async with client.session() as http:
            await client.project(http, "ACME")

    with pytest.raises(JiraSyncError) as exc:
        asyncio.run(scenario())
    assert exc.value.status == 500


def test_401_is_status_only_error_no_refresh() -> None:
    # Jira Basic auth has no token-refresh flow — a 401 is a plain status-only error.
    client = JiraClient(jira_settings(), transport=build_jira_transport(respond_401_once=True))

    async def scenario() -> None:
        async with client.session() as http:
            await client.project(http, "ACME")

    with pytest.raises(JiraSyncError) as exc:
        asyncio.run(scenario())
    assert exc.value.status == 401


def test_search_jql_paginates_via_next_page_token() -> None:
    client = JiraClient(jira_settings(), transport=build_jira_transport())

    async def scenario() -> list[dict[str, object]]:
        async with client.session() as http:
            return await client.search_jql(
                http, "project = ACME ORDER BY updated ASC", ["summary", "updated", "security"]
            )

    issues = asyncio.run(scenario())
    assert {i["key"] for i in issues} == {"ACME-1", "ACME-2", "ACME-3"}


def test_error_message_excludes_secret_and_query_string() -> None:
    settings = jira_settings(
        max_attempts=1, api_token="super-secret-jira-token", email="svc@acme.test"
    )
    client = JiraClient(settings, transport=build_jira_transport(fail_after=0))

    async def scenario() -> None:
        async with client.session() as http:
            await client.search_jql(http, "project = ACME ORDER BY updated ASC", ["summary"])

    with pytest.raises(JiraSyncError) as exc:
        asyncio.run(scenario())
    message = str(exc.value)
    assert "super-secret-jira-token" not in message
    assert "project = ACME" not in message
    assert "?" not in message
