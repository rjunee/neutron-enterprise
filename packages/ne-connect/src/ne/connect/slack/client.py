# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Thin Slack Web API HTTP client (NE-045) — one of exactly two httpx sites in ne.connect.

:class:`SlackClient` is a deliberately small bot-token client (NO ``slack_sdk`` —
see the build-vs-adopt rationale in the unit's commit message: its own
urllib/aiohttp transport stack would bypass both the C2 egress guardian and the
mandated ``httpx.MockTransport`` fixture-first CI). It owns the judgment-dense
surface: fail-closed endpoint validation, a method allowlist (defense-in-depth —
Slack pagination cursors are opaque query params, never followed URLs, so there
is no response-supplied-link SSRF surface here, unlike Graph's ``nextLink``), two
adaptive throttles (history reads are far more aggressively rate-limited than
general reads post-2026-03), and Slack's ``ok:false`` / ``Retry-After`` / HTTP
status retry semantics.

Retry-exhaustion semantics (NE-CLEAN3 item 2): HTTP 429 exhaustion and the
``ok: false error: 'ratelimited'`` envelope's exhaustion both raise
:class:`SlackRateLimitedError` — a retryable, per-item signal the connector
downgrades to a per-channel skip rather than a run abort. 5xx exhaustion and
every OTHER ``ok: false`` code (including the auth/permission codes
``invalid_auth`` / ``not_authed`` / ``account_inactive`` / ``missing_scope``)
keep raising the plain :class:`SlackSyncError` with NO retry, exactly as
before — those still abort the whole run.

C2 (the security spine): the constructor validates the configured base URL and
refuses to build under ``AIR_GAP`` BEFORE any I/O. The validated base is pinned
to the configured ``api_allowed_hosts`` — this client never follows a
response-supplied URL (Slack's ``cursor`` is an opaque param we attach to our
OWN validated base), so there is no off-host redirect surface to re-guard on
every call the way the Graph client must.

C12: the bot token appears only in the ``Authorization`` header; failures raise
:class:`SlackSyncError` with the STATUS / short Slack error CODE / host only —
never a token, request params, or a response body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx

from ne.connect.endpoints import AirGapViolationError, UnsafeEndpointError, validate_source_url
from ne.connect.errors import SlackRateLimitedError, SlackSyncError
from ne.connect.slack.config import SlackConnectorSettings
from ne.connect.throttle import AdaptiveThrottle
from ne.net.airgap import air_gap_engaged
from ne.net.ssrf import normalize_host

#: Re-exported transport alias so the connector/sync modules can type their
#: injectable transport WITHOUT importing httpx — keeping client.py (and
#: m365/graph.py) the only httpx import sites under ``ne/connect`` (locked by
#: the C2 egress guardian test).
type Transport = httpx.AsyncBaseTransport
#: The concrete client type :meth:`SlackClient.session` yields.
type Client = httpx.AsyncClient

__all__ = ["Client", "SlackClient", "Transport"]

_RETRYABLE = frozenset({429, 503, 504})
_HISTORY_METHODS = frozenset({"conversations.history", "conversations.replies"})
_ALLOWED_METHODS = frozenset(
    {
        "conversations.list",
        "conversations.history",
        "conversations.replies",
        "conversations.members",
        "users.list",
    }
)


class SlackClient:
    """Slack Web API client: SSRF-guarded, throttled, retried GET-with-token-auth."""

    def __init__(
        self,
        settings: SlackConnectorSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # (a) Fail-closed BEFORE any I/O — Slack is egress.
        if air_gap_engaged():
            raise AirGapViolationError(
                "AIR_GAP engaged: the Slack connector is egress and is not permitted"
            )
        # (b) Validate the base URL (https / no IP / no packed-IPv4 / no metadata).
        api_base = validate_source_url(settings.api_base_url, allow_external=True)
        # (c) Close the external allowlist gap (validate_source_url has none).
        host = normalize_host(urlparse(api_base).hostname or "")
        if host not in frozenset(settings.api_allowed_hosts):
            raise UnsafeEndpointError(
                f"api_base_url host {host!r} is not in the configured allowlist"
            )
        self._base = api_base
        self._host = host
        self._settings = settings
        self._transport = transport
        self._history_throttle = AdaptiveThrottle(settings.history_requests_per_minute / 60.0, 2)
        self._general_throttle = AdaptiveThrottle(settings.requests_per_minute / 60.0, 2)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[httpx.AsyncClient]:
        """Open ONE httpx client for a crawl (timeout + injected transport)."""
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            yield client

    async def call(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call one Slack Web API ``method``; throttled, retried, status/ok-aware.

        The method allowlist pins the path segment (defense-in-depth): every
        request URL is built ONLY from the validated ``self._base``, and Slack
        pagination cursors are opaque query params (never a response-supplied
        URL), so there is no followed-link SSRF surface to re-guard here.
        """
        if method not in _ALLOWED_METHODS:
            raise SlackSyncError(f"refusing to call disallowed Slack method {method!r}")
        settings = self._settings
        throttle = self._history_throttle if method in _HISTORY_METHODS else self._general_throttle
        max_attempts = settings.max_attempts
        cap = settings.retry_after_cap_seconds
        token = settings.bot_token.get_secret_value()

        for attempt in range(1, max_attempts + 1):
            await throttle.acquire()
            resp = await client.get(
                f"{self._base}/{method}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            status = resp.status_code
            if status in _RETRYABLE:
                if attempt == max_attempts:
                    if status == 429:
                        raise SlackRateLimitedError(
                            f"Slack API {method} returned HTTP {status} after {attempt} attempts",
                            status=status,
                        )
                    raise SlackSyncError(
                        f"Slack API {method} returned HTTP {status} after {attempt} attempts",
                        status=status,
                    )
                await throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                continue
            if status >= 400:
                raise SlackSyncError(f"Slack API {method} returned HTTP {status}", status=status)

            payload = resp.json()
            if payload.get("ok") is not True:
                code = str(payload.get("error") or "unknown_error")
                # Slack also signals rate limiting via ok:false error="ratelimited"
                # on HTTP 200 from some proxies — treat it as retryable.
                if code == "ratelimited":
                    if attempt < max_attempts:
                        await throttle.wait_retry(attempt, resp.headers.get("Retry-After"), cap)
                        continue
                    raise SlackRateLimitedError(
                        f"Slack API {method} returned error 'ratelimited' after {attempt} attempts",
                        status=200,
                        error_code="ratelimited",
                    )
                raise SlackSyncError(
                    f"Slack API {method} returned error {code!r}", status=200, error_code=code
                )
            return payload
        raise SlackSyncError("exhausted Slack retries")  # pragma: no cover

    async def paginate(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: Mapping[str, str] | None,
        *,
        key: str,
    ) -> list[Any]:
        """Call ``method`` repeatedly, following ``response_metadata.next_cursor``.

        The Slack ``key`` collection is a list of message/channel/user OBJECTS
        for most methods but a bare list of member-id STRINGS for
        ``conversations.members`` — the return type is therefore ``list[Any]``;
        callers narrow to the shape their ``key`` actually returns.
        """
        items: list[Any] = []
        base_params = dict(params or {})
        cursor: str | None = None
        while True:
            page_params = dict(base_params)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self.call(client, method, page_params)
            page_items = payload.get(key)
            if isinstance(page_items, list):
                items.extend(page_items)
            metadata = payload.get("response_metadata")
            cursor = str(metadata.get("next_cursor") or "") if isinstance(metadata, Mapping) else ""
            if not cursor:
                break
        return items
